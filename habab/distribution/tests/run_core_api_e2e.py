#!/usr/bin/env python3
"""Real Core API smoke tests with a local deterministic provider, never live-model QA.

Run after run_release_candidate_backend_tests.py in the same disposable Actions
container (--network none --memory=2g --cpus=2 --pids-limit=256). The only patched
product input is the two provider URLs in the child process; native Core startup,
onboarding, authentication, config flows, HTTP/WebSocket handlers and storage run
normally. OpenCode/model chat and physical/audio acceptance are NOT tested here.
"""
from __future__ import annotations

import argparse
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid

DIST = Path(__file__).resolve().parents[1]
VALID_KEY = "ci-fixture-provider-key-not-a-real-secret"
INVALID_KEY = "ci-fixture-rejected-key-not-a-real-secret"


class CheckFailed(Exception):
    """Fixed test identifiers only: never include server bodies or credentials."""


def require(condition, code):
    if not condition:
        raise CheckFailed(code)


class ProviderFixture:
    """A deterministic HTTP boundary fixture, not a model or device simulator."""

    def __init__(self):
        self.mode = "ok"
        self.requests = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 128 * 1024:
                    self.send_error(400)
                    return
                try:
                    body = json.loads(self.rfile.read(length))
                except (ValueError, UnicodeError):
                    self.send_error(400)
                    return
                fixture.requests.append({
                    "path": self.path,
                    "model": body.get("model"),
                    "tool_choice": body.get("tool_choice"),
                    "thinking_disabled": body.get("thinking") == {"type": "disabled"},
                })
                status, result = fixture.response(self.path, self.headers.get("Authorization"), body)
                encoded = json.dumps(result).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base(self):
        return f"http://127.0.0.1:{self.server.server_port}"

    def response(self, path, authorization, body):
        if path not in ("/openai/chat/completions", "/deepseek/chat/completions"):
            return 404, {"error": {"code": "fixture_route"}}
        if authorization != "Bearer " + VALID_KEY:
            return 401, {"error": {"code": "fixture_auth"}}
        if self.mode == "quota":
            return 429, {"error": {"code": "fixture_quota"}}
        if self.mode == "invalid_response":
            return 200, []
        message = {"role": "assistant", "content": None}
        if self.mode == "no_tools":
            message["content"] = "A fixture without the requested tool call."
        else:
            message["tool_calls"] = [{
                "id": "ci_fixture_tool", "type": "function", "function": {
                    "name": "habab_connection_test", "arguments": '{"ok":true}',
                },
            }]
        return 200, {"choices": [{"message": message}]}

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def run_ha_child(config, fixture_base):
    import homeassistant.components

    homeassistant.components.__path__.insert(0, str(DIST / "src"))
    from homeassistant.components.habab_home import providers

    providers.PROVIDERS["openai"]["url"] = fixture_base + "/openai/chat/completions"
    providers.PROVIDERS["deepseek"]["url"] = fixture_base + "/deepseek/chat/completions"
    from homeassistant.__main__ import main

    sys.argv = ["homeassistant", "--config", str(config), "--skip-pip"]
    return main()


class CoreAPI:
    def __init__(self, session, base):
        self.session, self.base, self.token = session, base, None
        self.ws = None
        self.sequence = 0

    async def http(self, method, path, *, body=None, form=None, authenticated=True):
        headers = {"Authorization": "Bearer " + self.token} if authenticated and self.token else {}
        async with self.session.request(method, self.base + path, json=body,
                                        data=form, headers=headers) as response:
            try:
                result = await response.json(content_type=None)
            except ValueError:
                result = None
            return response.status, result

    async def connect(self):
        self.ws = await self.session.ws_connect(self.base + "/api/websocket")
        require((await self.ws.receive_json())["type"] == "auth_required", "ws_auth_required")
        await self.ws.send_json({"type": "auth", "access_token": self.token})
        require((await self.ws.receive_json())["type"] == "auth_ok", "ws_owner_auth")

    async def command(self, command, *, error=None, **fields):
        self.sequence += 1
        await self.ws.send_json({"id": self.sequence, "type": command, **fields})
        while True:
            reply = await self.ws.receive_json()
            if reply.get("id") == self.sequence:
                break
        if error is not None:
            require(not reply.get("success") and reply.get("error", {}).get("code") == error,
                    "ws_expected_error_" + error)
            return reply
        require(reply.get("success") is True, "ws_failed_" + command.replace("/", "_"))
        return reply.get("result")

    async def entries(self):
        return (await self.command("habab_home/providers/list"))["connections"]


