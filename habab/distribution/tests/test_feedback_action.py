"""The chat feedback action saves only a unique current owner-mapped session."""

import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from homeassistant.components.habab_home import feedback, feedback_action
from homeassistant.components.habab_home.feedback import FeedbackManager
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
