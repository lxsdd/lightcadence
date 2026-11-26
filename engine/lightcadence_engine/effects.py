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
    TIMELINE = "timeline"


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
    tracks: tuple['Track', ...] = ()
    loop: bool = True
    auto_end: bool = False

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
        if type(self.loop) is not bool or not isinstance(self.tracks, tuple) or not all(isinstance(t, Track) for t in self.tracks):
            raise ValueError("invalid timeline")
        if type(self.auto_end) is not bool or (self.auto_end and (self.kind is not EffectKind.TIMELINE or self.loop)):
            raise ValueError("automatic stop requires a one-shot timeline")
        if self.kind is EffectKind.TIMELINE:
            if not self.tracks or not any(t.clips for t in self.tracks) or self.layers:
                raise ValueError("timeline requires clips, not layers")
            if any(c.layer.effect.audio_source is not None and c.layer.effect.audio_source != self.audio_source
                   for t in self.tracks for c in t.clips):
                raise ValueError("one audio source per show")
            needs_audio = any(c.layer.effect.kind is EffectKind.AUDIO_SPECTRUM for t in self.tracks for c in t.clips)
        elif self.tracks or self.loop is not True:
            raise ValueError("timeline fields require timeline effect")
        elif self.kind is EffectKind.LAYERED:
            if not self.layers or any(layer.effect.kind in (EffectKind.LAYERED, EffectKind.TIMELINE) for layer in self.layers):
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

    @property
    def duration(self):
        return max((c.start + c.duration for t in self.tracks for c in t.clips), default=0.)

    def validate_targets(self, devices):
        selected = set(devices)
        groups = self.layers if self.kind is EffectKind.LAYERED else self.tracks if self.kind is EffectKind.TIMELINE else ()
        covered = set()
        for group in groups:
            mask = group.devices
            if mask is not None and not set(mask) <= selected:
                raise ValueError("effect targets outside selected zone")
            if not isinstance(group, Track) or group.clips:
                covered.update(selected if mask is None else mask)
        if groups and covered != selected:
            raise ValueError("every selected lamp must belong to a nonempty layer or track")

    def render(self, elapsed: float, position: float, audio=None, device_id=None, group_gains=None) -> LightTarget:
        if (not isfinite(elapsed) or elapsed < 0 or
                not isfinite(position) or not 0 <= position <= 1):
            raise ValueError("invalid elapsed time or position")
        if self.kind is EffectKind.TIMELINE:
            duration = self.duration
            # Non-looping shows retain their lease and go dark at the end.
            # Explicit Stop uses the existing confirmed stop/restore path.
            if not self.loop and elapsed >= duration:
                return LightTarget(0., 0., 0., 0.)
            at = elapsed % duration if self.loop else elapsed
            rgb = (0., 0., 0.)
            for index, track in enumerate(self.tracks):
                gain = 1. if group_gains is None else group_gains[index]
                if gain == 0.:
                    continue
                if track.devices is not None and device_id not in track.devices:
                    continue
                for clip in track.clips:
                    local = at - clip.start
                    if 0 <= local < clip.duration:
                        target = clip.layer.effect.render(local, position, audio)
                        rgb = blend_linear(rgb, target, clip.level(local) * gain, clip.layer.blend)
            peak = max(rgb)
            return LightTarget(*(v / peak if peak else 0. for v in rgb), peak * self.brightness)
        if self.kind is EffectKind.STATIC:
            return LightTarget(*self.color, self.brightness)
        if self.kind is EffectKind.LAYERED:
            rgb = (0., 0., 0.)
            for index, layer in enumerate(self.layers):
                gain = 1. if group_gains is None else group_gains[index]
                if gain == 0.:
                    continue
                if layer.devices is not None and device_id not in layer.devices:
                    continue
                target = layer.effect.render(elapsed, position, audio)
                rgb = blend_linear(rgb, target, layer.opacity * gain, layer.blend)
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
    devices: tuple[str, ...] | None = None

    def __post_init__(self):
        validate_mask(self.devices)
        if not isinstance(self.effect, Effect) or self.effect.kind in (EffectKind.LAYERED, EffectKind.TIMELINE):
            raise ValueError("invalid layer effect")
        if type(self.opacity) not in (int, float) or not isfinite(self.opacity) or not 0 <= self.opacity <= 1:
            raise ValueError("invalid layer opacity")
        if not isinstance(self.blend, Blend):
            raise ValueError("invalid blend")


