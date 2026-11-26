"""Immutable piecewise-constant musical clock, independent of audio capture."""
from dataclasses import dataclass
from bisect import bisect_right
from math import isfinite
from typing import NamedTuple


def number(value):
    return type(value) in (int, float) and isfinite(value)


class TempoPoint(NamedTuple):
    beat: float
    bpm: float


class Marker(NamedTuple):
    time: float
    name: str


@dataclass(frozen=True, slots=True)
class TempoMap:
    points: tuple[TempoPoint, ...]

    def __post_init__(self):
        if (not isinstance(self.points, tuple) or not self.points
                or not all(isinstance(p, TempoPoint) and number(p.beat)
                           and number(p.bpm) and p.beat >= 0 and p.bpm > 0
                           for p in self.points)
                or self.points[0].beat != 0
                or any(a.beat >= b.beat for a,b in zip(self.points,self.points[1:]))):
            raise ValueError("tempo must begin at beat zero and increase strictly")
        self.boundaries()

    def boundaries(self):
        seconds = [0.]
        for a,b in zip(self.points,self.points[1:]):
            at = seconds[-1] + (b.beat-a.beat) / a.bpm * 60.
            if not isfinite(at) or at <= seconds[-1]:
                raise ValueError("unrepresentable tempo segment")
            seconds.append(at)
        return seconds

    def seconds(self, beat):
        if not number(beat) or beat < 0:
            raise ValueError("invalid beat")
        i = bisect_right([p.beat for p in self.points],beat)-1
        p = self.points[i]
        at = self.boundaries()[i] + (beat-p.beat)/p.bpm*60.
        if not isfinite(at):
            raise ValueError("unrepresentable musical time")
        return at

    def beat(self, seconds):
        if not number(seconds) or seconds < 0:
            raise ValueError("invalid seconds")
        times = self.boundaries()
        i = bisect_right(times,seconds)-1
        p = self.points[i]
        beat = p.beat+(seconds-times[i])/60.*p.bpm
        if not isfinite(beat):
            raise ValueError("unrepresentable musical time")
        return beat


def decode_tempo(raw):
    if not isinstance(raw,list):
        raise ValueError("tempo map must be a list")
    if not all(isinstance(p,dict) and set(p)=={"beat","bpm"} for p in raw):
        raise ValueError("invalid tempo point")
    return TempoMap(tuple(TempoPoint(**p) for p in raw))


def decode_markers(raw):
    if not isinstance(raw,list) or not all(isinstance(m,dict) and set(m)=={"time","name"} for m in raw):
        raise ValueError("invalid markers")
    return tuple(Marker(**m) for m in raw)
