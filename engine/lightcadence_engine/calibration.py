"""Owner-authored device calibration in the common linear-light contract.

RGB anchors are sRGB8 pairs measured by visual comparison, not photometry.
A device profile overrides its exact model/firmware/transport profile, never
stacks it. Black and zero intensity are invariant; original snapshots and
manual native state never pass through this correction.
"""
from dataclasses import dataclass
from math import isfinite
from .effects import LightTarget, linear_channel


def rgb(value):
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError("RGB8 triple required")
    return tuple(linear_channel(v) for v in value)


def unit(value):
    if type(value) not in (int, float) or not isfinite(value) or not 0 <= value <= 1:
        raise ValueError("normalized finite value required")
    return float(value)


def selector(value):
    if not isinstance(value, dict):
        raise ValueError("invalid calibration selector")
    keys = {"model", "firmware", "transport"}
    if set(value) not in (keys, keys | {"device_id"}):
        raise ValueError("exact device or model binding required")
    if any(not isinstance(v, str) or not v.strip() for v in value.values()):
        raise ValueError("nonempty calibration identity required")
    if value["transport"] not in ("chroma_v2", "yeelight_tcp", "tuya_v35"):
        raise ValueError("unsupported calibration transport")
    return tuple(sorted(value.items()))


@dataclass(frozen=True, slots=True)
class Correction:
    points: tuple
    brightness: tuple
    minimum: float

    @classmethod
    def decode(cls, value):
        if not isinstance(value, dict) or set(value) != {"name", "selector", "points", "brightness", "minimum"}:
            raise ValueError("invalid calibration profile")
        if not isinstance(value["name"], str) or not value["name"].strip():
            raise ValueError("name required")
        selector(value["selector"])
        if not isinstance(value["points"], list) or not isinstance(value["brightness"], list):
            raise ValueError("calibration point lists required")
        points = []
        for p in value["points"]:
            if not isinstance(p, dict) or set(p) != {"goal", "command"}:
                raise ValueError("invalid RGB anchor")
            a, b = rgb(p["goal"]), rgb(p["command"])
            if max(a) == 0 or max(b) == 0 or any(a == x for x, _ in points):
                raise ValueError("unique nonblack RGB anchors required")
            points.append((a, b))
        curve = []
        for p in value["brightness"]:
            if not isinstance(p, dict) or set(p) != {"goal", "command"}:
                raise ValueError("invalid intensity anchor")
            a, b = unit(p["goal"]), unit(p["command"])
            if a == 0 or b == 0 or (curve and (a <= curve[-1][0] or b < curve[-1][1])):
                raise ValueError("positive ordered monotone brightness curve required")
            curve.append((a, b))
        if not curve or curve[-1][0] != 1:
            curve.append((1., 1.))
        minimum = unit(value["minimum"])
        if minimum > curve[-1][1]:
            raise ValueError("minimum exceeds maximum output")
        return cls(tuple(points), ((0., 0.), *curve), minimum)

    def apply(self, target):
        a = (target.red, target.green, target.blue)
        if target.brightness == 0 or max(a) == 0:
            return target
        total = 0.
        delta = [0., 0., 0.]
        out = a
        for goal, command in self.points:
            distance = sum((x-y)**2 for x,y in zip(a, goal))
            if distance < 1e-20:
                out = command
                break
            weight = 1. / distance
            total += weight
            for i in range(3):
                delta[i] += weight * (command[i]-goal[i])
        else:
            if total:
                out = tuple(max(0., min(1., x+d/total)) for x,d in zip(a,delta))
        level = target.brightness
        for (x,y), (nx,ny) in zip(self.brightness,self.brightness[1:]):
            if level <= nx:
                level = y+(ny-y)*(level-x)/(nx-x)
                break
        return LightTarget(*out, max(self.minimum, min(1., level)))


PROFILE_FIELDS = ("name", "selector", "points", "brightness", "minimum")


def compile_profiles(rows):
    result = {}
    for row in rows:
        value = {k:row[k] for k in PROFILE_FIELDS}
        key = selector(value["selector"])
        if key in result:
            raise ValueError("duplicate calibration selector")
        result[key] = Correction.decode(value)
    return result


def raw_targets(value, devices):
    if not isinstance(value, dict) or set(value) != set(devices):
        raise ValueError("one raw target per selected lamp required")
    result = {}
    for device, item in value.items():
        if not isinstance(item, dict) or set(item) != {"rgb", "brightness"}:
            raise ValueError("invalid comparison target")
        result[device] = LightTarget(*rgb(item["rgb"]), unit(item["brightness"]))
    return result