@dataclass(frozen=True, slots=True)
class Keyframe:
    time: float
    value: float
    curve: str = "linear"

    def __post_init__(self):
        if (type(self.time) not in (int, float) or not isfinite(self.time) or self.time < 0
                or type(self.value) not in (int, float) or not isfinite(self.value) or not 0 <= self.value <= 1
                or self.curve not in ("linear", "hold", "smooth")):
            raise ValueError("invalid opacity keyframe")


@dataclass(frozen=True, slots=True)
class Clip:
    start: float
    duration: float
    layer: Layer
    fade_in: float = 0.
    fade_out: float = 0.
    keyframes: tuple[Keyframe, ...] = ()

    def __post_init__(self):
        if (not all(type(v) in (int, float) and isfinite(v) and v >= 0
                    for v in (self.start, self.duration, self.fade_in, self.fade_out))
                or self.duration <= 0 or not isfinite(self.start + self.duration)
                or self.start + self.duration <= self.start
                or self.fade_in + self.fade_out > self.duration or not isinstance(self.layer, Layer)
                or self.layer.devices is not None
                or not isinstance(self.keyframes, tuple)
                or not all(isinstance(k, Keyframe) and k.time <= self.duration for k in self.keyframes)
                or any(a.time >= b.time for a, b in zip(self.keyframes, self.keyframes[1:]))):
            raise ValueError("invalid clip timing")

    def level(self, local):
        level = 1.
        if self.keyframes:
            level = self.keyframes[0].value
            for a, b in zip(self.keyframes, self.keyframes[1:]):
                if local < b.time:
                    x = max(0., (local - a.time) / (b.time - a.time))
                    if a.curve == "hold":
                        x = 0.
                    elif a.curve == "smooth":
                        x = x * x * (3 - 2 * x)
                    level = a.value + (b.value - a.value) * x
                    break
                level = b.value
        fade = min(1., local / self.fade_in if self.fade_in else 1.,
                   (self.duration - local) / self.fade_out if self.fade_out else 1.)
        return self.layer.opacity * level * max(0., fade)


@dataclass(frozen=True, slots=True)
class Track:
    name: str
    clips: tuple[Clip, ...]
    devices: tuple[str, ...] | None = None

    def __post_init__(self):
        validate_mask(self.devices)
        if (not isinstance(self.name, str) or not self.name.strip()
                or not isinstance(self.clips, tuple) or not all(isinstance(c, Clip) for c in self.clips)):
            raise ValueError("invalid track")


def validate_mask(mask):
    if mask is not None and (not isinstance(mask, tuple) or not mask
            or not all(isinstance(d, str) and d.strip() for d in mask)
            or len(set(mask)) != len(mask)):
        raise ValueError("invalid device mask")


