"""Owner-scoped Xiaomi installation and native OAuth handoff inside the chat."""
from __future__ import annotations

import asyncio
from html.parser import HTMLParser
import json
from pathlib import Path
import secrets
import sys
from urllib.parse import parse_qs, urlencode, urlsplit
import uuid

import probatio
from homeassistant.components import webhook, websocket_api
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import callback
from homeassistant.data_entry_flow import UnknownFlow
from homeassistant.helpers import device_registry, entity_registry
from homeassistant.helpers.storage import Store

from .const import DOMAIN

REGIONS = {"cn", "de", "i2", "ru", "sg", "us"}
REDIRECT_ORIGIN = "http://homeassistant.local:8123"
COMPONENT_VERSION = "v0.5.0"
ACTIVE_PHASES = {"oauth", "homes", "configuring"}


class SetupError(Exception):
    """A bounded UI error code, never provider responses or credentials."""


class _AuthorizationLink(HTMLParser):
    url = None

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.url = dict(attrs).get("href")


def authorization_link(result):
    parser = _AuthorizationLink()
    parser.feed(result.get("description_placeholders", {}).get("link_left", ""))
    value = parser.url
    if not isinstance(value, str) or len(value) > 8192:
        raise SetupError("invalid_authorization_link")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname != "account.xiaomi.com"
            or parsed.username or parsed.password or parsed.port not in (None, 443)):
        raise SetupError("invalid_authorization_link")
    query = parse_qs(parsed.query)
    redirect = query.get("redirect_uri", [""])[0]
    target = urlsplit(redirect)
    if (target.scheme + "://" + target.netloc != REDIRECT_ORIGIN
            or not target.path.startswith("/api/webhook/") or not query.get("state")):
        raise SetupError("invalid_authorization_link")
    return value


def callback_request(value, expected_authorization):
    """Validate an owner-pasted redirect without making arbitrary HTTP requests."""
    if not isinstance(value, str) or len(value) > 8192:
        raise SetupError("invalid_callback")
    parsed = urlsplit(value.strip())
    expected = parse_qs(urlsplit(expected_authorization).query)
    redirect = urlsplit(expected["redirect_uri"][0])
    query = parse_qs(parsed.query)
    if (parsed.scheme != redirect.scheme or parsed.netloc != redirect.netloc
            or parsed.path != redirect.path or parsed.fragment
            or set(query) != {"code", "state"}
            or any(len(query[key]) != 1 for key in ("code", "state"))
            or not 1 <= len(query["code"][0]) <= 2048
            or not secrets.compare_digest(query["state"][0], expected["state"][0])):
        raise SetupError("invalid_callback")
    return redirect.path.removeprefix("/api/webhook/"), urlencode({
        "code": query["code"][0], "state": query["state"][0],
    })


def home_form(result):
    homes, area_rule = {}, None
    for key, validator in result.get("data_schema").schema.items():
        name = getattr(key, "schema", key)
        if name == "home_infos":
            homes = getattr(validator, "options", {})
        elif name == "area_name_rule" and callable(getattr(key, "default", None)):
            area_rule = key.default()
    if not isinstance(homes, dict) or area_rule is None:
        raise SetupError("unexpected_flow")
    return homes, area_rule


