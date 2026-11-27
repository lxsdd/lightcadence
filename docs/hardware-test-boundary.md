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
qualification or automatic restart/restore claim.
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


Physical candidate deployment also requires a dedicated owner-local writable
journal directory (LIGHTCADENCE_JOURNAL_PATH in the overlay), owned by the
container UID. The rest of the container stays read-only. Do not delete,
replace or share the journal while a candidate is running. Preserve it across
container restarts, and include it in local backup/rollback handling. The
engine holds a process-exclusive lock and atomically persists complete original
RGB state before acquisition. It removes the record only after release of the
transport or confirmed restore/stop; cooperating manual takeover removes it
before the subsequent manual command. No per-frame disk I/O. A failed
journal write blocks new starts/manual commands, not ordinary HA services.

After an interrupted show, no session, frame, or stop is replayed. The
recorded device is quarantined until the administrator explicitly chooses
keep current state or restore recorded original state in the same responsive
panel. Both require a matching configured binding and a fresh identity-bound
read; restoration requires independent matching readback. Each attempted
recovery rotates the durable record ID before any device write, so an old
request cannot repeat an uncertain write. External LAN clients can change
state; the UI warns that explicit restore may overwrite later changes.
A binding change/corrupt journal needs investigation rather than a blind
reset. Crash/power-loss durability depends on the actual local filesystem.
No automatic restart, physical hardware proof or protection against
external writers is implied.

## Explicit normal Yeelight TCP output

The optional per-device `transport` field is `chroma_v2` (legacy default) or
`yeelight_tcp`. Normal TCP uses the configured `tcp_port`, rejects a
`chroma_port`, and requires an interval of at least one second. This is a
protocol-specific cadence, not a limit on the renderer or other devices.
The public Yeelight WiFi Light Inter-Operation Specification states 60 command
messages/minute per TCP connection, 144/minute across a device, and four
concurrent TCP connections (https://www.yeelight.com/download/Yeelight_Inter-Operation_Spec.pdf).
The output path additionally tracks its own rolling 60-command budget, retaining
it across acquisitions; it cannot measure external clients or other state reads.
No automatic reconnect, quota bypass, Chroma fallback, or replay after a failed
command. Identical quantized targets are coalesced. Original snapshot, ownership,
recovery and independent stop readback stay on the existing shared coordinator.
A TCP connection ACK is not measured light output. Complete RGB or explicit native CT state can be enrolled using verified
capability ranges; music/flow modes remain unsupported. For dual-mode lights,
set both ct_min_kelvin and ct_max_kelvin and omit white_kelvin. Fixed-white
profiles use white_kelvin; explicit Kelvin targets still require the enrolled
native range. Chroma frames remain RGB-only. The catalog labels the selected protocol and cadence. The faster
Chroma UDP route remains unchanged. Loopback tests are not hardware qualification.
