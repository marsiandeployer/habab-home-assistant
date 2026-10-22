"""Native streaming Assist input; Home Assistant supplies end-of-speech VAD."""

import io
import json
import logging
import wave

import aiohttp

from homeassistant.components import stt

from .providers import ProviderError
from .speech import request_audio

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([HababSTT(entry)])


class HababSTT(stt.SpeechToTextEntity):
    def __init__(self, entry):
        self.entry = entry
        self._attr_name = "Habab OpenAI speech recognition"
        self._attr_unique_id = f"{entry.entry_id}_stt"

    @property
    def supported_languages(self):
        return ["ru-RU", "en-US"]

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
        return [stt.AudioSampleRates.SAMPLERATE_16000, stt.AudioSampleRates.SAMPLERATE_48000]

    @property
    def supported_channels(self):
        return [stt.AudioChannels.CHANNEL_MONO]

    async def async_process_audio_stream(self, metadata, stream):
        audio = bytearray()
        max_bytes = 60 * metadata.sample_rate.value * 2
        try:
            async for chunk in stream:
                audio.extend(chunk)
                if len(audio) > max_bytes:
                    raise ProviderError("audio_too_long")
            if not audio:
                raise ProviderError("empty_audio")
            buffer = io.BytesIO()
            with wave.open(buffer, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(metadata.sample_rate.value)
                wav.writeframes(audio)
            form = aiohttp.FormData()
            form.add_field("file", buffer.getvalue(), filename="command.wav", content_type="audio/wav")
            form.add_field("model", "whisper-1")
            form.add_field("language", metadata.language.split("-")[0])
            form.add_field("response_format", "json")
            payload = json.loads(await request_audio(self.hass, self.entry, "transcribe", data=form))
            text = payload.get("text")
            if not isinstance(text, str) or not text.strip():
                raise ProviderError("empty_transcript")
            return stt.SpeechResult(text.strip(), stt.SpeechResultState.SUCCESS)
        except (ProviderError, ValueError, TypeError) as err:
            _LOGGER.warning("Habab STT failed: %s", err.code if isinstance(err, ProviderError) else "invalid_response")
            return stt.SpeechResult(None, stt.SpeechResultState.ERROR)