class XiaomiSetup:
    def __init__(self, hass):
        self.hass = hass
        self.store = Store(hass, 1, "habab_home.xiaomi_setup", private=True)
        self.records = {}
        self.boot_id = uuid.uuid4().hex
        self._lock = asyncio.Lock()
        self._flows = {}
        self._loaded_at_start = False

    def _component_present(self):
        path = Path(self.hass.config.path("custom_components/xiaomi_home/manifest.json"))
        try:
            manifest = json.loads(path.read_text())
            return manifest.get("domain") == "xiaomi_home" and manifest.get("version") == COMPONENT_VERSION
        except (OSError, ValueError):
            return False

    async def setup(self):
        self.records = await self.store.async_load() or {}
        self._loaded_at_start = await self.hass.async_add_executor_job(self._component_present)
        for record in self.records.values():
            if record.get("boot_id") != self.boot_id:
                record.pop("flow_id", None)
                if record.get("entry_id") and self.hass.config_entries.async_get_entry(record["entry_id"]):
                    record["phase"] = "configuring"
                elif record.get("phase") in ACTIVE_PHASES | {"restart_required", "restarting", "installing"}:
                    record["phase"] = "ready" if self._loaded_at_start else "install_required"
                record["boot_id"] = self.boot_id
        await self._save()

    async def _save(self):
        # OAuth links, callback codes and native config-entry data never enter this Store.
        await self.store.async_save(self.records)

    def _record(self, owner):
        if owner not in self.records:
            raise SetupError("setup_not_requested")
        return self.records[owner]

    def _existing_entry(self):
        return next(iter(self.hass.config_entries.async_entries("xiaomi_home")), None)

    def _summary(self, owner):
        record = self._record(owner)
        result = {key: record[key] for key in ("phase", "region", "error") if key in record}
        if record.get("entry_id"):
            entry = self.hass.config_entries.async_get_entry(record["entry_id"])
            result["entry_state"] = entry.state.value if entry else "missing"
            result["device_count"] = sum(record["entry_id"] == d.config_entry_id
                for d in device_registry.async_get(self.hass).devices)
            result["entity_count"] = sum(e.config_entry_id == record["entry_id"]
                for e in entity_registry.async_get(self.hass).entities.values())
        return result

    async def request(self, owner):
        async with self._lock:
            if (owner in self.records and self.records[owner].get("phase") == "completed"
                    and not self.hass.config_entries.async_get_entry(self.records[owner].get("entry_id"))):
                del self.records[owner]
            if owner not in self.records:
                entry = self._existing_entry()
                self.records[owner] = {"phase": ("completed" if entry.state.value == "loaded" else "configuring") if entry else
                    ("ready" if self._loaded_at_start else "install_required"),
                    "region": "ru", "boot_id": self.boot_id}
                if entry:
                    self.records[owner]["entry_id"] = entry.entry_id
                await self._save()
            return self._summary(owner)

    async def install(self, owner, accept_license):
        async with self._lock:
            record = self._record(owner)
            if record["phase"] != "install_required" or accept_license is not True:
                raise SetupError("license_required")
            record.update(phase="installing", error=None)
            await self._save()
            process = None
            try:
                process = await asyncio.create_subprocess_exec(
                    sys.executable, "/usr/local/bin/habab-install-xiaomi-home",
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                async with asyncio.timeout(40):
                    raw, _ = await process.communicate()
                result = json.loads(raw) if len(raw) <= 4096 else {}
                if process.returncode or result.get("installed") is not True:
                    raise SetupError("installation_failed")
            except (OSError, TimeoutError, ValueError, SetupError):
                if process and process.returncode is None:
                    process.kill()
                    await process.wait()
                record.update(phase="install_required", error="installation_failed")
                await self._save()
                raise SetupError("installation_failed") from None
            record.update(phase="restart_required", boot_id=self.boot_id)
            await self._save()
            return self._summary(owner)

    async def restart(self, owner):
        async with self._lock:
            record = self._record(owner)
            if record["phase"] != "restart_required":
                raise SetupError("restart_not_required")
            record.update(phase="restarting", error=None)
            await self._save()
            async def restart_later():
                await asyncio.sleep(1)
                try:
                    await self.hass.services.async_call("homeassistant", "restart", {}, blocking=True)
                except Exception:
                    record.update(phase="restart_required", error="restart_failed")
                    await self._save()
            self.hass.async_create_task(restart_later())
            return self._summary(owner)

    async def begin(self, owner, region, accept_license, language):
        async with self._lock:
            record = self._record(owner)
            if record["phase"] not in {"ready", "error"} or accept_license is not True:
                raise SetupError("license_required")
            if region not in REGIONS or language not in {"en", "ru"}:
                raise SetupError("invalid_region")
            entry = self._existing_entry()
            if entry:
                record.update(phase="completed" if entry.state.value == "loaded" else "configuring",
                    entry_id=entry.entry_id, error=None)
                await self._save()
                return await self._ui_state(owner)
            if record.get("flow_id"):
                try:
                    self.hass.config_entries.flow.async_abort(record["flow_id"])
                except UnknownFlow:
                    pass
                record.pop("flow_id", None)
                self._flows.pop(owner, None)
            result = await self.hass.config_entries.flow.async_init("xiaomi_home", context={"source": SOURCE_USER})
            flow_id = result.get("flow_id")
            if result.get("step_id") != "eula" or not flow_id:
                raise SetupError("unexpected_flow")
            record["flow_id"] = flow_id
            self._flows[owner] = result
            result = await self.hass.config_entries.flow.async_configure(flow_id, {"eula": True})
            if result.get("step_id") != "auth_config":
                raise SetupError("unexpected_flow")
            result = await self.hass.config_entries.flow.async_configure(flow_id, {
                "cloud_server": region, "integration_language": language,
                "oauth_redirect_url": REDIRECT_ORIGIN, "network_detect_config": False})
            if result.get("step_id") != "oauth" or result.get("type") != "progress":
                raise SetupError("unexpected_flow")
            authorization_link(result)
            self._flows[owner] = result
            record.update(phase="oauth", region=region, error=None)
            await self._save()
            return await self._ui_state(owner)

    async def refresh(self, owner):
        if owner not in self.records:
            return {"phase": "idle"}
        async with self._lock:
            record = self._record(owner)
            if record["phase"] in {"oauth", "homes"}:
                try:
                    result = await self.hass.config_entries.flow.async_configure(record["flow_id"])
                    if result.get("type") == "progress_done":
                        result = await self.hass.config_entries.flow.async_configure(record["flow_id"])
                    await self._advance(owner, result)
                except Exception:
                    record.update(phase="error", error="flow_failed")
                    await self._save()
            if record["phase"] == "configuring":
                entry = self.hass.config_entries.async_get_entry(record["entry_id"])
                if entry and entry.state.value == "loaded":
                    record["phase"] = "completed"
                    await self._save()
                elif not entry or entry.state.value in {"setup_error", "setup_retry", "migration_error"}:
                    record.update(phase="error", error="integration_not_loaded")
                    await self._save()
            if record["phase"] == "completed" and not self.hass.config_entries.async_get_entry(record.get("entry_id")):
                record.update(phase="ready" if self._loaded_at_start else "install_required", error=None)
                record.pop("entry_id", None)
                await self._save()
            return await self._ui_state(owner)

    async def _advance(self, owner, result):
        record = self._record(owner)
        self._flows[owner] = result
        if result.get("type") == "create_entry":
            record.update(phase="configuring", entry_id=result["result"].entry_id, error=None)
            record.pop("flow_id", None)
            self._flows.pop(owner, None)
        elif result.get("step_id") == "homes_select":
            homes, area_rule = home_form(result)
            if any(result.get("errors", {}).values()) or not homes:
                record.update(phase="error", error="no_devices_in_region")
            elif len(homes) == 1:
                result = await self.hass.config_entries.flow.async_configure(record["flow_id"], {
                    "home_infos": list(homes), "area_name_rule": area_rule, "advanced_options": False})
                await self._advance(owner, result)
                return
            else:
                record["phase"] = "homes"
        elif result.get("step_id") != "oauth" or result.get("type") not in {"progress", "form"}:
            record.update(phase="error", error="flow_failed")
        await self._save()

    async def select_homes(self, owner, selected):
        async with self._lock:
            record = self._record(owner)
            if record["phase"] != "homes":
                raise SetupError("unexpected_flow")
            homes, area_rule = home_form(self._flows[owner])
            if (not isinstance(selected, list) or not selected
                    or any(not isinstance(x, str) or x not in homes for x in selected)):
                raise SetupError("invalid_homes")
            result = await self.hass.config_entries.flow.async_configure(record["flow_id"], {
                "home_infos": selected, "area_name_rule": area_rule, "advanced_options": False})
            await self._advance(owner, result)
            return await self._ui_state(owner)

    async def complete_callback(self, owner, value):
        async with self._lock:
            record = self._record(owner)
            if record["phase"] != "oauth":
                raise SetupError("unexpected_flow")
            webhook_id, query = callback_request(value, authorization_link(self._flows[owner]))
            request = webhook.MockRequest(b"", "habab_home_xiaomi_setup", method="GET",
                query_string=query, remote="127.0.0.1")
            await webhook.async_handle_webhook(self.hass, webhook_id, request)
        return await self.refresh(owner)

    async def _ui_state(self, owner):
        state = self._summary(owner)
        record = self._record(owner)
        if record["phase"] == "oauth":
            state["authorization_url"] = authorization_link(self._flows[owner])
        elif record["phase"] == "homes":
            homes, _ = home_form(self._flows[owner])
            state["homes"] = [{"value": key, "label": label} for key, label in homes.items()]
        return state


@callback
def async_register_commands(hass):
    websocket_api.async_register_command(hass, get_setup)
    websocket_api.async_register_command(hass, act_setup)


@websocket_api.websocket_command({probatio.Required("type"): "habab_home/xiaomi/get"})
@websocket_api.require_admin
@websocket_api.async_response
async def get_setup(hass, connection, msg):
    try:
        result = await hass.data[DOMAIN]["xiaomi_setup"].refresh(connection.user.id)
        connection.send_result(msg["id"], result)
    except SetupError as error:
        connection.send_error(msg["id"], str(error), str(error))
    except Exception:
        connection.send_error(msg["id"], "setup_failed", "setup_failed")


@websocket_api.websocket_command({
    probatio.Required("type"): "habab_home/xiaomi/act",
    probatio.Required("action"): probatio.In({"request", "install", "restart", "begin", "callback", "homes"}),
    probatio.Optional("region", default="ru"): str,
    probatio.Optional("language", default="ru"): str,
    probatio.Optional("accept_license", default=False): bool,
    probatio.Optional("callback_url", default=""): probatio.All(str, probatio.Length(max=8192)),
    probatio.Optional("homes", default=[]): list,
})
@websocket_api.require_admin
@websocket_api.async_response
async def act_setup(hass, connection, msg):
    manager = hass.data[DOMAIN]["xiaomi_setup"]
    owner = connection.user.id
    try:
        match msg["action"]:
            case "request": result = await manager.request(owner)
            case "install": result = await manager.install(owner, msg["accept_license"])
            case "restart": result = await manager.restart(owner)
            case "begin": result = await manager.begin(owner, msg["region"], msg["accept_license"], msg["language"])
            case "callback": result = await manager.complete_callback(owner, msg["callback_url"])
            case "homes": result = await manager.select_homes(owner, msg["homes"])
        connection.send_result(msg["id"], result)
    except SetupError as error:
        connection.send_error(msg["id"], str(error), str(error))
    except Exception:
        # Native/provider exceptions can contain credential-bearing URLs.
        connection.send_error(msg["id"], "setup_failed", "setup_failed")
