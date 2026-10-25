"""Single-recipient I-Digital SMS notify integration."""

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import DirectClient, IDigitalError
from .const import DOMAIN, SERVICE_GET_STATUS

PLATFORMS = [Platform.NOTIFY]
_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a client and the notify entity without contacting I-Digital."""
    client = DirectClient(
        async_get_clientsession(hass),
        api_key=entry.data["api_key"],
        sender=entry.data["sender"],
        recipient=entry.data["recipient"],
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {"client": client}

    async def async_get_status(call: ServiceCall) -> dict[str, object]:
        """Query one provider receipt only when an automation explicitly asks."""
        try:
            result = await client.get_status(call.data["message_uuid"])
        except IDigitalError as err:
            _LOGGER.warning("I-Digital SMS status query failed: %s", err.code)
            raise HomeAssistantError(err.code) from None
        return result

    if not hass.services.has_service(DOMAIN, SERVICE_GET_STATUS):
        hass.services.async_register(
            DOMAIN,
            SERVICE_GET_STATUS,
            async_get_status,
            schema=vol.Schema({vol.Required("message_uuid"): str}),
            supports_response=SupportsResponse.ONLY,
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the notify entity and remove its explicit status action."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        if not hass.data.get(DOMAIN):
            hass.data.pop(DOMAIN, None)
            hass.services.async_remove(DOMAIN, SERVICE_GET_STATUS)
    return unloaded
