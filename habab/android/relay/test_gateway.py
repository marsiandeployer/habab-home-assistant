"""Exercise auth, route boundaries and full response forwarding without live keys."""
import hashlib
import asyncio
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from openai_gateway import Gateway


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        self.echo_secret = False
        async def upstream(request):
            self.calls.append((request.path, request.headers.get('Authorization')))
            if self.echo_secret:
                return web.json_response({'error': {'message': request.headers.get('Authorization')}})
            if request.path.endswith('client_secrets'):
                return web.json_response({'value': 'synthetic-ephemeral', 'expires_at': 9999999999})
            return web.Response(body=b'x' * 150000)
        app = web.Application()
        app.router.add_route('*', '/{path:.*}', upstream)
        self.upstream = TestServer(app)
        await self.upstream.start_server()
        self.gateway = Gateway(hashlib.sha256(b'synthetic-owner').hexdigest(), upstream_key='synthetic-vendor')
        self.patch = patch('openai_gateway.UPSTREAM', str(self.upstream.make_url('')).rstrip('/'))
        self.patch.start()
        self.client = TestClient(TestServer(self.gateway.app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.upstream.close()
        self.patch.stop()

    async def test_unknown_keys_cannot_reach_either_provider(self):
        for path in ['/v1/models', '/v1/realtime?model=gpt-realtime-mini']:
            response = await self.client.get(path, headers={'Authorization': 'Bearer wrong'})
            self.assertEqual(response.status, 401)
        self.assertEqual(self.calls, [])

    async def test_owner_cannot_proxy_arbitrary_route_or_method(self):
        headers = {'Authorization': 'Bearer synthetic-owner'}
        for method, path in [('GET', '/v1/files'), ('POST', '/v1/models'), ('GET', '/v1/files/example')]:
            response = await self.client.request(method, path, headers=headers)
            self.assertNotEqual(response.status, 200)
        self.assertEqual(self.calls, [])

    async def test_large_response_is_forwarded_to_eof(self):
        response = await self.client.get('/v1/models', headers={'Authorization': 'Bearer synthetic-owner'})
        self.assertEqual(response.status, 200)
        self.assertEqual(len(await response.read()), 150000)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(self.calls[-1][1], 'Bearer synthetic-vendor')

    async def test_vendor_key_does_not_authorize_service_requests(self):
        response = await self.client.get('/v1/models', headers={'Authorization': 'Bearer synthetic-vendor'})
        self.assertEqual(response.status, 401)
        self.assertEqual(self.calls, [])

    async def test_even_a_malformed_upstream_echo_cannot_return_the_vendor_key(self):
        self.echo_secret = True
        response = await self.client.get('/v1/models', headers={'Authorization': 'Bearer synthetic-owner'})
        payload = await response.read()
        self.assertNotIn(b'synthetic-vendor', payload)
        self.assertIn(b'[redacted]', payload)

    async def test_missing_ram_enrollment_is_explicit_and_never_forwards_service_key(self):
        self.gateway.upstream_key = None
        response = await self.client.get('/v1/models', headers={'Authorization': 'Bearer synthetic-owner'})
        self.assertEqual(response.status, 503)
        self.assertEqual((await response.json())['error']['code'], 'habab_upstream_not_ready')
        self.assertEqual(self.calls, [])

    async def test_ephemeral_cache_retains_hash_and_rejects_other_model(self):
        response = await self.client.post('/v1/realtime/client_secrets',
            headers={'Authorization': 'Bearer synthetic-owner'}, json={'session': {'model': 'gpt-realtime-mini'}})
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())['value'], 'synthetic-ephemeral')
        self.assertNotIn('synthetic-ephemeral', repr(self.gateway.ephemeral))
        self.assertIn(hashlib.sha256(b'synthetic-ephemeral').hexdigest(), self.gateway.ephemeral)
        response = await self.client.get('/v1/realtime?model=gpt-realtime', headers={'Authorization': 'Bearer synthetic-ephemeral'})
        self.assertEqual(response.status, 401)

    async def test_telegram_token_does_not_authorize_openai(self):
        response = await self.client.get('/v1/models', headers={'Authorization': 'Bearer synthetic-bot'})
        self.assertEqual(response.status, 401)
        response = await self.client.post('/telegram/deleteWebhook', headers={'Authorization': 'Bearer synthetic-bot'})
        self.assertEqual(response.status, 404)
        self.assertEqual(self.calls, [])

    async def test_local_operator_enrolls_only_in_ram_and_socket_is_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'control.sock'
            gateway = Gateway(hashlib.sha256(b'synthetic-owner').hexdigest(), control_socket=path)
            async with TestClient(TestServer(gateway.app())) as client:
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                reader, writer = await asyncio.open_unix_connection(str(path))
                writer.write(json.dumps({'upstream_key': 'synthetic-enrolled-vendor'}).encode() + b'\n')
                await writer.drain()
                self.assertEqual(json.loads(await reader.readline()), {'enrolled': True})
                writer.close()
                await writer.wait_closed()
                self.assertEqual(gateway.upstream_key, 'synthetic-enrolled-vendor')
                response = await client.get('/v1/models', headers={'Authorization': 'Bearer synthetic-owner'})
                self.assertEqual(response.status, 200)
                self.assertEqual(self.calls[-1][1], 'Bearer synthetic-enrolled-vendor')
                self.assertEqual(list(Path(directory).iterdir()), [path])
            self.assertIsNone(gateway.upstream_key)
            self.assertFalse(path.exists())


if __name__ == '__main__':
    unittest.main()
