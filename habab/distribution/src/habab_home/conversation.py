"""Model tool calls execute through Home Assistant's exposed Assist API."""

import asyncio
import json
import logging

import probatio

from homeassistant.components import conversation
from homeassistant.const import MATCH_ALL
from homeassistant.helpers import intent, llm
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.translation import async_get_translations

from .const import DOMAIN
from .providers import PROVIDERS, ProviderClient, ProviderError

_LOGGER = logging.getLogger(__name__)
MAX_ROUNDS = 4
MAX_TOOLS = 8
INSTRUCTIONS = """You are a home voice assistant. Reply briefly in the user's language.
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


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities([HababConversationEntity(entry)])


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


class HababConversationEntity(conversation.ConversationEntity):
    """Selectable as the conversation agent of a native Assist pipeline."""

    _attr_supported_features = conversation.ConversationEntityFeature.CONTROL
    _attr_has_entity_name = True

    def __init__(self, entry) -> None:
        self.entry = entry
        self._attr_unique_id = entry.entry_id
        self._attr_name = f"Habab {PROVIDERS[entry.data['provider']]['name']}"
        self._request_lock = asyncio.Lock()

    @property
    def supported_languages(self):
        return MATCH_ALL

    async def _error_text(self, language, key):
        translations = await async_get_translations(self.hass, language, "config", {DOMAIN})
        return translations[f"component.{DOMAIN}.config.error.{key}"]

    async def _async_handle_message(self, user_input, chat_log):
        # Serialise turns for this agent, preventing overlapping device commands.
        if self._request_lock.locked():
            response = intent.IntentResponse(language=user_input.language)
            response.async_set_error(intent.IntentResponseErrorCode.UNKNOWN,
                                     await self._error_text(user_input.language, "assistant_busy"))
            return conversation.ConversationResult(response=response, conversation_id=chat_log.conversation_id)
        async with self._request_lock:
            try:
                async with asyncio.timeout(90):
                    await chat_log.async_provide_llm_data(
                        user_input.as_llm_context(DOMAIN), llm.LLM_API_ASSIST,
                        INSTRUCTIONS, user_input.extra_system_prompt,
                    )
                    await self._complete(chat_log)
            except conversation.ConverseError as err:
                return err.as_conversation_result()
            except (ProviderError, TimeoutError) as err:
                code = err.code if isinstance(err, ProviderError) else "timeout"
                # Safe code only: never log credentials, prompts or upstream bodies.
                _LOGGER.warning("Habab conversation failed: %s", code)
                response = intent.IntentResponse(language=user_input.language)
                response.async_set_error(
                    intent.IntentResponseErrorCode.UNKNOWN,
                    await self._error_text(user_input.language, "assistant_failed") + f" ({code})",
                )
                return conversation.ConversationResult(response=response, conversation_id=chat_log.conversation_id)
        return conversation.async_get_result_from_chat_log(user_input, chat_log)

    async def _complete(self, chat_log) -> None:
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
        for _ in range(MAX_ROUNDS):
            message = await client.complete(format_messages(chat_log.content), tools)
            calls = parse_calls(message, {tool.name for tool in api.tools})
            actions += len(calls)
            if actions > MAX_TOOLS:
                raise ProviderError("too_many_actions")
            if not calls:
                if not message.get("content", "").strip():
                    raise ProviderError("invalid_response")
                chat_log.async_add_assistant_content_without_tools(conversation.AssistantContent(
                    agent_id=self.entity_id, content=message["content"],
                ))
                return
            # HA owns argument validation, entity exposure, intent handling and
            # tool-result traces. No arbitrary service/shell dispatcher exists here.
            async for _result in chat_log.async_add_assistant_content(conversation.AssistantContent(
                agent_id=self.entity_id, content=message.get("content"), tool_calls=calls,
            )):
                pass
        raise ProviderError("too_many_actions")
