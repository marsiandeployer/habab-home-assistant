"""Voice ACL, selected-route boundaries and bounded audio handling."""
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from homeassistant.components.habab_telegram.voice import (
    MAX_VOICE_BYTES, VoiceError, transcribe_voice, validate_voice, valid_file_path,
)
import test_telegram_backend as _backend

VOICE = {'file_id': 'test_voice', 'duration': 3, 'file_size': 80, 'mime_type': 'audio/ogg'}


class VoiceACLTests(unittest.IsolatedAsyncioTestCase):
    manager = _backend.TelegramManagerTests.manager
    # Reuse fixture helpers without repeating inherited text-bridge tests.
    async def voice_message(self, manager, **changes):
        entry = SimpleNamespace(entry_id='entry', data={'owner_id': 'owner', 'bot_id': 1})
        manager.entry_for_owner = lambda owner: entry
        manager.bindings['entry'] = {'chat_id': '10', 'user_id': '20', 'bot_id': 1}
        message = {'chat_id': 10, 'user_id': 20, 'message_id': 99, 'chat_type': 'private',
                   'text': '', 'voice': dict(VOICE), **changes}
        await manager.handle_one(entry, message)
        await asyncio.gather(*manager.turn_tasks.values(), return_exceptions=True)
        return entry

    async def test_voice_owner_transcript_uses_existing_assistant_and_text_reply(self):
        manager = self.manager()
        assistant = manager.hass.data['habab_home']['assistant']
        assistant.telegram_session.return_value = {'session_id': 'session'}
        assistant.telegram_sessions['owner:entry:10'] = 'session'
        assistant.run.return_value = {'messages': [{'text': 'Готово'}]}
        with patch('homeassistant.components.habab_telegram.transcribe_voice', new=AsyncMock(return_value='Покажи устройства')) as stt, \
             patch('homeassistant.components.habab_telegram.async_get_clientsession', return_value=object()):
            await self.voice_message(manager)
        stt.assert_awaited_once()
        assistant.run.assert_awaited_once_with('owner', 'telegram', 'session', 'Покажи устройства', 'entry', '10')
        manager.send.assert_awaited_with('entry', '10', 'Готово')

    async def test_unauthorized_voice_is_not_downloaded_or_transcribed(self):
        manager = self.manager()
        with patch('homeassistant.components.habab_telegram.transcribe_voice', new=AsyncMock()) as stt:
            await self.voice_message(manager, user_id=999, chat_id=999)
        stt.assert_not_awaited()
        manager.hass.data['habab_home']['assistant'].run.assert_not_awaited()

    async def test_group_voice_is_ignored_before_membership_or_transcription(self):
        manager = self.manager()
        with patch('homeassistant.components.habab_telegram.transcribe_voice', new=AsyncMock()) as stt:
            await self.voice_message(manager, chat_id=-100, chat_type='supergroup')
        stt.assert_not_awaited()
        manager.get_chat_member.assert_not_awaited()
        manager.send.assert_not_awaited()

    async def test_transcription_failure_does_not_run_agent(self):
        manager = self.manager()
        assistant = manager.hass.data['habab_home']['assistant']
        assistant.telegram_session.return_value = {'session_id': 'session'}
        assistant.telegram_sessions['owner:entry:10'] = 'session'
        with patch('homeassistant.components.habab_telegram.transcribe_voice', new=AsyncMock(side_effect=VoiceError('voice_relay_unsupported'))), \
             patch('homeassistant.components.habab_telegram.async_get_clientsession', return_value=object()):
            await self.voice_message(manager)
        assistant.run.assert_not_awaited()
        self.assertIn('Не удалось распознать', manager.send.call_args.args[2])


    async def test_cancel_while_transcribing_never_runs_agent(self):
        manager = self.manager()
        assistant = manager.hass.data['habab_home']['assistant']
        assistant.telegram_session.return_value = {'session_id': 'session'}
        assistant.telegram_sessions['owner:entry:10'] = 'session'
        async def transcript(*args):
            manager.cancel_epochs.setdefault('entry', {})['10'] = 1
            return 'Покажи устройства'
        with patch('homeassistant.components.habab_telegram.transcribe_voice', new=transcript), \
             patch('homeassistant.components.habab_telegram.async_get_clientsession', return_value=object()):
            await self.voice_message(manager)
        assistant.run.assert_not_awaited()
        manager.send.assert_not_awaited()

    async def test_revoked_owner_after_transcription_never_runs_agent(self):
        manager = self.manager()
        assistant = manager.hass.data['habab_home']['assistant']
        assistant.telegram_session.return_value = {'session_id': 'session'}
        assistant.telegram_sessions['owner:entry:10'] = 'session'
        async def transcript(*args):
            manager.hass.auth.async_get_user.return_value.is_active = False
            return 'Покажи устройства'
        with patch('homeassistant.components.habab_telegram.transcribe_voice', new=transcript), \
             patch('homeassistant.components.habab_telegram.async_get_clientsession', return_value=object()):
            await self.voice_message(manager)
        assistant.run.assert_not_awaited()
        manager.send.assert_not_awaited()


