import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch

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
        for mode in [None, 1, {}, 'unknown']:
            response = client.post(
                '/api/ha-demo/chat', json={'text': 'hello', 'mode': mode},
                headers={'Origin': 'http://localhost'},
            )
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json, {'error': 'invalid_mode'})
        self.assertEqual(client.post('/api/ha-demo/chat', json={'text': 'hello'}, headers={'Origin': 'https://evil.example'}).status_code, 403)
        self.assertEqual(client.post('/api/ha-demo/chat/reset', headers={'Origin': 'https://evil.example'}).status_code, 403)
        self.store.chat_turn.assert_not_called()

    def test_default_and_compat_mode_use_one_prompt_and_same_provider(self):
        def reply(_session_hash, text, callback):
            answer = callback([{'role': 'user', 'content': text}])
            return [{'role': 'assistant', 'content': answer}]

        self.store.chat_turn.side_effect = reply

        with tempfile.TemporaryDirectory() as directory:
            key_file = Path(directory) / 'provider-key'
            key_file.write_text('test-provider-key')
            self.app.config['HA_DEMO_DEEPSEEK_KEY_FILE'] = str(key_file)

            responses = []
            for content in ('Уточните модель.', 'Уточните точную модель устройства.'):
                response = MagicMock(status_code=200)
                response.iter_content.return_value = [json.dumps({
                    'choices': [{
                        'finish_reason': 'stop',
                        'message': {'content': content},
                    }],
                }).encode()]
                context = MagicMock()
                context.__enter__.return_value = response
                context.__exit__.return_value = False
                responses.append(context)

            with patch('habab_demo.chat.requests.post', side_effect=responses) as provider:
                client = self.app.test_client()
                cases = [
                    ({'text': 'Подскажи настройку.'}, chat.PROMPT),
                    (
                        {'text': 'Подключи устройство.', 'mode': 'onboarding'},
                        chat.PROMPT,
                    ),
                ]
                for body, expected_prompt in cases:
                    result = client.post(
                        '/api/ha-demo/chat', json=body,
                        headers={'Origin': 'http://localhost'},
                    )
                    self.assertEqual(result.status_code, 200)

        self.assertEqual(provider.call_count, 2)
        for call, expected_prompt in zip(
            provider.call_args_list,
            (chat.PROMPT, chat.PROMPT),
        ):
            self.assertEqual(call.args[0], 'https://api.deepseek.com/chat/completions')
            payload = call.kwargs['json']
            self.assertEqual(payload['model'], 'deepseek-flash')
            self.assertEqual(payload['messages'][0], {'role': 'system', 'content': expected_prompt})
            self.assertEqual(set(payload), {'model', 'messages', 'max_tokens', 'thinking'})
        self.assertIn('не больше двух коротких вопросов', chat.PROMPT)
        self.assertIn('не включает Supervisor и магазин add-ons', chat.PROMPT)
        self.assertIn('Не утверждай, что выполнил', chat.PROMPT)
        self.assertNotIn('ONBOARDING_PROMPT', vars(chat))

    def test_upstream_failure_is_an_error_without_body_or_fake_answer(self):
        self.store.chat_turn.side_effect = chat.ChatError('provider_error')
        response = self.app.test_client().post('/api/ha-demo/chat', json={'text': 'hello'}, headers={'Origin': 'http://localhost'})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json, {'error': 'provider_error'})
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
