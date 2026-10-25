"""Offline protocol tests for the single-recipient I-Digital Direct client."""

import asyncio
import base64
import importlib.util
import json
from pathlib import Path
import sys
import types
import unittest


_SRC = Path(__file__).parents[1] / "src" / "idigital_sms"
_PACKAGE = "_idigital_sms_protocol_tests"
_package = types.ModuleType(_PACKAGE)
_package.__path__ = [str(_SRC)]
sys.modules.setdefault(_PACKAGE, _package)
_spec = importlib.util.spec_from_file_location(
    f"{_PACKAGE}.api", _SRC / "api.py"
)
_api = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = _api
_spec.loader.exec_module(_api)

DirectClient = _api.DirectClient
IDigitalError = _api.IDigitalError
validate_api_key = _api.validate_api_key
validate_recipient = _api.validate_recipient


class FakeContent:
    def __init__(self, body):
        self._body = json.dumps(body).encode()

    async def read(self, _limit):
        return self._body


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self.content = FakeContent(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class FakeSession:
    def __init__(self, response=None, *, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.error:
            raise self.error
        return self.response


def make_client(session):
    # Test-only credentials and RFC 5737 documentation address; never transmitted.
    api_key = base64.b64encode(b"123:placeholder").decode()
    return DirectClient(
        session,
        api_key=api_key,
        sender="approved-sender",
        recipient="+19995550123",
    )


class IDigitalSmsProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_posts_one_item_to_only_the_configured_recipient(self):
        message_uuid = "063474ec-a34f-4558-90c5-984395000004"
        session = FakeSession(FakeResponse(200, {
            "errors": False,
            "items": [{"messageUuid": message_uuid}],
        }))
        client = make_client(session)

        result = await client.send_message("Test body")

        self.assertEqual(result, message_uuid)
        self.assertEqual(len(session.calls), 1)
        method, url, kwargs = session.calls[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/message"))
        self.assertEqual(kwargs["headers"]["Authorization"], "Basic MTIzOnBsYWNlaG9sZGVy")
        self.assertEqual(kwargs["json"], [{
            "channelType": "SMS",
            "senderName": "approved-sender",
            "destination": "19995550123",
            "content": "Test body",
        }])
        self.assertNotIn("callbackUrl", kwargs["json"][0])

    async def test_invalid_credentials_and_recipient_fail_before_network(self):
        session = FakeSession(FakeResponse(200, {"errors": False, "items": []}))

        with self.assertRaises(IDigitalError):
            validate_api_key("not-a-direct-key")
        with self.assertRaises(IDigitalError):
            validate_recipient("+1 999 555 0123")
        with self.assertRaises(IDigitalError):
            DirectClient(session, api_key="bad", sender="sender", recipient="+19995550123")

        self.assertEqual(session.calls, [])

    async def test_rejected_response_is_not_retried(self):
        session = FakeSession(FakeResponse(200, {
            "errors": True,
            "items": [],
        }))
        client = make_client(session)

        with self.assertRaises(IDigitalError) as raised:
            await client.send_message("Test body")

        self.assertEqual(raised.exception.code, "provider_rejected")
        self.assertEqual(len(session.calls), 1)

    async def test_ambiguous_network_failure_is_not_retried(self):
        session = FakeSession(error=OSError("offline fixture"))
        client = make_client(session)

        with self.assertRaises(IDigitalError) as raised:
            await client.send_message("Test body")

        self.assertEqual(raised.exception.code, "unknown_outcome")
        self.assertEqual(len(session.calls), 1)

    async def test_status_is_explicit_and_404_does_not_claim_delivery(self):
        message_uuid = "063474ec-a34f-4558-90c5-984395000004"
        session = FakeSession(FakeResponse(404, {"message": "not visible yet"}))
        client = make_client(session)

        result = await client.get_status(message_uuid)

        self.assertEqual(result, {"status": "unknown", "provider_status": "not_found"})
        self.assertEqual(len(session.calls), 1)
        method, url, kwargs = session.calls[0]
        self.assertEqual(method, "GET")
        self.assertTrue(url.endswith(f"/message/{message_uuid}"))
        self.assertIsNone(kwargs["json"])

    async def test_delivered_requires_an_explicit_provider_status_response(self):
        message_uuid = "063474ec-a34f-4558-90c5-984395000004"
        session = FakeSession(FakeResponse(200, {"status": "delivered"}))
        client = make_client(session)

        result = await client.get_status(message_uuid)

        self.assertEqual(result, {
            "status": "delivered",
            "provider_status": "delivered",
        })
        self.assertEqual(len(session.calls), 1)

    async def test_outer_timeout_is_bounded_and_not_retried(self):
        class HangingSession:
            def __init__(self):
                self.calls = 0

            def request(self, *_args, **_kwargs):
                self.calls += 1
                return self

            async def __aenter__(self):
                await asyncio.sleep(1)

            async def __aexit__(self, *_args):
                return None

        session = HangingSession()
        client = make_client(session)
        old_timeout = _api.REQUEST_TIMEOUT
        _api.REQUEST_TIMEOUT = 0.001
        try:
            with self.assertRaises(IDigitalError) as raised:
                await client.send_message("Test body")
        finally:
            _api.REQUEST_TIMEOUT = old_timeout

        self.assertEqual(raised.exception.code, "unknown_outcome")
        self.assertEqual(session.calls, 1)


if __name__ == "__main__":
    unittest.main()
