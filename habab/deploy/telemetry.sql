-- Habab opt-in telemetry, separate from demo sessions and chat histories.
CREATE TABLE IF NOT EXISTS ha_telemetry_installations (
    installation_hash char(64) PRIMARY KEY,
    preferences jsonb NOT NULL,
    analytics jsonb,
    last_analytics_at timestamptz,
    diagnostic_window timestamptz NOT NULL DEFAULT now(),
    diagnostic_count integer NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS ha_telemetry_diagnostics (
    id bigserial PRIMARY KEY,
    installation_hash char(64) NOT NULL REFERENCES ha_telemetry_installations ON DELETE CASCADE,
    payload jsonb NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ha_telemetry_diagnostics_received_idx
    ON ha_telemetry_diagnostics (received_at);
CREATE TABLE IF NOT EXISTS ha_telemetry_budget (
    bucket timestamptz PRIMARY KEY,
    count integer NOT NULL DEFAULT 0
);
