"""Durable, bounded state for the isolated Home Assistant frontend demo."""

from contextlib import contextmanager

class HADemoRateLimit(Exception):
    pass


class HADemoStore:
    def __init__(self, dsn):
        self.dsn = dsn

    def chat_history(self, session_hash):
        import psycopg
        with psycopg.connect(self.dsn, connect_timeout=5) as connection:
            row = connection.execute("SELECT messages FROM ha_demo_chat WHERE session_hash=%s AND updated_at >= now()-interval '7 days'", (session_hash,)).fetchone()
            return row[0] if row else []

    def chat_reset(self, session_hash):
        import psycopg
        with psycopg.connect(self.dsn, connect_timeout=5) as connection:
            connection.execute('UPDATE ha_demo_chat SET messages=\'[]\'::jsonb, updated_at=now() WHERE session_hash=%s', (session_hash,))

    def chat_turn(self, session_hash, text, reply):
        import psycopg
        from psycopg.types.json import Jsonb
        from .chat import ChatError
        with psycopg.connect(self.dsn, connect_timeout=5, autocommit=True) as connection:
            # Quotas survive workers, restarts and the New chat action.
            with connection.transaction():
                count = connection.execute('''INSERT INTO ha_demo_chat_budget (bucket, count)
                    VALUES (date_trunc('hour', now()), 1) ON CONFLICT (bucket)
                    DO UPDATE SET count=ha_demo_chat_budget.count+1 RETURNING count''').fetchone()[0]
                if count > 200:
                    raise ChatError('rate_limited')
            with connection.transaction():
                locked = connection.execute('SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0))', ('ha-chat:'+session_hash,)).fetchone()[0]
                if not locked:
                    raise ChatError('busy')
                connection.execute('''INSERT INTO ha_demo_chat (session_hash) VALUES (%s)
                    ON CONFLICT (session_hash) DO NOTHING''', (session_hash,))
                row = connection.execute('''UPDATE ha_demo_chat SET
                    messages=CASE WHEN updated_at < now()-interval '7 days' THEN '[]'::jsonb ELSE messages END,
                    turn_count=CASE WHEN window_started < now()-interval '1 minute' THEN 1 ELSE turn_count+1 END,
                    window_started=CASE WHEN window_started < now()-interval '1 minute' THEN now() ELSE window_started END
                    WHERE session_hash=%s RETURNING messages, turn_count''', (session_hash,)).fetchone()
                if row[1] > 6:
                    raise ChatError('rate_limited')
                messages = row[0] + [{'role': 'user', 'content': text}]
                answer = reply(messages)
                messages = (messages + [{'role': 'assistant', 'content': answer}])[-20:]
                connection.execute('UPDATE ha_demo_chat SET messages=%s, updated_at=now() WHERE session_hash=%s',
                                   (Jsonb(messages), session_hash))
                connection.execute("DELETE FROM ha_demo_chat WHERE updated_at < now()-interval '7 days'")
                connection.execute("DELETE FROM ha_demo_chat_budget WHERE bucket < now()-interval '2 days'")
                return messages

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
