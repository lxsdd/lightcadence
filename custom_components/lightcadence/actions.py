"""Explicit cooperating HA actions; never wrap or intercept unrelated services."""
from uuid import uuid4

import probatio as vol
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .catalog import light_catalog, validate_preview_selection
from .client import EngineError
from .const import DOMAIN


def _rgb(value):
    if not isinstance(value, (list, tuple)) or len(value) != 3 or any(type(c) is not int or not 0 <= c <= 255 for c in value):
        raise vol.Invalid("exactly three integer RGB channels required")
    return list(value)


def _brightness(value):
    if type(value) is not int or not 1 <= value <= 100:
        raise vol.Invalid("integer brightness percent 1–100 required")
    return value


def _kelvin(value):
    if type(value) is not int or not 1700 <= value <= 6500:
        raise vol.Invalid("integer color temperature 1700–6500 K required")
    return value


def register_actions(hass):
    # A single explicit entity reference, no group/area expansion or automatic
    # fallback to another light service. Only a configured physical engine can act.
    power_schema = vol.Schema({vol.Required("entity_id"): str})
    color_schema = vol.Schema({vol.Required("entity_id"): str,
                               vol.Required("rgb_color"): _rgb,
                               vol.Required("brightness_pct"): _brightness})

    ct_schema = vol.Schema({vol.Required("entity_id"): str,
                            vol.Required("kelvin"): _kelvin,
                            vol.Required("brightness_pct"): _brightness})

    async def authorized_client(call):
        # Direct user actions require admin. HA-owned automations run with a
        # system context (no user_id); their configuration is an owner action.
        if call.context.user_id is not None:
            user = await hass.auth.async_get_user(call.context.user_id)
            if user is None or not user.is_admin or not user.is_active:
                raise HomeAssistantError("LightCadence action requires an administrator")
        clients = hass.data.get(DOMAIN, {})
        if len(clients) != 1:
            raise ServiceValidationError("Exactly one loaded LightCadence entry required")
        client = next(iter(clients.values()))
        if not client.physical:
            raise ServiceValidationError("Physical LightCadence candidate is not enabled")
        return client

    async def action(call):
        client = await authorized_client(call)
        entity_id = call.data["entity_id"]
        matches = [l for l in light_catalog(hass) if l["entity_id"] == entity_id]
        if len(matches) != 1:
            raise ServiceValidationError("Select one visible, enabled registered light")
        device = matches[0]["id"]
        try:
            state = await client.state()
            if device not in state["enrolled_devices"]:
                raise ServiceValidationError("Light is not enrolled in the physical engine")
            # Do not auto-retry across HTTP failure or restart. The engine instance
            # fences this command, and its original manual reservation owns the I/O.
            payload = {"op": {"set_rgb": "manual_set", "set_ct": "manual_set", "turn_on": "manual_on",
                              "turn_off": "manual_off"}[call.service],
                       "request_id": str(uuid4()), "instance_id": state["instance_id"],
                       "device_id": device}
            if call.service == "set_rgb":
                payload["state"] = {"power": True, "rgb": call.data["rgb_color"],
                                    "brightness_pct": call.data["brightness_pct"]}
            if call.service == "set_ct":
                payload["state"] = {"power": True, "kelvin": call.data["kelvin"],
                                    "brightness_pct": call.data["brightness_pct"]}
            result = await client.physical_command(payload)
        except EngineError as exc:
            raise HomeAssistantError("LightCadence action failed; reconcile before retrying: " + exc.code) from None
        if result.get("manual_result") != "confirmed":
            raise HomeAssistantError("LightCadence outcome unconfirmed; inspect device before another action")

    async def scene_action(call):
        client = await authorized_client(call)
        scene_id = call.data["scene_id"]
        try:
            state = await client.state()
            if call.service == "start_scene":
                lib = state.get("library")
                if not isinstance(lib, dict) or lib.get("fault"):
                    raise ServiceValidationError("Scene library unavailable")
                scene = next((s for s in lib["scenes"] if s["id"] == scene_id), None)
                if scene is None:
                    raise ServiceValidationError("Unknown LightCadence scene ID")
                zone = next(z for z in lib["zones"] if z["id"] == scene["zone_id"])
                payload = {"op": "scene_start", "instance_id": state["instance_id"],
                           "request_id": str(uuid4()), "scene_id": scene_id,
                           "library_revision": lib["revision"], "devices": zone["devices"]}
                try:
                    validate_preview_selection(hass, payload)
                except ValueError:
                    raise ServiceValidationError("Scene contains missing or disabled lights") from None
                if not set(zone["devices"]) <= set(state["enrolled_devices"]):
                    raise ServiceValidationError("Scene contains unenrolled lights")
                if "audio_source_name" in call.data:
                    sources = [s for s in state.get("audio_sources", [])
                               if s["name"] == call.data["audio_source_name"] and s["status"] == "live"]
                    if len(sources) != 1:
                        raise ServiceValidationError("Select one uniquely named live audio source")
                    payload["audio_source"] = sources[0]["id"]
                for key in ("floorplan_id", "path_id"):
                    if key in call.data:
                        payload[key] = call.data[key]
                await client.physical_command(payload)
            else:
                # Only sessions explicitly launched from this saved scene are
                # candidates. Never infer ownership from a matching zone/name.
                matches = [s for s in state["sessions"] if s["phase"] != "stopped"
                           and s.get("scene_origin", {}).get("scene_id") == scene_id]
                if not matches:
                    return
                if len(matches) != 1:
                    raise ServiceValidationError("Multiple matching sessions; use individual panel stop")
                session = matches[0]
                result = await client.physical_command({"op": "stop", "instance_id": state["instance_id"],
                    "session_id": session["id"], "revision": session["revision"]})
                current = next(s for s in result["sessions"] if s["id"] == session["id"])
                if current["phase"] != "stopped":
                    raise HomeAssistantError("Stop outcome unconfirmed; reconcile before another action")
        except EngineError as exc:
            raise HomeAssistantError("LightCadence scene action failed; reconcile before retrying: " + exc.code) from None

    scene_schema = vol.Schema({vol.Required("scene_id"): str, vol.Optional("audio_source_name"): str,
                               vol.Optional("floorplan_id"): str, vol.Optional("path_id"): str})
    hass.services.async_register(DOMAIN, "start_scene", scene_action, schema=scene_schema)
    hass.services.async_register(DOMAIN, "stop_scene", scene_action, schema=vol.Schema({vol.Required("scene_id"): str}))

    for service, schema in (("set_rgb", color_schema), ("set_ct", ct_schema), ("turn_on", power_schema), ("turn_off", power_schema)):
        hass.services.async_register(DOMAIN, service, action, schema=schema)
