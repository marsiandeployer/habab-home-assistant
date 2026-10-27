"""Chat tool history projects actual persisted OpenCode events, never model prose."""

import copy
import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.components.habab_home.action_outcome import (
    OUTCOME_FORMAT, OUTCOME_PROTOCOL, UNVERIFIED_TODO_TEXT,
)
from homeassistant.components.habab_home.fast_dialog import dialogue_part
from homeassistant.components.habab_home.setup_agent import SetupAgent
from homeassistant.components.habab_home.tool_history import (
    MAX_FIELD_BYTES, MAX_TOOL_CALLS, MAX_TURN_BYTES,
)
from homeassistant.components.habab_home.voice_history import encode_voice_event


def user(identifier="u1"):
    return {"info": {"id": identifier, "role": "user", "format": OUTCOME_FORMAT},
            "parts": [{"type": "text", "text": "Запрос " + identifier}]}


def tool(identifier="call-1", name="bash", status="completed", **state):
    return {"id": "part-" + identifier, "type": "tool", "callID": identifier,
            "tool": name, "state": {"status": status, **state}}


def assistant(identifier="a1", parent="u1", *, parts=None, text=None, final=False):
    info = {"id": identifier, "role": "assistant"}
    if parent is not None:
        info["parentID"] = parent
    if final:
        info["structured"] = {"protocol": OUTCOME_PROTOCOL, "kind": "answer",
                              "text": text or "Ответ", "todo_additions": []}
    elif text:
        parts = [*(parts or []), {"type": "text", "text": text}]
    return {"info": info, "parts": parts or []}


