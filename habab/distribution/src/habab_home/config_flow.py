"""Native Home Assistant configuration entries own provider credentials."""

import probatio

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    SelectSelector, SelectSelectorConfig, TextSelector, TextSelectorConfig,
    TextSelectorType,
)

from .beta import BetaError, _installation_id, _enroll
from .const import DOMAIN
from .effective_provider import effective_provider_entry
from .providers import PROVIDERS, ProviderClient, ProviderError, validate_settings


class HababHomeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Connect and verify an AI provider; one entry per provider."""

    VERSION = 1

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            try:
                user_input = dict(user_input)
                if user_input.get("provider") == "habab" and not self.context.get("habab_beta_enrollment"):
                    # Habab owns activation; arbitrary user text is never a provider secret.
                    installation_id = await _installation_id(self.hass)
                    user_input["api_key"] = await _enroll(async_get_clientsession(self.hass), installation_id)
                settings = validate_settings(user_input)
                await ProviderClient(async_get_clientsession(self.hass), settings).check()
            except BetaError:
                errors["base"] = "cannot_connect"
            except ProviderError as err:
                errors["base"] = err.code
            else:
                await self.async_set_unique_id(settings["provider"])
                if (self.context.get("habab_beta_enrollment")
                        and effective_provider_entry(self._async_current_entries()) is not None):
                    return self.async_abort(reason="already_configured")
                for entry in self._async_current_entries():
                    if entry.unique_id == settings["provider"]:
                        self.hass.config_entries.async_update_entry(entry, data=settings)
                        if not await self.hass.config_entries.async_reload(entry.entry_id):
                            return self.async_abort(reason="setup_failed")
                        return self.async_abort(reason="reconfigured")
                return self.async_create_entry(
                    title=PROVIDERS[settings["provider"]]["name"], data=settings,
                )

        # Never put an API key into suggested/default values returned to clients.
        return self.async_show_form(
            step_id="user",
            data_schema=probatio.Schema({
                probatio.Required("provider"): SelectSelector(SelectSelectorConfig(
                    options=[{"value": k, "label": v["name"]} for k, v in PROVIDERS.items()],
                )),
                probatio.Optional("api_key"): TextSelector(TextSelectorConfig(
                    type=TextSelectorType.PASSWORD,
                )),
                probatio.Required("model"): str,
            }),
            errors=errors,
        )
