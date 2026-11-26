"""Local PCM ingestion and analysis; no recording, playback, or synthetic beats."""
from collections import deque
from dataclasses import dataclass
from math import isfinite
from statistics import median
from time import monotonic
from uuid import uuid4

SAMPLE_RATE = 48000
FRAME_SAMPLES = 2048
FRAME_BYTES = FRAME_SAMPLES * 2
FRESH_SECONDS = .25
FADE_SECONDS = .5
AMBIENT = (.06, .06, .06)


@dataclass(frozen=True, slots=True)
class Features:
    rms: float
    bands: tuple[float, float, float]
    beat: bool
    bpm: float | None


class Analyzer:
    """Windowed real FFT, RMS bands and conservative bass onset estimate."""
    def __init__(self):
        import numpy as np
        self.np = np
        self.window = np.hanning(FRAME_SAMPLES)
        frequencies = np.fft.rfftfreq(FRAME_SAMPLES, 1 / SAMPLE_RATE)
        self.masks = [(frequencies >= lo) & (frequencies < hi)
                      for lo, hi in ((20, 250), (250, 2500), (2500, 24001))]
        self.norm = 2 / (FRAME_SAMPLES * float(np.sum(self.window ** 2)))
        self.previous = None
        self.flux = deque(maxlen=32)
        self.intervals = deque(maxlen=8)
        self.last_beat = None

    def analyze(self, pcm, at):
        if not isinstance(pcm, bytes) or len(pcm) != FRAME_BYTES or not isfinite(at):
            raise ValueError("invalid PCM frame")
        np = self.np
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float64) / 32768
        samples -= np.mean(samples)
        rms = float(np.sqrt(np.mean(samples ** 2)))
        power = np.abs(np.fft.rfft(samples * self.window)) ** 2 * self.norm
        power[-1] *= .5
        bands = tuple(min(1., float(np.sqrt(np.sum(power[m])))) for m in self.masks)
        delta = max(0., bands[0] - self.previous) if self.previous is not None else 0.
        threshold = max(.025, 2.5 * median(self.flux)) if self.flux else .025
        beat = (self.previous is not None and rms > .015 and delta > threshold
                and (self.last_beat is None or at - self.last_beat >= .25))
        if beat:
            if self.last_beat is not None:
                interval = at - self.last_beat
                if .25 <= interval <= 2:
                    self.intervals.append(interval)
                else:
                    self.intervals.clear()
            self.last_beat = at
        if self.last_beat is not None and at - self.last_beat > 2.:
            self.intervals.clear()
            self.last_beat = None
        self.previous = bands[0]
        self.flux.append(delta)
        bpm = None
        if len(self.intervals) >= 3:
            typical = median(self.intervals)
            if all(abs(i - typical) <= .15 * typical for i in self.intervals):
                bpm = round(60 / typical, 1)
        return Features(rms, bands, beat, bpm)


@dataclass(slots=True)
class _Source:
    name: str
    analyzer: Analyzer
    kind: str = "stdin"
    clock_mapping: str = "arrival_only"
    read_time: float | None = None
    transit_seconds: float | None = None
    jitter_seconds: float | None = None
    sequence: int = -1
    source_time: float | None = None
    arrival: float | None = None
    features: Features | None = None
    beats: int = 0


class AudioHub:
    """Server-owned streams. One latest feature per source, no raw PCM retained.

    Sequence is the source sample clock (2048 samples per increment). Arrival
    uses engine monotonic time; neither is a measured playback clock mapping.
    """
    def __init__(self, clock=monotonic):
        self.clock = clock
        self.sources = {}

    def open(self, name, kind="stdin", clock_mapping="arrival_only"):
        if not isinstance(name, str) or not name.strip() or len(name) > 128:
            raise ValueError("invalid audio source name")
        if (kind, clock_mapping) not in (("stdin", "arrival_only"), ("pulse_monitor", "local_read_monotonic"),
                                         ("pulse_microphone", "local_read_monotonic")):
            raise ValueError("unsupported source clock or kind")
        source = str(uuid4())
        self.sources[source] = _Source(name.strip(), Analyzer(), kind, clock_mapping)
        return source

    def close(self, source):
        if source not in self.sources:
            raise ValueError("unknown audio source")
        del self.sources[source]

    def push(self, source, sequence, pcm, read_time=None):
        s = self.sources.get(source)
        if s is None or type(sequence) is not int or sequence < 0 or sequence <= s.sequence:
            raise ValueError("unknown source or stale audio sequence")
        if not isinstance(pcm, bytes) or len(pcm) != FRAME_BYTES:
            raise ValueError("invalid PCM frame length")
        now = self.clock()
        if s.arrival is not None and now < s.arrival:
            raise ValueError("nonmonotonic arrival")
        if s.clock_mapping == "local_read_monotonic":
            if (type(read_time) not in (int, float) or not isfinite(read_time) or
                    read_time < 0 or read_time > now or now - read_time > FRESH_SECONDS or
                    (s.read_time is not None and read_time < s.read_time)):
                raise ValueError("invalid or stale local read clock")
            transit = now - read_time
        elif read_time is not None:
            raise ValueError("unexpected local read clock")
        else:
            transit = None
        source_time = sequence * FRAME_SAMPLES / SAMPLE_RATE
        if not isfinite(source_time):
            raise ValueError("invalid source clock")
        if (sequence != s.sequence + 1 or (s.arrival is not None and now - s.arrival > FRESH_SECONDS)
                or (s.read_time is not None and read_time - s.read_time > FRESH_SECONDS)):
            s.analyzer = Analyzer()
        features = s.analyzer.analyze(pcm, source_time)
        if transit is not None:
            if s.transit_seconds is not None and sequence == s.sequence + 1 and now - s.arrival <= FRESH_SECONDS:
                delta = abs(transit - s.transit_seconds)
                s.jitter_seconds = delta if s.jitter_seconds is None else .875 * s.jitter_seconds + .125 * delta
            else:
                s.jitter_seconds = None
            s.transit_seconds = transit
            s.read_time = read_time
        s.sequence, s.source_time, s.arrival, s.features = sequence, source_time, now, features
        s.beats += int(features.beat)

    def levels(self, source, at):
        s = self.sources.get(source)
        if s is None or s.arrival is None or s.features is None:
            return AMBIENT
        timestamp = s.read_time if s.read_time is not None else s.arrival
        fade = max(0., min(1., (at - timestamp - FRESH_SECONDS) / FADE_SECONDS))
        return tuple(v * (1 - fade) + base * fade for v, base in zip(s.features.bands, AMBIENT))

    def snapshot(self):
        now = self.clock()
        result = []
        for source, s in self.sources.items():
            timestamp = s.read_time if s.read_time is not None else s.arrival
            age = None if timestamp is None else max(0., now - timestamp)
            live = age is not None and age <= FRESH_SECONDS
            f = s.features if live else None
            result.append({"id": source, "name": s.name, "status": "live" if live else
                           "waiting" if age is None else "lost", "sequence": s.sequence,
                           "source_time": s.source_time, "age_seconds": age,
                           "rms": f.rms if f else None, "bands": list(f.bands) if f else None,
                           "beat_count": s.beats, "bpm": f.bpm if f else None,
                           "kind": s.kind, "clock_mapping": s.clock_mapping,
                           "read_to_engine_seconds": s.transit_seconds if live else None,
                           "transport_jitter_seconds": s.jitter_seconds if live else None,
                           "playback_clock_verified": False})
        return result
