-- VIGILANT Proxmox VM Monitor — PostgreSQL schema
-- The central app creates the database and runs this automatically on start-up
-- (python monitor.py --mode init-db does the same). Running it by hand is optional:
--   psql -U postgres -c "CREATE DATABASE vigilant_vm_monitor;"
--   psql -U postgres -d vigilant_vm_monitor -f schema.sql

-- Static information about each monitored VM (kept in sync with config.py)
CREATE TABLE IF NOT EXISTS vm_info (
    vm_id        TEXT PRIMARY KEY,
    hostname     TEXT NOT NULL,
    ip_address   TEXT NOT NULL,
    port         INTEGER NOT NULL DEFAULT 5000,
    last_seen    TIMESTAMP,
    updated_at   TIMESTAMP NOT NULL DEFAULT NOW()
);

-- One row per Start -> Stop monitoring run
CREATE TABLE IF NOT EXISTS monitor_sessions (
    session_id       BIGSERIAL PRIMARY KEY,
    started_at       TIMESTAMP NOT NULL DEFAULT NOW(),
    ended_at         TIMESTAMP,
    central_host     TEXT,
    vms_monitored    TEXT NOT NULL,
    poll_interval_s  REAL,
    failure_limit    INTEGER,
    thresholds_json  JSONB
);

-- One row per VM per polling cycle (every 5 s). Metrics are NULL when the VM did not answer.
CREATE TABLE IF NOT EXISTS resource_samples (
    sample_id      BIGSERIAL PRIMARY KEY,
    session_id     BIGINT REFERENCES monitor_sessions(session_id) ON DELETE CASCADE,
    sampled_at     TIMESTAMP NOT NULL DEFAULT NOW(),
    vm_id          TEXT NOT NULL REFERENCES vm_info(vm_id) ON UPDATE CASCADE,
    hostname       TEXT,
    ip_address     TEXT,
    cpu_percent    REAL,
    ram_percent    REAL,
    disk_percent   REAL,
    severity       TEXT NOT NULL,          -- NORMAL / ELEVATED / WARNING / CRITICAL / OFFLINE / UNKNOWN
    status         TEXT NOT NULL,          -- ONLINE / RETRYING / OFFLINE / RECOVERED / CONNECTING
    top_processes  JSONB
);

-- Alerts, offline events, recovery events, info and alarm tests
CREATE TABLE IF NOT EXISTS monitor_events (
    event_id         BIGSERIAL PRIMARY KEY,
    session_id       BIGINT REFERENCES monitor_sessions(session_id) ON DELETE CASCADE,
    occurred_at      TIMESTAMP NOT NULL DEFAULT NOW(),
    vm_id            TEXT,
    hostname         TEXT,
    ip_address       TEXT,
    event_type       TEXT NOT NULL CHECK (event_type IN ('THRESHOLD','RESOLVED','OFFLINE','RECOVERED','INFO','TEST')),
    metric           TEXT,
    metric_value     REAL,
    threshold_value  REAL,
    severity         TEXT NOT NULL,        -- ELEVATED / WARNING / CRITICAL / NORMAL / OFFLINE / RECOVERED / INFO / TEST
    vm_status        TEXT,
    message          TEXT NOT NULL
);

-- Yellow / Orange / Red thresholds per metric (edited on the Threshold Settings page)
CREATE TABLE IF NOT EXISTS threshold_settings (
    metric      TEXT PRIMARY KEY CHECK (metric IN ('CPU','RAM','DISK')),
    yellow      REAL NOT NULL,
    orange      REAL NOT NULL,
    red         REAL NOT NULL,
    updated_at  TIMESTAMP NOT NULL DEFAULT NOW(),
    CHECK (0 < yellow AND yellow < orange AND orange < red AND red <= 100)
);

-- Misc. key/value settings (data retention)
CREATE TABLE IF NOT EXISTS app_settings (
    setting_key    TEXT PRIMARY KEY,
    setting_value  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_samples_time     ON resource_samples(sampled_at);
CREATE INDEX IF NOT EXISTS idx_samples_session  ON resource_samples(session_id);
CREATE INDEX IF NOT EXISTS idx_samples_vm_time  ON resource_samples(vm_id, sampled_at);
CREATE INDEX IF NOT EXISTS idx_events_time      ON monitor_events(occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_session   ON monitor_events(session_id);
CREATE INDEX IF NOT EXISTS idx_events_vm        ON monitor_events(vm_id);

-- Convenience views for reporting / demonstration
CREATE OR REPLACE VIEW alert_events AS
    SELECT * FROM monitor_events WHERE event_type = 'THRESHOLD';
CREATE OR REPLACE VIEW offline_recovery_events AS
    SELECT * FROM monitor_events WHERE event_type IN ('OFFLINE','RECOVERED');
