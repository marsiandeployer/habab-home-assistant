"""One fixed-recipient notify entity per I-Digital configuration entry."""

import logging

from homeassistant.components.notify import NotifyEntity
from homeassistant.exceptions import HomeAssistantError

from .api import IDigitalError
from .const import ATTR_MESSAGE_UUID, DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    """Create the single configured SMS notify entity."""
    client = hass.data[DOMAIN][entry.entry_id]["client"]
    async_add_entities([IDigitalSmsNotifyEntity(client, entry.entry_id)])


class IDigitalSmsNotifyEntity(NotifyEntity):
    """Send to the recipient configured in the integration flow."""

    _attr_has_entity_name = True
    _attr_translation_key = "sms"

    def __init__(self, client: object, entry_id: str) -> None:
        self._client = client
        self._attr_unique_id = f"{entry_id}_notify"
        self._message_uuid: str | None = None

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        """Expose only the last provider UUID, in memory, for explicit status checks."""
        if self._message_uuid is None:
            return None
        return {ATTR_MESSAGE_UUID: self._message_uuid}

    async def async_send_message(self, message: str, title: str | None = None) -> None:
        """Send once; a timeout is ambiguous and is never retried."""
        self._message_uuid = None
        self.async_write_ha_state()
        try:
            self._message_uuid = await self._client.send_message(message)
        except IDigitalError as err:
            _LOGGER.warning("I-Digital SMS send failed: %s", err.code)
            raise HomeAssistantError(err.code) from None
