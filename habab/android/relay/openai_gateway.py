"""Authenticated, bounded Gemini API transport for the owner's Android installation.

No HA access, model substitution, public demo context, or credential logging.
Habab credentials are hashed; Operator credentials stay in the private relay configuration across restarts.
"""
import argparse
import asyncio
from collections import deque
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import struct
import time
import tempfile
from urllib.parse import urlsplit

from aiohttp import ClientSession, ClientResponseError, ClientTimeout, ClientWSTimeout, WSMsgType, web

from beta_codes import BetaCodes
from usage_stats import UsageJournal, StreamUsage, token_counts
from provider_health import ProviderHealth
from telegram_speech import capability as speech_capability, telegram_voice_capability, register_routes as register_speech_routes

logger = logging.getLogger(__name__)

GEMINI_TEXT_BASE = 'https://generativelanguage.googleapis.com/v1beta/models'
DEEPSEEK_CHAT_COMPLETIONS = 'https://api.deepseek.com/chat/completions'
DEEPSEEK_TEXT_PROVIDER = 'deepseek'
GEMINI_TOKEN_URL = 'https://generativelanguage.googleapis.com/v1beta/auth_tokens'
GEMINI_CONSTRAINED_UPSTREAM = 'wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContentConstrained'
GEMINI_MODEL = 'gemini-3.8-live'
GEMINI_TEXT_MODEL = 'gemini-3.1-flash-lite'
GEMINI_TEXT_MODELS = (GEMINI_TEXT_MODEL, 'gemini-3.8-flash')
HABAB_TEXT_MODEL = 'habab-chat'
DEEPSEEK_TEXT_MODEL = 'deepseek-flash'
GEMINI_WEBSOCKET = 'wss://habab-voice.wpmix.net/gemini/ws'
VOICE_RELAY_URL = 'http://127.0.0.1:8326'
VOICE_RELAY_TIMEOUT = 15
VOICE_BOOTSTRAP_WIRE_MAX = 512 * 1024
VOICE_RELAY_RESPONSE_MAX = 512 * 1024
OPENAI_REALTIME_UPSTREAM = 'wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1'
OPENAI_DEMO_WEBSOCKET = 'wss://habab-voice.wpmix.net/openai/demo/ws'
TOKEN_RESPONSE_MAX = 64 * 1024
VOICE_FIRST_FRAME_TIMEOUT = 10
VOICE_SETUP_MAX_BYTES = 64 * 1024
BROWSER_VOICE_AUTH_TYPE = 'habab.voice.auth.v1'
DEMO_OPENAI_AUTH_TYPE = 'habab.demo.openai.auth.v1'
LEGACY_OPENAI_ROUTES = {'audio/speech', 'audio/transcriptions', 'realtime/client_secrets'}
TELEGRAM_METHODS = ('getMe', 'getUpdates', 'getChatMember', 'getFile', 'sendMessage', 'sendChatAction')
TELEGRAM_BOT_TOKEN = re.compile(r'[0-9]{1,20}:[A-Za-z0-9_-]{20,256}\Z')
TELEGRAM_API_BASE = 'https://api.telegram.org/bot'
TELEGRAM_REQUEST_MAX = 1024 * 1024
TELEGRAM_RESPONSE_MAX = 8 * 1024 * 1024
TELEGRAM_LONG_POLL_MAX = 25
TELEGRAM_REQUEST_TIMEOUT = 20
TELEGRAM_LONG_POLL_REQUEST_TIMEOUT = 35


def exact_web_origin(value):
    if not isinstance(value, str) or not value or value == '*':
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
        return (parsed.scheme in {'http', 'https'} and bool(parsed.hostname) and bool(parsed.netloc)
                and parsed.username is None and parsed.password is None and parsed.path == ''
                and (port is None or 1 <= port <= 65535)
                and not parsed.query and not parsed.fragment and value == f'{parsed.scheme}://{parsed.netloc}')
    except ValueError:
        return False


async def redacted_stream(content, secret, max_bytes):
    """Stream upstream bytes while redacting a secret even across chunk boundaries."""
    pending = b''
    total = 0
    overlap = max(1, len(secret))
    async for block in content.iter_chunked(16384):
        total += len(block)
        if total > max_bytes:
            raise ValueError('relay_response_too_large')
        combined = pending + block
        cutoff = max(0, len(combined) - overlap + 1)
        search_from = 0
        while True:
            index = combined.find(secret, search_from)
            if index < 0 or index >= cutoff:
                break
            cutoff = max(cutoff, index + len(secret))
            search_from = index + len(secret)
        safe = combined[:cutoff].replace(secret, b'[redacted]')
        pending = combined[cutoff:]
        if safe:
            yield safe
    if pending:
        yield pending.replace(secret, b'[redacted]')



def trace_gemini_wire(raw, state):
    """Opt-in bounded event metadata only; never log payloads, transcripts or provider IDs."""
    try:
        frame = json.loads(raw)
        if not isinstance(frame, dict):
            return
        now = time.monotonic()
        content = frame.get('serverContent')
        content = content if isinstance(content, dict) else {}
        model = content.get('modelTurn')
        model = model if isinstance(model, dict) else {}
        parts = model.get('parts', [])
        for part in parts if isinstance(parts, list) else []:
            if not isinstance(part, dict) or not isinstance(part.get('inlineData'), dict):
                continue
            state['audio_chunks'] = state.get('audio_chunks', 0) + 1
            if state.get('last_audio_at') is not None:
                state['max_audio_gap_ms'] = max(state.get('max_audio_gap_ms', 0),
                                                round((now - state['last_audio_at']) * 1000))
            state['last_audio_at'] = now
            if part.get('thought') is True:
                state['thought_audio_chunks'] = state.get('thought_audio_chunks', 0) + 1
        activity = frame.get('voiceActivity')
        activity = activity if isinstance(activity, dict) else {}
        flags = [name for name, active in (
            ('setup', 'setupComplete' in frame),
            ('vad_start', activity.get('type') == 'ACTIVITY_START'),
            ('vad_end', activity.get('type') == 'ACTIVITY_END'),
            ('interrupted', content.get('interrupted') is True),
            ('generation_complete', content.get('generationComplete') is True),
            ('turn_complete', content.get('turnComplete') is True),
            ('tool_call', isinstance(frame.get('toolCall'), dict)),
        ) if active]
        if flags and state.get('events', 0) < 128:
            state['events'] = state.get('events', 0) + 1
            print('HababVoiceWire ' + json.dumps({
                'session': state['session'], 'at_ms': round(time.time() * 1000),
                'events': flags, 'audio_chunks': state.get('audio_chunks', 0),
                'max_audio_gap_ms': state.get('max_audio_gap_ms', 0),
                'thought_audio_chunks': state.get('thought_audio_chunks', 0),
            }, separators=(',', ':')), flush=True)
        if content.get('turnComplete') is True:
            for key in ('audio_chunks', 'last_audio_at', 'max_audio_gap_ms', 'thought_audio_chunks'):
                state.pop(key, None)
    except (ValueError, TypeError, KeyError, OSError):
        pass  # Diagnostics never change the wire protocol or fail a conversation.


