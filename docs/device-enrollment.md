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

## Parallel admission and cooperative manual operation

Snapshots of distinct enrolled devices are read concurrently; acquisition starts only
once every snapshot is valid. All independent UDP handshakes then run concurrently.
The batch settles every operation before publishing or cleaning up; failure or
cancellation closes all attempted transports. The ordering of selected devices is
preserved. No arbitrary lamp limit or claimed physical startup speed.

A cooperating manual writer can use `async with physical.manual_handover(device_id)`.
Admission and other manual operations on that device are excluded until the context
exits. The context waits for output cleanup and detaches an owned device before
granting access; it does not restore it afterwards. The caller must finish all I/O
inside the context. Failed/stale handover never grants access. This is an in-process
contract, not interception of normal HA/Google Home or third-party LAN actions.

The existing native HA catalog and responsive panel expose read-only, explicitly
labelled HA cached-state diagnostics. Missing RGB/brightness, effect modes, off
states and unsupported/unknown colour modes are distinguished without invented
values. HA cached state is not an independent physical readback and must not be
wired as a confirmed StatePort. Device identity, verified restore, activation
and cooperating cross-process HA action routing still require qualification.
