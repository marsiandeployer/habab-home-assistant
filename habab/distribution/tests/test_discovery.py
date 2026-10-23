"""Discovery distinguishes unavailable hardware from an empty observation window."""
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from homeassistant.components import bluetooth
from homeassistant.components.habab_home import discovery


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self, components):
        return SimpleNamespace(config=SimpleNamespace(components=set(components)),
            config_entries=SimpleNamespace(flow=SimpleNamespace(async_progress=lambda: [])))

    async def test_missing_adapter_does_not_report_stale_bluetooth_devices(self):
        hass = self.fixture(["bluetooth", "zeroconf", "ssdp"])
        with patch.object(bluetooth, "async_scanner_count", return_value=0), \
             patch.object(bluetooth, "async_discovered_service_info") as read, \
             patch.object(discovery.asyncio, "sleep", AsyncMock()):
            result = await discovery.observe(hass, SimpleNamespace(flow_owners={}), 2)
        self.assertEqual(result["bluetooth"]["status"], "no_adapter")
        self.assertFalse(result["network"]["multicast_reachability_verified"])
        self.assertEqual(result["network"]["status"], "no_discovery_results")
        read.assert_not_called()

    async def test_setup_failure_is_unavailable_not_successful_empty_scan(self):
        hass = self.fixture([])
        with patch.object(discovery, "async_setup_component", AsyncMock(side_effect=RuntimeError())), \
             patch.object(discovery.asyncio, "sleep", AsyncMock()):
            result = await discovery.observe(hass, SimpleNamespace(flow_owners={}), 1)
        self.assertEqual(result["bluetooth"]["status"], "unavailable")
        self.assertEqual(result["network"]["status"], "unavailable")

    async def test_available_scanner_returns_only_observed_advertisements(self):
        hass = self.fixture(["bluetooth", "dhcp"])
        info = SimpleNamespace(name="Fixture thermometer", address="AA:BB:CC:DD:EE:FF",
                               rssi=-55, connectable=True)
        with patch.object(bluetooth, "async_scanner_count", return_value=1), \
             patch.object(bluetooth, "async_discovered_service_info", return_value=[info]), \
             patch.object(discovery.asyncio, "sleep", AsyncMock()):
            result = await discovery.observe(hass, SimpleNamespace(flow_owners={}), 3)
        self.assertEqual(result["bluetooth"]["status"], "observed")
        self.assertEqual(result["bluetooth"]["devices"][0]["rssi"], -55)
        self.assertEqual(result["bluetooth"]["devices"][0]["name"], info.name)
