# Explicit local Tuya 3.5 RGB candidate

The existing physical runtime can mix explicitly enrolled Chroma, Yeelight TCP
and Tuya LAN 3.5 profiles. This is a candidate, not hardware qualification.
The public MIT-licensed TinyTuya v1.21.0 library is used as a pinned dependency,
not vendored code. Its pinned source constructor, send retry control and
set_multiple_values fallback were reviewed. The crypto stack and dependencies
are pinned; full distribution/license/security qualification remains open.
No scans, cloud calls, automatic key extraction, writes to HA configuration,
or automatic device selection. Only the standard twelve-hex-digit HSV format
(H 0..360, S and V 0..1000), explicit colour mode and boolean power are supported.

Owner-local existing read-only device file: use transport tuya_v35, explicit
ha_registry_id, model, firmware, host (local IPv4), interval, lan_id (actual
Tuya device ID), optionally tcp_port (6668 by default), and a tuya object
with local_key (16 Latin-1 bytes), power_dp, mode_dp, colour_dp (distinct
integer IDs). These must come from verified device documentation/inspection,
never guessed. Protect the owner-local file, never commit it. No real IDs or
keys belong in examples or diagnostics. Model and firmware are configured
metadata, not freshly attested by the device. Authentication with the local
key and matching supported DPs is not a guarantee of physical identity.

All per-device blocking library operations run outside the asyncio render
loop and settle before ownership release, even on cancellation. Output uses
one persistent authenticated connection, one command batch, coalesced
quantized targets and no retries/reconnect after uncertainty. State readback
is a separate fresh transaction, not ACK or cache. Native HSV is saved
losslessly (including hue at zero saturation) in the existing recovery
journal. Manual RGB targets are quantized once before detaching; white/scene
mode and incomplete snapshots are refused. Never power on an already-off
device just to change hidden attributes. Actual device acceptance, datapoint
maps, colour/brightness calibration, physical timing, failure/recovery
and coexistence with external clients are still required.
