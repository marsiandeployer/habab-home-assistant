"""The chat feedback action saves only a unique current owner-mapped session."""

import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from homeassistant.components.habab_home import feedback, feedback_action
from homeassistant.components.habab_home.feedback import FeedbackManager
from homeassistant.components.habab_home.feedback_transport import FeedbackDeliveryResult
from homeassistant.components.habab_home.setup_actions import execute


class FakeStore:
    def __init__(self):
        self.data = None
        self.async_save = AsyncMock(side_effect=self.save)

    async def async_load(self):
        return self.data

    async def save(self, data):
        self.data = data


def make_context(channel="web"):
    return (
        {
            "channel": channel,
            "provider_models": [],
            "created_at": None,
            "updated_at": None,
            "status": "completed",
            "processing_duration_ms": None,
            "message_count": 2,
            "events_truncated": False,
        },
        [{"kind": "user_message", "text": "Это неудобно"}],
        [],
    )


class FeedbackActionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.hass = SimpleNamespace(data={"habab_home": {}})

        async def run_executor(job):
            return job()

        self.hass.async_add_executor_job = AsyncMock(side_effect=run_executor)
        self.assistant = SimpleNamespace(
            web_sessions={"alice": "web-alice"},
            telegram_sessions={"alice:entry:1": "tg-alice"},
            runtime=SimpleNamespace(clean=lambda value: value),
        )
        self.setup_agent = SimpleNamespace(active=("alice", "web-alice"))
        self.hass.data["habab_home"]["assistant"] = self.assistant
        self.store = FakeStore()
        with patch.object(feedback, "Store", return_value=self.store):
            self.feedback_manager = FeedbackManager(self.hass)
        self.hass.data["habab_home"]["feedback"] = self.feedback_manager
        self.body = {
            "action": "save_feedback",
            "feedback": "Это неудобно",
            "consent": True,
            "idempotency_key": str(uuid.uuid4()),
        }

    async def test_dispatch_saves_redacted_local_receipt_idempotently(self):
        with patch.object(
            feedback_action, "_session_data", AsyncMock(return_value=make_context())
        ) as load_context, patch.object(
            feedback_action, "_redact_text", return_value="Очищенный отзыв"
        ) as redact:
            first = await execute(self.hass, self.setup_agent, "alice", self.body)
            second = await execute(self.hass, self.setup_agent, "alice", self.body)

        self.assertEqual(first["state"], "saved_locally")
        self.assertFalse(first["duplicate"])
        self.assertEqual(second["receipt_id"], first["receipt_id"])
        self.assertTrue(second["duplicate"])
        record = self.store.data["records"][first["receipt_id"]]
        self.assertEqual(record["feedback"], "Очищенный отзыв")
        self.assertEqual(record["context"]["channel"], "web")
        self.assertEqual(
            record["context"]["source"]["kind"], "owner_mapped_current_session"
        )
        self.assertEqual(load_context.await_count, 1)
        redact.assert_called_once_with(self.assistant.runtime, "Это неудобно", limit=2000)

    async def test_model_cannot_choose_session_or_omit_explicit_consent(self):
        with patch.object(
            feedback_action, "_session_data", AsyncMock(return_value=make_context())
        ) as load_context:
            for body in (
                {**self.body, "session_id": "tg-alice"},
                {**self.body, "consent": False},
                {**self.body, "send_to_habab": 1},
            ):
                with self.subTest(body=body):
                    result = await execute(self.hass, self.setup_agent, "alice", body)
                    self.assertEqual(result["error"], "invalid_feedback")
        load_context.assert_not_awaited()
        self.assertIsNone(self.store.data)

    async def test_current_mapped_telegram_session_saves_locally(self):
        self.setup_agent.active = ("alice", "tg-alice")
        with patch.object(
            feedback_action, "_session_data",
            AsyncMock(return_value=make_context("telegram")),
        ) as load_context:
            result = await execute(self.hass, self.setup_agent, "alice", self.body)

        self.assertEqual(result["state"], "saved_locally")
        self.assertEqual(result["issue_state"], "not_configured")
        self.assertFalse(result["duplicate"])
        record = self.store.data["records"][result["receipt_id"]]
        self.assertEqual(record["context"]["channel"], "telegram")
        self.assertEqual(
            record["context"]["source"]["kind"], "owner_mapped_current_session"
        )
        load_context.assert_awaited_once()

    async def test_local_only_and_unconfigured_explicit_delivery_never_call_sender(self):
        with patch.object(
            feedback_action, "_session_data", AsyncMock(return_value=make_context())
        ), patch.object(feedback, "send_feedback") as sender:
            local = await execute(self.hass, self.setup_agent, "alice", self.body)
            remote_requested = await execute(
                self.hass, self.setup_agent, "alice",
                {**self.body, "idempotency_key": str(uuid.uuid4()), "send_to_habab": True},
            )
            await self.feedback_manager.async_set_feedback_delivery_credential("C" * 40)
            disabled = await execute(
                self.hass, self.setup_agent, "alice",
                {**self.body, "idempotency_key": str(uuid.uuid4()), "send_to_habab": True},
            )

        self.assertEqual(local, {
            "receipt_id": local["receipt_id"], "state": "saved_locally",
            "issue_state": "not_configured", "created_at": local["created_at"],
            "duplicate": False,
        })
        self.assertEqual(remote_requested["state"], "saved_locally")
        self.assertEqual(set(remote_requested), set(local))
        self.assertEqual(remote_requested["issue_state"], "not_configured")
        sender.assert_not_called()
        self.hass.async_add_executor_job.assert_not_awaited()

        self.assertEqual(set(disabled), set(local))
        self.assertEqual(disabled["issue_state"], "not_configured")
        sender.assert_not_called()
        self.hass.async_add_executor_job.assert_not_awaited()

    async def test_explicit_delivery_stores_only_sanitized_server_receipt_and_short_circuits(self):
        token = "T" * 40
        await self.feedback_manager.async_set_feedback_delivery_credential(token)
        await self.feedback_manager.async_set_feedback_delivery_enabled(True)
        package = self.body | {"send_to_habab": True}
        with patch.object(
            feedback_action, "_session_data", AsyncMock(return_value=make_context())
        ), patch.object(
            feedback_action, "_redact_text", return_value="Очищенный отзыв"
        ), patch.object(
            feedback, "send_feedback", return_value=FeedbackDeliveryResult(
                status="saved_on_server", receipt_id="server-r-1",
                issue_state="pending_issue", duplicate=False, attempts=1,
            ),
        ) as sender, patch.object(
            feedback, "get_feedback_status", return_value=FeedbackDeliveryResult(
                status="saved_on_server", receipt_id="server-r-1",
                issue_state="pending_issue", duplicate=False, attempts=1,
            ),
        ) as status_reader:
            result = await execute(self.hass, self.setup_agent, "alice", package)
            repeat = await execute(self.hass, self.setup_agent, "alice", package)

        self.assertEqual(result["delivery_status"], "saved_on_server")
        self.assertEqual(result["server_issue_state"], "pending_issue")
        self.assertNotIn(token, repr(result))
        self.assertEqual(repeat["delivery_status"], "saved_on_server")
        self.assertTrue(repeat["duplicate"])
        sender.assert_called_once()
        status_reader.assert_called_once_with("server-r-1", token)
        call = sender.call_args
        sent_package, sent_token = call.args
        self.assertEqual(sent_token, token)
        self.assertEqual(sent_package["feedback"], "Очищенный отзыв")
        self.assertEqual(sent_package["idempotency_key"], package["idempotency_key"])
        record = self.store.data["records"][result["receipt_id"]]
        self.assertEqual(record["delivery_status"], "saved_on_server")
        self.assertEqual(record["server_issue_state"], "pending_issue")
        self.assertNotIn("token", record)

    async def test_same_key_refresh_returns_created_only_with_issue_reference(self):
        token = "V" * 40
        await self.feedback_manager.async_set_feedback_delivery_credential(token)
        await self.feedback_manager.async_set_feedback_delivery_enabled(True)
        package = self.body | {"send_to_habab": True}
        issue_url = "https://github.com/habab-test/feedback-test/issues/42"
        created = FeedbackDeliveryResult(
            status="saved_on_server", receipt_id="server-r-3", issue_state="created",
            issue_number=42, issue_url=issue_url, duplicate=False, attempts=1,
        )
        with patch.object(
            feedback_action, "_session_data", AsyncMock(return_value=make_context())
        ), patch.object(
            feedback_action, "_redact_text", return_value="Очищенный отзыв"
        ), patch.object(
            feedback, "send_feedback", return_value=FeedbackDeliveryResult(
                status="saved_on_server", receipt_id="server-r-3",
                issue_state="pending_issue", duplicate=False, attempts=1,
            ),
        ) as sender, patch.object(
            feedback, "get_feedback_status", return_value=created,
        ) as status_reader:
            first = await execute(self.hass, self.setup_agent, "alice", package)
            repeat = await execute(self.hass, self.setup_agent, "alice", package)

        self.assertEqual(first["server_issue_state"], "pending_issue")
        self.assertEqual(repeat["server_issue_state"], "created")
        self.assertEqual(repeat["server_issue_number"], 42)
        self.assertEqual(repeat["server_issue_url"], issue_url)
        self.assertTrue(repeat["duplicate"])
        sender.assert_called_once()
        status_reader.assert_called_once_with("server-r-3", token)
        record = self.store.data["records"][first["receipt_id"]]
        self.assertEqual(record["server_issue_state"], "created")
        self.assertEqual(record["server_issue_number"], 42)
        self.assertEqual(record["server_issue_url"], issue_url)

    async def test_cached_server_receipt_does_not_refresh_after_delivery_is_disabled(self):
        token = "W" * 40
        await self.feedback_manager.async_set_feedback_delivery_credential(token)
        await self.feedback_manager.async_set_feedback_delivery_enabled(True)
        package = self.body | {"send_to_habab": True}
        with patch.object(
            feedback_action, "_session_data", AsyncMock(return_value=make_context())
        ), patch.object(
            feedback_action, "_redact_text", return_value="Очищенный отзыв"
        ), patch.object(
            feedback, "send_feedback", return_value=FeedbackDeliveryResult(
                status="saved_on_server", receipt_id="server-r-4",
                issue_state="pending_issue", duplicate=False, attempts=1,
            ),
        ) as sender, patch.object(feedback, "get_feedback_status") as status_reader:
            accepted = await execute(self.hass, self.setup_agent, "alice", package)
            await self.feedback_manager.async_set_feedback_delivery_enabled(False)
            repeat = await execute(self.hass, self.setup_agent, "alice", package)

        self.assertEqual(accepted["delivery_status"], "saved_on_server")
        self.assertEqual(repeat["state"], "saved_locally")
        self.assertNotIn("delivery_status", repeat)
        sender.assert_called_once()
        status_reader.assert_not_called()
        self.assertEqual(self.hass.async_add_executor_job.await_count, 1)

    async def test_explicit_same_key_retry_reuses_stored_projection_and_uuid(self):
        await self.feedback_manager.async_set_feedback_delivery_credential("R" * 40)
        await self.feedback_manager.async_set_feedback_delivery_enabled(True)
        package = self.body | {"send_to_habab": True}
        sender_results = [
            FeedbackDeliveryResult(status="pending", attempts=1, error_code="timeout"),
            FeedbackDeliveryResult(
                status="saved_on_server", receipt_id="server-r-2",
                issue_state="pending_issue", duplicate=True, attempts=1,
            ),
        ]
        with patch.object(
            feedback_action, "_session_data", AsyncMock(return_value=make_context())
        ) as load_context, patch.object(
            feedback_action, "_redact_text", return_value="Первичная проекция"
        ), patch.object(
            feedback, "send_feedback", side_effect=sender_results
        ) as sender:
            first = await execute(self.hass, self.setup_agent, "alice", package)
            second = await execute(self.hass, self.setup_agent, "alice", package)

        self.assertEqual(first["delivery_status"], "pending")
        self.assertEqual(second["delivery_status"], "saved_on_server")
        self.assertTrue(second["duplicate"])
        load_context.assert_awaited_once()
        self.assertEqual(sender.call_count, 2)
        first_call, second_call = sender.call_args_list
        self.assertEqual(first_call.args[0], second_call.args[0])
        self.assertEqual(first_call.args[0]["feedback"], "Первичная проекция")
        self.assertEqual(first_call.args[0]["idempotency_key"], package["idempotency_key"])
        self.assertFalse(first_call.kwargs["retry_ambiguous"])
        self.assertTrue(second_call.kwargs["retry_ambiguous"])

    async def test_foreign_active_owner_cannot_trigger_remote_delivery(self):
        await self.feedback_manager.async_set_feedback_delivery_credential("O" * 40)
        await self.feedback_manager.async_set_feedback_delivery_enabled(True)
        self.setup_agent.active = ("bob", "web-alice")
        with patch.object(feedback, "send_feedback") as sender:
            result = await execute(
                self.hass, self.setup_agent, "alice",
                {**self.body, "send_to_habab": True},
            )

        self.assertEqual(result["error"], "feedback_session_unavailable")
        sender.assert_not_called()
        self.assertEqual(self.feedback_manager.records, {})

    async def test_unmapped_or_ambiguous_telegram_session_is_rejected(self):
        with patch.object(
            feedback_action, "_session_data", AsyncMock(return_value=make_context())
        ) as load_context:
            self.setup_agent.active = ("alice", "tg-unmapped")
            result = await execute(self.hass, self.setup_agent, "alice", self.body)
            self.assertEqual(result["error"], "feedback_session_unavailable")

            self.setup_agent.active = ("alice", "tg-alice")
            self.assistant.web_sessions["alice"] = "tg-alice"
            result = await execute(self.hass, self.setup_agent, "alice", self.body)
            self.assertEqual(result["error"], "feedback_session_unavailable")

            self.setup_agent.active = ("bob", "web-alice")
            result = await execute(self.hass, self.setup_agent, "alice", self.body)
            self.assertEqual(result["error"], "feedback_session_unavailable")

        load_context.assert_not_awaited()
        self.assertIsNone(self.store.data)

    async def test_telegram_mapping_must_remain_current_after_history_read(self):
        self.setup_agent.active = ("alice", "tg-alice")

        async def read_then_revoke(*args, **kwargs):
            self.assistant.telegram_sessions.clear()
            return make_context("telegram")

        with patch.object(
            feedback_action, "_session_data", AsyncMock(side_effect=read_then_revoke)
        ) as load_context:
            result = await execute(self.hass, self.setup_agent, "alice", self.body)

        self.assertEqual(result["error"], "feedback_session_unavailable")
        load_context.assert_awaited_once()
        self.assertIsNone(self.store.data)


if __name__ == "__main__":
    unittest.main()
