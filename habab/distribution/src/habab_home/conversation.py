"""Home Assistant native Assist entity backed by the shared assistant service."""

import asyncio
import logging

from homeassistant.components import conversation
from homeassistant.const import MATCH_ALL
from homeassistant.helpers import intent, llm
from homeassistant.helpers.translation import async_get_translations

from .assist_adapter import INSTRUCTIONS
from .const import DOMAIN
from .providers import PROVIDERS, ProviderError

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities([HababConversationEntity(entry)])


class HababConversationEntity(conversation.ConversationEntity):
    """Selectable native Assist agent using HA's exposed tools only."""

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
        if self._request_lock.locked():
            response = intent.IntentResponse(language=user_input.language)
            response.async_set_error(
                intent.IntentResponseErrorCode.UNKNOWN,
                await self._error_text(user_input.language, "assistant_busy"),
            )
            return conversation.ConversationResult(response=response, conversation_id=chat_log.conversation_id)
        async with self._request_lock:
            try:
                async with asyncio.timeout(90):
                    await chat_log.async_provide_llm_data(
                        user_input.as_llm_context(DOMAIN),
                        llm.LLM_API_ASSIST,
                        INSTRUCTIONS, user_input.extra_system_prompt,
                    )
                    service = self.hass.data[DOMAIN]["assistant"]
                    await service.run_assist(self, chat_log)
            except conversation.ConverseError as err:
                return err.as_conversation_result()
            except (ProviderError, TimeoutError) as err:
                code = err.code if isinstance(err, ProviderError) else "timeout"
                _LOGGER.warning("Habab conversation failed: %s", code)
                response = intent.IntentResponse(language=user_input.language)
                response.async_set_error(
                    intent.IntentResponseErrorCode.UNKNOWN,
                    await self._error_text(user_input.language, "assistant_failed") + f" ({code})",
                )
                return conversation.ConversationResult(response=response, conversation_id=chat_log.conversation_id)
        return conversation.async_get_result_from_chat_log(user_input, chat_log)
