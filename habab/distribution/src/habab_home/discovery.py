"""Bounded observation of Home Assistant's native discovery, not host Wi-Fi control."""
import asyncio

import probatio
from homeassistant.components import websocket_api
from homeassistant.core import callback
from homeassistant.setup import async_setup_component

from .const import DOMAIN


async def observe(hass, manager, duration=8):
    from .setup_actions import discovered_flows

    components = hass.config.components
    methods = {name: name in components for name in ("zeroconf", "ssdp", "dhcp")}
    bluetooth = {"status": "unavailable", "scanner_count": 0, "devices": []}
    try:
        if "bluetooth" not in components:
            async with asyncio.timeout(20):
                await async_setup_component(hass, "bluetooth", {})
        from homeassistant.components import bluetooth as api

        count = api.async_scanner_count(hass, connectable=False)
        bluetooth["scanner_count"] = count
        bluetooth["status"] = "observing" if count else "no_adapter"
    except (Exception, TimeoutError):
        bluetooth["status"] = "unavailable"
    await asyncio.sleep(duration)
    if bluetooth["scanner_count"]:
        try:
            infos = api.async_discovered_service_info(hass, connectable=False)
            bluetooth["devices"] = [
                {"name": str(info.name)[:128], "address": str(info.address)[:64],
                 "rssi": info.rssi, "connectable": info.connectable}
                for info in list(infos)[:100]
            ]
            bluetooth["status"] = "observed" if infos else "no_advertisements"
        except Exception:
            bluetooth["status"] = "unavailable"
    flows = discovered_flows(hass, manager)
    return {"mode": "native_discovery_observation", "duration_seconds": duration,
            "network": {"methods_loaded": methods, "flows": flows,
                        "status": "results" if flows else "no_discovery_results" if any(methods.values()) else "unavailable",
                        "multicast_reachability_verified": False},
            "bluetooth": bluetooth,
            "instruction": "These are native HA discovery results, not a list of every device or Wi-Fi network. No results do not prove the LAN is empty. A missing Bluetooth adapter requires an adapter/proxy accessible to HA; do not claim Windows Bluetooth is forwarded."}


@callback
def async_register_commands(hass):
    websocket_api.async_register_command(hass, scan)


@websocket_api.websocket_command({
    probatio.Required("type"): "habab_home/discovery/scan",
    probatio.Optional("duration", default=8): probatio.All(int, probatio.Range(min=1, max=15)),
})
@websocket_api.require_admin
@websocket_api.async_response
async def scan(hass, connection, msg):
    result = await observe(hass, hass.data[DOMAIN]["setup_agent"], msg["duration"])
    connection.send_result(msg["id"], result)
