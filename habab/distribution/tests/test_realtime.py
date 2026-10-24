"""Voice must use the authenticated owner's canonical chat, never another session."""
import json
import os
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.components.habab_home import realtime
from homeassistant.components.habab_home.setup_agent import AgentError
from homeassistant.components.habab_home.assistant import AssistantService


class Request(dict):
    def __init__(self, owner=None, data=None, assistant=None):
        self.app = {'hass': SimpleNamespace(data={'habab_home': {'assistant': assistant}})}
        super().__init__(hass_user=owner)
        self.json = AsyncMock(return_value=data or {})


class RealtimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_gemini_http_event_reaches_real_shared_store_without_engine_or_false_pair(self):
        owner = SimpleNamespace(id='alice', is_active=True, is_admin=True)
        runtime = SimpleNamespace(ensure=AsyncMock(), diagnostics=SimpleNamespace())
        hass = SimpleNamespace(auth=SimpleNamespace(async_get_user=AsyncMock(return_value=owner)))
        stores = []
        def store_factory(*args, **kwargs):
            store = SimpleNamespace(async_load=AsyncMock(return_value=None), async_save=AsyncMock())
            stores.append(store); return store
        with patch('homeassistant.components.habab_home.assistant.Store', side_effect=store_factory):
            assistant = AssistantService(hass, runtime)
        await assistant.setup()
        assistant.web_sessions = {'alice': 'web-alice'}
        assistant.web_contexts = {'alice': {'session_id': 'web-alice', 'epoch': 3}}
        request = Request(owner, {'session_id': 'voice2_fixture', 'event_id': 'gemini_local_fixture_input_7',
            'kind': 'event', 'role': 'user', 'text': 'Запоздавший фрагмент'}, assistant)
        request.app['hass'] = hass
        hass.data = {'habab_home': {'assistant': assistant, 'voice_sessions': {'voice2_fixture': {
            'owner': 'alice', 'expires': time.monotonic() + 60, 'history_session': 'web-alice',
            'history_epoch': 3, 'provider': 'gemini'}}}}
        with patch.object(realtime, 'async_dispatcher_send'):
            response = await realtime.RealtimeTranscriptView().post(request)
            duplicate = await realtime.RealtimeTranscriptView().post(request)
        self.assertEqual(response.status, 200); self.assertEqual(duplicate.status, 200)
        self.assertTrue(json.loads(duplicate.body)['duplicate'])
        runtime.ensure.assert_not_awaited()
        self.assertEqual(len(assistant.voice_receipts), 1)
        receipt = next(iter(assistant.voice_receipts.values()))
        self.assertEqual(receipt['role'], 'user'); self.assertEqual(receipt['text'], 'Запоздавший фрагмент')
        self.assertIsNone(receipt['correlation']); self.assertNotIn('assistant_text', receipt)
        self.assertEqual(assistant._project_pending_voice('alice', 'web-alice', [], 3),
                         [{'role': 'user', 'text': 'Запоздавший фрагмент'}])
        self.assertEqual(stores[-1].async_save.await_count, 1)

    async def test_gemini_without_independent_journal_rejects_before_provider_http(self):
        owner = SimpleNamespace(id='alice', is_active=True, is_admin=True)
        request = Request(owner, {'client': 'native_v2'}, SimpleNamespace(web_session=AsyncMock()))
        request.app['hass'].data['habab_home']['setup_agent'] = SimpleNamespace(settings=lambda: {
            'provider': 'gemini', 'api_key': 'synthetic-master'})
        with patch.object(realtime, 'async_get_clientsession') as client:
            response = await realtime.RealtimeSessionView().post(request)
        self.assertEqual(response.status, 400)
        self.assertEqual(json.loads(response.body)['message'], 'voice_history_protocol_required')
        client.assert_not_called()

    async def test_independent_gemini_event_uses_server_lease_metadata_without_fake_pair(self):
        owner = SimpleNamespace(id='alice', is_active=True, is_admin=True)
        for role, text in [('user', 'Поздняя часть распознанной речи'),
                           ('assistant', 'Полностью проигранный ответ')]:
            assistant = SimpleNamespace(record_voice_turn=AsyncMock(),
                record_voice_event=AsyncMock(return_value={'recorded': True, 'duplicate': False, 'messages': []}))
            request = Request(owner, {'session_id': 'voice2_test', 'event_id': 'gemini_local_fixture',
                'kind': 'event', 'role': role, 'text': text, 'owner': 'bob',
                'metadata': {'provider_response_id': 'forged', 'correlation': 'exact', 'provider': 'openai'}}, assistant)
            request.app['hass'].data['habab_home']['voice_sessions'] = {'voice2_test': {
                'owner': 'alice', 'expires': time.monotonic() + 60, 'history_session': None,
                'history_epoch': 7, 'provider': 'gemini'}}
            with patch.object(realtime, 'async_dispatcher_send'):
                response = await realtime.RealtimeTranscriptView().post(request)
            self.assertEqual(response.status, 200)
            assistant.record_voice_turn.assert_not_awaited()
            assistant.record_voice_event.assert_awaited_once_with('alice', None, 'gemini_local_fixture', role, text,
                history_epoch=7, provider='gemini', correlation=None)

    async def test_provider_journal_modes_cannot_silently_fall_back_to_each_other(self):
        owner = SimpleNamespace(id='alice', is_active=True, is_admin=True)
        for provider, data in [('openai', {'kind': 'event', 'role': 'user', 'text': 'Речь'}),
                ('openai', {'kind': 'event_typo', 'user_text': 'Речь', 'assistant_text': 'Ответ'}),
                ('gemini', {'user_text': 'Речь', 'assistant_text': 'Ответ'})]:
            assistant = SimpleNamespace(record_voice_turn=AsyncMock(), record_voice_event=AsyncMock())
            request = Request(owner, dict(data, session_id='voice2_test', event_id='local_fixture'), assistant)
            request.app['hass'].data['habab_home']['voice_sessions'] = {'voice2_test': {
                'owner': 'alice', 'expires': time.monotonic() + 60, 'history_session': None,
                'history_epoch': 1, 'provider': provider}}
            result = await realtime.RealtimeTranscriptView().post(request)
            self.assertEqual(result.status, 400)
            assistant.record_voice_turn.assert_not_awaited(); assistant.record_voice_event.assert_not_awaited()

    async def test_gemini_invalid_protocol_or_model_never_falls_back_or_calls_provider(self):
        owner = SimpleNamespace(id='alice', is_active=True, is_admin=True)
        for data in [{}, {'client': 'native_v2', 'model': 'gpt-realtime-mini'},
                     {'client': 'native_v2', 'model': 'gemini-3.8-flash'}]:
            request = Request(owner, data, SimpleNamespace(web_session=AsyncMock(), record_voice_event=AsyncMock()))
            request.app['hass'].data['habab_home']['setup_agent'] = SimpleNamespace(settings=lambda: {
                'provider': 'gemini', 'api_key': 'synthetic-master'})
            with patch.object(realtime, 'voice_context', return_value={}), \
                    patch.object(realtime, 'async_get_clientsession') as client:
                response = await realtime.RealtimeSessionView().post(request)
            self.assertEqual(response.status, 400)
            client.assert_not_called()

    async def test_habab_voice_rejects_openai_model_without_relay_fallback(self):
        owner = SimpleNamespace(id='alice', is_active=True, is_admin=True)
        request = Request(owner, {'client': 'native_v2', 'model': 'gpt-realtime-mini'},
                          SimpleNamespace(web_session=AsyncMock(), record_voice_event=AsyncMock()))
        request.app['hass'].data['habab_home']['setup_agent'] = SimpleNamespace(settings=lambda: {
            'provider': 'habab', 'api_key': 'synthetic-selected-habab'})
        with patch.object(realtime, 'voice_context', return_value={'time_zone': 'UTC'}), \
                patch.object(realtime, 'async_get_clientsession') as client:
            response = await realtime.RealtimeSessionView().post(request)
        self.assertEqual(response.status, 400)
        self.assertEqual(json.loads(response.body)['message'], 'invalid_voice_settings')
        client.assert_not_called()
        self.assertNotIn('voice_sessions', request.app['hass'].data['habab_home'])

    async def test_openai_voice_rejects_gemini_model_before_provider_request(self):
        owner = SimpleNamespace(id='alice', is_active=True, is_admin=True)
        assistant = SimpleNamespace(web_session=AsyncMock())
        request = Request(owner, {'model': 'gemini-3.8-live'}, assistant)
        request.app['hass'].data['habab_home']['setup_agent'] = SimpleNamespace(settings=lambda: {
            'provider': 'openai', 'api_key': 'synthetic-selected-openai', 'model': 'gpt-4.1-mini'})
        with patch.object(realtime, 'async_get_clientsession') as client:
            response = await realtime.RealtimeSessionView().post(request)
        self.assertEqual(response.status, 400)
        self.assertEqual(json.loads(response.body)['message'], 'invalid_voice_settings')
        client.assert_not_called()
        assistant.web_session.assert_not_awaited()
        self.assertNotIn('voice_sessions', request.app['hass'].data['habab_home'])

    async def test_gemini_token_failure_never_creates_lease_or_leaks_provider_body(self):
        owner = SimpleNamespace(id='alice', is_active=True, is_admin=True)
        for status, token in [(403, {'error': 'synthetic-master'}), (200, {}), (200, {'name': ''})]:
            request = Request(owner, {'client': 'native_v2'}, SimpleNamespace(
                web_session=AsyncMock(), record_voice_event=AsyncMock(), voice_history_context=AsyncMock(return_value={'session_id': None, 'epoch': 0})))
            request.app['hass'].data['habab_home']['setup_agent'] = SimpleNamespace(settings=lambda: {
                'provider': 'gemini', 'api_key': 'synthetic-master'})
            provider_response = SimpleNamespace(status=status, json=AsyncMock(return_value=token))
            class Context:
                async def __aenter__(self): return provider_response
                async def __aexit__(self, *args): pass
            with patch.object(realtime, 'voice_context', return_value={}), \
                    patch.object(realtime, 'async_get_clientsession', return_value=SimpleNamespace(post=Mock(return_value=Context()))):
                response = await realtime.RealtimeSessionView().post(request)
            self.assertEqual(response.status, 502)
            self.assertNotIn('synthetic-master', response.body.decode())
            self.assertNotIn('voice_sessions', request.app['hass'].data['habab_home'])

    async def test_gemini_bootstrap_locks_setup_and_never_returns_master_key(self):
        owner = SimpleNamespace(id='alice', is_active=True, is_admin=True)
        assistant = SimpleNamespace(web_session=AsyncMock(), record_voice_event=AsyncMock(),
            voice_history_context=AsyncMock(return_value={'session_id': None, 'epoch': 2}))
        request = Request(owner, {'client': 'native_v2'}, assistant)
        request.app['hass'].data['habab_home']['setup_agent'] = SimpleNamespace(settings=lambda: {
            'provider': 'gemini', 'api_key': 'synthetic-master-gemini', 'model': 'gemini-3.8-flash'})
        response = SimpleNamespace(status=200, json=AsyncMock(return_value={'name': 'synthetic-single-use-token'}))
        class Context:
            async def __aenter__(self): return response
            async def __aexit__(self, *args): pass
        client = SimpleNamespace(post=Mock(return_value=Context()))
        with patch.object(realtime, 'voice_context', return_value={'time_zone': 'UTC'}), \
                patch.object(realtime, 'async_get_clientsession', return_value=client):
            result = await realtime.RealtimeSessionView().post(request)
        self.assertEqual(result.status, 200)
        payload = json.loads(result.body)
        self.assertEqual(payload['provider'], 'gemini')
        self.assertEqual(payload['model'], 'gemini-3.8-live')
        self.assertTrue(payload['websocket_url'].endswith('BidiGenerateContentConstrained'))
        args = client.post.call_args
        self.assertEqual(args.args[0], realtime.gemini_live.TOKEN_URL)
        self.assertEqual(args.kwargs['headers'], {'x-goog-api-key': 'synthetic-master-gemini'})
        self.assertFalse(args.kwargs['allow_redirects'])
        self.assertEqual(args.kwargs['json']['uses'], 1)
        self.assertEqual(args.kwargs['json']['bidiGenerateContentSetup'], payload['setup'])
        self.assertNotIn('fieldMask', args.kwargs['json'])
        self.assertNotIn('synthetic-master-gemini', result.body.decode())
        self.assertNotIn('tool_choice', payload['setup'])
        self.assertEqual(payload['setup']['tools'][0]['functionDeclarations'][0]['behavior'], 'NON_BLOCKING')
        assistant.web_session.assert_not_awaited()

    async def test_reset_during_provider_http_revokes_bootstrap(self):
        owner = SimpleNamespace(id='alice', is_active=True, is_admin=True)
        assistant = SimpleNamespace(web_session=AsyncMock(), voice_history_context=AsyncMock(side_effect=[
            {'session_id': None, 'epoch': 0}, {'session_id': None, 'epoch': 1}]))
        request = Request(owner, {'client': 'native_v2'}, assistant)
        request.app['hass'].data['habab_home']['setup_agent'] = SimpleNamespace(settings=lambda: {
            'provider': 'openai', 'api_key': 'synthetic-master'})
        response = SimpleNamespace(status=200, json=AsyncMock(return_value={'value': 'synthetic-ephemeral'}))
        class Context:
            async def __aenter__(self): return response
            async def __aexit__(self, *args): pass
        with patch.object(realtime, 'voice_context', return_value={'time_zone': 'UTC'}), \
                patch.object(realtime, 'async_get_clientsession', return_value=SimpleNamespace(post=Mock(return_value=Context()))):
            result = await realtime.RealtimeSessionView().post(request)
        self.assertEqual(result.status, 403)
        self.assertNotIn('voice_sessions', request.app['hass'].data['habab_home'])

    async def test_revoked_context_never_reads_device_snapshot(self):
        owner = SimpleNamespace(id='alice', is_active=True, is_admin=True)
        assistant = SimpleNamespace(voice_history_context=AsyncMock(return_value={'session_id': 'new', 'epoch': 1}))
        request = Request(owner, {'session_id': 'voice2_test'}, assistant)
        request.app['hass'].data['habab_home']['voice_sessions'] = {
            'voice2_test': {'owner': 'alice', 'expires': time.monotonic() + 60, 'history_epoch': 0}}
        with patch.object(realtime, 'voice_context') as snapshot:
            result = await realtime.RealtimeContextView().post(request)
        self.assertEqual(result.status, 403)
        snapshot.assert_not_called()

    async def test_reset_between_canonical_lookup_and_run_is_guarded_atomically(self):
        owner = SimpleNamespace(id='alice', is_active=True, is_admin=True)
        assistant = SimpleNamespace(voice_history_context=AsyncMock(return_value={'session_id': None, 'epoch': 0}),
            web_session=AsyncMock(return_value={'session_id': 'new-session'}),
            run=AsyncMock(side_effect=AgentError('invalid_session')))
        request = Request(owner, {'session_id': 'voice2_test', 'text': 'Включи свет'}, assistant)
        request.app['hass'].data['habab_home']['voice_sessions'] = {
            'voice2_test': {'owner': 'alice', 'expires': time.monotonic() + 60, 'history_epoch': 0}}
        with patch.object(realtime, 'async_dispatcher_send') as dispatch:
            result = await realtime.RealtimeTurnView().post(request)
        self.assertEqual(result.status, 400)
        assistant.run.assert_awaited_once_with('alice', 'web', 'new-session', 'Включи свет', expected_history_epoch=0)
        dispatch.assert_not_called()

    async def test_native_bootstrap_does_not_start_or_query_opencode(self):
        owner = SimpleNamespace(id='alice', is_active=True, is_admin=True)
        assistant = SimpleNamespace(web_session=AsyncMock(),
            voice_history_context=AsyncMock(return_value={'session_id': None, 'epoch': 2}),
            record_voice_event=AsyncMock())
        request = Request(owner, {'client': 'native_v2'}, assistant)
        request.app['hass'].data['habab_home']['setup_agent'] = SimpleNamespace(settings=lambda: {
            'provider': 'habab', 'api_key': 'synthetic-selected-habab'})
        snapshot = {'time_zone': 'UTC', 'states': []}
        config = realtime.session_config('gemini-3.8-live', client='native_v2', context=snapshot,
                                         selected_provider='habab')
        setup = realtime.gemini_live.live_setup('gemini-3.8-live', config)
        relay_payload = {'provider': 'gemini', 'transport': 'habab', 'model': 'gemini-3.8-live',
                         'value': 'synthetic-relay-credential', 'expires_at': 12345,
                         'websocket_url': 'wss://habab-voice.wpmix.net/gemini/ws', 'setup': setup}
        response = SimpleNamespace(status=200, json=AsyncMock(return_value=relay_payload))
        class Context:
            async def __aenter__(self): return response
            async def __aexit__(self, *args): pass
        client = SimpleNamespace(post=Mock(return_value=Context()))
        with patch.object(realtime, 'voice_context', return_value=snapshot), \
                patch.object(realtime, 'async_get_clientsession', return_value=client):
            result = await realtime.RealtimeSessionView().post(request)
        self.assertEqual(result.status, 200)
        assistant.web_session.assert_not_awaited()
        payload = json.loads(result.body)
        self.assertEqual(payload['client'], 'native_v2')
        self.assertTrue(payload['session_id'].startswith('voice2_'))
        self.assertEqual(payload['voice_context'], snapshot)
        call = client.post.call_args
        self.assertEqual(call.args[0], 'https://habab-voice.wpmix.net/v1/realtime/bootstrap')
        self.assertEqual(call.kwargs['headers']['Authorization'], 'Bearer synthetic-selected-habab')
        self.assertEqual(call.kwargs['json'], {'model': 'gemini-3.8-live', 'setup': setup})
        instructions = setup['systemInstruction']['parts'][0]['text']
        self.assertNotIn('synthetic-selected-habab', instructions)
        self.assertEqual(json.loads(instructions.split('\n')[-1])['runtime_metadata'], {
            'selected_provider': {'id': 'habab', 'name': 'Habab'},
            'selected_model': 'gemini-3.8-live',
        })
        self.assertEqual(payload['provider'], 'gemini')
        self.assertEqual(payload['transport'], 'habab')
        self.assertEqual(payload['model'], 'gemini-3.8-live')
        self.assertEqual(payload['value'], 'synthetic-relay-credential')
        lease = realtime.voice_lease(request.app['hass'], 'alice', payload['session_id'])
        self.assertEqual(lease['owner'], 'alice')
        self.assertEqual(lease['history_epoch'], 2)
        self.assertEqual(lease['provider'], 'gemini')

    async def test_voice_tool_resolves_canonical_chat_only_when_called(self):
        owner = SimpleNamespace(id='alice', is_active=True, is_admin=True)
        assistant = SimpleNamespace(web_session=AsyncMock(return_value={'session_id': 'ses_alice'}),
            voice_history_context=AsyncMock(return_value={'session_id': 'ses_alice', 'epoch': 0}),
            run=AsyncMock(return_value={'session_id': 'ses_alice', 'messages': [{'role': 'assistant', 'text': 'Готово'}]}))
        request = Request(owner, {'session_id': 'voice2_test', 'text': 'Проверь свет'}, assistant)
        request.app['hass'].data['habab_home']['voice_sessions'] = {
            'voice2_test': {'owner': 'alice', 'expires': time.monotonic() + 60, 'history_epoch': 0}}
        with patch.object(realtime, 'async_dispatcher_send'):
            result = await realtime.RealtimeTurnView().post(request)
        self.assertEqual(result.status, 200)
        assistant.web_session.assert_awaited_once_with('alice')
        assistant.run.assert_awaited_once_with('alice', 'web', 'ses_alice', 'Проверь свет', expected_history_epoch=0)

    async def test_foreign_or_expired_voice_lease_never_starts_opencode(self):
        for lease in [{'owner': 'bob', 'expires': time.monotonic() + 60},
                      {'owner': 'alice', 'expires': time.monotonic() - 1}]:
            owner = SimpleNamespace(id='alice', is_active=True, is_admin=True)
            assistant = SimpleNamespace(web_session=AsyncMock(), run=AsyncMock())
            request = Request(owner, {'session_id': 'voice2_test', 'text': 'Проверь свет'}, assistant)
            request.app['hass'].data['habab_home']['voice_sessions'] = {'voice2_test': lease}
            result = await realtime.RealtimeTurnView().post(request)
            self.assertEqual(result.status, 400)
            assistant.web_session.assert_not_awaited()
            assistant.run.assert_not_awaited()

    async def test_context_refresh_is_read_only_and_bound_to_owner(self):
        owner = SimpleNamespace(id='alice', is_active=True, is_admin=True)
        assistant = SimpleNamespace(web_session=AsyncMock(), run=AsyncMock(),
            voice_history_context=AsyncMock(return_value={'session_id': None, 'epoch': 0}))
        request = Request(owner, {'session_id': 'voice2_test'}, assistant)
        request.app['hass'].data['habab_home']['voice_sessions'] = {
            'voice2_test': {'owner': 'alice', 'expires': time.monotonic() + 60, 'history_epoch': 0}}
        with patch.object(realtime, 'voice_context', return_value={'time_zone': 'UTC'}) as snapshot:
            result = await realtime.RealtimeContextView().post(request)
            self.assertEqual(result.status, 200)
            self.assertEqual(result.headers['Cache-Control'], 'no-store')
            request['hass_user'] = SimpleNamespace(id='bob', is_active=True, is_admin=True)
            denied = await realtime.RealtimeContextView().post(request)
            self.assertEqual(denied.status, 403)
            self.assertEqual(snapshot.call_count, 1)
        assistant.web_session.assert_not_awaited()
        assistant.run.assert_not_awaited()

    async def test_transcript_is_journaled_with_server_lease_epoch_without_opencode(self):
        owner = SimpleNamespace(id='alice', is_active=True, is_admin=True)
        recorded = {'recorded': True, 'duplicate': False, 'session_id': None, 'messages': []}
        assistant = SimpleNamespace(record_voice_turn=AsyncMock(return_value=recorded),
                                   web_session=AsyncMock(), run=AsyncMock())
        data = {'session_id': 'voice2_test', 'event_id': 'resp_actual_fixture',
                'user_text': 'Как тебя зовут?', 'assistant_text': 'Я Habab.',
                'history_epoch': 999, 'owner': 'bob'}
        request = Request(owner, data, assistant)
        request.app['hass'].data['habab_home']['voice_sessions'] = {
            'voice2_test': {'owner': 'alice', 'expires': time.monotonic() + 60,
                            'history_session': None, 'history_epoch': 7}}
        with patch.object(realtime, 'async_dispatcher_send') as dispatch:
            result = await realtime.RealtimeTranscriptView().post(request)
        self.assertEqual(result.status, 200)
        assistant.record_voice_turn.assert_awaited_once_with(
            'alice', None, 'resp_actual_fixture', 'Как тебя зовут?', 'Я Habab.', history_epoch=7)
        assistant.web_session.assert_not_awaited(); assistant.run.assert_not_awaited()
        self.assertEqual(dispatch.call_args.args[1], 'habab_voice_history_alice')
        self.assertEqual(result.headers['Cache-Control'], 'no-store')

    async def test_foreign_lease_cannot_append_a_voice_transcript(self):
        owner = SimpleNamespace(id='alice', is_active=True, is_admin=True)
        assistant = SimpleNamespace(record_voice_turn=AsyncMock())
        request = Request(owner, {'session_id': 'voice2_other', 'event_id': 'resp_fixture',
                                 'user_text': 'Вопрос', 'assistant_text': 'Ответ'}, assistant)
        request.app['hass'].data['habab_home']['voice_sessions'] = {
            'voice2_other': {'owner': 'bob', 'expires': time.monotonic() + 60}}
        response = await realtime.RealtimeTranscriptView().post(request)
        self.assertEqual(response.status, 403); assistant.record_voice_turn.assert_not_awaited()

    async def test_selected_provider_controls_both_ephemeral_http_and_native_websocket(self):
        owner = SimpleNamespace(id='alice', is_active=True, is_admin=True)
        for provider, host in [('habab', 'habab-voice.wpmix.net'), ('openai', 'api.openai.com')]:
            with self.subTest(provider=provider):
                assistant = SimpleNamespace(web_session=AsyncMock(return_value={'session_id': 'ses_alice'}),
                    record_voice_event=AsyncMock(),
                    voice_history_context=AsyncMock(return_value={'session_id': None, 'epoch': 0}))
                request = Request(owner, {'client': 'native_v2'} if provider == 'habab' else {}, assistant)
                request.app['hass'].data['habab_home']['setup_agent'] = SimpleNamespace(settings=Mock(return_value={
                    'provider': provider, 'api_key': 'synthetic-selected-' + provider, 'model': 'gpt-4.1-mini'}))
                if provider == 'habab':
                    snapshot = {'time_zone': 'UTC'}
                    config = realtime.session_config('gemini-3.8-live', client='native_v2', context=snapshot,
                                                     selected_provider='habab')
                    setup = realtime.gemini_live.live_setup('gemini-3.8-live', config)
                    remote = {'provider': 'gemini', 'transport': 'habab', 'model': 'gemini-3.8-live',
                              'value': 'synthetic-relay', 'expires_at': 12345,
                              'websocket_url': 'wss://habab-voice.wpmix.net/gemini/ws', 'setup': setup}
                else:
                    remote = {'value': 'synthetic-ephemeral', 'expires_at': 12345}
                response = SimpleNamespace(status=200, json=AsyncMock(return_value=remote))
                class Context:
                    async def __aenter__(self): return response
                    async def __aexit__(self, *args): pass
                client = SimpleNamespace(post=Mock(return_value=Context()))
                with patch.dict(os.environ, {'HABAB_OPENAI_BASE_URL': 'https://wrong.test/v1'}), \
                        patch.object(realtime, 'voice_context', return_value=snapshot if provider == 'habab' else None), \
                        patch.object(realtime, 'async_get_clientsession', return_value=client):
                    result = await realtime.RealtimeSessionView().post(request)
                self.assertEqual(result.status, 200)
                expected_path = '/v1/realtime/bootstrap' if provider == 'habab' else '/v1/realtime/client_secrets'
                self.assertEqual(client.post.call_args.args[0], f'https://{host}{expected_path}')
                self.assertEqual(client.post.call_args.kwargs['headers']['Authorization'], 'Bearer synthetic-selected-' + provider)
                self.assertFalse(client.post.call_args.kwargs['allow_redirects'])
                payload = json.loads(result.body)
                if provider == 'habab':
                    self.assertEqual((payload['provider'], payload['transport'], payload['model']),
                                     ('gemini', 'habab', 'gemini-3.8-live'))
                    self.assertEqual(payload['websocket_url'], remote['websocket_url'])
                    self.assertEqual(client.post.call_args.kwargs['json'], {'model': 'gemini-3.8-live', 'setup': setup})
                else:
                    self.assertEqual(payload['websocket_url'], f'wss://{host}/v1/realtime?model=gpt-realtime-mini')
                    self.assertEqual(payload['provider'], 'openai')
                if provider == 'habab':
                    self.assertTrue(payload['session_id'].startswith('voice2_'))
                else:
                    self.assertEqual(payload['session_id'], 'ses_alice')
                self.assertNotIn('synthetic-selected-', result.body.decode())
                self.assertEqual(result.headers['Cache-Control'], 'no-store')

    async def test_deepseek_selection_does_not_borrow_another_entries_openai_key(self):
        owner = SimpleNamespace(id='alice', is_active=True, is_admin=True)
        assistant = SimpleNamespace(web_session=AsyncMock())
        request = Request(owner, assistant=assistant)
        request.app['hass'].data['habab_home']['setup_agent'] = SimpleNamespace(settings=Mock(return_value={
            'provider': 'deepseek', 'api_key': 'synthetic-deepseek', 'model': 'deepseek-flash'}))
        request.app['hass'].config_entries = SimpleNamespace(async_entries=Mock(return_value=[
            SimpleNamespace(data={'provider': 'openai', 'api_key': 'synthetic-unselected-openai'})]))
        with patch.object(realtime, 'async_get_clientsession') as client:
            result = await realtime.RealtimeSessionView().post(request)
        self.assertEqual(result.status, 400)
        self.assertEqual(json.loads(result.body)['message'], 'realtime_provider_required')
        client.assert_not_called()
        assistant.web_session.assert_not_awaited()

    def test_realtime_config_requires_shared_assistant_tool(self):
        config = realtime.session_config('gpt-realtime-mini')
        self.assertEqual(config['tool_choice'], 'required')
        self.assertEqual(config['tools'][0]['name'], 'ask_home_assistant')
        self.assertEqual(config['audio']['input']['format']['rate'], 24000)
        with self.assertRaises(ValueError):
            realtime.session_config('arbitrary-model')
        native_gemini = realtime.session_config('gemini-3.8-live', client='native_v2')
        self.assertEqual(native_gemini['model'], 'gemini-3.8-live')

    def test_native_v2_can_answer_directly_and_has_explicit_session_end(self):
        snapshot = {'assist_exposure': 'verified', 'states': [],
                    'weather': {'status': 'unavailable', 'entities': []}, 'truncated': False}
        config = realtime.session_config('gpt-realtime-mini', client='native_v2', context=snapshot,
                                         selected_provider='habab')
        self.assertEqual(config['tool_choice'], 'auto')
        tools = {tool['name']: tool for tool in config['tools']}
        self.assertEqual(set(tools), {'ask_home_assistant', 'end_voice_session'})
        self.assertFalse(tools['end_voice_session']['parameters']['additionalProperties'])
        self.assertEqual(tools['end_voice_session']['parameters']['properties'], {})
        self.assertEqual(config['model'], 'gpt-realtime-mini')
        prompt_data = json.loads(config['instructions'].split('\n')[-1])
        self.assertEqual(prompt_data, {
            'runtime_metadata': {
                'selected_provider': {'id': 'habab', 'name': 'Habab'},
                'selected_model': 'gpt-realtime-mini',
            },
            'home_snapshot': snapshot,
        })
        self.assertIn('не является полным перечнем дома', config['instructions'])
        self.assertIn('не доказывает отсутствие устройств', config['instructions'])
        self.assertIn('Не утверждай, что проверил данные, пока инструмент не вернул результат',
                      config['instructions'])
        self.assertIn('не означает, что пользователь подключил Google Gemini напрямую',
                      config['instructions'])
        self.assertNotIn('api_key', config['instructions'])

    def test_native_v2_provider_model_metadata_uses_validated_selection_without_secrets(self):
        for provider, name in [('gemini', 'Google Gemini'), ('openai', 'OpenAI')]:
            with self.subTest(provider=provider):
                config = realtime.session_config(
                    'gemini-3.8-live' if provider == 'gemini' else 'gpt-realtime-mini',
                    client='native_v2', context={'states': []}, selected_provider=provider)
                metadata = json.loads(config['instructions'].split('\n')[-1])['runtime_metadata']
                self.assertEqual(metadata['selected_provider'], {'id': provider, 'name': name})
                self.assertEqual(metadata['selected_model'], config['model'])
                self.assertNotIn('synthetic-secret', config['instructions'])
        with self.assertRaisesRegex(ValueError, 'unsupported_voice_provider'):
            realtime.session_config('gpt-realtime-mini', client='native_v2',
                                    selected_provider='deepseek')

    def test_unknown_home_snapshot_does_not_imply_tool_denial(self):
        context = {'assist_exposure': 'unknown', 'states': [],
                   'weather': {'status': 'unknown', 'entities': []}}
        config = realtime.session_config('gemini-3.8-live', client='native_v2',
                                         context=context, selected_provider='habab')
        self.assertEqual(config['tool_choice'], 'auto')
        self.assertIn('ask_home_assistant', {tool['name'] for tool in config['tools']})
        self.assertIn('это правило действует и при assist_exposure=unknown', config['instructions'])
        self.assertIn('Неизвестный снимок не означает, что инструмент недоступен',
                      config['instructions'])
        self.assertIn('сначала вызови ask_home_assistant', config['instructions'])
        self.assertIn('Не говори, что у тебя нет доступа к домашним данным', config['instructions'])
        prompt_data = json.loads(config['instructions'].split('\n')[-1])
        self.assertEqual(prompt_data['home_snapshot'], context)

    def test_unknown_client_never_silently_falls_back_to_legacy_tools(self):
        for client in ['native_typo', [], {}]:
            with self.subTest(client=client), self.assertRaises(ValueError):
                realtime.session_config('gpt-realtime-mini', client=client)

    async def test_turn_rejects_inactive_or_non_admin_before_chat(self):
        assistant = SimpleNamespace(run=AsyncMock())
        for owner in [None, SimpleNamespace(is_active=False, is_admin=True), SimpleNamespace(is_active=True, is_admin=False)]:
            response = await realtime.RealtimeTurnView().post(Request(owner, assistant=assistant))
            self.assertEqual(response.status, 403)
        assistant.run.assert_not_awaited()

    async def test_invalid_turn_never_reaches_assistant(self):
        assistant = SimpleNamespace(run=AsyncMock())
        owner = SimpleNamespace(id='alice', is_active=True, is_admin=True)
        for data in [{'text': 'test'}, {'session_id': 's', 'text': ' '}, {'session_id': 's', 'text': 'x' * 4001}]:
            response = await realtime.RealtimeTurnView().post(Request(owner, data, assistant))
            self.assertEqual(response.status, 400)
        assistant.run.assert_not_awaited()

    async def test_owner_and_web_session_are_preserved_and_history_dispatched(self):
        result = {'session_id': 'ses_alice', 'messages': [{'role': 'assistant', 'text': 'Real answer'}]}
        assistant = SimpleNamespace(run=AsyncMock(return_value=result))
        owner = SimpleNamespace(id='alice', is_active=True, is_admin=True)
        with patch.object(realtime, 'async_dispatcher_send') as dispatch:
            response = await realtime.RealtimeTurnView().post(Request(owner, {'session_id': 'ses_alice', 'text': 'Check lights'}, assistant))
        assistant.run.assert_awaited_once_with('alice', 'web', 'ses_alice', 'Check lights')
        self.assertEqual(json.loads(response.body)['text'], 'Real answer')
        self.assertEqual(dispatch.call_args.args[1:], ('habab_voice_history_alice', result))

    async def test_rejected_session_does_not_emit_history(self):
        assistant = SimpleNamespace(run=AsyncMock(side_effect=AgentError('invalid_session')))
        owner = SimpleNamespace(id='alice', is_active=True, is_admin=True)
        with patch.object(realtime, 'async_dispatcher_send') as dispatch:
            response = await realtime.RealtimeTurnView().post(Request(owner, {'session_id': 'ses_bob', 'text': 'Check lights'}, assistant))
        self.assertEqual(response.status, 400)
        dispatch.assert_not_called()
