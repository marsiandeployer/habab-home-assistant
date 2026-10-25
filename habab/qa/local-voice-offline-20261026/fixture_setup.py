#!/usr/bin/env python3
"""Create and remove native Home Assistant configuration for local voice QA.

The helper is intentionally not a CLI. Callers pass the short-lived HA token
directly to ``setup_fixture``/``cleanup_fixture``; it is never read from a
file, environment variable, or command line.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


QA_BASE_URL = "http://127.0.0.1:18444"
WHISPER_HOST = "whisper"
WHISPER_PORT = 10300
PIPER_HOST = "piper"
PIPER_PORT = 10200
OLLAMA_URL = "http://ollama:11434"
MODEL = "qwen3:4b-instruct"
PIPELINE_NAME = "Habab local voice QA"
CONVERSATION_NAME = "Habab local voice QA"
ENTITY_ID = "light.qa_virtual_light"
LANGUAGE = "ru"
REQUEST_TIMEOUT = 8
READINESS_TIMEOUT = 45
MAX_RESPONSE_BYTES = 1_048_576
_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_CLEANUP_GUARD = object()


class FixtureSetupError(RuntimeError):
    """A sanitized setup failure with a stable machine-readable code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SetupResult:
    """In-memory fixture handles; do not serialize this object to receipts."""

    status: str
    ollama_entry_id: str
    ollama_conversation_subentry_id: str
    conversation_entity_id: str
    stt_entry_id: str
    stt_engine_id: str
    tts_entry_id: str
    tts_engine_id: str
    tts_voice_id: str
    pipeline_id: str
    fixture_entity_id: str
    _cleanup_guard: object = field(repr=False, compare=False)
    _exposure_changed: bool = field(default=False, repr=False, compare=False)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _HttpClient:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url
        self._token = token
        self._opener = build_opener(_NoRedirect)

    def request(self, method: str, path: str, data: dict[str, Any] | None = None) -> Any:
        body = None if data is None else json.dumps(data, separators=(",", ":")).encode()
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        request = Request(f"{self.base_url}/api/{path.lstrip('/')}", data=body, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=REQUEST_TIMEOUT) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as err:
            raise FixtureSetupError(f"http_{err.code}") from None
        except (URLError, TimeoutError, OSError):
            raise FixtureSetupError("http_unavailable") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise FixtureSetupError("http_response_too_large")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise FixtureSetupError("http_invalid_json") from None


def _match_ws_result(message: Any, request_id: int) -> Any:
    if (
        not isinstance(message, dict)
        or type(message.get("id")) is not int
        or message.get("id") != request_id
        or message.get("type") != "result"
        or message.get("success") is not True
    ):
        raise FixtureSetupError("ws_command_failed")
    return message.get("result")


class _WsClient:
    def __init__(self, base_url: str, token: str) -> None:
        try:
            import websocket
        except ImportError:
            raise FixtureSetupError("websocket_client_unavailable") from None

        ws_url = "ws://" + base_url.removeprefix("http://") + "/api/websocket"
        try:
            self._ws = websocket.create_connection(ws_url, timeout=REQUEST_TIMEOUT)
            greeting = self._receive()
            if not isinstance(greeting, dict) or greeting.get("type") != "auth_required":
                raise FixtureSetupError("ws_auth_protocol_error")
            self._ws.send(json.dumps({"type": "auth", "access_token": token}))
            auth_result = self._receive()
            if not isinstance(auth_result, dict) or auth_result.get("type") != "auth_ok":
                raise FixtureSetupError("ws_auth_failed")
        except FixtureSetupError:
            self.close()
            raise
        except Exception:
            self.close()
            raise FixtureSetupError("ws_unavailable") from None
        self._next_id = 0

    def _receive(self) -> Any:
        try:
            raw = self._ws.recv()
            if not isinstance(raw, str) or len(raw) > MAX_RESPONSE_BYTES:
                raise FixtureSetupError("ws_invalid_message")
            return json.loads(raw)
        except FixtureSetupError:
            raise
        except Exception:
            raise FixtureSetupError("ws_unavailable") from None

    def call(self, command_type: str, **params: Any) -> Any:
        self._next_id += 1
        request_id = self._next_id
        try:
            self._ws.send(json.dumps({"id": request_id, "type": command_type, **params}))
            deadline = time.monotonic() + REQUEST_TIMEOUT
            while time.monotonic() < deadline:
                message = self._receive()
                if isinstance(message, dict) and message.get("id") == request_id:
                    return _match_ws_result(message, request_id)
        except FixtureSetupError:
            raise
        except Exception:
            raise FixtureSetupError("ws_unavailable") from None
        raise FixtureSetupError("ws_timeout")

    def close(self) -> None:
        socket = getattr(self, "_ws", None)
        if socket is not None:
            try:
                socket.close()
            except Exception:
                pass