class Gateway:
    def __init__(self, key_hash, *, upstream_key=None, text_upstream_key=None, control_socket=None,
                 browser_origins=(), demo_openai_session_limit=5, demo_openai_sessions_per_session=2,
                 demo_openai_session_window_seconds=86400, beta_codes=None, voice_service_key=None, credential_store=None, usage_journal=None):
        if len(key_hash) != 64:
            raise ValueError('Expected an enrolled project key hash')
        if not isinstance(browser_origins, (list, tuple, set, frozenset)):
            raise ValueError('Expected a list of exact browser origins')
        if any(not exact_web_origin(origin) for origin in browser_origins):
            raise ValueError('Invalid browser origin')
        if (type(demo_openai_session_limit) is not int or not 0 <= demo_openai_session_limit <= 1000
                or type(demo_openai_sessions_per_session) is not int
                or not 0 <= demo_openai_sessions_per_session <= demo_openai_session_limit
                or type(demo_openai_session_window_seconds) is not int
                or not 1 <= demo_openai_session_window_seconds <= 31_536_000):
            raise ValueError('Invalid demo Realtime session limits')
        self.key_hash = key_hash
        self.beta_codes = beta_codes or BetaCodes()
        if (voice_service_key is not None
                and (not isinstance(voice_service_key, str) or not 1 <= len(voice_service_key) <= 2048
                     or not voice_service_key.isascii()
                     or any(not 0x21 <= ord(char) <= 0x7e for char in voice_service_key))):
            raise ValueError('Invalid voice relay service credential')
        self.voice_service_key = voice_service_key
        self.provider_health = None
        self.voice_diagnostics = False
        self.usage_journal = usage_journal
        self.credential_store = credential_store
        self.upstream_key = upstream_key
        self.text_upstream_key = text_upstream_key
        self.control_socket = Path(control_socket) if control_socket else None
        self.browser_origins = frozenset(browser_origins)
        self.gemini_credentials = {}
        self.demo_openai_credentials = {}
        self.openai_upstream_key = None
        self.demo_openai_session_limit = demo_openai_session_limit
        self.demo_openai_sessions_per_session = demo_openai_sessions_per_session
        self.demo_openai_session_window_seconds = demo_openai_session_window_seconds
        self.demo_openai_session_starts = deque()
        self.demo_openai_sessions_started = 0
        self.demo_openai_sessions_by_hash = {}
        self.active_voice = 0
        self.inflight = 0
        self.client = None

    def _prune_demo_openai_session_starts(self):
        """Expire global and per-session spend charges using monotonic time."""
        now = self._demo_openai_clock()
        expires_before = now - self.demo_openai_session_window_seconds
        while self.demo_openai_session_starts and self.demo_openai_session_starts[0] <= expires_before:
            self.demo_openai_session_starts.popleft()
        for session_hash, starts in tuple(self.demo_openai_sessions_by_hash.items()):
            while starts and starts[0] <= expires_before:
                starts.popleft()
            if not starts:
                del self.demo_openai_sessions_by_hash[session_hash]
        return now

    @staticmethod
    def _demo_openai_clock():
        return time.monotonic()

    def authorization(self, request):
        authorization = request.headers.get('Authorization', '')
        api_key = request.headers.get('x-goog-api-key', '')
        if bool(authorization) == bool(api_key):
            return None, None
        if authorization:
            if not authorization.startswith('Bearer ') or len(authorization) > 2048:
                return None, None
            secret = authorization[7:]
        else:
            if len(api_key) > 2048:
                return None, None
            secret = api_key
        if not secret:
            return None, None
        return secret, hashlib.sha256(secret.encode()).hexdigest()

    def owner(self, digest, *, token=None):
        if digest is None:
            return False
        if hmac.compare_digest(digest, self.key_hash):
            return True
        return (isinstance(token, str) and self.beta_codes.verify(token)
                and hmac.compare_digest(digest, hashlib.sha256(token.encode()).hexdigest()))

    @staticmethod
    def _voice_error(code, status):
        return web.json_response({'error': {'code': code}}, status=status,
                                 headers={'Cache-Control': 'no-store'})

    async def _voice_relay_request(self, method, path, *, body=None):
        """Call only the fixed local voice relay with its private client credential."""
        if self.voice_service_key is None:
            return None
        headers = {'Authorization': 'Bearer ' + self.voice_service_key}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        try:
            async with self.client.request(
                    method, VOICE_RELAY_URL + path, headers=headers, data=body,
                    timeout=ClientTimeout(total=VOICE_RELAY_TIMEOUT, connect=3, sock_connect=3,
                                          sock_read=VOICE_RELAY_TIMEOUT),
                    allow_redirects=False) as response:
                if response.status != 200:
                    if response.status == 429:
                        return self._voice_error('relay_voice_busy', 429)
                    if response.status == 503:
                        return self._voice_error('habab_upstream_not_ready', 503)
                    return self._voice_error('relay_voice_unavailable', 502)
                payload = bytearray()
                async for block in response.content.iter_chunked(16384):
                    payload.extend(block)
                    if len(payload) > VOICE_RELAY_RESPONSE_MAX:
                        return self._voice_error('relay_invalid_voice_response', 502)
                result = bytes(payload)
                if self.voice_service_key.encode('ascii') in result:
                    return self._voice_error('relay_invalid_voice_response', 502)
                return result
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            return self._voice_error('relay_voice_timeout', 504)
        except Exception:
            return self._voice_error('relay_voice_unavailable', 502)

    @staticmethod
    def _decode_voice_json(payload):
        def object_without_duplicates(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('duplicate_json_key')
                result[key] = value
            return result

        try:
            value = json.loads(payload, object_pairs_hook=object_without_duplicates)
        except (ValueError, UnicodeError, RecursionError, TypeError):
            return None
        return value if isinstance(value, dict) else None

    def _valid_voice_capabilities(self, payload):
        realtime = payload.get('realtime')
        auth = realtime.get('browser_auth') if isinstance(realtime, dict) else None
        models = realtime.get('models') if isinstance(realtime, dict) else None
        default_model = realtime.get('default_model') if isinstance(realtime, dict) else None
        return (payload.get('provider') == 'habab'
                and isinstance(payload.get('chat'), dict)
                and isinstance(payload.get('telegram'), dict)
                and ('speech' not in payload or isinstance(payload.get('speech'), dict))
                and isinstance(realtime, dict)
                and realtime.get('provider') == 'gemini'
                and realtime.get('transport') == 'habab'
                and realtime.get('websocket') == '/gemini/ws'
                and isinstance(models, list) and 1 <= len(models) <= 8
                and all(isinstance(model, str) and 1 <= len(model) <= 128 for model in models)
                and default_model in models
                and isinstance(auth, dict) and type(auth.get('enabled')) is bool
                and auth.get('mode') == 'first_text_frame'
                and auth.get('type') == BROWSER_VOICE_AUTH_TYPE)

    async def proxied_voice_capabilities(self):
        result = await self._voice_relay_request('GET', '/v1/capabilities')
        if isinstance(result, web.Response):
            return result
        payload = self._decode_voice_json(result)
        if payload is None or not self._valid_voice_capabilities(payload):
            return self._voice_error('relay_invalid_voice_response', 502)
        return web.Response(body=result, content_type='application/json',
                            headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})

    async def lifecycle(self, app):
        self.client = ClientSession(timeout=ClientTimeout(total=60), trust_env=False)
        control = None
        health_task = None
        try:
            if self.control_socket:
                if self.control_socket.exists():
                    info = self.control_socket.stat()
                    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                        raise RuntimeError('Unsafe enrollment socket path')
                    self.control_socket.unlink()
                control = await asyncio.start_unix_server(self.enroll, path=str(self.control_socket), limit=4096)
                self.control_socket.chmod(0o600)
            if self.provider_health is not None:
                key = (lambda: self.text_upstream_key) if self.voice_service_key else (lambda: self.upstream_key)
                health_task = asyncio.create_task(self.provider_health.run(self.client, key))
            yield
        finally:
            if health_task is not None:
                health_task.cancel()
                await asyncio.gather(health_task, return_exceptions=True)
            if control:
                control.close()
                await control.wait_closed()
                self.control_socket.unlink(missing_ok=True)
            self.upstream_key = None
            self.text_upstream_key = None
            self.gemini_credentials.clear()
            self.demo_openai_credentials.clear()
            self.openai_upstream_key = None
            await self.client.close()

    async def enroll(self, reader, writer):
        """Accept a local operator credential; production persists it privately."""
        try:
            peer = writer.get_extra_info('socket')
            _, uid, _ = struct.unpack('3i', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != os.getuid():
                raise ValueError('Wrong enrollment owner')
            payload = json.loads(await asyncio.wait_for(reader.readline(), 3))
            key = payload.get('upstream_key')
            purpose = payload.get('purpose', 'voice')
            if purpose == 'demo_openai_bootstrap':
                session_hash = payload.get('session_hash')
                if (set(payload) != {'purpose', 'upstream_key', 'session_hash'}
                        or not isinstance(session_hash, str)
                        or not re.fullmatch(r'[0-9a-f]{64}', session_hash)):
                    raise ValueError('Invalid demo bootstrap')
                if not isinstance(key, str) or not 8 <= len(key) <= 2048 or any(ord(c) < 33 or ord(c) > 126 for c in key):
                    raise ValueError('Invalid enrollment')
                now = time.time()
                self.demo_openai_credentials = {
                    token_hash: grant for token_hash, grant in self.demo_openai_credentials.items()
                    if grant['expires'] > now
                }
                self._prune_demo_openai_session_starts()
                session_count = len(self.demo_openai_sessions_by_hash.get(session_hash, ()))
                if self.active_voice >= 2 or len(self.demo_openai_credentials) >= 8:
                    writer.write(b'{"ready":false}\n')
                    return
                if (len(self.demo_openai_session_starts) >= self.demo_openai_session_limit
                        or session_count >= self.demo_openai_sessions_per_session):
                    writer.write(b'{"ready":false}\n')
                    return
                value = secrets.token_urlsafe(32)
                self.openai_upstream_key = key
                self.demo_openai_credentials[hashlib.sha256(value.encode()).hexdigest()] = {
                    'expires': now + 60, 'session_hash': session_hash,
                }
                writer.write((json.dumps({'ready': True, 'value': value, 'expires_at': int(now + 60),
                                          'websocket_url': OPENAI_DEMO_WEBSOCKET}) + '\n').encode())
                return
            if set(payload) - {'upstream_key', 'purpose'} or purpose not in {'voice', 'text'}:
                raise ValueError('Invalid enrollment purpose')
            if not isinstance(key, str) or not 8 <= len(key) <= 2048 or any(ord(c) < 33 or ord(c) > 126 for c in key):
                raise ValueError('Invalid enrollment')
            if self.credential_store is not None:
                self.credential_store(purpose, key)
            if purpose == 'text':
                self.text_upstream_key = key
            else:
                self.upstream_key = key
            writer.write(b'{"enrolled":true}\n')
        except Exception:
            writer.write(b'{"enrolled":false}\n')
        finally:
            try: await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

    async def openai_demo_voice(self, request):
        if request.query:
            return web.json_response({'error': {'code': 'relay_invalid_query'}}, status=400)
        if request.headers.get('Authorization'):
            return web.json_response({'error': {'code': 'relay_route_not_allowed'}}, status=404)
        if request.headers.get('Origin') not in self.browser_origins:
            return web.json_response({'error': {'code': 'relay_origin_not_allowed'}}, status=403)
        if self.openai_upstream_key is None:
            return web.json_response({'error': {'code': 'openai_demo_not_ready'}}, status=503)
        if self.active_voice >= 2:
            return web.json_response({'error': {'code': 'relay_voice_busy'}}, status=429)
        self.active_voice += 1
        downstream = web.WebSocketResponse(max_msg_size=256 * 1024, heartbeat=20)
        try:
            await downstream.prepare(request)
            try:
                first = await downstream.receive(timeout=VOICE_FIRST_FRAME_TIMEOUT)
            except asyncio.TimeoutError:
                await downstream.close(code=1008, message=b'auth_timeout')
                return downstream
            if first.type != WSMsgType.TEXT or len(first.data.encode()) > 2048:
                await downstream.close(code=1008, message=b'invalid_auth_frame')
                return downstream
            try:
                frame = json.loads(first.data)
                if (not isinstance(frame, dict) or set(frame) != {'type', 'token'}
                        or frame.get('type') != DEMO_OPENAI_AUTH_TYPE
                        or not isinstance(frame.get('token'), str) or not 1 <= len(frame['token']) <= 2048):
                    raise ValueError
            except (ValueError, TypeError):
                await downstream.close(code=1008, message=b'invalid_auth_frame')
                return downstream
            digest = hashlib.sha256(frame['token'].encode()).hexdigest()
            item = self.demo_openai_credentials.pop(digest, None)
            if item is None or item['expires'] <= time.time() or self.openai_upstream_key is None:
                await downstream.close(code=1008, message=b'relay_not_authorized')
                return downstream
            quota_now = self._prune_demo_openai_session_starts()
            session_hash = item['session_hash']
            session_count = len(self.demo_openai_sessions_by_hash.get(session_hash, ()))
            if (len(self.demo_openai_session_starts) >= self.demo_openai_session_limit
                    or session_count >= self.demo_openai_sessions_per_session):
                await downstream.close(code=1008, message=b'session_limit_reached')
                return downstream
            # Charge the process and anonymous session before opening the paid upstream socket.
            self.demo_openai_session_starts.append(quota_now)
            self.demo_openai_sessions_started += 1
            self.demo_openai_sessions_by_hash.setdefault(session_hash, deque()).append(quota_now)
            upstream_key = self.openai_upstream_key
            async with self.client.ws_connect(OPENAI_REALTIME_UPSTREAM,
                    headers={'Authorization': 'Bearer ' + upstream_key,
                             'OpenAI-Safety-Identifier': item['session_hash']},
                    max_msg_size=256 * 1024,
                    timeout=ClientWSTimeout(ws_receive=60, ws_close=5), heartbeat=20) as upstream:
                upstream_key = None
                session = {
                    'type': 'realtime',
                    'model': 'gpt-realtime-2.1',
                    'output_modalities': ['audio'],
                    'instructions': ('Ты голосовой помощник демо-квартиры Habab. Отвечай по-русски. '
                        'Для управления используй только объявленные функции. Объясняй, что меняешь, '
                        'и не утверждай, что состояние изменено, до результата функции.'),
                    'audio': {
                        'input': {'format': {'type': 'audio/pcm', 'rate': 24000},
                                  'transcription': {'model': 'gpt-4o-mini-transcribe'},
                                  'turn_detection': {'type': 'semantic_vad'}},
                        'output': {'format': {'type': 'audio/pcm', 'rate': 24000}, 'voice': 'marin'},
                    },
                    'tools': [
                        {'type': 'function', 'name': 'set_demo_light',
                         'description': 'Включить или выключить разрешённый свет в демо-квартире.',
                         'parameters': {'type': 'object', 'properties': {
                             'entity_id': {'type': 'string', 'enum': sorted({'light.living_room_floor_lamp',
                                 'light.living_room_spotlights', 'light.kitchen_spotlights', 'light.kitchen_worktop',
                                 'light.entrance_ceiling', 'light.bedroom_ceiling', 'light.bedside_lamp',
                                 'light.office_desk_lamp', 'light.garden_path'})},
                             'state': {'type': 'string', 'enum': ['on', 'off']}},
                             'required': ['entity_id', 'state'], 'additionalProperties': False}},
                        {'type': 'function', 'name': 'set_demo_climate',
                         'description': 'Настроить климат демо-гостиной.',
                         'parameters': {'type': 'object', 'properties': {
                             'state': {'type': 'string', 'enum': ['heat', 'off', 'auto']},
                             'temperature': {'type': 'number', 'minimum': 7, 'maximum': 35}},
                             'required': ['state'], 'additionalProperties': False}},
                        {'type': 'function', 'name': 'activate_demo_scene',
                         'description': 'Включить одну из двух сцен демо-квартиры.',
                         'parameters': {'type': 'object', 'properties': {
                             'entity_id': {'type': 'string', 'enum': ['scene.evening', 'scene.focus']}},
                             'required': ['entity_id'], 'additionalProperties': False}},
                    ],
                    'tool_choice': 'auto',
                }
                await upstream.send_json({'type': 'session.update', 'session': session})

                async def browser_to_openai():
                    total = 0
                    async for message in downstream:
                        if message.type == WSMsgType.TEXT:
                            total += len(message.data.encode())
                            if total > 16 * 1024 * 1024:
                                break
                            try:
                                event = json.loads(message.data)
                            except (ValueError, TypeError):
                                await downstream.close(code=1008, message=b'invalid_event')
                                break
                            if isinstance(event, dict) and event.get('type') == 'session.update':
                                await downstream.close(code=1008, message=b'session_config_is_server_owned')
                                break
                            await upstream.send_str(message.data)
                        elif message.type == WSMsgType.BINARY:
                            total += len(message.data)
                            if total > 16 * 1024 * 1024:
                                break
                            await upstream.send_bytes(message.data)
                        elif message.type in (WSMsgType.ERROR, WSMsgType.CLOSE, WSMsgType.CLOSED):
                            break

                async def openai_to_browser():
                    total = 0
                    async for message in upstream:
                        if message.type == WSMsgType.TEXT:
                            total += len(message.data.encode())
                            if total > 16 * 1024 * 1024:
                                break
                            await downstream.send_str(message.data)
                        elif message.type == WSMsgType.BINARY:
                            total += len(message.data)
                            if total > 16 * 1024 * 1024:
                                break
                            await downstream.send_bytes(message.data)
                        elif message.type in (WSMsgType.ERROR, WSMsgType.CLOSE, WSMsgType.CLOSED):
                            break

                tasks = [asyncio.create_task(browser_to_openai()), asyncio.create_task(openai_to_browser())]
                try:
                    await asyncio.wait(tasks, timeout=300, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            status = (error.status if isinstance(error, ClientResponseError)
                      and type(error.status) is int and 100 <= error.status <= 599 else None)
            logger.warning('OpenAI demo relay upstream failure (exception=%s, status=%s)',
                           type(error).__name__, status)
            if not downstream.prepared:
                return web.json_response({'error': {'code': 'openai_demo_unavailable'}}, status=502)
            try:
                await downstream.close(code=1011, message=b'openai_demo_unavailable')
            except Exception:
                pass
        finally:
            self.active_voice -= 1
            if downstream.prepared:
                await downstream.close()
        return downstream

    async def health(self, request):
        return web.json_response({'service': 'habab-voice-relay', 'ok': True, 'upstreamReady': self.upstream_key is not None})

    async def readiness(self, request):
        own = (self.provider_health.snapshot() if self.provider_health is not None else
               {'ready': False, 'reason': 'not_checked'})
        checks = {'text' if self.voice_service_key else 'voice': own}
        if self.voice_service_key:
            try:
                async with self.client.get('http://127.0.0.1:8326/health/ready',
                        timeout=ClientTimeout(total=3)) as response:
                    other = await response.json()
                    checks['voice'] = other.get('checks', {}).get('voice', {'ready': False, 'reason': 'invalid_health_response'})
            except Exception:
                checks['voice'] = {'ready': False, 'reason': 'voice_relay_unavailable'}
        ready = all(value.get('ready') is True for value in checks.values())
        return web.json_response({'service': 'habab-ai', 'ready': ready, 'checks': checks},
                                 status=200 if ready else 503, headers={'Cache-Control': 'no-store'})

    async def capabilities(self, request):
        secret, digest = self.authorization(request)
        if not self.owner(digest, token=secret):
            return web.json_response({'error': {'code': 'relay_not_authorized'}}, status=401)
        if request.query:
            return web.json_response({'error': {'code': 'relay_invalid_query'}}, status=400)
        if self.voice_service_key is not None:
            return await self.proxied_voice_capabilities()
        return web.json_response({
            'provider': 'habab',
            'chat': {'provider': 'gemini', 'protocol': 'google-generative-language-v1beta',
                     'api_base': 'https://habab-voice.wpmix.net/gemini/v1beta',
                     'models': list(GEMINI_TEXT_MODELS), 'default_model': GEMINI_TEXT_MODEL,
                     'streaming': True},
            'realtime': {'provider': 'gemini', 'transport': 'habab', 'models': [GEMINI_MODEL],
                         'default_model': GEMINI_MODEL, 'protocol': 'google-generative-language-bidi-v1beta',
                         'bootstrap': '/v1/realtime/bootstrap', 'websocket': '/gemini/ws',
                         'browser_auth': {'enabled': bool(self.browser_origins),
                                          'mode': 'first_text_frame', 'type': BROWSER_VOICE_AUTH_TYPE}},
            'telegram': {'protocol': 'telegram-bot-api-relay-v1', 'base': '/telegram',
                         'methods': list(TELEGRAM_METHODS), 'voice': telegram_voice_capability()},
            'speech': speech_capability(self),
        }, headers={'Cache-Control': 'no-store'})

    async def text_capabilities(self, request):
        secret, digest = self.authorization(request)
        if not self.owner(digest, token=secret):
            return web.json_response({'error': {'code': 'relay_not_authorized'}}, status=401)
        if request.method != 'GET':
            return web.json_response({'error': {'code': 'relay_http_method_not_allowed'}}, status=405)
        if request.query:
            return web.json_response({'error': {'code': 'relay_invalid_query'}}, status=400)
        ready = self.text_upstream_key is not None
        payload = {
            'protocol': 'openai_compatible', 'model': HABAB_TEXT_MODEL, 'api_path': '/v1',
            'managed': True, 'configured': ready, 'ready': ready,
        }
        if ready:
            payload.update({
                'configured_upstream_provider': DEEPSEEK_TEXT_PROVIDER,
                'configured_upstream_model': DEEPSEEK_TEXT_MODEL,
            })
        return web.json_response(payload, headers={'Cache-Control': 'no-store'})

    async def text_models(self, request):
        secret, digest = self.authorization(request)
        if not self.owner(digest, token=secret):
            return web.json_response({'error': {'code': 'relay_not_authorized'}}, status=401)
        if request.method != 'GET':
            return web.json_response({'error': {'code': 'relay_http_method_not_allowed'}}, status=405)
        if request.query or request.content_length not in (None, 0) or request.headers.get('Transfer-Encoding'):
            return web.json_response({'error': {'code': 'relay_invalid_query'}}, status=400)
        return web.json_response({
            'object': 'list',
            'data': [{'id': HABAB_TEXT_MODEL, 'object': 'model', 'owned_by': 'habab'}],
        }, headers={'Cache-Control': 'no-store'})

    async def text_chat_completions(self, request):
        service_key, digest = self.authorization(request)
        if not self.owner(digest, token=service_key):
            return web.json_response({'error': {'code': 'relay_not_authorized'}}, status=401)
        if request.method != 'POST':
            return web.json_response({'error': {'code': 'relay_http_method_not_allowed'}}, status=405)
        if request.query:
            return web.json_response({'error': {'code': 'relay_invalid_query'}}, status=400)
        if request.content_type != 'application/json':
            return web.json_response({'error': {'code': 'relay_invalid_request'}}, status=400)
        try:
            body = await request.read()
            payload = json.loads(body)
            if (not body or len(body) > 2 * 1024 * 1024 or not isinstance(payload, dict)
                    or service_key.encode() in body):
                raise ValueError
            if payload.get('model') != HABAB_TEXT_MODEL:
                return web.json_response({'error': {'code': 'relay_unknown_model'}}, status=404)
            if not isinstance(payload.get('messages'), list) or not payload['messages']:
                raise ValueError
            streaming = payload.get('stream', False)
            if not isinstance(streaming, bool):
                raise ValueError
        except (ValueError, UnicodeError, TypeError):
            return web.json_response({'error': {'code': 'relay_invalid_request'}}, status=400)
        if self.text_upstream_key is None:
            return web.json_response({'error': {'code': 'habab_text_upstream_not_ready'}}, status=503)
        upstream_key = self.text_upstream_key
        if upstream_key.encode() in body:
            return web.json_response({'error': {'code': 'relay_invalid_request'}}, status=400)
        if self.inflight >= 6:
            return web.json_response({'error': {'code': 'relay_busy'}}, status=429)
        # This alias intentionally uses DeepSeek's non-thinking mode for fast
        # replies and tool continuations; the direct provider path is untouched.
        payload['thinking'] = {'type': 'disabled'}
        if 'reasoning_effort' in payload:
            payload['reasoning_effort'] = 'none'
        payload['model'] = DEEPSEEK_TEXT_MODEL
        upstream_body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        self.inflight += 1
        result = None
        usage = StreamUsage()
        outcome = "failed"
        started = time.monotonic()
        try:
            async with self.client.post(DEEPSEEK_CHAT_COMPLETIONS,
                    headers={'Authorization': 'Bearer ' + upstream_key, 'Content-Type': 'application/json'},
                    data=upstream_body, allow_redirects=False,
                    timeout=ClientTimeout(total=120, sock_read=60)) as response:
                if self.provider_health is not None:
                    self.provider_health.observe_http(response.status)
                headers = {'Content-Type': response.headers.get('Content-Type', 'application/json'),
                           'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}
                if streaming and response.status == 200:
                    result = web.StreamResponse(status=response.status, headers=headers)
                    await result.prepare(request)
                    try:
                        async for block in redacted_stream(response.content, upstream_key.encode(), 16 * 1024 * 1024):
                            usage.feed(block)
                            await result.write(block)
                    except ValueError:
                        outcome = 'response_too_large'
                        await result.write(b'data: {"error":{"code":"relay_response_too_large"}}\n\n')
                    else:
                        outcome = 'ok'
                    await result.write_eof()
                    return result
                response_body = bytearray()
                async for block in response.content.iter_chunked(65536):
                    response_body.extend(block)
                    if len(response_body) > 8 * 1024 * 1024:
                        return web.json_response({'error': {'code': 'relay_response_too_large'}}, status=502)
                if response.status == 200:
                    outcome = 'ok'
                    try:
                        decoded = json.loads(response_body)
                        usage.usage = token_counts(decoded.get('usage')) if isinstance(decoded, dict) else None
                    except (ValueError, UnicodeError, RecursionError):
                        outcome = 'invalid_response'
                safe = bytes(response_body).replace(upstream_key.encode(), b'[redacted]')
                return web.Response(body=safe, status=response.status, headers=headers)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            if result is not None and result.prepared:
                try:
                    await result.write(b'data: {"error":{"code":"relay_upstream_timeout"}}\n\n')
                    await result.write_eof()
                except Exception:
                    pass
                return result
            return web.json_response({'error': {'code': 'relay_upstream_timeout'}}, status=504)
        except Exception:
            if result is not None and result.prepared:
                try:
                    await result.write(b'data: {"error":{"code":"relay_upstream_unavailable"}}\n\n')
                    await result.write_eof()
                except Exception:
                    pass
                return result
            return web.json_response({'error': {'code': 'relay_upstream_unavailable'}}, status=502)
        finally:
            self.inflight -= 1
            if self.usage_journal is not None:
                try:
                    self.usage_journal.record(installation=digest, provider=DEEPSEEK_TEXT_PROVIDER,
                        model=DEEPSEEK_TEXT_MODEL, status=outcome,
                        elapsed_ms=int((time.monotonic() - started) * 1000), usage=usage.usage)
                except Exception:
                    logger.error('usage_statistics_write_failed')

    async def telegram(self, request):
        auth_values = request.headers.getall('Authorization', [])
        if len(auth_values) != 1:
            return web.json_response({'error': {'code': 'relay_not_authorized'}}, status=401)
        secret, digest = self.authorization(request)
        if not self.owner(digest, token=secret):
            return web.json_response({'error': {'code': 'relay_not_authorized'}}, status=401)
        if request.query:
            return web.json_response({'error': {'code': 'telegram_invalid_query'}}, status=400)

        method = request.match_info.get('method', '')
        if method not in TELEGRAM_METHODS:
            return web.json_response({'error': {'code': 'telegram_method_not_allowed'}}, status=404)
        allowed_methods = ('GET', 'POST') if method == 'getMe' else ('POST',)
        if request.method not in allowed_methods:
            return web.json_response({'error': {'code': 'telegram_http_method_not_allowed'}}, status=405)

        token_values = request.headers.getall('X-Habab-Telegram-Bot-Token', [])
        if (len(token_values) != 1 or len(token_values[0]) > 256
                or TELEGRAM_BOT_TOKEN.fullmatch(token_values[0]) is None):
            return web.json_response({'error': {'code': 'telegram_invalid_bot_token'}}, status=400)
        bot_token = token_values[0]

        body = b''
        if method == 'getMe' and request.method == 'GET':
            if request.content_length not in (None, 0) or request.headers.get('Transfer-Encoding'):
                return web.json_response({'error': {'code': 'telegram_invalid_request'}}, status=400)
        else:
            if request.content_type != 'application/json':
                return web.json_response({'error': {'code': 'telegram_invalid_request'}}, status=400)
            payload = bytearray()
            async for block in request.content.iter_chunked(65536):
                payload.extend(block)
                if len(payload) > TELEGRAM_REQUEST_MAX:
                    return web.json_response({'error': {'code': 'telegram_request_too_large'}}, status=413)
            body = bytes(payload)
            try:
                decoded = json.loads(body)
                if not body or not isinstance(decoded, dict):
                    raise ValueError
                if method == 'getMe' and decoded:
                    raise ValueError
                if method == 'getUpdates':
                    timeout = decoded.get('timeout', 0)
                    if (isinstance(timeout, bool) or not isinstance(timeout, int)
                            or not 0 <= timeout <= TELEGRAM_LONG_POLL_MAX):
                        raise ValueError
            except (ValueError, UnicodeError, TypeError):
                return web.json_response({'error': {'code': 'telegram_invalid_request'}}, status=400)

        if self.inflight >= 6:
            return web.json_response({'error': {'code': 'relay_busy'}}, status=429)
        self.inflight += 1
        timeout_seconds = (TELEGRAM_LONG_POLL_REQUEST_TIMEOUT if method == 'getUpdates'
                           else TELEGRAM_REQUEST_TIMEOUT)
        upstream_url = f'{TELEGRAM_API_BASE}{bot_token}/{method}'
        try:
            timeout = ClientTimeout(total=timeout_seconds, connect=10, sock_connect=10,
                                    sock_read=TELEGRAM_LONG_POLL_MAX + 5 if method == 'getUpdates' else 15)
            async with self.client.request(request.method, upstream_url,
                    headers={'Content-Type': 'application/json'} if body else {},
                    data=body if body else None, allow_redirects=False, timeout=timeout) as response:
                payload = bytearray()
                async for block in response.content.iter_chunked(65536):
                    payload.extend(block)
                    if len(payload) > TELEGRAM_RESPONSE_MAX:
                        return web.json_response({'error': {'code': 'telegram_response_too_large'}}, status=502)
                safe = bytes(payload).replace(bot_token.encode(), b'[redacted]')
                try:
                    parsed = json.loads(safe)
                    if not isinstance(parsed, dict):
                        raise ValueError
                except (ValueError, UnicodeError, TypeError):
                    return web.json_response({'error': {'code': 'telegram_invalid_upstream_response'}}, status=502)
                headers = {'Content-Type': response.headers.get('Content-Type', 'application/json'),
                           'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}
                retry_after = response.headers.get('Retry-After')
                if retry_after is not None and re.fullmatch(r'\d{1,6}', retry_after):
                    headers['Retry-After'] = retry_after
                return web.Response(body=safe, status=response.status, headers=headers)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            return web.json_response({'error': {'code': 'telegram_upstream_timeout'}}, status=504)
        except Exception:
            return web.json_response({'error': {'code': 'telegram_upstream_unavailable'}}, status=502)
        finally:
            self.inflight -= 1

    async def gemini_bootstrap(self, request):
        service_key, digest = self.authorization(request)
        if not self.owner(digest, token=service_key):
            return web.json_response({'error': {'code': 'relay_not_authorized'}}, status=401)
        if request.query:
            return web.json_response({'error': {'code': 'relay_invalid_query'}}, status=400)
        if self.voice_service_key is not None:
            return await self.proxied_gemini_bootstrap(request, service_key)
        if self.upstream_key is None:
            return web.json_response({'error': {'code': 'habab_upstream_not_ready'}}, status=503)
        if self.active_voice >= 2:
            return web.json_response({'error': {'code': 'relay_voice_busy'}}, status=429)
        try:
            body = await request.json()
            model, setup = body.get('model'), body.get('setup')
            if (set(body) != {'model', 'setup'} or model != GEMINI_MODEL or not isinstance(setup, dict)
                    or setup.get('model') != 'models/' + GEMINI_MODEL):
                raise ValueError
            encoded = json.dumps(setup, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            encoded_bytes = encoded.encode('utf-8')
            if len(encoded_bytes) > 64 * 1024 or service_key.encode() in encoded_bytes:
                raise ValueError
        except (ValueError, AttributeError, TypeError):
            return web.json_response({'error': {'code': 'relay_invalid_model_or_setup'}}, status=400)
        now = time.time()
        token_request = {'uses': 1, 'expireTime': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now + 180)),
                         'newSessionExpireTime': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now + 60)),
                         'bidiGenerateContentSetup': setup}
        self.gemini_credentials = {key: item for key, item in self.gemini_credentials.items() if item['expires'] > now}
        if len(self.gemini_credentials) >= 8:
            return web.json_response({'error': {'code': 'relay_voice_busy'}}, status=429)
        if self.inflight >= 6:
            return web.json_response({'error': {'code': 'relay_busy'}}, status=429)
        self.inflight += 1
        try:
            async with self.client.post(GEMINI_TOKEN_URL, headers={'x-goog-api-key': self.upstream_key},
                                        json=token_request, allow_redirects=False) as response:
                if self.provider_health is not None:
                    self.provider_health.observe_http(response.status)
                if response.status == 429:
                    return self._voice_error('relay_voice_busy', 429)
                if response.status in (401, 403):
                    return self._voice_error('habab_upstream_not_ready', 503)
                if response.status != 200:
                    return web.json_response({'error': {'code': 'relay_upstream_unavailable'}}, status=502)
                token_payload = await response.content.read(TOKEN_RESPONSE_MAX + 1)
                if len(token_payload) > TOKEN_RESPONSE_MAX:
                    return web.json_response({'error': {'code': 'relay_invalid_upstream_response'}}, status=502)
                google_token = json.loads(token_payload)
            if not isinstance(google_token, dict):
                return web.json_response({'error': {'code': 'relay_invalid_upstream_response'}}, status=502)
            google_value = google_token.get('name')
            if not isinstance(google_value, str) or not google_value or len(google_value) > 2048:
                return web.json_response({'error': {'code': 'relay_invalid_upstream_response'}}, status=502)
        except asyncio.CancelledError:
            raise
        except Exception:
            return web.json_response({'error': {'code': 'relay_upstream_unavailable'}}, status=502)
        finally:
            self.inflight -= 1
        value = secrets.token_urlsafe(32)
        self.gemini_credentials[hashlib.sha256(value.encode()).hexdigest()] = {
            'expires': now + 60, 'model': model, 'setup': encoded, 'google_token': google_value,
        }
        return web.json_response({'provider': 'gemini', 'transport': 'habab', 'model': model,
                                  'value': value, 'expires_at': int(now + 60),
                                  'websocket_url': GEMINI_WEBSOCKET, 'setup': setup},
                                 headers={'Cache-Control': 'no-store'})

    async def proxied_gemini_bootstrap(self, request, service_key):
        started = time.monotonic()
        status = 'failed'
        try:
            result = await self._proxied_gemini_bootstrap(request, service_key)
            status = 'ok' if result.status == 200 else 'failed'
            return result
        finally:
            if self.usage_journal is not None:
                try:
                    self.usage_journal.record(installation=hashlib.sha256(service_key.encode()).hexdigest(),
                        provider='gemini', model=GEMINI_MODEL, channel='voice_activation', status=status,
                        elapsed_ms=int((time.monotonic() - started) * 1000))
                except Exception:
                    logger.error('usage_statistics_write_failed')

    async def _proxied_gemini_bootstrap(self, request, service_key):
        if request.content_type != 'application/json':
            return self._voice_error('relay_invalid_model_or_setup', 400)
        if request.content_length is not None and request.content_length > VOICE_BOOTSTRAP_WIRE_MAX:
            return self._voice_error('relay_request_too_large', 413)
        try:
            raw = bytearray()
            async for block in request.content.iter_chunked(16384):
                raw.extend(block)
                if len(raw) > VOICE_BOOTSTRAP_WIRE_MAX:
                    return self._voice_error('relay_request_too_large', 413)
            body = self._decode_voice_json(raw)
            if body is None or set(body) != {'model', 'setup'}:
                raise ValueError
            model, setup = body.get('model'), body.get('setup')
            if (model != GEMINI_MODEL or not isinstance(setup, dict)
                    or setup.get('model') != 'models/' + GEMINI_MODEL):
                raise ValueError
            encoded = json.dumps(setup, ensure_ascii=False, sort_keys=True,
                                 separators=(',', ':')).encode('utf-8')
            if (len(encoded) > VOICE_SETUP_MAX_BYTES
                    or service_key.encode('utf-8') in encoded
                    or self.voice_service_key.encode('ascii') in encoded):
                raise ValueError
            forwarded_body = json.dumps({'model': model, 'setup': setup}, ensure_ascii=False,
                                        separators=(',', ':')).encode('utf-8')
        except (ValueError, UnicodeError, TypeError, RecursionError):
            return self._voice_error('relay_invalid_model_or_setup', 400)

        result = await self._voice_relay_request(
            'POST', '/v1/realtime/bootstrap', body=forwarded_body)
        if isinstance(result, web.Response):
            return result
        payload = self._decode_voice_json(result)
        if (payload is None or payload.get('provider') != 'gemini'
                or payload.get('transport') != 'habab' or payload.get('model') != model
                or payload.get('setup') != setup
                or payload.get('websocket_url') != GEMINI_WEBSOCKET
                or not isinstance(payload.get('value'), str)
                or not 1 <= len(payload['value']) <= 2048
                or (type(payload.get('expires_at')) is not int)
                or self.voice_service_key.encode('ascii') in result
                or service_key.encode('utf-8') in result):
            return self._voice_error('relay_invalid_voice_response', 502)
        return web.Response(body=result, content_type='application/json',
                            headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})

    async def gemini_text(self, request):
        service_key, digest = self.authorization(request)
        if not self.owner(digest, token=service_key):
            return web.json_response({'error': {'code': 'relay_not_authorized'}}, status=401)
        if self.upstream_key is None:
            return web.json_response({'error': {'code': 'habab_upstream_not_ready'}}, status=503)
        upstream_key = self.upstream_key
        match = re.fullmatch(
            r'/gemini/v1beta/models/(gemini-3\.1-flash-lite|gemini-3\.8-flash):(generateContent|streamGenerateContent)',
            request.path,
        )
        if request.method != 'POST' or match is None:
            return web.json_response({'error': {'code': 'relay_route_not_allowed'}}, status=404)
        model, action = match.groups()
        streaming = action == 'streamGenerateContent'
        if ((streaming and (len(request.query) != 1 or request.query.get('alt') != 'sse'))
                or (not streaming and len(request.query) != 0)):
            return web.json_response({'error': {'code': 'relay_invalid_query'}}, status=400)
        if request.content_type != 'application/json':
            return web.json_response({'error': {'code': 'relay_invalid_request'}}, status=400)
        try:
            body = await request.read()
            if (not body or len(body) > 2 * 1024 * 1024 or service_key.encode() in body
                    or not isinstance(json.loads(body), dict)):
                raise ValueError
        except (ValueError, UnicodeError, TypeError):
            return web.json_response({'error': {'code': 'relay_invalid_request'}}, status=400)
        if self.inflight >= 6:
            return web.json_response({'error': {'code': 'relay_busy'}}, status=429)
        self.inflight += 1
        upstream_url = GEMINI_TEXT_BASE + '/' + model + ':' + action
        query = {'alt': 'sse'} if streaming else None
        result = None
        try:
            async with self.client.post(upstream_url, params=query,
                    headers={'x-goog-api-key': upstream_key, 'Content-Type': 'application/json'},
                    data=body, allow_redirects=False, timeout=ClientTimeout(total=120, sock_read=60)) as response:
                headers = {'Content-Type': response.headers.get('Content-Type', 'application/json'),
                           'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}
                if streaming and response.status == 200:
                    result = web.StreamResponse(status=response.status, headers=headers)
                    await result.prepare(request)
                    needle = upstream_key.encode()
                    try:
                        async for block in redacted_stream(response.content, needle, 16 * 1024 * 1024):
                            await result.write(block)
                    except ValueError:
                        await result.write(b'event: error\ndata: {"error":{"code":"relay_response_too_large"}}\n\n')
                    await result.write_eof()
                    return result
                payload = bytearray()
                async for block in response.content.iter_chunked(65536):
                    payload.extend(block)
                    if len(payload) > 8 * 1024 * 1024:
                        return web.json_response({'error': {'code': 'relay_response_too_large'}}, status=502)
                safe = bytes(payload).replace(upstream_key.encode(), b'[redacted]')
                return web.Response(body=safe, status=response.status, headers=headers)
        except asyncio.CancelledError:
            raise
        except Exception:
            if result is not None and result.prepared:
                try:
                    await result.write(b'event: error\ndata: {"error":{"code":"relay_upstream_unavailable"}}\n\n')
                    await result.write_eof()
                except Exception:
                    pass
                return result
            return web.json_response({'error': {'code': 'relay_upstream_unavailable'}}, status=502)
        finally:
            self.inflight -= 1

    async def gemini_voice(self, request):
        if request.query:
            return web.json_response({'error': {'code': 'relay_invalid_query'}}, status=400)
        header = request.headers.get('Authorization', '')
        browser_mode = 'Authorization' not in request.headers
        voice_slot = False
        item = None
        if browser_mode:
            origin = request.headers.get('Origin')
            if origin not in self.browser_origins:
                return web.json_response({'error': {'code': 'relay_origin_not_allowed'}}, status=403)
            if self.active_voice >= 2:
                return web.json_response({'error': {'code': 'relay_voice_busy'}}, status=429)
            # Bound unauthenticated upgraded sockets to the same two voice slots.
            self.active_voice += 1
            voice_slot = True
        else:
            secret = header[6:] if header.startswith('Token ') and len(header) <= 2048 else ''
            digest = hashlib.sha256(secret.encode()).hexdigest() if secret else None
            item = self.gemini_credentials.get(digest)
            if item is None or item['expires'] <= time.time() or self.upstream_key is None:
                return web.json_response({'error': {'code': 'relay_not_authorized'}}, status=401)
            if self.active_voice >= 2:
                return web.json_response({'error': {'code': 'relay_voice_busy'}}, status=429)
            # A valid native credential is consumed as soon as an upgrade is accepted.
            self.gemini_credentials.pop(digest, None)
            self.active_voice += 1
            voice_slot = True
        downstream = web.WebSocketResponse(max_msg_size=256 * 1024, heartbeat=20)
        try:
            await downstream.prepare(request)
            try:
                first = await downstream.receive(timeout=VOICE_FIRST_FRAME_TIMEOUT)
            except asyncio.TimeoutError:
                reason = b'auth_timeout' if browser_mode else b'invalid_setup'
                await downstream.close(code=1008, message=reason)
                return downstream
            if first.type != WSMsgType.TEXT or len(first.data.encode()) > VOICE_SETUP_MAX_BYTES:
                await downstream.close(code=1008, message=b'invalid_auth_frame' if browser_mode else b'invalid_setup')
                return downstream
            try:
                frame = json.loads(first.data)
                if browser_mode:
                    valid_envelope = (isinstance(frame, dict) and set(frame) == {'type', 'credential', 'setup'}
                                      and frame.get('type') == BROWSER_VOICE_AUTH_TYPE
                                      and isinstance(frame.get('credential'), str)
                                      and 1 <= len(frame['credential']) <= 2048)
                    if not valid_envelope:
                        await downstream.close(code=1008, message=b'invalid_auth_frame')
                        return downstream
                    secret = frame['credential']
                    digest = hashlib.sha256(secret.encode()).hexdigest()
                    item = self.gemini_credentials.get(digest)
                    if item is None or item['expires'] <= time.time() or self.upstream_key is None:
                        await downstream.close(code=1008, message=b'relay_not_authorized')
                        return downstream
                    # Consume before setup comparison: even a mismatched setup cannot replay the grant.
                    self.gemini_credentials.pop(digest, None)
                    setup = frame['setup']
                else:
                    setup = frame.get('setup') if isinstance(frame, dict) and set(frame) == {'setup'} else None
                canonical = json.dumps(setup, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
            except (ValueError, TypeError, AttributeError):
                if browser_mode:
                    await downstream.close(code=1008, message=b'invalid_auth_frame')
                    return downstream
                setup, canonical = None, None
            if not isinstance(setup, dict) or canonical != item['setup']:
                await downstream.close(code=1008, message=b'invalid_setup')
                return downstream
            google_token = item.pop('google_token')
            async with self.client.ws_connect(GEMINI_CONSTRAINED_UPSTREAM,
                    headers={'Authorization': 'Token ' + google_token}, max_msg_size=256 * 1024,
                    timeout=ClientWSTimeout(ws_receive=60, ws_close=5), heartbeat=20) as upstream:
                google_token = None
                setup_frame = (json.dumps({'setup': setup}, ensure_ascii=False, separators=(',', ':'))
                               if browser_mode else first.data)
                await upstream.send_str(setup_frame)

                wire_trace = {'session': secrets.token_hex(6)}
                async def relay(source, target, *, from_upstream=False):
                    total = 0
                    async for message in source:
                        if message.type == WSMsgType.TEXT:
                            if from_upstream and self.voice_diagnostics:
                                trace_gemini_wire(message.data, wire_trace)
                            total += len(message.data.encode())
                            if total > 16 * 1024 * 1024:
                                break
                            await target.send_str(message.data)
                        elif message.type == WSMsgType.BINARY:
                            total += len(message.data)
                            if total > 16 * 1024 * 1024:
                                break
                            await target.send_bytes(message.data)
                        elif message.type == WSMsgType.ERROR:
                            if from_upstream:
                                raise RuntimeError('voice_transport_failed')
                            break
                        elif message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED):
                            if from_upstream and source.close_code not in (1000, 1001):
                                raise RuntimeError('voice_transport_failed')
                            break
                    if from_upstream and source.close_code not in (1000, 1001):
                        raise RuntimeError('voice_transport_failed')

                tasks = [asyncio.create_task(relay(downstream, upstream)),
                         asyncio.create_task(relay(upstream, downstream, from_upstream=True))]
                try:
                    done, _ = await asyncio.wait(tasks, timeout=180, return_when=asyncio.FIRST_COMPLETED)
                    if any(task.exception() is not None for task in done if not task.cancelled()):
                        raise RuntimeError('voice_transport_failed')
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            if not downstream.prepared:
                return web.json_response({'error': {'code': 'relay_voice_unavailable'}}, status=502)
            try:
                await downstream.close(code=1011, message=b'relay_voice_unavailable')
            except Exception:
                pass
        finally:
            if voice_slot:
                self.active_voice -= 1
            if downstream.prepared:
                await downstream.close()
        return downstream

    async def legacy_http(self, request):
        secret, digest = self.authorization(request)
        if not self.owner(digest, token=secret):
            return web.json_response({'error': {'code': 'relay_not_authorized'}}, status=401)
        route = request.match_info['route']
        if route in LEGACY_OPENAI_ROUTES:
            return web.json_response({'error': {'code': 'habab_api_upgrade_required'}}, status=426,
                                     headers={'Cache-Control': 'no-store'})
        return web.json_response({'error': {'code': 'relay_route_not_allowed'}}, status=404)

    def app(self):
        app = web.Application(client_max_size=6 * 1024 * 1024)
        app.cleanup_ctx.append(self.lifecycle)
        self.beta_codes.register_routes(app)
        register_speech_routes(app, self)
        app.router.add_get('/health', self.health)
        app.router.add_get('/health/ready', self.readiness)
        app.router.add_get('/v1/capabilities', self.capabilities)
        app.router.add_route('*', '/text/capabilities', self.text_capabilities)
        app.router.add_route('*', '/v1/models', self.text_models)
        app.router.add_route('*', '/v1/chat/completions', self.text_chat_completions)
        app.router.add_route('*', '/telegram/{method}', self.telegram)
        app.router.add_post('/v1/realtime/bootstrap', self.gemini_bootstrap)
        app.router.add_get('/gemini/ws', self.gemini_voice)
        app.router.add_get('/openai/demo/ws', self.openai_demo_voice)
        app.router.add_route('*', '/gemini/{tail:.*}', self.gemini_text)
        app.router.add_route('*', '/v1/{route:.*}', self.legacy_http)
        return app


def load_private_config(config_path):
    path = Path(config_path)
    try:
        parent_info = path.parent.lstat()
        file_info = path.lstat()
    except OSError as error:
        raise ValueError('private_config_unavailable') from error
    if (not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode)
            or parent_info.st_uid != os.getuid() or stat.S_IMODE(parent_info.st_mode) != 0o700):
        raise ValueError('private_config_directory_required')
    if (not stat.S_ISREG(file_info.st_mode) or stat.S_ISLNK(file_info.st_mode)
            or file_info.st_uid != os.getuid() or stat.S_IMODE(file_info.st_mode) != 0o600
            or file_info.st_size > 64 * 1024):
        raise ValueError('private_config_file_required')

    def object_without_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate_json_key')
            result[key] = value
        return result

    try:
        config = json.loads(path.read_text(encoding='utf-8'),
                            object_pairs_hook=object_without_duplicates)
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError) as error:
        raise ValueError('private_config_invalid') from error
    key_hash = config.get('key_sha256') if isinstance(config, dict) else None
    if not isinstance(key_hash, str) or re.fullmatch(r'[0-9a-f]{64}', key_hash) is None:
        raise ValueError('private_config_invalid')
    if 'voice_service_key' in config:
        key = config['voice_service_key']
        if (not isinstance(key, str) or not 1 <= len(key) <= 2048 or not key.isascii()
                or any(not 0x21 <= ord(char) <= 0x7e for char in key)):
            raise ValueError('private_config_invalid')
    keys = config.get('upstream_keys', {})
    if (not isinstance(keys, dict) or set(keys) - {'voice', 'text'}
            or any(not isinstance(key, str) or not 8 <= len(key) <= 2048
                   or any(not 33 <= ord(c) <= 126 for c in key) for key in keys.values())):
        raise ValueError('private_config_invalid')
    return config


