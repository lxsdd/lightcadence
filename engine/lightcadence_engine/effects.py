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
    STATIC = "static"
    LAYERED = "layered"


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
    layers: tuple['Layer', ...] = ()

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
        if not isinstance(self.layers, tuple) or not all(isinstance(layer, Layer) for layer in self.layers):
            raise ValueError("invalid layers")
        if self.kind is EffectKind.LAYERED:
            if not self.layers or any(layer.effect.kind is EffectKind.LAYERED for layer in self.layers):
                raise ValueError("nonempty flat layer stack required")
            if any(layer.effect.audio_source is not None and layer.effect.audio_source != self.audio_source for layer in self.layers):
                raise ValueError("one audio source per show")
            needs_audio = any(layer.effect.kind is EffectKind.AUDIO_SPECTRUM for layer in self.layers)
        else:
            if self.layers:
                raise ValueError("layers require layered effect")
            needs_audio = self.kind is EffectKind.AUDIO_SPECTRUM
        if needs_audio:
            if not isinstance(self.audio_source, str) or not self.audio_source.strip():
                raise ValueError("select an audio source")
        elif self.audio_source is not None:
            raise ValueError("audio source is only valid for audio effects")

    def render(self, elapsed: float, position: float, audio=None) -> LightTarget:
        if (not isfinite(elapsed) or elapsed < 0 or
                not isfinite(position) or not 0 <= position <= 1):
            raise ValueError("invalid elapsed time or position")
        if self.kind is EffectKind.STATIC:
            return LightTarget(*self.color, self.brightness)
        if self.kind is EffectKind.LAYERED:
            rgb = (0., 0., 0.)
            for layer in self.layers:
                target = layer.effect.render(elapsed, position, audio)
                rgb = blend_linear(rgb, target, layer.opacity, layer.blend)
            peak = max(rgb)
            return LightTarget(*(v / peak if peak else 0. for v in rgb), peak * self.brightness)
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


class Blend(StrEnum):
    OVER = "over"
    ADD = "add"
    MAX = "max"


@dataclass(frozen=True, slots=True)
class Layer:
    effect: Effect
    opacity: float = 1.
    blend: Blend = Blend.OVER

    def __post_init__(self):
        if not isinstance(self.effect, Effect) or self.effect.kind is EffectKind.LAYERED:
            raise ValueError("invalid layer effect")
        if type(self.opacity) not in (int, float) or not isfinite(self.opacity) or not 0 <= self.opacity <= 1:
            raise ValueError("invalid layer opacity")
        if not isinstance(self.blend, Blend):
            raise ValueError("invalid blend")


def blend_linear(base, target, opacity, blend):
    """Combine premultiplied linear-light energy; normalize once at the output."""
    rgb = (target.red, target.green, target.blue)
    # Effect brightness determines coverage for OVER, so a scanner's dark
    # region reveals the underlying ambient layer rather than erasing it.
    alpha = opacity * target.brightness
    if blend is Blend.OVER:
        return tuple(a * (1 - alpha) + b * alpha for a, b in zip(base, rgb))
    if blend is Blend.ADD:
        return tuple(min(1., a + b * alpha) for a, b in zip(base, rgb))
    return tuple(max(a, b * alpha) for a, b in zip(base, rgb))


def decode_effect(value, *, template=False):
    """Shared API/preset parser. Persisted presets never contain live source IDs."""
    base = {"kind", "period_seconds", "brightness", "color", "width"}
    if not isinstance(value, dict) or "kind" not in value or not value.keys() <= base | {"layers"} | (set() if template else {"audio_source"}):
        raise ValueError("invalid effect fields")
    if template and not base <= value.keys():
        raise ValueError("incomplete preset")
    params = dict(value)
    kind = EffectKind(params.pop("kind"))
    raw_layers = params.pop("layers", None)
    if "color" in params:
        if not isinstance(params["color"], list):
            raise ValueError("invalid color")
        params["color"] = tuple(params["color"])
    if kind is EffectKind.LAYERED:
        if not isinstance(raw_layers, list) or not raw_layers:
            raise ValueError("select layers")
        source = params.get("audio_source")
        layers = []
        for raw in raw_layers:
            if not isinstance(raw, dict) or set(raw) != {"effect", "opacity", "blend"}:
                raise ValueError("invalid layer fields")
            child = raw["effect"]
            if not isinstance(child, dict) or set(child) != base or child["kind"] == "layered":
                raise ValueError("invalid nested effect")
            if child["kind"] == "audio_spectrum":
                if template:
                    source = "selection-required"
                child = {**child, "audio_source": source}
            layers.append(Layer(decode_effect(child), raw["opacity"], Blend(raw["blend"])))
        params.update(layers=tuple(layers), audio_source=source)
    elif raw_layers is not None:
        raise ValueError("layers require layered effect")
    elif template and kind is EffectKind.AUDIO_SPECTRUM:
        params["audio_source"] = "selection-required"
    return Effect(kind, **params)


def encode_preset(spec):
    result = {"kind": spec.kind.value, "period_seconds": spec.period_seconds,
              "brightness": spec.brightness, "color": list(spec.color), "width": spec.width}
    if spec.kind is EffectKind.LAYERED:
        result["layers"] = [{"effect": encode_preset(layer.effect), "opacity": layer.opacity,
                             "blend": layer.blend.value} for layer in spec.layers]
    return result
