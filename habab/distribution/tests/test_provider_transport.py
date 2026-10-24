"""Explicit relay selection must not change the default provider route."""
import os
import unittest
from unittest.mock import patch
from homeassistant.components.habab_home.providers import provider_api_base
from homeassistant.components.habab_telegram import transport


class TransportTests(unittest.TestCase):
    def test_default_routes_are_direct(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(provider_api_base('openai'), 'https://api.openai.com/v1')
            self.assertEqual(transport.get_me_request('synthetic-token'), ('https://api.telegram.org/botsynthetic-token/getMe', {}))

    def test_relay_moves_bot_credential_out_of_url(self):
        with patch.dict(os.environ, {'HABAB_TELEGRAM_RELAY': 'https://relay.test/'}):
            self.assertEqual(transport.get_me_request('synthetic-token'), ('https://relay.test/telegram/getMe', {'Authorization': 'Bearer synthetic-token'}))

    def test_unsafe_relay_bases_are_rejected(self):
        for value in ['http://relay.test', 'https://user:pass@relay.test', 'https://relay.test?key=x', 'https://relay.test/path']:
            with self.subTest(value=value), patch.dict(os.environ, {'HABAB_TELEGRAM_RELAY': value}):
                with self.assertRaises(ValueError):
                    transport.relay_base()

    def test_provider_base_has_no_environment_redirect(self):
        with patch.dict(os.environ, {'HABAB_OPENAI_BASE_URL': 'https://relay.test/v1'}):
            self.assertEqual(provider_api_base('openai'), 'https://api.openai.com/v1')
            self.assertEqual(provider_api_base('habab'), 'https://habab-voice.wpmix.net/gemini/v1beta')
