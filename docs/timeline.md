# Timeline Studio, initial seconds-based editing

Timeline is another immutable Effect in the existing scene, zone, session and
lease pipeline. A track contains an ordered list of clips. Tracks and clips
are composited in listed order, bottom to top, using the existing linear-light
over/add/max rules. Overlap plus fade envelopes permits layered transitions
without a second device writer. Each clip starts its effect clock at its own
start, with duration, fade-in/out and opacity keys (linear, hold, smooth;
outgoing curve on each key). Keys outside the clip or non-increasing times
are rejected. Audio clips share one explicitly selected live source; persisted
templates never store live source IDs. Spatial coordinates are frozen at start.
Timeline duration is the latest clip end. Looping wraps on server monotonic
time; one-shot playback goes dark at end and retains leases until explicit Stop
so the existing restore/hold/off rules remain authoritative. No automatic replay.
The UI exposes track/clip/key editing and a proportional visual time ruler;
playhead telemetry is server-derived. Pause/resume and explicit seek are
revisioned, instance-fenced session actions. The same lease remains owned;
the monotonic timeline clock freezes on pause and resumes from the frozen
position. Audio-reactive targets freeze the latest band values on pause
and use current live bands after resume; seek does not seek external audio.
Old queued frames are invalidated by generation; an already-submitted
physical frame cannot be recalled, and transport submission is not optical
confirmation. A seek to the end of a looping timeline wraps to zero.
One-shot end still requires Stop. No per-track target masks, tempo/marker
maps, recorded audio or hardware synchronization claimed.


## Target masks and optional end action

A layer or timeline track can omit its devices field to target the entire
selected zone or specify a nonempty ordered list of stable registry IDs.
Masks cannot reference outside the chosen zone; the union of actual layers
or nonempty tracks must cover the selected devices. Validation happens
before device I/O or lease acquisition. Changes to a stored zone that would
invalidate a scene are rejected. Detach still removes the actual lease; a
mask cannot re-enroll a detached lamp. Missing masks in existing presets
retain their previous semantics. Clip-local masks are not supported.

The optional auto_end flag is valid only on a non-looping timeline. Default
remains the previous dark target and explicit Stop. With auto_end enabled,
the monotonic end (including a seek while playing or resume at the end)
fences queued output and initiates the existing stop policy once. Pause at
the end does not release the lease until resumed. HOLD retains the last
submitted light state, not a guaranteed optical state at the exact endpoint.
RESTORE/OFF require the original confirmed stop/readback path; an uncertain
stop remains STOPPING and can only be retried explicitly. Already submitted
frames and external LAN/HA clients are not fenced by the local engine.
Preview only releases virtual leases. No new hardware evidence.
