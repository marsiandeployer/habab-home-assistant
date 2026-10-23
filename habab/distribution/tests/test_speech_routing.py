"""Habab speech uses its service; direct providers never inherit a relay override."""
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from homeassistant.components.habab_home import speech
from homeassistant.components.habab_home.providers import ProviderError


class Content:
    async def iter_chunked(self, _size):
        yield b'fixture-audio'


class Response:
    status = 200
    content = Content()
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass


class SpeechRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_speech_provider_controls_endpoint_and_key(self):
        for provider, host in [('habab', 'habab-voice.wpmix.net'), ('openai', 'api.openai.com')]:
            for operation, path in [('transcribe', 'audio/transcriptions'), ('speak', 'audio/speech')]:
                with self.subTest(provider=provider, operation=operation):
                    client = SimpleNamespace(post=Mock(return_value=Response()))
                    entry = SimpleNamespace(data={'provider': provider, 'api_key': 'fixture-' + provider})
                    with patch.dict(os.environ, {'HABAB_OPENAI_BASE_URL': 'https://wrong.test/v1'}), \
                            patch.object(speech, 'async_get_clientsession', return_value=client):
                        result = await speech.request_audio(object(), entry, operation, json={})
                    self.assertEqual(result, b'fixture-audio')
                    self.assertEqual(client.post.call_args.args[0], f'https://{host}/v1/{path}')
                    self.assertEqual(client.post.call_args.kwargs['headers']['Authorization'], 'Bearer fixture-' + provider)
                    self.assertFalse(client.post.call_args.kwargs['allow_redirects'])

    async def test_deepseek_key_is_never_sent_to_a_speech_api(self):
        entry = SimpleNamespace(data={'provider': 'deepseek', 'api_key': 'fixture-deepseek'})
        with patch.object(speech, 'async_get_clientsession') as client:
            with self.assertRaises(ProviderError) as caught:
                await speech.request_audio(object(), entry, 'speak', json={})
        self.assertEqual(caught.exception.code, 'invalid_provider')
        client.assert_not_called()
