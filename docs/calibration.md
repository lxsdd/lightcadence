# Calibration assistant (first RGB comparison candidate)

The owner-local project library has additive v1 calibration objects with UUID,
revision, timestamp, name, selector (exact model, firmware, transport, optional
stable registry device ID), RGB8 goal/command anchor pairs, positive normalized
monotone brightness pairs and minimum stable intensity. Device-specific profiles
replace matching model profiles, not multiply them. Existing libraries migrate
by adding an empty collection. Corrections use inverse-distance-weighted
linear RGB deltas (exact at anchors) and piecewise linear brightness mapping.
No photometric measurement or automatic color matching is implied.

The common manager applies one correction after effect composition and live
mixing before native transport projection. No correction of captured originals,
manual native values or comparison targets. Dark targets remain dark. Profile
writes require no ordinary running sessions or pending physical admission;
raw comparison sessions remain adjustable and may save a profile. Existing
noncooperating HA/LAN clients are not locked. Stop/restore, journal, conflict
checks and generation fences are those of the existing physical runtime.

Admin panel: explicitly select two enrolled RGB lamps, apply reference RGB and
brightness, adjust second lamp, Apply comparison targets, visually compare,
record RGB/brightness points, name and save a model or per-device profile.
Stop via existing Running shows; if restore remains pending, inspect before retry.
Comparison changes are raw; target acceptance is not optical feedback. Use
several white, warm and saturated color anchors and several brightness levels.
Profile library must be configured persistent for use across restart.

Initial candidate only supports RGB comparison including RGB-projected warm
white on RGB lamps; native CT mode selection, per-Kelvin curves, physical
enrollment of dual-mode lamps currently requiring RGB state, HA scene target
export, multi-client draft recovery, colorimeter support and actual hardware
acceptance remain open. No household identifiers or values included.

## Read-only native HA target proposals

The physical authenticated command calibration_project uses the current library revision and engine instance fence. It accepts explicit nominal RGB8/brightness targets, applies the same renderer correction and uses the existing native quantization (Yeelight RGB/percent; Tuya HSV/value). The panel shows proposed HA entities JSON from a common goal or from inspected native scene targets. No scene is activated, created or modified. Existing scene targets may already include corrections: never apply a second correction without review. HA integration color-mode conversion and physical fidelity need acceptance; non-RGB/zero targets are rejected. The copied proposal is not a live snapshot of any later scene or profile edits.
