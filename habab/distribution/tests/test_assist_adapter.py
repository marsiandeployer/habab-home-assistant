"""Native Assist keeps bounded HA tool history in its provider adapter."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.components.habab_home.assist_adapter import AssistAdapter, format_messages
from homeassistant.components.habab_home.providers import ProviderError


class AssistAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def _run_tool_rounds(self, responses):
        tool = SimpleNamespace(name="HassTurnOn", description="Fixture only", parameters={})
        chat_log = SimpleNamespace(
            llm_api=SimpleNamespace(tools=[tool], custom_serializer=None),
            content=[SimpleNamespace(role="system", content="Fixture instructions")],
            async_add_assistant_content_without_tools=Mock(),
        )
        executed = []

        async def add_content(content):
            chat_log.content.append(SimpleNamespace(
                role="assistant", content=content.content, tool_calls=content.tool_calls,
            ))
            for call in content.tool_calls:
                executed.append(call.id)
                chat_log.content.append(SimpleNamespace(
                    role="tool_result", tool_call_id=call.id,
                    result=SimpleNamespace(data={"success": True}, error=None),
                ))
                yield None

        chat_log.async_add_assistant_content = add_content
        entry = SimpleNamespace(data={"provider": "deepseek", "api_key": "fixture-only"})
        with patch("homeassistant.components.habab_home.assist_adapter.async_get_clientsession"):
            with patch("homeassistant.components.habab_home.assist_adapter.ProviderClient") as client_type:
                complete = client_type.return_value.complete = AsyncMock(side_effect=responses)
                try:
                    await AssistAdapter(SimpleNamespace(), entry).complete(chat_log, "conversation.fixture")
                except ProviderError as error:
                    return chat_log, executed, complete, error
        return chat_log, executed, complete, None

    @staticmethod
    def _calls(round_number, count=1):
        return {"content": None, "tool_calls": [{
            "id": f"fixture-{round_number}-{index}", "type": "function",
            "function": {"name": "HassTurnOn", "arguments": json.dumps({"name": "Fixture"})},
        } for index in range(count)]}

    async def test_four_successful_tool_rounds_receive_a_final_reply_without_tools(self):
        log, executed, complete, error = await self._run_tool_rounds([
            *[self._calls(index) for index in range(4)],
            {"content": "Fixture actions completed.", "tool_calls": []},
        ])
        self.assertIsNone(error)
        self.assertEqual(len(executed), 4)
        self.assertEqual(complete.await_count, 5)
        self.assertEqual(complete.await_args.args[1], [])
        self.assertEqual(sum(item["role"] == "tool" for item in complete.await_args.args[0]), 4)
        log.async_add_assistant_content_without_tools.assert_called_once()

    async def test_final_reply_cannot_execute_a_fifth_tool_round(self):
        log, executed, complete, error = await self._run_tool_rounds([
            *[self._calls(index) for index in range(5)],
        ])
        self.assertIsInstance(error, ProviderError)
        self.assertEqual(len(executed), 4)
        self.assertEqual(complete.await_args.args[1], [])
        log.async_add_assistant_content_without_tools.assert_not_called()

    async def test_eight_successful_actions_receive_a_final_reply_without_tools(self):
        log, executed, complete, error = await self._run_tool_rounds([
            self._calls(0, count=8),
            {"content": "Eight fixture actions completed.", "tool_calls": []},
        ])
        self.assertIsNone(error)
        self.assertEqual(len(executed), 8)
        self.assertEqual(complete.await_args.args[1], [])
        log.async_add_assistant_content_without_tools.assert_called_once()

    async def test_ninth_action_is_rejected_before_the_batch_executes(self):
        log, executed, _complete, error = await self._run_tool_rounds([
            self._calls(0, count=7), self._calls(1, count=2),
        ])
        self.assertIsInstance(error, ProviderError)
        self.assertEqual(str(error), "too_many_actions")
        self.assertEqual(len(executed), 7)
        log.async_add_assistant_content_without_tools.assert_not_called()

    async def test_empty_final_reply_keeps_results_without_inventing_success(self):
        log, executed, complete, error = await self._run_tool_rounds([
            *[self._calls(index) for index in range(4)],
            {"content": "", "tool_calls": []},
        ])
        self.assertEqual(str(error), "invalid_response")
        self.assertEqual(len(executed), 4)
        self.assertEqual(complete.await_count, 5)
        log.async_add_assistant_content_without_tools.assert_not_called()

    async def test_provider_failure_in_final_reply_does_not_repeat_actions(self):
        log, executed, complete, error = await self._run_tool_rounds([
            *[self._calls(index) for index in range(4)], ProviderError("timeout"),
        ])
        self.assertEqual(str(error), "timeout")
        self.assertEqual(len(executed), 4)
        self.assertEqual(complete.await_count, 5)
        log.async_add_assistant_content_without_tools.assert_not_called()

    def test_format_messages_keeps_complete_recent_turns_with_tool_results(self):
        content = [SimpleNamespace(role="system", content="instructions")]
        for index in range(7):
            content.append(SimpleNamespace(role="user", content=f"user-{index}"))
            content.append(SimpleNamespace(
                role="assistant", content=None,
                tool_calls=[SimpleNamespace(id=f"call-{index}", tool_name="HassTurnOn", tool_args={"n": index})],
            ))
            content.append(SimpleNamespace(
                role="tool_result", tool_call_id=f"call-{index}",
                result=SimpleNamespace(data={"ok": index}, error=None),
            ))
        messages = format_messages(content)
        self.assertEqual(messages[0], {"role": "system", "content": "instructions"})
        self.assertEqual([m["content"] for m in messages if m["role"] == "user"],
                         [f"user-{index}" for index in range(1, 7)])
        self.assertEqual(sum(m["role"] == "tool" for m in messages), 6)
        self.assertEqual(sum(bool(m.get("tool_calls")) for m in messages), 6)

    async def test_complete_rejects_empty_model_reply_without_inserting_assistant_text(self):
        api = SimpleNamespace(tools=[], custom_serializer=None)
        chat_log = SimpleNamespace(
            llm_api=api, content=[],
            async_add_assistant_content_without_tools=Mock(),
            async_add_assistant_content=AsyncMock(),
        )
        entry = SimpleNamespace(data={"provider": "deepseek", "api_key": "private"})
        with patch("homeassistant.components.habab_home.assist_adapter.async_get_clientsession"):
            with patch("homeassistant.components.habab_home.assist_adapter.ProviderClient") as client_type:
                client_type.return_value.complete = AsyncMock(return_value={"content": "", "tool_calls": []})
                with self.assertRaisesRegex(ProviderError, "^invalid_response$"):
                    await AssistAdapter(SimpleNamespace(), entry).complete(chat_log, "conversation.test")
        chat_log.async_add_assistant_content_without_tools.assert_not_called()
        chat_log.async_add_assistant_content.assert_not_awaited()

    async def test_complete_uses_only_the_entry_provider_and_exposed_tool_schema(self):
        tool = SimpleNamespace(
            name="HassTurnOn", description="Turn on exposed devices", parameters={"type": "object"},
        )
        api = SimpleNamespace(tools=[tool], custom_serializer=None)
        chat_log = SimpleNamespace(
            llm_api=api, content=[],
            async_add_assistant_content_without_tools=Mock(),
            async_add_assistant_content=AsyncMock(return_value=None),
        )
        entry = SimpleNamespace(data={"provider": "deepseek", "api_key": "private"})
        message = {"content": "Done", "tool_calls": []}
        with patch("homeassistant.components.habab_home.assist_adapter.async_get_clientsession"):
            with patch("homeassistant.components.habab_home.assist_adapter.ProviderClient") as client_type:
                client_type.return_value.complete = AsyncMock(return_value=message)
                await AssistAdapter(SimpleNamespace(), entry).complete(chat_log, "conversation.test")
        tools = client_type.return_value.complete.await_args.args[1]
        self.assertEqual([item["function"]["name"] for item in tools], ["HassTurnOn"])
        chat_log.async_add_assistant_content_without_tools.assert_called_once()


if __name__ == "__main__":
    unittest.main()
