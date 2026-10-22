"""Native Assist speech output. The voice is AI-generated."""

from homeassistant.components.tts import TextToSpeechEntity
from homeassistant.exceptions import HomeAssistantError

from .providers import ProviderError
from .speech import request_audio


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([HababTTS(entry)])


class HababTTS(TextToSpeechEntity):
    _attr_supported_languages = ["ru-RU", "en-US"]
    _attr_default_language = "ru-RU"

    def __init__(self, entry):
        self.entry = entry
        self._attr_name = "Habab OpenAI voice"
        self._attr_unique_id = f"{entry.entry_id}_tts"

    async def async_get_tts_audio(self, message, language, options):
        if not message or len(message) > 2048:
            raise HomeAssistantError("speech_text_too_long")
        try:
            audio = await request_audio(self.hass, self.entry, "speak", json={
                "model": "tts-1", "voice": "alloy", "input": message,
                "response_format": "mp3",
            })
        except ProviderError as err:
            raise HomeAssistantError(err.code) from None
        return "mp3", audio
