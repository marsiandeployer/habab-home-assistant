"""Read a small, redacted OpenCode error summary from the local HA volume."""
import json
from pathlib import Path
import sqlite3
import sys

DB_PATH = Path('/config/.habab-opencode/data/opencode/opencode.db')
MAX_FIELD_BYTES = 128 * 1024
MAX_MESSAGES = 200
MAX_PARTS = 500
MAX_ERRORS = 3
KNOWN_ERROR_NAMES = {
    'APIError', 'AuthenticationError', 'ContextOverflowError', 'InvalidModelError',
    'MessageAbortedError', 'ModelNotFoundError', 'ProviderAuthError',
    'ProviderError', 'RateLimitError', 'UnknownError',
}
FLAG_MARKERS = {
    'missing_reasoning_content': ('missing_reasoning_content',),
    'mentions_reasoning_content': ('reasoning_content',),
    'invalid_model': ('invalid_model', 'model_not_found', 'model not found'),
    'quota': ('insufficient_quota', 'quota_exceeded', 'billing_hard_limit', 'rate_limit_exceeded'),
    'mentions_tool_calls': ('tool_calls', 'tool calls'),
    'mentions_context_limit': ('context_length_exceeded', 'maximum context length', 'context_limit'),
    'mentions_timeout': ('timeout', 'timed out', 'etimedout'),
}


def _parse_json(value):
    if not isinstance(value, str) or len(value.encode('utf-8', errors='replace')) > MAX_FIELD_BYTES:
        raise ValueError('oversized or invalid JSON field')
    return json.loads(value)


def _schema_columns(connection, table):
    return {row[1] for row in connection.execute(f'PRAGMA table_info({table})')}


def _walk_error_values(value, depth=0):
    if depth > 4:
        return
    if isinstance(value, dict):
        for key, child in value.items():
            yield key, child
            if isinstance(child, (dict, list)):
                yield from _walk_error_values(child, depth + 1)
    elif isinstance(value, list):
        for child in value[:20]:
            if isinstance(child, (dict, list)):
                yield from _walk_error_values(child, depth + 1)


def _error_summary(error):
    if not isinstance(error, (dict, str)):
        error = {}
    values = list(_walk_error_values(error))
    if isinstance(error, str):
        values.append(('message', error[:MAX_FIELD_BYTES]))

    raw_name = next((str(value) for key, value in values if key in ('name', 'code', 'type') and isinstance(value, str)), '')
    name = raw_name if raw_name in KNOWN_ERROR_NAMES else 'other'
    status = None
    for key, value in values:
        if key in ('status', 'statusCode', 'status_code', 'httpStatus'):
            try:
                candidate = int(value)
            except (TypeError, ValueError):
                continue
            if 100 <= candidate <= 599:
                status = candidate
                break

    searchable = []
    for key, value in values:
        if key.lower() in ('message', 'responsebody', 'response_body', 'body', 'code'):
            if isinstance(value, str):
                searchable.append(value[:MAX_FIELD_BYTES].lower())
            elif isinstance(value, (dict, list)):
                searchable.append(json.dumps(value, ensure_ascii=True)[:MAX_FIELD_BYTES].lower())
    joined = '\n'.join(searchable)
    result = {'name': name, 'http_status': status}
    result.update({flag: any(marker in joined for marker in markers) for flag, markers in FLAG_MARKERS.items()})
    return result


def diagnose_database(path=DB_PATH):
    output = {'db_present': False, 'read_only': True, 'status': 'missing_db', 'assistant_errors': []}
    path = Path(path)
    if not path.is_file():
        return output
    output['db_present'] = True

    connection = None
    try:
        connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)
        connection.execute('PRAGMA query_only=ON')
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'message' not in tables or 'part' not in tables:
            output['status'] = 'unsupported_schema'
            return output
        if not {'id', 'data'} <= _schema_columns(connection, 'message') or not {'message_id', 'data'} <= _schema_columns(connection, 'part'):
            output['status'] = 'unsupported_schema'
            return output

        messages = []
        query = 'SELECT id, substr(data, 1, ?) FROM message ORDER BY rowid DESC LIMIT ?'
        for message_id, raw in connection.execute(query, (MAX_FIELD_BYTES + 1, MAX_MESSAGES)):
            data = _parse_json(raw)
            if isinstance(data, dict) and data.get('role') == 'assistant':
                messages.append((message_id, data))
        message_errors = []
        for message_id, data in messages:
            error = data.get('error')
            if error is not None or data.get('finish') == 'error':
                message_errors.append((message_id, _error_summary(error)))

        ids = [message_id for message_id, _ in messages]
        part_errors = {}
        if ids:
            placeholders = ','.join('?' for _ in ids)
            query = f"SELECT message_id, substr(data, 1, ?) FROM part WHERE message_id IN ({placeholders}) AND json_extract(data, '$.type') = 'error' ORDER BY rowid DESC LIMIT ?"
            for message_id, raw in connection.execute(query, (MAX_FIELD_BYTES + 1, *ids, MAX_PARTS)):
                data = _parse_json(raw)
                if isinstance(data, dict) and data.get('type') == 'error':
                    error = data.get('error', data)
                    part_errors.setdefault(message_id, _error_summary(error))

        summaries = {message_id: summary for message_id, summary in message_errors}
        for message_id, summary in part_errors.items():
            current = summaries.get(message_id)
            if current is None or (current['name'] == 'other' and current['http_status'] is None
                                   and not any(current[flag] for flag in FLAG_MARKERS)):
                summaries[message_id] = summary
        recent_ids = [message_id for message_id, _ in messages if message_id in summaries]
        output['assistant_errors'] = [summaries[message_id] for message_id in recent_ids[:MAX_ERRORS]]
        output['status'] = 'ok'
    except (sqlite3.Error, OSError, ValueError, TypeError, UnicodeError):
        output['status'] = 'unreadable_db'
        output['assistant_errors'] = []
    finally:
        if connection is not None:
            connection.close()
    return output


def main():
    print(json.dumps(diagnose_database(), separators=(',', ':'), ensure_ascii=True))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Never include arbitrary database content or exception strings in output.
        print('{"db_present":false,"read_only":true,"status":"diagnostic_failed","assistant_errors":[]}')
        raise SystemExit(1)
