"""Native Gemini text protocol and tool-loop compatibility tests."""

import asyncio
import importlib
import json
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from google import genai
from google.genai import errors, types
import httpx

from homeassistant.components.habab_home.providers import (
    PROVIDERS, ProviderClient, ProviderError, provider_api_base,
)
from homeassistant.components.habab_home.providers_gemini import GeminiProvider
from homeassistant.components.habab_home import providers as providers_module


def response(*parts, finish=types.FinishReason.STOP):
    return SimpleNamespace(candidates=[SimpleNamespace(
        finish_reason=finish,
        content=SimpleNamespace(parts=list(parts)),
    )])


class FakeModels:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    async def generate_content(self, **kwargs):
        self.requests.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if callable(outcome):
            return await outcome()
        return outcome


class FakeClient:
    def __init__(self, outcomes):
        self.models = FakeModels(outcomes)
        self.closed = False
        self.aio = SimpleNamespace(
            models=self.models,
            aclose=self.close,
        )

    async def close(self):
        self.closed = True


class GeminiProviderTests(unittest.IsolatedAsyncioTestCase):
    def settings(self, provider="gemini"):
        return {"provider": provider, "api_key": "fixture-not-a-real-key",
                "model": PROVIDERS[provider]["model"]}

    def make_client(self, outcomes):
        fake = FakeClient(outcomes)
        return fake, patch("homeassistant.components.habab_home.providers_gemini.genai.Client",
                           return_value=fake)

    async def test_native_probe_forces_exact_safe_tool_and_uses_no_proxy(self):
        fake, client_patch = self.make_client([response(types.Part(
            function_call=types.FunctionCall(id="probe-id", name="habab_connection_test", args={"ok": True}),
        ), finish=types.FinishReason.STOP)])
        with client_patch as client_factory, patch.dict("os.environ", {
            "HABAB_OPENAI_BASE_URL": "https://old-relay.test/v1",
        }):
            await ProviderClient(None, self.settings()).check()

        client_factory.assert_called_once()
        self.assertEqual(client_factory.call_args.kwargs["api_key"], "fixture-not-a-real-key")
        self.assertEqual(client_factory.call_args.kwargs["http_options"].api_version, "v1beta")
        self.assertEqual(client_factory.call_args.kwargs["http_options"].retry_options.attempts, 1)
        request = fake.models.requests[0]
        self.assertEqual(request["model"], "gemini-3.1-flash-lite")
        self.assertEqual(request["config"].tool_config.function_calling_config.mode, "ANY")
        self.assertEqual(request["config"].tool_config.function_calling_config.allowed_function_names,
                         ["habab_connection_test"])
        self.assertEqual(request["config"].max_output_tokens, 256)
        self.assertEqual(request["config"].thinking_config.thinking_level, "MINIMAL")
        self.assertTrue(fake.closed)
        self.assertEqual(provider_api_base("gemini"), "https://generativelanguage.googleapis.com/v1beta")

    async def test_real_sdk_keeps_selected_native_route_and_key_in_header(self):
        sdk_client = genai.Client
        for provider, expected_url in (
            ("gemini", "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite:generateContent"),
            ("habab", "https://habab-voice.wpmix.net/gemini/v1beta/models/gemini-3.1-flash-lite:generateContent"),
        ):
            with self.subTest(provider=provider):
                requests = []
                unused_clients = []

                def handle(request):
                    requests.append(request)
                    return httpx.Response(200, json={"candidates": [{
                        "finishReason": "STOP", "content": {"role": "model", "parts": [{
                            "functionCall": {"id": "probe", "name": "habab_connection_test", "args": {"ok": True}},
                        }]},
                    }]})

                def factory(*, api_key, http_options):
                    if http_options.httpx_async_client is not None:
                        self.assertFalse(http_options.httpx_async_client.follow_redirects)
                        unused_clients.append(http_options.httpx_async_client)
                    options = http_options.model_copy(update={
                        "httpx_async_client": httpx.AsyncClient(transport=httpx.MockTransport(handle)),
                    })
                    return sdk_client(api_key=api_key, http_options=options)

                with patch("homeassistant.components.habab_home.providers_gemini.genai.Client", side_effect=factory):
                    await ProviderClient(None, self.settings(provider)).check()
                for unused in unused_clients:
                    await unused.aclose()
                self.assertEqual(len(requests), 1)
                request = requests[0]
                self.assertEqual(str(request.url), expected_url)
                self.assertEqual(request.headers["x-goog-api-key"], "fixture-not-a-real-key")
                self.assertNotIn("authorization", request.headers)
                self.assertFalse(request.url.query)
                self.assertNotIn("fixture-not-a-real-key", str(request.url))
                body = json.loads(request.content)
                self.assertEqual(body["toolConfig"]["functionCallingConfig"]["mode"], "ANY")

    async def test_lazy_module_import_and_sdk_client_init_run_off_event_loop(self):
        fake = FakeClient([response(types.Part(text="ready"))])
        main_thread = threading.get_ident()
        import_threads = []
        client_threads = []
        import_original = importlib.import_module

        def tracked_import(*args, **kwargs):
            import_threads.append(threading.get_ident())
            return import_original(*args, **kwargs)

        def tracked_client(**_kwargs):
            client_threads.append(threading.get_ident())
            time.sleep(0.03)
            return fake

        with patch("homeassistant.components.habab_home.providers_gemini.genai.Client",
                   side_effect=tracked_client):
            with patch.object(providers_module.importlib, "import_module", side_effect=tracked_import):
                message = await ProviderClient(None, self.settings()).complete(
                    [{"role": "user", "content": "hello"}], [],
                )

        self.assertEqual(message["content"], "ready")
        self.assertTrue(import_threads)
        self.assertNotEqual(import_threads[0], main_thread)
        self.assertEqual(len(client_threads), 1)
        self.assertNotEqual(client_threads[0], main_thread)
        self.assertTrue(fake.closed)

    async def test_auto_tool_roundtrip_preserves_google_id_and_thought_signature(self):
        await self._assert_native_tool_roundtrip("gemini")

    async def test_habab_tool_roundtrip_preserves_google_id_and_thought_signature(self):
        await self._assert_native_tool_roundtrip("habab")

    async def _assert_native_tool_roundtrip(self, provider):
        signature = b"opaque-thought-signature"
        first = response(
            types.Part(text="internal reasoning", thought=True),
            types.Part(text="Checking now."),
            types.Part(function_call=types.FunctionCall(
                id="google-call-1", name="HassTurnOn", args={"name": "desk lamp"},
            ), thought_signature=signature),
            finish=types.FinishReason.STOP,
        )
        second = response(types.Part(text="Done."))
        fake, client_patch = self.make_client([first, second])
        with client_patch:
            client = ProviderClient(None, self.settings(provider))
            tools = [{"type": "function", "function": {
                "name": "HassTurnOn", "description": "Turn on a selected device.",
                "parameters": {"type": "object", "properties": {"name": {"type": "string"}},
                               "required": ["name"]},
            }}]
            first_message = await client.complete([{"role": "user", "content": "Turn on desk lamp."}], tools)
            self.assertEqual(first_message["content"], "Checking now.")
            self.assertEqual(first_message["tool_calls"][0]["id"], "google-call-1")
            self.assertNotIn("internal reasoning", str(first_message))

            second_message = await client.complete([
                {"role": "user", "content": "Turn on desk lamp."},
                {"role": "assistant", "content": first_message["content"],
                 "tool_calls": first_message["tool_calls"]},
                {"role": "tool", "tool_call_id": "google-call-1",
                 "content": '{"data":{"success":true},"error":null}'},
            ], tools)

        self.assertEqual(second_message["content"], "Done.")
        self.assertEqual(fake.models.requests[0]["config"].tool_config.function_calling_config.mode, "AUTO")
        replay = fake.models.requests[1]["contents"]
        model_call = replay[1].parts[1]
        tool_result = replay[2].parts[0].function_response
        self.assertEqual(model_call.function_call.id, "google-call-1")
        self.assertEqual(model_call.thought_signature, signature)
        self.assertEqual(tool_result.id, "google-call-1")
        self.assertEqual(tool_result.name, "HassTurnOn")
        self.assertEqual(tool_result.response, {"data": {"success": True}, "error": None})
        self.assertTrue(all(client.closed for client in [fake]))

    async def test_parallel_tool_results_share_one_google_user_content(self):
        signature_one = b"signature-one"
        signature_two = b"signature-two"
        first = response(
            types.Part(function_call=types.FunctionCall(
                id="parallel-call-1", name="HassTurnOn", args={"name": "desk lamp"},
            ), thought_signature=signature_one),
            types.Part(function_call=types.FunctionCall(
                id="parallel-call-2", name="HassTurnOff", args={"name": "ceiling light"},
            ), thought_signature=signature_two),
        )
        second = response(types.Part(text="Both actions completed."))
        fake, client_patch = self.make_client([first, second])
        tools = [{"type": "function", "function": {
            "name": name, "description": "A harmless fixture tool.",
            "parameters": {"type": "object", "properties": {"name": {"type": "string"}}},
        }} for name in ("HassTurnOn", "HassTurnOff")]
        with client_patch:
            client = ProviderClient(None, self.settings())
            first_message = await client.complete([{"role": "user", "content": "Set two lights."}], tools)
            self.assertEqual([call["id"] for call in first_message["tool_calls"]],
                             ["parallel-call-1", "parallel-call-2"])
            result = await client.complete([
                {"role": "user", "content": "Set two lights."},
                {"role": "assistant", "content": None, "tool_calls": first_message["tool_calls"]},
                {"role": "tool", "tool_call_id": "parallel-call-1", "content": '{"success":true}'},
                {"role": "tool", "tool_call_id": "parallel-call-2", "content": '{"success":true}'},
            ], tools)

        self.assertEqual(result["content"], "Both actions completed.")
        contents = fake.models.requests[1]["contents"]
        self.assertEqual(len(contents), 3)
        self.assertEqual([part.function_call.id for part in contents[1].parts],
                         ["parallel-call-1", "parallel-call-2"])
        self.assertEqual([part.thought_signature for part in contents[1].parts],
                         [signature_one, signature_two])
        self.assertEqual(contents[2].role, "user")
        self.assertEqual(len(contents[2].parts), 2)
        self.assertEqual([part.function_response.id for part in contents[2].parts],
                         ["parallel-call-1", "parallel-call-2"])
        self.assertEqual([part.function_response.name for part in contents[2].parts],
                         ["HassTurnOn", "HassTurnOff"])

    async def test_native_error_classes_are_safe_and_do_not_echo_upstream_body(self):
        cases = [
            (errors.ClientError(403, {"message": "fixture-not-a-real-key"}), "invalid_auth"),
            (errors.ClientError(429, {"message": "quota"}), "quota"),
            (errors.ClientError(400, {"message": "bad model"}), "invalid_model"),
            (errors.ServerError(503, {"message": "unavailable"}), "cannot_connect"),
        ]
        for error, expected in cases:
            fake, client_patch = self.make_client([error])
            with client_patch:
                with self.assertRaises(ProviderError) as raised:
                    await ProviderClient(None, self.settings()).complete(
                        [{"role": "user", "content": "hello"}], [],
                    )
            self.assertEqual(raised.exception.code, expected)
            self.assertNotIn("fixture-not-a-real-key", str(raised.exception))
            self.assertTrue(fake.closed)

    async def test_structured_google_api_key_invalid_detail_maps_to_auth_error(self):
        error = errors.ClientError(400, {"error": {
            "code": 400,
            "status": "INVALID_ARGUMENT",
            "message": "The API key is invalid.",
            "details": [{
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": "API_KEY_INVALID",
                "domain": "googleapis.com",
            }],
        }})
        fake, client_patch = self.make_client([error])
        with client_patch:
            with self.assertRaises(ProviderError) as raised:
                await ProviderClient(None, self.settings()).complete(
                    [{"role": "user", "content": "hello"}], [],
                )
        self.assertEqual(raised.exception.code, "invalid_auth")
        self.assertTrue(fake.closed)

        generic_bad_request = errors.ClientError(400, {"error": {
            "code": 400,
            "status": "INVALID_ARGUMENT",
            "message": "Invalid model name.",
            "details": [{
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": "MODEL_NOT_FOUND",
            }],
        }})
        self.assertEqual(GeminiProvider._error_code(generic_bad_request), "invalid_model")

    async def test_timeout_and_cancellation_propagate_safely(self):
        async def stalled():
            await asyncio.sleep(1)

        fake, client_patch = self.make_client([stalled])
        with client_patch, patch("homeassistant.components.habab_home.providers_gemini.REQUEST_TIMEOUT", 0.001):
            with self.assertRaises(ProviderError) as raised:
                await ProviderClient(None, self.settings()).complete(
                    [{"role": "user", "content": "hello"}], [],
                )
        self.assertEqual(raised.exception.code, "timeout")
        self.assertTrue(fake.closed)

        fake, client_patch = self.make_client([stalled])
        with client_patch:
            task = asyncio.create_task(ProviderClient(None, self.settings()).complete(
                [{"role": "user", "content": "hello"}], [],
            ))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(fake.closed)

    async def test_malformed_history_and_safety_finish_are_rejected(self):
        fake, client_patch = self.make_client([
            response(types.Part(text="blocked"), finish=types.FinishReason.SAFETY),
        ])
        with client_patch:
            with self.assertRaises(ProviderError) as raised:
                await ProviderClient(None, self.settings()).complete(
                    [{"role": "user", "content": "hello"}], [],
                )
        self.assertEqual(raised.exception.code, "invalid_response")

        with self.assertRaises(ProviderError):
            await ProviderClient(None, self.settings()).complete(
                [{"role": "tool", "tool_call_id": "forged", "content": "{}"}], [],
            )

    async def test_custom_models_are_not_rejected_or_given_unsupported_thinking_level(self):
        fake, client_patch = self.make_client([response(types.Part(text="ready"))])
        settings = {**self.settings(), "model": "gemini-2.5-flash"}
        with client_patch:
            result = await ProviderClient(None, settings).complete(
                [{"role": "user", "content": "hello"}], [],
            )
        self.assertEqual(result["content"], "ready")
        self.assertEqual(fake.models.requests[0]["model"], "gemini-2.5-flash")
        self.assertIsNone(fake.models.requests[0]["config"].thinking_config)

        fake, client_patch = self.make_client([response(types.Part(text="ready"))])
        settings = {**self.settings(), "model": "gemini-3.8-flash"}
        with client_patch:
            result = await ProviderClient(None, settings).complete(
                [{"role": "user", "content": "hello"}], [],
            )
        self.assertEqual(result["content"], "ready")
        self.assertEqual(fake.models.requests[0]["model"], "gemini-3.8-flash")
        self.assertEqual(fake.models.requests[0]["config"].thinking_config.thinking_level, "LOW")

    async def test_other_providers_still_use_their_direct_http_transport(self):
        from homeassistant.components.habab_home.providers import ProviderClient as DirectClient

        class DirectSession:
            def __init__(self):
                self.url = None

            def post(self, url, **kwargs):
                self.url = url
                return _Response()

        for provider in ("openai", "deepseek"):
            session = DirectSession()
            with patch("homeassistant.components.habab_home.providers_gemini.genai.Client") as factory:
                await DirectClient(session, self.settings(provider)).complete(
                    [{"role": "user", "content": "hello"}], [],
                )
            self.assertEqual(session.url, PROVIDERS[provider]["url"])
            factory.assert_not_called()

    async def test_opencode_gemini_provider_uses_native_sdk_and_exact_model(self):
        from homeassistant.components.habab_home.setup_agent import SetupAgent

        process = SimpleNamespace(returncode=None)
        agent = object.__new__(SetupAgent)
        agent.process = None
        agent.fingerprint = None
        agent.root = Path("/private/runtime")
        agent.password = "private-password"
        agent.control_token = "private-control-token"
        agent.hass = SimpleNamespace(async_add_executor_job=AsyncMock())
        agent.settings = Mock(return_value=self.settings())
        agent.prepare_directories = Mock()
        agent.close = AsyncMock()
        agent.request = AsyncMock(return_value={})

        with patch.dict("os.environ", {
            "HABAB_RUNTIME_KIND": "docker",
            "HABAB_OPENCODE_BINARY": "/fake/opencode",
            "HABAB_OPENCODE_SHA256": "a" * 64,
        }):
            with patch("homeassistant.components.habab_home.setup_agent.verify_binary"):
                with patch("homeassistant.components.habab_home.setup_agent.asyncio.create_subprocess_exec",
                           new_callable=AsyncMock, return_value=process) as spawn:
                    await agent.ensure()

        config = json.loads(spawn.await_args.kwargs["env"]["OPENCODE_CONFIG_CONTENT"])
        provider = config["provider"]["habab-gemini"]
        self.assertEqual(provider["npm"], "@ai-sdk/google")
        self.assertEqual(provider["options"]["baseURL"],
                         "https://generativelanguage.googleapis.com/v1beta")
        self.assertEqual(config["model"], "habab-gemini/gemini-3.1-flash-lite")
        self.assertEqual(list(provider["models"]), ["gemini-3.1-flash-lite"])
        self.assertEqual(spawn.await_args.kwargs["env"]["HABAB_GEMINI_KEY"],
                         "fixture-not-a-real-key")


class _Response:
    status = 200

    class Content:
        async def iter_chunked(self, _size):
            yield (b'{"choices":[{"finish_reason":"stop","message":{"role":"assistant",'
                   b'"content":"hello"}}]}')

    content = Content()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


if __name__ == "__main__":
    unittest.main()
