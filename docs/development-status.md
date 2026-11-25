# Development scope

The initial engine domain core provides deterministic rainbow/scanner/pulse targets,
independent zone sessions, one lease per device, request-id deduplication, revision
checks, manual detachment, latest-value mailboxes, and two-phase stop intents.
It has no hardware I/O, no runtime transport scheduler or persistent journal yet,
and no native Home Assistant integration, engine API or frontend. Tests exercise
the original engine classes and synthetic device identities only. No socket send
or simulated target is evidence of actual light output. Physical stop and restore
require a qualified serialized transport and real HA coordination.

The next vertical increment wires these same classes into the authenticated local
engine service and HA integration, then the shared panel and real transport.
No license, binary candidate or release is authorized. The initial FAST job is
engine-core; later native HA/UI/runtime gates must be added rather than replaced.
