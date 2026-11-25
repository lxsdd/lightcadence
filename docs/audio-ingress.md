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