async def exercise(base, fixture, process, config):
    import aiohttp

    results = []

    def passed(name):
        results.append({"case": name, "status": "PASS"})
        print(json.dumps(results[-1]), flush=True)

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
        api = CoreAPI(session, base)
        deadline = time.monotonic() + 90
        while True:
            require(process.poll() is None, "core_exited_before_onboarding")
            try:
                status, flows = await api.http("GET", "/api/onboarding", authenticated=False)
                if status == 200 and isinstance(flows, list):
                    break
            except (aiohttp.ClientError, TimeoutError):
                pass
            require(time.monotonic() < deadline, "core_onboarding_timeout")
            await asyncio.sleep(0.5)
        require(any(row.get("step") == "user" and not row.get("done") for row in flows),
                "fresh_owner_onboarding_missing")
        status, owner = await api.http("POST", "/api/onboarding/users", authenticated=False, body={
            "client_id": base + "/", "name": "CI fixture owner", "username": "ci_fixture_owner",
            "password": secrets.token_urlsafe(24), "language": "en",
        })
        require(status == 200 and bool(owner.get("auth_code")), "owner_onboarding_failed")
        status, auth = await api.http("POST", "/auth/token", authenticated=False, form={
            "grant_type": "authorization_code", "code": owner["auth_code"], "client_id": base + "/",
        })
        require(status == 200 and bool(auth.get("access_token")), "owner_token_failed")
        api.token = auth["access_token"]
        status, _ = await api.http("GET", "/api/habab_home/status", authenticated=False)
        require(status == 401, "unauthenticated_status_allowed")
        bad = await session.ws_connect(base + "/api/websocket")
        await bad.receive_json()
        await bad.send_json({"type": "auth", "access_token": "ci-invalid-access-token"})
        require((await bad.receive_json())["type"] == "auth_invalid", "invalid_ws_token_allowed")
        await bad.close()
        await api.connect()
        while True:
            status, product = await api.http("GET", "/api/habab_home/status")
            if status == 200 and product.get("product") == "Habab Home":
                # HTTP can become ready before this component registers its WS commands.
                api.sequence += 1
                await api.ws.send_json({"id": api.sequence, "type": "habab_home/providers/list"})
                reply = await api.ws.receive_json()
                if reply.get("success"):
                    break
                require(reply.get("error", {}).get("code") == "unknown_command", "unexpected_component_startup_error")
            require(time.monotonic() < deadline, "habab_component_ready_timeout")
            await asyncio.sleep(0.5)
        passed("fresh_core_onboarding_owner_auth_and_component_ready")

        require(await api.entries() == [], "fresh_provider_entries_not_empty")
        await api.command("habab_home/agent/session", error="provider_required")
        status, voice = await api.http("POST", "/api/habab_home/realtime/session", body={"client": "native_v2"})
        require(status == 400 and voice.get("message") == "provider_required", "voice_provider_required")
        await api.command("habab_home/providers/save", provider="not_a_provider", model="ci-model",
                          api_key=VALID_KEY, error="invalid_provider")
        require(await api.entries() == [], "invalid_provider_was_persisted")
        passed("unconfigured_chat_voice_and_invalid_config_errors")

        settings = {"provider": "openai", "model": "gpt-4.1-mini", "api_key": VALID_KEY}
        saved = await api.command("habab_home/providers/save", **settings)
        require(saved.get("saved") and saved.get("tool_call_verified"), "provider_save_not_verified")
        entries = await api.entries()
        require(len(entries) == 1 and entries[0]["assistant_selected"], "saved_provider_not_selected")
        entry_id = entries[0]["entry_id"]
        checked = await api.command("habab_home/providers/check", entry_id=entry_id)
        require(checked.get("tool_call_verified") and checked.get("api_check_status") == "verified",
                "provider_check_not_verified")
        require(VALID_KEY not in json.dumps(entries), "provider_list_leaked_key")
        status, _ = await api.http("POST", f"/api/config/config_entries/entry/{entry_id}/reload")
        require(status == 200, "provider_reload_failed")
        after = (await api.entries())[0]
        key = await api.command("habab_home/providers/key", entry_id=entry_id)
        require(after["entry_id"] == entry_id and after["model"] == settings["model"]
                and after["assistant_selected"] and key["api_key"] == VALID_KEY,
                "native_provider_reload_lost_settings")
        persisted_deadline = time.monotonic() + 20
        while True:
            try:
                stored = json.loads((config / ".storage/core.config_entries").read_text())
                persisted = next((row for row in stored["data"]["entries"]
                                  if row["entry_id"] == entry_id), None)
                if (persisted and persisted["data"].get("api_key") == VALID_KEY
                        and persisted["data"].get("model") == settings["model"]
                        and persisted["options"].get("assistant_selected") is True):
                    break
            except (OSError, ValueError):
                pass
            require(time.monotonic() < persisted_deadline, "provider_not_persisted_to_native_storage")
            await asyncio.sleep(0.25)
        passed("native_provider_save_check_secret_projection_and_reload")

        await api.command("habab_home/providers/save", provider="openai", model="gpt-4.1-mini",
                          api_key=INVALID_KEY, error="invalid_auth")
        require((await api.command("habab_home/providers/key", entry_id=entry_id))["api_key"] == VALID_KEY,
                "rejected_save_overwrote_saved_key")
        for mode, code in (("quota", "quota"), ("no_tools", "tools_unsupported"),
                           ("invalid_response", "invalid_response")):
            fixture.mode = mode
            await api.command("habab_home/providers/check", entry_id=entry_id, error=code)
            state = (await api.entries())[0]
            require(state["api_check_status"] == "unknown" and state["api_check_reason"] == code,
                    "failed_check_missing_native_readback_" + code)
        fixture.mode = "ok"
        await api.command("habab_home/providers/check", entry_id=entry_id)
        passed("real_http_provider_errors_and_rejected_save_preserve_settings")

        await api.command("habab_home/providers/save", provider="deepseek", model="deepseek-flash", api_key=VALID_KEY)
        entries = await api.entries()
        require(len(entries) == 2 and [v["provider"] for v in entries if v["assistant_selected"]] == ["deepseek"],
                "provider_selection_not_persisted")
        require(any(v["path"] == "/openai/chat/completions" for v in fixture.requests)
                and any(v["path"] == "/deepseek/chat/completions" and v["thinking_disabled"] for v in fixture.requests),
                "selected_provider_http_transport_mismatch")
        status, voice = await api.http("POST", "/api/habab_home/realtime/session", body={"client": "native_v2"})
        require(status == 400 and voice.get("message") == "realtime_provider_required", "deepseek_voice_not_explicit")
        passed("two_provider_selection_and_explicit_unsupported_voice")

        helper = await api.command("input_boolean/create", name="CI virtual-only switch", initial=False)
        helper_id = helper["id"]
        registry = await api.command("config/entity_registry/list")
        entity = next(v["entity_id"] for v in registry if v.get("platform") == "input_boolean"
                      and v.get("unique_id") == helper_id)
        try:
            for action, expected in (("turn_on", "on"), ("turn_off", "off")):
                await api.command("call_service", domain="input_boolean", service=action,
                                  service_data={"entity_id": entity})
                status, state = await api.http("GET", "/api/states/" + entity)
                require(status == 200 and state.get("state") == expected, "virtual_service_state_not_persisted")
        finally:
            await api.command("input_boolean/delete", input_boolean_id=helper_id)
        require(not any(v["id"] == helper_id for v in await api.command("input_boolean/list")), "helper_cleanup_failed")
        status, _ = await api.http("GET", "/api/states/" + entity)
        require(status == 404, "deleted_helper_state_survived")
        passed("native_virtual_helper_service_cycle_and_exact_cleanup")

        status, body = await api.http("POST", "/api/habab_home/feedback", body={})
        require(status == 400 and body.get("message") == "invalid_feedback", "feedback_invalid_body_contract")
        status, body = await api.http("POST", "/api/habab_home/feedback", body={
            "session_id": "ci_missing_session", "feedback": "Local CI fixture feedback",
            "consent": True, "idempotency_key": str(uuid.uuid4()),
        })
        require(status == 404 and body.get("message") == "session_not_found", "feedback_foreign_session_contract")
        status, _ = await api.http("GET", "/api/habab_home/feedback/" + str(uuid.uuid4()))
        require(status == 404, "feedback_missing_receipt_not_hidden")
        status, body = await api.http("POST", "/api/habab_home/realtime/context", body={"session_id": "voice2_ci_missing"})
        require(status == 403 and body.get("message") == "invalid_session", "voice_context_unknown_session_allowed")
        passed("feedback_and_voice_context_session_boundaries")

        for entry in await api.entries():
            status, _ = await api.http("DELETE", "/api/config/config_entries/entry/" + entry["entry_id"])
            require(status == 200, "temporary_provider_cleanup_failed")
        require(await api.entries() == [], "provider_entries_survived_cleanup")
        await api.command("habab_home/agent/session", new=True, error="provider_required")
        await api.ws.close()
        passed("native_provider_cleanup_and_new_chat_without_provider_error")
    return results


