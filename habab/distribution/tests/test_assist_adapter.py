"""Native Assist keeps bounded HA tool history in its provider adapter."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.components.habab_home.assist_adapter import AssistAdapter, format_messages
from homeassistant.components.habab_home.providers import ProviderError


class AssistAdapterTests(unittest.IsolatedAsyncioTestCase):
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
