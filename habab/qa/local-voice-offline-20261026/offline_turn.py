#!/usr/bin/env python3
"""Run one synthetic WAV through the isolated native Assist pipeline.

The access token and audio stay in memory. The public result contains only
fixture IDs, the configured model ID, stage timings, and the fixture light
state. Home Assistant does not expose a trustworthy Ollama invocation counter
through this path, so this helper never reports that an LLM call was recorded.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import time
import wave
from typing import Any, Iterable

import fixture_setup


QA_BASE_URL = fixture_setup.QA_BASE_URL
ENTITY_ID = fixture_setup.ENTITY_ID
MODEL = fixture_setup.MODEL
MAX_AUDIO_SECONDS = 30
MIN_SAMPLE_RATE = 8_000
MAX_SAMPLE_RATE = 48_000
MAX_AUDIO_BYTES = MAX_SAMPLE_RATE * 2 * MAX_AUDIO_SECONDS
MAX_MESSAGE_BYTES = fixture_setup.MAX_RESPONSE_BYTES
REQUEST_TIMEOUT = fixture_setup.REQUEST_TIMEOUT
RUN_TIMEOUT = 120
_REQUIRED_EVENTS = (
    "run-start",
    "stt-start",
    "stt-end",
    "intent-start",
    "intent-end",
    "tts-start",
    "tts-end",
    "run-end",
)


class OfflineTurnError(RuntimeError):
    """A failure represented by a safe, stable code only."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _WsReadTimeout(Exception):
    """Internal signal used to distinguish bounded websocket reads."""


@dataclass(frozen=True)
class TurnResult:
    """Sanitized summary; never attach raw events, transcript, or audio."""

    status: str
    pipeline_id: str
    model_id: str
    llm_invocation_status: str
    stage_timings_ms: dict[str, int]
    virtual_light_state: str


def _read_pcm_wav(wav_path: str | Path) -> tuple[int, bytes]:
    try:
        with wave.open(str(wav_path), "rb") as source:
            sample_rate = source.getframerate()
            if (
                source.getnchannels() != 1
                or source.getsampwidth() != 2
                or not MIN_SAMPLE_RATE <= sample_rate <= MAX_SAMPLE_RATE
                or source.getcomptype() != "NONE"
            ):
                raise OfflineTurnError("wav_format_invalid")
            frames = source.getnframes()
            if frames <= 0 or frames > sample_rate * MAX_AUDIO_SECONDS:
                raise OfflineTurnError("wav_duration_invalid")
            pcm = source.readframes(frames + 1)
            if len(pcm) != frames * 2:
                raise OfflineTurnError("wav_data_invalid")
            return sample_rate, pcm
    except OfflineTurnError:
        raise
    except (OSError, EOFError, wave.Error, ValueError, TypeError):
        raise OfflineTurnError("wav_invalid") from None


def _summarize_events(
    events: Iterable[tuple[str, Any, float]], conversation_entity_id: str
) -> dict[str, int]:
    """Validate the required stage chain and return only relative timings."""
    observed: list[str] = []
    first_at: float | None = None
    terminal_at: float | None = None
    selected_engine: str | None = None
    stage_times: dict[str, int] = {}
    for event_type, data, received_at in events:
        if not isinstance(event_type, str) or not isinstance(received_at, (int, float)):
            raise OfflineTurnError("pipeline_event_invalid")
        if event_type == "error":
            raise OfflineTurnError("pipeline_error")
        if event_type not in _REQUIRED_EVENTS:
            continue
        if observed and event_type == "run-start":
            raise OfflineTurnError("pipeline_event_order_invalid")
        if event_type in observed:
            raise OfflineTurnError("pipeline_event_order_invalid")
        observed.append(event_type)
        if event_type == "run-start":
            first_at = float(received_at)
        elif event_type == "intent-start":
            selected_engine = data.get("engine") if isinstance(data, dict) else None
        if event_type == "run-end":
            terminal_at = float(received_at)
        if first_at is not None:
            stage_times[event_type] = max(0, round((float(received_at) - first_at) * 1000))

    if tuple(observed) != _REQUIRED_EVENTS or first_at is None or terminal_at is None:
        raise OfflineTurnError("pipeline_incomplete")
    if selected_engine != conversation_entity_id:
        raise OfflineTurnError("conversation_agent_mismatch")
    return stage_times


