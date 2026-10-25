"""Yandex SpeechKit integration uses bounded, explicit API v1 requests."""

from types import SimpleNamespace
from urllib.parse import parse_qs
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.components.habab_yandex_speech import (
    async_setup_entry,
    async_unload_entry,
)
from homeassistant.components.habab_yandex_speech import api, config_flow
from homeassistant.components.habab_yandex_speech.api import (
    SpeechKitClient,
    SpeechKitError,
)
from homeassistant.components.habab_yandex_speech.stt import YandexSpeechToText
from homeassistant.components.habab_yandex_speech.tts import YandexTextToSpeech
from homeassistant import loader
from homeassistant.config_entries import ConfigEntries, SOURCE_USER
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError


class Content:
    def __init__(self, chunks):
        self.chunks = chunks

    async def iter_chunked(self, _size):
        for chunk in self.chunks:
            yield chunk


class Response:
    def __init__(self, status=200, chunks=(b"fixture",)):
        self.status = status
        self.content = Content(chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class SpeechKitTests(unittest.IsolatedAsyncioTestCase):
    async def test_stt_posts_raw_lpcm_with_key_header_and_no_key_in_url(self):
        session = Session([Response(chunks=('{"result":"включи свет"}'.encode(),))])
        client = SpeechKitClient(object(), "A" * 32)
        with patch.object(api, "async_get_clientsession", return_value=session):
            result = await client.recognize(
                b"raw-pcm", sample_rate=16000, language="ru-RU",
            )
        self.assertEqual(result, "включи свет")
        method, url, kwargs = session.calls[0]
        self.assertEqual((method, url), ("POST", api.STT_URL))
        self.assertEqual(kwargs["headers"], {"Authorization": "Api-Key " + "A" * 32})
        self.assertEqual(kwargs["params"], {
            "format": "lpcm", "sampleRateHertz": "16000", "lang": "ru-RU",
        })
        self.assertEqual(kwargs["data"], b"raw-pcm")
        self.assertNotIn("A" * 32, url)
        self.assertFalse(kwargs["allow_redirects"])

    async def test_tts_posts_text_as_form_and_returns_oggopus(self):
        session = Session([Response(chunks=(b"OggS-fixture",))])
        client = SpeechKitClient(object(), "B" * 32)
        with patch.object(api, "async_get_clientsession", return_value=session):
            audio = await client.synthesize("Привет")
        self.assertEqual(audio, b"OggS-fixture")
        method, url, kwargs = session.calls[0]
        self.assertEqual((method, url), ("POST", api.TTS_URL))
        self.assertEqual(kwargs["headers"]["Authorization"], "Api-Key " + "B" * 32)
        self.assertEqual(parse_qs(kwargs["data"].decode("ascii")), {
            "text": ["Привет"], "lang": ["ru-RU"], "voice": ["filipp"],
        })
        self.assertEqual(
            kwargs["headers"]["Content-Type"],
            "application/x-www-form-urlencoded",
        )
        self.assertLessEqual(len(kwargs["data"]), api.MAX_TTS_REQUEST_BYTES)
        self.assertNotIn("B" * 32, url)
        self.assertFalse(kwargs["allow_redirects"])

    async def test_audio_and_text_limits_fail_before_network(self):
        client = SpeechKitClient(object(), "C" * 32)
        with patch.object(api, "async_get_clientsession") as get_session:
            for kwargs in (
                {"audio": b"", "sample_rate": 16000, "language": "ru-RU"},
                {
                    "audio": b"x" * (16000 * 2 * 30 + 1),
                    "sample_rate": 16000, "language": "ru-RU",
                },
                {"audio": b"pcm", "sample_rate": 16000, "language": "en-US"},
            ):
                with self.subTest(kwargs={key: value if key != "audio" else len(value)
                                          for key, value in kwargs.items()}):
                    with self.assertRaises(SpeechKitError):
                        await client.recognize(**kwargs)
            with self.assertRaises(SpeechKitError) as caught:
                await client.synthesize("x" * (api.MAX_TEXT_CHARS + 1))
        self.assertEqual(caught.exception.code, "invalid_text")
        get_session.assert_not_called()

    async def test_urlencoded_tts_body_obeys_exact_byte_boundary_for_russian(self):
        session = Session([Response(chunks=(b"OggS-fixture",))])
        client = SpeechKitClient(object(), "J" * 32)
        allowed = "я" * 2495
        with patch.object(api, "async_get_clientsession", return_value=session):
            await client.synthesize(allowed)
        body = session.calls[0][2]["data"]
        self.assertEqual(len(body), api.MAX_TTS_REQUEST_BYTES - 1)

        with patch.object(api, "async_get_clientsession") as get_session:
            with self.assertRaises(SpeechKitError) as caught:
                await client.synthesize("я" * 2496)
        self.assertEqual(caught.exception.code, "invalid_text")
        get_session.assert_not_called()

    async def test_provider_failures_are_mapped_without_upstream_body(self):
        for status, expected in (
            (401, "invalid_auth"), (429, "quota"), (503, "cannot_connect"),
        ):
            with self.subTest(status=status):
                session = Session([Response(
                    status=status, chunks=(b"private upstream body",),
                )])
                client = SpeechKitClient(object(), "D" * 32)
                with patch.object(api, "async_get_clientsession", return_value=session):
                    with self.assertRaises(SpeechKitError) as caught:
                        await client.synthesize("Проверка")
                self.assertEqual(caught.exception.code, expected)
                self.assertNotIn("private upstream body", str(caught.exception))

    async def test_timeout_and_connection_failures_are_safe(self):
        for response, expected in ((TimeoutError(), "timeout"),
                                   (OSError("fixture connection"), "cannot_connect")):
            with self.subTest(expected=expected):
                session = Session([response])
                client = SpeechKitClient(object(), "H" * 32)
                with patch.object(api, "async_get_clientsession", return_value=session):
                    with self.assertRaises(SpeechKitError) as caught:
                        await client.synthesize("Проверка")
                self.assertEqual(caught.exception.code, expected)
                self.assertNotIn("fixture connection", str(caught.exception))

    async def test_config_flow_checks_tts_before_storing_the_secret(self):
        entry = {"type": "create_entry"}
        flow = SimpleNamespace(
            hass=object(),
            async_set_unique_id=AsyncMock(),
            _abort_if_unique_id_configured=Mock(),
            async_create_entry=Mock(return_value=entry),
            async_show_form=Mock(return_value={"type": "form"}),
        )
        flow.async_step_user = (
            config_flow.HababYandexSpeechConfigFlow.async_step_user.__get__(flow)
        )
        with patch.object(
            config_flow.SpeechKitClient,
            "synthesize",
            new=AsyncMock(return_value=b"ogg"),
        ) as check:
            result = await flow.async_step_user({"api_key": "E" * 32})
        self.assertIs(result, entry)
        check.assert_awaited_once_with("Проверка подключения SpeechKit.")
        flow.async_create_entry.assert_called_once_with(
            title="Yandex SpeechKit", data={"api_key": "E" * 32},
        )

    async def test_config_flow_does_not_store_a_key_if_check_fails(self):
        flow = SimpleNamespace(
            hass=object(),
            async_set_unique_id=AsyncMock(),
            _abort_if_unique_id_configured=Mock(),
            async_show_form=Mock(return_value={"type": "form"}),
            async_create_entry=Mock(),
        )
        flow.async_step_user = (
            config_flow.HababYandexSpeechConfigFlow.async_step_user.__get__(flow)
        )
        with patch.object(
            config_flow.SpeechKitClient,
            "synthesize",
            new=AsyncMock(side_effect=SpeechKitError("invalid_auth")),
        ):
            result = await flow.async_step_user({"api_key": "F" * 32})
        self.assertEqual(result, {"type": "form"})
        self.assertEqual(
            flow.async_show_form.call_args.kwargs["errors"],
            {"base": "invalid_auth"},
        )
        flow.async_create_entry.assert_not_called()

    async def test_duplicate_entry_aborts_before_billable_tts_check(self):
        flow = SimpleNamespace(
            hass=object(),
            async_set_unique_id=AsyncMock(),
            _abort_if_unique_id_configured=Mock(
                side_effect=RuntimeError("already configured"),
            ),
            async_show_form=Mock(),
            async_create_entry=Mock(),
        )
        flow.async_step_user = (
            config_flow.HababYandexSpeechConfigFlow.async_step_user.__get__(flow)
        )
        with patch.object(
            config_flow.SpeechKitClient, "synthesize", new=AsyncMock(),
        ) as check:
            with self.assertRaisesRegex(RuntimeError, "already configured"):
                await flow.async_step_user({"api_key": "K" * 32})
        flow.async_set_unique_id.assert_awaited_once_with("yandex-speechkit")
        check.assert_not_awaited()
        flow.async_create_entry.assert_not_called()

    async def test_home_assistant_flow_saves_sensitive_key_and_skips_duplicate_probe(
        self,
    ):
        with tempfile.TemporaryDirectory() as config_dir:
            hass = HomeAssistant(config_dir)
            for key in (
                loader.DATA_COMPONENTS,
                loader.DATA_INTEGRATIONS,
                loader.DATA_PRELOAD_PLATFORMS,
                loader.DATA_MISSING_PLATFORMS,
            ):
                hass.data[key] = {}
            hass.config_entries = ConfigEntries(hass, {})
            try:
                first = await hass.config_entries.flow.async_init(
                    "habab_yandex_speech", context={"source": SOURCE_USER},
                )
                self.assertEqual((first["type"], first["step_id"]), ("form", "user"))
                api_key_field = first["data_schema"].schema["api_key"]
                self.assertEqual(api_key_field.config["type"], "password")

                with (
                    patch.object(
                        hass.config_entries,
                        "async_setup",
                        new=AsyncMock(return_value=True),
                    ),
                    patch.object(
                        config_flow.SpeechKitClient,
                        "synthesize",
                        new=AsyncMock(return_value=b"OggS-fixture"),
                    ) as check,
                ):
                    created = await hass.config_entries.flow.async_configure(
                        first["flow_id"], {"api_key": "L" * 32},
                    )
                self.assertEqual(created["type"], "create_entry")
                self.assertEqual(created["data"], {"api_key": "L" * 32})
                check.assert_awaited_once_with("Проверка подключения SpeechKit.")

                second = await hass.config_entries.flow.async_init(
                    "habab_yandex_speech", context={"source": SOURCE_USER},
                )
                with patch.object(
                    config_flow.SpeechKitClient, "synthesize", new=AsyncMock(),
                ) as duplicate_check:
                    duplicate = await hass.config_entries.flow.async_configure(
                        second["flow_id"], {"api_key": "M" * 32},
                    )
                self.assertEqual(duplicate["type"], "abort")
                duplicate_check.assert_not_awaited()
            finally:
                await hass.async_stop()

    async def test_config_entry_forwards_and_unloads_native_speech_platforms(self):
        entry = SimpleNamespace(
            domain="habab_yandex_speech", entry_id="entry-1",
            data={"api_key": "G" * 32},
        )
        hass = SimpleNamespace(config_entries=SimpleNamespace(
            async_forward_entry_setups=AsyncMock(),
            async_unload_platforms=AsyncMock(return_value=True),
        ), data={})
        self.assertTrue(await async_setup_entry(hass, entry))
        hass.config_entries.async_forward_entry_setups.assert_awaited_once_with(
            entry, [Platform.STT, Platform.TTS],
        )
        self.assertIsInstance(hass.data[entry.domain][entry.entry_id], SpeechKitClient)
        self.assertTrue(await async_unload_entry(hass, entry))
        hass.config_entries.async_unload_platforms.assert_awaited_once_with(
            entry, [Platform.STT, Platform.TTS],
        )
        self.assertNotIn(entry.entry_id, hass.data[entry.domain])

    async def test_stt_entity_sends_one_russian_request_and_returns_transcript(self):
        client = SimpleNamespace(recognize=AsyncMock(return_value="включи свет"))
        entity = YandexSpeechToText(client, "entry-1")

        async def audio_stream():
            yield b"pcm"

        result = await entity.async_process_audio_stream(
            SimpleNamespace(sample_rate=SimpleNamespace(value=16000), language="ru-RU"),
            audio_stream(),
        )
        self.assertEqual(result.text, "включи свет")
        client.recognize.assert_awaited_once_with(
            b"pcm", sample_rate=16000, language="ru-RU",
        )
        self.assertEqual(entity.unique_id, "entry-1_stt")

    async def test_stt_failure_returns_error_without_fallback(self):
        client = SimpleNamespace(
            recognize=AsyncMock(side_effect=SpeechKitError("cannot_connect")),
        )
        entity = YandexSpeechToText(client, "entry-1")

        async def audio_stream():
            yield b"pcm"

        result = await entity.async_process_audio_stream(
            SimpleNamespace(sample_rate=SimpleNamespace(value=16000), language="ru-RU"),
            audio_stream(),
        )
        self.assertIsNone(result.text)
        client.recognize.assert_awaited_once()

    async def test_tts_entity_returns_audio_and_propagates_failure(self):
        client = SimpleNamespace(synthesize=AsyncMock(return_value=b"OggS-fixture"))
        entity = YandexTextToSpeech(client, "entry-1")
        self.assertEqual(await entity.async_get_tts_audio("Привет", "ru-RU", {}),
                         ("ogg", b"OggS-fixture"))
        self.assertEqual(entity.unique_id, "entry-1_tts")
        client.synthesize.assert_awaited_once_with("Привет", language="ru-RU")

        client.synthesize.side_effect = SpeechKitError("quota")
        with self.assertRaises(HomeAssistantError):
            await entity.async_get_tts_audio("Привет", "ru-RU", {})
        self.assertEqual(client.synthesize.await_count, 2)


if __name__ == "__main__":
    unittest.main()
