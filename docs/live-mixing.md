# Live mixing

The running session owns its master gain and one gain/mute/solo channel per
layer or timeline track. Defaults are unity and no toggles. Mute wins over solo;
if any channel is soloed, non-soloed channels contribute nothing. The master
attenuates the final normalized brightness. Gain multiplies existing opacity,
fades and keys before the original blend. The master is also available for
single effects. No effect preset, playhead, stop policy, device mask, lease or
recovery record is changed. A new session starts at defaults.

The existing authenticated preview/physical command accepts mix_control with
current instance_id, session_id, revision, action (master/gain/mute/solo), value
and index only for group controls (zero-based). Invalid values, stale revisions
and stopped/stopping sessions are rejected. A successful mutation increments
revision and generation and discards queued previous frames. Already submitted
hardware frames cannot be recalled. The existing output coordinator remains
the only device writer. Manual detach cannot be undone by mixing.

The shared HA workspace exposes a collapsible live mixer in each active session.
Sliders commit on change rather than transmitting every pointer event. The
mixer is not saved to the scene library. The physical path and actual latency
remain subject to hardware acceptance.
