"""Native Assist provider adapter using Home Assistant's exposed tools."""

import json

import probatio

from homeassistant.components import conversation
from homeassistant.helpers import llm
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .providers import ProviderClient, ProviderError
from .assistant_identity import ASSISTANT_IDENTITY

MAX_ROUNDS = 4
MAX_TOOLS = 8


def format_messages(content) -> list[dict]:
    """Keep complete user turns, including their tool results, in bounded history."""
    user_positions = [i for i, item in enumerate(content) if item.role == "user"]
    selected = content if len(user_positions) <= 6 else [content[0], *content[user_positions[-6]:]]
    messages = []
    for item in selected:
        if item.role in ("system", "user"):
            messages.append({"role": item.role, "content": item.content})
        elif item.role == "assistant":
            message = {"role": "assistant", "content": item.content}
            if item.tool_calls:
                message["tool_calls"] = [{
                    "id": call.id, "type": "function",
                    "function": {"name": call.tool_name, "arguments": json.dumps(call.tool_args)},
                } for call in item.tool_calls]
            messages.append(message)
        elif item.role == "tool_result":
            messages.append({
                "role": "tool", "tool_call_id": item.tool_call_id,
                "content": json.dumps({"data": item.result.data, "error": item.result.error}),
            })
    return messages


def parse_calls(message: dict, allowed: set[str]) -> list[llm.ToolInput]:
    """Reject an entire malformed batch before any action can execute."""
    calls = message.get("tool_calls") or []
    if not isinstance(calls, list) or len(calls) > MAX_TOOLS:
        raise ProviderError("invalid_response")
    result = []
    seen = set()
    try:
        for call in calls:
            function = call["function"]
            name, call_id = function["name"], call["id"]
            if (name not in allowed or not isinstance(call_id, str)
                    or not call_id or len(call_id) > 128 or call_id in seen):
                raise ProviderError("invalid_response")
            args = json.loads(function["arguments"])
            if not isinstance(args, dict):
                raise ProviderError("invalid_response")
            result.append(llm.ToolInput(tool_name=name, tool_args=args, id=call_id))
            seen.add(call_id)
    except (KeyError, TypeError, ValueError):
        raise ProviderError("invalid_response") from None
    return result


INSTRUCTIONS = ASSISTANT_IDENTITY + """You are a home voice assistant. Reply briefly in the user's language.
Use the provided Assist tools to perform all requested device actions.
Only claim an action succeeded when its tool result confirms success.
If a tool reports an error, say so and do not claim the device changed.
Never invent device names or operate devices absent from the exposed tools.
Ask for clarification when the device or requested action is ambiguous.
If the user specifies an unknown room, location, floor, or device, do not
substitute another device, even if only one device is exposed. Do not choose
the closest name or a default target. Ask which existing device they mean.
Preserve every explicit target constraint in the request when calling tools.
"""


class AssistAdapter:
    """Run one native Assist turn without granting OpenCode or root access."""

    def __init__(self, hass, entry):
        self.hass = hass
        self.entry = entry

    async def complete(self, chat_log, agent_id) -> None:
        api = chat_log.llm_api
        tools = [{"type": "function", "function": {
            "name": tool.name,
            "description": tool.description or tool.name,
            "parameters": probatio.to_openapi(
                tool.parameters, custom_serializer=api.custom_serializer,
                openapi_version="3.1.0",
            ),
        }} for tool in api.tools]
        client = ProviderClient(async_get_clientsession(self.hass), self.entry.data)
        actions = 0
        for round_number in range(MAX_ROUNDS + 1):
            # Reserve one text-only completion after the last allowed action.
            # Otherwise successful tool results end in a false limit error.
            available_tools = tools if round_number < MAX_ROUNDS and actions < MAX_TOOLS else []
            message = await client.complete(format_messages(chat_log.content), available_tools)
            calls = parse_calls(message, {
                tool["function"]["name"] for tool in available_tools
            })
            actions += len(calls)
            if actions > MAX_TOOLS:
                raise ProviderError("too_many_actions")
            if not calls:
                if not message.get("content", "").strip():
                    raise ProviderError("invalid_response")
                chat_log.async_add_assistant_content_without_tools(conversation.AssistantContent(
                    agent_id=agent_id, content=message["content"],
                ))
                return
            # HA validates arguments and executes only exposed Assist tools.
            async for _result in chat_log.async_add_assistant_content(conversation.AssistantContent(
                agent_id=agent_id, content=message.get("content"), tool_calls=calls,
            )):
                pass
        raise ProviderError("too_many_actions")
