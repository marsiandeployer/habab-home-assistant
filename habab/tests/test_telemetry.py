"""Reject sensitive/unbounded telemetry before any persistent write."""
from copy import deepcopy
import unittest
from unittest.mock import patch
from flask import Flask
from habab_demo import telemetry


class TelemetryContractTests(unittest.TestCase):
    def setUp(self):
        self.body = {'schema': 1, 'installation_id': 'a'*32, 'kind': 'analytics',
            'preferences': {'base': True, 'usage': True, 'statistics': True, 'diagnostics': False},
            'data': {'base': {'ha_version': '2026.10.2', 'habab_version': '0.1.0',
                'installation_type': 'Home Assistant Container', 'arch': 'x86_64', 'python_version': '3.14.2'},
                'usage': {'integrations': ['light'], 'device_models': [{'manufacturer': 'Acme', 'model': 'Lamp', 'count': 1}]},
                'statistics': {'devices': 1, 'entities': 2, 'automations': 0, 'users': 1}}}
        app = Flask(__name__)
        app.secret_key = 'fixture-only-secret'
        app.config['HA_DEMO_DATABASE_DSN'] = 'fixture-dsn'
        app.register_blueprint(telemetry.telemetry_bp)
        self.client = app.test_client()

    def test_acceptance_passes_only_hmac_identifier_to_storage(self):
        with patch.object(telemetry, 'ingest') as ingest:
            response = self.client.post('/api/ha-telemetry', json=self.body)
        self.assertEqual(response.status_code, 202)
        args = ingest.call_args.args
        self.assertRegex(args[1], r'^[a-f0-9]{64}$')
        self.assertNotEqual(args[1], self.body['installation_id'])
        self.assertEqual(args[2], self.body)

    def test_sensitive_fields_wrong_consent_and_counts_rejected_before_storage(self):
        bodies = []
        for path in [('data', 'api_key'), ('data', 'usage', 'device_names'), ('data', 'base', 'hostname')]:
            body = deepcopy(self.body)
            obj = body
            for key in path[:-1]: obj = obj[key]
            obj[path[-1]] = 'fixture-private'
            bodies.append(body)
        body = deepcopy(self.body); body['preferences']['base'] = False; bodies.append(body)
        body = deepcopy(self.body); body['data']['statistics']['users'] = True; bodies.append(body)
        body = deepcopy(self.body); body['data']['statistics']['devices'] = 1_000_001; bodies.append(body)
        with patch.object(telemetry, 'ingest') as ingest:
            for body in bodies:
                with self.subTest(body=body):
                    self.assertEqual(self.client.post('/api/ha-telemetry', json=body).status_code, 400)
            ingest.assert_not_called()

    def test_browser_origin_and_oversized_payload_rejected_before_storage(self):
        with patch.object(telemetry, 'ingest') as ingest:
            self.assertEqual(self.client.post('/api/ha-telemetry', json=self.body,
                headers={'Origin': 'https://elsewhere.example'}).status_code, 403)
            self.assertEqual(self.client.post('/api/ha-telemetry', data=b' '*32769,
                content_type='application/json').status_code, 413)
            ingest.assert_not_called()

    def test_diagnostic_rejects_message_full_path_and_missing_consent(self):
        body = deepcopy(self.body)
        body.update(kind='diagnostic', data={'error_type': 'ValueError', 'component': 'light',
            'frames': [{'file': 'light.py', 'function': 'setup', 'line': 24}]})
        body['preferences']['diagnostics'] = True
        telemetry.validate_payload(body)
        for key, value in [('message', 'fixture-secret')]:
            changed = deepcopy(body); changed['data'][key] = value
            with self.assertRaises(telemetry.InvalidTelemetry): telemetry.validate_payload(changed)
        changed = deepcopy(body); changed['data']['frames'][0]['file'] = '/private/light.py'
        with self.assertRaises(telemetry.InvalidTelemetry): telemetry.validate_payload(changed)
        body['preferences']['diagnostics'] = False
        with self.assertRaises(telemetry.InvalidTelemetry): telemetry.validate_payload(body)

    def test_withdraw_requires_all_off_and_backend_errors_are_bounded(self):
        body = deepcopy(self.body); body.update(kind='withdraw', data={})
        with self.assertRaises(telemetry.InvalidTelemetry): telemetry.validate_payload(body)
        body['preferences'] = dict.fromkeys(telemetry.FLAGS, False)
        telemetry.validate_payload(body)
        for error, status in [(telemetry.TelemetryRateLimit(), 429), (RuntimeError('fixture-secret'), 503)]:
            with patch.object(telemetry, 'ingest', side_effect=error):
                response = self.client.post('/api/ha-telemetry', json=body)
            self.assertEqual(response.status_code, status)
            self.assertNotIn('fixture-secret', response.text)
