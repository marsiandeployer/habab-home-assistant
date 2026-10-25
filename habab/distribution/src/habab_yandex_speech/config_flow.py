"""Native Home Assistant setup for a Yandex SpeechKit service account."""

import probatio

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import SpeechKitClient, SpeechKitError, validate_api_key
from .const import DOMAIN


class HababYandexSpeechConfigFlow(ConfigFlow, domain=DOMAIN):
    """Store and check SpeechKit credentials in a protected HA config entry."""

    VERSION = 1

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            await self.async_set_unique_id("yandex-speechkit")
            self._abort_if_unique_id_configured()
            try:
                api_key = validate_api_key(user_input.get("api_key"))
                await SpeechKitClient(
                    self.hass, api_key,
                ).synthesize("Проверка подключения SpeechKit.")
            except SpeechKitError as err:
                errors["base"] = err.code
            else:
                return self.async_create_entry(
                    title="Yandex SpeechKit", data={"api_key": api_key},
                )

        return self.async_show_form(
            step_id="user",
            data_schema=probatio.Schema({
                probatio.Required("api_key"): TextSelector(TextSelectorConfig(
                    type=TextSelectorType.PASSWORD,
                )),
            }),
            errors=errors,
        )
