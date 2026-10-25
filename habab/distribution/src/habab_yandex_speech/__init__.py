"""Yandex SpeechKit speech entities for Home Assistant Assist."""

from homeassistant.const import Platform

from .api import SpeechKitClient


PLATFORMS = [Platform.STT, Platform.TTS]


async def async_setup_entry(hass, entry):
    """Set up STT and TTS entities for a validated SpeechKit credential."""
    hass.data.setdefault(entry.domain, {})[entry.entry_id] = SpeechKitClient(
        hass, entry.data["api_key"],
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass, entry):
    """Unload speech entities and discard the client."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data.get(entry.domain, {}).pop(entry.entry_id, None)
    return unloaded
