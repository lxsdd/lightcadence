# Development scope

The original engine domain core provides deterministic rainbow/scanner/pulse
targets, independent zone sessions, one lease per device, request-id
deduplication, revision checks, manual detachment, latest-value mailboxes,
and two-phase stop intents. The server now runs the same core on a monotonic
asyncio render loop, independent of clients, with authenticated loopback HTTP
state and explicit preview commands. Preview uses synthetic targets and HOLD,
never acknowledges physical restores and never sends device commands.
A native HA config flow and admin-only WebSocket bridge are implemented;
real HA runtime verification and the shared Lit panel remain pending.
The local Dockerfile and host-network Compose configuration bind the service
to loopback, with no published port, non-root user, read-only filesystem and
no capabilities. The owner must supply a locally generated token file readable
by UID 10001; never publish it or expose the engine listener.
No installation, release, device transport or hardware qualification is claimed.
The container base is version-tagged; digest locking and full dependency
security/license inventory remain required before a candidate.
Original core and real loopback HTTP regressions run in engine-core; HA native
runtime and frontend/browser gates must be added before block 1 completion.
Next: validate HA config/bridge in real HA test runtime, build the shared Lit
panel on these commands, and qualify the device transport/stop handover path.
