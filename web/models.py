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
    def create(name, output_type, config, created_by, mode="api", notes=None):
        conn = get_db()
        cursor = conn.execute(
            """INSERT INTO outputs (name, output_type, mode, config, created_by, notes)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (name, output_type, mode, config, created_by, notes)
        )
        output_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return output_id

    @staticmethod
    def update(output_id, data):
        allowed = {"name", "output_type", "config", "is_public", "status", "notes"}
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
    def _hash(key: str) -> str:
        import hashlib
        return hashlib.sha256(key.encode()).hexdigest()

    @staticmethod
    def generate(output_id: int) -> str:
        """Generate a new key, store its hash, return the raw key (shown once)."""
        import secrets
        import hashlib
        raw = "tak-" + secrets.token_urlsafe(32)
        key_hash = hashlib.sha256(raw.encode()).hexdigest()
        prefix = raw[:12]
        conn = get_db()
        conn.execute("DELETE FROM output_api_keys WHERE output_id = ?", (output_id,))
        conn.execute(
            "INSERT INTO output_api_keys (output_id, key_hash, key_prefix) VALUES (?, ?, ?)",
            (output_id, key_hash, prefix)
        )
        conn.commit()
        conn.close()
        return raw

    @staticmethod
    def get_for_output(output_id: int):
        """Return key metadata (never the raw key) for an output."""
        conn = get_db()
        row = conn.execute(
            "SELECT id, key_prefix, created_at, last_used FROM output_api_keys WHERE output_id = ?",
            (output_id,)
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    @staticmethod
    def validate(raw_key: str):
        """Validate a raw key. Returns output dict or None."""
        import hashlib
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        conn = get_db()
        row = conn.execute(
            """SELECT o.*, k.id as key_id FROM output_api_keys k
               JOIN outputs o ON k.output_id = o.id
               WHERE k.key_hash = ? AND o.status = 'active'""",
            (key_hash,)
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE output_api_keys SET last_used = datetime('now') WHERE id = ?",
                (row["key_id"],)
            )
            conn.commit()
        conn.close()
        return dict(row) if row else None

    @staticmethod
    def delete(output_id: int):
        conn = get_db()
        conn.execute("DELETE FROM output_api_keys WHERE output_id = ?", (output_id,))
        conn.commit()
        conn.close()
