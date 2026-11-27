# Spatial workspace, first vertical delivery

The same engine library contains floorplans (one named floor per object), with
meter dimensions, point lamps, path strips, polygons for rooms/furniture/zones,
wall/opening lines and zone-linked effect paths. No hardcoded home geometry.
Zone shapes do not create walls. Paths are open polylines; strips remain one
physical device (arc-length midpoint), not invented independently controllable
segments. Height/elevation, polygon topology checks and multi-floor show paths
are future work.

The engine projects each placed lamp to the nearest point along the selected
effect path and normalizes its cumulative arc length. Missing placements prevent
selection of that path; no arbitrary ordering fallback. Existing session starts
carry these explicit positions, so subsequent geometry edits cannot change a
running show. Multiple paths per zone are supported. Edits never send lamp
commands. Each floor is a revisioned library object; existing v1 files without
floorplans are migrated in memory and persisted on the next explicit save.
Storage and permissions are unchanged.

The responsive SVG editor is part of the same HA panel. Drafts are local until
explicit save; stale library revisions cannot overwrite another client's edits.
Use pointer drawing/vertex movement or numeric coordinates; keep room polygons
and separate functional zones. Real Companion/HA and device acceptance pending.

## View and geometry editing

The SVG view has local zoom (fit through 32x), an explicit pan mode for pointer/touch and directional keyboard-accessible buttons, and a meter grid whose spacing adapts to scale. The view does not change saved coordinates, revision, undo history or active sessions. Pointer coordinates use the current SVG inverse screen transform; panning uses the transform captured at gesture start. Fit resets only the view. No wheel interception. Selected geometry names and vertices can be edited, including insertion of segment midpoints and removal down to the minimum valid vertex count. Point lamps remain single points. All geometry changes use the existing draft undo/redo and explicit save. This is still 2D, without independent LED segments, topology validation or physical calibration.
