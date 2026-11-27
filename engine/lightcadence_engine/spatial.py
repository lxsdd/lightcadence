"""Meter-based floor geometry and deterministic projection onto effect paths."""
from math import hypot, isfinite

KINDS = {"room", "zone", "furniture", "wall", "opening", "lamp", "strip", "effect_path"}


def number(value):
    if type(value) not in (int, float) or not isfinite(value):
        raise ValueError("finite coordinate required")
    return value


def geometry(plan, zones):
    """Validate a single floor, without inferring walls from functional zones."""
    width, height = number(plan["width"]), number(plan["height"])
    if width <= 0 or height <= 0 or not isfinite(hypot(width, height)):
        raise ValueError("invalid floor dimensions")
    features = plan["features"]
    if not isinstance(features, list):
        raise ValueError("invalid features")
    ids, devices = set(), set()
    for item in features:
        if not isinstance(item, dict) or set(item) != {"id", "kind", "name", "points", "device_id", "zone_id"}:
            raise ValueError("invalid feature fields")
        key, kind = item["id"], item["kind"]
        if (not isinstance(key, str) or not key.strip() or key in ids or
                not isinstance(kind, str) or kind not in KINDS or
                not isinstance(item["name"], str) or not item["name"].strip()):
            raise ValueError("invalid feature identity")
        ids.add(key)
        points = item["points"]
        if not isinstance(points, list):
            raise ValueError("invalid vertices")
        for point in points:
            if not isinstance(point, list) or len(point) != 2:
                raise ValueError("invalid point")
            x, y = map(number, point)
            if not (0 <= x <= width and 0 <= y <= height):
                raise ValueError("point outside floor")
        minimum = 3 if kind in ("room", "zone", "furniture") else 2
        if kind == "lamp":
            if len(points) != 1:
                raise ValueError("point lamp needs one point")
        elif len(points) < minimum or any(a == b for a, b in zip(points, points[1:])):
            raise ValueError("invalid path or polygon")
        if kind in ("room", "zone", "furniture") and (points[0] == points[-1] or len({tuple(p) for p in points}) < 3):
            raise ValueError("invalid polygon vertices")
        if kind in ("lamp", "strip"):
            device = item["device_id"]
            if not isinstance(device, str) or not device.strip() or device in devices:
                raise ValueError("unique registry device per floor required")
            devices.add(device)
        elif item["device_id"] is not None:
            raise ValueError("unexpected device reference")
        if kind in ("zone", "effect_path"):
            if not isinstance(item["zone_id"], str) or item["zone_id"] not in zones:
                raise ValueError("unknown zone reference")
        elif item["zone_id"] is not None:
            raise ValueError("unexpected zone reference")
        if kind in ("strip", "effect_path"):
            segments(points)


def segments(points):
    result = []
    total = 0.
    for a, b in zip(points, points[1:]):
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = hypot(dx, dy)
        if length <= 0 or not isfinite(total + length):
            raise ValueError("invalid path length")
        result.append((a, dx / length, dy / length, length, total))
        total += length
    return result, total


def midpoint(points):
    parts, total = segments(points)
    for a, ux, uy, length, offset in parts:
        if offset + length >= total / 2:
            delta = total / 2 - offset
            return (a[0] + ux * delta, a[1] + uy * delta)
    return points[-1]


def project(point, parts, total):
    best_distance, best_offset = float("inf"), 0.
    for a, ux, uy, length, offset in parts:
        dx, dy = point[0] - a[0], point[1] - a[1]
        along = min(length, max(0., dx * ux + dy * uy))
        distance = hypot(dx - along * ux, dy - along * uy)
        if distance < best_distance:
            best_distance, best_offset = distance, offset + along
    return min(1., max(0., best_offset / total))


def spatial_paths(plans, zones):
    """Authoritative normalized arc-length positions; never invent missing lamps."""
    members = {z["id"]: z["devices"] for z in zones}
    output = []
    for plan in plans:
        locations = {f["device_id"]: (f["points"][0] if f["kind"] == "lamp" else midpoint(f["points"]))
                     for f in plan["features"] if f["kind"] in ("lamp", "strip")}
        for path in plan["features"]:
            if path["kind"] != "effect_path":
                continue
            devices = members[path["zone_id"]]
            missing = [d for d in devices if d not in locations]
            parts, total = segments(path["points"])
            output.append({"floorplan_id": plan["id"], "path_id": path["id"],
                           "name": plan["name"] + " / " + path["name"], "zone_id": path["zone_id"],
                           "missing": missing, "positions": None if missing else
                           {d: project(locations[d], parts, total) for d in devices}})
    return output
