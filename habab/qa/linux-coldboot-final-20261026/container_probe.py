"""One-shot loopback-only probe for the pinned disposable Habab Home image."""
import asyncio
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import secrets
import time

import aiohttp
import yaml

BASE = "http://127.0.0.1:8123"
START = time.monotonic()
OUT = {"root_http": None, "onboarding_page_http": None, "onboarding_api_http": None}


def config_metadata():
    path = Path("/config/configuration.yaml")
    if not path.is_file():
        return {"exists": False, "sha256": None, "top_level_keys": [], "habab_home": False}
    raw = path.read_bytes()
    parsed = yaml.safe_load(raw) or {}
    if not isinstance(parsed, dict):
        parsed = {}
    keys = sorted(str(key) for key in parsed.keys())
    return {"exists": True, "sha256": hashlib.sha256(raw).hexdigest(),
            "top_level_keys": keys, "habab_home": "habab_home" in parsed}


async def request_json(session, path, *, method="GET", payload=None, token=None, form=False, timeout=8):
    headers = {"Authorization": "Bearer " + token} if token else {}
    kwargs = {"headers": headers, "timeout": aiohttp.ClientTimeout(total=timeout)}
    if payload is not None:
        kwargs["data" if form else "json"] = payload
    async with session.request(method, BASE + path, **kwargs) as response:
        body = await response.text()
        try:
            value = json.loads(body) if body else None
        except json.JSONDecodeError:
            value = None
        return response.status, value


async def websocket_diagnostics(session, token):
    async with session.ws_connect("ws://127.0.0.1:8123/api/websocket", timeout=8) as ws:
        greeting = await ws.receive_json(timeout=8)
        if greeting.get("type") != "auth_required":
            return {"status": "unexpected_greeting"}
        await ws.send_json({"type": "auth", "access_token": token})
        auth = await ws.receive_json(timeout=8)
        if auth.get("type") != "auth_ok":
            return {"status": "auth_failed"}
        await ws.send_json({"id": 1, "type": "auth/current_user"})
        current = await ws.receive_json(timeout=10)
        user = current.get("result") or {}
        current_user = {
            "status": "ok" if current.get("type") == "result" and current.get("success") else "error",
            "is_owner": user.get("is_owner") is True,
            "is_admin": user.get("is_admin") is True,
            "is_active": user.get("is_active") is True,
            "id_present": isinstance(user.get("id"), str),
        }
        await ws.send_json({"id": 2, "type": "habab_home/feedback_delivery/status"})
        result = await ws.receive_json(timeout=10)
        if result.get("type") == "result" and result.get("success"):
            value = result.get("result") or {}
            feedback = {"status": "ok", "enabled": value.get("enabled"),
                        "credential_configured": value.get("credential_configured"),
                        "ready": value.get("ready")}
        else:
            error = result.get("error") or {}
            code = error.get("code")
            feedback = {"status": "error", "code": code if isinstance(code, str) else "unknown"}
        return current_user, feedback


async def main():
    OUT["seed_before_onboarding"] = config_metadata()
    async with aiohttp.ClientSession() as session:
        end = START + 48
        while time.monotonic() < end:
            try:
                root, _ = await request_json(session, "/", timeout=4)
                page, _ = await request_json(session, "/onboarding.html", timeout=4)
                api, onboarding = await request_json(session, "/api/onboarding", timeout=4)
                OUT.update({"root_http": root, "onboarding_page_http": page,
                            "onboarding_api_http": api})
                if root == 200 and page == 200 and api == 200 and isinstance(onboarding, list):
                    break
            except (aiohttp.ClientError, asyncio.TimeoutError):
                await asyncio.sleep(1)
        else:
            OUT["onboarding_wait"] = "deadline"
            return

        steps = {item.get("step") for item in onboarding if isinstance(item, dict)}
        OUT["onboarding_steps"] = sorted(item for item in steps if isinstance(item, str))
        if "user" not in steps:
            OUT["owner_creation"] = "user_step_unavailable"
            return

        username = "qa-coldboot-" + secrets.token_hex(5)
        password = secrets.token_urlsafe(24)
        client_id = "http://home-assistant.io"
        status, created = await request_json(session, "/api/onboarding/users", method="POST",
            payload={"client_id": client_id, "name": "Coldboot QA", "username": username,
                     "password": password, "language": "en"})
        OUT["owner_creation_http"] = status
        if status != 200 or not isinstance(created, dict) or not isinstance(created.get("auth_code"), str):
            OUT["owner_creation"] = "failed"
            return
        status, tokens = await request_json(session, "/auth/token", method="POST", form=True,
            payload={"grant_type": "authorization_code", "code": created["auth_code"],
                     "client_id": client_id})
        OUT["token_exchange_http"] = status
        token = tokens.get("access_token") if isinstance(tokens, dict) else None
        if status != 200 or not isinstance(token, str):
            OUT["owner_creation"] = "token_exchange_failed"
            return
        OUT["owner_creation"] = "PASS"
        OUT["auth_token_stored"] = "RAM_ONLY"

        deadline = START + 158
        first_state = None
        final_state = None
        final_safe_mode = None
        while time.monotonic() < deadline:
            try:
                status, config = await request_json(session, "/api/config", token=token, timeout=6)
                state = config.get("state") if status == 200 and isinstance(config, dict) else None
                if first_state is None and state is not None:
                    first_state = state
                if state is not None:
                    final_state = state
                    safe = config.get("safe_mode")
                    final_safe_mode = safe if isinstance(safe, bool) else None
                    if state == "RUNNING":
                        break
            except (aiohttp.ClientError, asyncio.TimeoutError):
                pass
            await asyncio.sleep(2)
        OUT["api_config_http"] = status
        OUT["api_config_first_state"] = first_state
        OUT["api_config_final_state"] = final_state
        OUT["safe_mode"] = final_safe_mode
        ready = final_state == "RUNNING"
        OUT["readiness"] = "PASS" if ready else "UNVERIFIED"

        if ready:
            status, config = await request_json(session, "/api/config", token=token)
            components = config.get("components", []) if status == 200 and isinstance(config, dict) else []
            OUT["habab_home_component"] = "habab_home" in components
            OUT["current_user"], OUT["feedback_delivery_status"] = await websocket_diagnostics(session, token)
            OUT["seed_after_core_config"] = config_metadata()
        else:
            OUT["post_readiness_checks"] = "NOT_RUN"

    if (OUT.get("habab_home_component") is not True
            or (OUT.get("feedback_delivery_status") or {}).get("status") == "error"
            and (OUT.get("feedback_delivery_status") or {}).get("code") == "unknown_command"):
        OUT["startup_diagnostics"] = {
            "google_genai_version": version_or_none("google-genai"),
            "manifest_present": Path("/usr/src/homeassistant/homeassistant/components/habab_home/manifest.json").is_file(),
            "init_present": Path("/usr/src/homeassistant/homeassistant/components/habab_home/__init__.py").is_file(),
            "elapsed_seconds": round(time.monotonic() - START, 1),
        }
    return


def version_or_none(name):
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


try:
    asyncio.run(main())
except Exception as exc:
    # Keep failure classification short and never include request bodies or credentials.
    OUT["probe_error"] = type(exc).__name__
    OUT["probe_error_message"] = str(exc)[:180]
print(json.dumps(OUT, sort_keys=True, separators=(",", ":")))
