"""Yandex SpeechKit STT entity for Russian Assist pipelines."""

import logging

from homeassistant.components import stt

from .api import MAX_AUDIO_BYTES, SpeechKitError
from .const import DOMAIN


_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    """Create the configured STT entity."""
    client = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([YandexSpeechToText(client, entry.entry_id)])


class YandexSpeechToText(stt.SpeechToTextEntity):
    """Use Yandex synchronous recognition for bounded Russian utterances."""

    _attr_has_entity_name = True
    _attr_name = "Yandex SpeechKit"

    def __init__(self, client, entry_id: str) -> None:
        self._client = client
        self._attr_unique_id = f"{entry_id}_stt"

    @property
    def supported_languages(self):
        return ["ru-RU"]

    @property
    def supported_formats(self):
        return [stt.AudioFormats.WAV]

    @property
    def supported_codecs(self):
        return [stt.AudioCodecs.PCM]

    @property
    def supported_bit_rates(self):
        return [stt.AudioBitRates.BITRATE_16]

    @property
    def supported_sample_rates(self):
        return [stt.AudioSampleRates.SAMPLERATE_16000]

    @property
    def supported_channels(self):
        return [stt.AudioChannels.CHANNEL_MONO]

    async def async_process_audio_stream(self, metadata, stream):
        """Buffer within provider limits, drop the WAV header, and transcribe."""
        audio = bytearray()
        try:
            async for chunk in stream:
                audio.extend(chunk)
                if len(audio) > MAX_AUDIO_BYTES:
                    raise SpeechKitError("invalid_audio")
            if not audio:
                raise SpeechKitError("invalid_audio")
            # Assist streams the PCM payload; Yandex API v1 expects raw LPCM.
            transcript = await self._client.recognize(
                bytes(audio), sample_rate=metadata.sample_rate.value,
                language=metadata.language,
            )
            return stt.SpeechResult(transcript, stt.SpeechResultState.SUCCESS)
        except SpeechKitError as err:
            _LOGGER.warning("Yandex SpeechKit STT failed: %s", err.code)
            return stt.SpeechResult(None, stt.SpeechResultState.ERROR)
