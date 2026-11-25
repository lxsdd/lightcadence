# Local hardware candidate boundary

Default Compose and existing HA entries remain preview-only. A separate local
Compose overlay and owner-local JSON profile file are required for physical
candidate mode, plus matching explicit HA config-flow consent. Do not use the
overlay until separately authorized by the owner. No profile data, token or
household identifiers belong in source control or CI. Keep the profile under
local/ or outside the checkout and mount read-only; do not expose the loopback
engine port. The file must have schema_version 1, owner_hardware_test true and
a devices array. Each device requires ha_registry_id (stable HA registry ID,
also used as engine device ID), model, firmware, host (local IPv4), interval
(seconds), lan_id (protocol identity); optional chroma_port, tcp_port and
discovery_port. No automatic discovery or fallback to preview on malformed
physical config. Starting the service reads configuration but opens no device
sockets until an explicit command. HA setup validates the exact mode.

Physical commands require the current engine instance ID as well as existing
request IDs/revisions; stale requests from before a restart fail closed.
The panel exposes the same session engine for preview and the explicitly
configured candidate. Only enrolled registry lights are offered for physical
starts; stop/detach remain available for existing sessions if a registry
entry changes. Admin-only physical WebSocket supports cooperative manual_set
with exact linear RGB state, not interception of ordinary HA or Google Home
actions. Device identity is protocol-reported, not cryptographic. No hardware
qualification, durable recovery journal or automatic restart/restore claim.
Actual installation and hardware commands require separate owner permission.


The shared panel now exposes explicit stop behaviour and a cooperative manual
RGB/on or off action for an enrolled light. RGB8 and integer brightness percent
are converted by the original engine's canonical linear-light conversion.
Off reads current complete state after acquiring the manual reservation,
preserving hidden attributes. Both paths release the lamp from its running
show before writing; a matching independent readback is distinguished from
an uncertain result, with no automatic retry. Explicit retry of the same
request ID cannot replay a recorded uncertain command. Ordinary HA light
service calls, vendor apps and Google Home are still outside this route.
Native HA and browser tests use synthetic peers/state only.
