"""Local feedback receipts require an authenticated mapped owner and explicit consent."""

import json
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.components.habab_home import feedback
from homeassistant.components.habab_home import feedback_transport
from homeassistant.components.habab_home.feedback_transport import FeedbackDeliveryResult


class Request(dict):
    def __init__(self, owner, hass, body=None):
        super().__init__(hass_user=owner)
        self.app = {"hass": hass}
        self.content_length = None
        self.json = AsyncMock(return_value=body or {})


class FakeStore:
    def __init__(self, initial=None, fail_save=False):
        self.data = initial
        self.fail_save = fail_save
        self.async_save = AsyncMock(side_effect=self.save)

    async def async_load(self):
        return self.data

    async def save(self, data):
        if self.fail_save:
            raise OSError("synthetic store failure")
        self.data = data


def make_runtime(text="Проверяю результат", output="Ок"):
    message = {
        "info": {"role": "assistant", "providerID": "habab-deepseek", "modelID": "deepseek-chat",
                 "time": {"created": 1792933200000, "completed": 1792933201000}},
        "parts": [
            {"type": "text", "text": text},
            {"type": "tool", "tool": "habab-ha", "callID": "safe-call", "state": {
                "status": "completed", "input": {"action": "states"}, "output": output}},
        ],
    }
    runtime = SimpleNamespace(
        request=AsyncMock(return_value=[message]), clean=lambda value: value,
        visible=Mock(return_value=[]), active=None,
        diagnostics=SimpleNamespace(get=Mock(return_value=None)),
    )
    # This fake models one projected row per raw message; the single-message
    # fixture also supplies a mocked user row alongside its assistant record.
    runtime.feedback_visible = lambda messages: [
        {**row, "_raw_index": min(index, len(messages) - 1)}
        for index, row in enumerate(runtime.visible(messages))]
    assistant = SimpleNamespace(
        runtime=runtime, web_sessions={"alice": "web-alice", "bob": "web-bob"},
        telegram_sessions={"alice:entry:1": "tg-alice", "bob:entry:2": "tg-bob"},
    )
    hass = SimpleNamespace(data={"habab_home": {"assistant": assistant}})
    return hass, assistant, runtime


def body(session_id="web-alice", feedback_text="Please fix the setup explanation", key=None,
         consent=True):
    return {
        "session_id": session_id,
        "feedback": feedback_text,
        "consent": consent,
        "idempotency_key": key or str(uuid.uuid4()),
    }


