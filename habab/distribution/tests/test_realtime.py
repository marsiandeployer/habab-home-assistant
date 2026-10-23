"""Voice must use the authenticated owner's canonical chat, never another session."""
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.components.habab_home import realtime
from homeassistant.components.habab_home.setup_agent import AgentError


class Request(dict):
    def __init__(self, owner=None, data=None, assistant=None):
        self.app = {'hass': SimpleNamespace(data={'habab_home': {'assistant': assistant}})}
        super().__init__(hass_user=owner)
        self.json = AsyncMock(return_value=data or {})


class RealtimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_provider_controls_both_ephemeral_http_and_native_websocket(self):
        owner = SimpleNamespace(id='alice', is_active=True, is_admin=True)
        for provider, host in [('habab', 'habab-voice.wpmix.net'), ('openai', 'api.openai.com')]:
            with self.subTest(provider=provider):
                assistant = SimpleNamespace(web_session=AsyncMock(return_value={'session_id': 'ses_alice'}))
                request = Request(owner, assistant=assistant)
                request.app['hass'].data['habab_home']['setup_agent'] = SimpleNamespace(settings=Mock(return_value={
                    'provider': provider, 'api_key': 'synthetic-selected-' + provider, 'model': 'gpt-4.1-mini'}))
                response = SimpleNamespace(status=200, json=AsyncMock(return_value={'value': 'synthetic-ephemeral', 'expires_at': 12345}))
                class Context:
                    async def __aenter__(self): return response
                    async def __aexit__(self, *args): pass
                client = SimpleNamespace(post=Mock(return_value=Context()))
                with patch.dict(os.environ, {'HABAB_OPENAI_BASE_URL': 'https://wrong.test/v1'}), \
                        patch.object(realtime, 'async_get_clientsession', return_value=client):
                    result = await realtime.RealtimeSessionView().post(request)
                self.assertEqual(result.status, 200)
                self.assertEqual(client.post.call_args.args[0], f'https://{host}/v1/realtime/client_secrets')
                self.assertEqual(client.post.call_args.kwargs['headers']['Authorization'], 'Bearer synthetic-selected-' + provider)
                self.assertFalse(client.post.call_args.kwargs['allow_redirects'])
                payload = json.loads(result.body)
                self.assertEqual(payload['websocket_url'], f'wss://{host}/v1/realtime?model=gpt-realtime-mini')
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
