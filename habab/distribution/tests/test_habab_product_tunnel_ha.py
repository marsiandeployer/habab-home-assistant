"""Exercise the tunnel flow and lifecycle with Home Assistant's real classes."""

import asyncio
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import homeassistant.components


DIST = Path(__file__).resolve().parents[1]
SRC = DIST / "src"
homeassistant.components.__path__.insert(0, str(SRC))
_CANONICAL_NAME = "homeassistant.components.habab_product_tunnel.web_tunnel"
if _CANONICAL_NAME not in sys.modules:
    canonical_path = DIST.parent / "product-tunnel" / "web_tunnel.py"
    spec = spec_from_file_location(_CANONICAL_NAME, canonical_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Canonical web tunnel source is missing")
    canonical = module_from_spec(spec)
    sys.modules[_CANONICAL_NAME] = canonical
    spec.loader.exec_module(canonical)

from homeassistant.components.habab_product_tunnel import (  # noqa: E402
    async_setup_entry,
    async_unload_entry,
)
from homeassistant.components.habab_product_tunnel.config_flow import (  # noqa: E402
    HababTunnelConfigFlow,
    HababTunnelOptionsFlow,
)


class HababTunnelConfigFlowTests(unittest.IsolatedAsyncioTestCase):
    def make_flow(self):
        flow = HababTunnelConfigFlow()
        flow.hass = Mock()
        flow.context = {}
        flow.async_set_unique_id = AsyncMock()
        flow._abort_if_unique_id_configured = Mock()
        flow.async_create_entry = Mock(return_value={"type": "create_entry"})
        flow.async_show_form = Mock(return_value={"type": "form"})
        return flow

    async def test_user_flow_accepts_valid_wss_and_keeps_token_in_entry_data(self):
        flow = self.make_flow()
        token = "R" * 32
        result = await flow.async_step_user({
            "relay": "wss://relay.example/_habab_tunnel/control",
            "home": "Home.Example",
            "token": token,
        })

        flow.async_set_unique_id.assert_awaited_once_with("home.example")
        flow._abort_if_unique_id_configured.assert_called_once_with()
        flow.async_create_entry.assert_called_once_with(
            title="Удалённый доступ · home.example",
            data={
                "relay": "wss://relay.example/_habab_tunnel/control",
                "home": "home.example",
                "token": token,
            },
        )
        self.assertEqual(result, {"type": "create_entry"})

    async def test_user_flow_rejects_non_wss_relay(self):
        flow = self.make_flow()
        result = await flow.async_step_user({
            "relay": "ws://relay.example/_habab_tunnel/control",
            "home": "home.example",
            "token": "R" * 32,
        })

        self.assertEqual(result, {"type": "form"})
        self.assertEqual(flow.async_show_form.call_args.kwargs["errors"], {"base": "invalid_config"})
        flow.async_create_entry.assert_not_called()

    def test_home_assistant_config_flow_registers_native_options_flow(self):
        config_entry = Mock()
        self.assertIsInstance(
            HababTunnelConfigFlow.async_get_options_flow(config_entry),
            HababTunnelOptionsFlow,
        )


class HababTunnelLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_entry_setup_shutdown_and_unload_own_the_client_task(self):
        agent_started = asyncio.Event()
        release_agent = asyncio.Event()

        async def fake_agent(session, relay, home, token):
            agent_started.set()
            await release_agent.wait()

        event_listener = Mock()
        entry_unload_callbacks = []
        hass = SimpleNamespace(
            data={},
            bus=SimpleNamespace(async_listen_once=Mock(return_value=event_listener)),
            config_entries=SimpleNamespace(
                async_forward_entry_setups=AsyncMock(),
                async_unload_platforms=AsyncMock(side_effect=[False, True]),
                async_reload=AsyncMock(return_value=True),
            ),
        )
        entry = SimpleNamespace(
            entry_id="test-home",
            data={"relay": "wss://relay.example/_habab_tunnel/control",
                  "home": "home.example", "token": "R" * 32},
            options={},
            async_on_unload=entry_unload_callbacks.append,
            add_update_listener=Mock(return_value=Mock()),
        )

        with patch(f"{_CANONICAL_NAME}.run_agent", new=fake_agent):
            await async_setup_entry(hass, entry)
            await asyncio.wait_for(agent_started.wait(), timeout=3)
            client = hass.data["habab_product_tunnel"][entry.entry_id]
            session = client._session
            self.assertEqual(client.status, "enabled")
            self.assertFalse(session.closed)
            hass.bus.async_listen_once.assert_called_once()
            self.assertEqual(len(entry_unload_callbacks), 2)
            self.assertTrue(all(callable(callback) for callback in entry_unload_callbacks))

            self.assertFalse(await async_unload_entry(hass, entry))
            self.assertIs(hass.data["habab_product_tunnel"][entry.entry_id], client)
            self.assertEqual(client.status, "enabled")
            self.assertFalse(session.closed)

            self.assertTrue(await async_unload_entry(hass, entry))
            self.assertTrue(session.closed)
            self.assertEqual(hass.data["habab_product_tunnel"], {})
            hass.config_entries.async_forward_entry_setups.assert_awaited_once()
            self.assertEqual(hass.config_entries.async_unload_platforms.await_count, 2)


if __name__ == "__main__":
    unittest.main()
