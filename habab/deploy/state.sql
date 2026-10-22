-- Additive storage for anonymous, mock-only Home Assistant demo state.
CREATE TABLE IF NOT EXISTS ha_demo_sessions (
    session_hash char(64) PRIMARY KEY,
    state jsonb NOT NULL DEFAULT '{}'::jsonb,
    event_window_started timestamptz NOT NULL DEFAULT now(),
    event_count integer NOT NULL DEFAULT 0 CHECK (event_count >= 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ha_demo_sessions_updated_at_idx
    ON ha_demo_sessions (updated_at);
