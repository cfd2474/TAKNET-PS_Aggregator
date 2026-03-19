"""Database models and query helpers for the TAKNET-PS dashboard."""

import json
import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.environ.get("DB_PATH", "/data/aggregator.db")

_initialized = False


def get_db():
    """Get a database connection, initializing schema if needed."""
    global _initialized
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")

    if not _initialized:
        schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
        if os.path.exists(schema_path):
            with open(schema_path) as f:
                conn.executescript(f.read())
            conn.commit()
        # Migration: add status column to users if it doesn't exist
        try:
            conn.execute("ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT 'active'")
            conn.commit()
        except Exception:
            pass  # Column already exists
        # Migration: add user profile fields (optional metadata)
        for col in ["first_name", "last_name", "email", "phone", "agency"]:
            try:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT")
                conn.commit()
            except Exception:
                pass
        # Migration: output_format and use_cotproxy for CoT/COTProxy support
        for col, defn in [
            ("output_format", "TEXT NOT NULL DEFAULT 'as_is'"),
            ("use_cotproxy", "BOOLEAN NOT NULL DEFAULT 0"),
        ]:
            try:
                conn.execute(f"ALTER TABLE outputs ADD COLUMN {col} {defn}")
                conn.commit()
            except Exception:
                pass
        # Migration: cot_transforms table for COTProxy-style transform rules
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cot_transforms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                output_id INTEGER NOT NULL,
                domain TEXT, agency TEXT, reg TEXT, callsign TEXT, type TEXT, model TEXT,
                hex TEXT NOT NULL, cot TEXT, icon TEXT, remarks TEXT, video TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (output_id) REFERENCES outputs(id) ON DELETE CASCADE
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cot_transforms_output ON cot_transforms(output_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cot_transforms_hex ON cot_transforms(output_id, hex)")
        conn.commit()
        # Migration: remarks on cot_transforms (COTProxy parity — CoT <remarks>)
        try:
            conn.execute("ALTER TABLE cot_transforms ADD COLUMN remarks TEXT")
            conn.commit()
        except Exception:
            pass
        # Migration: video on cot_transforms (CoT __video element)
        try:
            conn.execute("ALTER TABLE cot_transforms ADD COLUMN video TEXT")
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(
                "ALTER TABLE feeders ADD COLUMN owners TEXT NOT NULL DEFAULT '[]'"
            )
            conn.commit()
        except Exception:
            pass
        # Migration: CoT push TLS certs (encrypted at rest)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS output_cot_certs (
                output_id INTEGER PRIMARY KEY,
                cert_encrypted TEXT NOT NULL,
                key_encrypted TEXT NOT NULL,
                ca_encrypted TEXT,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (output_id) REFERENCES outputs(id) ON DELETE CASCADE
            )
        """)
        conn.commit()
        _initialized = True

    return conn


def dict_row(row):
    """Convert sqlite3.Row to dict."""
    if row is None:
        return None
    return dict(row)


def dict_rows(rows):
    """Convert list of sqlite3.Row to list of dict."""
    return [dict(r) for r in rows]


def parse_mlat_client_name(name):
    """Parse MLAT client name into (display_name, software_version).
    Tries separators: ' | v' (e.g. '92882-corona-feeder-1 | v2.59.33'), then '___v' (e.g. '92563-Leckliter___v2.59.34').
    If none match, return (name, '') with version blank.
    """
    if not name or not isinstance(name, str):
        return (name or "", "")
    for sep in (" | v", "___v"):
        if sep in name:
            parts = name.split(sep, 1)
            return (parts[0].strip(), (parts[1].strip() if len(parts) > 1 else ""))
    return (name.strip(), "")


def parse_feeder_owners(raw):
    """Parse feeders.owners (JSON array string or list) into a list of usernames."""
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    try:
        d = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(d, list):
            return [str(x).strip() for x in d if str(x).strip()]
    except Exception:
        pass
    return []


def user_can_access_feeder(feeder, username, role):
    """Admin: all feeders. Others: only if username is in owners (non-empty). Unassigned feeders: admin only."""
    if not feeder:
        return False
    if role == "admin":
        return True
    owners = parse_feeder_owners(feeder.get("owners"))
    if not owners:
        return False
    u = (username or "").strip().lower()
    return any(u == (o or "").strip().lower() for o in owners)


def filter_feeders_for_user(feeders, username, role):
    if role == "admin":
        return list(feeders)
    return [f for f in feeders if user_can_access_feeder(f, username, role)]


def feeder_stats_from_rows(rows):
    """Same shape as FeederModel.get_stats() but for a subset of feeder rows."""
    rows = list(rows)
    total = len(rows)
    active = sum(1 for f in rows if f.get("status") == "active")
    stale = sum(1 for f in rows if f.get("status") == "stale")
    offline = sum(1 for f in rows if f.get("status") == "offline")
    by_ct = {}
    for f in rows:
        ct = f.get("conn_type") or "unknown"
        if ct not in by_ct:
            by_ct[ct] = {"conn_type": ct, "count": 0, "active_count": 0}
        by_ct[ct]["count"] += 1
        if f.get("status") == "active":
            by_ct[ct]["active_count"] += 1
    return {
        "total": total,
        "active": active,
        "stale": stale,
        "offline": offline,
        "breakdown": list(by_ct.values()),
    }


def tunnel_feeder_id(feeder):
    """Derive tunnel feeder_id (same logic as feeder tunnel client: MLAT_SITE_NAME sanitized or hostname).
    Used for /feeder/<tunnel_feeder_id>/ URL. Sanitize: lowercase, spaces to dashes; allow [a-z0-9_-]
    so we match the feeder's register id (feeder keeps underscores, e.g. 92882-test_test_test).
    """
    import re
    raw = (
        (feeder.get("display_name") or feeder.get("name") or feeder.get("hostname") or "")
        .strip()
        or str(feeder.get("id") or "")
    )
    s = raw.lower().replace(" ", "-")
    s = re.sub(r"[^a-z0-9\-_]", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s or str(feeder.get("id") or "unknown")


def enrich_feeder_mlat_display(feeder):
    """Add display_name, software_version, and tunnel_feeder_id to a feeder dict from its name (parsed)."""
    if not feeder:
        return feeder
    name = feeder.get("name") or ""
    display_name, software_version = parse_mlat_client_name(name)
    feeder = dict(feeder)
    feeder["display_name"] = display_name or name
    feeder["software_version"] = software_version  # blank when no " | v"
    feeder["tunnel_feeder_id"] = tunnel_feeder_id(feeder)
    feeder["owners"] = parse_feeder_owners(feeder.get("owners"))
    return feeder


# ── Feeders ──────────────────────────────────────────────────────────────────

class FeederModel:
    @staticmethod
    def get_all(status_filter=None, conn_type_filter=None):
        conn = get_db()
        query = "SELECT * FROM feeders"
        params = []
        conditions = []

        if status_filter and status_filter != "all":
            conditions.append("status = ?")
            params.append(status_filter)
        if conn_type_filter and conn_type_filter != "all":
            conditions.append("conn_type = ?")
            params.append(conn_type_filter)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY last_seen DESC"

        rows = conn.execute(query, params).fetchall()
        conn.close()
        return dict_rows(rows)

    @staticmethod
    def get_by_id(feeder_id):
        conn = get_db()
        row = conn.execute("SELECT * FROM feeders WHERE id = ?", (feeder_id,)).fetchone()
        conn.close()
        return dict_row(row)

    @staticmethod
    def get_by_tunnel_feeder_id(tunnel_id):
        """Return feeder dict whose tunnel_feeder_id matches (for URL /feeder/<tunnel_id>/)."""
        if not tunnel_id:
            return None
        for f in FeederModel.get_all():
            enriched = enrich_feeder_mlat_display(f)
            if (enriched.get("tunnel_feeder_id") or "").lower() == str(tunnel_id).lower():
                return enriched
        return None

    @staticmethod
    def get_stats():
        conn = get_db()
        total = conn.execute("SELECT COUNT(*) as c FROM feeders").fetchone()["c"]
        active = conn.execute("SELECT COUNT(*) as c FROM feeders WHERE status='active'").fetchone()["c"]
        stale = conn.execute("SELECT COUNT(*) as c FROM feeders WHERE status='stale'").fetchone()["c"]
        offline = conn.execute("SELECT COUNT(*) as c FROM feeders WHERE status='offline'").fetchone()["c"]

        breakdown = dict_rows(conn.execute(
            """SELECT conn_type, COUNT(*) as count,
               SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) as active_count
               FROM feeders GROUP BY conn_type"""
        ).fetchall())

        conn.close()
        return {
            "total": total,
            "active": active,
            "stale": stale,
            "offline": offline,
            "breakdown": breakdown,
        }

    @staticmethod
    def update(feeder_id, data, *, allow_owners=False):
        conn = get_db()
        allowed = {"name", "tar1090_url", "graphs1090_url", "notes", "mlat_enabled"}
        if allow_owners:
            allowed.add("owners")
        fields = []
        values = []
        for key, val in data.items():
            if key not in allowed:
                continue
            if key == "owners":
                if isinstance(val, list):
                    val = json.dumps([str(x).strip() for x in val if str(x).strip()])
                elif val is None:
                    val = "[]"
                elif isinstance(val, str) and not val.strip().startswith("["):
                    # comma-separated usernames
                    parts = [p.strip() for p in val.replace(",", " ").split() if p.strip()]
                    val = json.dumps(parts)
            fields.append(f"{key} = ?")
            values.append(val)
        if not fields:
            conn.close()
            return False
        fields.append("updated_at = datetime('now')")
        values.append(feeder_id)
        conn.execute(f"UPDATE feeders SET {', '.join(fields)} WHERE id = ?", values)
        conn.commit()
        conn.close()
        return True

    @staticmethod
    def delete(feeder_id):
        conn = get_db()
        conn.execute("DELETE FROM feeders WHERE id = ?", (feeder_id,))
        conn.commit()
        conn.close()

    @staticmethod
    def purge_old(hours=24):
        """Delete feeders not seen in the last N hours. Returns count deleted."""
        conn = get_db()
        cur = conn.execute(
            "DELETE FROM feeders WHERE last_seen < datetime('now', ?)",
            (f"-{hours} hours",)
        )
        count = cur.rowcount
        conn.commit()
        conn.close()
        return count

    @staticmethod
    def purge_inactive():
        """Delete all feeders that are not currently active. Returns count deleted."""
        conn = get_db()
        cur = conn.execute(
            "DELETE FROM feeders WHERE status != 'active'"
        )
        count = cur.rowcount
        conn.commit()
        conn.close()
        return count
        return True


# ── Connections ──────────────────────────────────────────────────────────────

class ConnectionModel:
    @staticmethod
    def get_history(feeder_id, limit=50):
        conn = get_db()
        rows = conn.execute(
            "SELECT * FROM connections WHERE feeder_id = ? ORDER BY connected_at DESC LIMIT ?",
            (feeder_id, limit),
        ).fetchall()
        conn.close()
        return dict_rows(rows)

    @staticmethod
    def get_active():
        conn = get_db()
        rows = conn.execute(
            "SELECT * FROM connections WHERE disconnected_at IS NULL ORDER BY connected_at DESC"
        ).fetchall()
        conn.close()
        return dict_rows(rows)


# ── Activity Log ─────────────────────────────────────────────────────────────

class ActivityModel:
    @staticmethod
    def get_recent(limit=20):
        conn = get_db()
        rows = conn.execute(
            """SELECT a.*, f.name as feeder_name
               FROM activity_log a
               LEFT JOIN feeders f ON a.feeder_id = f.id
               ORDER BY a.timestamp DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        conn.close()
        return dict_rows(rows)

    @staticmethod
    def cleanup(days=7):
        conn = get_db()
        conn.execute(
            "DELETE FROM activity_log WHERE timestamp < datetime('now', ?)",
            (f"-{days} days",),
        )
        conn.commit()
        conn.close()


