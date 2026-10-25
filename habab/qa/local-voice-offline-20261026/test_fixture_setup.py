"""Focused safety checks for the offline Assist fixture helper."""

import unittest

from fixture_setup import (
    FixtureSetupError,
    _create_ollama_subentry,
    _match_ws_result,
    _pipeline_payload,
    _resolve_conversation_engine,
    setup_fixture,
)


class FixtureSetupGuardTests(unittest.TestCase):
    def test_ollama_agent_list_resolves_conversation_entity_id_not_entry_uuid(self):
        ollama_entry_id = "62a4f9c7-uuid-only-entry"
        conversation_entity_id = "conversation.habab_local_voice_qa"
        response = {
            "agents": [
                {"id": conversation_entity_id, "name": "Habab local voice QA", "supported_languages": "*"}
            ]
        }

        resolved = _resolve_conversation_engine(response, conversation_entity_id)
        payload = _pipeline_payload(resolved, "stt.whisper", "ru", "tts.piper", "ru", "ru_RU-denis-medium")

        self.assertNotEqual(ollama_entry_id, conversation_entity_id)
        self.assertEqual(resolved, conversation_entity_id)
        self.assertEqual(payload["conversation_engine"], conversation_entity_id)
        self.assertNotEqual(payload["conversation_engine"], ollama_entry_id)
        with self.assertRaises(FixtureSetupError) as raised:
            _resolve_conversation_engine({"agents": []}, conversation_entity_id)
        self.assertEqual(raised.exception.code, "ollama_conversation_agent_unavailable")

    def test_non_qa_targets_are_rejected_before_transport_creation(self):
        calls = []

        def ws_factory(*_args):
            calls.append("ws")
            raise AssertionError("transport must not be constructed")

        for target in ("http://127.0.0.1:18124", "http://127.0.0.1:8395", "https://127.0.0.1:18444"):
            with self.subTest(target=target), self.assertRaises(FixtureSetupError) as raised:
                setup_fixture(target, "ephemeral-token", _ws_factory=ws_factory)
            self.assertEqual(raised.exception.code, "invalid_qa_target")
        self.assertEqual(calls, [])

    def test_missing_downloaded_model_never_submits_subentry_options(self):
        class FakeHttp:
            def __init__(self):
                self.calls = []

            def request(self, method, path, data=None):
                self.calls.append((method, path, data))
                if path == "config/config_entries/subentries/flow":
                    return {"type": "form", "flow_id": "flow-1", "step_id": "set_options", "data_schema": [
                        {"name": "name"},
                        {"name": "model", "selector": {"select": {"options": [{"label": "qwen3:4b-instruct:latest", "value": "qwen3:4b-instruct:latest"}]}}},
                        {"name": "llm_hass_api", "selector": {"select": {"options": [{"label": "Assist", "value": "assist"}]}}},
                    ]}
                if method == "DELETE":
                    return None
                raise AssertionError("unexpected request")

        http = FakeHttp()
        with self.assertRaises(FixtureSetupError) as raised:
            _create_ollama_subentry(http, "ollama-entry-1")
        self.assertEqual(raised.exception.code, "ollama_model_not_downloaded")
        self.assertEqual(http.calls[0][1], "config/config_entries/subentries/flow")
        self.assertEqual(
            [call for call in http.calls if call[1].endswith("/flow-1") and call[0] == "POST"],
            [],
        )

    def test_missing_schema_fields_fail_before_model_submission(self):
        class FakeHttp:
            def __init__(self):
                self.calls = []

            def request(self, method, path, data=None):
                self.calls.append((method, path, data))
                if method == "DELETE":
                    return None
                return {"type": "form", "flow_id": "flow-1", "step_id": "set_options", "data_schema": []}

        http = FakeHttp()
        with self.assertRaises(FixtureSetupError) as raised:
            _create_ollama_subentry(http, "ollama-entry-1")
        self.assertEqual(raised.exception.code, "ollama_subentry_schema_incomplete")
        self.assertFalse(any(call[0] == "POST" and call[1].endswith("/flow-1") for call in http.calls))

    def test_websocket_success_must_match_request_id_and_result_type(self):
        self.assertEqual(_match_ws_result({"id": 7, "type": "result", "success": True, "result": {"ok": True}}, 7), {"ok": True})
        for message in (
            {"id": 8, "type": "result", "success": True, "result": {}},
            {"id": 7, "type": "event", "success": True, "result": {}},
            {"id": 7, "type": "result", "success": False, "result": {}},
        ):
            with self.subTest(message=message), self.assertRaises(FixtureSetupError):
                _match_ws_result(message, 7)


if __name__ == "__main__":
    unittest.main()
