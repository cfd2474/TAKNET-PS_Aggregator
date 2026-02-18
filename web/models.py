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
