"""Native Home Assistant entry and administrator-only preview WebSocket bridge."""
import probatio as vol

from homeassistant.components import websocket_api
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .catalog import light_catalog, validate_preview_selection
from .client import EngineClient, EngineError
from .const import DOMAIN, CONF_PORT, CONF_TOKEN, CONF_PHYSICAL
from .panel import register_panel
from .actions import register_actions
from .ha_scenes import scene_catalog, inspect_scene, import_payload


async def async_setup(hass, config):
    if not await register_panel(hass):
        return False
    websocket_api.async_register_command(hass, ws_state)
    websocket_api.async_register_command(hass, ws_preview)
    websocket_api.async_register_command(hass, ws_catalog)
    websocket_api.async_register_command(hass, ws_physical)
    websocket_api.async_register_command(hass, ws_ha_scenes)
    websocket_api.async_register_command(hass, ws_ha_scene_inspect)
    websocket_api.async_register_command(hass, ws_ha_scene_import)
    register_actions(hass)
    return True


async def async_setup_entry(hass, entry):
    try:
        client = EngineClient(async_get_clientsession(hass), entry.data[CONF_PORT], entry.data[CONF_TOKEN], entry.data.get(CONF_PHYSICAL, False))
        await client.state()
    except ValueError:
        raise ConfigEntryAuthFailed("Invalid local engine configuration") from None
    except EngineError as exc:
        if exc.code == "unauthorized":
            raise ConfigEntryAuthFailed("Local engine authentication failed") from None
        raise ConfigEntryNotReady("Local engine unavailable") from None
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = client
    if client.physical:
        await hass.config_entries.async_forward_entry_setups(entry, ["switch"])
    return True


async def async_unload_entry(hass, entry):
    if entry.data.get(CONF_PHYSICAL, False):
        if not await hass.config_entries.async_unload_platforms(entry, ["switch"]):
            return False
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    # Disconnecting HA never stops server-owned sessions.
    return True


async def _bridge(hass, connection, msg, mutate=False, physical=False):
    client = hass.data.get(DOMAIN, {}).get(msg["entry_id"])
    if client is None:
        connection.send_error(msg["id"], "not_found", "LightCadence entry is not loaded")
        return
    try:
        if mutate:
            payload = msg["payload"]
            # Only the dedicated native-scene endpoint may derive this operation.
            if payload.get("op") == "library_import":
                raise ValueError("use native scene import")
            if client.physical is not physical:
                raise EngineError("incompatible_engine")
            validate_preview_selection(hass, payload)
            if physical:
                if payload.get("op") in ("manual_set", "manual_off", "manual_on", "recover") and payload.get("device_id") not in {l["id"] for l in light_catalog(hass)}:
                    raise ValueError("invalid manual selection")
                result = await client.physical_command(payload)
            else:
                result = await client.preview(payload)
        else:
            result = await client.state()
    except ValueError:
        connection.send_error(msg["id"], "invalid_selection", "Select registered, enabled lights")
        return
    except EngineError as exc:
        connection.send_error(msg["id"], exc.code, "LightCadence engine request failed")
    else:
        connection.send_result(msg["id"], result)


@websocket_api.require_admin
@websocket_api.websocket_command({"type": "lightcadence/state", vol.Required("entry_id"): str})
@websocket_api.async_response
async def ws_state(hass, connection, msg):
    await _bridge(hass, connection, msg)


@websocket_api.require_admin
@websocket_api.websocket_command({"type": "lightcadence/preview", vol.Required("entry_id"): str,
                                  vol.Required("payload"): dict})
@websocket_api.async_response
async def ws_preview(hass, connection, msg):
    await _bridge(hass, connection, msg, mutate=True)


@websocket_api.require_admin
@websocket_api.websocket_command({"type": "lightcadence/physical", vol.Required("entry_id"): str,
                                  vol.Required("payload"): dict})
@websocket_api.async_response
async def ws_physical(hass, connection, msg):
    await _bridge(hass, connection, msg, mutate=True, physical=True)


@websocket_api.require_admin
@websocket_api.websocket_command({"type": "lightcadence/ha_scenes"})
@websocket_api.async_response
async def ws_ha_scenes(hass, connection, msg):
    connection.send_result(msg["id"], scene_catalog(hass))


async def _ha_scene(hass, connection, msg, save=False):
    client = hass.data.get(DOMAIN, {}).get(msg["entry_id"])
    if client is None:
        connection.send_error(msg["id"], "not_found", "LightCadence entry is not loaded")
        return
    try:
        state = await client.state()
        enrolled = set(state["enrolled_devices"]) if client.physical else None
        inspected = inspect_scene(hass, msg["scene_id"], enrolled)
        if save:
            if inspected["fingerprint"] != msg["fingerprint"]:
                raise EngineError("stale_revision")
            if state["instance_id"] != msg["instance_id"]:
                raise EngineError("stale_instance")
            payload = import_payload(inspected, msg["revision"], msg["instance_id"], msg["name"])
            result = await (client.physical_command(payload) if client.physical else client.preview(payload))
        else:
            result = inspected
    except ValueError:
        connection.send_error(msg["id"], "invalid_selection", "Native scene unavailable or unsupported")
    except EngineError as exc:
        connection.send_error(msg["id"], exc.code, "LightCadence scene import failed")
    else:
        connection.send_result(msg["id"], result)


@websocket_api.require_admin
@websocket_api.websocket_command({"type": "lightcadence/ha_scene_inspect", vol.Required("entry_id"): str,
                                  vol.Required("scene_id"): str})
@websocket_api.async_response
async def ws_ha_scene_inspect(hass, connection, msg):
    await _ha_scene(hass, connection, msg)


@websocket_api.require_admin
@websocket_api.websocket_command({"type": "lightcadence/ha_scene_import", vol.Required("entry_id"): str,
                                  vol.Required("scene_id"): str, vol.Required("fingerprint"): str,
                                  vol.Required("instance_id"): str, vol.Required("revision"): int,
                                  vol.Required("name"): str})
@websocket_api.async_response
async def ws_ha_scene_import(hass, connection, msg):
    await _ha_scene(hass, connection, msg, save=True)


@websocket_api.require_admin
@websocket_api.websocket_command({"type": "lightcadence/catalog"})
@websocket_api.async_response
async def ws_catalog(hass, connection, msg):
    # Registry identities survive HA entity_id renames. They are preview
    # targets, not proof of a distinct physical lamp or transport capability.
    clients = hass.data.get(DOMAIN, {})
    lights = light_catalog(hass)
    physical = len(clients) == 1 and next(iter(clients.values())).physical
    if physical:
        try:
            state = await next(iter(clients.values())).state()
        except EngineError as exc:
            connection.send_error(msg["id"], exc.code, "LightCadence engine request failed")
            return
        enrolled = set(state["enrolled_devices"])
        transports = state.get("device_transports", {})
        lights = [dict(light, physical_output_enabled=True, transport=transports.get(light["id"]))
                  for light in lights if light["id"] in enrolled]
    connection.send_result(msg["id"], {
        "schema_version": 1,
        "entries": sorted(clients),
        "lights": lights,
        "physical_output_enabled": physical,
    })
