-- ============================================================
-- Smart Building DR Middleware — Database Schema
-- TimescaleDB on PostgreSQL 16
-- ============================================================

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ------------------------------------------------------------
-- 1. ZONES — authoritative zone metadata
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS zones (
    zone_id     TEXT        PRIMARY KEY,
    zone_name   TEXT        NOT NULL,
    floor       SMALLINT    NOT NULL,
    capacity    SMALLINT    NOT NULL,  -- max persons
    area_sqft   SMALLINT    NOT NULL,  -- for kW/sqft metrics
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO zones (zone_id, zone_name, floor, capacity, area_sqft) VALUES
    ('zone_1', 'Conference Room A', 1, 20,  800),
    ('zone_2', 'Open Office B',     2, 50, 2400),
    ('zone_3', 'Lab C',             2, 15,  600),
    ('zone_4', 'Lobby',             1, 30, 1200)
ON CONFLICT (zone_id) DO NOTHING;


-- ------------------------------------------------------------
-- 2. SENSOR_READINGS — raw IoT hypertable (core time-series)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sensor_readings (
    id          BIGSERIAL,
    zone_id     TEXT            NOT NULL,
    sensor_type TEXT            NOT NULL,
    -- temperature | humidity | co2 | occupancy | energy_kw
    value       DOUBLE PRECISION NOT NULL,
    unit        TEXT            NOT NULL,
    timestamp   TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    PRIMARY KEY (id, timestamp)
);

SELECT create_hypertable(
    'sensor_readings', 'timestamp',
    chunk_time_interval => INTERVAL '1 day',
    if_not_exists       => TRUE
);

CREATE INDEX IF NOT EXISTS idx_sr_zone_sensor_time
    ON sensor_readings (zone_id, sensor_type, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_sr_sensor_time
    ON sensor_readings (sensor_type, timestamp DESC);

-- 30-day raw retention
SELECT add_retention_policy(
    'sensor_readings', INTERVAL '30 days', if_not_exists => TRUE
);

-- 5-minute continuous aggregate
CREATE MATERIALIZED VIEW IF NOT EXISTS readings_5min
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('5 minutes', timestamp) AS bucket,
    zone_id,
    sensor_type,
    AVG(value)  AS avg_value,
    MIN(value)  AS min_value,
    MAX(value)  AS max_value,
    COUNT(*)    AS sample_count
FROM sensor_readings
GROUP BY bucket, zone_id, sensor_type
WITH NO DATA;

SELECT add_continuous_aggregate_policy(
    'readings_5min',
    start_offset      => INTERVAL '1 hour',
    end_offset        => INTERVAL '5 minutes',
    schedule_interval => INTERVAL '5 minutes',
    if_not_exists     => TRUE
);

-- Convenience: latest reading per zone × sensor
CREATE OR REPLACE VIEW latest_readings AS
SELECT DISTINCT ON (zone_id, sensor_type)
    zone_id, sensor_type, value, unit, timestamp
FROM sensor_readings
ORDER BY zone_id, sensor_type, timestamp DESC;


-- ------------------------------------------------------------
-- 3. OCCUPANCY_BASELINES — hourly learned averages per zone
--    Populated/updated by the ML predictor on startup and
--    periodically. Drives time-of-day occupancy forecasting.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS occupancy_baselines (
    zone_id     TEXT        NOT NULL,
    hour_of_day SMALLINT    NOT NULL CHECK (hour_of_day BETWEEN 0 AND 23),
    day_of_week SMALLINT    NOT NULL CHECK (day_of_week BETWEEN 0 AND 6),
    -- 0=Mon … 6=Sun
    avg_occupancy  DOUBLE PRECISION NOT NULL DEFAULT 0,
    sample_count   INTEGER          NOT NULL DEFAULT 0,
    updated_at     TIMESTAMPTZ      DEFAULT NOW(),
    PRIMARY KEY (zone_id, hour_of_day, day_of_week)
);


-- ------------------------------------------------------------
-- 4. SETPOINT_LOG — every BACnet setpoint write
--    Written by the backend before issuing BACnet command.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS setpoint_log (
    id              BIGSERIAL   PRIMARY KEY,
    zone_id         TEXT        NOT NULL,
    setpoint_type   TEXT        NOT NULL,  -- cooling_setpoint | heating_setpoint
    value_before    DOUBLE PRECISION,
    value_after     DOUBLE PRECISION NOT NULL,
    unit            TEXT        NOT NULL DEFAULT '°C',
    source          TEXT        NOT NULL,  -- dr_engine | manual | schedule
    dr_event_id     UUID,                  -- FK set after dr_events table created
    timestamp       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sp_zone_time
    ON setpoint_log (zone_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_sp_event
    ON setpoint_log (dr_event_id);


-- ------------------------------------------------------------
-- 5. DR_EVENTS — one row per demand response event
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dr_events (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    triggered_by        TEXT        NOT NULL DEFAULT 'api',
    -- api | utility_webhook | schedule
    status              TEXT        NOT NULL DEFAULT 'pending',
    -- pending | active | completed | aborted
    target_kw_reduction DOUBLE PRECISION NOT NULL,
    actual_kw_reduction DOUBLE PRECISION,       -- measured after completion
    kwh_avoided         DOUBLE PRECISION,       -- kWh over duration
    duration_minutes    INTEGER     NOT NULL,
    comfort_maintained  BOOLEAN,                -- all zones stayed in bounds?
    zones_affected      TEXT[],                 -- array of zone_ids
    started_at          TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notes               TEXT
);

-- Back-fill the FK now that dr_events exists
ALTER TABLE setpoint_log
    ADD CONSTRAINT fk_sp_dr_event
    FOREIGN KEY (dr_event_id) REFERENCES dr_events(id)
    ON DELETE SET NULL
    DEFERRABLE INITIALLY DEFERRED;


-- ------------------------------------------------------------
-- 6. DR_ZONE_ACTIONS — per-zone plan for each DR event
--    One row per zone per DR event.
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dr_zone_actions (
    id                  BIGSERIAL   PRIMARY KEY,
    dr_event_id         UUID        NOT NULL REFERENCES dr_events(id) ON DELETE CASCADE,
    zone_id             TEXT        NOT NULL,
    predicted_occupancy INTEGER,
    occupancy_ratio     DOUBLE PRECISION,   -- occupancy / capacity
    kw_before           DOUBLE PRECISION,
    kw_target           DOUBLE PRECISION,
    kw_actual           DOUBLE PRECISION,
    setpoint_delta_c    DOUBLE PRECISION,   -- °C raised on cooling setpoint
    comfort_bound_hit   BOOLEAN     DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dza_event
    ON dr_zone_actions (dr_event_id);


-- ------------------------------------------------------------
-- 7. AUDIT_TRAIL — human-readable event log for the dashboard
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_trail (
    id          BIGSERIAL   PRIMARY KEY,
    event_type  TEXT        NOT NULL,
    -- sensor_anomaly | dr_triggered | dr_completed | setpoint_write
    -- comfort_violation | bacnet_error | predictor_updated
    severity    TEXT        NOT NULL DEFAULT 'info',
    -- info | warn | error
    zone_id     TEXT,
    dr_event_id UUID,
    message     TEXT        NOT NULL,
    metadata    JSONB,
    timestamp   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_time
    ON audit_trail (timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_audit_type
    ON audit_trail (event_type, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_audit_event
    ON audit_trail (dr_event_id);

-- Seed initial audit entry
INSERT INTO audit_trail (event_type, severity, message)
VALUES ('system_start', 'info', 'Database initialized. Smart Building DR Middleware ready.');
