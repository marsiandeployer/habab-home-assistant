"""Run inside the pinned HA image; no network, keys, or real devices needed.

Protects provider error handling and rejects invalid tool batches before any
device action can run. The separate live QA proves real provider execution.
"""

import json
import unittest
import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from homeassistant.components.habab_home.assist_adapter import parse_calls
from homeassistant.components.habab_home.providers import (
    PROVIDERS, ProviderClient, ProviderError, provider_api_base, validate_settings,
)
from homeassistant.components.habab_home.websocket import get_connection_key, save_connection
from homeassistant.components.habab_home import websocket


class Stream:
    def __init__(self, body):
        self.body = body

    async def iter_chunked(self, size):
        yield self.body


class Response:
    def __init__(self, status=200, payload=None, raw=None):
        self.status = status
        self.content = Stream(raw if raw is not None else json.dumps(payload).encode())

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class Session:
    def __init__(self, response):
        self.response = response
        self.request = None

    def post(self, url, **kwargs):
        self.request = {"url": url, **kwargs}
        return self.response


def message(function="habab_connection_test", arguments='{"ok":true}'):
    return {"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "test-1", "type": "function", "function": {
            "name": function, "arguments": arguments,
        }}],
    }}]}


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    def settings(self, provider="deepseek"):
        return {"provider": provider, "api_key": "fixture-not-a-real-key", "model": PROVIDERS[provider]["model"]}

    async def test_all_fixed_endpoints_and_tool_checks(self):
        self.assertEqual(provider_api_base("gemini"),
                         "https://generativelanguage.googleapis.com/v1beta")
        for provider in ("openai", "deepseek"):
            session = Session(Response(payload=message()))
            await ProviderClient(session, self.settings(provider)).check()
            self.assertEqual(session.request["url"], PROVIDERS[provider]["url"])
            self.assertFalse(session.request["allow_redirects"])
            self.assertEqual(session.request["json"]["tool_choice"]["function"]["name"], "habab_connection_test")

    async def test_old_openai_relay_environment_cannot_reroute_direct_providers(self):
        with patch.dict("os.environ", {"HABAB_OPENAI_BASE_URL": "https://old-relay.test/v1"}):
            for provider in ("openai", "deepseek"):
                session = Session(Response(payload=message()))
                await ProviderClient(session, self.settings(provider)).check()
                self.assertEqual(session.request["url"], PROVIDERS[provider]["url"])
                self.assertEqual(session.request["headers"]["Authorization"], "Bearer fixture-not-a-real-key")

    async def test_rejected_auth_quota_and_redirect_do_not_become_success(self):
        for status, code in [(401, "invalid_auth"), (403, "invalid_auth"),
                             (429, "quota"), (402, "quota"), (302, "cannot_connect")]:
            client = ProviderClient(Session(Response(status=status)), self.settings())
            with self.assertRaises(ProviderError) as raised:
                await client.check()
            self.assertEqual(raised.exception.code, code)
            self.assertNotIn(self.settings()["api_key"], str(raised.exception))

    async def test_plain_text_or_wrong_tool_is_not_a_verified_connection(self):
        for payload in [message("wrong_tool"), message(arguments='{"ok":false}'),
                        {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "OK"}}]}]:
            with self.assertRaises(ProviderError):
                await ProviderClient(Session(Response(payload=payload)), self.settings()).check()

    async def test_malformed_and_oversized_responses_are_rejected(self):
        for raw in [b"invalid-json", b"{}", b"x" * (1024 * 1024 + 1)]:
            with self.assertRaises(ProviderError):
                await ProviderClient(Session(Response(raw=raw)), self.settings()).check()

    async def test_invalid_key_aborts_flow_without_returning_credentials(self):
        flow = SimpleNamespace(
            async_init=AsyncMock(return_value={"type": "form", "flow_id": "failed-flow", "errors": {"base": "invalid_auth"}}),
            async_abort=Mock(return_value=None),
        )
        config_entries = SimpleNamespace(flow=flow, async_update_entry=Mock())
        hass = SimpleNamespace(data={"habab_home": {"settings_lock": asyncio.Lock()}},
                               config_entries=config_entries)
        connection = Mock()
        await inspect.unwrap(save_connection)(hass, connection, {
            "id": 1, "provider": "openai", "api_key": "fixture-secret", "model": "gpt-4.1-mini",
        })
        flow.async_abort.assert_called_once_with("failed-flow")
        connection.send_error.assert_called_once_with(1, "invalid_auth", "invalid_auth")
        connection.send_result.assert_not_called()
        config_entries.async_update_entry.assert_not_called()

    async def test_saved_key_command_rejects_non_admin_before_reading_credentials(self):
        from homeassistant.exceptions import Unauthorized

        for user in (None, SimpleNamespace(is_admin=False)):
            with self.subTest(user=user):
                hass = SimpleNamespace(config_entries=Mock())
                connection = Mock(user=user)
                with self.assertRaises(Unauthorized):
                    get_connection_key(hass, connection, {"id": 6, "entry_id": "selected-entry"})
                hass.config_entries.async_get_entry.assert_not_called()
                connection.send_result.assert_not_called()

    async def test_saved_key_command_returns_only_the_selected_entry_key(self):
        from homeassistant.components.habab_home.const import DOMAIN

        entry = SimpleNamespace(domain=DOMAIN, data={"api_key": "fixture-saved-key"})
        hass = SimpleNamespace(config_entries=SimpleNamespace(
            async_get_entry=Mock(return_value=entry)))
        connection = Mock()

        await inspect.unwrap(get_connection_key)(hass, connection, {
            "id": 7, "entry_id": "selected-entry",
        })

        hass.config_entries.async_get_entry.assert_called_once_with("selected-entry")
        connection.send_result.assert_called_once_with(7, {"api_key": "fixture-saved-key"})
        connection.send_error.assert_not_called()

    async def test_saved_key_command_rejects_missing_or_foreign_entries(self):
        from homeassistant.components.habab_home.const import DOMAIN

        for entry in (
            None,
            SimpleNamespace(domain="another_integration", data={"api_key": "fixture-key"}),
            SimpleNamespace(domain=DOMAIN, data={}),
            SimpleNamespace(domain=DOMAIN, data={"api_key": ""}),
        ):
            with self.subTest(entry=entry):
                hass = SimpleNamespace(config_entries=SimpleNamespace(
                    async_get_entry=Mock(return_value=entry)))
                connection = Mock()
                await inspect.unwrap(get_connection_key)(hass, connection, {
                    "id": 8, "entry_id": "selected-entry",
                })
                connection.send_error.assert_called_once_with(8, "not_found", "not_found")
                connection.send_result.assert_not_called()

    async def test_provider_list_returns_only_sanitized_matching_check_metadata(self):
        from homeassistant.components.habab_home.const import DOMAIN

        data = {"provider": "openai", "api_key": "fixture-saved-key", "model": "gpt-4.1-mini"}
        entry = SimpleNamespace(
            entry_id="listed-entry", domain=DOMAIN, data=data,
            state=SimpleNamespace(value="loaded"), options={
                "assistant_selected": True,
                "habab_api_check": {
                    "fingerprint": websocket._settings_fingerprint(data),
                    "status": "verified", "checked_at": "2026-10-24T12:00:00+00:00",
                },
            },
        )
        hass = SimpleNamespace(config_entries=SimpleNamespace(async_entries=Mock(return_value=[entry])))
        connection = Mock()
        with patch.object(websocket.er, "async_get", return_value=Mock()), \
             patch.object(websocket.er, "async_entries_for_config_entry", return_value=[]):
            await inspect.unwrap(websocket.list_connections)(hass, connection, {"id": 5})

        result = connection.send_result.call_args.args[1]["connections"][0]
        self.assertEqual(result["assistant_selected"], True)
        self.assertEqual(result["api_check_status"], "verified")
        self.assertEqual(result["api_check_at"], "2026-10-24T12:00:00+00:00")
        self.assertNotIn("fixture-saved-key", str(result))
        self.assertNotIn("fingerprint", result)

    async def test_provider_list_marks_effective_provider_for_legacy_and_explicit_entries(self):
        from homeassistant.components.habab_home.const import DOMAIN

        cases = [
            (
                ["openai", "deepseek"],
                [False, False],
                "deepseek",
            ),
            (
                ["deepseek", "openai"],
                [False, True],
                "openai",
            ),
            (
                ["openai", "deepseek"],
                [True, True],
                "openai",
            ),
        ]
        for providers, flags, expected in cases:
            with self.subTest(providers=providers, flags=flags):
                entries = [
                    SimpleNamespace(
                        entry_id=f"{provider}-entry",
                        domain=DOMAIN,
                        data={
                            "provider": provider,
                            "model": PROVIDERS[provider]["model"],
                            "api_key": f"fixture-{provider}-key",
                        },
                        state=SimpleNamespace(value="loaded"),
                        options={"assistant_selected": flag} if flag else {},
                    )
                    for provider, flag in zip(providers, flags, strict=True)
                ]
                hass = SimpleNamespace(
                    config_entries=SimpleNamespace(async_entries=Mock(return_value=entries)))
                connection = Mock()
                with patch.object(websocket.er, "async_get", return_value=Mock()), \
                     patch.object(websocket.er, "async_entries_for_config_entry", return_value=[]):
                    await inspect.unwrap(websocket.list_connections)(hass, connection, {"id": 9})

                listed = connection.send_result.call_args.args[1]["connections"]
                self.assertEqual(
                    [row["provider"] for row in listed if row["assistant_selected"]],
                    [expected],
                )

    async def test_successful_save_selects_provider_without_returning_flow_credentials(self):
        from homeassistant.components.habab_home.const import DOMAIN

        entry = SimpleNamespace(
            entry_id="saved-entry", domain=DOMAIN,
            data={"provider": "habab", "api_key": "fixture-secret", "model": "gpt-4.1-mini"},
            options={},
        )
        flow = SimpleNamespace(async_init=AsyncMock(return_value={"type": "create_entry", "result": entry}))
        agent = SimpleNamespace(select_provider=Mock())
        config_entries = SimpleNamespace(
            flow=flow,
            async_update_entry=Mock(side_effect=lambda current, options: setattr(current, "options", options)),
        )
        hass = SimpleNamespace(data={"habab_home": {"settings_lock": asyncio.Lock(), "setup_agent": agent}},
                               config_entries=config_entries)
        connection = Mock()
        await inspect.unwrap(save_connection)(hass, connection, {
            "id": 1, "provider": "habab", "api_key": "fixture-secret", "model": "gpt-4.1-mini",
        })
        agent.select_provider.assert_called_once_with("habab")
        result = connection.send_result.call_args.args[1]
        self.assertEqual(result["saved"], True)
        self.assertEqual(result["tool_call_verified"], True)
        self.assertEqual(result["api_check_status"], "verified")
        self.assertIsNotNone(result["api_check_at"])
        self.assertNotIn("fixture-secret", str(result))
        self.assertEqual(websocket._api_check_readback(entry)["api_check_status"], "verified")
        self.assertNotIn("fingerprint", result)

    async def test_provider_check_persists_verified_readback_without_exposing_fingerprint(self):
        from homeassistant.components.habab_home.const import DOMAIN

        entry = SimpleNamespace(
            entry_id="checked-entry", domain=DOMAIN,
            data={"provider": "openai", "api_key": "fixture-saved-key", "model": "gpt-4.1-mini"},
            options={"assistant_selected": True},
        )
        config_entries = SimpleNamespace(
            async_get_entry=Mock(return_value=entry),
            async_update_entry=Mock(side_effect=lambda current, options: setattr(current, "options", options)),
        )
        hass = SimpleNamespace(
            data={"habab_home": {"settings_lock": asyncio.Lock()}},
            config_entries=config_entries,
        )
        connection = Mock()
        with patch.object(websocket, "async_get_clientsession", return_value=Mock()), \
             patch.object(websocket, "ProviderClient") as client:
            client.return_value.check = AsyncMock()
            await inspect.unwrap(websocket.check_connection)(hass, connection, {
                "id": 2, "entry_id": "checked-entry",
            })

        result = connection.send_result.call_args.args[1]
        self.assertEqual(result["api_check_status"], "verified")
        self.assertIsNotNone(result["api_check_at"])
        self.assertNotIn("fixture-saved-key", str(result))
        self.assertNotIn("fingerprint", result)
        self.assertEqual(websocket._api_check_readback(entry), {
            "api_check_status": result["api_check_status"],
            "api_check_at": result["api_check_at"],
        })

    async def test_rejected_check_persists_rejection_for_the_saved_key(self):
        from homeassistant.components.habab_home.const import DOMAIN

        entry = SimpleNamespace(
            entry_id="rejected-entry", domain=DOMAIN,
            data={"provider": "openai", "api_key": "fixture-saved-key", "model": "gpt-4.1-mini"},
            options={},
        )
        config_entries = SimpleNamespace(
            async_get_entry=Mock(return_value=entry),
            async_update_entry=Mock(side_effect=lambda current, options: setattr(current, "options", options)),
        )
        hass = SimpleNamespace(
            data={"habab_home": {"settings_lock": asyncio.Lock()}},
            config_entries=config_entries,
        )
        connection = Mock()
        with patch.object(websocket, "async_get_clientsession", return_value=Mock()), \
             patch.object(websocket, "ProviderClient") as client:
            client.return_value.check = AsyncMock(side_effect=ProviderError("invalid_auth"))
            await inspect.unwrap(websocket.check_connection)(hass, connection, {
                "id": 3, "entry_id": "rejected-entry",
            })

        connection.send_error.assert_called_once_with(3, "invalid_auth", "invalid_auth")
        self.assertEqual(websocket._api_check_readback(entry)["api_check_status"], "rejected")

    def test_api_check_readback_becomes_unknown_after_saved_settings_change(self):
        data = {"provider": "openai", "api_key": "fixture-saved-key", "model": "gpt-4.1-mini"}
        entry = SimpleNamespace(
            data=data,
            options={"habab_api_check": {
                "fingerprint": websocket._settings_fingerprint(data),
                "status": "verified", "checked_at": "2026-10-24T12:00:00+00:00",
            }},
        )
        for change in (
            {"model": "gpt-4.1"},
            {"api_key": "rotated-saved-key"},
            {"provider": "deepseek"},
        ):
            with self.subTest(change=tuple(change)):
                entry.data = {**data, **change}
                self.assertEqual(websocket._api_check_readback(entry), {
                    "api_check_status": "unknown", "api_check_at": None,
                })

    async def test_check_result_is_not_attached_to_settings_rotated_during_request(self):
        from homeassistant.components.habab_home.const import DOMAIN

        entry = SimpleNamespace(
            entry_id="rotated-entry", domain=DOMAIN,
            data={"provider": "openai", "api_key": "fixture-saved-key", "model": "gpt-4.1-mini"},
            options={},
        )
        config_entries = SimpleNamespace(
            async_get_entry=Mock(return_value=entry),
            async_update_entry=Mock(side_effect=lambda current, options: setattr(current, "options", options)),
        )
        hass = SimpleNamespace(
            data={"habab_home": {"settings_lock": asyncio.Lock()}},
            config_entries=config_entries,
        )
        connection = Mock()

        async def rotate_settings():
            entry.data = {**entry.data, "model": "gpt-4.1"}

        with patch.object(websocket, "async_get_clientsession", return_value=Mock()), \
             patch.object(websocket, "ProviderClient") as client:
            client.return_value.check = AsyncMock(side_effect=rotate_settings)
            await inspect.unwrap(websocket.check_connection)(hass, connection, {
                "id": 4, "entry_id": "rotated-entry",
            })

        connection.send_error.assert_called_once_with(4, "connection_changed", "connection_changed")
        config_entries.async_update_entry.assert_not_called()
        self.assertEqual(websocket._api_check_readback(entry), {
            "api_check_status": "unknown", "api_check_at": None,
        })

    async def test_provider_removed_during_save_returns_safe_error(self):
        from homeassistant.components.habab_home.setup_agent import AgentError
        flow = SimpleNamespace(async_init=AsyncMock(return_value={"type": "create_entry"}))
        agent = SimpleNamespace(select_provider=Mock(side_effect=AgentError("provider_required")))
        hass = SimpleNamespace(data={"habab_home": {"settings_lock": asyncio.Lock(), "setup_agent": agent}},
                               config_entries=SimpleNamespace(flow=flow))
        connection = Mock()
        await inspect.unwrap(save_connection)(hass, connection, {
            "id": 1, "provider": "habab", "api_key": "fixture-secret", "model": "gpt-4.1-mini",
        })
        connection.send_error.assert_called_once_with(1, "provider_required", "provider_required")
        connection.send_result.assert_not_called()


class ActionValidationTests(unittest.TestCase):
    def test_settings_reject_user_endpoints_and_invalid_models(self):
        for data in [
            {"provider": "http://localhost", "api_key": "fixture-key", "model": "model"},
            {"provider": "openai", "api_key": "bad\nheader", "model": "model"},
            {"provider": "deepseek", "api_key": "fixture-key", "model": "model?key=secret"},
        ]:
            with self.assertRaises(ProviderError):
                validate_settings(data)

    def test_invalid_batches_are_rejected_before_returning_actions(self):
        valid = message("HassTurnOn", '{"name":"lamp"}')["choices"][0]["message"]
        self.assertEqual(parse_calls(valid, {"HassTurnOn"})[0].tool_args, {"name": "lamp"})
        for batch in [
            message("shell_exec")["choices"][0]["message"],
            message("HassTurnOn", "[]")["choices"][0]["message"],
            message("HassTurnOn", "broken")["choices"][0]["message"],
            {"tool_calls": valid["tool_calls"] * 2},
            {"tool_calls": valid["tool_calls"] * 9},
        ]:
            with self.assertRaises(ProviderError):
                parse_calls(batch, {"HassTurnOn"})


if __name__ == "__main__":
    unittest.main()
