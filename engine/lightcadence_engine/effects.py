"""Deterministic linear-light effect targets; transport conversion is separate."""
from dataclasses import dataclass
from enum import StrEnum
from math import cos, isfinite, pi
import colorsys


def linear_channel(channel: int) -> float:
    """Canonical 8-bit sRGB to normalized linear-light conversion."""
    if type(channel) is not int or not 0 <= channel <= 255:
        raise ValueError("invalid sRGB channel")
    value = channel / 255
    return value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4


class EffectKind(StrEnum):
    RAINBOW = "rainbow"
    SCANNER = "scanner"
    PULSE = "pulse"
    AUDIO_SPECTRUM = "audio_spectrum"


@dataclass(frozen=True, slots=True)
class LightTarget:
    red: float
    green: float
    blue: float
    brightness: float

    def __post_init__(self):
        if not all(isinstance(x, (int, float)) and not isinstance(x, bool)
                   and isfinite(x) and 0 <= x <= 1
                   for x in (self.red, self.green, self.blue, self.brightness)):
            raise ValueError("linear-light components must be finite and in [0, 1]")


@dataclass(frozen=True, slots=True)
class Effect:
    kind: EffectKind
    period_seconds: float = 4.0
    brightness: float = 1.0
    color: tuple[float, float, float] = (1.0, 0.0, 0.0)
    width: float = 0.25
    audio_source: str | None = None

    def __post_init__(self):
        if not isinstance(self.kind, EffectKind):
            raise ValueError("unknown effect")
        if (not isinstance(self.period_seconds, (int, float))
                or isinstance(self.period_seconds, bool)
                or not isfinite(self.period_seconds) or self.period_seconds <= 0):
            raise ValueError("period must be positive and finite")
        if (not isinstance(self.width, (int, float)) or isinstance(self.width, bool)
                or not isfinite(self.width) or not 0 < self.width <= 1):
            raise ValueError("width must be in (0, 1]")
        if not isinstance(self.color, tuple) or len(self.color) != 3:
            raise ValueError("color must be an immutable RGB triple")
        LightTarget(*self.color, self.brightness)
        if self.kind is EffectKind.AUDIO_SPECTRUM:
            if not isinstance(self.audio_source, str) or not self.audio_source.strip():
                raise ValueError("select an audio source")
        elif self.audio_source is not None:
            raise ValueError("audio source is only valid for audio effects")

    def render(self, elapsed: float, position: float, audio=None) -> LightTarget:
        if (not isfinite(elapsed) or elapsed < 0 or
                not isfinite(position) or not 0 <= position <= 1):
            raise ValueError("invalid elapsed time or position")
        if self.kind is EffectKind.AUDIO_SPECTRUM:
            bands = audio if audio is not None else (.06, .06, .06)
            LightTarget(*bands, 1.)
            # Spatially distribute bass, mids and treble across the existing path.
            x = position * 2
            weights = (max(0., 1 - x), 1 - abs(x - 1), max(0., x - 1))
            rgb = tuple(a * w for a, w in zip(bands, weights))
            peak = max(rgb)
            return LightTarget(*(v / peak if peak else 0. for v in rgb),
                               self.brightness * peak)
        phase = (elapsed % self.period_seconds) / self.period_seconds
        if self.kind is EffectKind.RAINBOW:
            return LightTarget(*colorsys.hsv_to_rgb((position + phase) % 1, 1, 1),
                               self.brightness)
        if self.kind is EffectKind.SCANNER:
            center = 1 - abs(2 * phase - 1)
            level = max(0.0, 1 - abs(position - center) / self.width)
        else:
            level = (1 - cos(2 * pi * phase)) / 2
        return LightTarget(*self.color, self.brightness * level)
