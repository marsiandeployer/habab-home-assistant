import unittest

from flask import Flask

from habab_demo.routes import ha_demo_bp, HADemoSessionInterface


class MemoryStore:
    def __init__(self):
        self.sessions = {}

    def get_state(self, key, seed):
        self.sessions.setdefault(key, seed.copy())
        return self.sessions[key]

    def update_state(self, key, updates, seed):
        state = self.sessions.setdefault(key, seed.copy())
        state.update(updates)


class HADemoRoutesTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        app = Flask(__name__)
        app.secret_key = 'test-secret'
        app.session_interface = HADemoSessionInterface()
        app.config['HA_DEMO_STORE'] = self.store
        app.register_blueprint(ha_demo_bp)
        self.client = app.test_client()

    def test_get_returns_seeded_demo_entities(self):
        response = self.client.get('/api/ha-demo/state')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['states']['light.living_room_floor_lamp']['state'], 'on')
        self.assertEqual(response.json['states']['climate.living_room']['state'], 'heat')

    def test_post_saves_only_allowlisted_entity_state(self):
        self.client.get('/api/ha-demo/state')
        response = self.client.post(
            '/api/ha-demo/state',
            json={'entities': [{'entity_id': 'light.living_room_floor_lamp', 'state': 'off'}]},
            headers={'Origin': 'http://localhost'},
        )
        self.assertEqual(response.status_code, 200)
        saved = next(iter(self.store.sessions.values()))
        self.assertEqual(saved['light.living_room_floor_lamp']['state'], 'off')

    def test_post_rejects_arbitrary_entities_and_invalid_origin(self):
        body = {'entities': [{'entity_id': 'switch.real_device', 'state': 'on'}]}
        self.assertEqual(self.client.post('/api/ha-demo/state', json=body, headers={'Origin': 'http://localhost'}).status_code, 400)
        self.assertEqual(self.client.post('/api/ha-demo/state', json={'entities': [{'entity_id': 'light.kitchen_worktop', 'state': 'on'}]}, headers={'Origin': 'https://evil.example'}).status_code, 403)

    def test_post_rejects_non_string_entity_and_state_values(self):
        invalid_items = [
            {'entity_id': {'unexpected': 'dict'}, 'state': 'on'},
            {'entity_id': ['light.living_room_floor_lamp'], 'state': 'on'},
            {'entity_id': 'light.living_room_floor_lamp', 'state': {'unexpected': 'dict'}},
            {'entity_id': 'light.living_room_floor_lamp', 'state': ['on']},
        ]
        for item in invalid_items:
            with self.subTest(item=item):
                response = self.client.post(
                    '/api/ha-demo/state', json={'entities': [item]},
                    headers={'Origin': 'http://localhost'},
                )
                self.assertEqual(response.status_code, 400)

    def test_https_public_origin_survives_local_http_proxy_scheme(self):
        app = self.client.application
        client = app.test_client()
        response = client.post(
            '/api/ha-demo/state',
            base_url='http://habab.ru',
            json={'entities': [{'entity_id': 'light.living_room_floor_lamp', 'state': 'off'}]},
            headers={'Origin': 'https://habab.ru'},
        )
        self.assertEqual(response.status_code, 200)

    def test_state_responses_are_not_cached_and_host_is_scoped(self):
        get_response = self.client.get('/api/ha-demo/state')
        post_response = self.client.post(
            '/api/ha-demo/state',
            json={'entities': [{'entity_id': 'light.living_room_floor_lamp', 'state': 'off'}]},
            headers={'Origin': 'http://localhost'},
        )
        self.assertEqual(get_response.headers['Cache-Control'], 'no-store')
        self.assertEqual(post_response.headers['Cache-Control'], 'no-store')

        other_host = self.client.get('/api/ha-demo/state', base_url='http://other-product.example')
        self.assertEqual(other_host.status_code, 404)

    def test_demo_subdomain_uses_partitioned_host_only_cookie_and_persists(self):
        base = 'https://habab-demo.wpmix.net'
        response = self.client.get('/api/ha-demo/state', base_url=base)
        cookie = response.headers['Set-Cookie']
        for attribute in ('__Host-habab_demo=', 'Secure', 'HttpOnly', 'SameSite=None', 'Partitioned', 'Path=/'):
            self.assertIn(attribute, cookie)
        self.assertNotIn('Domain=', cookie)
        saved = self.client.post('/api/ha-demo/state', base_url=base,
            headers={'Origin': base}, json={'entities': [{'entity_id': 'light.living_room_floor_lamp', 'state': 'off'}]})
        self.assertEqual(saved.status_code, 200)
        reloaded = self.client.get('/api/ha-demo/state', base_url=base)
        self.assertEqual(reloaded.json['states']['light.living_room_floor_lamp']['state'], 'off')
        unrelated = self.client.get('/api/ha-demo/state', base_url='https://habab.ru')
        self.assertNotIn('Partitioned', unrelated.headers['Set-Cookie'])
        self.assertNotIn('__Host-habab_demo=', unrelated.headers['Set-Cookie'])


if __name__ == '__main__':
    unittest.main()
