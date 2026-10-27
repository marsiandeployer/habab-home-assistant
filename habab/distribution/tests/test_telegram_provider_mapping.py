"""Accept the immutable provider settings returned by Home Assistant."""

from types import MappingProxyType, SimpleNamespace
import unittest

from homeassistant.components.habab_telegram.transport import telegram_transport_identity


class ProviderMappingTest(unittest.TestCase):
    def test_habab_config_entry_data_selects_relay(self):
        settings = MappingProxyType({"provider": "habab", "api_key": "test-key"})
        hass = SimpleNamespace(data={"habab_home": {
            "setup_agent": SimpleNamespace(settings=lambda: settings),
        }})
        self.assertEqual(telegram_transport_identity(hass)[0], "habab_relay")
