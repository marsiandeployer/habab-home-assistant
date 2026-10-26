"""Focused tests for the public demo voice bootstrap boundary."""
import json
import unittest
from unittest.mock import patch

from flask import Flask

from habab_demo.routes import HADemoSessionInterface, ha_demo_bp


class FakeControlSocket:
    last_instance = None

    def __init__(self, *args):
        self.sent = b''
        FakeControlSocket.last_instance = self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def settimeout(self, timeout):
        self.timeout = timeout

    def connect(self, path):
        self.path = path

    def sendall(self, payload):
        self.sent = payload

    def recv(self, size):
        return (json.dumps({'ready': True, 'value': 'synthetic-one-use-token', 'expires_at': 123,
                            'websocket_url': 'wss://habab-voice.wpmix.net/openai/demo/ws'}) + '\n').encode()


class DemoVoiceBootstrapTests(unittest.TestCase):
    def setUp(self):
        app = Flask(__name__)
        app.secret_key = 'synthetic-test-secret'
        app.config.update(TESTING=True, OPENAI_API_KEY='synthetic-openai-key',
                          HA_DEMO_REALTIME_CONTROL_SOCKET='/tmp/relay.sock')
        app.session_interface = HADemoSessionInterface()
        app.register_blueprint(ha_demo_bp)
        self.client = app.test_client()

    def test_bootstrap_passes_only_session_binding_and_server_key_over_local_socket(self):
        with patch('habab_demo.routes.socket.socket', FakeControlSocket):
            response = self.client.post('/api/ha-demo/realtime/bootstrap',
                base_url='https://habab-demo.wpmix.net', headers={'Origin': 'https://habab-demo.wpmix.net'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {
            'value': 'synthetic-one-use-token', 'expires_at': 123,
            'websocket_url': 'wss://habab-voice.wpmix.net/openai/demo/ws'})
        connection = FakeControlSocket.last_instance
        payload = json.loads(connection.sent)
        self.assertEqual(payload['purpose'], 'demo_openai_bootstrap')
        self.assertEqual(payload['upstream_key'], 'synthetic-openai-key')
        self.assertRegex(payload['session_hash'], r'^[0-9a-f]{64}$')
        self.assertEqual(connection.path, '/tmp/relay.sock')
        self.assertNotIn('synthetic-openai-key', response.get_data(as_text=True))

    def test_bootstrap_rejects_cross_origin_and_client_supplied_context(self):
        with patch('habab_demo.routes.socket.socket') as socket_factory:
            response = self.client.post('/api/ha-demo/realtime/bootstrap',
                base_url='https://habab-demo.wpmix.net', headers={'Origin': 'https://attacker.invalid'})
            self.assertEqual(response.status_code, 403)
            response = self.client.post('/api/ha-demo/realtime/bootstrap',
                base_url='https://habab-demo.wpmix.net', headers={'Origin': 'https://habab-demo.wpmix.net'},
                json={'instructions': 'untrusted'})
            self.assertEqual(response.status_code, 400)
        socket_factory.assert_not_called()


if __name__ == '__main__':
    unittest.main()
