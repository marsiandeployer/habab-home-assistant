"""Home Assistant lifecycle for the outbound-only Habab tunnel agent."""

from .client import TunnelClient

DOMAIN = "habab_product_tunnel"


async def async_setup_entry(hass, entry) -> bool:
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform

    # `web_tunnel.py` is staged from habab/product-tunnel at package/build time.
    from .web_tunnel import run_agent

    values = {**entry.data, **entry.options}
    client = TunnelClient(values["relay"], values["home"], values["token"], run_agent)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = client
    await client.async_start()
    async def stop_agent(_event):
        await client.async_stop()

    remove_stop_listener = hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop_agent)
    entry.async_on_unload(remove_stop_listener)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    await hass.config_entries.async_forward_entry_setups(entry, [Platform.SENSOR])
    return True


async def async_reload_entry(hass, entry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass, entry) -> bool:
    from homeassistant.const import Platform

    unloaded = await hass.config_entries.async_unload_platforms(entry, [Platform.SENSOR])
    if not unloaded:
        return False
    client = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if client:
        await client.async_stop()
    return True
