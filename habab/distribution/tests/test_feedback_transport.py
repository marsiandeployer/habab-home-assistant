"""Offline contract checks for optional remote feedback delivery."""

from __future__ import annotations

from email.message import Message
import json
from pathlib import Path
import sys
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "habab_home"))

from feedback_transport import (  # noqa: E402
    RECEIVER_URL,
    REQUEST_TIMEOUT_SECONDS,
    send_feedback,
)


INSTALLATION_TOKEN = "Q" * 43
IDEMPOTENCY_KEY = "4b4a9a47-7919-47d7-9f5f-f67adfd6e616"
RECEIPT_ID = "736bab99-2de3-4c33-9e7b-58d1e16938e7"


def representative_package() -> dict:
    """Mirror the bounded feedback_action projection, not owner/history data."""
    return {
        "idempotency_key": IDEMPOTENCY_KEY,
        "feedback": "Голосовой ответ был слишком длинным.",
        "context": {
            "channel": "web",
            "session": {
                "provider_models": [{"provider": "habab-deepseek", "model": "deepseek-chat"}],
                "created_at": "2026-10-26T10:00:00Z",
                "updated_at": "2026-10-26T10:00:01Z",
                "status": "completed",
                "processing_duration_ms": 1000,
                "message_count": 2,
                "events_truncated": False,
            },
            "events": [{
                "id": "0.0.call",
                "at": "2026-10-26T10:00:00Z",
                "channel": "web",
                "provider": "habab-deepseek",
                "model": "deepseek-chat",
                "stage": "tool_call",
                "kind": "tool_invocation",
                "tool": "ask_home_assistant",
                "role": "assistant",
                "status": "completed",
                "text": None,
                "input": {"action": "states"},
                "output": None,
                "duration_ms": 10,
                "exit_code": None,
                "confirmation": "runtime_call_recorded",
                "available": True,
                "reason": None,
            }],
            "source": {
                "kind": "owner_mapped_current_session",
                "history_message_limit": 60,
                "truncated": False,
            },
        },
    }


class FakeResponse:
    status = 200

    def __init__(
        self,
        payload: dict,
        *,
        content_type: str = "application/json",
        cache_control: str = "no-store",
    ):
        self._raw = json.dumps(payload).encode()
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.headers["Cache-Control"] = cache_control

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit: int) -> bytes:
        return self._raw[:limit]


def receipt(*, issue_state: str = "pending_issue", duplicate: bool = False) -> dict:
    return {
        "receipt_id": RECEIPT_ID,
        "state": "saved",
        "issue_state": issue_state,
        "created_at": "2026-10-26T10:05:00Z",
        "duplicate": duplicate,
    }


class FakeOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class FeedbackTransportTests(unittest.TestCase):
    def test_no_installation_credential_returns_not_configured_without_network(self):
        opener = FakeOpener([])

        result = send_feedback(representative_package(), None, _opener=opener)

        self.assertEqual(result.status, "not_configured")
        self.assertEqual(opener.calls, [])

    def test_disallowed_event_fields_and_nested_credentials_are_rejected_before_http(self):
        cases = (
            ("owner_id", "foreign-owner"),
            ("input", {"api_key": "must-never-be-sent"}),
        )
        for field, value in cases:
            with self.subTest(field=field):
                package = representative_package()
                package["context"]["events"][0][field] = value
                opener = FakeOpener([])

                result = send_feedback(package, INSTALLATION_TOKEN, _opener=opener)

                self.assertEqual(result.status, "rejected")
                self.assertEqual(result.error_code, "package_invalid")
                self.assertEqual(opener.calls, [])

    def test_invalid_session_timestamps_are_rejected_before_http(self):
        for value in (42, "not-a-date", "2" * 65):
            with self.subTest(value_type=type(value).__name__):
                package = representative_package()
                package["context"]["session"]["created_at"] = value
                opener = FakeOpener([])

                result = send_feedback(package, INSTALLATION_TOKEN, _opener=opener)

                self.assertEqual(result.error_code, "package_invalid")
                self.assertEqual(opener.calls, [])

    def test_real_projection_maps_to_receiver_allowlist_and_requires_durable_receipt(self):
        opener = FakeOpener([FakeResponse(receipt())])

        result = send_feedback(representative_package(), INSTALLATION_TOKEN, _opener=opener)

        self.assertEqual(result.status, "saved_on_server")
        self.assertEqual(result.receipt_id, RECEIPT_ID)
        self.assertEqual(result.issue_state, "pending_issue")
        self.assertFalse(result.duplicate)
        request, timeout = opener.calls[0]
        self.assertEqual(request.full_url, RECEIVER_URL)
        self.assertEqual(timeout, REQUEST_TIMEOUT_SECONDS)
        self.assertEqual(request.get_header("Authorization"), f"Bearer {INSTALLATION_TOKEN}")
        self.assertEqual(request.get_header("Idempotency-key"), IDEMPOTENCY_KEY)
        body = json.loads(request.data)
        self.assertEqual(set(body), {"schema_version", "consent", "channel", "feedback", "context"})
        self.assertEqual(body["consent"], {
            "explicit": True,
            "scope": "feedback_and_bounded_context",
            "version": 1,
        })
        self.assertEqual(set(body["context"]), {"channel", "session", "events", "source"})
        self.assertEqual(set(body["context"]["events"][0]), {
            "at", "available", "channel", "confirmation", "duration_ms", "exit_code",
            "id", "input", "kind", "model", "output", "provider", "reason", "role",
            "stage", "status", "text", "tool",
        })
        self.assertNotIn("idempotency_key", body)
        self.assertNotIn("owner_id", request.data.decode())
        self.assertNotIn("web-alice", request.data.decode())
        self.assertNotIn(INSTALLATION_TOKEN, repr(result))
        self.assertNotIn("Голосовой ответ был слишком длинным.", repr(result))

    def test_timeout_is_pending_and_explicit_retry_reuses_the_same_key_once(self):
        no_retry = FakeOpener([TimeoutError()])
        pending = send_feedback(representative_package(), INSTALLATION_TOKEN, _opener=no_retry)
        self.assertEqual(
            (pending.status, pending.error_code, pending.attempts),
            ("pending", "timeout", 1),
        )
        self.assertEqual(len(no_retry.calls), 1)

        retry = FakeOpener([TimeoutError(), FakeResponse(receipt(duplicate=True))])
        recovered = send_feedback(
            representative_package(), INSTALLATION_TOKEN, retry_ambiguous=True, _opener=retry,
        )
        self.assertEqual(
            (recovered.status, recovered.issue_state, recovered.duplicate, recovered.attempts),
            ("saved_on_server", "pending_issue", True, 2),
        )
        self.assertEqual(len(retry.calls), 2)
        self.assertEqual(
            [call[0].get_header("Idempotency-key") for call in retry.calls],
            [IDEMPOTENCY_KEY, IDEMPOTENCY_KEY],
        )

    def test_ambiguous_or_wrong_receipt_schema_never_claims_server_save(self):
        bad_receipt = FakeOpener([FakeResponse(receipt(issue_state="created"))])
        result = send_feedback(representative_package(), INSTALLATION_TOKEN, _opener=bad_receipt)
        self.assertEqual((result.status, result.error_code, result.receipt_id),
                         ("pending", "invalid_receipt", None))

        invalid_payload = {"receipt_id": RECEIPT_ID, "state": "saved"}
        ambiguous_response = FakeOpener([
            FakeResponse(invalid_payload), FakeResponse(invalid_payload),
        ])
        retried = send_feedback(
            representative_package(), INSTALLATION_TOKEN,
            retry_ambiguous=True, _opener=ambiguous_response,
        )
        self.assertEqual(
            (retried.status, retried.error_code, retried.attempts),
            ("pending", "invalid_receipt", 2),
        )
        self.assertEqual(len(ambiguous_response.calls), 2)


if __name__ == "__main__":
    unittest.main()
