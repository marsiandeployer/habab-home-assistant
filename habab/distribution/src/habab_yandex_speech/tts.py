"""Yandex SpeechKit TTS entity for Russian Assist pipelines."""

import logging

from homeassistant.components.tts import TextToSpeechEntity
from homeassistant.exceptions import HomeAssistantError

from .api import SpeechKitError
from .const import DOMAIN


_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    """Create the configured TTS entity."""
    client = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([YandexTextToSpeech(client, entry.entry_id)])


class YandexTextToSpeech(TextToSpeechEntity):
    """Generate Russian OggOpus audio with SpeechKit API v1."""

    _attr_has_entity_name = True
    _attr_name = "Yandex SpeechKit"
    _attr_supported_languages = ["ru-RU"]
    _attr_default_language = "ru-RU"

    def __init__(self, client, entry_id: str) -> None:
        self._client = client
        self._attr_unique_id = f"{entry_id}_tts"

    async def async_get_tts_audio(self, message, language, options):
        """Return generated audio or a safe Home Assistant error code."""
        try:
            audio = await self._client.synthesize(message, language=language)
        except SpeechKitError as err:
            _LOGGER.warning("Yandex SpeechKit TTS failed: %s", err.code)
            raise HomeAssistantError(err.code) from None
        return "ogg", audio