def _run_command(request_id: int, pipeline_id: str, sample_rate: int) -> dict[str, Any]:
    return {
        "id": request_id,
        "type": "assist_pipeline/run",
        "pipeline": pipeline_id,
        "start_stage": "stt",
        "end_stage": "tts",
        "input": {"sample_rate": sample_rate},
    }


class _AssistConnection:
    """Small native HA websocket client for the run subscription and PCM frames."""

    def __init__(self, base_url: str, token: str) -> None:
        try:
            import websocket
        except ImportError:
            raise OfflineTurnError("websocket_client_unavailable") from None
        self._timeout_exception = websocket.WebSocketTimeoutException
        ws_url = "ws://" + base_url.removeprefix("http://") + "/api/websocket"
        try:
            self._ws = websocket.create_connection(ws_url, timeout=REQUEST_TIMEOUT)
            greeting = self._receive()
            if not isinstance(greeting, dict) or greeting.get("type") != "auth_required":
                raise OfflineTurnError("ws_auth_protocol_error")
            self._ws.send(json.dumps({"type": "auth", "access_token": token}))
            auth = self._receive()
            if not isinstance(auth, dict) or auth.get("type") != "auth_ok":
                raise OfflineTurnError("ws_auth_failed")
        except OfflineTurnError:
            self.close()
            raise
        except Exception:
            self.close()
            raise OfflineTurnError("ws_unavailable") from None

    def _receive(self) -> Any:
        try:
            raw = self._ws.recv()
            if not isinstance(raw, str) or len(raw) > MAX_MESSAGE_BYTES:
                raise OfflineTurnError("ws_invalid_message")
            return json.loads(raw)
        except OfflineTurnError:
            raise
        except self._timeout_exception:
            raise _WsReadTimeout from None
        except Exception:
            raise OfflineTurnError("ws_unavailable") from None

    def run(
        self,
        pipeline_id: str,
        sample_rate: int,
        pcm: bytes,
        conversation_entity_id: str,
    ) -> dict[str, int]:
        request_id = 1
        command = _run_command(request_id, pipeline_id, sample_rate)
        try:
            self._ws.send(json.dumps(command))
            deadline = time.monotonic() + RUN_TIMEOUT
            queued_events: list[tuple[str, Any, float]] = []
            acknowledged = False
            while not acknowledged:
                message = self._receive_with_deadline(deadline)
                if isinstance(message, dict) and message.get("id") == request_id and message.get("type") == "result":
                    if message.get("success") is not True:
                        raise OfflineTurnError("pipeline_start_rejected")
                    acknowledged = True
                elif isinstance(message, dict) and message.get("id") == request_id and message.get("type") == "event":
                    queued_events.append(self._event_tuple(message))
            event_times: list[tuple[str, Any, float]] = []
            sent_audio = False
            handler_id: int | None = None
            while True:
                if queued_events:
                    event_type, data, received_at = queued_events.pop(0)
                else:
                    message = self._receive_with_deadline(deadline)
                    if not isinstance(message, dict) or message.get("id") != request_id or message.get("type") != "event":
                        continue
                    event_type, data, received_at = self._event_tuple(message)

                event_times.append((event_type, data, received_at))
                if event_type == "run-start":
                    runner_data = data.get("runner_data") if isinstance(data, dict) else None
                    candidate = runner_data.get("stt_binary_handler_id") if isinstance(runner_data, dict) else None
                    if type(candidate) is not int or not 0 <= candidate <= 255:
                        raise OfflineTurnError("audio_handler_invalid")
                    handler_id = candidate
                elif event_type == "stt-start":
                    if handler_id is None or sent_audio:
                        raise OfflineTurnError("audio_handler_invalid")
                    self._send_pcm(handler_id, sample_rate, pcm, deadline)
                    sent_audio = True
                elif event_type == "error":
                    raise OfflineTurnError("pipeline_error")
                elif event_type == "run-end":
                    if not sent_audio:
                        raise OfflineTurnError("pipeline_incomplete")
                    return _summarize_events(event_times, conversation_entity_id)
        except OfflineTurnError:
            raise
        except Exception:
            raise OfflineTurnError("pipeline_transport_failed") from None

    @staticmethod
    def _event_tuple(message: dict[str, Any]) -> tuple[str, Any, float]:
        event = message.get("event")
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise OfflineTurnError("pipeline_event_invalid")
        return event["type"], event.get("data"), time.monotonic()

    def _receive_with_deadline(self, deadline: float) -> Any:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OfflineTurnError("pipeline_timeout")
            try:
                self._ws.settimeout(remaining)
            except Exception:
                pass
            try:
                return self._receive()
            except _WsReadTimeout:
                if time.monotonic() >= deadline:
                    raise OfflineTurnError("pipeline_timeout") from None

    def _send_pcm(self, handler_id: int, sample_rate: int, pcm: bytes, deadline: float) -> None:
        bytes_per_chunk = sample_rate * 2 // 50
        for offset in range(0, len(pcm), bytes_per_chunk):
            if time.monotonic() >= deadline:
                raise OfflineTurnError("pipeline_timeout")
            self._ws.send_binary(bytes((handler_id,)) + pcm[offset : offset + bytes_per_chunk])
            time.sleep(0.02)
        if time.monotonic() >= deadline:
            raise OfflineTurnError("pipeline_timeout")
        self._ws.send_binary(bytes((handler_id,)))

    def close(self) -> None:
        socket = getattr(self, "_ws", None)
        if socket is not None:
            try:
                socket.close()
            except Exception:
                pass


