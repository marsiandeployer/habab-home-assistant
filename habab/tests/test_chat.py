import unittest
from unittest.mock import Mock, patch

from flask import Flask

from habab_demo import chat
from habab_demo.routes import ha_demo_bp, HADemoSessionInterface


class ChatRoutesTests(unittest.TestCase):
    def setUp(self):
        self.store = Mock()
        self.store.chat_history.return_value = []
        self.store.chat_turn.return_value = [{'role': 'assistant', 'content': 'Проверьте модель устройства.'}]
        self.app = Flask(__name__)
        self.app.secret_key = 'test-only-secret'
        self.app.session_interface = HADemoSessionInterface()
        self.app.config['HA_DEMO_STORE'] = self.store
        self.app.register_blueprint(ha_demo_bp)

    def test_separate_cookies_cannot_choose_another_chat_id(self):
        one, two = self.app.test_client(), self.app.test_client()
        one.get('/api/ha-demo/chat')
        first = self.store.chat_history.call_args.args[0]
        two.get('/api/ha-demo/chat?session_id=' + first)
        second = self.store.chat_history.call_args.args[0]
        self.assertNotEqual(first, second)
        one.get('/api/ha-demo/chat')
        self.assertEqual(first, self.store.chat_history.call_args.args[0])

    def test_origin_size_and_text_are_checked_before_provider(self):
        client = self.app.test_client()
        for text in ['', 'x' * 4001, {'bad': 'object'}]:
            self.assertEqual(client.post('/api/ha-demo/chat', json={'text': text}, headers={'Origin': 'http://localhost'}).status_code, 400)
        self.assertEqual(client.post('/api/ha-demo/chat', json={'text': 'hello'}, headers={'Origin': 'https://evil.example'}).status_code, 403)
        self.assertEqual(client.post('/api/ha-demo/chat/reset', headers={'Origin': 'https://evil.example'}).status_code, 403)
        self.store.chat_turn.assert_not_called()

    def test_upstream_failure_is_an_error_without_body_or_fake_answer(self):
        self.store.chat_turn.side_effect = chat.ChatError('provider_error')
        response = self.app.test_client().post('/api/ha-demo/chat', json={'text': 'hello'}, headers={'Origin': 'http://localhost'})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json, {'error': 'provider_error'})
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
