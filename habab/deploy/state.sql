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

CREATE TABLE IF NOT EXISTS ha_demo_chat (
    session_hash char(64) PRIMARY KEY,
    messages jsonb NOT NULL DEFAULT '[]'::jsonb,
    window_started timestamptz NOT NULL DEFAULT now(),
    turn_count integer NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS ha_demo_chat_budget (
    bucket timestamptz PRIMARY KEY,
    count integer NOT NULL DEFAULT 0
);
