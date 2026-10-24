"""Legacy Habab speech fails closed; direct OpenAI keeps its speech engines."""
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

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
        for provider, host in [('openai', 'api.openai.com')]:
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

    async def test_habab_legacy_speech_never_sends_credentials_or_audio(self):
        entry = SimpleNamespace(data={'provider': 'habab', 'api_key': 'fixture-habab'})
        for operation in ('speak', 'transcribe'):
            with self.subTest(operation=operation), patch.object(speech, 'async_get_clientsession') as client:
                with self.assertRaises(ProviderError) as caught:
                    await speech.request_audio(object(), entry, operation, json={})
                self.assertEqual(caught.exception.code, 'speech_unsupported')
                client.assert_not_called()

    async def test_only_direct_openai_advertises_legacy_speech_platforms(self):
        from homeassistant.components.habab_home import async_setup_entry
        from homeassistant.const import Platform

        for provider in ('habab', 'gemini', 'deepseek', 'openai'):
            with self.subTest(provider=provider):
                hass = SimpleNamespace(config_entries=SimpleNamespace(async_forward_entry_setups=AsyncMock()))
                entry = SimpleNamespace(data={'provider': provider})
                self.assertTrue(await async_setup_entry(hass, entry))
                expected = [Platform.CONVERSATION]
                if provider == 'openai':
                    expected.extend([Platform.STT, Platform.TTS])
                hass.config_entries.async_forward_entry_setups.assert_awaited_once_with(entry, expected)

    async def test_unload_cleans_up_legacy_habab_speech_platforms(self):
        from homeassistant.components.habab_home import async_unload_entry
        from homeassistant.const import Platform

        hass = SimpleNamespace(config_entries=SimpleNamespace(async_unload_platforms=AsyncMock(return_value=True)))
        entry = SimpleNamespace(data={'provider': 'habab'})
        self.assertTrue(await async_unload_entry(hass, entry))
        hass.config_entries.async_unload_platforms.assert_awaited_once_with(
            entry, [Platform.CONVERSATION, Platform.STT, Platform.TTS],
        )
