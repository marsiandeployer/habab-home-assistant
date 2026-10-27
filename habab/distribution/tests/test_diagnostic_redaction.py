"""The shared diagnostics redactor covers serialized secrets and quoted values."""

import json
from types import SimpleNamespace
import unittest

from homeassistant.components.habab_home.diagnostics import _redact_text


class DiagnosticRedactionTests(unittest.TestCase):
    def setUp(self):
        self.runtime = SimpleNamespace(clean=lambda text: text)

    def test_serialized_prefixed_and_camel_case_secret_fields_are_redacted(self):
        payload = {"mqtt_password": "fixture first second", "access_token": "fixture access secret",
                   "mqttPassword": "fixture camel secret", "status": "ready", "tokenizer": "keep"}
        cleaned = json.loads(_redact_text(self.runtime, json.dumps(payload)))
        for field in ("mqtt_password", "access_token", "mqttPassword"):
            self.assertEqual(cleaned[field], "[redacted]")
        self.assertEqual(cleaned["status"], "ready")
        self.assertEqual(cleaned["tokenizer"], "keep")

    def test_escaped_serialized_prefixed_secrets_are_redacted(self):
        payload = json.dumps({"output": json.dumps({
            "mqtt_password": "fixture first second", "access_token": "fixture access secret"})})
        cleaned = _redact_text(self.runtime, payload)
        for secret in ("fixture first second", "fixture access secret"):
            self.assertNotIn(secret, cleaned)
        self.assertIn("[redacted]", cleaned)

    def test_quoted_yaml_assignments_hide_the_entire_multiword_value(self):
        cases = (
            'mqtt_password: "fixture first second"',
            "mqtt_password: 'fixture first second'",
            r'mqtt_password: "fixture first \"quoted\" second"',
            "mqtt_password: 'fixture first ''quoted'' second'",
            'ACCESS_TOKEN="fixture first second"',
        )
        for value in cases:
            with self.subTest(value=value):
                cleaned = _redact_text(self.runtime, value + "\nhealthy: ready")
                for word in ("fixture", "first", "quoted", "second"):
                    self.assertNotIn(word, cleaned)
                self.assertIn("healthy: ready", cleaned)

    def test_unquoted_environment_bearer_and_scalar_json_cases_stay_redacted(self):
        value = ('OPENAI_API_KEY=env-fixture-value Authorization: Bearer bearer-fixture-value '
                 '{"mqtt_password":1234567,"access_token":true} status=ready')
        cleaned = _redact_text(self.runtime, value)
        for secret in ("env-fixture-value", "bearer-fixture-value", "1234567"):
            self.assertNotIn(secret, cleaned)
        self.assertIn('"access_token":"[redacted]"', cleaned)
        self.assertIn("status=ready", cleaned)

    def test_incomplete_quoted_values_do_not_expose_remaining_secret_words(self):
        for value in ('mqtt_password: "fixture first second', "mqtt_password: 'fixture first second"):
            with self.subTest(value=value):
                cleaned = _redact_text(self.runtime, value)
                self.assertNotIn("fixture", cleaned)
                self.assertNotIn("first", cleaned)
                self.assertNotIn("second", cleaned)


if __name__ == "__main__":
    unittest.main()
