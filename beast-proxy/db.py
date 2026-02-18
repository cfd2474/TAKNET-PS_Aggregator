"""Database wrapper for beast-proxy SQLite operations."""

import os
import sqlite3
import threading
from datetime import datetime, timezone

DB_PATH = os.environ.get("DB_PATH", "/data/aggregator.db")
_local = threading.local()


def _get_conn():
    """Get thread-local database connection."""
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(DB_PATH, timeout=10)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA busy_timeout=5000")
    return _local.conn


def init_db():
    """Initialize database with schema if tables don't exist."""
    conn = _get_conn()
    schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
    with open(schema_path) as f:
        conn.executescript(f.read())
    # Migrations for existing databases
    try:
        conn.execute("ALTER TABLE feeders ADD COLUMN altitude REAL")
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.commit()
    print(f"[db] Database initialized at {DB_PATH}")


def now_utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def upsert_feeder(ip_address, hostname, conn_type, location=None, lat=None, lon=None):
    """Create or update feeder record. Returns feeder ID."""
    conn = _get_conn()
    ts = now_utc()

    # Try to find existing feeder by IP
    row = conn.execute(
        "SELECT id, name FROM feeders WHERE ip_address = ?", (ip_address,)
    ).fetchone()

    if row:
        feeder_id = row["id"]
        conn.execute(
            """UPDATE feeders SET
                hostname = COALESCE(?, hostname),
                conn_type = ?,
                location = COALESCE(?, location),
                latitude = COALESCE(?, latitude),
                longitude = COALESCE(?, longitude),
                last_seen = ?,
                status = 'active',
                updated_at = ?
            WHERE id = ?""",
            (hostname, conn_type, location, lat, lon, ts, ts, feeder_id),
        )
    else:
        # Auto-generate name
        if hostname:
            name = hostname
        elif location:
            short_hash = ip_address.replace(".", "")[-4:]
            name = f"feeder-{location.lower().replace(' ', '-').replace(',', '')}-{short_hash}"
        else:
            short_hash = ip_address.replace(".", "")[-4:]
            name = f"feeder-{short_hash}"

        cursor = conn.execute(
            """INSERT INTO feeders
                (name, conn_type, ip_address, hostname, location, latitude, longitude,
                 first_seen, last_seen, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
            (name, conn_type, ip_address, hostname, location, lat, lon, ts, ts, ts, ts),
        )
        feeder_id = cursor.lastrowid

    conn.commit()
    return feeder_id


def log_connection(feeder_id, ip_address):
    """Log a new connection event. Returns connection ID."""
    conn = _get_conn()
    ts = now_utc()
    cursor = conn.execute(
        "INSERT INTO connections (feeder_id, ip_address, connected_at) VALUES (?, ?, ?)",
        (feeder_id, ip_address, ts),
    )
    conn.execute(
        "INSERT INTO activity_log (event_type, feeder_id, message) VALUES (?, ?, ?)",
        ("feeder_connected", feeder_id, f"Feeder connected from {ip_address}"),
    )
    conn.commit()
    return cursor.lastrowid


def log_disconnection(feeder_id, connection_id, bytes_transferred=0):
    """Log disconnection and compute duration."""
    conn = _get_conn()
    ts = now_utc()
    conn.execute(
        """UPDATE connections SET
            disconnected_at = ?,
            duration_seconds = CAST((julianday(?) - julianday(connected_at)) * 86400 AS INTEGER),
            bytes_transferred = ?
        WHERE id = ?""",
        (ts, ts, bytes_transferred, connection_id),
    )
    conn.execute(
        "UPDATE feeders SET status = 'offline', last_seen = ?, updated_at = ? WHERE id = ?",
        (ts, ts, feeder_id),
    )
    conn.execute(
        "INSERT INTO activity_log (event_type, feeder_id, message) VALUES (?, ?, ?)",
        ("feeder_disconnected", feeder_id, "Feeder disconnected"),
    )
    conn.commit()


def update_feeder_stats(feeder_id, bytes_count, messages_count=0, positions_count=0):
    """Increment feeder byte, message, and position counters."""
    conn = _get_conn()
    ts = now_utc()
    conn.execute(
        """UPDATE feeders SET
            bytes_received = bytes_received + ?,
            messages_received = messages_received + ?,
            positions_received = positions_received + ?,
            last_seen = ?,
            status = 'active',
            updated_at = ?
        WHERE id = ?""",
        (bytes_count, messages_count, positions_count, ts, ts, feeder_id),
    )
    conn.commit()


def touch_feeder(feeder_id):
    """Update last_seen and ensure feeder is marked active (even with no new data)."""
    conn = _get_conn()
    ts = now_utc()
    conn.execute(
        "UPDATE feeders SET last_seen = ?, status = 'active', updated_at = ? WHERE id = ?",
        (ts, ts, feeder_id),
    )
    conn.commit()


def update_feeder_mlat(feeder_id, mlat_enabled, lat=None, lon=None, alt=None, mlat_name=None):
    """Update MLAT status and coordinates for a feeder."""
    conn = _get_conn()
    ts = now_utc()
    if mlat_enabled and lat is not None and lon is not None:
        conn.execute(
            """UPDATE feeders SET
                mlat_enabled = 1,
                latitude = ?,
                longitude = ?,
                altitude = ?,
                updated_at = ?
            WHERE id = ?""",
            (lat, lon, alt, ts, feeder_id),
        )
    else:
        conn.execute(
            "UPDATE feeders SET mlat_enabled = ?, updated_at = ? WHERE id = ?",
            (1 if mlat_enabled else 0, ts, feeder_id),
        )
    conn.commit()


def mark_inactive_feeders(active_feeder_ids):
    """Mark feeders not in active_feeder_ids as stale if they were active."""
    conn = _get_conn()
    ts = now_utc()
    if active_feeder_ids:
        placeholders = ",".join("?" for _ in active_feeder_ids)
        conn.execute(
            f"""UPDATE feeders SET status = 'stale', updated_at = ?
                WHERE status = 'active' AND id NOT IN ({placeholders})""",
            (ts, *active_feeder_ids),
        )
    else:
        conn.execute(
            "UPDATE feeders SET status = 'stale', updated_at = ? WHERE status = 'active'",
            (ts,),
        )
    conn.commit()