class VoiceBoundsTests(unittest.IsolatedAsyncioTestCase):
    def test_metadata_limits_and_path_traversal(self):
        self.assertEqual(validate_voice(VOICE), VOICE)
        for voice in ({**VOICE, 'duration': 121}, {**VOICE, 'file_size': MAX_VOICE_BYTES + 1},
                      {**VOICE, 'file_id': '../oops'}, {**VOICE, 'duration': True},
                      {**VOICE, 'mime_type': 'video/mp4'}):
            with self.subTest(voice=voice), self.assertRaises(VoiceError):
                validate_voice(voice)
        for path in ('../x', 'voice/../x', 'voice/a?token=x', 'https://host/file', '/voice/file'):
            self.assertFalse(valid_file_path(path))
        self.assertTrue(valid_file_path('voice/file_1.oga'))

    async def test_unsupported_provider_has_zero_network_calls(self):
        session = SimpleNamespace(get=AsyncMock(), post=AsyncMock())
        hass = SimpleNamespace(data={'habab_home': {'setup_agent': SimpleNamespace(
            settings=lambda: {'provider': 'deepseek', 'api_key': 'test-key'})}})
        with self.assertRaisesRegex(VoiceError, 'voice_provider_unsupported'):
            await transcribe_voice(hass, session, SimpleNamespace(data={}), {}, VOICE)
        session.get.assert_not_called()
        session.post.assert_not_called()

    async def test_failed_relay_capability_does_not_try_direct(self):
        class Content:
            async def iter_chunked(self, size):
                yield json.dumps({'telegram': {}, 'speech': {}}).encode()
        class Reply:
            status = 200
            content = Content()
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
        calls = []
        def get(url, **kwargs):
            calls.append(url)
            return Reply()
        session = SimpleNamespace(get=get, post=AsyncMock())
        hass = SimpleNamespace(data={'habab_home': {'setup_agent': SimpleNamespace(
            settings=lambda: {'provider': 'habab', 'api_key': 'test-key'})}})
        state = {'telegram_transport': {'mode': 'habab_relay', 'base_url': 'https://habab-voice.wpmix.net',
                 'service_key': 'test-key', 'protocol': 'telegram-bot-api-relay-v1'}}
        with self.assertRaisesRegex(VoiceError, 'voice_relay_unsupported'):
            await transcribe_voice(hass, session, SimpleNamespace(data={'token': 'test-bot'}), state, VOICE)
        self.assertEqual(calls, ['https://habab-voice.wpmix.net/v1/capabilities'])
        session.post.assert_not_called()

    async def test_habab_download_and_transcription_stay_on_selected_route(self):
        class Reply:
            status = 200
            def __init__(self, raw): self.raw = raw; self.content = self
            async def iter_chunked(self, size): yield self.raw
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
        calls = []
        def get(url, **kwargs):
            calls.append(('GET', url, kwargs))
            return Reply(json.dumps({'telegram': {'protocol': 'telegram-bot-api-relay-v1',
                'methods': ['getFile'], 'voice': {'download': '/telegram/downloadFile'}},
                'speech': {'transcriptions': '/v1/audio/transcriptions', 'model': 'habab-transcribe', 'ready': True}}).encode())
        def post(url, **kwargs):
            calls.append(('POST', url, kwargs))
            if url.endswith('/getFile'):
                return Reply(json.dumps({'ok': True, 'result': {'file_path': 'voice/test.oga', 'file_size': 20}}).encode())
            if url.endswith('/downloadFile'): return Reply(b'OggS' + b'unit')
            return Reply(json.dumps({'text': 'Покажи устройства'}).encode())
        session = SimpleNamespace(get=get, post=post)
        hass = SimpleNamespace(data={'habab_home': {'setup_agent': SimpleNamespace(
            settings=lambda: {'provider': 'habab', 'api_key': 'test-key'})}})
        state = {'telegram_transport': {'mode': 'habab_relay', 'base_url': 'https://habab-voice.wpmix.net',
                 'service_key': 'test-key', 'protocol': 'telegram-bot-api-relay-v1'}}
        result = await transcribe_voice(hass, session, SimpleNamespace(data={'token': 'test-bot'}), state, VOICE)
        self.assertEqual(result, 'Покажи устройства')
        self.assertEqual([call[1] for call in calls], [
            'https://habab-voice.wpmix.net/v1/capabilities',
            'https://habab-voice.wpmix.net/telegram/getFile',
            'https://habab-voice.wpmix.net/telegram/downloadFile',
            'https://habab-voice.wpmix.net/v1/audio/transcriptions'])
        self.assertEqual(calls[1][2]['headers']['X-Habab-Telegram-Bot-Token'], 'test-bot')
        self.assertNotIn('test-bot', calls[1][1])
        self.assertEqual(calls[3][2]['headers'], {'Authorization': 'Bearer test-key'})

    async def test_direct_download_honors_explicit_proxy_and_byte_bound(self):
        class Reply:
            status = 200
            def __init__(self, raw): self.raw = raw; self.content = self
            async def iter_chunked(self, size): yield self.raw
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
        calls = []
        def request(url, **kwargs):
            calls.append((url, kwargs))
            if url.endswith('/getFile'):
                return Reply(json.dumps({'ok': True, 'result': {'file_path': 'voice/test.oga'}}).encode())
            return Reply(b'OggS' + b'x' * 40)
        settings = {'provider': 'openai', 'api_key': 'test-provider-key'}
        hass = SimpleNamespace(data={'habab_home': {'setup_agent': SimpleNamespace(settings=lambda: settings)}})
        with patch('homeassistant.components.habab_telegram.voice.MAX_VOICE_BYTES', 32):
            with self.assertRaisesRegex(VoiceError, 'voice_too_large'):
                await transcribe_voice(hass, SimpleNamespace(get=request, post=request),
                    SimpleNamespace(data={'token': 'test-bot', 'proxy_url': 'http://proxy.invalid:8080'}),
                    {'telegram_transport': {'mode': 'direct'}}, {**VOICE, 'file_size': 10})
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call[1]['proxy'] == 'http://proxy.invalid:8080' for call in calls))
        self.assertTrue(all(call[0].startswith('https://api.telegram.org/') for call in calls))