def run_offline_turn(
    base_url: str,
    access_token: str,
    setup_result: fixture_setup.SetupResult,
    wav_path: str | Path,
) -> TurnResult:
    """Run one prepared synthetic WAV against only the isolated QA fixture."""
    try:
        fixture_setup._validate_target(
            base_url,
            access_token,
            fixture_setup.WHISPER_HOST,
            fixture_setup.WHISPER_PORT,
            fixture_setup.PIPER_HOST,
            fixture_setup.PIPER_PORT,
            fixture_setup.OLLAMA_URL,
        )
    except fixture_setup.FixtureSetupError:
        raise OfflineTurnError("invalid_qa_target") from None
    if (
        not isinstance(setup_result, fixture_setup.SetupResult)
        or setup_result.status != "ready"
        or setup_result._cleanup_guard is not fixture_setup._CLEANUP_GUARD
        or setup_result.fixture_entity_id != ENTITY_ID
        or not setup_result.pipeline_id
        or not setup_result.conversation_entity_id
    ):
        raise OfflineTurnError("invalid_setup_result")
    sample_rate, pcm = _read_pcm_wav(wav_path)
    if len(pcm) > MAX_AUDIO_BYTES:
        raise OfflineTurnError("wav_duration_invalid")

    http = fixture_setup._HttpClient(QA_BASE_URL, access_token)
    try:
        before = http.request("GET", f"states/{ENTITY_ID}")
    except fixture_setup.FixtureSetupError:
        raise OfflineTurnError("virtual_light_unavailable") from None
    if not isinstance(before, dict) or before.get("entity_id") != ENTITY_ID or before.get("state") != "off":
        raise OfflineTurnError("virtual_light_not_off")

    connection = _AssistConnection(QA_BASE_URL, access_token)
    try:
        timings = connection.run(
            setup_result.pipeline_id,
            sample_rate,
            pcm,
            setup_result.conversation_entity_id,
        )
    finally:
        connection.close()

    try:
        after = http.request("GET", f"states/{ENTITY_ID}")
    except fixture_setup.FixtureSetupError:
        raise OfflineTurnError("virtual_light_unavailable") from None
    if not isinstance(after, dict) or after.get("entity_id") != ENTITY_ID:
        raise OfflineTurnError("virtual_light_unavailable")
    if after.get("state") != "on":
        raise OfflineTurnError("virtual_light_state_mismatch")
    return TurnResult(
        status="completed",
        pipeline_id=setup_result.pipeline_id,
        model_id=MODEL,
        llm_invocation_status="NOT_RECORDED",
        stage_timings_ms=timings,
        virtual_light_state="on",
    )
