"""Use HA's own authenticated APIs for the active administrator's task.

The temporary native credential stays inside Core and is revoked after the call.
There is no per-feature command whitelist: HA applies its normal permissions.
"""
import asyncio
import base64
from contextlib import ExitStack
import hashlib
import os
from pathlib import Path
import tempfile

import aiohttp
from datetime import timedelta
import json
from urllib.parse import urlsplit

from homeassistant.helpers.aiohttp_client import async_get_clientsession

MAX_NATIVE_BYTES = 4 * 1024 * 1024
ORIGIN = "http://127.0.0.1:8123"
METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


def validate_request(body):
    protocol = body.get("protocol")
    if protocol == "websocket":
        message = body.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("type"), str):
            raise ValueError("native_message_required")
        if not message["type"] or "id" in message or "access_token" in message:
            raise ValueError("native_message_invalid")
    elif protocol == "rest":
        path, method = body.get("path"), body.get("method", "GET")
        if not isinstance(path, str) or not path.startswith("/api/"):
            raise ValueError("native_path_invalid")
        parsed = urlsplit(path)
        if parsed.scheme or parsed.netloc or parsed.fragment or any(c in path for c in "\r\n\\"):
            raise ValueError("native_path_invalid")
        if method not in METHODS:
            raise ValueError("native_method_invalid")
    else:
        raise ValueError("native_protocol_invalid")
    if len(json.dumps(body).encode()) > MAX_NATIVE_BYTES:
        raise ValueError("native_request_too_large")


async def native_api(hass, manager, owner, body):
    """Relay one native REST/WS operation with the same user as the active turn."""
    validate_request(body)
    active = manager.active
    if not active or active[0] != owner:
        raise ValueError("inactive")
    user = await hass.auth.async_get_user(owner)
    if user is None or not user.is_active or not user.is_admin:
        raise ValueError("not_allowed")
    if manager.active != active:
        raise ValueError("inactive")
    refresh = await hass.auth.async_create_refresh_token(
        user, client_id=ORIGIN + "/", client_name="Habab active assistant task",
        access_token_expiration=timedelta(seconds=45))
    try:
        token = hass.auth.async_create_access_token(refresh)
        session = async_get_clientsession(hass)
        async with asyncio.timeout(300 if body.get("save_to") or body.get("files") else 35):
            if body["protocol"] == "websocket":
                async with session.ws_connect(ORIGIN + "/api/websocket", max_msg_size=MAX_NATIVE_BYTES) as ws:
                    greeting = await ws.receive_json()
                    if greeting.get("type") != "auth_required":
                        raise ValueError("native_auth_protocol_error")
                    await ws.send_json({"type": "auth", "access_token": token})
                    if (await ws.receive_json()).get("type") != "auth_ok":
                        raise ValueError("native_auth_failed")
                    await ws.send_json({**body["message"], "id": 1})
                    while True:
                        result = await ws.receive_json()
                        if result.get("type") == "result" and result.get("id") == 1:
                            response = {"ok": result.get("success") is True,
                                        "result": result.get("result") if result.get("success") else result.get("error")}
                            break
            else:
                response = await _rest(session, token, body)
        if manager.active != active:
            raise ValueError("inactive")
        return response
    finally:
        hass.auth.async_remove_refresh_token(refresh)


async def _rest(session, token, body):
    """Preserve native JSON, YAML/text, multipart uploads and file downloads."""
    kwargs = {"headers": {"Authorization": "Bearer " + token}, "allow_redirects": False}
    with ExitStack() as files:
        if body.get("files"):
            if not isinstance(body["files"], dict):
                raise ValueError("native_files_invalid")
            form = aiohttp.FormData()
            for name, value in body.get("body", {}).items():
                form.add_field(name, str(value))
            for name, path in body["files"].items():
                stream = await asyncio.to_thread(Path(path).open, "rb")
                files.callback(stream.close)
                form.add_field(name, stream, filename=Path(path).name)
            kwargs["data"] = form
        elif "body" in body:
            if body.get("body_format", "json") == "json":
                kwargs["json"] = body["body"]
            elif body.get("body_format") == "text" and isinstance(body["body"], str):
                kwargs["data"] = body["body"].encode()
                kwargs["headers"]["Content-Type"] = body.get("content_type", "text/plain")
            elif body.get("body_format") == "base64":
                kwargs["data"] = base64.b64decode(body["body"], validate=True)
                kwargs["headers"]["Content-Type"] = body.get("content_type", "application/octet-stream")
            else:
                raise ValueError("native_body_format_invalid")
        async with session.request(body.get("method", "GET"), ORIGIN + body["path"], **kwargs) as result:
            ok = 200 <= result.status < 300
            if ok and body.get("save_to"):
                value = await _save_download(result, body["save_to"])
                encoding = "file"
            else:
                raw = bytearray()
                async for block in result.content.iter_chunked(65536):
                    raw.extend(block)
                    if len(raw) > MAX_NATIVE_BYTES:
                        raise ValueError("native_response_too_large_use_save_to")
                content_type = result.headers.get("Content-Type", "")
                if "json" in content_type and raw:
                    value, encoding = json.loads(raw), "json"
                elif content_type.startswith("text/") or not raw:
                    value, encoding = raw.decode("utf-8"), "text"
                else:
                    value, encoding = base64.b64encode(raw).decode("ascii"), "base64"
            return {"ok": ok, "status": result.status,
                    "content_type": result.headers.get("Content-Type", ""),
                    "encoding": encoding, "result": value}


def _download_file(destination, expected):
    destination = Path(destination)
    if not destination.is_absolute() or destination.exists():
        raise ValueError("native_download_requires_unused_absolute_path")
    destination.parent.mkdir(parents=True, exist_ok=True)
    free = os.statvfs(destination.parent)
    available = free.f_bavail * free.f_frsize
    if available < (expected or 0) + 64 * 1024 ** 2:
        raise ValueError("native_download_no_space")
    descriptor, temporary = tempfile.mkstemp(prefix=".habab-download-", dir=destination.parent)
    return destination, temporary, os.fdopen(descriptor, "wb")


async def _save_download(response, destination):
    expected = response.headers.get("Content-Length")
    expected = int(expected) if expected and expected.isdigit() else None
    destination, temporary, output = await asyncio.to_thread(_download_file, destination, expected)
    count = 0
    digest = hashlib.sha256()
    try:
        async for block in response.content.iter_chunked(65536):
            count += len(block)
            if count > 8 * 1024 ** 3:
                raise ValueError("native_download_too_large")
            if count % (8 * 1024 ** 2) < len(block):
                space = await asyncio.to_thread(os.statvfs, destination.parent)
                if space.f_bavail * space.f_frsize < 64 * 1024 ** 2:
                    raise ValueError("native_download_no_space")
            await asyncio.to_thread(output.write, block)
            digest.update(block)
        await asyncio.to_thread(output.close)
        await asyncio.to_thread(os.replace, temporary, destination)
        return {"path": str(destination), "bytes": count, "sha256": digest.hexdigest()}
    finally:
        await asyncio.to_thread(output.close)
        if await asyncio.to_thread(os.path.exists, temporary):
            await asyncio.to_thread(os.unlink, temporary)