# ── Update History ───────────────────────────────────────────────────────────

class UpdateModel:
    @staticmethod
    def log_update(from_version, to_version, success, output=""):
        conn = get_db()
        conn.execute(
            """INSERT INTO update_history
               (from_version, to_version, success, output, timestamp)
               VALUES (?, ?, ?, ?, datetime('now'))""",
            (from_version, to_version, 1 if success else 0, output),
        )
        conn.commit()
        conn.close()

    @staticmethod
    def get_history(limit=6):
        conn = get_db()
        rows = conn.execute(
            "SELECT * FROM update_history ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
        conn.close()
        return dict_rows(rows)


# ── Background Tasks ─────────────────────────────────────────────────────────


_STALE_WINDOW_SECONDS = 120  # how far back we look for traffic (2 minutes)
_STALE_MPS_THRESHOLD = 0.01  # messages per second below this -> stale (approx 1 msg every ~100s)


def _get_feeder_stale_snapshot(conn, feeder_id: int):
    """Return (last_messages, ts_utc) snapshot for feeder_id from settings, or None."""
    key = f"feeder_stale_snapshot_{feeder_id}"
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if not row or not row["value"]:
        return None
    try:
        data = json.loads(row["value"])
        return int(data.get("last_messages", 0)), float(data.get("ts", 0.0))
    except Exception:
        return None


def _set_feeder_stale_snapshot(conn, feeder_id: int, last_messages: int, ts_utc: float):
    """Persist (last_messages, ts_utc) snapshot for feeder_id into settings."""
    key = f"feeder_stale_snapshot_{feeder_id}"
    value = json.dumps({"last_messages": int(last_messages), "ts": float(ts_utc)})
    conn.execute(
        "INSERT INTO settings(key, value, updated_at) VALUES(?, ?, CURRENT_TIMESTAMP) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (key, value),
    )


def mark_stale_feeders():
    """Mark feeders as stale/active based on recent message rate instead of VPN state.

    For each feeder we track a snapshot of (messages_received, timestamp_utc) in the settings table.
    Every run we:
      - If we have no snapshot yet: create one and skip (need a baseline).
      - If enough time has passed (>= _STALE_WINDOW_SECONDS), compute messages-per-second over that
        window. If it's below _STALE_MPS_THRESHOLD, mark feeder as 'stale'; otherwise 'active'.
      - Refresh the snapshot to the current counters/time.
    This way a feeder that remains connected at VPN level but stops sending ADS-B messages will
    eventually move from active -> stale, and one that resumes traffic will move back to active.
    """
    conn = get_db()
    now_ts = datetime.now(timezone.utc).timestamp()

    rows = conn.execute(
        "SELECT id, messages_received, status FROM feeders"
    ).fetchall()

    for row in rows:
        feeder_id = row["id"]
        current_msgs = int(row.get("messages_received") or 0)
        current_status = row.get("status") or "active"

        snap = _get_feeder_stale_snapshot(conn, feeder_id)
        # First time seeing this feeder: record snapshot and move on
        if not snap:
            _set_feeder_stale_snapshot(conn, feeder_id, current_msgs, now_ts)
            continue

        last_msgs, last_ts = snap
        delta_sec = now_ts - last_ts
        if delta_sec < _STALE_WINDOW_SECONDS:
            # Not enough history yet; just refresh snapshot to avoid drift and continue
            _set_feeder_stale_snapshot(conn, feeder_id, current_msgs, now_ts)
            continue

        delta_msgs = max(current_msgs - last_msgs, 0)
        mps = (delta_msgs / delta_sec) if delta_sec > 0 else 0.0

        new_status = "active" if mps > _STALE_MPS_THRESHOLD else "stale"
        if new_status != current_status:
            conn.execute(
                "UPDATE feeders SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (new_status, feeder_id),
            )

        # Refresh snapshot for next window
        _set_feeder_stale_snapshot(conn, feeder_id, current_msgs, now_ts)

    conn.commit()
    conn.close()


# ── Users ─────────────────────────────────────────────────────────────────────

from werkzeug.security import generate_password_hash, check_password_hash


class UserModel:
    ROLES = ("admin", "network_admin", "viewer")

    @staticmethod
    def seed_default():
        """Create default admin if no users exist."""
        conn = get_db()
        count = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
        if count == 0:
            conn.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                ("admin", generate_password_hash("password"), "admin"),
            )
            conn.commit()
        conn.close()

    @staticmethod
    def get_by_username(username):
        conn = get_db()
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
        conn.close()
        return dict_row(row)

    @staticmethod
    def get_by_email(email: str):
        """Return user by email (exact match)."""
        email = (email or "").strip()
        if not email:
            return None
        conn = get_db()
        row = conn.execute(
            "SELECT * FROM users WHERE email = ?", (email,)
        ).fetchone()
        conn.close()
        return dict_row(row)

    @staticmethod
    def get_by_id(user_id):
        conn = get_db()
        row = conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        conn.close()
        return dict_row(row)

    @staticmethod
    def get_all():
        conn = get_db()
        rows = conn.execute(
            "SELECT id, username, role, status, created_at, updated_at FROM users WHERE status != 'pending' ORDER BY username"
        ).fetchall()
        conn.close()
        return dict_rows(rows)

    @staticmethod
    def get_pending():
        conn = get_db()
        rows = conn.execute(
            "SELECT id, username, role, status, created_at FROM users WHERE status = 'pending' ORDER BY created_at ASC"
        ).fetchall()
        conn.close()
        return dict_rows(rows)

    @staticmethod
    def pending_count():
        conn = get_db()
        count = conn.execute("SELECT COUNT(*) as c FROM users WHERE status = 'pending'").fetchone()["c"]
        conn.close()
        return count

    @staticmethod
    def register(username, password, profile: dict):
        """Create a pending user from a self-registration request."""
        role = "viewer"  # Permission group is set by the approving admin later.

        def _clean(v):
            if v is None:
                return None
            s = str(v).strip()
            return s if s else None

        profile_fields = {
            "first_name": _clean(profile.get("first_name")),
            "last_name": _clean(profile.get("last_name")),
            "email": _clean(profile.get("email")),
            "phone": _clean(profile.get("phone")),
            "agency": _clean(profile.get("agency")),
        }

        conn = get_db()
        try:
            conn.execute(
                """
                INSERT INTO users
                    (username, password_hash, role, status, first_name, last_name, email, phone, agency)
                VALUES
                    (?, ?, ?, 'pending', ?, ?, ?, ?, ?)
                """,
                (
                    username,
                    generate_password_hash(password),
                    role,
                    profile_fields["first_name"],
                    profile_fields["last_name"],
                    profile_fields["email"],
                    profile_fields["phone"],
                    profile_fields["agency"],
                ),
            )
            conn.commit()
            conn.close()
            return True, "Registration submitted"
        except Exception as e:
            conn.close()
            return False, str(e)

    @staticmethod
    def approve(user_id, role):
        if role not in UserModel.ROLES:
            role = "viewer"
        conn = get_db()
        conn.execute(
            "UPDATE users SET status = 'active', role = ?, updated_at = datetime('now') WHERE id = ?",
            (role, user_id),
        )
        conn.commit()
        conn.close()
        return True, "User approved"

    @staticmethod
    def deny(user_id):
        conn = get_db()
        conn.execute(
            "UPDATE users SET status = 'denied', updated_at = datetime('now') WHERE id = ?",
            (user_id,),
        )
        conn.commit()
        conn.close()
        return True, "User denied"

    @staticmethod
    def verify_password(username, password):
        user = UserModel.get_by_username(username)
        if not user:
            return None, None
        if check_password_hash(user["password_hash"], password):
            return user, user.get("status", "active")
        return None, None

    @staticmethod
    def create(username, password, role):
        if role not in UserModel.ROLES:
            return False, "Invalid role"
        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
                (username, generate_password_hash(password), role),
            )
            conn.commit()
            conn.close()
            return True, "User created"
        except Exception as e:
            conn.close()
            return False, str(e)

    @staticmethod
    def update_password(user_id, new_password):
        conn = get_db()
        conn.execute(
            "UPDATE users SET password_hash = ?, updated_at = datetime('now') WHERE id = ?",
            (generate_password_hash(new_password), user_id),
        )
        conn.commit()
        conn.close()

    @staticmethod
    def create_password_reset_token(user_id: int, *, expires_in_seconds: int = 3600) -> str:
        """Create a reset token for a user and return the raw token.

        Token is stored hashed. Any previous unused tokens for the user are invalidated.
        """
        import hashlib
        import secrets

        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        # Use SQLite's datetime('now', '+N seconds') to avoid timezone confusion.
        expires_sql = f"datetime('now', '+{int(expires_in_seconds)} seconds')"

        conn = get_db()
        try:
            # Invalidate previous unused tokens for the user (avoid accumulation).
            conn.execute(
                "UPDATE password_reset_tokens SET used_at = datetime('now') WHERE user_id = ? AND used_at IS NULL",
                (user_id,),
            )
            conn.execute(
                "INSERT INTO password_reset_tokens (user_id, token_hash, expires_at) VALUES (?, ?, "
                + expires_sql +
                ")",
                (user_id, token_hash),
            )
            conn.commit()
        finally:
            conn.close()

        return token

    @staticmethod
    def consume_password_reset_token(raw_token: str, new_password: str) -> tuple[bool, str]:
        """Consume a reset token and set a new password.

        Returns (ok, message). Token can be used only once and must not be expired.
        """
        import hashlib

        raw_token = (raw_token or "").strip()
        if not raw_token:
            return False, "missing_token"

        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT id, user_id FROM password_reset_tokens "
                "WHERE token_hash = ? AND used_at IS NULL AND expires_at > datetime('now')",
                (token_hash,),
            ).fetchone()
            if not row:
                return False, "invalid_or_expired_token"

            reset_id = row["id"]
            user_id = row["user_id"]

            conn.execute(
                "UPDATE users SET password_hash = ?, updated_at = datetime('now') WHERE id = ?",
                (generate_password_hash(new_password), user_id),
            )
            conn.execute(
                "UPDATE password_reset_tokens SET used_at = datetime('now') WHERE id = ?",
                (reset_id,),
            )
            conn.commit()
            return True, "ok"
        finally:
            conn.close()

    @staticmethod
    def verify_password_reset_token(raw_token: str) -> tuple[bool, str]:
        """Validate a reset token without consuming it."""
        import hashlib

        raw_token = (raw_token or "").strip()
        if not raw_token:
            return False, "missing_token"

        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        conn = get_db()
        try:
            row = conn.execute(
                "SELECT 1 FROM password_reset_tokens "
                "WHERE token_hash = ? AND used_at IS NULL AND expires_at > datetime('now')",
                (token_hash,),
            ).fetchone()
            if not row:
                return False, "invalid_or_expired_token"
            return True, "ok"
        finally:
            conn.close()

    @staticmethod
    def update_role(user_id, role):
        if role not in UserModel.ROLES:
            return False, "Invalid role"
        conn = get_db()
        conn.execute(
            "UPDATE users SET role = ?, updated_at = datetime('now') WHERE id = ?",
            (role, user_id),
        )
        conn.commit()
        conn.close()
        return True, "Role updated"

    @staticmethod
    def update_profile(user_id, data: dict):
        """Admin-only: update optional profile metadata fields."""
        def _clean(v):
            if v is None:
                return None
            s = str(v).strip()
            return s if s else None

        fields = {
            "first_name": _clean(data.get("first_name")),
            "last_name": _clean(data.get("last_name")),
            "email": _clean(data.get("email")),
            "phone": _clean(data.get("phone")),
            "agency": _clean(data.get("agency")),
        }

        conn = get_db()
        conn.execute(
            """
            UPDATE users
               SET first_name = ?,
                   last_name  = ?,
                   email      = ?,
                   phone      = ?,
                   agency     = ?,
                   updated_at = datetime('now')
             WHERE id = ?
            """,
            (
                fields["first_name"],
                fields["last_name"],
                fields["email"],
                fields["phone"],
                fields["agency"],
                user_id,
            ),
        )
        conn.commit()
        conn.close()

        return True, "Profile updated"

    @staticmethod
    def delete(user_id):
        conn = get_db()
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
        conn.close()


