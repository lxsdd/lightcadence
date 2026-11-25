"""Explicit cooperating HA actions; never wrap or intercept unrelated services."""
from uuid import uuid4

import probatio as vol
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .catalog import light_catalog
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


def register_actions(hass):
    # A single explicit entity reference, no group/area expansion or automatic
    # fallback to another light service. Only a configured physical engine can act.
    power_schema = vol.Schema({vol.Required("entity_id"): str})
    color_schema = vol.Schema({vol.Required("entity_id"): str,
                               vol.Required("rgb_color"): _rgb,
                               vol.Required("brightness_pct"): _brightness})

    async def action(call):
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
            payload = {"op": {"set_rgb": "manual_set", "turn_on": "manual_on",
                              "turn_off": "manual_off"}[call.service],
                       "request_id": str(uuid4()), "instance_id": state["instance_id"],
                       "device_id": device}
            if call.service == "set_rgb":
                payload["state"] = {"power": True, "rgb": call.data["rgb_color"],
                                    "brightness_pct": call.data["brightness_pct"]}
            result = await client.physical_command(payload)
        except EngineError as exc:
            raise HomeAssistantError("LightCadence action failed; reconcile before retrying: " + exc.code) from None
        if result.get("manual_result") != "confirmed":
            raise HomeAssistantError("LightCadence outcome unconfirmed; inspect device before another action")

    for service, schema in (("set_rgb", color_schema), ("turn_on", power_schema), ("turn_off", power_schema)):
        hass.services.async_register(DOMAIN, service, action, schema=schema)
