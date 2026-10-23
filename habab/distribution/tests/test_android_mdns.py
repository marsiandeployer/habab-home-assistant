"""Do not take a LAN name already advertised by another Home Assistant."""
import asyncio
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.components.habab_home import mdns


class AndroidAliasTests(unittest.IsolatedAsyncioTestCase):
    def test_publish_only_lan_unicast(self):
        self.assertEqual(mdns.lan_addresses([
            "127.0.0.1", "0.0.0.0", "192.168.3.70", "192.168.3.70",
            "8.8.8.8", "224.0.0.251", "fe80::1", "::1", "fd00::123",
        ]), ["192.168.3.70", "fd00::123"])

    async def test_non_android_does_not_start_responder(self):
        with patch.dict(os.environ, {"HABAB_RUNTIME_KIND": ""}), patch.object(mdns.zeroconf, "async_get_async_instance", AsyncMock()) as instance:
            await mdns.async_setup_alias(SimpleNamespace())
        instance.assert_not_awaited()

    async def scenario(self, occupied):
        callbacks = {}
        hass = SimpleNamespace(data={"habab_home": {}}, http=SimpleNamespace(server_port=8123),
                               async_add_executor_job=AsyncMock(return_value=["192.168.3.70"]),
                               bus=SimpleNamespace(async_listen_once=lambda event, cb: callbacks.update(stop=cb)))
        async def announced(*args, **kwargs):
            return asyncio.sleep(0)
        aio = SimpleNamespace(zeroconf=object(), async_register_service=AsyncMock(side_effect=announced),
                              async_unregister_service=AsyncMock(side_effect=announced))
        resolver = SimpleNamespace(async_request=AsyncMock(), parsed_addresses=lambda: occupied)
        cancel = Mock()
        with patch.dict(os.environ, {"HABAB_RUNTIME_KIND": "android_proot"}), \
                patch.object(mdns.zeroconf, "async_get_async_instance", AsyncMock(return_value=aio)), \
                patch.object(mdns, "AddressResolver", return_value=resolver), \
                patch.object(mdns, "async_at_started", side_effect=lambda h, cb: callbacks.update(start=cb)), \
                patch.object(mdns, "async_track_time_interval", return_value=cancel):
            await mdns.async_setup_alias(hass)
            await callbacks["start"]()
            state = hass.data["habab_home"]["lan_alias"]["status"]
            await callbacks["stop"](None)
        cancel.assert_called_once()
        return aio, state

    async def test_conflict_never_registers(self):
        aio, state = await self.scenario(["192.168.3.20"])
        self.assertEqual(state, "hostname_conflict")
        aio.async_register_service.assert_not_awaited()

    async def test_free_hostname_announces_and_withdraws(self):
        aio, state = await self.scenario([])
        self.assertEqual(state, "announced")
        info = aio.async_register_service.await_args.args[0]
        self.assertEqual(info.server, "homeassistant.local.")
        self.assertEqual(info.port, 8123)
        aio.async_unregister_service.assert_awaited_once_with(info)
