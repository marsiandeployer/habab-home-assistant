"""Administrator-only connection settings; secrets never appear in results."""

import hashlib
import json
from datetime import datetime, timezone

import probatio

from homeassistant.components import websocket_api
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN
from .effective_provider import effective_provider_entry
from .providers import ProviderClient, ProviderError, resolve_provider_contract
from .setup_agent import AgentError


_API_CHECK_OPTION = "habab_api_check"
_API_CHECK_STATUSES = {"verified", "rejected", "unknown"}
_API_CHECK_REASONS = {
    "invalid_provider", "invalid_auth", "quota", "invalid_model", "cannot_connect", "timeout",
    "invalid_response", "provider_not_ready", "tools_unsupported",
}


def _settings_fingerprint(data):
    """Identify the exact settings that a provider check used without exposing them."""
    value = json.dumps(
        [data.get("provider"), data.get("model"), data.get("api_key")],
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _api_check_readback(entry):
    """Return only a matching check outcome; never return the settings fingerprint."""
    record = entry.options.get(_API_CHECK_OPTION)
    if not isinstance(record, dict) or record.get("fingerprint") != _settings_fingerprint(entry.data):
        return {"api_check_status": "unknown", "api_check_at": None, "api_check_reason": None}
    status = record.get("status")
    if status not in _API_CHECK_STATUSES:
        return {"api_check_status": "unknown", "api_check_at": None, "api_check_reason": None}
    checked_at = record.get("checked_at")
    reason = record.get("reason")
    return {
        "api_check_status": status,
        "api_check_at": checked_at if isinstance(checked_at, str) else None,
        "api_check_reason": reason if isinstance(reason, str) and reason in _API_CHECK_REASONS else None,
    }


def _record_api_check(hass, entry, fingerprint, status, reason=None):
    """Persist a terminal result for one exact provider/model/key combination."""
    if _settings_fingerprint(entry.data) != fingerprint:
        return False
    options = {
        **entry.options,
        _API_CHECK_OPTION: {
            "fingerprint": fingerprint,
            "status": status if status in _API_CHECK_STATUSES else "unknown",
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "reason": reason if isinstance(reason, str) and reason in _API_CHECK_REASONS else None,
        },
    }
    hass.config_entries.async_update_entry(entry, options=options)
    return True


@callback
def async_register_commands(hass):
    websocket_api.async_register_command(hass, list_connections)
    websocket_api.async_register_command(hass, get_connection_key)
    websocket_api.async_register_command(hass, save_connection)
    websocket_api.async_register_command(hass, check_connection)
    websocket_api.async_register_command(hass, agent_session)
    websocket_api.async_register_command(hass, agent_run)
    websocket_api.async_register_command(hass, agent_abort)
    websocket_api.async_register_command(hass, agent_diagnostics)


@websocket_api.websocket_command({
    probatio.Required("type"): "habab_home/agent/session",
    probatio.Optional("new", default=False): bool,
})
@websocket_api.require_admin
@websocket_api.async_response
async def agent_session(hass, connection, msg):
    try:
        result = await hass.data[DOMAIN]["assistant"].web_session(connection.user.id, msg["new"])
        connection.send_result(msg["id"], result)
    except AgentError as error:
        connection.send_error(msg["id"], error.code, error.code)


@websocket_api.websocket_command({
    probatio.Required("type"): "habab_home/agent/run",
    probatio.Required("session_id"): str,
    probatio.Required("text"): probatio.All(str, probatio.Length(min=1, max=4000)),
})
@websocket_api.require_admin
@websocket_api.async_response
async def agent_run(hass, connection, msg):
    try:
        if not msg["text"].strip():
            raise AgentError("empty_message")
        result = await hass.data[DOMAIN]["assistant"].run(
            connection.user.id, "web", msg["session_id"], msg["text"])
        connection.send_result(msg["id"], result)
    except AgentError as error:
        connection.send_error(msg["id"], error.code, error.code)


@websocket_api.websocket_command({
    probatio.Required("type"): "habab_home/agent/abort",
    probatio.Required("session_id"): str,
})
@websocket_api.require_admin
@websocket_api.async_response
async def agent_abort(hass, connection, msg):
    try:
        stopped = await hass.data[DOMAIN]["assistant"].abort(
            connection.user.id, "web", msg["session_id"])
        connection.send_result(msg["id"], {"stopped": stopped})
    except AgentError as error:
        connection.send_error(msg["id"], error.code, error.code)


@websocket_api.websocket_command({
    probatio.Required("type"): "habab_home/agent/diagnostics",
    probatio.Required("session_id"): str,
})
@websocket_api.require_admin
@websocket_api.async_response
async def agent_diagnostics(hass, connection, msg):
    try:
        result = await hass.data[DOMAIN]["assistant"].diagnostics_for_web(
            connection.user.id, msg["session_id"])
        connection.send_result(msg["id"], result)
    except AgentError as error:
        connection.send_error(msg["id"], error.code, error.code)


@websocket_api.websocket_command({probatio.Required("type"): "habab_home/providers/list"})
@websocket_api.require_admin
@websocket_api.async_response
async def list_connections(hass, connection, msg):
    registry = er.async_get(hass)
    config_entries = hass.config_entries.async_entries(DOMAIN)
    selected_entry = effective_provider_entry(config_entries)
    entries = []
    for entry in config_entries:
        entities = {
            item.domain: item.entity_id
            for item in er.async_entries_for_config_entry(registry, entry.entry_id)
            if not item.disabled
        }
        if entry is not selected_entry and entry.data["provider"] == "habab":
            text_metadata = {
                "managed_model": None,
                "effective_model": None,
                "protocol": None,
                "text_ready": None,
                "text_error": "not_selected",
            }
        else:
            try:
                contract = await resolve_provider_contract(
                    async_get_clientsession(hass) if entry.data["provider"] == "habab" else None,
                    entry.data,
                )
                text_metadata = {
                    "managed_model": contract.managed_model,
                    "effective_model": contract.model,
                    "protocol": contract.protocol,
                    "text_ready": contract.text_ready,
                }
            except ProviderError as error:
                text_metadata = {
                    "managed_model": None,
                    "effective_model": None,
                    "protocol": "unavailable",
                    "text_ready": False,
                    "text_error": error.code,
                }
        entries.append({
            "entry_id": entry.entry_id, "provider": entry.data["provider"],
            "model": entry.data["model"], "state": entry.state.value,
            "assistant_selected": entry is selected_entry,
            "entities": entities, **_api_check_readback(entry), **text_metadata,
        })
    connection.send_result(msg["id"], {"connections": entries})


@websocket_api.websocket_command({
    probatio.Required("type"): "habab_home/providers/key",
    probatio.Required("entry_id"): str,
})
@websocket_api.require_admin
@websocket_api.async_response
async def get_connection_key(hass, connection, msg):
    """Return one saved credential to the authenticated HA administrator."""
    entry = hass.config_entries.async_get_entry(msg["entry_id"])
    if entry is None or entry.domain != DOMAIN:
        connection.send_error(msg["id"], "not_found", "not_found")
        return
    api_key = entry.data.get("api_key")
    if not isinstance(api_key, str) or not api_key:
        connection.send_error(msg["id"], "not_found", "not_found")
        return
    connection.send_result(msg["id"], {"api_key": api_key})


@websocket_api.websocket_command({
    probatio.Required("type"): "habab_home/providers/save",
    probatio.Required("provider"): str,
    probatio.Required("api_key"): str,
    probatio.Required("model"): str,
})
@websocket_api.require_admin
@websocket_api.async_response
async def save_connection(hass, connection, msg):
    lock = hass.data[DOMAIN]["settings_lock"]
    if lock.locked():
        connection.send_error(msg["id"], "busy", "busy")
        return
    api_check_at = None
    api_check_status = "unknown"
    async with lock:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER},
            data={key: msg[key] for key in ("provider", "api_key", "model")},
        )
        if result["type"] == "form":
            code = result.get("errors", {}).get("base", "unknown")
            hass.config_entries.flow.async_abort(result["flow_id"])
            connection.send_error(msg["id"], code, code)
            return
        if result["type"] == "abort" and result.get("reason") != "reconfigured":
            connection.send_error(msg["id"], "setup_failed", "setup_failed")
            return
        try:
            hass.data[DOMAIN]["setup_agent"].select_provider(msg["provider"])
        except AgentError as error:
            connection.send_error(msg["id"], error.code, error.code)
            return
        saved_entry = result.get("result") if result["type"] == "create_entry" else next(
            (item for item in hass.config_entries.async_entries(DOMAIN)
             if item.data.get("provider") == msg["provider"]), None,
        )
        fingerprint = _settings_fingerprint({key: msg[key] for key in ("provider", "model", "api_key")})
        if saved_entry is not None and _settings_fingerprint(saved_entry.data) == fingerprint:
            if _record_api_check(hass, saved_entry, fingerprint, "verified"):
                api_check_at = _api_check_readback(saved_entry)["api_check_at"]
                api_check_status = "verified"
    # Do not send the flow result: it contains a ConfigEntry with the key.
    connection.send_result(msg["id"], {
        "saved": True, "tool_call_verified": True,
        "api_check_status": api_check_status, "api_check_at": api_check_at,
    })


