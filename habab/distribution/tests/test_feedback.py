"""Local feedback receipts require an authenticated mapped owner and explicit consent."""

import json
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.components.habab_home import feedback


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

        self.assertEqual(store.data, {"records": {}})
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
