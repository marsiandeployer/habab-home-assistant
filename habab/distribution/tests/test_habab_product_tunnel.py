"""The HA lifecycle owns and closes the credential-free tunnel session."""

import asyncio
import inspect
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from aiohttp import web


DISTRIBUTION = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DISTRIBUTION / 'src'))

from habab_product_tunnel import async_setup_entry, async_unload_entry  # noqa: E402
from habab_product_tunnel.client import TunnelClient  # noqa: E402


class TunnelClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_loopback_session_and_shutdown_cancel(self):
        seen = asyncio.Event()
        request_state = {}

        async def handle(request):
            request_state['cookie'] = request.headers.get('Cookie')
            seen.set()
            return web.Response(text='Home Assistant')

        app = web.Application()
        app.router.add_get('/', handle)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        sessions = []
        client = None

        async def agent(session, relay, home, token):
            sessions.append(session)
            async with session.get(f'http://127.0.0.1:{port}/') as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(await response.text(), 'Home Assistant')
            await asyncio.Event().wait()

        try:
            client = TunnelClient(
                'wss://relay.example/_habab_tunnel/control',
                'home.example', 'secret-value-not-logged', agent,
            )
            await client.async_start()
            await asyncio.wait_for(seen.wait(), timeout=3)
            self.assertEqual(client.status, 'enabled')
            self.assertIsNone(request_state['cookie'])
            self.assertFalse(sessions[0].closed)
            await client.async_stop()
            self.assertEqual(client.status, 'stopped')
            self.assertTrue(sessions[0].closed)
        finally:
            if client:
                await client.async_stop()
            await runner.cleanup()

    async def test_home_assistant_entry_starts_and_unloads_agent(self):
        waiting = asyncio.Event()
        started = asyncio.Event()
        run_agent_calls = []

        async def run_agent(session, relay, home, token):
            run_agent_calls.append((session, relay, home, token))
            started.set()
            await waiting.wait()

        canonical = ModuleType('habab_product_tunnel.web_tunnel')
        canonical.run_agent = run_agent
        ha = ModuleType('homeassistant')
        ha_const = ModuleType('homeassistant.const')
        ha_const.Platform = SimpleNamespace(SENSOR='sensor')
        ha_const.EVENT_HOMEASSISTANT_STOP = 'homeassistant_stop'

        class ConfigEntries:
            async def async_forward_entry_setups(self, entry, platforms):
                self.forwarded = platforms

            async def async_unload_platforms(self, entry, platforms):
                self.unloaded = platforms
                return True

        class EventBus:
            def async_listen_once(self, event, listener):
                self.event = event
                self.listener = listener
                return lambda: setattr(self, 'unsubscribed', True)

            async def async_reload(self, entry_id):
                return True

        class Entry:
            entry_id = 'test-home'
            data = {
                'relay': 'wss://relay.example/_habab_tunnel/control',
                'home': 'home.example', 'token': 'test-token',
            }
            options = {}

            def async_on_unload(self, callback):
                if inspect.iscoroutinefunction(callback):
                    raise AssertionError('unload hooks must be synchronous unsubscribe callbacks')
                self.unload_callbacks = getattr(self, 'unload_callbacks', []) + [callback]

            def add_update_listener(self, listener):
                return lambda: None

        bus = EventBus()
        hass = SimpleNamespace(data={}, bus=bus, config_entries=ConfigEntries())
        entry = Entry()
        with patch.dict(sys.modules, {
            'homeassistant': ha,
            'homeassistant.const': ha_const,
            'habab_product_tunnel.web_tunnel': canonical,
        }):
            await async_setup_entry(hass, entry)
            await asyncio.wait_for(started.wait(), timeout=3)
            session = run_agent_calls[0][0]
            self.assertEqual(hass.config_entries.forwarded, ['sensor'])
            self.assertFalse(session.closed)
            self.assertEqual(run_agent_calls[0][1:], (
                entry.data['relay'], entry.data['home'], entry.data['token'],
            ))
            self.assertEqual(bus.event, ha_const.EVENT_HOMEASSISTANT_STOP)
            self.assertTrue(inspect.iscoroutinefunction(bus.listener))
            await bus.listener(None)
            self.assertTrue(session.closed)

            self.assertTrue(await async_unload_entry(hass, entry))
            self.assertEqual(hass.config_entries.unloaded, ['sensor'])
            self.assertTrue(session.closed)
            self.assertEqual(hass.data['habab_product_tunnel'], {})
            for unsubscribe in entry.unload_callbacks:
                unsubscribe()
            self.assertTrue(bus.unsubscribed)


if __name__ == '__main__':
    unittest.main()
