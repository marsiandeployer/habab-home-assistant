"""Administrator-only connection settings; secrets never appear in results."""

import probatio

from homeassistant.components import websocket_api
from homeassistant.config_entries import SOURCE_USER
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import DOMAIN
from .providers import ProviderClient, ProviderError
from .setup_agent import AgentError


@callback
def async_register_commands(hass):
    websocket_api.async_register_command(hass, list_connections)
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
    entries = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        entities = {
            item.domain: item.entity_id
            for item in er.async_entries_for_config_entry(registry, entry.entry_id)
            if not item.disabled
        }
        entries.append({
            "entry_id": entry.entry_id, "provider": entry.data["provider"],
            "model": entry.data["model"], "state": entry.state.value,
            "entities": entities,
        })
    connection.send_result(msg["id"], {"connections": entries})


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
    # Do not send the flow result: it contains a ConfigEntry with the key.
    connection.send_result(msg["id"], {"saved": True, "tool_call_verified": True})


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
        try:
            await ProviderClient(async_get_clientsession(hass), entry.data).check()
        except ProviderError as err:
            connection.send_error(msg["id"], err.code, err.code)
            return
    connection.send_result(msg["id"], {"tool_call_verified": True})
