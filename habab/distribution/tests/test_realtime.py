"""Voice must use the authenticated owner's canonical chat, never another session."""
import json
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
