"""Read-only HA registry projection for preview selection.

Do not infer physical identity, LAN credentials, or transport readiness from
an entity registry entry. Multi-channel devices and groups need later mapping.
"""
from math import isfinite

from homeassistant.helpers import entity_registry as er


def cached_light_state(state):
    """Describe HA's cached state, never infer fresh device feedback.

    Off lights frequently omit their previous colour/brightness. No fallback
    values are invented. The projection is diagnostic only, not a StatePort.
    """
    if state is None or state.state not in ("on", "off"):
        return {"source": "ha_cache", "status": "unavailable", "power": None,
                "color_mode": None, "rgb": None, "brightness": None}
    attrs = state.attributes
    mode = attrs.get("color_mode")
    mode = mode if isinstance(mode, str) else None
    rgb = attrs.get("rgb_color")
    brightness = attrs.get("brightness")
    rgb_valid = (isinstance(rgb, (list, tuple)) and len(rgb) == 3
                 and all(type(x) is int and 0 <= x <= 255 for x in rgb))
    brightness_valid = type(brightness) is int and 1 <= brightness <= 255
    kelvin = attrs.get("color_temp_kelvin")
    kelvin_valid = type(kelvin) is int and kelvin > 0
    hs = attrs.get("hs_color")
    hs_valid = (isinstance(hs, (list, tuple)) and len(hs) == 2
                and all(type(v) in (int, float) and isfinite(v) for v in hs)
                and 0 <= hs[0] <= 360 and 0 <= hs[1] <= 100)
    effect = attrs.get("effect")
    if effect not in (None, "off"):
        status = "effect_active"
    elif mode == "color_temp":
        status = "cached_kelvin" if kelvin_valid and brightness_valid else "incomplete_kelvin"
    elif mode == "hs":
        status = "cached_hs" if hs_valid and brightness_valid else "incomplete_hs"
    elif mode != "rgb":
        status = "unsupported_or_unknown_mode"
    elif not rgb_valid or not brightness_valid:
        status = "incomplete_rgb"
    else:
        status = "cached_rgb"
    return {"source": "ha_cache", "status": status, "power": state.state == "on",
            "color_mode": mode,
            "rgb": list(rgb) if mode == "rgb" and rgb_valid else None,
            "brightness": brightness if brightness_valid else None,
            **({"kelvin": kelvin if kelvin_valid else None} if mode == "color_temp" else {}),
            **({"hs": list(hs) if hs_valid else None} if mode == "hs" else {})}


def light_catalog(hass):
    lights = []
    for entry in er.async_get(hass).entities.values():
        if entry.domain != "light" or entry.disabled or entry.hidden:
            continue
        state = hass.states.get(entry.entity_id)
        name = entry.name or (state.name if state is not None else None)
        name = name or entry.original_name or entry.entity_id
        lights.append({
            "id": entry.id,
            "entity_id": entry.entity_id,
            "name": name,
            "area_id": entry.area_id,
            "available": state is not None and state.state not in ("unavailable", "unknown"),
            "physical_output_enabled": False,
            "observation": cached_light_state(state),
        })
    return sorted(lights, key=lambda item: (item["name"].casefold(), item["id"]))


def validate_preview_selection(hass, payload):
    """Only new starts require a current catalog selection.

    Existing sessions can always be stopped or detached after a registry change.
    Registry selection is not physical access authorization. Output is preview
    only; no HA service calls, registry mutations, or device network I/O.
    """
    if payload.get("op") == "library_save" and payload.get("kind") == "zones":
        value = payload.get("value")
        devices = value.get("devices") if isinstance(value, dict) else None
    elif payload.get("op") == "library_save" and payload.get("kind") == "floorplans":
        value = payload.get("value")
        features = value.get("features") if isinstance(value, dict) else None
        if not isinstance(features, list) or not all(isinstance(f, dict) for f in features):
            raise ValueError("invalid geometry")
        devices = [f.get("device_id") for f in features if f.get("kind") in ("lamp", "strip")]
        if not devices:
            return
    elif payload.get("op") == "library_save" and payload.get("kind") == "calibrations":
        value = payload.get("value")
        sel = value.get("selector") if isinstance(value, dict) else None
        if not isinstance(sel, dict):
            raise ValueError("invalid calibration selector")
        if "device_id" not in sel:
            return
        devices = [sel["device_id"]]
    elif payload.get("op") in ("start", "scene_start", "calibration_start", "calibration_project"):
        devices = payload.get("devices")
    else:
        return
    allowed = {light["id"] for light in light_catalog(hass)}
    if (not isinstance(devices, list) or not devices
            or not all(isinstance(device, str) and device in allowed for device in devices)
            or len(devices) != len(set(devices))):
        raise ValueError("invalid preview selection")


def enrollment_report(hass, enrolled):
    """Account for all enrolled IDs, even those omitted from light selection.

    Only the HA registry and state cache are read. A shared HA device is an
    advisory identity ambiguity (e.g. independent segments), not proof of a
    duplicate lamp. No live network probe or automatic registry modification.
    """
    entries = {entry.id: entry for entry in er.async_get(hass).entities.values()}
    groups = {}
    for identity in enrolled:
        entry = entries.get(identity)
        if entry is not None and entry.device_id is not None:
            groups.setdefault(entry.device_id, []).append(identity)
    shared = {identity for group in groups.values() if len(group) > 1
              for identity in group}
    rows = []
    for identity in sorted(enrolled):
        entry = entries.get(identity)
        state = hass.states.get(entry.entity_id) if entry is not None else None
        if entry is None:
            status = "missing_registry"
        elif entry.domain != "light":
            status = "not_light"
        elif entry.disabled:
            status = "disabled"
        elif entry.hidden:
            status = "hidden"
        elif state is None or state.state not in ("on", "off"):
            status = "unavailable"
        else:
            status = "registered"
        rows.append({"id": identity, "entity_id": entry.entity_id if entry else None,
                     "name": (entry.name or (state.name if state is not None else None)
                              or entry.original_name or entry.entity_id) if entry else None,
                     "status": status, "shared_ha_device": identity in shared,
                     "observation": cached_light_state(state) if entry and entry.domain == "light" else None})
    return rows
