"""The Windows assistant diagnostic emits only small, redacted summaries."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

SCRIPT = Path(__file__).parents[1] / 'diagnose-assistant.py'
spec = importlib.util.spec_from_file_location('diagnose_assistant', SCRIPT)
diagnose_assistant = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnose_assistant)


def create_database(path):
    connection = sqlite3.connect(path)
    connection.executescript('''
        CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);
        CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT);
    ''')
    return connection


class DiagnoseAssistantTests(unittest.TestCase):
    def test_redacts_error_payload_and_user_message_and_keeps_only_whitelisted_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'opencode.db'
            connection = create_database(path)
            error = {
                'name': 'APIError',
                'data': {
                    'status': 400,
                    'message': 'missing_reasoning_content PRIVATE_MESSAGE_SECRET',
                    'responseBody': 'quota_exceeded PRIVATE_BODY_SECRET',
                    'headers': {'Authorization': 'PRIVATE_TOKEN_SECRET'},
                },
            }
            connection.execute('INSERT INTO message VALUES (?, ?, ?, ?, ?)',
                               ('a1', 'PRIVATE_SESSION_SECRET', 1, 1,
                                json.dumps({'role': 'assistant', 'finish': 'error', 'error': error})))
            connection.execute('INSERT INTO message VALUES (?, ?, ?, ?, ?)',
                               ('u1', 'PRIVATE_SESSION_SECRET', 2, 2,
                                json.dumps({'role': 'user', 'text': 'PRIVATE_USER_SECRET', 'error': error})))
            connection.commit()
            before = path.read_bytes()
            connection.close()

            result = diagnose_assistant.diagnose_database(path)

            self.assertEqual(result, {
                'db_present': True,
                'read_only': True,
                'status': 'ok',
                'assistant_errors': [{
                    'name': 'APIError', 'http_status': 400,
                    'missing_reasoning_content': True, 'mentions_reasoning_content': True, 'invalid_model': False, 'quota': True,
                    'mentions_tool_calls': False, 'mentions_context_limit': False, 'mentions_timeout': False,
                }],
            })
            self.assertFalse(set(('session_id', 'text', 'message', 'headers', 'path')) & set(result))
            self.assertNotIn(b'PRIVATE_', json.dumps(result).encode())
            self.assertEqual(path.read_bytes(), before)

    def test_unknown_error_name_maps_to_other_and_status_is_range_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'opencode.db'
            connection = create_database(path)
            data = {'role': 'assistant', 'finish': 'error', 'error': {
                'name': 'PrivateExceptionName', 'status': 999,
                'message': 'invalid_model quota_exceeded tool_calls context_length_exceeded timed out',
            }}
            connection.execute('INSERT INTO message VALUES (?, ?, ?, ?, ?)', ('a1', 's', 1, 1, json.dumps(data)))
            connection.commit()
            connection.close()
            result = diagnose_assistant.diagnose_database(path)
            self.assertEqual(result['assistant_errors'][0], {
                'name': 'other', 'http_status': None,
                'missing_reasoning_content': False, 'mentions_reasoning_content': False, 'invalid_model': True, 'quota': True,
                'mentions_tool_calls': True, 'mentions_context_limit': True, 'mentions_timeout': True,
            })

    def test_part_error_is_reported_when_assistant_message_has_no_error_object(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'opencode.db'
            connection = create_database(path)
            connection.execute('INSERT INTO message VALUES (?, ?, ?, ?, ?)',
                               ('a1', 's', 1, 1, json.dumps({'role': 'assistant', 'finish': 'error'})))
            connection.execute('INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)',
                               ('p1', 'a1', 's', 1, 1, json.dumps({'type': 'error', 'error': {'name': 'RateLimitError', 'status': 429}})))
            connection.commit()
            connection.close()
            result = diagnose_assistant.diagnose_database(path)
            self.assertEqual(result['assistant_errors'][0]['name'], 'RateLimitError')
            self.assertEqual(result['assistant_errors'][0]['http_status'], 429)

    def test_large_tool_output_does_not_hide_the_provider_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'opencode.db'
            connection = create_database(path)
            connection.execute('INSERT INTO message VALUES (?, ?, ?, ?, ?)',
                               ('a1', 's', 1, 1, json.dumps({'role': 'assistant', 'error': {'name': 'APIError', 'status': 400}})))
            connection.execute('INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)',
                               ('p1', 'a1', 's', 1, 1, json.dumps({'type': 'tool', 'output': 'PRIVATE_TOOL_OUTPUT' * 20000})))
            connection.commit()
            connection.close()
            result = diagnose_assistant.diagnose_database(path)
            self.assertEqual(result['status'], 'ok')
            self.assertEqual(result['assistant_errors'][0]['name'], 'APIError')
            self.assertNotIn('PRIVATE_TOOL_OUTPUT', json.dumps(result))

    def test_missing_malformed_and_unsupported_databases_are_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(diagnose_assistant.diagnose_database(root / 'missing.db'), {
                'db_present': False, 'read_only': True, 'status': 'missing_db', 'assistant_errors': [],
            })
            malformed = root / 'malformed.db'
            malformed.write_text('not sqlite', encoding='ascii')
            self.assertEqual(diagnose_assistant.diagnose_database(malformed)['status'], 'unreadable_db')
            unsupported = root / 'unsupported.db'
            connection = sqlite3.connect(unsupported)
            connection.execute('CREATE TABLE unrelated (value TEXT)')
            connection.commit()
            connection.close()
            self.assertEqual(diagnose_assistant.diagnose_database(unsupported)['status'], 'unsupported_schema')

    def test_recent_error_count_and_sqlite_field_size_are_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'opencode.db'
            connection = create_database(path)
            for index in range(5):
                data = {'role': 'assistant', 'finish': 'error', 'error': {'name': 'APIError', 'status': 400}}
                connection.execute('INSERT INTO message VALUES (?, ?, ?, ?, ?)',
                                   (f'a{index}', 's', index, index, json.dumps(data)))
            connection.commit()
            connection.close()
            result = diagnose_assistant.diagnose_database(path)
            self.assertEqual(len(result['assistant_errors']), diagnose_assistant.MAX_ERRORS)

            oversized = Path(directory) / 'oversized.db'
            connection = create_database(oversized)
            data = {'role': 'assistant', 'finish': 'error', 'error': {'message': 'x' * (diagnose_assistant.MAX_FIELD_BYTES + 10)}}
            connection.execute('INSERT INTO message VALUES (?, ?, ?, ?, ?)', ('a', 's', 1, 1, json.dumps(data)))
            connection.commit()
            connection.close()
            self.assertEqual(diagnose_assistant.diagnose_database(oversized)['status'], 'unreadable_db')


if __name__ == '__main__':
    unittest.main()
