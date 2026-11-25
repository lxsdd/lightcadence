# Explicit cooperating Home Assistant actions

The same physical engine reservation used by the panel is available through
three native HA actions: lightcadence.set_rgb, lightcadence.turn_on and
lightcadence.turn_off. All require the explicitly configured physical candidate.
They accept one current light entity_id and resolve its stable registry identity
against the engine enrollment. RGB and integer percent brightness are required
for set_rgb; power-only actions preserve a fresh complete device RGB target.
Direct user calls require an active administrator; HA-owned system automations
may invoke them. No group/area fan-out or automatic retry/fallback.

Example configuration (illustrative, not installed or executed):

```yaml
action: lightcadence.set_rgb
data:
  entity_id: light.example
  rgb_color: [64, 128, 255]
  brightness_pct: 71
```

The engine fences the request by instance ID, releases the affected lamp from
its show, and confirms a separate state readback. A transport error or
unconfirmed outcome fails the HA action rather than claiming success. Other
lamps continue; ordinary light.turn_on/off calls and existing Google Home
routes are not intercepted or modified. An owner-approved script or other
explicit integration can call these actions; transparent Google Home exposure
and real hardware acceptance still need work. No changes to HA automations,
scenes or devices are made by registering the actions. Tests use original
native HA services with a synthetic state authority and loopback peers.
