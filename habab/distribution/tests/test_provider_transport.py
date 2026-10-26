"""Explicit relay selection must not change the default provider route."""
import os
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from homeassistant.components.habab_home.providers import (
    HABAB_TEXT_CAPABILITIES_URL,
    HABAB_TEXT_CAPABILITY_TIMEOUT,
    ProviderClient,
    ProviderError,
    provider_api_base,
    resolve_provider_contract,
)
from homeassistant.components.habab_telegram import transport


class TransportTests(unittest.TestCase):
    def test_default_routes_are_direct(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(provider_api_base('openai'), 'https://api.openai.com/v1')
            self.assertEqual(transport.get_me_request('synthetic-token'), ('https://api.telegram.org/botsynthetic-token/getMe', {}))

    def test_legacy_relay_environment_does_not_redirect_or_add_auth(self):
        with patch.dict(os.environ, {'HABAB_TELEGRAM_RELAY': 'https://relay.test/'}):
            self.assertEqual(
                transport.get_me_request('synthetic-token'),
                ('https://api.telegram.org/botsynthetic-token/getMe', {}),
            )

    def test_unsafe_legacy_relay_urls_are_ignored_for_direct_requests(self):
        for value in ['http://relay.test', 'https://user:pass@relay.test', 'https://relay.test?key=x', 'https://relay.test/path']:
            with self.subTest(value=value), patch.dict(os.environ, {'HABAB_TELEGRAM_RELAY': value}):
                self.assertEqual(
                    transport.get_me_request('synthetic-token'),
                    ('https://api.telegram.org/botsynthetic-token/getMe', {}),
                )

    def test_provider_base_has_no_environment_redirect(self):
        with patch.dict(os.environ, {'HABAB_OPENAI_BASE_URL': 'https://relay.test/v1'}):
            self.assertEqual(provider_api_base('openai'), 'https://api.openai.com/v1')
            self.assertEqual(provider_api_base('habab'), 'https://habab-voice.wpmix.net/gemini/v1beta')


class _Response:
    def __init__(self, status, payload):
        self.status = status
        self.content_length = len(payload)
        self.content = SimpleNamespace(read=self._read, iter_chunked=self._iter_chunked)
        self._payload = payload

    async def _read(self, _limit):
        return self._payload

    async def _iter_chunked(self, _size):
        yield self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None


class _Session:
    def __init__(self, get_responses=(), post_response=None):
        self.get_responses = list(get_responses)
        self.post_response = post_response
        self.get_calls = []
        self.post_calls = []

    def get(self, *args, **kwargs):
        self.get_calls.append((args, kwargs))
        return self.get_responses.pop(0)

    def post(self, *args, **kwargs):
        self.post_calls.append((args, kwargs))
        return self.post_response


class HababTextContractTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def settings():
        return {"provider": "habab", "model": "gemini-3.1-flash-lite", "api_key": "fixture-project-key"}

    async def test_legacy_404_keeps_the_existing_google_route(self):
        session = _Session([_Response(404, b"legacy")])
        contract = await resolve_provider_contract(session, self.settings())
        self.assertEqual((contract.protocol, contract.model, contract.base_url), (
            "google", "gemini-3.1-flash-lite", "https://habab-voice.wpmix.net/gemini/v1beta",
        ))
        self.assertIsNone(contract.text_ready)
        args, kwargs = session.get_calls[0]
        self.assertEqual(args, (HABAB_TEXT_CAPABILITIES_URL,))
        self.assertEqual(kwargs["headers"], {"Authorization": "Bearer fixture-project-key"})
        self.assertEqual(kwargs["timeout"].total, HABAB_TEXT_CAPABILITY_TIMEOUT)
        self.assertFalse(kwargs["allow_redirects"])

    async def test_managed_capability_resolves_fixed_alias_and_sdk_payload(self):
        capability = json.dumps({
            "protocol": "openai_compatible", "model": "habab-chat", "api_path": "/v1",
            "managed": True, "configured": True, "ready": True,
            "configured_upstream_provider": "deepseek",
            "configured_upstream_model": "deepseek-flash",
        }).encode()
        completion = json.dumps({"choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": "ok",
        }}]}).encode()
        session = _Session([_Response(200, capability)], _Response(200, completion))
        client = ProviderClient(session, self.settings())
        result = await client.complete([{"role": "user", "content": "hello"}], [])
        self.assertEqual(result["content"], "ok")
        self.assertEqual(client.contract.model, "habab-chat")
        self.assertEqual(client.contract.configured_upstream_provider, "deepseek")
        self.assertEqual(client.contract.configured_upstream_model, "deepseek-flash")
        (url,), kwargs = session.post_calls[0]
        self.assertEqual(url, "https://habab-voice.wpmix.net/v1/chat/completions")
        self.assertEqual(kwargs["json"]["model"], "habab-chat")
        self.assertEqual(kwargs["headers"], {"Authorization": "Bearer fixture-project-key"})

    async def test_invalid_optional_upstream_metadata_is_ignored_without_changing_managed_alias(self):
        capability = json.dumps({
            "protocol": "openai_compatible", "model": "habab-chat", "api_path": "/v1",
            "managed": True, "configured": True, "ready": True,
            "configured_upstream_provider": ["deepseek"],
            "configured_upstream_model": "not a model",
        }).encode()
        contract = await resolve_provider_contract(_Session([_Response(200, capability)]), self.settings())
        self.assertEqual(contract.model, "habab-chat")
        self.assertIsNone(contract.configured_upstream_provider)
        self.assertIsNone(contract.configured_upstream_model)

    async def test_invalid_and_unready_capabilities_fail_without_provider_fallback(self):
        malformed = json.dumps({
            "protocol": "openai_compatible", "model": "attacker-model", "api_path": "https://bad.invalid",
            "managed": True, "configured": True, "ready": True,
        }).encode()
        with self.assertRaises(ProviderError) as invalid:
            await resolve_provider_contract(_Session([_Response(200, malformed)]), self.settings())
        self.assertEqual(invalid.exception.code, "invalid_response")

        not_ready = json.dumps({
            "protocol": "openai_compatible", "model": "habab-chat", "api_path": "/v1",
            "managed": True, "configured": False, "ready": False,
        }).encode()
        session = _Session([_Response(200, not_ready)])
        with self.assertRaises(ProviderError) as unavailable:
            await ProviderClient(session, self.settings()).complete([], [])
        self.assertEqual(unavailable.exception.code, "provider_not_ready")
        self.assertFalse(session.post_calls)

        service_error = b'{"error":{"code":"habab_text_upstream_not_ready"}}'
        session = _Session([_Response(503, service_error)])
        with self.assertRaises(ProviderError) as unavailable_response:
            await ProviderClient(session, self.settings()).complete([], [])
        self.assertEqual(unavailable_response.exception.code, "provider_not_ready")
        self.assertFalse(session.post_calls)
