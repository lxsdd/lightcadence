"""Native setup flow for the local engine."""
import probatio as vol

from homeassistant import config_entries
from homeassistant.helpers.selector import SelectSelector, SelectSelectorConfig
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .client import EngineClient, EngineError
from .const import DOMAIN, CONF_PORT, CONF_TOKEN, DEFAULT_PORT, CONF_PHYSICAL, CONF_SCENE_SWITCHES
from .switch import scene_options


class LightCadenceConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry):
        return SceneSwitchOptions()

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


class SceneSwitchOptions(config_entries.OptionsFlowWithReload):
    """Opt-in only. HA config stores references, not scene definitions."""

    async def async_step_init(self, user_input=None):
        entry = self.config_entry
        if not entry.data.get(CONF_PHYSICAL, False):
            return self.async_abort(reason="preview_only")
        client = self.hass.data.get(DOMAIN, {}).get(entry.entry_id)
        try:
            if client is None:
                raise EngineError("cannot_connect")
            state = await client.state()
        except EngineError:
            return self.async_abort(reason="engine_unavailable")
        choices = scene_options(state)
        existing = entry.options.get(CONF_SCENE_SWITCHES, [])
        for scene_id in existing:
            choices.setdefault(scene_id, f"Unavailable scene ({scene_id})")
        errors = {}
        if user_input is not None:
            selected = user_input[CONF_SCENE_SWITCHES]
            if (isinstance(selected, list) and len(set(selected)) == len(selected)
                    and all(s in choices for s in selected)):
                return self.async_create_entry(title="", data={CONF_SCENE_SWITCHES: selected})
            errors["base"] = "invalid_scene_selection"
        options = [{"value": key, "label": label} for key, label in choices.items()]
        return self.async_show_form(step_id="init", errors=errors, data_schema=vol.Schema({
            vol.Required(CONF_SCENE_SWITCHES, default=existing):
                SelectSelector(SelectSelectorConfig(options=options, multiple=True)),
        }))
