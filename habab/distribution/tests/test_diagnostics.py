"""Owner-scoped diagnostics never expose raw runtime payloads or foreign sessions."""

import io
import json
import unittest
import zipfile
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
            {"type": "text", "text": "Сейчас проверю. OPENAI_API_KEY=short-env-key private_key=short-private-key"},
            {"type": "tool", "tool": "habab-ha", "callID": "call-safe-1", "state": {
                "status": "completed",
                "input": {"action": "states", "api_key": "nested-private",
                          "refreshToken": "camel-private", "OPENAI_API_KEY": "short-env-json",
                          "ssh_private_key": "short-json-private",
                          "signing_key": "short-signing-key"},
                "output": "Authorization: Bearer runtime-private-token\n"
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
                       "short-json-private", "short-signing-key", "short-query-encryption"):
            self.assertNotIn(secret, body)
        payload = json.loads(body)
        events = payload["events"]
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
        self.assertEqual(call["input"]["api_key"], "[redacted]")
        self.assertEqual(call["input"]["ssh_private_key"], "[redacted]")
        self.assertEqual(call["input"]["signing_key"], "[redacted]")
        self.assertEqual(result["confirmation"], "runtime_result_recorded")
        self.assertEqual(result["exit_code"], 0)
        self.assertFalse(any(event["stage"] == "backend_accept" and event["available"] for event in events))
        self.assertTrue(any(event["stage"] == "backend_accept" and not event["available"] for event in events))
        self.assertFalse(payload["sources"]["native_assist_history"]["available"])

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
            self.assertNotIn("short-oauth", history)
            self.assertNotIn("short-id-token", history)
            for secret in ("short-env-key", "short-env-json", "short-oauth", "short-id-token",
                           "short-private-key", "short-json-private", "short-signing-key",
                           "short-query-encryption"):
                self.assertNotIn(secret, history_text)
            self.assertTrue(archive.read("opencode/availability.json"))

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

    async def test_registration_exposes_only_admin_panel_and_three_http_views(self):
        http = SimpleNamespace(register_view=Mock())
        hass = SimpleNamespace(http=http)
        diagnostics.async_register_views(hass)
        views = [call.args[0] for call in http.register_view.call_args_list]
        self.assertEqual({view.url for view in views}, {
            "/api/habab_home/diagnostics/sessions",
            "/api/habab_home/diagnostics/sessions/{session_id}",
            "/api/habab_home/diagnostics/export",
        })
        self.assertTrue(all(view.requires_auth for view in views))
