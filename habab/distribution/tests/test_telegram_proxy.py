"""Provider-aware Telegram routing never confuses service and bot credentials."""
import json
import unittest
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.components.habab_telegram.config_flow import (
    HababTelegramConfigFlow, get_bot,
)
from homeassistant.components.habab_telegram.transport import (
    TELEGRAM_METHODS, TELEGRAM_PROTOCOL, get_me_request, proxy_url,
    resolve_telegram_transport, worker_proxy_env,
)


def provider_hass(provider, api_key):
    return SimpleNamespace(data={"habab_home": {
        "setup_agent": SimpleNamespace(
            settings=lambda: {"provider": provider, "api_key": api_key}),
    }})


def capabilities_payload(methods=None):
    return {
        "provider": {"unrelated": True},
        "telegram": {
            "protocol": TELEGRAM_PROTOCOL,
            "base": "/telegram",
            "methods": list(TELEGRAM_METHODS if methods is None else methods),
        },
    }


class ProxyValidationTests(unittest.TestCase):
    def test_empty_proxy_preserves_direct_worker(self):
        self.assertEqual(worker_proxy_env(""), {})
        self.assertIsNone(proxy_url(None))

    def test_explicit_proxy_overrides_relay_and_stays_worker_local(self):
        value = "http://test-user:test-password@127.0.0.1:8081"
        url, headers = get_me_request("fixture-token", value, {
            "mode": "habab_relay", "base_url": "https://habab-voice.wpmix.net",
            "service_key": "fixture-service-key", "protocol": TELEGRAM_PROTOCOL,
        })
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


