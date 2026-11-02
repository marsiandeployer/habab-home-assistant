import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from provider_health import ProviderHealth, EndpointHealth

class Response:
    def __init__(self, status): self.status=status
    async def __aenter__(self): return self
    async def __aexit__(self,*args): pass

class HealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_endpoint_alert_requires_two_failures_and_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = EndpointHealth([{'name': 'Telegram', 'url': 'https://example.org/health'}], directory)
            check = monitor.checks[0]
            check['health'].notify = AsyncMock()
            client = SimpleNamespace(get=Mock(return_value=Response(503)))
            await monitor.check_one(client, check)
            check['health'].notify.assert_not_awaited()
            await monitor.check_one(client, check)
            self.assertEqual(check['health'].snapshot()['reason'], 'provider_unavailable')
            client.get.return_value = Response(200)
            await monitor.check_one(client, check)
            self.assertEqual(check['health'].snapshot()['reason'], 'ready')

    async def test_endpoint_probe_accepts_explicit_unauthenticated_status(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = EndpointHealth([{'name': 'Protected route', 'url': 'https://example.org/telegram/getMe',
                                       'expected_status': 401}], directory)
            await monitor.check_one(SimpleNamespace(get=Mock(return_value=Response(401))), monitor.checks[0])
            self.assertTrue(monitor.checks[0]['health'].snapshot()['ready'])

    def test_monitor_rejects_credentials_in_endpoint_urls(self):
        with self.assertRaises(ValueError):
            EndpointHealth([{'name': 'bad', 'url': 'https://secret@example.org/health'}], '/tmp/unused')

    async def test_missing_rejected_and_limited_are_distinct_and_no_body_is_read(self):
        with tempfile.TemporaryDirectory() as directory:
            health=ProviderHealth('gemini','gemini-3.8-live',Path(directory)/'state.json')
            client=SimpleNamespace(get=Mock(return_value=Response(403)))
            await health.probe(client,None)
            self.assertEqual(health.snapshot()['reason'],'missing_credential')
            client.get.assert_not_called()
            await health.probe(client,'synthetic-key')
            self.assertEqual(health.snapshot()['reason'],'credential_rejected')
            client.get.return_value=Response(200)
            health.observe_http(429)
            await health.probe(client,'synthetic-key')
            self.assertEqual(health.snapshot()['reason'],'rate_limited')
            self.assertFalse(health.snapshot()['ready'])
            restored=ProviderHealth('gemini','gemini-3.8-live',Path(directory)/'state.json')
            await restored.probe(client,'synthetic-key')
            self.assertFalse(restored.snapshot()['ready'])
            health.observe_http(200)
            await health.probe(client,'synthetic-key')
            self.assertTrue(health.snapshot()['ready'])
            self.assertNotIn('synthetic-key',health.path.read_text())

    async def test_notifications_once_per_change_and_retry_after_delivery_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            h=ProviderHealth('deepseek','deepseek-flash',Path(directory)/'state.json',
                             notify_command=['python3','sender.py'],recipient='@synthetic')
            h.observe_http(429)
            p=SimpleNamespace(returncode=0,wait=AsyncMock(return_value=0),communicate=AsyncMock(return_value=(b'HABAB_TELEGRAM_RESULT={"state":"delivered","message_id":1}\n', b'')))
            with patch('asyncio.create_subprocess_exec',new=AsyncMock(return_value=p)) as send:
                await h.notify(); await h.notify()
                self.assertEqual(send.await_count,1)
                self.assertIn('HTTP 429',send.call_args.args[-1])
                h.observe_http(200);await h.notify()
                self.assertEqual(send.await_count,2)
                restarted=ProviderHealth('deepseek','deepseek-flash',h.path,
                                         notify_command=['sender'],recipient='@synthetic')
                restarted.observe_http(200);await restarted.notify()
                self.assertEqual(send.await_count,2)
                h.observe_http(403);p.returncode=1
                await h.notify();await h.notify()
                self.assertEqual(send.await_count,4)
                self.assertEqual(h.notified,'ready')

    async def test_aggregate_endpoint_is_503_when_voice_is_not_ready(self):
        from openai_gateway import Gateway
        class HealthResponse:
            async def __aenter__(self): return self
            async def __aexit__(self,*args): pass
            async def json(self): return {'checks': {'voice': {'ready': False, 'reason': 'missing_credential'}}}
        with tempfile.TemporaryDirectory() as directory:
            health=ProviderHealth('deepseek','deepseek-flash',Path(directory)/'state.json')
            health.observe_http(200)
            gateway=Gateway('a'*64,voice_service_key='synthetic-service')
            gateway.provider_health=health
            gateway.client=SimpleNamespace(get=Mock(return_value=HealthResponse()))
            response=await gateway.readiness(None)
            self.assertEqual(response.status,503)
            payload=json.loads(response.body)
            self.assertFalse(payload['ready'])
            self.assertTrue(payload['checks']['text']['ready'])
            self.assertEqual(payload['checks']['voice']['reason'],'missing_credential')
