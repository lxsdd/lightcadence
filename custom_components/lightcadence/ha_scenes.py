"""Read-only adapter to loaded native HA scene configuration.

No scene activation, YAML/storage reads, or service calls. The platform
adapter is isolated because HA does not promise a public configuration API
for every scene integration. Qualified against the pinned native HA runtime.
"""
from hashlib import sha256
import json

from homeassistant.components.homeassistant.scene import DATA_PLATFORM, HomeAssistantScene
from homeassistant.helpers import entity_registry as er

from .catalog import light_catalog


def _linear(channel):
    v = channel / 255
    return v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4


def inspect_scene(hass, scene_id, enrolled=None):
    registry = er.async_get(hass)
    entry = next((e for e in registry.entities.values() if e.id == scene_id and e.domain == "scene"), None)
    if entry is None or entry.hidden or entry.disabled or entry.platform != "homeassistant":
        raise ValueError("unknown native scene")
    platform = hass.data.get(DATA_PLATFORM)
    scene = platform.entities.get(entry.entity_id) if platform is not None else None
    if not isinstance(scene, HomeAssistantScene) or scene.from_service or scene.unique_id != entry.unique_id:
        raise ValueError("scene definition unavailable")
    definition = scene.scene_config
    lights = {l["entity_id"]: l["id"] for l in light_catalog(hass)}
    problems = []
    targets = []
    for entity_id, state in sorted(definition.states.items()):
        device = lights.get(entity_id)
        attrs = state.attributes
        rgb, brightness = attrs.get("rgb_color"), attrs.get("brightness")
        if device is None:
            reason = "Not a visible registered light"
        elif enrolled is not None and device not in enrolled:
            reason = "Not enrolled in physical engine"
        elif state.state != "on":
            reason = "Power-off or non-on state cannot be represented as a running RGB effect"
        elif attrs.get("effect") not in (None, "off"):
            reason = "Device-specific effect unsupported"
        elif attrs.get("color_mode") not in (None, "rgb"):
            reason = "Non-RGB color mode unsupported"
        elif attrs.get("color_mode") is None and any(k in attrs for k in ("color_temp", "color_temp_kelvin", "hs_color", "xy_color", "rgbw_color", "rgbww_color", "white")):
            reason = "Ambiguous color specification"
        elif (not isinstance(rgb, (list, tuple)) or len(rgb) != 3
                or any(type(v) is not int or not 0 <= v <= 255 for v in rgb)
                or not any(rgb) or type(brightness) is not int or not 1 <= brightness <= 255):
            reason = "Complete nonblack RGB and brightness required"
        else:
            targets.append({"device_id": device, "entity_id": entity_id,
                            "rgb": list(rgb), "brightness": brightness})
            continue
        problems.append({"entity_id": entity_id, "reason": reason})
    if not definition.states:
        problems.append({"entity_id": entry.entity_id, "reason": "Empty scene"})
    if len({t["device_id"] for t in targets}) != len(targets):
        problems.append({"entity_id": entry.entity_id, "reason": "Duplicate device"})
    # Only normalized relevant values; never expose arbitrary HA attributes.
    content = {"scene_id": entry.id, "name": definition.name, "targets": targets, "problems": problems}
    digest = sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {**content, "entity_id": entry.entity_id, "fingerprint": digest, "importable": bool(targets) and not problems}


def scene_catalog(hass):
    rows = []
    for entry in er.async_get(hass).entities.values():
        if entry.domain != "scene" or entry.hidden or entry.disabled:
            continue
        state = hass.states.get(entry.entity_id)
        rows.append({"id": entry.id, "entity_id": entry.entity_id,
                     "name": entry.name or (state.name if state else None) or entry.entity_id,
                     "native": entry.platform == "homeassistant"})
    return sorted(rows, key=lambda row: (row["name"].casefold(), row["id"]))


def import_payload(inspected, revision, instance_id, name):
    if not inspected["importable"]:
        raise ValueError("scene has unsupported targets")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("select import name")
    targets = inspected["targets"]
    layers = [{"effect": {"kind": "static", "period_seconds": 4., "brightness": t["brightness"] / 255,
                          "color": [_linear(c) for c in t["rgb"]], "width": .25},
               "opacity": 1., "blend": "over", "devices": [t["device_id"]]} for t in targets]
    return {"op": "library_import", "instance_id": instance_id, "revision": revision,
            "name": name.strip(), "devices": [t["device_id"] for t in targets],
            "effect": {"kind": "layered", "period_seconds": 4., "brightness": 1., "color": [1., 1., 1.],
                       "width": .25, "layers": layers},
            "ha_source": {"registry_id": inspected["scene_id"], "fingerprint": inspected["fingerprint"]}}
