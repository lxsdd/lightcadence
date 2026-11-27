"""Owner-authored device calibration in the common linear-light contract.

RGB anchors are sRGB8 pairs measured by visual comparison, not photometry.
A device profile overrides its exact model/firmware/transport profile, never
stacks it. Black and zero intensity are invariant; original snapshots and
manual native state never pass through this correction.
"""
from dataclasses import dataclass
from math import isfinite
from .effects import LightTarget, KelvinTarget, linear_channel


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
    white_points: tuple = ()

    @classmethod
    def decode(cls, value):
        if not isinstance(value, dict) or set(value) not in (set(PROFILE_FIELDS), set(PROFILE_FIELDS) | {"white_points"}):
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
        white = value.get("white_points", [])
        if not isinstance(white, list):
            raise ValueError("white point list required")
        anchors = []
        mode = None
        for p in white:
            if not isinstance(p, dict) or set(p) != {"goal", "command"}:
                raise ValueError("invalid white anchor")
            goal = kelvin(p["goal"])
            command = p["command"]
            kind = "ct" if type(command) is int else "rgb"
            command = kelvin(command) if kind == "ct" else rgb(command)
            if mode is not None and mode != kind:
                raise ValueError("white anchors must use one native command mode")
            if kind == "rgb" and max(command) == 0:
                raise ValueError("white anchor must emit light")
            if anchors and (goal <= anchors[-1][0] or
                            (kind == "ct" and command < anchors[-1][1])):
                raise ValueError("ordered unique white goals and monotone Kelvin commands required")
            mode = kind
            anchors.append((goal, command))
        return cls(tuple(points), ((0., 0.), *curve), minimum, tuple(anchors))

    def level(self, level):
        if level == 0:
            return 0.
        for (x,y), (nx,ny) in zip(self.brightness,self.brightness[1:]):
            if level <= nx:
                level = y+(ny-y)*(level-x)/(nx-x)
                break
        return max(self.minimum, min(1., level))

    def white_target(self, target, native_ct):
        """Interpolate recorded white anchors only inside their measured range."""
        level = self.level(target.brightness)
        if not self.white_points:
            if not native_ct:
                raise ValueError("record RGB white anchors before projecting Kelvin")
            return KelvinTarget(1, 1, 1, level, target.kelvin)
        anchors = self.white_points
        if native_ct != (type(anchors[0][1]) is int):
            raise ValueError("white profile command mode does not match transport")
        k = target.kelvin
        if not anchors[0][0] <= k <= anchors[-1][0]:
            raise ValueError("Kelvin goal outside recorded white anchors")
        command = anchors[0][1]
        for (a,x),(b,y) in zip(anchors,anchors[1:]):
            if k <= b:
                fraction = (k-a)/(b-a)
                command = round(x+(y-x)*fraction) if native_ct else tuple(v+(w-v)*fraction for v,w in zip(x,y))
                break
        return KelvinTarget(1,1,1,level,command) if native_ct else LightTarget(*command,level)


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
        return LightTarget(*out, self.level(target.brightness))


PROFILE_FIELDS = ("name", "selector", "points", "brightness", "minimum")


def kelvin(value):
    if type(value) is not int or not 1700 <= value <= 6500:
        raise ValueError("Kelvin must be an integer from 1700 to 6500")
    return value


def supports_native_white(transport):
    return transport.get("kind") == "yeelight_tcp" and transport.get("ct_min_kelvin") is not None


def validate_raw(target, transport):
    if isinstance(target, KelvinTarget):
        if (not supports_native_white(transport) or
                not transport["ct_min_kelvin"] <= target.kelvin <= transport["ct_max_kelvin"]
                or round(target.brightness * 100) < 1):
            raise ValueError("native Kelvin target outside enrolled TCP capabilities")
    elif transport.get("white_kelvin") is not None:
        raise ValueError("white-only lamp needs an explicit Kelvin comparison target")
    return target



def compile_profiles(rows):
    result = {}
    for row in rows:
        value = {k:row[k] for k in PROFILE_FIELDS}
        if "white_points" in row:
            value["white_points"] = row["white_points"]
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
        if not isinstance(item, dict) or set(item) not in ({"rgb", "brightness"}, {"kelvin", "brightness"}):
            raise ValueError("invalid comparison target")
        level = unit(item["brightness"])
        result[device] = (KelvinTarget(1,1,1,level,kelvin(item["kelvin"])) if "kelvin" in item
                          else LightTarget(*rgb(item["rgb"]), level))
    return result
