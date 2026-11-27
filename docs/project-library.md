# Engine-owned project library v1

The shared preview and physical runtimes expose ordered zones and single-effect
scene presets in the same authenticated state contract. Objects have UUID,
version, revision and timestamp; mutations require current whole-library
revision and engine instance ID. Scene references resolve the current zone.
Editing the library never changes an active session or any HA scene. Loading a
preset populates the existing quick-start controls; the user explicitly starts
the session. Audio source IDs are not stored; select the live source again.

Default library is volatile; the optional compose.library.yaml overlay mounts
an owner-local writable directory separately from the read-only root and
recovery journal. The owner must provision the directory for UID 10001.
Atomic replacement, fsync and exclusive lock protect the local file; no
automatic migration of unknown versions or reset of corrupt storage.
Do not commit the local directory. No real installation authorized.

This is the initial library, not the professional layered scene or timeline
editor. Geometry, custom paths, multi-layer composition and HA scene import
remain subsequent work.