class FeedbackTests(unittest.IsolatedAsyncioTestCase):
    def make_manager(self, hass, store=None):
        store = store or FakeStore()
        with patch.object(feedback, "Store", return_value=store):
            manager = feedback.FeedbackManager(hass)
        manager.store = store
        hass.data["habab_home"]["feedback"] = manager
        return manager, store

    async def test_legacy_created_status_without_issue_reference_is_not_reported_as_created(self):
        delivery = feedback.FeedbackManager._public_delivery({
            "delivery_status": "saved_on_server",
            "server_receipt_id": str(uuid.uuid4()),
            "server_issue_state": "created",
        })

        self.assertEqual(delivery["server_issue_state"], "unknown")
        self.assertEqual(delivery["delivery_error"], "issue_reference_unavailable")
        self.assertNotIn("server_issue_number", delivery)
        self.assertNotIn("server_issue_url", delivery)

    async def test_submit_requires_active_admin_explicit_consent_and_exact_schema(self):
        hass, _assistant, runtime = make_runtime()
        manager, store = self.make_manager(hass)
        view = feedback.FeedbackSubmitView()

        denied = await view.post(Request(SimpleNamespace(id="alice", is_active=True, is_admin=False), hass,
                                         body(consent=True)))
        self.assertEqual(denied.status, 403)
        no_consent = await view.post(Request(SimpleNamespace(id="alice", is_active=True, is_admin=True), hass,
                                             body(consent=False)))
        self.assertEqual(no_consent.status, 400)
        arbitrary_context = body()
        arbitrary_context["context"] = {"untrusted": "not accepted"}
        invalid = await view.post(Request(SimpleNamespace(id="alice", is_active=True, is_admin=True), hass,
                                          arbitrary_context))
        self.assertEqual(invalid.status, 400)
        runtime.request.assert_not_awaited()
        store.async_save.assert_not_awaited()
        self.assertEqual(manager.records, {})

    async def test_submit_stores_redacted_bounded_owner_mapped_context_and_truthful_receipt(self):
        hass, _assistant, runtime = make_runtime(
            text='Result source: vendor docs; password="short-private-value"',
            output='token="short-tool-secret"')
        manager, store = self.make_manager(hass)
        request = Request(SimpleNamespace(id="alice", is_active=True, is_admin=True), hass,
                          body(feedback_text='The search missed apiKey="short-user-secret"'))

        response = await feedback.FeedbackSubmitView().post(request)

        self.assertEqual(response.status, 200)
        payload = json.loads(response.body)
        self.assertEqual(payload["state"], "saved_locally")
        self.assertEqual(payload["issue_state"], "not_configured")
        self.assertFalse(payload["duplicate"])
        self.assertNotIn("issue_url", payload)
        self.assertNotIn("owner_id", payload)
        self.assertNotIn("session_id", payload)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        runtime.request.assert_awaited_once_with("GET", "session/web-alice/message?limit=60")
        store.async_save.assert_awaited_once()

        record = next(iter(manager.records.values()))
        package = json.dumps({"feedback": record["feedback"], "context": record["context"]}, ensure_ascii=False)
        for private_value in ("short-private-value", "short-tool-secret", "short-user-secret", "alice", "web-alice"):
            self.assertNotIn(private_value, package)
        self.assertEqual(record["context"]["channel"], "web")
        self.assertEqual(record["context"]["session"]["provider_models"], [
            {"provider": "habab-deepseek", "model": "deepseek-chat"}])
        self.assertIn("vendor docs", package)
        self.assertIn("[redacted]", package)

    async def test_per_answer_feedback_sends_only_selected_turn_trace(self):
        hass, _assistant, runtime = make_runtime(text="The selected answer", output="selected tool result")
        runtime.visible.return_value = [
            {"role": "user", "text": "Selected question"},
            {"role": "assistant", "text": "The selected answer"},
        ]
        manager, _store = self.make_manager(hass)
        request_body = body(feedback_text="This answer helped")
        request_body.update({
            "answer_index": 1,
            "answer_text": "The selected answer",
            "prompt_text": "Selected question",
        })

        response = await feedback.FeedbackSubmitView().post(Request(
            SimpleNamespace(id="alice", is_active=True, is_admin=True), hass, request_body))

        self.assertEqual(response.status, 200)
        payload = json.loads(response.body)
        self.assertEqual(payload["trace_status"], "available")
        self.assertEqual(payload["delivery_status"], "not_configured")
        record = next(iter(manager.records.values()))
        events = record["context"]["events"]
        self.assertEqual([event["kind"] for event in events], [
            "user_message", "tool_invocation", "tool_output", "assistant_text",
        ])
        self.assertEqual(events[0]["text"], "Selected question")
        self.assertEqual(events[-1]["text"], "The selected answer")
        self.assertIn("selected tool result", json.dumps(events, ensure_ascii=False))
        self.assertEqual(record["context"]["source"]["kind"], "owner_mapped_current_session")

    async def test_per_answer_action_explicitly_delivers_saved_package_to_receiver(self):
        hass, _assistant, runtime = make_runtime(text="The selected answer", output="tool result")
        runtime.visible.return_value = [{"role": "assistant", "text": "The selected answer"}]

        async def run_executor(function, *args):
            return function(*args)

        hass.async_add_executor_job = run_executor
        manager, _store = self.make_manager(hass)
        await manager.async_set_feedback_delivery_credential("r" * 32)
        await manager.async_set_feedback_delivery_enabled(True)
        receiver_receipt = FeedbackDeliveryResult(
            status="saved_on_server", receipt_id=str(uuid.uuid4()),
            issue_state="pending_issue", duplicate=False,
        )
        request_body = body(feedback_text="This answer helped")
        request_body.update({"answer_index": 0, "answer_text": "The selected answer", "prompt_text": ""})

        with patch.object(feedback, "send_feedback", return_value=receiver_receipt) as sender:
            response = await feedback.FeedbackSubmitView().post(Request(
                SimpleNamespace(id="alice", is_active=True, is_admin=True), hass, request_body))

        payload = json.loads(response.body)
        self.assertEqual(payload["delivery_status"], "saved_on_server")
        package = sender.call_args.args[0]
        self.assertEqual(package["feedback"], "This answer helped")
        self.assertEqual(package["context"]["events"][-1]["text"], "The selected answer")
        self.assertNotIn("correlation_id", json.dumps(package["context"]))

    async def test_native_answer_feedback_marks_missing_runtime_trace(self):
        hass, _assistant, runtime = make_runtime(text="Different OpenCode answer")
        runtime.visible.return_value = [
            {"role": "user", "text": "Native question with token=private-native-token"},
            {"role": "assistant", "text": "Native answer with password=private-native-password"},
        ]
        runtime.visible.side_effect = lambda messages: (
            [{"role": "assistant", "text": "Different OpenCode answer"}] if messages else []
        )
        manager, _store = self.make_manager(hass)
        request_body = body(feedback_text="This answer did not help")
        request_body.update({
            "answer_index": 1,
            "answer_text": "Native answer with password=private-native-password",
            "prompt_text": "Native question with token=private-native-token",
        })

        response = await feedback.FeedbackSubmitView().post(Request(
            SimpleNamespace(id="alice", is_active=True, is_admin=True), hass, request_body))

        payload = json.loads(response.body)
        self.assertEqual(payload["trace_status"], "unavailable")
        self.assertEqual(payload["delivery_status"], "not_configured")
        record = next(iter(manager.records.values()))
        rendered = json.dumps(record["context"], ensure_ascii=False)
        for secret in ("private-native-token", "private-native-password"):
            self.assertNotIn(secret, rendered)
        self.assertIn("trace_not_recorded_by_runtime", rendered)
        self.assertIn("Native answer", rendered)

    async def test_voice_answer_trace_survives_different_raw_and_visible_history_windows(self):
        from homeassistant.components.habab_home.setup_agent import SetupAgent
        from homeassistant.components.habab_home.voice_history import encode_voice_event
        hass, _assistant, runtime = make_runtime()
        projector = object.__new__(SetupAgent)
        projector.hass = SimpleNamespace(config_entries=SimpleNamespace(async_entries=lambda _: []))
        projector._runtime_secrets = set()
        projector.password = "runtime-password"
        projector.control_token = "runtime-control"
        raw = [{"info": {"role": "user", "id": "hidden-" + str(index)}, "parts": [{
            "type": "text", "synthetic": True, "text": "runtime context"}]} for index in range(8)]
        dialogue = [("user" if index % 2 == 0 else "assistant", "Recorded " + str(index))
                    for index in range(50)] + [("user", "Selected voice question"),
                                               ("assistant", "Selected voice answer")]
        for index, (role, text) in enumerate(dialogue):
            encoded, _digest, metadata = encode_voice_event("voice-" + str(index), role, text)
            raw.append({"info": {"id": "voice-raw-" + str(index), "role": "user"}, "parts": [{
                "type": "text", "synthetic": True, "text": encoded, "metadata": metadata}]})
        self.assertEqual(len(raw), 60)
        self.assertEqual(len(projector.visible(raw)), 52)
        runtime.request.return_value = raw
        runtime.visible = projector.visible
        runtime.feedback_visible = projector.feedback_visible
        manager, _store = self.make_manager(hass)
        request_body = body(feedback_text="Please improve this recorded voice answer")
        request_body.update(answer_index=59, answer_text="Selected voice answer",
                            prompt_text="Selected voice question")
        response = await feedback.FeedbackSubmitView().post(Request(
            SimpleNamespace(id="alice", is_active=True, is_admin=True), hass, request_body))
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.body)["trace_status"], "available")
        events = next(iter(manager.records.values()))["context"]["events"]
        self.assertEqual(events[0]["text"], "Selected voice question")
        self.assertEqual(events[-1]["text"], "Selected voice answer")
        self.assertEqual(events[-1]["confirmation"], "runtime_answer_recorded")
        self.assertFalse(any(event.get("stage") == "trace_unavailable" for event in events))

    def test_shifted_answer_binding_requires_unique_exact_recorded_question_and_answer(self):
        runtime = SimpleNamespace(feedback_visible=lambda messages: [
            {**row, "_raw_index": index} for index, row in enumerate(messages)])
        first = [{"role": "user", "text": "Question one"},
                 {"role": "assistant", "text": "Same answer"}]
        second = [{"role": "user", "text": "Question two"},
                  {"role": "assistant", "text": "Same answer"}]
        self.assertEqual(feedback._selected_runtime_answer(
            runtime, first + second, 59, "Same answer", "Question two"), ("Same answer", 3, "Question two"))
        for prompt in ("Missing question", "", "question two"):
            self.assertIsNone(feedback._selected_runtime_answer(
                runtime, first + second, 59, "Same answer", prompt))
        self.assertIsNone(feedback._selected_runtime_answer(
            runtime, first + first, 59, "Same answer", "Question one"))
        self.assertIsNone(feedback._selected_runtime_answer(
            runtime, first + second, 1, "Same answer", "Missing question"))

    async def test_first_visible_answer_uses_one_projection_and_its_origin_in_2000_raw_records(self):
        from homeassistant.components.habab_home.setup_agent import SetupAgent
        from homeassistant.components.habab_home.voice_history import encode_voice_event
        hass, _assistant, runtime = make_runtime()
        projector = object.__new__(SetupAgent)
        projector.hass = SimpleNamespace(config_entries=SimpleNamespace(async_entries=lambda _: []))
        projector._runtime_secrets = set()
        projector.password = "runtime-password"
        projector.control_token = "runtime-control"
        raw = [{"info": {"role": "user", "id": "raw-" + str(index),
                         "time": {"created": 1792933200000 + index}}, "parts": [{
                    "type": "text", "synthetic": True, "text": "runtime context"}]}
               for index in range(2000)]
        for index in range(301):
            role = "user" if index % 2 == 0 else "assistant"
            text = ("Question " if role == "user" else "Answer ") + str(index // 2)
            encoded, _digest, metadata = encode_voice_event("event-" + str(index), role, text)
            raw[index * 5]["parts"] = [{"type": "text", "synthetic": True,
                                       "text": encoded, "metadata": metadata}]
        public = projector.visible(raw)
        self.assertEqual(len(public), 60)
        self.assertEqual(public[0], {"role": "assistant", "text": "Answer 120"})
        self.assertTrue(all("_raw_index" not in row for row in public))
        self.assertNotIn(public[0], projector.visible(raw[-60:] ))
        runtime.feedback_visible = Mock(wraps=projector.feedback_visible)
        runtime.visible = Mock(side_effect=AssertionError("feedback must not reproject raw prefixes"))
        runtime.request.return_value = raw
        manager, _store = self.make_manager(hass)
        request_body = body(feedback_text="Please check the first displayed answer")
        request_body.update(answer_index=0, answer_text="Answer 120", prompt_text="")
        response = await feedback.FeedbackSubmitView().post(Request(
            SimpleNamespace(id="alice", is_active=True, is_admin=True), hass, request_body))
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.body)["trace_status"], "available")
        runtime.feedback_visible.assert_called_once_with(raw)
        runtime.request.assert_awaited_once_with("GET", "session/web-alice/message?limit=2000")
        context = next(iter(manager.records.values()))["context"]
        self.assertEqual(context["events"][0]["text"], "Question 120")
        self.assertEqual(context["events"][-1]["text"], "Answer 120")
        self.assertEqual(context["source"]["history_message_limit"], feedback.MAX_MESSAGES)
        self.assertLessEqual(len(json.dumps(context).encode()), feedback.MAX_CONTEXT_BYTES)
        self.assertIsNotNone(feedback_transport._context_bytes(context))
        self.assertNotIn("Answer 149", json.dumps(context))

    async def test_feedback_materializes_current_owner_pending_voice_before_history_lookup(self):
        hass, assistant, runtime = make_runtime()
        raw = [
            {"info": {"role": "user"}, "parts": [{"type": "text", "text": "Voice question"}]},
            {"info": {"role": "assistant"}, "parts": [{"type": "text", "text": "Voice answer"}]},
        ]
        order = []

        async def prepare(owner, session_id):
            self.assertEqual((owner, session_id), ("alice", "web-alice"))
            order.append("materialized")

        async def read(*args):
            self.assertEqual(order, ["materialized"])
            return raw

        assistant.prepare_feedback_history = AsyncMock(side_effect=prepare)
        runtime.request = AsyncMock(side_effect=read)
        runtime.visible.side_effect = lambda messages: [
            {"role": row["info"]["role"], "text": row["parts"][0]["text"]} for row in messages]
        manager, _store = self.make_manager(hass)
        request_body = body(feedback_text="Please check this voice response")
        request_body.update(answer_index=1, answer_text="Voice answer", prompt_text="Voice question")
        response = await feedback.FeedbackSubmitView().post(Request(
            SimpleNamespace(id="alice", is_active=True, is_admin=True), hass, request_body))
        self.assertEqual(json.loads(response.body)["trace_status"], "available")
        assistant.prepare_feedback_history.assert_awaited_once_with("alice", "web-alice")
        self.assertEqual(next(iter(manager.records.values()))["context"]["events"][-1]["text"], "Voice answer")

    async def test_native_answer_feedback_still_saves_when_opencode_history_is_unavailable(self):
        hass, _assistant, _runtime = make_runtime()
        manager, _store = self.make_manager(hass)
        request_body = body(feedback_text="This answer helped")
        request_body.update({"answer_index": 0, "answer_text": "Native answer", "prompt_text": "Native question"})

        with patch.object(feedback, "_load_session_messages", side_effect=OSError("runtime offline")):
            response = await feedback.FeedbackSubmitView().post(Request(
                SimpleNamespace(id="alice", is_active=True, is_admin=True), hass, request_body))

        self.assertEqual(response.status, 200)
        payload = json.loads(response.body)
        self.assertEqual(payload["trace_status"], "unavailable")
        record = next(iter(manager.records.values()))
        self.assertIn("Native answer", json.dumps(record["context"], ensure_ascii=False))

    async def test_selected_older_answer_is_scoped_before_projection_and_answer_survives_truncation(self):
        hass, _assistant, runtime = make_runtime()

        def message(role, text, *, tool_output=None):
            parts = [{"type": "text", "text": text}]
            if tool_output is not None:
                parts.append({"type": "tool", "tool": "habab-ha", "callID": "scoped-call", "state": {
                    "status": "completed", "input": {"action": "states"}, "output": tool_output,
                }})
            return {"info": {"role": role, "providerID": "habab-deepseek", "modelID": "deepseek-chat",
                              "time": {"created": 1792933200000, "completed": 1792933201000}},
                    "parts": parts}

        raw = [
            message("user", "Old question"),
            message("assistant", "Old answer", tool_output="old private trace"),
            message("user", "Selected question"),
            message("assistant", "The selected answer explains how to connect the device. " * 300,
                    tool_output="selected tool result"),
        ]

        def visible(messages):
            return [{"role": item["info"]["role"], "text": "\n".join(
                part.get("text", "") for part in item.get("parts", []) if part.get("type") == "text"
            )} for item in messages]

        runtime.visible.side_effect = visible
        manager, _store = self.make_manager(hass)
        request_body = body(feedback_text="This answer helped")
        request_body.update({"answer_index": 3, "answer_text": visible(raw)[3]["text"], "prompt_text": "Selected question"})

        with patch.object(feedback, "_load_session_messages", return_value=(runtime, raw, False)):
            response = await feedback.FeedbackSubmitView().post(Request(
                SimpleNamespace(id="alice", is_active=True, is_admin=True), hass, request_body))

        payload = json.loads(response.body)
        self.assertEqual(payload["trace_status"], "truncated")
        record = next(iter(manager.records.values()))
        context = record["context"]
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False).encode()), feedback.MAX_CONTEXT_BYTES)
        events = context["events"]
        self.assertEqual(events[0]["id"], "selected.prompt")
        self.assertEqual(events[0]["text"], "Selected question")
        self.assertTrue(events[-1]["text"].startswith("The selected answer"))
        rendered = json.dumps(events, ensure_ascii=False)
        self.assertIn("selected tool result", rendered)
        self.assertNotIn("old private trace", rendered)

    async def test_selected_turn_keeps_canonical_prompt_when_large_tool_trace_evicts_it(self):
        hass, _assistant, runtime = make_runtime()
        prompt = ('Canonical question "quoted" with newline\n' * 80)
        answer = ('Selected answer "quoted" with newline\n' * 850)
        tool_parts = [{
            "type": "tool", "tool": "habab-ha", "callID": f"large-call-{index}", "state": {
                "status": "completed", "input": {"action": "states"},
                "output": (f"Tool trace {index}: " + "trace-value " * 150),
            },
        } for index in range(8)]
        raw = [
            {"info": {"role": "user", "time": {"created": 1792933200000}},
             "parts": [{"type": "text", "text": prompt}]},
            {"info": {"role": "assistant", "providerID": "habab-deepseek", "modelID": "deepseek-chat",
                       "time": {"created": 1792933201000, "completed": 1792933202000}},
             "parts": [{"type": "text", "text": answer}, *tool_parts]},
        ]

        def visible(messages):
            return [{"role": item["info"]["role"], "text": "\n".join(
                part.get("text", "") for part in item.get("parts", []) if part.get("type") == "text"
            )} for item in messages]

        runtime.visible.side_effect = visible
        old_trace_budget = feedback.ProjectionBudget(max_bytes=6 * 1024, max_events=16)
        old_trace_projection = feedback.project_messages(
            runtime, [raw[0], {**raw[1], "parts": tool_parts}], "web", budget=old_trace_budget,
        )
        self.assertFalse(any(event.get("kind") == "user_message" for event in old_trace_projection))
        manager, _store = self.make_manager(hass)
        request_body = body(feedback_text="This answer helped")
        request_body.update({"answer_index": 1, "answer_text": answer, "prompt_text": prompt})

        with patch.object(feedback, "_load_session_messages", return_value=(runtime, raw, False)):
            response = await feedback.FeedbackSubmitView().post(Request(
                SimpleNamespace(id="alice", is_active=True, is_admin=True), hass, request_body))

        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.body)["trace_status"], "truncated")
        record = next(iter(manager.records.values()))
        context = record["context"]
        events = context["events"]
        self.assertEqual(events[0]["id"], "selected.prompt")
        self.assertEqual(events[0]["kind"], "user_message")
        self.assertTrue(events[0]["text"].startswith('Canonical question "quoted" with newline\n'))
        self.assertLess(len(events[0]["text"]), len(prompt))
        self.assertEqual(events[0]["confirmation"], "runtime_message_recorded")
        self.assertEqual(sum(event.get("kind") == "user_message" for event in events), 1)
        self.assertTrue(any(event.get("kind") == "tool_invocation" for event in events))
        self.assertEqual(events[-1]["id"], "selected.answer")
        self.assertTrue(events[-1]["text"].startswith('Selected answer "quoted" with newline\n'))
        self.assertLess(len(events[-1]["text"]), len(answer))
        self.assertLessEqual(len(feedback_transport._context_bytes(context)), feedback_transport.MAX_CONTEXT_BYTES)
        self.assertIsNotNone(feedback_transport._request_body({
            "feedback": record["feedback"],
            "context": context,
            "idempotency_key": record["idempotency_key"],
        }))

    async def test_selected_quoted_answer_is_clipped_to_receiver_serialized_context_budget(self):
        hass, _assistant, runtime = make_runtime()
        answer = ('Quoted "value" with newline\n' * 850)

        def message(role, text, *, tool_output=None):
            parts = [{"type": "text", "text": text}]
            if tool_output is not None:
                parts.append({"type": "tool", "tool": "habab-ha", "callID": "quoted-call", "state": {
                    "status": "completed", "input": {"action": "states"}, "output": tool_output,
                }})
            return {"info": {"role": role, "providerID": "habab-deepseek", "modelID": "deepseek-chat",
                              "time": {"created": 1792933200000, "completed": 1792933201000}},
                    "parts": parts}

        raw = [
            message("user", "Quoted answer question"),
            message("assistant", answer, tool_output=('Tool "trace"\n' * 3000)),
        ]

        def visible(messages):
            return [{"role": item["info"]["role"], "text": "\n".join(
                part.get("text", "") for part in item.get("parts", []) if part.get("type") == "text"
            )} for item in messages]

        runtime.visible.side_effect = visible
        manager, _store = self.make_manager(hass)
        request_body = body(feedback_text="This answer helped")
        request_body.update({"answer_index": 1, "answer_text": answer, "prompt_text": "Quoted answer question"})

        with patch.object(feedback, "_load_session_messages", return_value=(runtime, raw, False)):
            response = await feedback.FeedbackSubmitView().post(Request(
                SimpleNamespace(id="alice", is_active=True, is_admin=True), hass, request_body))

        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.body)["trace_status"], "truncated")
        record = next(iter(manager.records.values()))
        context = record["context"]
        self.assertTrue(context["events"][-1]["text"].startswith('Quoted "value" with newline\n'))
        self.assertLessEqual(len(feedback_transport._context_bytes(context)), feedback_transport.MAX_CONTEXT_BYTES)
        package = {
            "feedback": record["feedback"],
            "context": context,
            "idempotency_key": record["idempotency_key"],
        }
        self.assertIsNotNone(feedback_transport._request_body(package))

    async def test_idempotency_returns_existing_receipt_and_rejects_changed_content(self):
        hass, _assistant, runtime = make_runtime()
        manager, store = self.make_manager(hass)
        owner = SimpleNamespace(id="alice", is_active=True, is_admin=True)
        key = str(uuid.uuid4())
        first = await feedback.FeedbackSubmitView().post(
            Request(owner, hass, body(feedback_text="same feedback", key=key)))
        first_payload = json.loads(first.body)
        repeated = await feedback.FeedbackSubmitView().post(
            Request(owner, hass, body(feedback_text="same feedback", key=key)))
        repeated_payload = json.loads(repeated.body)
        self.assertEqual(repeated_payload["receipt_id"], first_payload["receipt_id"])
        self.assertTrue(repeated_payload["duplicate"])
        runtime.request.assert_awaited_once()
        store.async_save.assert_awaited_once()

        conflict = await feedback.FeedbackSubmitView().post(
            Request(owner, hass, body(feedback_text="changed feedback", key=key)))
        self.assertEqual(conflict.status, 409)
        self.assertEqual(len(manager.records), 1)

    async def test_foreign_or_unmapped_session_cannot_be_submitted_or_read(self):
        hass, _assistant, runtime = make_runtime()
        manager, _store = self.make_manager(hass)
        owner = SimpleNamespace(id="alice", is_active=True, is_admin=True)
        foreign = await feedback.FeedbackSubmitView().post(Request(owner, hass, body(session_id="web-bob")))
        self.assertEqual(foreign.status, 404)
        self.assertEqual(manager.records, {})
        runtime.request.assert_not_awaited()

        accepted = await feedback.FeedbackSubmitView().post(Request(owner, hass, body()))
        receipt_id = json.loads(accepted.body)["receipt_id"]
        other_owner = SimpleNamespace(id="bob", is_active=True, is_admin=True)
        hidden = await feedback.FeedbackReceiptView().get(Request(other_owner, hass), receipt_id)
        self.assertEqual(hidden.status, 404)
        own = await feedback.FeedbackReceiptView().get(Request(owner, hass), receipt_id)
        self.assertEqual(own.status, 200)
        own_payload = json.loads(own.body)
        self.assertEqual(own_payload["state"], "saved_locally")
        self.assertNotIn("feedback", own_payload)
        self.assertNotIn("context", own_payload)

    async def test_owner_receipt_returns_only_safe_delivery_fields_and_validated_issue_link(self):
        hass, _assistant, _runtime = make_runtime()
        manager, _store = self.make_manager(hass)
        owner = SimpleNamespace(id="alice", is_active=True, is_admin=True)
        accepted = await feedback.FeedbackSubmitView().post(Request(owner, hass, body()))
        receipt_id = json.loads(accepted.body)["receipt_id"]
        record = manager.records[receipt_id]
        server_receipt_id = str(uuid.uuid4())
        issue_url = "https://github.com/example/project/issues/123"
        record.update(delivery_status="saved_on_server", server_receipt_id=server_receipt_id,
                      server_issue_state="created", server_issue_number=123, server_issue_url=issue_url,
                      api_key="private-provider-key", delivery_token="private-delivery-token")
        response = await feedback.FeedbackReceiptView().get(Request(owner, hass), receipt_id)
        payload = json.loads(response.body)
        self.assertEqual(payload["server_receipt_id"], server_receipt_id)
        self.assertEqual(payload["server_issue_url"], issue_url)
        self.assertEqual(payload["server_issue_number"], 123)
        self.assertEqual(payload["delivery_status"], "saved_on_server")
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertTrue(set(payload) <= {"receipt_id", "state", "issue_state", "created_at", "duplicate",
            "trace_status", "delivery_status", "delivery_error", "server_receipt_id", "server_issue_state",
            "server_issue_number", "server_issue_url", "server_duplicate"})
        for value in ("private-provider-key", "private-delivery-token", record["feedback"]):
            self.assertNotIn(value, response.text)
        record["server_issue_url"] = "https://github.com/example/project/issues/123?token=secret"
        invalid = await feedback.FeedbackReceiptView().get(Request(owner, hass), receipt_id)
        invalid_payload = json.loads(invalid.body)
        self.assertEqual(invalid_payload["server_issue_state"], "unknown")
        self.assertNotIn("server_issue_url", invalid_payload)
        self.assertNotIn("server_issue_number", invalid_payload)

    async def test_store_failure_does_not_return_receipt_or_report_saved(self):
        hass, _assistant, runtime = make_runtime()
        manager, store = self.make_manager(hass, FakeStore(fail_save=True))
        response = await feedback.FeedbackSubmitView().post(
            Request(SimpleNamespace(id="alice", is_active=True, is_admin=True), hass, body()))
        self.assertEqual(response.status, 503)
        self.assertEqual(json.loads(response.body)["message"], "feedback_unavailable")
        self.assertEqual(manager.records, {})
        self.assertTrue(store.async_save.awaited)
        runtime.request.assert_awaited_once()

    async def test_context_projection_stops_at_configured_budget(self):
        hass, _assistant, runtime = make_runtime(text="x" * 40000)
        manager, _store = self.make_manager(hass)
        response = await feedback.FeedbackSubmitView().post(
            Request(SimpleNamespace(id="alice", is_active=True, is_admin=True), hass, body()))
        self.assertEqual(response.status, 200)
        record = next(iter(manager.records.values()))
        self.assertLessEqual(len(json.dumps(record["context"], ensure_ascii=False).encode()),
                             feedback.MAX_CONTEXT_BYTES + 4096)
        self.assertTrue(record["context"]["source"]["truncated"])
        runtime.request.assert_awaited_once()

    async def test_failed_expired_record_purge_is_retried_before_receipt_is_hidden(self):
        hass, _assistant, _runtime = make_runtime()
        receipt_id = str(uuid.uuid4())
        created = datetime(2026, 10, 25, tzinfo=timezone.utc)
        record = {
            "receipt_id": receipt_id, "owner_id": "alice",
            "idempotency_key": str(uuid.uuid4()), "digest": "d" * 64,
            "created_at": created.isoformat().replace("+00:00", "Z"),
            "state": "saved_locally", "issue_state": "not_configured",
            "feedback": "old fixture", "context": {"events": []},
        }
        store = FakeStore({"records": {receipt_id: record}})
        manager, store = self.make_manager(hass, store)

        with patch.object(feedback, "_now", return_value=created + timedelta(days=1)):
            self.assertIs(await manager.async_receipt("alice", receipt_id), record)
        store.fail_save = True
        with patch.object(feedback, "_now", return_value=created + feedback.RETENTION + timedelta(seconds=1)):
            with self.assertRaises(OSError):
                await manager.async_receipt("alice", receipt_id)
            self.assertIn(receipt_id, manager.records)
            store.fail_save = False
            self.assertIsNone(await manager.async_receipt("alice", receipt_id))

        self.assertEqual(store.data, {
            "records": {},
            "feedback_delivery": {"enabled": False, "token": None},
        })
        self.assertEqual(store.async_save.await_count, 2)

    async def test_registration_installs_private_manager_and_both_authenticated_views(self):
        hass, _assistant, _runtime = make_runtime()
        registrations = []
        hass.http = SimpleNamespace(register_view=registrations.append)
        store = FakeStore()
        with patch.object(feedback, "Store", return_value=store):
            feedback.async_register_views(hass)

        self.assertIsInstance(hass.data["habab_home"]["feedback"], feedback.FeedbackManager)
        self.assertEqual([view.url for view in registrations], [
            "/api/habab_home/feedback",
            "/api/habab_home/feedback/{receipt_id}",
        ])
        self.assertTrue(all(view.requires_auth for view in registrations))

    async def test_delivery_credential_and_enable_state_stay_private_and_migrate_legacy_store(self):
        hass, _assistant, _runtime = make_runtime()
        legacy_record = {
            "receipt_id": str(uuid.uuid4()), "owner_id": "alice",
            "idempotency_key": str(uuid.uuid4()), "digest": "d" * 64,
            "created_at": feedback._iso(feedback._now()), "state": "saved_locally",
            "feedback": "legacy", "context": {},
        }
        store = FakeStore({"records": {legacy_record["receipt_id"]: legacy_record}})
        manager, store = self.make_manager(hass, store)

        self.assertEqual(await manager.async_feedback_delivery_status(), {
            "enabled": False, "credential_configured": False, "ready": False,
        })
        token = "T" * 40
        self.assertEqual(await manager.async_set_feedback_delivery_credential(token), {
            "enabled": False, "credential_configured": True, "ready": False,
        })
        self.assertEqual(await manager.async_set_feedback_delivery_enabled(True), {
            "enabled": True, "credential_configured": True, "ready": True,
        })
        self.assertEqual(await manager.async_feedback_delivery_status(), {
            "enabled": True, "credential_configured": True, "ready": True,
        })
        self.assertIn(legacy_record["receipt_id"], store.data["records"])
        self.assertEqual(store.data["feedback_delivery"], {"enabled": True, "token": token})

        self.assertEqual(await manager.async_clear_feedback_delivery_credential(), {
            "enabled": False, "credential_configured": False, "ready": False,
        })
        self.assertEqual(store.data["feedback_delivery"], {"enabled": False, "token": None})
