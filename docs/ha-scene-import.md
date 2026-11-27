# Read-only native HA scene import

The admin-only native scene browser reads registry scene identities and loaded
HomeAssistantScene definitions (isolated version-sensitive platform adapter,
qualified only on the CI HA version). No activation, file reads or writes,
or current lamp-state inference. Vendor scenes without a native definition,
dynamic scenes, missing/disabled/hidden/unregistered lights, nonlight members,
non-on targets, non-RGB modes and device effects are not silently approximated.
Complete explicit nonblack RGB and brightness required for every member.
Physical mode also requires every member to be enrolled. No partial imports.

Inspect, review, and explicitly import a new independent local zone and layered
static preset in one library CAS transaction. HA source registry ID and
normalized definition fingerprint record original provenance, not a live link.
Recheck definition and current library revision/engine instance before saving.
No HA service call, scene mutation or automatic start; existing LightCadence
presets are never overwritten. Subsequent edits retain provenance but do not
synchronize back. Power-off targets cannot be represented as an RGB frame;
a later explicit power-state model is needed before those scenes can be imported.
