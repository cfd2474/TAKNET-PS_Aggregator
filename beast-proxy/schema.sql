-- TAKNET-PS Aggregator Database Schema v1.0.31

-- Feeder registry
CREATE TABLE IF NOT EXISTS feeders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    conn_type TEXT NOT NULL DEFAULT 'public',   -- 'tailscale', 'netbird', 'public'
    ip_address TEXT,
    hostname TEXT,
    location TEXT,
    latitude REAL,
    longitude REAL,
    altitude REAL,
    tar1090_url TEXT,
    graphs1090_url TEXT,
    first_seen DATETIME NOT NULL,
    last_seen DATETIME NOT NULL,
    status TEXT DEFAULT 'active',                -- 'active', 'stale', 'offline'
    bytes_received INTEGER DEFAULT 0,
    messages_received INTEGER DEFAULT 0,
    positions_received INTEGER DEFAULT 0,
    mlat_enabled BOOLEAN DEFAULT 0,
    notes TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Connection history
CREATE TABLE IF NOT EXISTS connections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feeder_id INTEGER NOT NULL,
    ip_address TEXT NOT NULL,
    connected_at DATETIME NOT NULL,
    disconnected_at DATETIME,
    duration_seconds INTEGER,
    bytes_transferred INTEGER DEFAULT 0,
    FOREIGN KEY (feeder_id) REFERENCES feeders(id) ON DELETE CASCADE
);

-- Activity log
CREATE TABLE IF NOT EXISTS activity_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    event_type TEXT NOT NULL,
    feeder_id INTEGER,
    message TEXT,
    FOREIGN KEY (feeder_id) REFERENCES feeders(id) ON DELETE SET NULL
);

-- Settings key-value store
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Update history
CREATE TABLE IF NOT EXISTS update_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_version TEXT,
    to_version TEXT,
    success BOOLEAN DEFAULT 1,
    output TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_feeders_status ON feeders(status);
CREATE INDEX IF NOT EXISTS idx_feeders_last_seen ON feeders(last_seen);
CREATE INDEX IF NOT EXISTS idx_feeders_ip ON feeders(ip_address);
CREATE INDEX IF NOT EXISTS idx_connections_feeder ON connections(feeder_id);
CREATE INDEX IF NOT EXISTS idx_connections_connected ON connections(connected_at);
CREATE INDEX IF NOT EXISTS idx_activity_timestamp ON activity_log(timestamp DESC);
