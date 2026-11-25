# Explicit device enrollment (transport candidate)

The engine's existing SessionManager and OutputCoordinator are used by PhysicalSessions.
Explicit Chroma profiles bind a stable device ID, HA registry identity, model and firmware
to a local endpoint and device-specific cadence. Duplicate IDs, HA references and
endpoints are rejected. No adapter is opened until explicit admission. Admission
reserves all selected identities, captures fresh complete RGB snapshots from an injected
state authority, then acquires the Chroma sessions. Failure or cancellation before
publication closes attempted sockets and leaves no active session. Repeated completed
request IDs return the existing session; in-flight duplicates are rejected.

RESTORE/OFF uses the original stop intent. The UDP socket is closed before cooperating
state authority writes; identity is checked before the write, and acknowledgment plus
an independent matching readback are required. Unconfirmed results keep the lease.
Manual detachment never restores. Unsupported CT/effect/unknown states are rejected
rather than approximated. Strict readback equality is conservative; a future real
state authority must define accurate conversion/calibration and supported modes.

The state authority is currently an injected protocol, not a real HA implementation.
There is no physical activation path in the service or panel. Synthetic authority
tests exercise the actual enrollment/restore code and actual loopback UDP sockets;
they do not establish firmware compatibility, physical identity, state fidelity,
optical performance, or coordination with external writers. No automatic restoration
after a crash. Real state bridge and explicit HA action handover remain required.
