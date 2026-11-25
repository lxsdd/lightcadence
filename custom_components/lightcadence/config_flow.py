"""Native setup flow for the local engine."""
import probatio as vol

from homeassistant import config_entries
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .client import EngineClient, EngineError
from .const import DOMAIN, CONF_PORT, CONF_TOKEN, DEFAULT_PORT, CONF_PHYSICAL


class LightCadenceConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        await self.async_set_unique_id("local_engine")
        self._abort_if_unique_id_configured()
        errors = {}
        if user_input is not None:
            try:
                client = EngineClient(async_get_clientsession(self.hass),
                                      user_input[CONF_PORT], user_input[CONF_TOKEN],
                                      user_input.get(CONF_PHYSICAL, False))
                await client.state()
            except ValueError:
                errors["base"] = "invalid_config"
            except EngineError as exc:
                errors["base"] = "invalid_auth" if exc.code == "unauthorized" else "cannot_connect"
            else:
                return self.async_create_entry(title="LightCadence", data=user_input)
        return self.async_show_form(step_id="user", errors=errors, data_schema=vol.Schema({
            vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.All(int, vol.Range(min=1, max=65535)),
            vol.Required(CONF_TOKEN): str,
            vol.Optional(CONF_PHYSICAL, default=False): bool,
        }))
