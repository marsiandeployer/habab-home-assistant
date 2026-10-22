"""Same-origin API for the Home Assistant frontend's mock-only public demo."""

import hashlib
import hmac
import os
import secrets

from flask import Blueprint, current_app, jsonify, request, session
from flask.sessions import SecureCookieSessionInterface

from habab_demo.store import HADemoRateLimit, HADemoStore

ha_demo_bp = Blueprint('ha_demo', __name__, url_prefix='/api/ha-demo')
DEMO_HOST = 'habab-demo.wpmix.net'
PUBLIC_HOSTS = {'habab.ru', 'www.habab.ru', DEMO_HOST}
LOCAL_QA_HOSTS = {'localhost', '127.0.0.1'}


class HADemoSessionInterface(SecureCookieSessionInterface):
    """Scope cross-site cookie settings to the anonymous demo host only."""
    def _demo(self):
        return request.host.split(':', 1)[0].lower() == DEMO_HOST

    def get_cookie_name(self, app):
        return '__Host-habab_demo' if self._demo() else super().get_cookie_name(app)

    def get_cookie_domain(self, app):
        return None if self._demo() else super().get_cookie_domain(app)

    def get_cookie_path(self, app):
        return '/' if self._demo() else super().get_cookie_path(app)

    def get_cookie_secure(self, app):
        return True if self._demo() else super().get_cookie_secure(app)

    def get_cookie_samesite(self, app):
        return 'None' if self._demo() else super().get_cookie_samesite(app)

    def get_cookie_partitioned(self, app):
        return True if self._demo() else super().get_cookie_partitioned(app)

LIGHTS = {
    'light.living_room_floor_lamp', 'light.living_room_spotlights',
    'light.kitchen_spotlights', 'light.kitchen_worktop',
    'light.entrance_ceiling', 'light.bedroom_ceiling', 'light.bedside_lamp',
    'light.office_desk_lamp', 'light.garden_path',
}
CLIMATE = {'climate.living_room'}
SCENES = {'scene.evening', 'scene.focus'}
SEED = {
    entity_id: {'state': state, **({'attributes': {'brightness': 178}} if state == 'on' and entity_id.startswith('light.') else {})}
    for entity_id, state in {
        'light.living_room_floor_lamp': 'on',
        'light.living_room_spotlights': 'on',
        'light.kitchen_spotlights': 'on',
        'light.kitchen_worktop': 'off',
        'light.entrance_ceiling': 'off',
        'light.bedroom_ceiling': 'on',
        'light.bedside_lamp': 'off',
        'light.office_desk_lamp': 'on',
        'light.garden_path': 'off',
        'climate.living_room': 'heat',
    }.items()
}


def _store():
    configured = current_app.config.get('HA_DEMO_STORE')
    if configured is not None:
        return configured
    dsn = (current_app.config.get('HA_DEMO_DATABASE_DSN') or os.getenv('HA_DEMO_DATABASE_DSN') or '').strip()
    if not dsn:
        raise RuntimeError('HA demo database is not configured')
    return HADemoStore(dsn)


def _session_hash():
    session.setdefault('ha_demo_session', secrets.token_urlsafe(32))
    secret = str(current_app.secret_key).encode()
    return hmac.new(secret, session['ha_demo_session'].encode(), hashlib.sha256).hexdigest()


def _same_origin():
    origin = request.headers.get('Origin', '')
    host = request.host.split(':', 1)[0].lower()
    expected = f'https://{host}' if host in PUBLIC_HOSTS else (
        f'{request.scheme}://{request.host}' if host in LOCAL_QA_HOSTS else ''
    )
    return bool(expected and origin == expected)


@ha_demo_bp.before_request
def restrict_demo_api_hosts():
    host = request.host.split(':', 1)[0].lower()
    if host not in PUBLIC_HOSTS | LOCAL_QA_HOSTS:
        return jsonify(error='unavailable'), 404


@ha_demo_bp.after_request
def prevent_demo_state_caching(response):
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Robots-Tag'] = 'noindex, nofollow'
    return response


@ha_demo_bp.get('/state')
def get_demo_state():
    try:
        saved = _store().get_state(_session_hash(), SEED)
    except Exception:
        current_app.logger.warning('Home Assistant demo state read failed')
        return jsonify(error='Демо временно недоступно. Обновите страницу позже.'), 503
    merged = {**SEED, **saved}
    return jsonify(states=merged)


@ha_demo_bp.post('/state')
def put_demo_state():
    if not _same_origin():
        return jsonify(error='origin'), 403
    if request.content_length and request.content_length > 2048:
        return jsonify(error='too_large'), 413
    body = request.get_json(silent=True)
    items = body.get('entities') if isinstance(body, dict) else None
    if not isinstance(items, list) or not 1 <= len(items) <= 10:
        return jsonify(error='invalid'), 400

    updates = {}
    for item in items:
        if not isinstance(item, dict):
            return jsonify(error='invalid'), 400
        entity = item.get('entity_id')
        state = item.get('state')
        if not isinstance(entity, str) or not isinstance(state, str):
            return jsonify(error='invalid'), 400
        if entity in LIGHTS:
            if state not in {'on', 'off'}:
                return jsonify(error='invalid'), 400
            brightness = item.get('brightness')
            if brightness is not None and (type(brightness) is not int or not 0 <= brightness <= 255):
                return jsonify(error='invalid'), 400
            updates[entity] = {'state': state, **({'attributes': {'brightness': brightness}} if brightness is not None else {})}
        elif entity in CLIMATE:
            if state not in {'heat', 'off', 'auto'}:
                return jsonify(error='invalid'), 400
            temperature = item.get('temperature')
            if temperature is not None and (type(temperature) not in {int, float} or not 7 <= temperature <= 35):
                return jsonify(error='invalid'), 400
            updates[entity] = {'state': state, **({'attributes': {'temperature': temperature}} if temperature is not None else {})}
        elif entity in SCENES:
            if state != 'scening':
                return jsonify(error='invalid'), 400
            updates[entity] = {'state': state}
        else:
            return jsonify(error='invalid'), 400

    try:
        _store().update_state(_session_hash(), updates, SEED)
    except HADemoRateLimit:
        return jsonify(error='rate_limited'), 429
    except Exception:
        current_app.logger.warning('Home Assistant demo state write failed')
        return jsonify(error='Демо временно недоступно. Изменение не сохранено.'), 503
    return jsonify(saved=True)
