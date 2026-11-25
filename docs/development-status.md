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
The container base is pinned to the digest observed on the standard runner;
full dependency security/license inventory remains required before a candidate.
Original core and real loopback HTTP regressions run in engine-core; HA native
runtime and frontend/browser gates must be added before block 1 completion.
Next: validate HA config/bridge in real HA test runtime, build the shared Lit
panel on these commands, and qualify the device transport/stop handover path.

Native administrator-only HA panel and same-origin bundled Lit UI are now
implemented, including catalog selection, preview start/stop/detach, server
snapshots, conflict feedback, reconnect and responsive layout. Build frontend
with `cd frontend && npm install && npm run build` before copying the integration.
No built bundle is committed. Browser tests use synthetic HA responses; native
HA test separately checks actual panel registration and asset serving. Physical
output remains disabled. Exact dependency lock and full security review pending.

- Coordinated transport cleanup: close Chroma socket/token before HOLD or manual lease release; retain lease and fence frames on cleanup failure; retry cleanup without repeating an already confirmed restore. Loopback/synthetic regression coverage; physical output and real restore remain disabled.

- Integrated physical candidate: the original PhysicalSessions and its output coordinator now run on the common monotonic render loop through an explicitly composed PhysicalRuntime. Its authenticated /v1/physical API admits only enrolled devices, returns render intent separately from submitted/unverified transport status, supports revisioned detach/stop and cooperating manual_set with request-ID replay suppression and independent state-port confirmation. No preview/physical mode switch or dual writer. The normal CLI and HA client remain preview-only; no deployed state authority, hardware activation, external HA/GH action interception, crash journal or hardware qualification. Synthetic state authority and real loopback HTTP/UDP exercise the original paths. Shutdown closes local sockets without speculative restore. A new manual request ID is required after an uncertain outcome; no automatic replay. The state authority must be implemented and validated before an owner-approved hardware test.