def stop_child(process):
    if process is None or process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        process.wait(timeout=5)
        return
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--ha-child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--config", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--fixture-base", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if os.environ.get("GITHUB_ACTIONS") != "true":
        parser.error("Run only in the disposable GitHub Actions preflight container; local HA instances are not touched")
    if args.ha_child:
        return run_ha_child(args.config, args.fixture_base)
    os.umask(0o077)
    summary = {"suite": "real_core_api_e2e", "provider": "deterministic_loopback_http_fixture",
               "live_model_acceptance": "NOT_RUN", "opencode_chat_tool_trace": "NOT_RUN",
               "physical_devices_audio": "NOT_RUN", "status": "FAIL"}
    process = None
    try:
        with tempfile.TemporaryDirectory(prefix="habab-core-api-e2e-") as directory, ProviderFixture() as fixture:
            config = Path(directory)
            with socket.socket() as port_socket:
                port_socket.bind(("127.0.0.1", 0))
                port = port_socket.getsockname()[1]
            (config / "configuration.yaml").write_text(
                "homeassistant:\n  name: Disposable CI fixture\n  country: GB\n  time_zone: UTC\n"
                "api:\nconfig:\nfrontend:\nonboarding:\nhabab_home:\ninput_boolean:\n"
                f"http:\n  server_host: 127.0.0.1\n  server_port: {port}\n"
            )
            with (config / "private-child-output.log").open("w") as log:
                env = {key: os.environ[key] for key in ("PATH", "LANG", "TZ") if key in os.environ}
                env.update(GITHUB_ACTIONS="true", HOME=directory, PYTHONDONTWRITEBYTECODE="1")
                process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--ha-child",
                    "--config", directory, "--fixture-base", fixture.base], stdout=log, stderr=log,
                    env=env, start_new_session=True)
                try:
                    summary["cases"] = asyncio.run(exercise(f"http://127.0.0.1:{port}", fixture, process, config))
                    summary["status"] = "PASS"
                    summary["provider_http_requests"] = len(fixture.requests)
                finally:
                    stop_child(process)
                    summary["ha_child_stopped"] = process.poll() is not None
    except CheckFailed as error:
        summary["failure_code"] = str(error)
    except Exception as error:
        summary["failure_code"] = type(error).__name__
    finally:
        stop_child(process)
    if args.report:
        args.report.write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, sort_keys=True), flush=True)
    return 0 if summary["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
