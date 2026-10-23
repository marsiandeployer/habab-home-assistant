"""Admin-only Telegram configuration and pairing controls."""
import probatio

from homeassistant.components import websocket_api
from homeassistant.core import callback

from .const import DOMAIN


@callback
def async_register_commands(hass):
    websocket_api.async_register_command(hass, status)
    websocket_api.async_register_command(hass, configure)
    websocket_api.async_register_command(hass, pair)
    websocket_api.async_register_command(hass, unlink)


@websocket_api.websocket_command({probatio.Required("type"): "habab_telegram/status"})
@websocket_api.require_admin
@websocket_api.async_response
async def status(hass, connection, msg):
    connection.send_result(msg["id"], await hass.data[DOMAIN].status(connection.user.id))


@websocket_api.websocket_command({
    probatio.Required("type"): "habab_telegram/configure",
    probatio.Required("token"): probatio.All(str, probatio.Length(min=1, max=256)),
    probatio.Optional("proxy_url", default=""): probatio.All(str, probatio.Length(max=2048)),
})
@websocket_api.require_admin
@websocket_api.async_response
async def configure(hass, connection, msg):
    try:
        await hass.data[DOMAIN].configure(connection.user.id, msg["token"], msg.get("proxy_url", ""))
    except ValueError as err:
        connection.send_error(msg["id"], str(err), str(err))
        return
    connection.send_result(msg["id"], {"configured": True})


@websocket_api.websocket_command({probatio.Required("type"): "habab_telegram/pair"})
@websocket_api.require_admin
@websocket_api.async_response
async def pair(hass, connection, msg):
    try:
        result = await hass.data[DOMAIN].pair(connection.user.id)
    except ValueError as err:
        connection.send_error(msg["id"], str(err), str(err))
        return
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command({probatio.Required("type"): "habab_telegram/unlink"})
@websocket_api.require_admin
@websocket_api.async_response
async def unlink(hass, connection, msg):
    await hass.data[DOMAIN].unlink(connection.user.id)
    connection.send_result(msg["id"], {"unlinked": True})
