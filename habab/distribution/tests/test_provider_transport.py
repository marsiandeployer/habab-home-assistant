"""Explicit relay selection must not change the default provider route."""
import os
import unittest
from unittest.mock import patch
from homeassistant.components.habab_home import openai_transport
from homeassistant.components.habab_telegram import transport


class TransportTests(unittest.TestCase):
    def test_default_routes_are_direct(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(openai_transport.endpoint('chat/completions'), 'https://api.openai.com/v1/chat/completions')
            self.assertEqual(transport.get_me_request('synthetic-token'), ('https://api.telegram.org/botsynthetic-token/getMe', {}))

    def test_relay_moves_bot_credential_out_of_url(self):
        with patch.dict(os.environ, {'HABAB_TELEGRAM_RELAY': 'https://relay.test/'}):
            self.assertEqual(transport.get_me_request('synthetic-token'), ('https://relay.test/telegram/getMe', {'Authorization': 'Bearer synthetic-token'}))

    def test_unsafe_relay_bases_are_rejected(self):
        for value in ['http://relay.test', 'https://user:pass@relay.test', 'https://relay.test?key=x', 'https://relay.test/path']:
            with self.subTest(value=value), patch.dict(os.environ, {'HABAB_TELEGRAM_RELAY': value}):
                with self.assertRaises(ValueError):
                    transport.relay_base()

    def test_openai_route_and_voice_model_validation(self):
        with patch.dict(os.environ, {'HABAB_OPENAI_BASE_URL': 'https://relay.test/v1'}):
            self.assertEqual(openai_transport.websocket_url('gpt-realtime-mini'), 'wss://relay.test/v1/realtime?model=gpt-realtime-mini')
            for value in ['gpt-realtime?key=x', 'other-model']:
                with self.assertRaises(ValueError):
                    openai_transport.websocket_url(value)
            with self.assertRaises(ValueError):
                openai_transport.endpoint('../files')
