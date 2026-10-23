"""Android's friendly LAN hostname, using Core's shared mDNS sockets."""
import asyncio
from datetime import timedelta
from ipaddress import ip_address
import logging
import os
import json
from pathlib import Path
import time

from zeroconf import AddressResolver
from zeroconf.asyncio import AsyncServiceInfo

from homeassistant.components import zeroconf
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.start import async_at_started

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
HOST = "homeassistant.local."
SERVICE = "_home-assistant._tcp.local."


def lan_addresses(addresses):
    """Publish LAN unicast addresses; never a loopback or cellular public IP."""
    result = []
    for value in addresses:
        address = ip_address(value)
        if (address.is_private and not address.is_loopback and not address.is_link_local
                and not address.is_unspecified and not address.is_multicast):
            result.append(str(address))
    return sorted(set(result))


def android_wifi_addresses():
    """Use the app's ConnectivityManager snapshot, excluding the SIM interface."""
    raw = Path("/run/habab-network.json").read_text()
    if len(raw) > 65536:
        raise ValueError("network_snapshot_too_large")
    data = json.loads(raw)
    if data.get("schema") != 1 or abs(time.time() - data.get("updatedAt", 0)) > 120:
        raise ValueError("network_snapshot_stale")
    return lan_addresses([item["address"] for interface in data["interfaces"]
                          if interface["name"].startswith(("wlan", "swlan", "ap", "eth"))
                          for item in interface["addresses"]])


async def async_setup_alias(hass):
    if os.environ.get("HABAB_RUNTIME_KIND") != "android_proot":
        return
    state = hass.data[DOMAIN]["lan_alias"] = {"hostname": HOST.rstrip("."), "status": "starting"}
    lock = asyncio.Lock()
    registration = None
    published = []
    stopping = False
    aio = await zeroconf.async_get_async_instance(hass)

    async def withdraw():
        nonlocal registration, published
        if registration is not None:
            pending = await aio.async_unregister_service(registration)
            registration = None
            published = []
            await pending

    async def refresh(_now=None):
        nonlocal registration, published
        async with lock:
            if stopping:
                return
            try:
                addresses = await hass.async_add_executor_job(android_wifi_addresses)
                if addresses == published and registration is not None:
                    return
                await withdraw()
                if not addresses:
                    state["status"] = "no_lan_address"
                    return
                resolver = AddressResolver(HOST)
                await resolver.async_request(aio.zeroconf, 1800)
                if set(resolver.parsed_addresses()) - set(addresses):
                    state["status"] = "hostname_conflict"
                    _LOGGER.warning("LAN alias homeassistant.local is already used by another host; not publishing")
                    return
                info = AsyncServiceInfo(
                    SERVICE, "Habab Android LAN." + SERVICE, server=HOST,
                    parsed_addresses=addresses, port=hass.http.server_port,
                    properties={"location_name": "Habab Android", "internal_url": f"http://homeassistant.local:{hass.http.server_port}"},
                )
                pending = await aio.async_register_service(info, allow_name_change=False)
                registration, published = info, addresses
                await pending
                state["status"] = "announced"
            except Exception as error:
                state["status"] = "unavailable"
                _LOGGER.warning("Android LAN alias unavailable (%s)", type(error).__name__)

    cancel = async_track_time_interval(hass, refresh, timedelta(seconds=60))

    async def stop(_event):
        nonlocal stopping
        stopping = True
        cancel()
        async with lock:
            await withdraw()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop)
    async_at_started(hass, refresh)