class ToolHistoryTests(unittest.TestCase):
    def setUp(self):
        self.agent = object.__new__(SetupAgent)
        self.agent.password = "runtime-password-fixture"
        self.agent.control_token = "control-token-fixture"
        self.agent._runtime_secrets = {"runtime-private-key-fixture"}
        self.agent.hass = SimpleNamespace(config_entries=SimpleNamespace(
            async_entries=lambda _domain: [SimpleNamespace(data={"api_key": "provider-key-fixture"})]))

    def test_intermediate_calls_attach_once_to_final_without_changing_prose(self):
        history = [user(), assistant(parts=[tool(input={"command": "date"}, output="12:00")],
                                     text="Проверяю"),
                   assistant("a2", parts=[tool("call-2", "read", input={"path": "config.yaml"}, output="ok")]),
                   assistant("final", text="Точный ответ\nс переносом", final=True)]
        projected = self.agent.visible(history)
        self.assertEqual([row["text"] for row in projected], ["Запрос u1", "Точный ответ\nс переносом"])
        calls = projected[-1]["tool_calls"]
        self.assertEqual([call["id"] for call in calls], ["call-1", "call-2"])
        self.assertEqual(calls[0], {"id": "call-1", "name": "bash", "status": "completed",
                                   "input": '{"command":"date"}', "output": "12:00",
                                   "error": "", "truncated": False})
        self.assertEqual(projected, self.agent.visible(copy.deepcopy(history)), "reload uses persisted parts")

    def test_error_without_final_prose_exposes_tool_only_assistant(self):
        history = [user(), assistant(parts=[tool(status="error", input={"command": "false"},
                                                error="exit code 1")])]
        projected = self.agent.visible(history)
        self.assertEqual(projected[-1]["role"], "assistant")
        self.assertEqual(projected[-1]["text"], "")
        self.assertEqual(projected[-1]["tool_calls"][0]["error"], "exit code 1")
        self.assertEqual(projected[-1]["tool_calls"][0]["status"], "error")

    def test_parent_ids_isolate_turns_even_when_user_messages_are_not_in_history(self):
        history = [assistant("a1", "u1", parts=[tool("c1", output="one")]),
                   assistant("a2", "u2", parts=[tool("c2", output="two")]),
                   assistant("f1", "u1", text="Первый", final=True),
                   assistant("f2", "u2", text="Второй", final=True)]
        projected = self.agent.visible(history)
        self.assertEqual(len(projected), 2)
        self.assertEqual([call["id"] for call in projected[0]["tool_calls"]], ["c1"])
        self.assertEqual([call["id"] for call in projected[1]["tool_calls"]], ["c2"])

    def test_no_parent_id_does_not_merge_unrelated_legacy_messages(self):
        history = [assistant("a1", None, parts=[tool("c1", output="one")], text="Первый"),
                   assistant("a2", None, parts=[tool("c2", output="two")], text="Второй")]
        projected = self.agent.visible(history)
        self.assertEqual([row["text"] for row in projected], ["Первый", "Второй"])
        self.assertEqual([row["tool_calls"][0]["id"] for row in projected], ["c1", "c2"])

    def test_same_native_call_updates_state_and_preserves_recorded_input(self):
        history = [assistant("a1", parts=[tool("c1", status="running", input={"command": "date"})]),
                   assistant("a2", parts=[tool("c1", output="12:00")]),
                   assistant("f1", final=True)]
        calls = self.agent.visible(history)[-1]["tool_calls"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["status"], "completed")
        self.assertEqual(calls[0]["input"], '{"command":"date"}')
        self.assertEqual(calls[0]["output"], "12:00")

    def test_structured_output_and_malformed_or_synthetic_parts_are_not_calls(self):
        malformed = [tool("format", "StructuredOutput", output="hidden format"),
                     {"type": "tool", "state": {"output": "no identity"}},
                     {**tool("bad"), "callID": "has spaces", "id": "also invalid"},
                     {**tool("synthetic"), "synthetic": True},
                     {"type": "reasoning", "text": "hidden"}]
        projected = self.agent.visible([assistant(parts=malformed, text="Ответ", final=True)])
        self.assertEqual(projected, [{"role": "assistant", "text": "Ответ"}])

    def test_known_secrets_and_nested_credentials_use_existing_diagnostics_redaction(self):
        raw = tool(input={"api_key": "nested-key-fixture", "request": {
            "password": "nested-password-fixture", "safe": "hello"}},
            output="provider-key-fixture runtime-password-fixture control-token-fixture "
                   "runtime-private-key-fixture Authorization: Bearer bearer-secret-fixture "
                   "https://example.test/?token=url-secret-fixture",
            error="Cookie: session=cookie-secret-fixture")
        calls = self.agent.visible([assistant(parts=[raw], final=True)])[-1]["tool_calls"]
        serialized = json.dumps(calls)
        for secret in ("nested-key-fixture", "nested-password-fixture", "provider-key-fixture",
                       "runtime-password-fixture", "control-token-fixture", "runtime-private-key-fixture",
                       "bearer-secret-fixture", "url-secret-fixture", "cookie-secret-fixture"):
            self.assertNotIn(secret, serialized)
        self.assertIn("hello", calls[0]["input"])
        self.assertIn("[redacted", serialized)
        self.assertIsInstance(calls[0]["input"], str)

    def test_field_call_count_and_total_json_bytes_are_bounded_with_truncation(self):
        parts = [tool("c-" + str(index), input={"command": "данные " * 2000},
                      output="строка\n" * 3000, error="ошибка " * 2000)
                 for index in range(40)]
        calls = self.agent.visible([assistant(parts=parts, final=True)])[-1]["tool_calls"]
        self.assertLessEqual(len(calls), MAX_TOOL_CALLS)
        encoded = json.dumps(calls, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.assertLessEqual(len(encoded), MAX_TURN_BYTES)
        self.assertTrue(calls[-1]["truncated"])
        for call in calls:
            for field in ("input", "output", "error"):
                self.assertLessEqual(len(call[field].encode("utf-8")), MAX_FIELD_BYTES)

    def test_serialized_json_and_quoted_yaml_secrets_are_redacted_in_actual_tool_output(self):
        outputs = [json.dumps({"mqtt_password": "fixture first second",
                               "access_token": "fixture access secret", "status": "ready"}),
                   'mqtt_password: "fixture first second"\nstatus: ready']
        for output in outputs:
            with self.subTest(output=output):
                calls = self.agent.visible([assistant(parts=[tool(output=output)],
                                                      final=True)])[-1]["tool_calls"]
                projected = calls[0]["output"]
                for secret in ("fixture", "first", "second", "access secret"):
                    self.assertNotIn(secret, projected)
                self.assertIn("ready", projected)
                self.assertIn("[redacted]", projected)

    def test_many_small_calls_report_count_truncation_on_actual_last_call(self):
        calls = self.agent.visible([assistant(parts=[tool("c-" + str(i)) for i in range(30)],
                                              final=True)])[-1]["tool_calls"]
        self.assertEqual(len(calls), MAX_TOOL_CALLS)
        self.assertEqual(calls[-1]["id"], "c-23")
        self.assertTrue(calls[-1]["truncated"])

    def test_voice_and_direct_synthetic_pairs_never_fabricate_tool_events(self):
        voice, _digest, metadata = encode_voice_event("event-1", "assistant", "Голосовой ответ")
        history = [{"info": {"role": "user"}, "parts": [dialogue_part("Вопрос", "Ответ")]},
                   {"info": {"role": "user"}, "parts": [{"type": "text", "synthetic": True,
                       "text": voice, "metadata": metadata}]}]
        projected = self.agent.visible(history)
        self.assertEqual([row["text"] for row in projected], ["Вопрос", "Ответ", "Голосовой ответ"])
        self.assertFalse(any("tool_calls" in row for row in projected))

    def test_invalid_state_status_is_explicit_unknown_and_part_id_can_identify_call(self):
        part = tool("c1", status={"unexpected": True}, output="plain <b>text</b>")
        part.pop("callID")
        calls = self.agent.visible([assistant(parts=[part], final=True)])[-1]["tool_calls"]
        self.assertEqual(calls[0]["id"], "part-c1")
        self.assertEqual(calls[0]["status"], "unknown")
        self.assertEqual(calls[0]["output"], "plain <b>text</b>")


class ToolHistoryRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_search_and_unverified_todo_retain_actual_tools_with_safe_prose(self):
        for failure in ("search", "todo"):
            with self.subTest(failure=failure):
                fixture = ToolHistoryTests()
                fixture.setUp()
                agent = fixture.agent
                agent.lock = asyncio.Lock()
                agent._aborting = False
                agent.active = None
                agent.ensure = AsyncMock()
                agent.settings = lambda: {"provider": "deepseek", "model": "fixture-model"}
                agent.external_search_status = {}
                agent.diagnostics = SimpleNamespace(record=Mock(), clear=Mock())
                final = assistant("final", text="Непроверенное утверждение", final=True)
                if failure == "search":
                    part = tool(name="websearch", status="error", error="Search timed out")
                else:
                    part = tool(name="habab-ha", status="error", error="Write not verified")
                    final["info"]["structured"].update(kind="action_result", todo_additions=[{
                        "entity_id": "todo.shopping_list", "item": "хлеб"}])
                history = [user(), assistant("middle", parts=[part]), final]
                agent.request = AsyncMock(side_effect=[[], final, history])
                with patch("homeassistant.components.habab_home.setup_agent.get_assistant_status",
                           AsyncMock(return_value={})):
                    result = await agent.run("alice", "session", "Проверить")
                row = result["messages"][-1]
                self.assertNotIn("Непроверенное утверждение", row["text"])
                if failure == "todo":
                    self.assertEqual(row["text"], UNVERIFIED_TODO_TEXT)
                else:
                    self.assertIn("Веб-поиск завершился ошибкой", row["text"])
                self.assertEqual(row["tool_calls"][0]["id"], "call-1")
                self.assertEqual(row["tool_calls"][0]["status"], "error")
                self.assertEqual(row["tool_calls"][0]["error"], part["state"]["error"])


if __name__ == "__main__":
    unittest.main()
