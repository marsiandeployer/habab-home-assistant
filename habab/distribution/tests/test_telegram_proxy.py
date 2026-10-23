"""A bot's explicit proxy covers validation and its isolated Node worker."""
import unittest
from unittest.mock import patch

from homeassistant.components.habab_telegram.transport import (
    get_me_request, proxy_url, worker_proxy_env,
)
from homeassistant.components.habab_telegram.config_flow import get_bot


class ProxyValidationTests(unittest.TestCase):
    def test_empty_proxy_preserves_direct_worker(self):
        self.assertEqual(worker_proxy_env(""), {})
        self.assertIsNone(proxy_url(None))

    def test_explicit_proxy_overrides_service_relay_for_same_bot(self):
        value = "http://test-user:test-password@127.0.0.1:8081"
        with patch.dict("os.environ", {"HABAB_TELEGRAM_RELAY": "https://relay.test"}):
            url, headers = get_me_request("fixture-token", value)
        self.assertEqual(url, "https://api.telegram.org/botfixture-token/getMe")
        self.assertEqual(headers, {})
        env = worker_proxy_env(value)
        self.assertEqual(env["HTTPS_PROXY"], value)
        self.assertEqual(env["HTTP_PROXY"], value)
        self.assertEqual(env["NODE_USE_ENV_PROXY"], "1")
        self.assertEqual(env["NO_PROXY"], "")

    def test_bad_addresses_return_only_safe_code(self):
        for value in ("socks5://localhost:1080", "http://", "http://p:0", "http://p:bad",
                      "https://p/path", "https://p?secret=x", "http://p#x", "http://p\n", 1):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "^invalid_proxy$"):
                proxy_url(value)


class ValidationRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_me_uses_same_proxy_and_never_retries_direct(self):
        class Reply:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def json(self, **kwargs):
                return {"ok": True, "result": {"id": 123, "username": "fixture_bot"}}

        class Session:
            def __init__(self):
                self.calls = []
                self.fail = False

            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                if self.fail:
                    raise OSError("private proxy details must not escape")
                return Reply()

        session = Session()
        token = "123:" + "a" * 20
        value = "https://test-user:test-password@proxy.test:8443"
        result = await get_bot(session, token, value)
        self.assertEqual(result["username"], "fixture_bot")
        self.assertEqual(session.calls[0][1]["proxy"], value)
        self.assertFalse(session.calls[0][1]["allow_redirects"])
        session.fail = True
        with self.assertRaisesRegex(ValueError, "^cannot_validate$"):
            await get_bot(session, token, value)
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(session.calls[-1][1]["proxy"], value)
        session.fail = False
        Reply.status = 502
        with self.assertRaisesRegex(ValueError, "^cannot_validate$"):
            await get_bot(session, token, value)
        Reply.status = 401
        with self.assertRaisesRegex(ValueError, "^invalid_token$"):
            await get_bot(session, token, value)
