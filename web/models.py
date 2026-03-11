"""Database models and query helpers for the TAKNET-PS dashboard."""

import os
import sqlite3

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
                hex TEXT NOT NULL, cot TEXT, icon TEXT, remarks TEXT,
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
    def update(feeder_id, data):
        conn = get_db()
        allowed = {"name", "tar1090_url", "graphs1090_url", "notes", "mlat_enabled"}
        fields = []
        values = []
        for key, val in data.items():
            if key in allowed:
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

def mark_stale_feeders():
    """Mark feeders as stale if last_seen > 2 minutes ago."""
    conn = get_db()
    conn.execute(
        """UPDATE feeders SET status = 'stale'
           WHERE last_seen < datetime('now', '-2 minutes')
           AND status = 'active'"""
    )
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
    def register(username, password):
        """Create a pending user from a self-registration request."""
        conn = get_db()
        try:
            conn.execute(
                "INSERT INTO users (username, password_hash, role, status) VALUES (?, ?, 'viewer', 'pending')",
                (username, generate_password_hash(password)),
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
        else:  # network_admin
            rows = conn.execute(
                """SELECT o.*, u.username as creator_name
                   FROM outputs o JOIN users u ON o.created_by = u.id
                   WHERE o.created_by = ?
                   ORDER BY o.created_at DESC""",
                (user_id,)
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
        if role == "network_admin" and row["created_by"] == user_id:
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
        return row is not None and row["created_by"] == user_id

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

    CSV_HEADERS = ("DOMAIN", "AGENCY", "REG", "CALLSIGN", "TYPE", "MODEL", "HEX", "COT", "ICON", "REMARKS")

    _SORT_COLUMNS = ("hex", "callsign", "type", "domain", "agency", "reg")

    @staticmethod
    def get_all(output_id: int):
        conn = get_db()
        rows = conn.execute(
            """SELECT id, output_id, domain, agency, reg, callsign, type, model, hex, cot, icon, remarks, created_at
               FROM cot_transforms WHERE output_id = ? ORDER BY hex""",
            (output_id,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    @staticmethod
    def get_paginated(output_id: int, page: int = 1, per_page: int = 100, sort_by: str = "hex", order: str = "asc") -> tuple:
        """Return (list of transforms for page, total count). sort_by must be in _SORT_COLUMNS."""
        if sort_by not in CotTransformModel._SORT_COLUMNS:
            sort_by = "hex"
        order = "DESC" if (order or "").lower() == "desc" else "ASC"
        page = max(1, int(page))
        per_page = max(1, min(500, int(per_page)))
        conn = get_db()
        try:
            total = conn.execute(
                "SELECT COUNT(*) FROM cot_transforms WHERE output_id = ?",
                (output_id,),
            ).fetchone()[0]
            offset = (page - 1) * per_page
            rows = conn.execute(
                f"""SELECT id, output_id, domain, agency, reg, callsign, type, model, hex, cot, icon, remarks, created_at
                    FROM cot_transforms WHERE output_id = ?
                    ORDER BY {sort_by} {order}
                    LIMIT ? OFFSET ?""",
                (output_id, per_page, offset),
            ).fetchall()
            return [dict(r) for r in rows], total
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
            """INSERT INTO cot_transforms (output_id, domain, agency, reg, callsign, type, model, hex, cot, icon, remarks)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
            ),
        )
        tid = cursor.lastrowid
        conn.commit()
        conn.close()
        return tid

    @staticmethod
    def update(transform_id: int, output_id: int, data: dict) -> bool:
        allowed = {"domain", "agency", "reg", "callsign", "type", "model", "hex", "cot", "icon", "remarks"}
        fields = {k: v for k, v in data.items() if k in allowed}
        if not fields:
            return False
        conn = get_db()
        conn.execute(
            """UPDATE cot_transforms SET
               domain=?, agency=?, reg=?, callsign=?, type=?, model=?, hex=?, cot=?, icon=?, remarks=?
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
                            """INSERT INTO cot_transforms (output_id, domain, agency, reg, callsign, type, model, hex, cot, icon, remarks)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
