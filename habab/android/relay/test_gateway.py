"""Exercise auth, route boundaries and full response forwarding without live keys."""
import hashlib
import json
import unittest
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from openai_gateway import Gateway


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        async def upstream(request):
            self.calls.append((request.path, request.headers.get('Authorization')))
            if request.path.endswith('client_secrets'):
                return web.json_response({'value': 'synthetic-ephemeral', 'expires_at': 9999999999})
            return web.Response(body=b'x' * 150000)
        app = web.Application()
        app.router.add_route('*', '/{path:.*}', upstream)
        self.upstream = TestServer(app)
        await self.upstream.start_server()
        self.gateway = Gateway(hashlib.sha256(b'synthetic-owner').hexdigest(), hashlib.sha256(b'synthetic-bot').hexdigest())
        self.patch = patch('openai_gateway.UPSTREAM', str(self.upstream.make_url('')).rstrip('/'))
        self.patch.start()
        self.client = TestClient(TestServer(self.gateway.app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.upstream.close()
        self.patch.stop()

    async def test_unknown_keys_cannot_reach_either_provider(self):
        for path in ['/v1/models', '/v1/realtime?model=gpt-realtime-mini', '/telegram/getMe']:
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


if __name__ == '__main__':
    unittest.main()