@websocket_api.websocket_command({
    probatio.Required("type"): "habab_home/providers/check",
    probatio.Required("entry_id"): str,
})
@websocket_api.require_admin
@websocket_api.async_response
async def check_connection(hass, connection, msg):
    entry = hass.config_entries.async_get_entry(msg["entry_id"])
    if entry is None or entry.domain != DOMAIN:
        connection.send_error(msg["id"], "not_found", "not_found")
        return
    lock = hass.data[DOMAIN]["settings_lock"]
    if lock.locked():
        connection.send_error(msg["id"], "busy", "busy")
        return
    async with lock:
        fingerprint = _settings_fingerprint(entry.data)
        try:
            await ProviderClient(async_get_clientsession(hass), entry.data).check()
        except ProviderError as err:
            current = hass.config_entries.async_get_entry(msg["entry_id"])
            if current is not None and _settings_fingerprint(current.data) == fingerprint:
                _record_api_check(
                    hass, current, fingerprint,
                    "rejected" if err.code == "invalid_auth" else "unknown",
                    reason=err.code,
                )
            elif current is None or _settings_fingerprint(current.data) != fingerprint:
                connection.send_error(msg["id"], "connection_changed", "connection_changed")
                return
            connection.send_error(msg["id"], err.code, err.code)
            return
        current = hass.config_entries.async_get_entry(msg["entry_id"])
        if current is None or _settings_fingerprint(current.data) != fingerprint:
            connection.send_error(msg["id"], "connection_changed", "connection_changed")
            return
        _record_api_check(hass, current, fingerprint, "verified")
    connection.send_result(msg["id"], {
        "tool_call_verified": True, **_api_check_readback(current),
    })
