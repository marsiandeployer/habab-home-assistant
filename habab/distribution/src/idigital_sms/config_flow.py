"""Local-only setup for one I-Digital SMS recipient."""

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from .api import IDigitalError, validate_api_key, validate_recipient, validate_sender
from .const import DOMAIN


class IDigitalSmsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Store a Direct API key and one fixed recipient without sending a test SMS."""

    VERSION = 1

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            try:
                values = {
                    "api_key": validate_api_key(user_input.get("api_key")),
                    "sender": validate_sender(user_input.get("sender")),
                    "recipient": validate_recipient(user_input.get("recipient")),
                }
            except IDigitalError as err:
                errors["base"] = err.code
            else:
                await self.async_set_unique_id("default")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="I-Digital SMS", data=values)

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required("api_key"): TextSelector(TextSelectorConfig(
                    type=TextSelectorType.PASSWORD,
                )),
                vol.Required("sender"): TextSelector(TextSelectorConfig(
                    type=TextSelectorType.TEXT,
                )),
                vol.Required("recipient"): TextSelector(TextSelectorConfig(
                    type=TextSelectorType.TEXT,
                )),
            }),
            errors=errors,
        )
