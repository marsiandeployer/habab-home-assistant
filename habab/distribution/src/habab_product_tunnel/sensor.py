"""Report whether the Home Assistant managed reconnecting client is enabled."""

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.const import EntityCategory
from .const import DOMAIN

DESCRIPTION = SensorEntityDescription(
    key="agent",
    name="Tunnel agent",
    icon="mdi:cloud-sync",
    entity_category=EntityCategory.DIAGNOSTIC,
)


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([TunnelAgentSensor(hass.data[DOMAIN][entry.entry_id], entry)])


class TunnelAgentSensor(SensorEntity):
    entity_description = DESCRIPTION
    _attr_has_entity_name = True

    def __init__(self, client, entry):
        self._client = client
        self._attr_unique_id = f"{entry.entry_id}_agent"

    @property
    def native_value(self):
        return self._client.status

    @property
    def extra_state_attributes(self):
        # This reports the reconnect loop, not remote reachability.
        return {"home": self._client.home}