def decode_mask(obj):
    if "devices" not in obj:
        return None
    if not isinstance(obj["devices"], list):
        raise ValueError("device mask must be a list")
    result = tuple(obj["devices"])
    validate_mask(result)
    return result


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
    if not isinstance(value, dict) or "kind" not in value or not value.keys() <= base | {"layers", "tracks", "loop", "auto_end"} | (set() if template else {"audio_source"}):
        raise ValueError("invalid effect fields")
    if template and not base <= value.keys():
        raise ValueError("incomplete preset")
    params = dict(value)
    kind = EffectKind(params.pop("kind"))
    raw_layers = params.pop("layers", None)
    raw_tracks = params.pop("tracks", None)
    loop = params.pop("loop", True)
    auto_end = params.pop("auto_end", False)
    if kind is not EffectKind.TIMELINE and ("tracks" in value or "loop" in value or "auto_end" in value):
        raise ValueError("timeline fields require timeline effect")
    if "color" in params:
        if not isinstance(params["color"], list):
            raise ValueError("invalid color")
        params["color"] = tuple(params["color"])
    if kind is EffectKind.TIMELINE:
        if raw_layers is not None or not isinstance(raw_tracks, list):
            raise ValueError("select timeline tracks")
        source = params.get("audio_source")
        tracks = []
        for track in raw_tracks:
            if not isinstance(track, dict) or not {"name", "clips"} <= track.keys() or not track.keys() <= {"name", "clips", "devices"} or not isinstance(track["clips"], list):
                raise ValueError("invalid timeline track")
            clips = []
            for raw in track["clips"]:
                if not isinstance(raw, dict) or set(raw) != {"start", "duration", "layer", "fade_in", "fade_out", "keyframes"}:
                    raise ValueError("invalid clip fields")
                layer = raw["layer"]
                if not isinstance(layer, dict) or set(layer) != {"effect", "opacity", "blend"}:
                    raise ValueError("invalid clip layer")
                child = layer["effect"]
                if not isinstance(child, dict) or set(child) != base or child["kind"] in ("layered", "timeline"):
                    raise ValueError("invalid clip effect")
                if child["kind"] == "audio_spectrum":
                    if template:
                        source = "selection-required"
                    child = {**child, "audio_source": source}
                if not isinstance(raw["keyframes"], list):
                    raise ValueError("invalid keyframes")
                keys = []
                for key in raw["keyframes"]:
                    if not isinstance(key, dict) or set(key) != {"time", "value", "curve"}:
                        raise ValueError("invalid keyframe fields")
                    keys.append(Keyframe(**key))
                clips.append(Clip(raw["start"], raw["duration"], Layer(decode_effect(child), layer["opacity"], Blend(layer["blend"])),
                                  raw["fade_in"], raw["fade_out"], tuple(keys)))
            tracks.append(Track(track["name"], tuple(clips), decode_mask(track)))
        params.update(tracks=tuple(tracks), loop=loop, auto_end=auto_end, audio_source=source)
    elif kind is EffectKind.LAYERED:
        if not isinstance(raw_layers, list) or not raw_layers:
            raise ValueError("select layers")
        source = params.get("audio_source")
        layers = []
        for raw in raw_layers:
            if not isinstance(raw, dict) or not {"effect", "opacity", "blend"} <= raw.keys() or not raw.keys() <= {"effect", "opacity", "blend", "devices"}:
                raise ValueError("invalid layer fields")
            child = raw["effect"]
            if not isinstance(child, dict) or set(child) != base or child["kind"] in ("layered", "timeline"):
                raise ValueError("invalid nested effect")
            if child["kind"] == "audio_spectrum":
                if template:
                    source = "selection-required"
                child = {**child, "audio_source": source}
            layers.append(Layer(decode_effect(child), raw["opacity"], Blend(raw["blend"]), decode_mask(raw)))
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
                             "blend": layer.blend.value, **({"devices": list(layer.devices)} if layer.devices is not None else {})} for layer in spec.layers]
    if spec.kind is EffectKind.TIMELINE:
        result["loop"] = spec.loop
        if spec.auto_end:
            result["auto_end"] = True
        result["tracks"] = [{"name": t.name, **({"devices": list(t.devices)} if t.devices is not None else {}), "clips": [{"start": c.start, "duration": c.duration,
            "fade_in": c.fade_in, "fade_out": c.fade_out, "layer": {"effect": encode_preset(c.layer.effect),
            "opacity": c.layer.opacity, "blend": c.layer.blend.value},
            "keyframes": [{"time": k.time, "value": k.value, "curve": k.curve} for k in c.keyframes]}
            for c in t.clips]} for t in spec.tracks]
    return result