class ProviderRouteTests(unittest.IsolatedAsyncioTestCase):
    class Response:
        def __init__(self, status, payload, location=None):
            self.status = status
            self.payload = json.dumps(payload).encode()
            self.headers = {"Location": location} if location else {}
            self.content = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def iter_chunked(self, _size):
            yield self.payload

        async def json(self, **_kwargs):
            return json.loads(self.payload)

    class Session:
        def __init__(self, responses):
            self.responses = list(responses)
            self.calls = []

        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return self.responses.pop(0)

    async def test_selected_habab_validates_capability_then_separates_keys(self):
        session = self.Session([
            self.Response(200, capabilities_payload()),
            self.Response(200, {"ok": True, "result": {"id": 12, "username": "fixture_bot"}}),
        ])
        with patch.dict("os.environ", {"HABAB_TELEGRAM_RELAY": "https://legacy.test"}):
            route, provider = await resolve_telegram_transport(
                provider_hass("habab", "fixture-service-key"), session)
        self.assertEqual(provider, "habab")
        self.assertEqual(route, {
            "mode": "habab_relay",
            "base_url": "https://habab-voice.wpmix.net",
            "service_key": "fixture-service-key",
            "protocol": TELEGRAM_PROTOCOL,
        })
        self.assertEqual(session.calls[0][0], "https://habab-voice.wpmix.net/v1/capabilities")
        self.assertEqual(session.calls[0][1]["headers"], {
            "Authorization": "Bearer fixture-service-key",
        })
        token = "123456:" + "a" * 20
        result = await get_bot(session, token, telegram_transport=route)
        self.assertEqual(result, {"id": 12, "username": "fixture_bot"})
        url, kwargs = session.calls[1]
        self.assertEqual(url, "https://habab-voice.wpmix.net/telegram/getMe")
        self.assertNotIn(token, url)
        self.assertEqual(kwargs["headers"], {
            "Authorization": "Bearer fixture-service-key",
            "X-Habab-Telegram-Bot-Token": token,
        })
        self.assertFalse(kwargs["allow_redirects"])

    async def test_config_flow_resolves_route_before_getme_and_stores_no_service_key(self):
        token = "123456:" + "a" * 20
        session = self.Session([
            self.Response(200, capabilities_payload()),
            self.Response(200, {"ok": True, "result": {"id": 12, "username": "fixture_bot"}}),
        ])
        created = {"type": "create_entry"}
        flow = SimpleNamespace(
            context={"habab_owner_id": "owner"},
            hass=SimpleNamespace(
                auth=SimpleNamespace(async_get_user=AsyncMock(return_value=SimpleNamespace(
                    is_active=True, is_admin=True))),
                data=provider_hass("habab", "fixture-service-key").data,
            ),
            async_set_unique_id=AsyncMock(),
            _async_current_entries=lambda: [],
            async_create_entry=Mock(return_value=created),
        )
        flow.async_step_user = MethodType(HababTelegramConfigFlow.async_step_user, flow)
        with patch(
            "homeassistant.components.habab_telegram.config_flow.async_get_clientsession",
            return_value=session,
        ):
            result = await flow.async_step_user({"token": token})
        self.assertIs(result, created)
        self.assertEqual(session.calls[0][0], "https://habab-voice.wpmix.net/v1/capabilities")
        self.assertEqual(session.calls[1][0], "https://habab-voice.wpmix.net/telegram/getMe")
        flow.async_create_entry.assert_called_once_with(
            title="@fixture_bot",
            data={"token": token, "owner_id": "owner", "bot_id": 12,
                  "bot_username": "fixture_bot"},
        )

    async def test_rejected_or_unresponsive_getme_keeps_form_without_creating_entry(self):
        token = "123456:" + "a" * 20
        for failure, code in (("rejected", "invalid_token"), ("timeout", "cannot_validate")):
            with self.subTest(failure=failure):
                session = self.Session([
                    self.Response(401, {"ok": False, "error_code": 401,
                                        "description": "private upstream details"}),
                ])
                if failure == "timeout":
                    session.get = Mock(side_effect=TimeoutError("private upstream details"))
                form = {"type": "form"}
                flow = SimpleNamespace(
                    context={"habab_owner_id": "owner"},
                    hass=SimpleNamespace(
                        auth=SimpleNamespace(async_get_user=AsyncMock(return_value=SimpleNamespace(
                            is_active=True, is_admin=True))),
                        data=provider_hass("deepseek", "fixture-provider-key").data,
                    ),
                    async_set_unique_id=AsyncMock(),
                    async_create_entry=Mock(),
                    async_show_form=Mock(return_value=form),
                )
                with patch(
                    "homeassistant.components.habab_telegram.config_flow.async_get_clientsession",
                    return_value=session,
                ):
                    result = await HababTelegramConfigFlow.async_step_user(flow, {"token": token})
                self.assertIs(result, form)
                self.assertEqual(flow.async_show_form.call_args.kwargs["errors"], {"base": code})
                flow.async_set_unique_id.assert_not_awaited()
                flow.async_create_entry.assert_not_called()

    async def test_other_selected_provider_uses_direct_route_without_capability_request(self):
        token = "123456:" + "a" * 20
        session = self.Session([
            self.Response(200, {"ok": True, "result": {"id": 12, "username": "fixture_bot"}}),
        ])
        with patch.dict("os.environ", {"HABAB_TELEGRAM_RELAY": ""}):
            route, provider = await resolve_telegram_transport(
                provider_hass("deepseek", "fixture-provider-key"), session)
        self.assertEqual((route, provider), ({"mode": "direct"}, "deepseek"))
        await get_bot(session, token, telegram_transport=route)
        self.assertEqual(session.calls[0][0], "https://api.telegram.org/bot" + token + "/getMe")
        self.assertEqual(session.calls[0][1]["headers"], {})

    async def test_missing_provider_uses_direct_route(self):
        with patch.dict("os.environ", {"HABAB_TELEGRAM_RELAY": ""}):
            route, provider = await resolve_telegram_transport(SimpleNamespace(data={}), object())
        self.assertEqual((route, provider), ({"mode": "direct"}, None))

    async def test_legacy_env_is_not_used_as_automatic_route(self):
        with patch.dict("os.environ", {"HABAB_TELEGRAM_RELAY": "https://legacy.test"}):
            with self.assertRaisesRegex(ValueError, "^unsupported_legacy_relay$"):
                await resolve_telegram_transport(
                    provider_hass("openai", "fixture-provider-key"), object())

    async def test_explicit_proxy_overrides_habab_without_capability_request(self):
        session = self.Session([])
        with patch.dict("os.environ", {"HABAB_TELEGRAM_RELAY": "https://legacy.test"}):
            route, provider = await resolve_telegram_transport(
                provider_hass("habab", "fixture-service-key"), session,
                "http://proxy.test:8080")
        self.assertEqual((route, provider), ({"mode": "direct"}, None))
        self.assertEqual(session.calls, [])

    async def test_bad_or_unavailable_capabilities_fail_without_direct_fallback(self):
        self.assertEqual(TELEGRAM_METHODS, frozenset({
            "getMe", "getUpdates", "getChatMember", "sendMessage", "sendChatAction",
        }))
        for status, payload, error in (
            (401, {"error": {"code": "relay_not_authorized"}}, "relay_auth_failed"),
            (503, {}, "relay_capabilities_unavailable"),
            (200, {"telegram": {"protocol": TELEGRAM_PROTOCOL, "base": "/telegram",
                                  "methods": ["getMe", "getUpdates", "sendMessage",
                                              "sendChatAction"]}},
             "relay_capabilities_incompatible"),
        ):
            with self.subTest(error=error):
                session = self.Session([self.Response(status, payload)])
                with self.assertRaisesRegex(ValueError, f"^{error}$"):
                    await resolve_telegram_transport(
                        provider_hass("habab", "fixture-service-key"), session)
                self.assertEqual(len(session.calls), 1)

    async def test_relay_service_401_and_telegram_bot_401_are_distinct(self):
        route = {
            "mode": "habab_relay", "base_url": "https://habab-voice.wpmix.net",
            "service_key": "fixture-service-key", "protocol": TELEGRAM_PROTOCOL,
        }
        token = "123456:" + "a" * 20
        relay_auth = self.Session([self.Response(401, {"error": {"code": "relay_not_authorized"}})])
        with self.assertRaisesRegex(ValueError, "^relay_auth_failed$"):
            await get_bot(relay_auth, token, telegram_transport=route)
        bot_auth = self.Session([self.Response(401, {"ok": False, "error_code": 401})])
        with self.assertRaisesRegex(ValueError, "^invalid_token$"):
            await get_bot(bot_auth, token, telegram_transport=route)
