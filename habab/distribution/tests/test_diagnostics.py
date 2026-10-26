"""Owner-scoped diagnostics never expose raw runtime payloads or foreign sessions."""

import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.components.habab_home import diagnostics
from homeassistant.components.habab_home import setup_agent
from homeassistant.components.habab_home.setup_agent import AgentError, SetupAgent
from homeassistant.components.habab_home.voice_history import encode_voice_event


class Request(dict):
    def __init__(self, owner, hass, *, query=None, data=None):
        super().__init__(hass_user=owner)
        self.app = {"hass": hass}
        self.query = query or {}
        self.json = AsyncMock(return_value=data or {})


def runtime_fixture(messages=None):
    runtime = SimpleNamespace(
        request=AsyncMock(return_value=messages or []),
        clean=lambda text: text.replace("configured-provider-secret", "[redacted]"),
        visible=Mock(return_value=[]),
        active=None,
        diagnostics=SimpleNamespace(get=Mock(return_value=None)),
    )
    assistant = SimpleNamespace(
        runtime=runtime,
        web_sessions={"alice": "web-alice", "bob": "web-bob"},
        telegram_sessions={
            "alice:bot-entry:1001": "tg-alice",
            "bob:bot-entry:2002": "tg-bob",
        },
    )
    hass = SimpleNamespace(
        data={"habab_home": {"assistant": assistant}},
    )
    owner = SimpleNamespace(id="alice", is_active=True, is_admin=True)
    return owner, hass, assistant, runtime


def tool_messages():
    return [{
        "info": {"role": "assistant", "providerID": "habab-deepseek", "modelID": "deepseek-chat",
                 "time": {"created": 1792933200000, "completed": 1792933201000}},
        "parts": [
            {"type": "text", "text": "Сейчас проверю. OPENAI_API_KEY=short-env-key private_key=short-private-key "
             "details={\"password\": \"short-json-password\"} "
             "escaped={\\\"apiKey\\\": \\\"short-json-api-key\\\"} "
             "scalar={\"password\":123456789,\"token\":987654321,\"secret\":true,\"oauth\":null} "
             "escaped_scalar={\\\"id_token\\\":7654321} "
             r'escaped_unicode={\"pass\\u0077ord\":\"short-escaped-unicode-secret\"}'},
            {"type": "tool", "tool": "habab-ha", "callID": "call-safe-1", "state": {
                "status": "completed",
                "time": {"start": 1792933200250, "end": 1792933200750},
                "input": {"action": "states", "api_key": "nested-private",
                          "refreshToken": "camel-private", "OPENAI_API_KEY": "short-env-json",
                          "ssh_private_key": "short-json-private",
                          "signing_key": "short-signing-key"},
                "output": "Authorization: Bearer runtime-private-token\n"
                          'details={"refresh_token": "short-json-refresh"}\n'
                          "Cookie: session=private-cookie; refresh=private-refresh\n"
                          "https://example.invalid/callback?oauth_token=short-oauth&"
                          "id_token=short-id-token&encryption_key=short-query-encryption\n"
                          "Configured configured-provider-secret",
                "metadata": {"exit": 0},
            }},
        ],
    }]


class DiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_http_views_require_active_admin_before_runtime_access(self):
        owner, hass, assistant, runtime = runtime_fixture(tool_messages())
        for view, method, args in [
            (diagnostics.DiagnosticsSessionsView(), "get", (Request(
                SimpleNamespace(id="guest", is_active=True, is_admin=False), hass),)),
            (diagnostics.DiagnosticsSessionView(), "get", (Request(
                SimpleNamespace(id="guest", is_active=True, is_admin=False), hass), "web-alice")),
            (diagnostics.DiagnosticsExportView(), "post", (Request(
                SimpleNamespace(id="guest", is_active=True, is_admin=False), hass,
                data={"session_id": "web-alice"}),)),
            (diagnostics.DiagnosticsRuntimeLogsExportView(), "post", (Request(
                SimpleNamespace(id="guest", is_active=True, is_admin=False), hass),)),
        ]:
            response = await getattr(view, method)(*args)
            self.assertEqual(response.status, 403)
        runtime.request.assert_not_awaited()

        inactive = SimpleNamespace(id="alice", is_active=False, is_admin=True)
        response = await diagnostics.DiagnosticsSessionsView().get(Request(inactive, hass))
        self.assertEqual(response.status, 403)

    async def test_detail_requires_exact_owner_channel_mapping_and_safe_session_id(self):
        owner, hass, assistant, runtime = runtime_fixture(tool_messages())
        response = await diagnostics.DiagnosticsSessionView().get(
            Request(owner, hass), "web-bob")
        self.assertEqual(response.status, 404)
        response = await diagnostics.DiagnosticsSessionView().get(
            Request(owner, hass, query={"channel": "web"}), "tg-alice")
        self.assertEqual(response.status, 404)
        response = await diagnostics.DiagnosticsSessionView().get(
            Request(owner, hass), "../web-bob")
        self.assertEqual(response.status, 404)
        runtime.request.assert_not_awaited()

        response = await diagnostics.DiagnosticsSessionView().get(
            Request(owner, hass), "tg-alice")
        self.assertEqual(response.status, 200)
        runtime.request.assert_awaited_once_with(
            "GET", "session/tg-alice/message?limit=60")
        payload = json.loads(response.body)
        self.assertEqual(payload["session"]["channel"], "telegram")
        self.assertNotIn("1001", response.body.decode())
        self.assertNotIn("bot-entry", response.body.decode())

    async def test_session_reset_during_history_read_revokes_response(self):
        owner, hass, assistant, runtime = runtime_fixture(tool_messages())

        async def reset_mapping(*_args):
            assistant.web_sessions["alice"] = "web-replaced"
            return tool_messages()

        runtime.request.side_effect = reset_mapping
        response = await diagnostics.DiagnosticsSessionView().get(
            Request(owner, hass), "web-alice")
        self.assertEqual(response.status, 404)
        self.assertNotIn("Сейчас проверю", response.body.decode())

    async def test_diagnostics_runtime_request_uses_existing_streamed_response_cap(self):
        class Content:
            async def iter_chunked(self, _chunk_size):
                yield b"x" * 1_000_001
                yield b"y" * 1_000_000

        class ResponseContext:
            status = 200
            content = Content()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

        session = SimpleNamespace(request=Mock(return_value=ResponseContext()))
        runtime = object.__new__(SetupAgent)
        runtime.hass = SimpleNamespace()
        runtime.password = "fixture-password"
        with patch.object(setup_agent, "async_get_clientsession", return_value=session):
            with self.assertRaises(AgentError) as raised:
                await runtime.request("GET", "session/example/message?limit=60")
        self.assertEqual(raised.exception.code, "response_too_large")
        self.assertEqual(diagnostics.MAX_RUNTIME_RESPONSE_BYTES, 2_000_000)

    async def test_detail_redacts_messages_and_separates_assistant_text_tool_call_and_result(self):
        owner, hass, _assistant, runtime = runtime_fixture(tool_messages())
        response = await diagnostics.DiagnosticsSessionView().get(
            Request(owner, hass), "web-alice")
        self.assertEqual(response.status, 200)
        body = response.body.decode()
        for secret in ("nested-private", "camel-private", "runtime-private-token", "private-cookie",
                       "private-refresh", "configured-provider-secret", "short-env-key",
                       "short-env-json", "short-oauth", "short-id-token", "short-private-key",
                       "short-json-private", "short-signing-key", "short-query-encryption",
                       "short-json-password", "short-json-api-key", "short-json-refresh",
                       "123456789", "987654321", "7654321", "short-escaped-unicode-secret"):
            self.assertNotIn(secret, body)
        payload = json.loads(body)
        events = payload["events"]
        assistant_text = next(event["text"] for event in events if event["kind"] == "assistant_text")
        self.assertIn('details={"password": "[redacted]"}', assistant_text)
        self.assertIn('escaped={\\"apiKey\\": \\"[redacted]\\"}', assistant_text)
        self.assertIn('scalar={"password":"[redacted]","token":"[redacted]",'
                      '"secret":"[redacted]","oauth":"[redacted]"}', assistant_text)
        self.assertIn('escaped_scalar={\\"id_token\\":\\"[redacted]\\"}', assistant_text)
        self.assertIn(r'escaped_unicode={\"pass\\u0077ord\":\"[redacted]\"}', assistant_text)
        self.assertEqual(payload["session"]["provider_models"], [
            {"provider": "habab-deepseek", "model": "deepseek-chat"}])
        self.assertEqual(payload["session"]["processing_duration_ms"], 1000)
        model_event = next(event for event in events if event.get("provider") == "habab-deepseek")
        self.assertEqual(model_event["model"], "deepseek-chat")
        self.assertTrue(any(event["stage"] == "model_text" and
                            event["confirmation"] == "not_applicable" for event in events))
        call = next(event for event in events if event["stage"] == "tool_call")
        result = next(event for event in events if event["stage"] == "tool_result")
        self.assertEqual(call["confirmation"], "runtime_call_recorded")
        self.assertEqual(call["correlation_id"], result["correlation_id"])
        self.assertEqual(call["at"], "2026-10-25T13:00:00.250Z")
        self.assertEqual(result["at"], "2026-10-25T13:00:00.750Z")
        self.assertIsNone(call["duration_ms"])
        self.assertEqual(result["duration_ms"], 500)
        self.assertEqual(call["input"]["api_key"], "[redacted]")
        self.assertEqual(call["input"]["ssh_private_key"], "[redacted]")
        self.assertEqual(call["input"]["signing_key"], "[redacted]")
        self.assertEqual(result["confirmation"], "runtime_result_recorded")
        self.assertEqual(result["exit_code"], 0)
        self.assertFalse(any(event["stage"] == "backend_accept" and event["available"] for event in events))
        self.assertTrue(any(event["stage"] == "backend_accept" and not event["available"] for event in events))
        self.assertFalse(payload["sources"]["native_assist_history"]["available"])

    async def test_tool_events_do_not_substitute_parent_message_time_when_runtime_time_is_missing(self):
        messages = [{
            "info": {"role": "assistant", "time": {"created": 1792933200000,
                                                        "completed": 1792933201000}},
            "parts": [{"type": "tool", "tool": "habab-ha", "state": {
                "status": "completed", "input": {"action": "states"}, "output": "ok",
            }}],
        }]

        events = diagnostics.project_messages(None, messages, "web")

        call, result = events
        self.assertIsNone(call["at"])
        self.assertIsNone(result["at"])
        self.assertIsNone(call["duration_ms"])
        self.assertIsNone(result["duration_ms"])
        self.assertNotIn("_sort_at", json.dumps(events))
        self.assertTrue(diagnostics._matches_window(
            [call], diagnostics._parse_time("2026-10-25T00:00:00Z"),
            diagnostics._parse_time("2026-10-26T00:00:00Z"), messages))

    async def test_projection_event_budget_stops_before_later_parts(self):
        messages = [{"info": {"role": "assistant", "time": {"created": 1792933200000,
                                                                  "completed": 1792933201000}},
                     "parts": [
                         {"type": "text", "text": "Первый ответ"},
                         {"type": "tool", "tool": "habab-ha", "state": {
                             "status": "completed", "input": {"action": "states"},
                             "output": "synthetic early output"}},
                         {"type": "text", "text": "LATE_PART_SENTINEL"},
                     ]}]
        owner, hass, _assistant, runtime = runtime_fixture(messages)
        with patch.object(diagnostics, "MAX_PROJECTED_EVENTS", 1):
            response = await diagnostics.DiagnosticsSessionView().get(
                Request(owner, hass), "web-alice")
        body = response.body.decode()
        payload = json.loads(body)
        self.assertTrue(payload["session"]["events_truncated"])
        self.assertNotIn("LATE_PART_SENTINEL", body)
        self.assertNotIn("synthetic early output", body)

    async def test_projection_byte_budget_caps_text_and_nested_item_walk(self):
        messages = [
            {"info": {"role": "user"},
             "parts": [{"type": "text", "text": "bounded text " * 100}]},
            {"info": {"role": "assistant"},
             "parts": [{"type": "text", "text": "LATE_BYTE_SENTINEL"}]},
        ]
        owner, hass, _assistant, _runtime = runtime_fixture(messages)
        with patch.object(diagnostics, "MAX_PROJECTED_BYTES", 24):
            response = await diagnostics.DiagnosticsSessionView().get(
                Request(owner, hass), "web-alice")
        payload = json.loads(response.body)
        self.assertTrue(payload["session"]["events_truncated"])
        projected_events = [event for event in payload["events"] if event["available"]]
        self.assertEqual([event["id"] for event in projected_events], ["0.0", "1.0"])
        self.assertNotEqual(projected_events[0]["text"], "bounded text " * 100)
        self.assertEqual(projected_events[1]["text"], "LATE_BYTE_SENTINEL")
        budget = diagnostics.ProjectionBudget(max_bytes=1000, max_events=10)
        cleaned = diagnostics._clean_value(
            None, {"items": ["value"] * 100}, budget=budget)
        self.assertEqual(len(cleaned["items"]), 50)
        self.assertGreater(budget.truncation_count, 0)

        deep = {"level": {"level": {"level": {"level": {"level": {"level": "too deep"}}}}}}
        depth_budget = diagnostics.ProjectionBudget(max_bytes=1000, max_events=10)
        depth_cleaned = diagnostics._clean_value(None, deep, budget=depth_budget)
        self.assertEqual(
            depth_cleaned["level"]["level"]["level"]["level"]["level"]["level"],
            "[truncated]",
        )
        self.assertGreater(depth_budget.truncation_count, 0)

        item_budget = diagnostics.ProjectionBudget(max_bytes=1000, max_events=10)
        with patch.object(diagnostics, "MAX_PROJECTED_ITEMS", 3):
            item_budget.remaining_items = diagnostics.MAX_PROJECTED_ITEMS
            item_cleaned = diagnostics._clean_value(
                None, {"items": ["first", "SECOND_ITEM_SENTINEL"]}, budget=item_budget)
        self.assertEqual(item_cleaned["items"], ["first", "[truncated]"])
        self.assertGreater(item_budget.truncation_count, 0)

    async def test_newest_conversation_evidence_survives_older_large_tool_output(self):
        messages = [
            {"info": {"role": "assistant", "time": {"created": 1792933200000}},
             "parts": [{"type": "tool", "tool": "habab-ha", "state": {
                 "status": "completed", "input": {"action": "devices"},
                 "output": "OLD_LARGE_OUTPUT_START " + "x" * 4000 + " OLD_LATE_SENTINEL",
             }}]},
            {"info": {"role": "user", "time": {"created": 1792933300000}},
             "parts": [{"type": "text", "text": "LATEST_USER_SENTINEL"}]},
            {"info": {"role": "assistant", "time": {"created": 1792933400000}},
             "parts": [{"type": "tool", "tool": "habab-ha", "state": {
                 "status": "completed", "input": {"action": "states"},
                 "output": "LATEST_TOOL_SENTINEL",
             }}]},
            {"info": {"role": "assistant", "time": {"created": 1792933500000}},
             "parts": [{"type": "text", "text": "LATEST_REPLY_SENTINEL"}]},
        ]
        budget = diagnostics.ProjectionBudget(max_bytes=160, max_events=20)
        events = diagnostics.project_messages(None, messages, "web", budget)
        serialized = json.dumps(events)

        self.assertIn("LATEST_USER_SENTINEL", serialized)
        self.assertIn("LATEST_TOOL_SENTINEL", serialized)
        self.assertIn("LATEST_REPLY_SENTINEL", serialized)
        self.assertNotIn("OLD_LATE_SENTINEL", serialized)
        self.assertGreater(budget.truncation_count, 0)
        self.assertEqual(
            [event["id"] for event in events],
            ["0.0.call", "0.0.result", "1.0", "2.0.call", "2.0.result", "3.0"],
        )

    async def test_projection_preserves_source_part_order_and_stable_ids(self):
        messages = [{
            "info": {"role": "assistant", "time": {"created": 1792933200000}},
            "parts": [
                {"type": "text", "text": "before tool"},
                {"type": "tool", "tool": "habab-ha", "state": {
                    "status": "completed", "input": {"action": "states"},
                    "output": "tool result",
                }},
                {"type": "text", "text": "after tool"},
            ],
        }]

        events = diagnostics.project_messages(None, messages, "web")

        self.assertEqual([event["id"] for event in events], [
            "0.0", "0.1.call", "0.1.result", "0.2",
        ])
        self.assertEqual([event.get("text") or event.get("output") for event in events], [
            "before tool", None, "tool result", "after tool",
        ])

    async def test_projection_keeps_last_sixty_messages(self):
        messages = [{"info": {"role": "user"},
                     "parts": [{"type": "text", "text": f"MESSAGE_{index}"}]}
                    for index in range(65)]

        events = diagnostics.project_messages(None, messages, "web")

        self.assertEqual(len(events), diagnostics.MAX_MESSAGES)
        self.assertEqual(events[0]["id"], "0.0")
        self.assertEqual(events[0]["text"], "MESSAGE_5")
        self.assertEqual(events[-1]["id"], "59.0")
        self.assertEqual(events[-1]["text"], "MESSAGE_64")

    async def test_projection_excludes_synthetic_runtime_status_but_keeps_marked_voice(self):
        voice_raw, _digest, voice_metadata = encode_voice_event(
            "evt-trusted-voice", "user", "TRUSTED_VOICE_SENTINEL")
        messages = [
            {"info": {"role": "assistant"},
             "parts": [{"type": "text", "synthetic": True,
                        "text": "HABAB_RUNTIME_STATUS\nUNTRUSTED_ASSISTANT_STATUS"}]},
            {"info": {"role": "user"},
             "parts": [{"type": "text", "synthetic": True,
                        "text": "HABAB_RUNTIME_STATUS\nUNTRUSTED_USER_STATUS"},
                       {"type": "text", "text": "REAL_USER_SENTINEL"},
                       {"type": "text", "synthetic": True,
                        "text": voice_raw, "metadata": voice_metadata}]},
        ]

        events = diagnostics.project_messages(None, messages, "web")
        projected_text = json.dumps(events)

        self.assertNotIn("HABAB_RUNTIME_STATUS", projected_text)
        self.assertNotIn("UNTRUSTED_ASSISTANT_STATUS", projected_text)
        self.assertNotIn("UNTRUSTED_USER_STATUS", projected_text)
        self.assertIn("REAL_USER_SENTINEL", projected_text)
        self.assertIn("TRUSTED_VOICE_SENTINEL", projected_text)
        voice_event = next(event for event in events if event["channel"] == "voice")
        self.assertEqual(voice_event["kind"], "voice_transcript")

    async def test_voice_filter_uses_valid_persisted_voice_markers_not_plain_web_text(self):
        voice_raw, _digest, voice_metadata = encode_voice_event(
            "evt-voice-1", "user", "Включи свет")
        voice_messages = [
            {"info": {"role": "user", "time": {"created": 1792933200000}},
             "parts": [{"type": "text", "text": "обычный web текст"}]},
            {"info": {"role": "user", "time": {"created": 1792933201000}},
             "parts": [{"type": "text", "synthetic": True,
                        "text": voice_raw, "metadata": voice_metadata}]},
        ]
        owner, hass, assistant, runtime = runtime_fixture(voice_messages)
        list_response = await diagnostics.DiagnosticsSessionsView().get(
            Request(owner, hass, query={"channel": "voice"}))
        self.assertEqual(json.loads(list_response.body)["sessions"][0]["modes"], ["web", "voice"])
        detail = await diagnostics.DiagnosticsSessionView().get(
            Request(owner, hass, query={"channel": "voice"}), "web-alice")
        payload = json.loads(detail.body)
        voice_events = [event for event in payload["events"]
                        if event["channel"] == "voice" and event["kind"] == "voice_transcript"]
        self.assertEqual([event["text"] for event in voice_events], ["Включи свет"])
        self.assertEqual(payload["session"]["channel"], "web")
        self.assertIn("voice", payload["session"]["modes"])
        self.assertNotIn("обычный web текст", json.dumps(json.loads(list_response.body)))

    async def test_structured_error_and_cancellation_are_safe_runtime_evidence(self):
        owner, hass, _assistant, runtime = runtime_fixture([{
            "info": {"role": "assistant", "time": {"created": 1792933200000},
                     "error": {"name": "APIError", "message": "private-secret text",
                               "data": {"statusCode": 429}}},
            "parts": [],
        }])
        response = await diagnostics.DiagnosticsSessionView().get(
            Request(owner, hass), "web-alice")
        payload = json.loads(response.body)
        self.assertEqual(payload["session"]["status"], "error")
        error = next(event for event in payload["events"] if event["kind"] == "stage_error")
        self.assertEqual(error["status"], "error")
        self.assertEqual(error["output"]["name"], "APIError")
        self.assertEqual(error["output"]["http_status"], 429)
        self.assertNotIn("private-secret text", response.body.decode())

        runtime.request.return_value = [{
            "info": {"role": "assistant", "error": {"name": "MessageAbortedError",
                     "message": "cancelled privately"}}, "parts": [],
        }]
        response = await diagnostics.DiagnosticsSessionView().get(
            Request(owner, hass), "web-alice")
        payload = json.loads(response.body)
        self.assertEqual(payload["session"]["status"], "cancelled")
        error = next(event for event in payload["events"] if event["kind"] == "stage_error")
        self.assertEqual(error["status"], "cancelled")
        self.assertEqual(error["stage"], "cancellation")
        self.assertNotIn("cancelled privately", response.body.decode())

    async def test_newer_user_message_does_not_inherit_completed_status_from_older_turn(self):
        owner, hass, _assistant, _runtime = runtime_fixture([
            {"info": {"role": "assistant", "time": {"created": 1792933200000,
                                                        "completed": 1792933201000}},
             "parts": [{"type": "text", "text": "Ответ предыдущего хода"}]},
            {"info": {"role": "user", "time": {"created": 1792933300000}},
             "parts": [{"type": "text", "text": "Новый запрос"}]},
        ])
        response = await diagnostics.DiagnosticsSessionView().get(
            Request(owner, hass), "web-alice")
        self.assertEqual(json.loads(response.body)["session"]["status"], "unknown")

    async def test_session_status_bounds_message_part_scan_and_avoids_partial_completion_claim(self):
        owner, _hass, _assistant, runtime = runtime_fixture()
        parts = [{"type": "tool", "state": {"status": "running"}}] * diagnostics.MAX_PARTS_PER_MESSAGE
        parts.append({"type": "text", "text": "beyond bounded scan"})
        status, _error = diagnostics._session_status(runtime, owner.id, "web-alice", [{
            "info": {"role": "assistant", "time": {"created": 1792933200000,
                                                          "completed": 1792933201000}},
            "parts": parts,
        }])
        self.assertEqual(status, "unknown")

    async def test_filters_require_timezone_and_bound_window_and_pagination(self):
        owner, hass, _assistant, _runtime = runtime_fixture([])
        bad = await diagnostics.DiagnosticsSessionsView().get(
            Request(owner, hass, query={"from": "2026-10-25T12:00:00"}))
        self.assertEqual(bad.status, 400)
        one_bound = await diagnostics.DiagnosticsSessionsView().get(
            Request(owner, hass, query={"from": "2026-10-25T12:00:00Z"}))
        self.assertEqual(one_bound.status, 400)
        too_wide = await diagnostics.DiagnosticsSessionsView().get(
            Request(owner, hass, query={"from": "2026-08-01T00:00:00Z",
                                        "to": "2026-10-25T00:00:00Z"}))
        self.assertEqual(too_wide.status, 400)
        bad_cursor = await diagnostics.DiagnosticsSessionsView().get(
            Request(owner, hass, query={"cursor": "-1"}))
        self.assertEqual(bad_cursor.status, 400)
        first = await diagnostics.DiagnosticsSessionsView().get(
            Request(owner, hass, query={"limit": "1"}))
        self.assertEqual(len(json.loads(first.body)["sessions"]), 1)
        self.assertEqual(json.loads(first.body)["next_cursor"], "1")

    async def test_export_is_bounded_zip_with_manifest_and_no_secret_fields(self):
        owner, hass, _assistant, _runtime = runtime_fixture(tool_messages())
        response = await diagnostics.DiagnosticsExportView().post(
            Request(owner, hass, data={"session_id": "web-alice"}))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, "application/zip")
        self.assertEqual(response.headers["Content-Disposition"],
                         'attachment; filename="habab-diagnostics.zip"')
        self.assertLessEqual(len(response.body), diagnostics.MAX_EXPORT_BYTES)
        with zipfile.ZipFile(io.BytesIO(response.body)) as archive:
            self.assertLessEqual(
                sum(info.file_size for info in archive.infolist()), diagnostics.MAX_EXPORT_BYTES)
            self.assertEqual(set(archive.namelist()), {
                "manifest.json", "history.json", "history.txt", "opencode/availability.json"})
            manifest = json.loads(archive.read("manifest.json"))
            history = archive.read("history.json").decode()
            history_text = archive.read("history.txt").decode()
            self.assertFalse(manifest["audio_included"])
            self.assertEqual(manifest["period"]["timezone"], "UTC")
            self.assertIn("opencode_files", manifest["sources"])
            self.assertFalse(manifest["sources"]["opencode_files"]["available"])
            self.assertFalse(manifest["sources"]["native_assist_history"]["available"])
            self.assertNotIn("nested-private", history)
            self.assertNotIn("camel-private", history)
            self.assertNotIn("runtime-private-token", history)
            self.assertNotIn("private-cookie", history)
            self.assertNotIn("private-refresh", history)
            self.assertNotIn("configured-provider-secret", history)
            self.assertNotIn("short-env-key", history)
            self.assertNotIn("short-env-json", history)
            self.assertNotIn("short-json-password", history)
            self.assertNotIn("short-json-api-key", history)
            self.assertNotIn("short-json-refresh", history)
            for numeric_secret in ("123456789", "987654321", "7654321"):
                self.assertNotIn(numeric_secret, history)
            self.assertNotIn("short-escaped-unicode-secret", history)
            self.assertNotIn("short-oauth", history)
            self.assertNotIn("short-id-token", history)
            for secret in ("short-env-key", "short-env-json", "short-oauth", "short-id-token",
                           "short-private-key", "short-json-private", "short-signing-key",
                           "short-query-encryption", "short-json-password", "short-json-api-key",
                           "short-json-refresh", "123456789", "987654321", "7654321",
                           "short-escaped-unicode-secret"):
                self.assertNotIn(secret, history_text)
            self.assertTrue(archive.read("opencode/availability.json"))

    async def test_selected_export_can_append_redacted_shared_runtime_logs(self):
        owner, hass, _assistant, runtime = runtime_fixture(tool_messages())
        ha_timestamp = datetime(2026, 10, 25, 13, tzinfo=timezone.utc).astimezone().strftime(
            "%Y-%m-%d %H:%M:%S.%f")[:-3]
        with tempfile.TemporaryDirectory() as directory:
            config_root, opencode_log_dir = self.configure_instance_log_paths(
                hass, runtime, directory)
            opencode_log_dir.mkdir(parents=True)
            (opencode_log_dir / "2026-10-25T130000.log").write_text(
                "INFO   2026-10-25T13:00:00 +10ms request completed\n"
                "Authorization: Bearer short-runtime-token\n"
                "ERROR  2026-10-25T12:00:00 +0ms outside period\n"
                "WARN  timestamp format not confirmed\n",
                encoding="utf-8")
            (config_root / "home-assistant.log").write_text(
                f"{ha_timestamp} ERROR password=short-ha-password\n",
                encoding="utf-8")
            hass.async_add_executor_job = AsyncMock(
                side_effect=lambda function, *args: function(*args))
            runtime.process = SimpleNamespace(returncode=None)
            runtime.request.side_effect = [tool_messages(), {"version": "1.14.21"}]

            response = await diagnostics.DiagnosticsExportView().post(Request(
                owner, hass, data={
                    "session_id": "web-alice",
                    "from": "2026-10-25T12:30:00Z",
                    "to": "2026-10-25T13:30:00Z",
                    "include_runtime_logs": True,
                }))

        self.assertEqual(response.status, 200)
        hass.async_add_executor_job.assert_awaited_once_with(
            diagnostics._collect_instance_log_tails, config_root, runtime.root)
        runtime.request.assert_awaited_with("GET", "global/health", timeout=1)
        with zipfile.ZipFile(io.BytesIO(response.body)) as archive:
            self.assertEqual(set(archive.namelist()), {
                "manifest.json", "history.json", "history.txt", "opencode/availability.json",
                "logs/manifest.json", "opencode/logs/2026-10-25T130000.log",
                "homeassistant/home-assistant.log",
            })
            manifest = json.loads(archive.read("manifest.json"))
            logs_manifest = json.loads(archive.read("logs/manifest.json"))
            opencode_availability = json.loads(archive.read("opencode/availability.json"))
            opencode_log = archive.read("opencode/logs/2026-10-25T130000.log").decode()
            ha_log = archive.read("homeassistant/home-assistant.log").decode()
        self.assertEqual(manifest["versions"]["opencode"], "1.14.21")
        self.assertIsInstance(manifest["versions"]["frontend"], str)
        self.assertEqual(manifest["versions_scope"], "environment_at_export")
        self.assertEqual(manifest["environment_at_export"]["frontend"]["source"],
                         "installed_distribution_metadata")
        self.assertEqual(manifest["historical_session_versions"]["frontend"],
                         {"version": None, "reason": "not_recorded"})
        self.assertEqual(logs_manifest["period"]["filter_status"], "applied")
        self.assertEqual(logs_manifest["period"]["source"], "requested_period")
        self.assertIsNone(logs_manifest["period"]["reason"])
        self.assertEqual(logs_manifest["home_assistant_log_clock"]["kind"], "process_local")
        self.assertEqual(logs_manifest["home_assistant_log_clock"]["historical_binding"],
                         "unverified")
        self.assertEqual(logs_manifest["home_assistant_log_clock"]["fresh_file_per_boot"],
                         "not_verified")
        self.assertEqual(
            logs_manifest["sources"]["home_assistant_logs"]["period_timezone_binding"],
            "current_process_assumption")
        self.assertEqual(logs_manifest["scope"]["kind"], "home_assistant_instance")
        self.assertTrue(logs_manifest["scope"]["shared_across_conversations"])
        self.assertTrue(logs_manifest["scope"]["shared_across_users"])
        self.assertIsNone(logs_manifest["scope"]["session_id"])
        self.assertTrue(logs_manifest["sources"]["opencode_logs"]["available"])
        self.assertTrue(logs_manifest["sources"]["home_assistant_logs"]["available"])
        self.assertTrue(manifest["sources"]["opencode_files"]["available"])
        self.assertTrue(manifest["sources"]["home_assistant_logs"]["available"])
        self.assertEqual(opencode_availability["available"],
                         manifest["sources"]["opencode_files"]["available"])
        self.assertNotEqual(manifest["sources"]["opencode_files"]["reason"],
                            "not_exposed_by_runtime")
        self.assertEqual(logs_manifest["sources"]["opencode_logs"]["period_matched_records"], 1)
        self.assertEqual(logs_manifest["sources"]["opencode_logs"]["period_excluded_records"], 1)
        self.assertEqual(logs_manifest["sources"]["opencode_logs"]["period_unverified_records"], 1)
        self.assertNotIn("outside period", opencode_log)
        self.assertNotIn("timestamp format not confirmed", opencode_log)
        self.assertNotIn("short-runtime-token", opencode_log)
        self.assertNotIn("short-ha-password", ha_log)

    async def test_selected_export_derives_runtime_log_window_from_conversation(self):
        owner, hass, _assistant, runtime = runtime_fixture(tool_messages())
        with tempfile.TemporaryDirectory() as directory:
            _config_root, opencode_log_dir = self.configure_instance_log_paths(
                hass, runtime, directory)
            opencode_log_dir.mkdir(parents=True)
            (opencode_log_dir / "2026-10-25T130000.log").write_text(
                "INFO   2026-10-25T13:00:00 +10ms inside conversation\n"
                "ERROR  2026-10-25T13:00:02 +0ms outside conversation\n",
                encoding="utf-8")
            hass.async_add_executor_job = AsyncMock(
                side_effect=lambda function, *args: function(*args))
            runtime.process = SimpleNamespace(returncode=None)
            runtime.request.side_effect = [tool_messages(), {"version": "1.14.21"}]

            response = await diagnostics.DiagnosticsExportView().post(Request(
                owner, hass, data={
                    "session_id": "web-alice",
                    "include_runtime_logs": True,
                }))

        self.assertEqual(response.status, 200)
        with zipfile.ZipFile(io.BytesIO(response.body)) as archive:
            manifest = json.loads(archive.read("logs/manifest.json"))
            content = archive.read("opencode/logs/2026-10-25T130000.log").decode()
        self.assertEqual(manifest["period"]["source"], "selected_session_messages")
        self.assertIn("inside conversation", content)
        self.assertNotIn("outside conversation", content)

    async def test_selected_export_without_timestamps_does_not_include_shared_logs(self):
        owner, hass, _assistant, runtime = runtime_fixture([])
        with tempfile.TemporaryDirectory() as directory:
            _config_root, _opencode_log_dir = self.configure_instance_log_paths(
                hass, runtime, directory)
            hass.async_add_executor_job = AsyncMock()
            runtime.request.side_effect = [[], {"version": "1.14.21"}]

            response = await diagnostics.DiagnosticsExportView().post(Request(
                owner, hass, data={
                    "session_id": "web-alice",
                    "include_runtime_logs": True,
                }))

        self.assertEqual(response.status, 200)
        hass.async_add_executor_job.assert_not_awaited()
        with zipfile.ZipFile(io.BytesIO(response.body)) as archive:
            names = set(archive.namelist())
            self.assertIn("logs/manifest.json", names)
            self.assertFalse(any(name.startswith(("opencode/logs/", "homeassistant/"))
                                 for name in names))
            manifest = json.loads(archive.read("manifest.json"))
            logs_manifest = json.loads(archive.read("logs/manifest.json"))
        self.assertEqual(manifest["runtime_logs"]["reason"],
                         "selected_session_timestamps_unavailable")
        self.assertEqual(manifest["runtime_logs"]["period"]["filter_status"], "unavailable")
        self.assertEqual(logs_manifest["sources"]["opencode_logs"]["reason"],
                         "selected_session_timestamps_unavailable")

    async def test_selected_export_with_too_wide_session_omits_shared_logs(self):
        owner, hass, _assistant, runtime = runtime_fixture()
        messages = tool_messages()
        messages[0]["info"]["time"]["created"] = int(datetime(
            2026, 9, 1, 13, tzinfo=timezone.utc).timestamp() * 1000)
        with tempfile.TemporaryDirectory() as directory:
            self.configure_instance_log_paths(hass, runtime, directory)
            hass.async_add_executor_job = AsyncMock()
            runtime.process = SimpleNamespace(returncode=None)
            runtime.request.side_effect = [messages, {"version": "1.14.21"}]

            response = await diagnostics.DiagnosticsExportView().post(Request(
                owner, hass, data={
                    "session_id": "web-alice",
                    "include_runtime_logs": True,
                }))

        self.assertEqual(response.status, 200)
        hass.async_add_executor_job.assert_not_awaited()
        with zipfile.ZipFile(io.BytesIO(response.body)) as archive:
            names = set(archive.namelist())
            self.assertIn("logs/manifest.json", names)
            self.assertFalse(any(name.startswith(("opencode/logs/", "homeassistant/"))
                                 for name in names))
            manifest = json.loads(archive.read("manifest.json"))
            logs_manifest = json.loads(archive.read("logs/manifest.json"))
        self.assertEqual(manifest["runtime_logs"]["reason"],
                         "selected_session_window_too_wide")
        self.assertEqual(logs_manifest["period"]["filter_status"], "unavailable")

    async def test_export_includes_validated_build_identity(self):
        owner, hass, _assistant, _runtime = runtime_fixture(tool_messages())
        revision = "a" * 40
        with patch.dict(os.environ, {
            "HABAB_SOURCE_REVISION": revision,
            "HABAB_SOURCE_DIRTY": "false",
        }):
            response = await diagnostics.DiagnosticsExportView().post(
                Request(owner, hass, data={"session_id": "web-alice"}))

        self.assertEqual(response.status, 200)
        with zipfile.ZipFile(io.BytesIO(response.body)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
        self.assertEqual(manifest["build_identity"], {
            "habab_version": diagnostics.VERSION,
            "source_revision": revision,
            "source_dirty": "false",
        })
        self.assertIsNone(manifest["build_identity_reason"])
        self.assertNotIn("build_identity", manifest["unavailable_build_fields"])

    def test_build_identity_rejects_unvalidated_revision_and_dirty_values(self):
        with patch.dict(os.environ, {
            "HABAB_SOURCE_REVISION": "HEAD",
            "HABAB_SOURCE_DIRTY": "false",
        }):
            identity, reason = diagnostics._build_identity_at_export()
        self.assertIsNone(identity)
        self.assertEqual(reason, "source_revision_not_recorded")

        with patch.dict(os.environ, {
            "HABAB_SOURCE_REVISION": "b" * 64,
            "HABAB_SOURCE_DIRTY": "dirty?",
        }):
            identity, reason = diagnostics._build_identity_at_export()
        self.assertEqual(identity, {
            "habab_version": diagnostics.VERSION,
            "source_revision": "b" * 64,
            "source_dirty": "unknown",
        })
        self.assertIsNone(reason)

    async def test_foreign_selected_export_with_runtime_logs_denies_before_collection(self):
        owner, hass, _assistant, _runtime = runtime_fixture(tool_messages())
        hass.async_add_executor_job = AsyncMock()
        response = await diagnostics.DiagnosticsExportView().post(Request(
            owner, hass, data={"session_id": "web-bob", "include_runtime_logs": True}))
        self.assertEqual(response.status, 404)
        hass.async_add_executor_job.assert_not_awaited()

    async def test_export_marks_projection_truncation_and_keeps_newest_payload(self):
        messages = [
            {"info": {"role": "assistant", "time": {"created": 1792933200000,
                                                           "completed": 1792933201000}},
             "parts": [{"type": "text", "text": "Первый ответ"}]},
            {"info": {"role": "assistant", "time": {"created": 1792933300000,
                                                           "completed": 1792933301000}},
             "parts": [{"type": "tool", "tool": "habab-ha", "state": {
                 "status": "completed", "output": "LATE_EXPORT_SENTINEL"}}]},
        ]
        owner, hass, _assistant, _runtime = runtime_fixture(messages)
        with patch.object(diagnostics, "MAX_PROJECTED_EVENTS", 1):
            response = await diagnostics.DiagnosticsExportView().post(
                Request(owner, hass, data={"session_id": "web-alice"}))
        self.assertEqual(response.status, 200)
        with zipfile.ZipFile(io.BytesIO(response.body)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            self.assertTrue(manifest["projection_truncated"])
            self.assertTrue(manifest["truncated"])
            history = archive.read("history.json").decode()
            self.assertIn("LATE_EXPORT_SENTINEL", history)
            self.assertNotIn("Первый ответ", history)

    async def test_multi_session_export_budgets_newer_session_before_old_large_session(self):
        old_messages = [{
            "info": {"role": "assistant", "time": {"created": 1792933200000}},
            "parts": [{"type": "tool", "tool": "habab-ha", "state": {
                "status": "completed", "input": {"action": "devices"},
                "output": "OLD_SESSION_OUTPUT_START " + "old-data " * 500 + " OLD_SESSION_LATE_SENTINEL",
            }}],
        }]
        new_messages = [
            {"info": {"role": "user", "time": {"created": 1792933400000}},
             "parts": [{"type": "text", "text": "LATEST_SESSION_USER"}]},
            {"info": {"role": "assistant", "time": {"created": 1792933500000}},
             "parts": [{"type": "tool", "tool": "habab-ha", "state": {
                 "status": "completed", "input": {"action": "states"},
                 "output": "LATEST_SESSION_TOOL",
             }}]},
            {"info": {"role": "assistant", "time": {"created": 1792933600000}},
             "parts": [{"type": "text", "text": "LATEST_SESSION_REPLY"}]},
        ]
        owner, hass, assistant, runtime = runtime_fixture()
        assistant.web_sessions = {}
        assistant.telegram_sessions = {
            "alice:bot-entry:1001": "tg-a-old",
            "alice:bot-entry:1002": "tg-z-new",
        }
        runtime.request.side_effect = [old_messages, new_messages]

        with patch.object(diagnostics, "MAX_PROJECTED_BYTES", 160):
            response = await diagnostics.DiagnosticsExportView().post(
                Request(owner, hass, data={"from": "2026-10-25T00:00:00Z",
                                            "to": "2026-10-26T00:00:00Z",
                                            "channel": "telegram"}))

        self.assertEqual(response.status, 200)
        self.assertEqual(runtime.request.await_count, 2)
        runtime.request.assert_any_await(
            "GET", "session/tg-a-old/message?limit=60")
        runtime.request.assert_any_await(
            "GET", "session/tg-z-new/message?limit=60")
        with zipfile.ZipFile(io.BytesIO(response.body)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            history = json.loads(archive.read("history.json"))
        self.assertTrue(manifest["projection_truncated"])
        serialized = json.dumps(history)
        self.assertIn("LATEST_SESSION_USER", serialized)
        self.assertIn("LATEST_SESSION_TOOL", serialized)
        self.assertIn("LATEST_SESSION_REPLY", serialized)
        self.assertNotIn("OLD_SESSION_LATE_SENTINEL", serialized)
        self.assertEqual([item["session"]["session_id"] for item in history["sessions"]],
                         ["tg-a-old", "tg-z-new"])

    async def test_export_rejects_foreign_session_period_without_timezone_and_oversize(self):
        owner, hass, _assistant, _runtime = runtime_fixture(tool_messages())
        foreign = await diagnostics.DiagnosticsExportView().post(
            Request(owner, hass, data={"session_id": "web-bob"}))
        self.assertEqual(foreign.status, 404)
        no_selection = await diagnostics.DiagnosticsExportView().post(Request(owner, hass, data={}))
        self.assertEqual(no_selection.status, 400)
        naive = await diagnostics.DiagnosticsExportView().post(
            Request(owner, hass, data={"from": "2026-10-25T00:00:00",
                                       "to": "2026-10-26T00:00:00Z"}))
        self.assertEqual(naive.status, 400)
        large_request = Request(owner, hass, data={"session_id": "web-alice"})
        large_request.content_length = 8193
        self.assertEqual((await diagnostics.DiagnosticsExportView().post(large_request)).status, 413)
        with patch.object(diagnostics, "MAX_EXPORT_BYTES", 32):
            oversized = await diagnostics.DiagnosticsExportView().post(
                Request(owner, hass, data={"session_id": "web-alice"}))
        self.assertEqual(oversized.status, 413)

    def configure_instance_log_paths(self, hass, runtime, root):
        config_root = Path(root)
        runtime.root = config_root / ".habab-opencode"
        hass.config = SimpleNamespace(path=lambda *parts: str(config_root.joinpath(*parts)))
        hass.async_add_executor_job = AsyncMock(side_effect=lambda function, *args: function(*args))
        return config_root, runtime.root / "data" / "opencode" / "log"

    async def test_instance_log_export_is_separate_admin_only_bounded_and_redacted(self):
        owner, hass, _assistant, runtime = runtime_fixture()
        with tempfile.TemporaryDirectory() as directory:
            config_root, opencode_log_dir = self.configure_instance_log_paths(hass, runtime, directory)
            opencode_log_dir.mkdir(parents=True)
            (opencode_log_dir / "2026-10-25T120000.log").write_text(
                "INFO OPENAI_API_KEY=short-provider-key\n"
                "WARN Authorization: Bearer short-runtime-token\n"
                "https://example.invalid/?oauth_token=short-oauth-token\n"
                "Configured configured-provider-secret\n", encoding="utf-8")
            (config_root / "home-assistant.log").write_text(
                "ERROR password=short-ha-password\n", encoding="utf-8")

            response = await diagnostics.DiagnosticsRuntimeLogsExportView().post(
                Request(owner, hass, data={}))

        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, "application/zip")
        self.assertEqual(response.headers["Content-Disposition"],
                         'attachment; filename="habab-runtime-logs.zip"')
        self.assertEqual(runtime.request.await_count, 0)
        with zipfile.ZipFile(io.BytesIO(response.body)) as archive:
            self.assertEqual(set(archive.namelist()), {
                "logs/manifest.json", "opencode/logs/2026-10-25T120000.log",
                "homeassistant/home-assistant.log"})
            manifest = json.loads(archive.read("logs/manifest.json"))
            content = archive.read("opencode/logs/2026-10-25T120000.log").decode()
            ha_content = archive.read("homeassistant/home-assistant.log").decode()
            self.assertEqual(manifest["scope"]["kind"], "home_assistant_instance")
            self.assertTrue(manifest["scope"]["shared_across_conversations"])
            self.assertTrue(manifest["scope"]["shared_across_users"])
            self.assertIsNone(manifest["scope"]["session_id"])
            self.assertTrue(manifest["sources"]["opencode_logs"]["available"])
            self.assertTrue(manifest["sources"]["home_assistant_logs"]["available"])
            self.assertFalse(manifest["truncated"])
            for secret in ("short-provider-key", "short-runtime-token", "short-oauth-token",
                           "configured-provider-secret", "short-ha-password"):
                self.assertNotIn(secret, content + ha_content + archive.read("logs/manifest.json").decode())
            self.assertIn("[redacted]", content)
            self.assertIn("[redacted]", ha_content)

    async def test_instance_log_export_rejects_nonempty_scope_and_never_follows_symlinks(self):
        owner, hass, _assistant, runtime = runtime_fixture()
        with tempfile.TemporaryDirectory() as directory:
            config_root, opencode_log_dir = self.configure_instance_log_paths(hass, runtime, directory)
            opencode_log_dir.mkdir(parents=True)
            outside = config_root / "outside.log"
            outside.write_text("TRAVERSAL_SECRET=never-read", encoding="utf-8")
            (opencode_log_dir / "2026-10-25T120000.log").symlink_to(outside)

            traversal = await diagnostics.DiagnosticsRuntimeLogsExportView().post(
                Request(owner, hass, data={"path": str(outside)}))
            self.assertEqual(traversal.status, 400)
            response = await diagnostics.DiagnosticsRuntimeLogsExportView().post(
                Request(owner, hass, data={}))

        self.assertEqual(response.status, 200)
        with zipfile.ZipFile(io.BytesIO(response.body)) as archive:
            manifest = json.loads(archive.read("logs/manifest.json"))
            self.assertEqual(set(archive.namelist()), {"logs/manifest.json"})
            self.assertFalse(manifest["sources"]["opencode_logs"]["available"])
            self.assertEqual(manifest["sources"]["opencode_logs"]["reason"], "unsafe_files_skipped")
            self.assertEqual(manifest["sources"]["opencode_logs"]["unsafe_files"], 1)
            self.assertNotIn(b"TRAVERSAL_SECRET", b"".join(archive.read(name) for name in archive.namelist()))
        hass.async_add_executor_job.assert_awaited_once()

    async def test_instance_log_export_caps_files_and_marks_tail_truncation(self):
        owner, hass, _assistant, runtime = runtime_fixture()
        with tempfile.TemporaryDirectory() as directory:
            config_root, opencode_log_dir = self.configure_instance_log_paths(hass, runtime, directory)
            opencode_log_dir.mkdir(parents=True)
            for second in range(12):
                name = f"2026-10-25T1200{second:02d}.log"
                opencode_log_dir.joinpath(name).write_text(f"OLDER_{second}\n", encoding="utf-8")
            current = opencode_log_dir / "2026-10-25T130000.log"
            current.write_text("OLD_PREFIX_MUST_BE_DROPPED\n" + "fill-line\n" * 1200 + "LATEST_LOG_LINE\n",
                               encoding="utf-8")
            for index in range(5):
                config_root.joinpath("home-assistant.log" if index == 0 else f"home-assistant.log.{index}") \
                    .write_text(f"HA_ROTATION_{index}\n", encoding="utf-8")

            response = await diagnostics.DiagnosticsRuntimeLogsExportView().post(
                Request(owner, hass, data={}))

        self.assertEqual(response.status, 200)
        with zipfile.ZipFile(io.BytesIO(response.body)) as archive:
            manifest = json.loads(archive.read("logs/manifest.json"))
            opencode_names = [name for name in archive.namelist() if name.startswith("opencode/logs/")]
            ha_names = [name for name in archive.namelist() if name.startswith("homeassistant/")]
            self.assertEqual(len(opencode_names), diagnostics.MAX_OPENCODE_LOG_FILES)
            self.assertEqual(len(ha_names), diagnostics.MAX_HA_LOG_FILES)
            self.assertEqual(manifest["sources"]["opencode_logs"]["omitted_files"], 3)
            self.assertEqual(manifest["sources"]["home_assistant_logs"]["omitted_files"], 2)
            self.assertTrue(manifest["truncated"])
            current_name = "opencode/logs/2026-10-25T130000.log"
            tail = archive.read(current_name).decode()
            self.assertIn("LATEST_LOG_LINE", tail)
            self.assertNotIn("OLD_PREFIX_MUST_BE_DROPPED", tail)
            self.assertLessEqual(sum(info.file_size for info in archive.infolist()),
                                 diagnostics.MAX_EXPORT_BYTES)

    async def test_registration_exposes_only_admin_panel_and_http_views(self):
        http = SimpleNamespace(register_view=Mock())
        hass = SimpleNamespace(http=http)
        diagnostics.async_register_views(hass)
        views = [call.args[0] for call in http.register_view.call_args_list]
        self.assertEqual({view.url for view in views}, {
            "/api/habab_home/diagnostics/sessions",
            "/api/habab_home/diagnostics/sessions/{session_id}",
            "/api/habab_home/diagnostics/export",
            "/api/habab_home/diagnostics/runtime-logs/export",
        })
        self.assertTrue(all(view.requires_auth for view in views))
