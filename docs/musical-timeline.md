# Authored musical time and markers

A timeline without tempo_map retains seconds-based clip timing. A timeline
with tempo_map authors clip starts, durations, fades, and opacity keyframes
in quarter-note beats (zero-based, fractional positions supported). Tempo
points start at beat zero and are strictly ordered, positive finite BPM.
Tempo changes are piecewise constant. Effect oscillator periods remain
seconds. The server maps elapsed monotonic seconds to authored beats.
Existing seek/stop/loop/auto-end contracts use real seconds; marker buttons
seek to server-projected seconds. Markers are strictly ordered named points
within the timeline, in its authored units. No live audio synchronization
or automatic beat-grid inference is implied. No time signature/bar mapping
yet. The UI locks timebase changes once clips or markers exist to prevent
silent reinterpretation of saved values. Edits affect future starts only.
Legacy seconds presets remain valid without migration.
