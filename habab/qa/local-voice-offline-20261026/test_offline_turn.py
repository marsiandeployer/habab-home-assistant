"""Focused checks for the native Assist turn summary."""

import unittest

from offline_turn import OfflineTurnError, _REQUIRED_EVENTS, _run_command, _summarize_events


class OfflineTurnSummaryTests(unittest.TestCase):
    def test_valid_turn_requires_selected_agent_and_discards_sensitive_event_data(self):
        secret_marker = "private synthetic transcript and tool arguments"
        events = []
        for index, event_type in enumerate(_REQUIRED_EVENTS):
            data = {"transcript": secret_marker, "tts_input": secret_marker, "tool_args": secret_marker}
            if event_type == "run-start":
                data = {"runner_data": {"stt_binary_handler_id": 17}, "url": secret_marker}
            elif event_type == "intent-start":
                data["engine"] = "conversation.qa_ollama"
            events.append((event_type, data, 100.0 + index / 10))
        events.insert(4, ("intent-progress", {"tool_args": secret_marker, "tool_result": secret_marker}, 100.45))

        result = _summarize_events(events, "conversation.qa_ollama")
        command = _run_command(3, "pipeline-fixture", 22_050)

        self.assertEqual(set(result), set(_REQUIRED_EVENTS))
        self.assertTrue(all(type(value) is int for value in result.values()))
        self.assertNotIn(secret_marker, repr(result))
        self.assertEqual(command["type"], "assist_pipeline/run")
        self.assertEqual(command["input"], {"sample_rate": 22_050})
        self.assertEqual(command["end_stage"], "tts")
        events[3] = ("intent-start", {"engine": "conversation.wrong_agent", "intent_input": secret_marker}, 100.3)
        with self.assertRaises(OfflineTurnError) as raised:
            _summarize_events(events, "conversation.qa_ollama")
        self.assertEqual(raised.exception.code, "conversation_agent_mismatch")
        self.assertNotIn(secret_marker, str(raised.exception))


if __name__ == "__main__":
    unittest.main()