class OutputModel:
    """Outputs — scoped by creator. Admins see all; network_admins see only their own."""

    @staticmethod
    def _row_to_dict(row):
        if row is None:
            return None
        return dict(row)

    @staticmethod
    def get_for_user(user_id, role):
        """Return outputs visible to this user based on role."""
        conn = get_db()
        if role == "admin":
            rows = conn.execute(
                """SELECT o.*, u.username as creator_name
                   FROM outputs o JOIN users u ON o.created_by = u.id
                   ORDER BY o.created_at DESC"""
            ).fetchall()
        else:  # network_admin — compare as int so string user_id matches DB integer
            rows = conn.execute(
                """SELECT o.*, u.username as creator_name
                   FROM outputs o JOIN users u ON o.created_by = u.id
                   WHERE o.created_by = ?
                   ORDER BY o.created_at DESC""",
                (int(user_id),)
            ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    @staticmethod
    def get_by_id(output_id, user_id, role):
        """Return a single output if the user is allowed to see it."""
        conn = get_db()
        row = conn.execute(
            "SELECT o.*, u.username as creator_name FROM outputs o JOIN users u ON o.created_by = u.id WHERE o.id = ?",
            (output_id,)
        ).fetchone()
        conn.close()
        if row is None:
            return None
        row = dict(row)
        if role == "admin":
            return row
        # Compare as int so string user_id (e.g. from Flask-Login) matches DB integer created_by
        if role == "network_admin" and int(row["created_by"]) == int(user_id):
            return row
        return None

    @staticmethod
    def can_modify(output_id, user_id, role):
        """Return True if this user can create/edit/delete this output."""
        if role == "admin":
            return True
        if role != "network_admin":
            return False
        conn = get_db()
        row = conn.execute("SELECT created_by FROM outputs WHERE id = ?", (output_id,)).fetchone()
        conn.close()
        # Compare as int so string user_id (e.g. from Flask-Login) matches DB integer created_by
        return row is not None and int(row["created_by"]) == int(user_id)

    @staticmethod
    def create(name, output_type, config, created_by, mode="api", notes=None,
               output_format="as_is", use_cotproxy=False):
        conn = get_db()
        cursor = conn.execute(
            """INSERT INTO outputs (name, output_type, mode, config, created_by, notes, output_format, use_cotproxy)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (name, output_type, mode, config, created_by, notes,
             output_format, 1 if use_cotproxy else 0)
        )
        output_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return output_id

    @staticmethod
    def update(output_id, data):
        allowed = {"name", "output_type", "config", "is_public", "status", "notes",
                   "output_format", "use_cotproxy"}
        fields = {k: v for k, v in data.items() if k in allowed}
        if not fields:
            return False
        fields["updated_at"] = "datetime('now')"
        set_clause = ", ".join(
            f"{k} = datetime('now')" if v == "datetime('now')" else f"{k} = ?"
            for k, v in fields.items()
        )
        values = [v for v in fields.values() if v != "datetime('now')"]
        conn = get_db()
        conn.execute(
            f"UPDATE outputs SET {set_clause} WHERE id = ?",
            (*values, output_id)
        )
        conn.commit()
        conn.close()
        return True

    @staticmethod
    def delete(output_id):
        conn = get_db()
        conn.execute("DELETE FROM outputs WHERE id = ?", (output_id,))
        conn.commit()
        conn.close()



class OutputKeyModel:
    """API key management for outputs."""

    @staticmethod
    def generate(output_id: int, key_type: str = "single_use") -> str:
        """Generate a new key, store hash + display copy, return the raw key."""
        import secrets, hashlib
        if key_type not in ("single_use", "durable"):
            key_type = "single_use"
        raw = "tak-" + secrets.token_urlsafe(32)
        key_hash = hashlib.sha256(raw.encode()).hexdigest()
        prefix = raw[:12]
        conn = get_db()
        conn.execute("DELETE FROM output_api_keys WHERE output_id = ?", (output_id,))
        conn.execute(
            """INSERT INTO output_api_keys
               (output_id, key_hash, key_prefix, key_display, key_type, status)
               VALUES (?, ?, ?, ?, ?, 'ready')""",
            (output_id, key_hash, prefix, raw, key_type)
        )
        conn.commit()
        conn.close()
        return raw

    @staticmethod
    def get_for_output(output_id: int):
        """Return key metadata including display key, type, and status."""
        conn = get_db()
        row = conn.execute(
            """SELECT id, key_prefix, key_display, key_type, status, created_at, last_used
               FROM output_api_keys WHERE output_id = ?""",
            (output_id,)
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    @staticmethod
    def consume(raw_key: str):
        """Validate a key for beast_raw connection.
        - single_use: must be 'ready', marks it 'used' on first auth.
        - durable: always valid if key exists and output is active; never consumed.
        Returns output dict if valid, None otherwise."""
        import hashlib
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        conn = get_db()
        row = conn.execute(
            """SELECT o.*, k.id as key_id, k.key_type, k.status as key_status
               FROM output_api_keys k
               JOIN outputs o ON k.output_id = o.id
               WHERE k.key_hash = ? AND o.status = 'active'""",
            (key_hash,)
        ).fetchone()
        if not row:
            conn.close()
            return None
        key_type = row["key_type"]
        if key_type == "single_use":
            if row["key_status"] != "ready":
                conn.close()
                return None  # already used
            conn.execute(
                "UPDATE output_api_keys SET status = 'used', last_used = datetime('now') WHERE id = ?",
                (row["key_id"],)
            )
            conn.commit()
        else:
            # durable — just update last_used, never consume
            conn.execute(
                "UPDATE output_api_keys SET last_used = datetime('now') WHERE id = ?",
                (row["key_id"],)
            )
            conn.commit()
        conn.close()
        return dict(row)

    @staticmethod
    def validate(raw_key: str):
        """Non-consuming validate for JSON API (stateless HTTP).
        Works for both single_use and durable keys — no status change."""
        import hashlib
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        conn = get_db()
        row = conn.execute(
            """SELECT o.*, k.id as key_id, k.key_type, k.status as key_status
               FROM output_api_keys k
               JOIN outputs o ON k.output_id = o.id
               WHERE k.key_hash = ? AND o.status = 'active'""",
            (key_hash,)
        ).fetchone()
        if not row:
            conn.close()
            return None
        conn.execute(
            "UPDATE output_api_keys SET last_used = datetime('now') WHERE id = ?",
            (row["key_id"],)
        )
        conn.commit()
        conn.close()
        return dict(row)

    @staticmethod
    def delete(output_id: int):
        conn = get_db()
        conn.execute("DELETE FROM output_api_keys WHERE output_id = ?", (output_id,))
        conn.commit()
        conn.close()


class CotTransformModel:
    """COTProxy-style transform rules per output (hex -> callsign, type, icon, etc.)."""

    CSV_HEADERS = ("DOMAIN", "AGENCY", "REG", "CALLSIGN", "TYPE", "MODEL", "HEX", "COT", "ICON", "REMARKS", "VIDEO")

    _SORT_COLUMNS = ("hex", "callsign", "type", "domain", "agency", "reg")

    @staticmethod
    def get_all(output_id: int):
        conn = get_db()
        rows = conn.execute(
            """SELECT id, output_id, domain, agency, reg, callsign, type, model, hex, cot, icon, remarks, video, created_at
               FROM cot_transforms WHERE output_id = ? ORDER BY hex""",
            (output_id,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    @staticmethod
    def _like_escape(s: str) -> str:
        """Escape % and _ for use in LIKE with ESCAPE '\\'."""
        if not s:
            return s
        return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    @staticmethod
    def get_paginated(
        output_id: int,
        page: int = 1,
        per_page: int = 100,
        sort_by: str = "hex",
        order: str = "asc",
        *,
        filter_hex: str | None = None,
        filter_callsign: str | None = None,
        filter_type: str | None = None,
        filter_domain: str | None = None,
        filter_agency: str | None = None,
        filter_reg: str | None = None,
        filter_model: str | None = None,
        filter_cot: str | None = None,
    ) -> tuple:
        """Return (list of transforms for page, total count). sort_by must be in _SORT_COLUMNS.
        Text filters (hex, callsign, reg, model) use case-insensitive substring match;
        type, domain, agency, cot use exact match."""
        if sort_by not in CotTransformModel._SORT_COLUMNS:
            sort_by = "hex"
        order = "DESC" if (order or "").lower() == "desc" else "ASC"
        page = max(1, int(page))
        per_page = max(1, min(500, int(per_page)))
        where_parts = ["output_id = ?"]
        params = [output_id]
        if filter_hex and filter_hex.strip():
            where_parts.append("UPPER(hex) LIKE UPPER(?) ESCAPE '\\'")
            params.append("%" + CotTransformModel._like_escape(filter_hex.strip()) + "%")
        if filter_callsign and filter_callsign.strip():
            where_parts.append("callsign LIKE ? ESCAPE '\\'")
            params.append("%" + CotTransformModel._like_escape(filter_callsign.strip()) + "%")
        if filter_type and filter_type.strip():
            where_parts.append("type = ?")
            params.append(filter_type.strip())
        if filter_domain and filter_domain.strip():
            where_parts.append("domain = ?")
            params.append(filter_domain.strip())
        if filter_agency and filter_agency.strip():
            where_parts.append("agency = ?")
            params.append(filter_agency.strip())
        if filter_reg and filter_reg.strip():
            where_parts.append("reg LIKE ? ESCAPE '\\'")
            params.append("%" + CotTransformModel._like_escape(filter_reg.strip()) + "%")
        if filter_model and filter_model.strip():
            where_parts.append("model LIKE ? ESCAPE '\\'")
            params.append("%" + CotTransformModel._like_escape(filter_model.strip()) + "%")
        if filter_cot is not None and filter_cot.strip() == "":
            where_parts.append("(cot IS NULL OR TRIM(COALESCE(cot, '')) = '')")
        elif filter_cot and filter_cot.strip():
            where_parts.append("cot = ?")
            params.append(filter_cot.strip())
        where_sql = " AND ".join(where_parts)
        conn = get_db()
        try:
            total = conn.execute(
                "SELECT COUNT(*) FROM cot_transforms WHERE " + where_sql,
                tuple(params),
            ).fetchone()[0]
            offset = (page - 1) * per_page
            q_params = params + [per_page, offset]
            rows = conn.execute(
                f"""SELECT id, output_id, domain, agency, reg, callsign, type, model, hex, cot, icon, remarks, video, created_at
                    FROM cot_transforms WHERE {where_sql}
                    ORDER BY {sort_by} {order}
                    LIMIT ? OFFSET ?""",
                tuple(q_params),
            ).fetchall()
            return [dict(r) for r in rows], total
        finally:
            conn.close()

    @staticmethod
    def get_facets(output_id: int) -> dict:
        """Return distinct values for type, domain, agency (for filter dropdowns)."""
        conn = get_db()
        try:
            types = [
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT type FROM cot_transforms WHERE output_id = ? AND type IS NOT NULL AND TRIM(type) != '' ORDER BY type",
                    (output_id,),
                ).fetchall()
            ]
            domains = [
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT domain FROM cot_transforms WHERE output_id = ? AND domain IS NOT NULL AND TRIM(domain) != '' ORDER BY domain",
                    (output_id,),
                ).fetchall()
            ]
            agencies = [
                r[0]
                for r in conn.execute(
                    "SELECT DISTINCT agency FROM cot_transforms WHERE output_id = ? AND agency IS NOT NULL AND TRIM(agency) != '' ORDER BY agency",
                    (output_id,),
                ).fetchall()
            ]
            return {"type": types, "domain": domains, "agency": agencies}
        finally:
            conn.close()

    @staticmethod
    def get_by_id(transform_id: int, output_id: int):
        conn = get_db()
        row = conn.execute(
            "SELECT * FROM cot_transforms WHERE id = ? AND output_id = ?",
            (transform_id, output_id),
        ).fetchone()
        conn.close()
        return dict_row(row)

    @staticmethod
    def create(output_id: int, data: dict) -> int:
        hex_val = (data.get("hex") or "").strip().upper()
        if not hex_val:
            raise ValueError("hex is required")
        conn = get_db()
        cursor = conn.execute(
            """INSERT INTO cot_transforms (output_id, domain, agency, reg, callsign, type, model, hex, cot, icon, remarks, video)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                output_id,
                (data.get("domain") or "").strip() or None,
                (data.get("agency") or "").strip() or None,
                (data.get("reg") or "").strip() or None,
                (data.get("callsign") or "").strip() or None,
                (data.get("type") or "").strip() or None,
                (data.get("model") or "").strip() or None,
                hex_val,
                (data.get("cot") or "").strip() or None,
                (data.get("icon") or "").strip() or None,
                (data.get("remarks") or "").strip() or None,
                (data.get("video") or "").strip() or None,
            ),
        )
        tid = cursor.lastrowid
        conn.commit()
        conn.close()
        return tid

    @staticmethod
    def update(transform_id: int, output_id: int, data: dict) -> bool:
        allowed = {"domain", "agency", "reg", "callsign", "type", "model", "hex", "cot", "icon", "remarks", "video"}
        fields = {k: v for k, v in data.items() if k in allowed}
        if not fields:
            return False
        conn = get_db()
        conn.execute(
            """UPDATE cot_transforms SET
               domain=?, agency=?, reg=?, callsign=?, type=?, model=?, hex=?, cot=?, icon=?, remarks=?, video=?
               WHERE id = ? AND output_id = ?""",
            (
                (data.get("domain") or "").strip() or None,
                (data.get("agency") or "").strip() or None,
                (data.get("reg") or "").strip() or None,
                (data.get("callsign") or "").strip() or None,
                (data.get("type") or "").strip() or None,
                (data.get("model") or "").strip() or None,
                (data.get("hex") or "").strip().upper() or None,
                (data.get("cot") or "").strip() or None,
                (data.get("icon") or "").strip() or None,
                (data.get("remarks") or "").strip() or None,
                (data.get("video") or "").strip() or None,
                transform_id,
                output_id,
            ),
        )
        conn.commit()
        conn.close()
        return True

    @staticmethod
    def delete(transform_id: int, output_id: int):
        conn = get_db()
        conn.execute("DELETE FROM cot_transforms WHERE id = ? AND output_id = ?", (transform_id, output_id))
        conn.commit()
        conn.close()

    @staticmethod
    def bulk_delete(output_id: int, ids: list, delete_all: bool = False) -> int:
        """Delete many transforms: by id list (chunked) or all for output. One connection. Returns count deleted."""
        conn = get_db()
        try:
            if delete_all:
                cur = conn.execute("DELETE FROM cot_transforms WHERE output_id = ?", (output_id,))
                deleted = cur.rowcount
            else:
                ids = [int(x) for x in (ids or []) if x is not None]
                if not ids:
                    conn.close()
                    return 0
                chunk_size = 500
                deleted = 0
                for i in range(0, len(ids), chunk_size):
                    chunk = ids[i : i + chunk_size]
                    placeholders = ",".join("?" * len(chunk))
                    cur = conn.execute(
                        f"DELETE FROM cot_transforms WHERE output_id = ? AND id IN ({placeholders})",
                        (output_id,) + tuple(chunk),
                    )
                    deleted += cur.rowcount
            conn.commit()
            return deleted
        finally:
            conn.close()

    @staticmethod
    def import_from_csv(output_id: int, csv_text: str) -> tuple:
        """Parse CSV (header: DOMAIN,AGENCY,REG,CALLSIGN,TYPE,MODEL,HEX,COT,ICON) and insert rows.
        Uses a single connection and transaction to avoid timeouts on large imports.
        Returns (inserted_count, error_messages)."""
        import csv
        import io
        inserted = 0
        errors = []
        try:
            reader = csv.DictReader(io.StringIO(csv_text))
            if reader.fieldnames:
                reader.fieldnames = [f.strip().upper() for f in reader.fieldnames]
            conn = get_db()
            try:
                for i, row in enumerate(reader):
                    row_num = i + 2
                    hex_val = (row.get("HEX") or "").strip().upper()
                    if not hex_val:
                        errors.append(f"Row {row_num}: HEX is required")
                        continue
                    try:
                        conn.execute(
                            """INSERT INTO cot_transforms (output_id, domain, agency, reg, callsign, type, model, hex, cot, icon, remarks, video)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                output_id,
                                (row.get("DOMAIN") or "").strip() or None,
                                (row.get("AGENCY") or "").strip() or None,
                                (row.get("REG") or "").strip() or None,
                                (row.get("CALLSIGN") or "").strip() or None,
                                (row.get("TYPE") or "").strip() or None,
                                (row.get("MODEL") or "").strip() or None,
                                hex_val,
                                (row.get("COT") or "").strip() or None,
                                (row.get("ICON") or "").strip() or None,
                                (row.get("REMARKS") or "").strip() or None,
                                (row.get("VIDEO") or "").strip() or None,
                            ),
                        )
                        inserted += 1
                    except Exception as e:
                        errors.append(f"Row {row_num}: {e}")
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            errors.append(f"CSV parse error: {e}")
        return inserted, errors

    @staticmethod
    def get_by_hex(output_id: int, hex_code: str) -> dict:
        """Return first transform matching output_id and hex (for CoT pipeline)."""
        conn = get_db()
        row = conn.execute(
            "SELECT * FROM cot_transforms WHERE output_id = ? AND UPPER(TRIM(hex)) = ?",
            (output_id, (hex_code or "").strip().upper()),
        ).fetchone()
        conn.close()
        return dict_row(row)


class OutputCotCertModel:
    """CoT push TLS client certificates. Encrypted at rest; never returned to API/UI. Owner-only upload/replace."""

    @staticmethod
    def has_cert(output_id: int) -> bool:
        conn = get_db()
        row = conn.execute(
            "SELECT 1 FROM output_cot_certs WHERE output_id = ?", (output_id,)
        ).fetchone()
        conn.close()
        return row is not None

    @staticmethod
    def set(output_id: int, cert_pem: str, key_pem: str, ca_pem: str = None) -> None:
        from cert_crypto import encrypt_cert
        cert_enc = encrypt_cert((cert_pem or "").strip())
        key_enc = encrypt_cert((key_pem or "").strip())
        ca_enc = encrypt_cert((ca_pem or "").strip()) if (ca_pem and (ca_pem or "").strip()) else None
        conn = get_db()
        conn.execute(
            """INSERT OR REPLACE INTO output_cot_certs
               (output_id, cert_encrypted, key_encrypted, ca_encrypted, updated_at)
               VALUES (?, ?, ?, ?, datetime('now'))""",
            (output_id, cert_enc, key_enc, ca_enc),
        )
        conn.commit()
        conn.close()

    @staticmethod
    def delete(output_id: int) -> None:
        conn = get_db()
        conn.execute("DELETE FROM output_cot_certs WHERE output_id = ?", (output_id,))
        conn.commit()
        conn.close()

    @staticmethod
    def get_decrypted(output_id: int):
        """Return plaintext cert/key/ca for backend CoT sender only. Never expose via API."""
        conn = get_db()
        row = conn.execute(
            "SELECT cert_encrypted, key_encrypted, ca_encrypted FROM output_cot_certs WHERE output_id = ?",
            (output_id,),
        ).fetchone()
        conn.close()
        if not row:
            return None
        try:
            from cert_crypto import decrypt_cert
            return {
                "cert_pem": decrypt_cert(row["cert_encrypted"]),
                "key_pem": decrypt_cert(row["key_encrypted"]),
                "ca_pem": decrypt_cert(row["ca_encrypted"]) if row["ca_encrypted"] else None,
            }
        except Exception:
            return None


def signal_drop_output(output_id: int):
    """Signal beast-proxy to drop active connections for this output_id."""
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO output_drop_signals (output_id) VALUES (?)",
        (output_id,)
    )
    conn.commit()
    conn.close()
