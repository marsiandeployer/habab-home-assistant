"""Loopback-only Flask/bootstrap/Gateway contract for the isolated demo relay."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[3]
RELAY_VENV = Path('/root/.venvs/habab-voice-relay')
RELAY_SITE_PACKAGES = RELAY_VENV / 'lib' / f'python{sys.version_info.major}.{sys.version_info.minor}' / 'site-packages'
sys.path.insert(0, str(RELAY_SITE_PACKAGES))
sys.path.insert(0, str(ROOT / 'habab' / 'android' / 'relay'))
sys.path.insert(0, str(ROOT / 'habab'))

from aiohttp import ClientSession, WSMsgType, web  # noqa: E402
from flask import Flask  # noqa: E402

import openai_gateway as gateway_module  # noqa: E402
from openai_gateway import Gateway  # noqa: E402
from habab_demo.routes import HADemoSessionInterface, ha_demo_bp  # noqa: E402


async def run_fixture():
    observed = {'upstream_connected': False, 'server_session_received': False,
                'synthetic_authorization': False, 'browser_ping_received': False}

    async def synthetic_upstream(request):
        ws = web.WebSocketResponse(max_msg_size=256 * 1024)
        await ws.prepare(request)
        observed['upstream_connected'] = True
        observed['synthetic_authorization'] = (
            request.headers.get('Authorization') == 'Bearer synthetic-only-dummy-upstream-key'
        )
        session_event = await ws.receive_json(timeout=3)
        session = session_event.get('session', {})
        observed['server_session_received'] = (
            session_event.get('type') == 'session.update'
            and session.get('model') == 'gpt-realtime-2.1'
            and len(session.get('tools', [])) == 3
        )
        await ws.send_json({'type': 'fixture.ready'})
        async for message in ws:
            if message.type == WSMsgType.TEXT:
                event = json.loads(message.data)
                if event.get('type') == 'fixture.ping':
                    observed['browser_ping_received'] = True
                    await ws.send_json({'type': 'fixture.pong'})
        return ws

    upstream_app = web.Application()
    upstream_app.router.add_get('/{tail:.*}', synthetic_upstream)
    upstream_runner = web.AppRunner(upstream_app)
    relay_runner = None
    old_upstream_url = gateway_module.OPENAI_REALTIME_UPSTREAM
    old_browser_url = gateway_module.OPENAI_DEMO_WEBSOCKET
    original_request = ClientSession._request

    async def loopback_only_request(session, method, url, **kwargs):
        host = urlsplit(str(url)).hostname
        if host not in {'127.0.0.1', 'localhost', '::1'}:
            raise AssertionError('fixture blocked a non-loopback outbound request')
        return await original_request(session, method, url, **kwargs)

    ClientSession._request = loopback_only_request
    try:
        await upstream_runner.setup()
        upstream_reservation = socket.socket()
        upstream_reservation.bind(('127.0.0.1', 0))
        upstream_port = upstream_reservation.getsockname()[1]
        upstream_reservation.close()
        await web.TCPSite(upstream_runner, '127.0.0.1', upstream_port).start()

        relay_reservation = socket.socket()
        relay_reservation.bind(('127.0.0.1', 0))
        relay_port = relay_reservation.getsockname()[1]
        relay_reservation.close()
        origin = f'http://127.0.0.1:{relay_port}'

        with tempfile.TemporaryDirectory(prefix='issue1-demo-relay-contract-') as temporary:
            os.chmod(temporary, 0o700)
            control_socket = Path(temporary) / 'control.sock'
            key_hash = hashlib.sha256(b'synthetic-only-project-key').hexdigest()
            gateway = Gateway(
                key_hash,
                control_socket=control_socket,
                browser_origins=[origin],
                demo_openai_session_limit=5,
                demo_openai_sessions_per_session=2,
            )
            relay_app = gateway.app()
            route_paths = {route.resource.canonical for route in relay_app.router.routes()}
            assert '/gemini/ws' in route_paths
            assert '/openai/demo/ws' in route_paths
            relay_runner = web.AppRunner(relay_app)
            await relay_runner.setup()
            await web.TCPSite(relay_runner, '127.0.0.1', relay_port).start()

            socket_metadata = control_socket.stat()
            assert socket_metadata.st_uid == os.getuid()
            assert socket_metadata.st_mode & 0o777 == 0o600

            gateway_module.OPENAI_REALTIME_UPSTREAM = (
                f'ws://127.0.0.1:{upstream_port}/synthetic-only-upstream'
            )
            gateway_module.OPENAI_DEMO_WEBSOCKET = (
                f'ws://127.0.0.1:{relay_port}/openai/demo/ws'
            )
            flask_app = Flask('issue1-dedicated-relay-fixture')
            flask_app.secret_key = 'synthetic-only-flask-session-key'
            flask_app.config.update(TESTING=True,
                                    OPENAI_API_KEY='synthetic-only-dummy-upstream-key')
            flask_app.session_interface = HADemoSessionInterface()
            flask_app.register_blueprint(ha_demo_bp)
            flask_client = flask_app.test_client()

            # Exercise the route's real environment lookup; PM2 must supply this
            # value when the demo backend starts because a running process keeps
            # its old environment.
            with patch.dict(os.environ, {
                'HA_DEMO_REALTIME_CONTROL_SOCKET': str(control_socket),
                'OPENAI_API_KEY': 'synthetic-only-dummy-upstream-key',
            }):
                response = await asyncio.to_thread(
                    flask_client.post,
                    '/api/ha-demo/realtime/bootstrap',
                    base_url=origin,
                    headers={'Origin': origin},
                )
            assert response.status_code == 200
            grant = response.get_json()
            assert grant['websocket_url'] == gateway_module.OPENAI_DEMO_WEBSOCKET
            assert 'synthetic-only-dummy-upstream-key' not in response.get_data(as_text=True)

            async with ClientSession() as browser:
                async with browser.ws_connect(
                    grant['websocket_url'], headers={'Origin': origin}
                ) as ws:
                    await ws.send_json({'type': gateway_module.DEMO_OPENAI_AUTH_TYPE,
                                        'token': grant['value']})
                    ready = await ws.receive_json(timeout=4)
                    assert ready == {'type': 'fixture.ready'}
                    await ws.send_json({'type': 'fixture.ping'})
                    pong = await ws.receive_json(timeout=4)
                    assert pong == {'type': 'fixture.pong'}

            assert all(observed.values()), observed
            result = {
                'result': 'PASS',
                'bootstrap_status': response.status_code,
                'control_socket_uid_matches_process': True,
                'control_socket_mode': '0600',
                'route': '/openai/demo/ws',
                'gemini_route_coexists': True,
                'upstream': 'loopback synthetic peer',
                'model_requests': 0,
                'audio_frames': 0,
                'observed': observed,
            }
        assert not control_socket.exists()
        result['temporary_socket_cleanup'] = True
        print(json.dumps(result, sort_keys=True))
    finally:
        if relay_runner is not None:
            await relay_runner.cleanup()
        await upstream_runner.cleanup()
        ClientSession._request = original_request
        gateway_module.OPENAI_REALTIME_UPSTREAM = old_upstream_url
        gateway_module.OPENAI_DEMO_WEBSOCKET = old_browser_url


asyncio.run(run_fixture())