def _validate_target(
    base_url: str,
    token: str,
    whisper_host: str,
    whisper_port: int,
    piper_host: str,
    piper_port: int,
    ollama_url: str,
) -> None:
    try:
        parsed = urlsplit(base_url)
        port = parsed.port
    except (TypeError, ValueError):
        raise FixtureSetupError("invalid_qa_target") from None
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port != 18444
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise FixtureSetupError("invalid_qa_target")
    if (
        not isinstance(token, str)
        or not token
        or len(token) > 8192
        or any(char.isspace() for char in token)
    ):
        raise FixtureSetupError("invalid_token_argument")
    if (whisper_host, whisper_port) != (WHISPER_HOST, WHISPER_PORT):
        raise FixtureSetupError("invalid_whisper_target")
    if (piper_host, piper_port) != (PIPER_HOST, PIPER_PORT):
        raise FixtureSetupError("invalid_piper_target")
    if ollama_url != OLLAMA_URL:
        raise FixtureSetupError("invalid_ollama_target")


def _require_object(value: Any, code: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FixtureSetupError(code)
    return value


def _require_flow_form(flow: Any, expected_step: str) -> tuple[str, dict[str, Any]]:
    flow = _require_object(flow, "config_flow_invalid_response")
    flow_id = flow.get("flow_id")
    if not isinstance(flow_id, str) or not _ID_PATTERN.fullmatch(flow_id):
        raise FixtureSetupError("config_flow_invalid_response")
    if flow.get("type") != "form" or flow.get("step_id") != expected_step:
        raise FixtureSetupError("config_flow_unexpected_step")
    if flow.get("errors"):
        raise FixtureSetupError("config_flow_rejected")
    return flow_id, flow


def _created_entry_id(flow: Any, expected_domain: str) -> str:
    flow = _require_object(flow, "config_flow_invalid_response")
    if flow.get("type") != "create_entry":
        raise FixtureSetupError("config_flow_did_not_create_entry")
    result = _require_object(flow.get("result"), "config_flow_invalid_response")
    entry_id = result.get("entry_id")
    if result.get("domain") != expected_domain or not isinstance(entry_id, str) or not _ID_PATTERN.fullmatch(entry_id):
        raise FixtureSetupError("config_flow_invalid_response")
    return entry_id


def _options_contain(schema: Any, field_name: str, expected_value: str) -> bool:
    if not isinstance(schema, list):
        return False
    row = next((item for item in schema if isinstance(item, dict) and item.get("name") == field_name), None)
    if row is None:
        return False
    options = row.get("selector", {}).get("select", {}).get("options") if isinstance(row.get("selector"), dict) else None
    if not isinstance(options, list):
        return False
    for option in options:
        if isinstance(option, dict) and option.get("value") == expected_value:
            return True
        if option == expected_value:
            return True
    return False


def _flow_schema_contains(flow: dict[str, Any], field_name: str) -> bool:
    schema = flow.get("data_schema")
    return isinstance(schema, list) and any(
        isinstance(item, dict) and item.get("name") == field_name for item in schema
    )


def _create_config_entry(http: _HttpClient, domain: str, data: dict[str, Any]) -> str:
    flow_id, form = _require_flow_form(
        http.request("POST", "config/config_entries/flow", {"handler": domain}),
        "user",
    )
    fields = ("url",) if domain == "ollama" else ("host", "port")
    if not all(_flow_schema_contains(form, field) for field in fields):
        http.request("DELETE", f"config/config_entries/flow/{quote(flow_id, safe='')}")
        raise FixtureSetupError("config_flow_schema_incomplete")
    completed = http.request("POST", f"config/config_entries/flow/{quote(flow_id, safe='')}", data)
    try:
        return _created_entry_id(completed, domain)
    except FixtureSetupError:
        if isinstance(completed, dict) and completed.get("type") in {"form", "progress", "menu"}:
            http.request("DELETE", f"config/config_entries/flow/{quote(flow_id, safe='')}")
        raise


def _create_ollama_subentry(http: _HttpClient, ollama_entry_id: str) -> None:
    started = http.request(
        "POST",
        "config/config_entries/subentries/flow",
        {"handler": [ollama_entry_id, "conversation"]},
    )
    flow_id, form = _require_flow_form(started, "set_options")
    if not all(_flow_schema_contains(form, field) for field in ("name", "model", "llm_hass_api")):
        http.request("DELETE", f"config/config_entries/subentries/flow/{quote(flow_id, safe='')}")
        raise FixtureSetupError("ollama_subentry_schema_incomplete")
    if not _options_contain(form.get("data_schema"), "model", MODEL):
        http.request("DELETE", f"config/config_entries/subentries/flow/{quote(flow_id, safe='')}")
        raise FixtureSetupError("ollama_model_not_downloaded")
    if not _options_contain(form.get("data_schema"), "llm_hass_api", "assist"):
        http.request("DELETE", f"config/config_entries/subentries/flow/{quote(flow_id, safe='')}")
        raise FixtureSetupError("assist_tools_unavailable")
    completed = http.request(
        "POST",
        f"config/config_entries/subentries/flow/{quote(flow_id, safe='')}",
        {"name": CONVERSATION_NAME, "model": MODEL, "llm_hass_api": ["assist"]},
    )
    result = _require_object(completed, "ollama_subentry_invalid_response")
    if result.get("type") != "create_entry":
        # The Core flow's alternate step is "download"; never continue it.
        if result.get("type") in {"form", "progress", "menu"}:
            http.request("DELETE", f"config/config_entries/subentries/flow/{quote(flow_id, safe='')}")
        raise FixtureSetupError("ollama_subentry_did_not_create_without_download")


def _entry_rows(http: _HttpClient, domain: str) -> list[dict[str, Any]]:
    query = urlencode({"domain": domain})
    result = http.request("GET", f"config/config_entries/entry?{query}")
    if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
        raise FixtureSetupError("config_entry_list_invalid")
    return result


def _preflight(http: _HttpClient, ws: _WsClient) -> tuple[str, bool]:
    user = _require_object(ws.call("auth/current_user"), "auth_user_invalid")
    if user.get("is_admin") is not True or user.get("is_active") is not True:
        raise FixtureSetupError("admin_required")
    if _entry_rows(http, "wyoming") or _entry_rows(http, "ollama"):
        raise FixtureSetupError("qa_integrations_already_configured")

    entities = ws.call("config/entity_registry/list")
    if not isinstance(entities, list):
        raise FixtureSetupError("entity_registry_invalid")
    target = [item for item in entities if isinstance(item, dict) and item.get("entity_id") == ENTITY_ID]
    if (
        len(target) != 1
        or target[0].get("unique_id") != "qa_virtual_light"
        or target[0].get("platform") != "template"
    ):
        raise FixtureSetupError("fixture_light_missing")

    pipeline_data = _require_object(ws.call("assist_pipeline/pipeline/list"), "pipeline_list_invalid")
    pipelines = pipeline_data.get("pipelines")
    if not isinstance(pipelines, list) or any(
        isinstance(item, dict) and item.get("name") == PIPELINE_NAME for item in pipelines
    ):
        raise FixtureSetupError("qa_pipeline_already_exists")

    exposure_data = _require_object(ws.call("homeassistant/expose_entity/list"), "entity_exposure_invalid")
    exposed = exposure_data.get("exposed_entities")
    if not isinstance(exposed, dict):
        raise FixtureSetupError("entity_exposure_invalid")
    target_exposure = exposed.get(ENTITY_ID, {})
    if not isinstance(target_exposure, dict):
        raise FixtureSetupError("entity_exposure_invalid")
    return pipeline_data.get("preferred_pipeline"), target_exposure.get("conversation") is True


def _wait_for_entry(http: _HttpClient, domain: str, entry_id: str) -> None:
    deadline = time.monotonic() + READINESS_TIMEOUT
    while time.monotonic() < deadline:
        rows = _entry_rows(http, domain)
        entry = next((row for row in rows if row.get("entry_id") == entry_id), None)
        if entry is None:
            raise FixtureSetupError("created_config_entry_missing")
        if entry.get("state") == "loaded":
            return
        if entry.get("state") in {"setup_error", "migration_error", "failed_unload"}:
            raise FixtureSetupError("config_entry_setup_failed")
        time.sleep(0.5)
    raise FixtureSetupError("config_entry_setup_timeout")


def _entity_for_entry(
    ws: _WsClient,
    entry_id: str,
    platform: str,
    *,
    subentry_id: str | None = None,
    domain: str | None = None,
) -> str:
    def find() -> str | None:
        entities = ws.call("config/entity_registry/list")
        if not isinstance(entities, list):
            raise FixtureSetupError("entity_registry_invalid")
        matches = [
            item for item in entities
            if isinstance(item, dict)
            and item.get("config_entry_id") == entry_id
            and item.get("platform") == platform
            and (subentry_id is None or item.get("config_subentry_id") == subentry_id)
            and (
                domain is None
                or isinstance(item.get("entity_id"), str)
                and item["entity_id"].startswith(f"{domain}.")
            )
        ]
        if len(matches) > 1:
            raise FixtureSetupError("integration_entity_not_unique")
        if not matches:
            return None
        entity_id = matches[0].get("entity_id")
        if not isinstance(entity_id, str):
            raise FixtureSetupError("entity_registry_invalid")
        return entity_id

    result = _poll(find, lambda item: isinstance(item, str), "integration_entity_timeout")
    return result


def _language_for_engine(engine: Any) -> str:
    if not isinstance(engine, dict) or not isinstance(engine.get("supported_languages"), list):
        raise FixtureSetupError("engine_schema_invalid")
    languages = engine["supported_languages"]
    match = next((lang for lang in languages if isinstance(lang, str) and lang.split("-")[0].lower() == LANGUAGE), None)
    if match is None:
        raise FixtureSetupError("russian_engine_unavailable")
    return match


def _resolve_conversation_engine(agents_response: Any, entity_id: str) -> str:
    """Resolve the API's entity-based ID, not Ollama's config-entry ID."""
    if not isinstance(agents_response, dict) or not isinstance(agents_response.get("agents"), list):
        raise FixtureSetupError("conversation_agent_list_invalid")
    matches = [
        item for item in agents_response["agents"]
        if isinstance(item, dict) and item.get("id") == entity_id
    ]
    if len(matches) != 1:
        raise FixtureSetupError("ollama_conversation_agent_unavailable")
    return entity_id


def _pipeline_payload(
    conversation_engine_id: str,
    stt_engine_id: str,
    stt_language: str,
    tts_engine_id: str,
    tts_language: str,
    tts_voice_id: str,
) -> dict[str, Any]:
    return {
        "name": PIPELINE_NAME,
        "language": LANGUAGE,
        "conversation_engine": conversation_engine_id,
        "conversation_language": LANGUAGE,
        "stt_engine": stt_engine_id,
        "stt_language": stt_language,
        "tts_engine": tts_engine_id,
        "tts_language": tts_language,
        "tts_voice": tts_voice_id,
        "wake_word_entity": None,
        "wake_word_id": None,
        "prefer_local_intents": False,
    }


def _poll(call: Callable[[], Any], predicate: Callable[[Any], bool], code: str) -> Any:
    deadline = time.monotonic() + READINESS_TIMEOUT
    while time.monotonic() < deadline:
        value = call()
        if predicate(value):
            return value
        time.sleep(0.5)
    raise FixtureSetupError(code)


def _cleanup_resources(
    http: _HttpClient,
    ws: _WsClient,
    *,
    ollama_entry_id: str | None,
    subentry_id: str | None,
    stt_entry_id: str | None,
    tts_entry_id: str | None,
    pipeline_id: str | None,
    exposure_changed: bool,
) -> None:
    errors: list[str] = []
    if pipeline_id:
        try:
            preferred = ws.call("assist_pipeline/pipeline/list").get("preferred_pipeline")
            if preferred == pipeline_id:
                errors.append("pipeline_became_preferred")
            else:
                ws.call("assist_pipeline/pipeline/delete", pipeline_id=pipeline_id)
        except Exception:
            errors.append("pipeline_delete_failed")
    if exposure_changed:
        try:
            ws.call(
                "homeassistant/expose_entity",
                assistants=["conversation"],
                entity_ids=[ENTITY_ID],
                should_expose=False,
            )
        except Exception:
            errors.append("entity_unexpose_failed")
    if subentry_id and ollama_entry_id:
        try:
            ws.call(
                "config_entries/subentries/delete",
                entry_id=ollama_entry_id,
                subentry_id=subentry_id,
            )
        except Exception:
            errors.append("ollama_subentry_delete_failed")
    for entry_id in (ollama_entry_id, tts_entry_id, stt_entry_id):
        if not entry_id:
            continue
        try:
            rows = _entry_rows(http, "ollama" if entry_id == ollama_entry_id else "wyoming")
            if any(row.get("entry_id") == entry_id for row in rows):
                http.request("DELETE", f"config/config_entries/entry/{quote(entry_id, safe='')}")
        except Exception:
            errors.append("config_entry_delete_failed")
    if errors:
        raise FixtureSetupError("cleanup_incomplete")


def setup_fixture(
    base_url: str,
    token: str,
    *,
    whisper_host: str = WHISPER_HOST,
    whisper_port: int = WHISPER_PORT,
    piper_host: str = PIPER_HOST,
    piper_port: int = PIPER_PORT,
    ollama_url: str = OLLAMA_URL,
    _http_client: _HttpClient | None = None,
    _ws_factory: Callable[[str, str], _WsClient] = _WsClient,
) -> SetupResult:
    """Configure only the isolated local-voice QA Home Assistant instance."""
    _validate_target(base_url, token, whisper_host, whisper_port, piper_host, piper_port, ollama_url)
    http = _http_client or _HttpClient(base_url.rstrip("/"), token)
    try:
        ws = _ws_factory(base_url.rstrip("/"), token)
    except FixtureSetupError:
        raise
    except Exception:
        raise FixtureSetupError("ws_unavailable") from None
    ollama_entry_id = subentry_id = stt_entry_id = tts_entry_id = pipeline_id = None
    exposure_changed = False
    try:
        preferred_pipeline_id, already_exposed = _preflight(http, ws)

        ollama_entry_id = _create_config_entry(http, "ollama", {"url": ollama_url})
        _wait_for_entry(http, "ollama", ollama_entry_id)
        _create_ollama_subentry(http, ollama_entry_id)
        subentries = ws.call("config_entries/subentries/list", entry_id=ollama_entry_id)
        if not isinstance(subentries, list):
            raise FixtureSetupError("ollama_subentry_list_invalid")
        created_subentries = [
            item for item in subentries
            if isinstance(item, dict)
            and item.get("subentry_type") == "conversation"
            and item.get("title") == CONVERSATION_NAME
        ]
        if len(created_subentries) != 1 or not isinstance(created_subentries[0].get("subentry_id"), str):
            raise FixtureSetupError("ollama_subentry_not_unique")
        subentry_id = created_subentries[0]["subentry_id"]
        if not _ID_PATTERN.fullmatch(subentry_id):
            raise FixtureSetupError("ollama_subentry_invalid_id")

        stt_entry_id = _create_config_entry(
            http, "wyoming", {"host": whisper_host, "port": whisper_port}
        )
        tts_entry_id = _create_config_entry(
            http, "wyoming", {"host": piper_host, "port": piper_port}
        )
        _wait_for_entry(http, "wyoming", stt_entry_id)
        _wait_for_entry(http, "wyoming", tts_entry_id)

        conversation_entity_id = _entity_for_entry(
            ws, ollama_entry_id, "ollama", subentry_id=subentry_id, domain="conversation"
        )
        stt_engine_id = _entity_for_entry(ws, stt_entry_id, "wyoming", domain="stt")
        tts_engine_id = _entity_for_entry(ws, tts_entry_id, "wyoming", domain="tts")

        agents = ws.call("conversation/agent/list", language=LANGUAGE)
        conversation_engine_id = _resolve_conversation_engine(agents, conversation_entity_id)

        stt_engines = ws.call("stt/engine/list", language=LANGUAGE)
        if not isinstance(stt_engines, dict) or not isinstance(stt_engines.get("providers"), list):
            raise FixtureSetupError("stt_engine_list_invalid")
        stt_engine = next((item for item in stt_engines["providers"] if isinstance(item, dict) and item.get("engine_id") == stt_engine_id), None)
        stt_language = _language_for_engine(stt_engine)

        tts_engines = ws.call("tts/engine/list", language=LANGUAGE)
        if not isinstance(tts_engines, dict) or not isinstance(tts_engines.get("providers"), list):
            raise FixtureSetupError("tts_engine_list_invalid")
        tts_engine = next((item for item in tts_engines["providers"] if isinstance(item, dict) and item.get("engine_id") == tts_engine_id), None)
        tts_language = _language_for_engine(tts_engine)
        voices = ws.call("tts/engine/voices", engine_id=tts_engine_id, language=tts_language)
        if not isinstance(voices, dict) or not isinstance(voices.get("voices"), list) or not voices["voices"]:
            raise FixtureSetupError("russian_tts_voice_unavailable")
        voice_id = voices["voices"][0].get("voice_id") if isinstance(voices["voices"][0], dict) else None
        if not isinstance(voice_id, str) or not voice_id:
            raise FixtureSetupError("tts_voice_schema_invalid")

        if not already_exposed:
            ws.call(
                "homeassistant/expose_entity",
                assistants=["conversation"],
                entity_ids=[ENTITY_ID],
                should_expose=True,
            )
            exposure_changed = True

        pipeline = ws.call(
            "assist_pipeline/pipeline/create",
            **_pipeline_payload(
                conversation_engine_id,
                stt_engine_id,
                stt_language,
                tts_engine_id,
                tts_language,
                voice_id,
            ),
        )
        pipeline = _require_object(pipeline, "pipeline_create_invalid_response")
        pipeline_id = pipeline.get("id")
        if not isinstance(pipeline_id, str) or not _ID_PATTERN.fullmatch(pipeline_id):
            raise FixtureSetupError("pipeline_create_invalid_response")
        if pipeline_id == preferred_pipeline_id:
            raise FixtureSetupError("pipeline_unexpectedly_preferred")

        pipelines = _require_object(ws.call("assist_pipeline/pipeline/list"), "pipeline_list_invalid")
        created = [item for item in pipelines.get("pipelines", []) if isinstance(item, dict) and item.get("id") == pipeline_id]
        if len(created) != 1 or created[0].get("conversation_engine") != conversation_engine_id or created[0].get("stt_engine") != stt_engine_id or created[0].get("tts_engine") != tts_engine_id:
            raise FixtureSetupError("pipeline_readback_mismatch")
        exposure = _require_object(ws.call("homeassistant/expose_entity/list"), "entity_exposure_invalid").get("exposed_entities")
        if not isinstance(exposure, dict) or exposure.get(ENTITY_ID, {}).get("conversation") is not True:
            raise FixtureSetupError("fixture_light_not_exposed")

        return SetupResult(
            status="ready",
            ollama_entry_id=ollama_entry_id,
            ollama_conversation_subentry_id=subentry_id,
            conversation_entity_id=conversation_entity_id,
            stt_entry_id=stt_entry_id,
            stt_engine_id=stt_engine_id,
            tts_entry_id=tts_entry_id,
            tts_engine_id=tts_engine_id,
            tts_voice_id=voice_id,
            pipeline_id=pipeline_id,
            fixture_entity_id=ENTITY_ID,
            _cleanup_guard=_CLEANUP_GUARD,
            _exposure_changed=exposure_changed,
        )
    except FixtureSetupError as err:
        try:
            _cleanup_resources(
                http,
                ws,
                ollama_entry_id=ollama_entry_id,
                subentry_id=subentry_id,
                stt_entry_id=stt_entry_id,
                tts_entry_id=tts_entry_id,
                pipeline_id=pipeline_id,
                exposure_changed=exposure_changed,
            )
        except FixtureSetupError:
            raise FixtureSetupError("setup_failed_cleanup_incomplete") from None
        raise err
    except Exception:
        try:
            _cleanup_resources(
                http,
                ws,
                ollama_entry_id=ollama_entry_id,
                subentry_id=subentry_id,
                stt_entry_id=stt_entry_id,
                tts_entry_id=tts_entry_id,
                pipeline_id=pipeline_id,
                exposure_changed=exposure_changed,
            )
        except Exception:
            raise FixtureSetupError("setup_failed_cleanup_incomplete") from None
        raise FixtureSetupError("setup_unexpected_failure") from None
    finally:
        ws.close()


def cleanup_fixture(
    base_url: str,
    token: str,
    result: SetupResult,
    *,
    _http_client: _HttpClient | None = None,
    _ws_factory: Callable[[str, str], _WsClient] = _WsClient,
) -> None:
    """Remove only QA resources created by a still in-memory setup result."""
    _validate_target(base_url, token, WHISPER_HOST, WHISPER_PORT, PIPER_HOST, PIPER_PORT, OLLAMA_URL)
    if not isinstance(result, SetupResult) or result._cleanup_guard is not _CLEANUP_GUARD or result.status != "ready":
        raise FixtureSetupError("invalid_cleanup_handle")
    for value in (
        result.ollama_entry_id,
        result.ollama_conversation_subentry_id,
        result.stt_entry_id,
        result.tts_entry_id,
        result.pipeline_id,
    ):
        if not isinstance(value, str) or not _ID_PATTERN.fullmatch(value):
            raise FixtureSetupError("invalid_cleanup_handle")
    http = _http_client or _HttpClient(base_url.rstrip("/"), token)
    try:
        ws = _ws_factory(base_url.rstrip("/"), token)
    except FixtureSetupError:
        raise
    except Exception:
        raise FixtureSetupError("ws_unavailable") from None
    try:
        user = _require_object(ws.call("auth/current_user"), "auth_user_invalid")
        if user.get("is_admin") is not True or user.get("is_active") is not True:
            raise FixtureSetupError("admin_required")
        _cleanup_resources(
            http,
            ws,
            ollama_entry_id=result.ollama_entry_id,
            subentry_id=result.ollama_conversation_subentry_id,
            stt_entry_id=result.stt_entry_id,
            tts_entry_id=result.tts_entry_id,
            pipeline_id=result.pipeline_id,
            exposure_changed=result._exposure_changed,
        )
    finally:
        ws.close()
