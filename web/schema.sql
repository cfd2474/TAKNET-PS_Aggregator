-- TAKNET-PS Aggregator Database Schema v1.0.305

-- Users (authentication)
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'viewer',         -- 'admin', 'network_admin', 'viewer'
    first_name TEXT,
    last_name TEXT,
    email TEXT,
    phone TEXT,
    agency TEXT,
    status TEXT NOT NULL DEFAULT 'active',       -- 'active', 'pending', 'denied'
    feeder_claim_key TEXT,                     -- permanent UUID for feeder ownership claim (per user); unique index below
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Password reset tokens (for "forgot password" email flow)
CREATE TABLE IF NOT EXISTS password_reset_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,      -- SHA-256 hex of the raw token
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    expires_at DATETIME NOT NULL,
    used_at DATETIME,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_password_reset_tokens_user_id ON password_reset_tokens(user_id);
CREATE INDEX IF NOT EXISTS idx_password_reset_tokens_expires_at ON password_reset_tokens(expires_at);
CREATE INDEX IF NOT EXISTS idx_password_reset_tokens_hash ON password_reset_tokens(token_hash);

-- Feeder registry
CREATE TABLE IF NOT EXISTS feeders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    conn_type TEXT NOT NULL DEFAULT 'public',   -- 'tailscale', 'netbird', 'public'
    ip_address TEXT,
    device_mac TEXT,                            -- optional feeder-reported MAC (stable identity across IP changes)
    feeder_uuid TEXT,                           -- optional feeder-reported UDID
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
    owners TEXT NOT NULL DEFAULT '[]',   -- JSON array of usernames; admin-only edit; empty = admin-only access
    owners_locked INTEGER NOT NULL DEFAULT 0,  -- 1 = feeder claim cannot change owners (admin override)
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
CREATE INDEX IF NOT EXISTS idx_feeders_device_mac ON feeders(device_mac);
-- Partial unique index on users(feeder_claim_key) is created in models.get_db() migrations
-- after ALTER adds feeder_claim_key (executescript skips CREATE TABLE for existing DBs, so an
-- index here breaks upgrades with: no such column: feeder_claim_key → dashboard 502).
CREATE INDEX IF NOT EXISTS idx_connections_feeder ON connections(feeder_id);
CREATE INDEX IF NOT EXISTS idx_connections_connected ON connections(connected_at);
CREATE INDEX IF NOT EXISTS idx_activity_timestamp ON activity_log(timestamp DESC);

-- Outputs (network_admin-scoped, admin sees all)
CREATE TABLE IF NOT EXISTS outputs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    output_type TEXT NOT NULL,               -- 'json', 'beast_raw', 'cot'
    output_format TEXT NOT NULL DEFAULT 'as_is',  -- 'as_is' (JSON/API as-is) or 'cot' (Cursor on Target)
    use_cotproxy BOOLEAN NOT NULL DEFAULT 0,     -- when output_format='cot': apply transforms (COTProxy-style)
    mode TEXT NOT NULL DEFAULT 'api',        -- 'api' (key-authenticated inbound) or 'push' (outbound)
    config TEXT NOT NULL DEFAULT '{}',       -- JSON: push_url, push_interval, cot_url, etc.
    created_by INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    notes TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (created_by) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_outputs_created_by ON outputs(created_by);
CREATE INDEX IF NOT EXISTS idx_outputs_type ON outputs(output_type);

-- Output API keys (hashed, shown once on creation)
CREATE TABLE IF NOT EXISTS output_api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    output_id INTEGER NOT NULL UNIQUE,
    key_hash TEXT NOT NULL,          -- SHA-256 hex of the raw key
    key_prefix TEXT NOT NULL,        -- first 12 chars for display
    key_display TEXT NOT NULL DEFAULT '', -- full raw key stored for display (shown in UI)
    key_type TEXT NOT NULL DEFAULT 'single_use', -- 'single_use' or 'durable'
    status TEXT NOT NULL DEFAULT 'ready', -- 'ready' or 'used'
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_used DATETIME,
    FOREIGN KEY (output_id) REFERENCES outputs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_output_keys_hash ON output_api_keys(key_hash);

-- Signal table for beast-proxy to drop active output connections on key regen
CREATE TABLE IF NOT EXISTS output_drop_signals (
    output_id INTEGER PRIMARY KEY,
    signaled_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_output_keys_type ON output_api_keys(key_type);

-- COTProxy-style transforms per output (when output_format='cot' and use_cotproxy=1)
-- Maps ICAO hex to callsign, type, icon, etc. for CoT display (same concept as known_craft.csv)
CREATE TABLE IF NOT EXISTS cot_transforms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    output_id INTEGER NOT NULL,
    domain TEXT,
    agency TEXT,
    reg TEXT,
    callsign TEXT,
    type TEXT,
    model TEXT,
    hex TEXT NOT NULL,       -- ICAO 24-bit hex (match key)
    cot TEXT,                -- CoT type string (e.g. a-f-A-C-H, a-n-A-M-H-A)
    icon TEXT,
    remarks TEXT,
    video TEXT,
    link TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (output_id) REFERENCES outputs(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_cot_transforms_output ON cot_transforms(output_id);
CREATE INDEX IF NOT EXISTS idx_cot_transforms_hex ON cot_transforms(output_id, hex);

-- CoT push TLS client certificates (encrypted at rest; only output owner can upload/replace; never returned to UI or admins)
CREATE TABLE IF NOT EXISTS output_cot_certs (
    output_id INTEGER PRIMARY KEY,
    cert_encrypted TEXT NOT NULL,
    key_encrypted TEXT NOT NULL,
    ca_encrypted TEXT,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (output_id) REFERENCES outputs(id) ON DELETE CASCADE
);
