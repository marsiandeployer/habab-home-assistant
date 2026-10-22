"""Durable, bounded state for the isolated Home Assistant frontend demo."""

from contextlib import contextmanager

class HADemoRateLimit(Exception):
    pass


class HADemoStore:
    def __init__(self, dsn):
        self.dsn = dsn

    @contextmanager
    def session_state(self, session_hash, seed):
        import psycopg
        from psycopg.rows import dict_row
        from psycopg.types.json import Jsonb

        with psycopg.connect(self.dsn, connect_timeout=5, autocommit=True, row_factory=dict_row) as connection:
            connection.execute(
                "DELETE FROM ha_demo_sessions WHERE updated_at < now() - interval '90 days'"
            )
            connection.execute(
                """INSERT INTO ha_demo_sessions (session_hash, state)
                   VALUES (%s, %s) ON CONFLICT (session_hash) DO NOTHING""",
                (session_hash, Jsonb(seed)),
            )
            yield connection

    def get_state(self, session_hash, seed):
        with self.session_state(session_hash, seed) as connection:
            row = connection.execute(
                "SELECT state FROM ha_demo_sessions WHERE session_hash=%s",
                (session_hash,),
            ).fetchone()
            return row['state']

    def update_state(self, session_hash, updates, seed):
        import psycopg
        from psycopg.rows import dict_row
        from psycopg.types.json import Jsonb

        with psycopg.connect(self.dsn, connect_timeout=5, autocommit=True, row_factory=dict_row) as connection:
            with connection.transaction():
                connection.execute(
                    """INSERT INTO ha_demo_sessions (session_hash, state)
                       VALUES (%s, %s) ON CONFLICT (session_hash) DO NOTHING""",
                    (session_hash, Jsonb(seed)),
                )
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    ('ha-demo:' + session_hash,),
                )
                row = connection.execute(
                    """UPDATE ha_demo_sessions
                       SET state = state || %s, updated_at = now(),
                           event_count = CASE
                             WHEN event_window_started < now() - interval '1 minute' THEN 1
                             ELSE event_count + 1 END,
                           event_window_started = CASE
                             WHEN event_window_started < now() - interval '1 minute' THEN now()
                             ELSE event_window_started END
                       WHERE session_hash=%s RETURNING event_count""",
                    (Jsonb(updates), session_hash),
                ).fetchone()
                if row['event_count'] > 120:
                    raise HADemoRateLimit()
