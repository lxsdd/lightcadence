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
playhead telemetry is server-derived. No per-track target masks, tempo/marker
maps, transport seeking, arbitrary curve editing, recorded audio or hardware
synchronization claimed in this initial block.
