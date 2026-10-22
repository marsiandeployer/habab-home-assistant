"""Bounded opt-in telemetry ingestion. No HA tokens, cookies or free-form logs."""
import hashlib
import hmac
import re

from flask import Blueprint, current_app, jsonify, request

telemetry_bp = Blueprint('habab_telemetry', __name__)
FLAGS = {'base', 'usage', 'statistics', 'diagnostics'}
COUNTS = {'devices', 'entities', 'automations', 'users'}


class InvalidTelemetry(ValueError):
    pass


class TelemetryRateLimit(Exception):
    pass


def fields(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        raise InvalidTelemetry('invalid_fields')


def string(value, pattern, maximum=100):
    if not isinstance(value, str) or not 1 <= len(value) <= maximum or not re.fullmatch(pattern, value):
        raise InvalidTelemetry('invalid_value')


def count(value):
    if type(value) is not int or not 0 <= value <= 1_000_000:
        raise InvalidTelemetry('invalid_count')


def validate_payload(body):
    fields(body, {'schema', 'installation_id', 'kind', 'preferences', 'data'})
    if type(body['schema']) is not int or body['schema'] != 1:
        raise InvalidTelemetry('invalid_schema')
    string(body['installation_id'], r'[a-f0-9]{32}', 32)
    preferences = body['preferences']
    fields(preferences, FLAGS)
    if any(type(value) is not bool for value in preferences.values()):
        raise InvalidTelemetry('invalid_preferences')
    if (preferences['usage'] or preferences['statistics']) and not preferences['base']:
        raise InvalidTelemetry('base_required')
    kind, data = body['kind'], body['data']
    if not isinstance(kind, str):
        raise InvalidTelemetry('invalid_kind')
    if kind in {'withdraw', 'preferences'}:
        fields(data, set())
        if kind == 'withdraw' and any(preferences.values()):
            raise InvalidTelemetry('invalid_withdrawal')
    elif kind == 'analytics':
        if not preferences['base']:
            raise InvalidTelemetry('consent_required')
        required = {'base'} | ({'usage'} if preferences['usage'] else set()) | ({'statistics'} if preferences['statistics'] else set())
        fields(data, required)
        base = data['base']
        fields(base, {'ha_version', 'habab_version', 'installation_type', 'arch', 'python_version'})
        for value in base.values():
            string(value, r'[A-Za-z0-9 _./+()-]+')
        if 'usage' in data:
            usage = data['usage']
            fields(usage, {'integrations'}, {'device_models'})
            integrations = usage['integrations']
            if not isinstance(integrations, list) or len(integrations) > 500:
                raise InvalidTelemetry('too_many_integrations')
            for domain in integrations:
                string(domain, r'[a-z0-9_]+', 80)
            models = usage.get('device_models', [])
            if not isinstance(models, list) or len(models) > 200:
                raise InvalidTelemetry('too_many_models')
            for model in models:
                fields(model, {'manufacturer', 'model', 'count'})
                for field in ('manufacturer', 'model'):
                    string(model[field], r'[\w .+()-]+', 80)
                count(model['count'])
        if 'statistics' in data:
            fields(data['statistics'], COUNTS)
            for value in data['statistics'].values():
                count(value)
    elif kind == 'diagnostic':
        if not preferences['diagnostics']:
            raise InvalidTelemetry('consent_required')
        fields(data, {'error_type', 'component', 'frames'})
        string(data['error_type'], r'[A-Za-z_][A-Za-z0-9_.]*')
        string(data['component'], r'[a-zA-Z0-9_.]+', 160)
        frames = data['frames']
        if not isinstance(frames, list) or len(frames) > 12:
            raise InvalidTelemetry('too_many_frames')
        for frame in frames:
            fields(frame, {'file', 'function', 'line'})
            string(frame['file'], r'[\w.-]+')
            string(frame['function'], r'[A-Za-z0-9_.<>]+')
            count(frame['line'])
    else:
        raise InvalidTelemetry('unknown_kind')
    return body


def cleanup(connection):
    connection.execute("DELETE FROM ha_telemetry_diagnostics WHERE received_at < now()-interval '60 days'")
    connection.execute("DELETE FROM ha_telemetry_installations WHERE updated_at < now()-interval '60 days'")
    connection.execute("DELETE FROM ha_telemetry_budget WHERE bucket < now()-interval '2 days'")


def ingest(dsn, installation_hash, body):
    import psycopg
    from psycopg.types.json import Jsonb
    preferences, kind = body['preferences'], body['kind']
    with psycopg.connect(dsn, connect_timeout=5) as connection:
        budget = connection.execute('''INSERT INTO ha_telemetry_budget (bucket,count)
            VALUES (date_trunc('hour',now()),1) ON CONFLICT (bucket)
            DO UPDATE SET count=ha_telemetry_budget.count+1 RETURNING count''').fetchone()[0]
        if budget > 2000:
            raise TelemetryRateLimit()
        cleanup(connection)
        connection.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('ha-telemetry:'+installation_hash,))
        if kind == 'withdraw' or not any(preferences.values()):
            connection.execute('DELETE FROM ha_telemetry_installations WHERE installation_hash=%s', (installation_hash,))
            return
        connection.execute('''INSERT INTO ha_telemetry_installations (installation_hash,preferences)
            VALUES (%s,%s) ON CONFLICT (installation_hash) DO NOTHING''', (installation_hash, Jsonb(preferences)))
        previous = connection.execute('SELECT analytics FROM ha_telemetry_installations WHERE installation_hash=%s', (installation_hash,)).fetchone()[0]
        if previous:
            previous['preferences'] = preferences
            if not preferences['base']:
                previous = None
            else:
                for category in ('usage', 'statistics'):
                    if not preferences[category]:
                        previous['data'].pop(category, None)
        if not preferences['diagnostics']:
            connection.execute('DELETE FROM ha_telemetry_diagnostics WHERE installation_hash=%s', (installation_hash,))
        connection.execute('''UPDATE ha_telemetry_installations SET preferences=%s,analytics=%s,updated_at=now()
            WHERE installation_hash=%s''', (Jsonb(preferences), Jsonb(previous) if previous else None, installation_hash))
        if kind == 'analytics':
            row = connection.execute('''UPDATE ha_telemetry_installations SET analytics=%s,last_analytics_at=now()
                WHERE installation_hash=%s AND (last_analytics_at IS NULL OR last_analytics_at < now()-interval '1 minute')
                RETURNING installation_hash''', (Jsonb({'preferences': preferences, 'data': body['data']}), installation_hash)).fetchone()
            if row is None:
                raise TelemetryRateLimit()
        elif kind == 'diagnostic':
            row = connection.execute('''UPDATE ha_telemetry_installations SET
                diagnostic_count=CASE WHEN diagnostic_window < now()-interval '1 day' THEN 1 ELSE diagnostic_count+1 END,
                diagnostic_window=CASE WHEN diagnostic_window < now()-interval '1 day' THEN now() ELSE diagnostic_window END
                WHERE installation_hash=%s AND (diagnostic_window < now()-interval '1 day' OR diagnostic_count < 20)
                RETURNING installation_hash''', (installation_hash,)).fetchone()
            if row is None:
                raise TelemetryRateLimit()
            connection.execute('INSERT INTO ha_telemetry_diagnostics (installation_hash,payload) VALUES (%s,%s)',
                               (installation_hash, Jsonb(body['data'])))


@telemetry_bp.post('/api/ha-telemetry')
def receive_telemetry():
    if request.content_length is None or request.content_length > 32768:
        return jsonify(error='too_large'), 413
    if request.headers.get('Origin'):
        return jsonify(error='server_requests_only'), 403
    if not request.is_json:
        return jsonify(error='invalid_json'), 400
    try:
        body = validate_payload(request.get_json(silent=True))
        secret = current_app.secret_key
        if isinstance(secret, str):
            secret = secret.encode()
        installation_hash = hmac.new(secret, b'ha-telemetry:'+body['installation_id'].encode(), hashlib.sha256).hexdigest()
        ingest(current_app.config['HA_DEMO_DATABASE_DSN'], installation_hash, body)
    except InvalidTelemetry:
        return jsonify(error='invalid_telemetry'), 400
    except TelemetryRateLimit:
        return jsonify(error='rate_limited'), 429
    except Exception as error:
        current_app.logger.warning('Telemetry storage unavailable: %s', type(error).__name__)
        return jsonify(error='unavailable'), 503
    return jsonify(accepted=True), 202
