# Explicit Home Assistant saved-scene actions

The existing physical candidate configuration exposes `lightcadence.start_scene`
and `lightcadence.stop_scene`. Use the saved scene UUID shown in the library.
No existing HA scene is modified or imported, and no entity is automatically
exposed to Google Home. Preview engines reject physical actions. Direct user
actions require an active administrator; HA-owned automation context is supported
as with the original cooperating lamp actions.

Example automation action (replace the illustrative UUID with an actual saved
scene ID at installation time):

```yaml
action: lightcadence.start_scene
data:
  scene_id: "00000000-0000-4000-8000-000000000000"
```

For audio effects, supply `audio_source_name` matching exactly one live source.
For a spatial path supply both `floorplan_id` and `path_id`. Otherwise the zone's
stored order is used. The engine validates the whole-library revision, resolves
the saved effect and stop policy, and rejects stale or mismatching selections
before device admission. The original output coordinator owns all writes.
No automatic retry is performed on a failed or uncertain request. Successful
same-instance replay of the original engine request returns the existing session
without reacquiring devices, including after that session stopped.

Stop using `lightcadence.stop_scene` with the same scene UUID. Only a session
explicitly started from that scene can match; no match is a no-op, multiple
matches require individual session control. Stopping still works after a scene
was edited/deleted; provenance belongs to the launched session. Manual detach,
recovery and stop policy are unchanged. Panel quick starts are independent
editor launches, not saved-scene actions. No actual hardware validation yet.
