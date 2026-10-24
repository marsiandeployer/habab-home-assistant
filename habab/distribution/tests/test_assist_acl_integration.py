"""Exercise the native Assist adapter against Home Assistant's real tool ACL."""

import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from homeassistant.auth import auth_manager_from_config
from homeassistant.components.conversation import (
    ConversationInput,
    DOMAIN as CONVERSATION_DOMAIN,
)
from homeassistant.components.conversation.chat_log import ChatLog
from homeassistant.components.homeassistant import async_setup as async_setup_homeassistant
from homeassistant.components.homeassistant import exposed_entities
from homeassistant.components.homeassistant.const import (
    DATA_EXPOSED_ENTITIES,
    DOMAIN as HOMEASSISTANT_DOMAIN,
)
from homeassistant.components.homeassistant.llm import async_get_tools as homeassistant_tools
from homeassistant.components.intent import OnOffIntentHandler
from homeassistant.components.intent.llm import async_get_tools as intent_tools
from homeassistant.components.llm import AssistAPI, DATA_PLATFORMS
from homeassistant.const import SERVICE_TURN_ON
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import area_registry, device_registry, entity_registry, intent

from homeassistant.components.habab_home.assist_adapter import AssistAdapter
from homeassistant.components.habab_home.const import DOMAIN


class _AssistPlatforms:
    """Expose the installed Core Assist tool providers to the real AssistAPI."""

    async def async_get_platforms(self):
        return {
            "homeassistant": SimpleNamespace(async_get_tools=homeassistant_tools),
            "intent": SimpleNamespace(async_get_tools=intent_tools),
        }


def _tool_call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "content": None,
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }],
    }


class NativeAssistACLIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._config_dir = tempfile.TemporaryDirectory(prefix="habab-assist-acl-")
        self.addCleanup(self._config_dir.cleanup)
        self.hass = HomeAssistant(self._config_dir.name)
        await self.hass.async_start()
        self.addAsyncCleanup(self.hass.async_stop, force=True)

        # Register Core's actual homeassistant.turn_on dispatcher and exposure registry.
        await async_setup_homeassistant(self.hass, {})
        device_registry.async_setup(self.hass)
        await device_registry.async_load(self.hass)
        await area_registry.async_load(self.hass)
        await entity_registry.async_load(self.hass)
        self.assertIn(DATA_EXPOSED_ENTITIES, self.hass.data)

        registry = entity_registry.async_get(self.hass)
        self.allowed = registry.async_get_or_create(
            "light", "assist_acl_fixture", "allowed",
            suggested_object_id="assist_acl_allowed",
            original_name="Allowed fixture lamp",
        )
        self.hidden = registry.async_get_or_create(
            "light", "assist_acl_fixture", "hidden",
            suggested_object_id="assist_acl_hidden",
            original_name="Hidden fixture lamp",
        )
        self.hass.states.async_set(self.allowed.entity_id, "off")
        self.hass.states.async_set(self.hidden.entity_id, "off")

        self.service_calls = []

        async def turn_on(call):
            self.service_calls.append((call.data["entity_id"], call.context.user_id))
            for entity_id in call.data["entity_id"]:
                self.hass.states.async_set(entity_id, "on")

        self.hass.services.async_register("light", SERVICE_TURN_ON, turn_on)
        intent.async_register(
            self.hass,
            OnOffIntentHandler(
                intent.INTENT_TURN_ON,
                HOMEASSISTANT_DOMAIN,
                SERVICE_TURN_ON,
                description="Turn on a fixture light",
            ),
        )
        auth = await auth_manager_from_config(
            self.hass, [{"type": "homeassistant"}], []
        )
        user = await auth.async_create_user("Assist ACL fixture user")
        user_input = ConversationInput(
            text="fixture request",
            context=Context(user_id=user.id),
            conversation_id=None,
            device_id=None,
            satellite_id=None,
            language="en",
            agent_id="conversation.assist_acl_fixture",
        )
        self.llm_context = user_input.as_llm_context(DOMAIN)
        self.assertEqual(self.llm_context.platform, DOMAIN)
        self.assertEqual(self.llm_context.assistant, CONVERSATION_DOMAIN)
        exposed_entities.async_expose_entity(
            self.hass, self.llm_context.assistant, self.allowed.entity_id, True
        )
        exposed_entities.async_expose_entity(
            self.hass, self.llm_context.assistant, self.hidden.entity_id, False
        )
        self.hass.data[DATA_PLATFORMS] = _AssistPlatforms()
        self.api = await AssistAPI(self.hass).async_get_api_instance(self.llm_context)
        self.entry = SimpleNamespace(
            data={"provider": "deepseek", "api_key": "fixture-only"}
        )

    async def _run_adapter(self, chat_log, responses):
        with patch(
            "homeassistant.components.habab_home.assist_adapter.async_get_clientsession"
        ):
            with patch(
                "homeassistant.components.habab_home.assist_adapter.ProviderClient"
            ) as client_type:
                client_type.return_value.complete = AsyncMock(side_effect=responses)
                await AssistAdapter(self.hass, self.entry).complete(
                    chat_log, "conversation.assist_acl_fixture"
                )

    async def test_native_assist_calls_and_reads_only_exposed_fixture_entities(self):
        tool_names = {tool.name for tool in self.api.tools}
        self.assertEqual(
            tool_names,
            {"intent__HassTurnOn", "homeassistant__GetLiveContext"},
        )
        self.assertFalse(
            any(
                "setup_agent" in name.lower() or "opencode" in name.lower()
                for name in tool_names
            )
        )
        self.assertNotIn("Hidden fixture lamp", self.api.api_prompt)
        self.assertIn("Allowed fixture lamp", self.api.api_prompt)

        chat_log = ChatLog(
            hass=self.hass, conversation_id="assist-acl-fixture", llm_api=self.api
        )
        await self._run_adapter(
            chat_log,
            [
                _tool_call(
                    "turn-on-allowed",
                    "intent__HassTurnOn",
                    {"name": "Allowed fixture lamp"},
                ),
                _tool_call("read-after-action", "homeassistant__GetLiveContext", {}),
                {"content": "The allowed fixture lamp is on.", "tool_calls": []},
            ],
        )

        self.assertEqual(self.hass.states.get(self.allowed.entity_id).state, "on")
        self.assertEqual(self.hass.states.get(self.hidden.entity_id).state, "off")
        self.assertEqual(
            self.service_calls,
            [([self.allowed.entity_id], self.llm_context.context.user_id)],
        )
        read_result = next(
            item for item in chat_log.content
            if getattr(item, "tool_name", None) == "homeassistant__GetLiveContext"
        )
        self.assertIn("Allowed fixture lamp", read_result.result.data["result"])
        self.assertIn("state: 'on'", read_result.result.data["result"])
        self.assertNotIn("Hidden fixture lamp", read_result.result.data["result"])

        denied_chat_log = ChatLog(
            hass=self.hass, conversation_id="assist-acl-hidden-fixture", llm_api=self.api
        )
        await self._run_adapter(
            denied_chat_log,
            [
                _tool_call(
                    "turn-on-hidden",
                    "intent__HassTurnOn",
                    {"name": "Hidden fixture lamp"},
                ),
                {"content": "That fixture is not exposed.", "tool_calls": []},
            ],
        )

        denied_result = next(
            item for item in denied_chat_log.content
            if getattr(item, "tool_name", None) == "intent__HassTurnOn"
        )
        self.assertTrue(denied_result.result.error)
        self.assertIn("exposed", denied_result.result.data.get("error_text", ""))
        self.assertEqual(self.hass.states.get(self.hidden.entity_id).state, "off")
        self.assertEqual(len(self.service_calls), 1)


if __name__ == "__main__":
    unittest.main()
