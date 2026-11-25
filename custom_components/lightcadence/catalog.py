"""Read-only HA registry projection for preview selection.

Do not infer physical identity, LAN credentials, or transport readiness from
an entity registry entry. Multi-channel devices and groups need later mapping.
"""
from homeassistant.helpers import entity_registry as er


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
        })
    return sorted(lights, key=lambda item: (item["name"].casefold(), item["id"]))


def validate_preview_selection(hass, payload):
    """Only new starts require a current catalog selection.

    Existing sessions can always be stopped or detached after a registry change.
    Registry selection is not physical access authorization. Output is preview
    only; no HA service calls, registry mutations, or device network I/O.
    """
    if payload.get("op") != "start":
        return
    devices = payload.get("devices")
    allowed = {light["id"] for light in light_catalog(hass)}
    if (not isinstance(devices, list) or not devices
            or not all(isinstance(device, str) and device in allowed for device in devices)
            or len(devices) != len(set(devices))):
        raise ValueError("invalid preview selection")
