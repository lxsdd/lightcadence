"""Resolve a saved scene against one immutable library revision, not client copies."""
from copy import deepcopy
from uuid import uuid4

from .effects import decode_effect
from .sessions import StaleRevision, StopPolicy


class SceneLaunches:
    """Session provenance and same-instance replay fence for saved-scene launches."""
    def __init__(self):
        self.requests = {}
        self.origins = {}

    def prepare(self, body, library):
        required = {"op", "request_id", "scene_id", "library_revision", "devices"}
        optional = {"audio_source", "floorplan_id", "path_id"}
        if not isinstance(body, dict) or not required <= body.keys() or not body.keys() <= required | optional:
            raise ValueError("invalid saved-scene launch fields")
        request = body["request_id"]
        if not isinstance(request, str) or not request.strip():
            raise ValueError("invalid request identity")
        previous = self.requests.get(request)
        if previous is not None:
            if previous[0] != body or previous[1] is None:
                raise ValueError("saved-scene request conflicts or outcome pending")
            return previous[1], None
        if library.fault:
            raise OSError("library unavailable")
        if type(body["library_revision"]) is not int or body["library_revision"] != library.data["revision"]:
            raise StaleRevision("refresh library")
        snapshot = library.snapshot()
        scene = next((s for s in snapshot["scenes"] if s["id"] == body["scene_id"]), None)
        if scene is None:
            raise ValueError("unknown saved scene")
        zone = next(z for z in snapshot["zones"] if z["id"] == scene["zone_id"])
        # HA must validate exactly the ordered registry selection it observed.
        if body["devices"] != zone["devices"]:
            raise StaleRevision("zone membership changed")
        positions = None
        if ("floorplan_id" in body) != ("path_id" in body):
            raise ValueError("complete path reference required")
        if "path_id" in body:
            path = next((p for p in snapshot["spatial_paths"] if
                         p["floorplan_id"] == body["floorplan_id"] and p["path_id"] == body["path_id"]
                         and p["zone_id"] == zone["id"]), None)
            if path is None or path["positions"] is None:
                raise ValueError("missing or incomplete spatial path")
            positions = path["positions"]
        params = deepcopy(scene["effect"])
        template = decode_effect(params, template=True)
        if template.audio_source is not None:
            source = body.get("audio_source")
            if not isinstance(source, str) or not source.strip():
                raise ValueError("explicit audio source required")
            params["audio_source"] = source
        elif "audio_source" in body:
            raise ValueError("audio source only allowed for audio scenes")
        effect = decode_effect(params)
        effect.validate_targets(zone["devices"])
        origin = {"scene_id": scene["id"], "scene_revision": scene["revision"], "scene_name": scene["name"],
                  "zone_revision": zone["revision"], "library_revision": snapshot["revision"],
                  "floorplan_id": body.get("floorplan_id"), "path_id": body.get("path_id")}
        return None, (str(uuid4()), zone["id"], zone["devices"], effect, StopPolicy(scene["stop_policy"]), positions, origin)

    def begin(self, body):
        self.requests[body["request_id"]] = (deepcopy(body), None)

    def finish(self, body, session, origin):
        self.origins[session.id] = origin
        self.requests[body["request_id"]] = (deepcopy(body), session.id)
