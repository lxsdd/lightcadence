"""Native Home Assistant entry and administrator-only preview WebSocket bridge."""
import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .client import EngineClient, EngineError
from .const import DOMAIN, CONF_PORT, CONF_TOKEN


async def async_setup(hass, config):
    websocket_api.async_register_command(hass, ws_state)
    websocket_api.async_register_command(hass, ws_preview)
    return True


async def async_setup_entry(hass, entry):
    try:
        client = EngineClient(async_get_clientsession(hass), entry.data[CONF_PORT], entry.data[CONF_TOKEN])
        await client.state()
    except ValueError:
        raise ConfigEntryAuthFailed("Invalid local engine configuration") from None
    except EngineError as exc:
        if exc.code == "unauthorized":
            raise ConfigEntryAuthFailed("Local engine authentication failed") from None
        raise ConfigEntryNotReady("Local engine unavailable") from None
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = client
    return True


async def async_unload_entry(hass, entry):
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    # Disconnecting HA never stops server-owned sessions.
    return True


async def _bridge(hass, connection, msg, mutate=False):
    client = hass.data.get(DOMAIN, {}).get(msg["entry_id"])
    if client is None:
        connection.send_error(msg["id"], "not_found", "LightCadence entry is not loaded")
        return
    try:
        result = await (client.preview(msg["payload"]) if mutate else client.state())
    except EngineError as exc:
        connection.send_error(msg["id"], exc.code, "LightCadence engine request failed")
    else:
        connection.send_result(msg["id"], result)


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "lightcadence/state", vol.Required("entry_id"): str})
@websocket_api.async_response
async def ws_state(hass, connection, msg):
    await _bridge(hass, connection, msg)


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): "lightcadence/preview", vol.Required("entry_id"): str,
                                  vol.Required("payload"): dict})
@websocket_api.async_response
async def ws_preview(hass, connection, msg):
    await _bridge(hass, connection, msg, mutate=True)
