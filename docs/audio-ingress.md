# Local PCM audio analysis

The existing engine accepts explicitly opened local streams through its authenticated
loopback API. No capture permission, audio hardware, player integration, cloud or
recording is enabled automatically. A local adapter must provide signed 16-bit
little-endian mono PCM at 48 kHz, 2048 samples per frame. This is a v1 wire format,
not a limit on lamp count, show duration or output cadence. The optional
`python -m engine.lightcadence_engine.pcm_sender --token-file LOCAL_FILE --name NAME`
reads authorized raw PCM from stdin; it never opens capture devices or plays audio.
Do not supply private audio to CI or commit local tokens.

Each source has a server-issued ID and instance fence, ordered sample-clock
sequence, no queued raw audio, current feature snapshot, source loss and conservative
beat/BPM estimate. Hann-window NumPy real FFT produces RMS bass/mid/treble.
The shared session renderer uses the selected source once per session tick.
Different zones can use independent sources. Loss fades into a low static spatial
color state; explicit close removes the source. Silence does not invent beats.
The existing HA state bridge exposes only features, not PCM, and the responsive
panel offers a source selector and analysis status.

The initial timing uses sequence-derived source time and engine arrival freshness;
it is not a synchronized playback clock. Actual latency, music tempo accuracy,
capture connectors, resampling, Sendspin/Music Assistant, WASAPI and Android
permissions remain to be qualified. No real audio capture or lamp operation has
been tested. Generated tones and onsets test the original DSP and HTTP/render path.

## Explicit Linux digital monitor or microphone adapter

On the same Linux host/monotonic clock domain as the engine, with an existing
PulseAudio or PipeWire Pulse-compatible server and installed `parec`, an owner
can choose a concrete already authorized source. The adapter does not install
packages, enumerate devices automatically, change routing, or run inside HA.
Use `pactl list short sources` locally to identify the source. For a monitor:

```sh
python -m engine.lightcadence_engine.pulse_capture --token-file LOCAL_FILE --name NAME --kind pulse_monitor --device EXPLICIT_SINK.monitor
```

For a microphone, use `--kind pulse_microphone --device EXPLICIT_SOURCE`.
Run only after the owner's capture authorization. The selected source is
converted by the existing audio server to mono s16le 48 kHz; no additional
engine dependency, container privileges, socket mount or public port.
See https://manpages.debian.org/bookworm/pulseaudio-utils/parec.1.en.html for
parec options. No captured PCM is written to disk. Ctrl-C ends capture.
The capture reader keeps only the latest frame for HTTP; it drops superseded
frames rather than letting a slow HTTP connection build a queue. Sequence gaps
reset onset analysis. The server rejects stale/future/backward read timestamps,
uses read-time freshness for fade and exposes read-to-engine delay and smoothed
transport jitter. This is a same-host pipe-read clock, not a hardware timestamp;
PulseAudio/PipeWire internal buffers, sound output latency and optical lamp
latency are excluded. No actual source or playback-clock qualification yet.
A remote Windows/Android adapter, Sendspin presentation clock and real hardware
measurements require additional work; the original stdin wire format is unchanged.