def persist_upstream(config_path, purpose, key):
    """Keep operator credentials in the existing private config across restarts."""
    config = load_private_config(config_path)
    config.setdefault('upstream_keys', {})[purpose] = key
    descriptor, filename = tempfile.mkstemp(prefix='.credentials-', dir=Path(config_path).parent)
    temporary = Path(filename)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as out:
            json.dump(config, out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, config_path)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    try:
        config = load_private_config(args.config)
    except (OSError, ValueError, TypeError):
        parser.error('private_config_invalid')
    gateway = Gateway(config['key_sha256'], control_socket=args.config.parent / 'control.sock',
                      upstream_key=config.get('upstream_keys', {}).get('voice'),
                      text_upstream_key=config.get('upstream_keys', {}).get('text'),
                      credential_store=lambda purpose, key: persist_upstream(args.config, purpose, key),
                      usage_journal=UsageJournal(args.config.parent / 'usage'),
                      beta_codes=BetaCodes(config.get('beta_config') or os.environ.get('HABAB_BETA_CONFIG')),
                      browser_origins=config.get('browser_origins', []),
                      demo_openai_session_limit=config.get('demo_openai_session_limit', 5),
                      demo_openai_sessions_per_session=config.get('demo_openai_sessions_per_session', 2),
                      demo_openai_session_window_seconds=config.get('demo_openai_session_window_seconds', 86400),
                      voice_service_key=config.get('voice_service_key'))
    gateway.voice_diagnostics = config.get('voice_diagnostics') is True
    provider = 'deepseek' if gateway.voice_service_key else 'gemini'
    model = DEEPSEEK_TEXT_MODEL if provider == 'deepseek' else GEMINI_MODEL
    gateway.provider_health = ProviderHealth(provider, model, args.config.parent / 'health-state.json',
        notify_command=config.get('health_notify_command', []), recipient=config.get('health_notify_recipient'))
    web.run_app(gateway.app(), host='127.0.0.1', port=config.get('port', 8326), access_log=None, print=None, handler_cancellation=True)


if __name__ == '__main__':
    main()
