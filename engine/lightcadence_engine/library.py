"""One engine-owned versioned zone/scene library. No device or HA writes."""
from copy import deepcopy
from datetime import datetime, timezone
from dataclasses import asdict
import fcntl
import json
import os
from pathlib import Path
import tempfile
from uuid import UUID, uuid4

from .chroma import _unique_pairs
from .effects import Effect, EffectKind
from .sessions import StaleRevision, StopPolicy
from .spatial import geometry, spatial_paths


def exact(obj, keys):
    if not isinstance(obj, dict) or set(obj) != set(keys):
        raise ValueError("invalid library fields")


def name(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("nonempty name required")
    return value.strip()


def members(value):
    if (not isinstance(value, list) or not value or
            not all(isinstance(d, str) and d.strip() for d in value) or
            len(set(value)) != len(value)):
        raise ValueError("unique ordered devices required")
    return list(value)


def effect(value):
    exact(value, ("kind", "period_seconds", "brightness", "color", "width"))
    if not isinstance(value["color"], (list, tuple)):
        raise ValueError("invalid color")
    params = {**value, "kind": EffectKind(value["kind"]), "color": tuple(value["color"])}
    # Audio source IDs are ephemeral. Choose a live source explicitly at launch.
    spec = Effect(**params, audio_source="selection-required" if params["kind"] is EffectKind.AUDIO_SPECTRUM else None)
    data = asdict(spec)
    data.pop("audio_source")
    data["kind"] = spec.kind.value
    data["color"] = list(spec.color)
    return data


class ProjectLibrary:
    """Revisioned whole-document CAS, stable IDs and atomic owner-local persistence."""
    def __init__(self, directory=None):
        self.directory = Path(directory) if directory is not None else None
        self.lock = None
        self.fault = False
        self.data = {"schema_version": 1, "revision": 0, "zones": [], "scenes": [], "floorplans": []}
        if self.directory is not None:
            try:
                self.lock = os.open(self.directory / "library.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.path = self.directory / "library-v1.json"
                if self.path.is_symlink():
                    raise ValueError("invalid library path")
                if self.path.exists():
                    self.data = json.loads(self.path.read_text(encoding="utf-8"), object_pairs_hook=_unique_pairs)
                    # Additive v1 extension: only the exact old document shape is migrated.
                    if isinstance(self.data, dict) and set(self.data) == {"schema_version", "revision", "zones", "scenes"}:
                        self.data["floorplans"] = []
                    self.validate(self.data)
            except BaseException:
                self.close()
                raise

    @staticmethod
    def validate(doc):
        exact(doc, ("schema_version", "revision", "zones", "scenes", "floorplans"))
        if type(doc["schema_version"]) is not int or doc["schema_version"] != 1 or type(doc["revision"]) is not int or doc["revision"] < 0:
            raise ValueError("invalid library version")
        ids = set()
        zones = set()
        for kind in ("zones", "scenes", "floorplans"):
            if not isinstance(doc[kind], list):
                raise ValueError("invalid library collection")
            for obj in doc[kind]:
                keys = ("schema_version", "id", "revision", "updated_at", "name")
                exact(obj, keys + (("devices",) if kind == "zones" else ("zone_id", "effect", "stop_policy") if kind == "scenes" else ("width", "height", "features")))
                if (type(obj["schema_version"]) is not int or obj["schema_version"] != 1 or
                        type(obj["revision"]) is not int or obj["revision"] < 1 or
                        not isinstance(obj["id"], str) or str(UUID(obj["id"])) != obj["id"] or obj["id"] in ids):
                    raise ValueError("invalid library identity")
                ids.add(obj["id"])
                name(obj["name"])
                if datetime.fromisoformat(obj["updated_at"]).utcoffset() is None:
                    raise ValueError("timestamp must be timezone aware")
                if kind == "zones":
                    members(obj["devices"])
                    zones.add(obj["id"])
                elif kind == "scenes":
                    if obj["zone_id"] not in zones:
                        raise ValueError("unknown zone reference")
                    effect(obj["effect"])
                    StopPolicy(obj["stop_policy"])
                else:
                    geometry(obj, zones)

    def snapshot(self):
        return {**deepcopy(self.data), "persistent": self.directory is not None, "fault": self.fault,
                "spatial_paths": spatial_paths(self.data["floorplans"], self.data["zones"])}

    def command(self, body):
        exact(body, ("op", "revision", "kind", "id", "value"))
        if self.fault or (self.directory is not None and self.lock is None):
            raise OSError("library unavailable")
        if type(body["revision"]) is not int or body["revision"] != self.data["revision"]:
            raise StaleRevision("refresh library")
        kind = body["kind"]
        if kind not in ("zones", "scenes", "floorplans"):
            raise ValueError("unknown library collection")
        doc = deepcopy(self.data)
        rows = doc[kind]
        key = body["id"]
        old = next((row for row in rows if row["id"] == key), None)
        if key is not None and old is None:
            raise ValueError("unknown library object")
        if body["op"] == "library_delete":
            if old is None or body["value"] is not None:
                raise ValueError("invalid deletion")
            rows.remove(old)
        elif body["op"] == "library_save":
            value = body["value"]
            if kind == "zones":
                exact(value, ("name", "devices"))
                item = {"name": name(value["name"]), "devices": members(value["devices"])}
            elif kind == "scenes":
                exact(value, ("name", "zone_id", "effect", "stop_policy"))
                item = {"name": name(value["name"]), "zone_id": value["zone_id"],
                        "effect": effect(value["effect"]), "stop_policy": StopPolicy(value["stop_policy"]).value}
            else:
                exact(value, ("name", "width", "height", "features"))
                item = deepcopy(value)
                item["name"] = name(item["name"])
            item.update(schema_version=1, id=key or str(uuid4()), revision=1 if old is None else old["revision"] + 1,
                        updated_at=datetime.now(timezone.utc).isoformat())
            if old is None:
                rows.append(item)
            else:
                rows[rows.index(old)] = item
        else:
            raise ValueError("unknown library operation")
        doc["revision"] += 1
        self.validate(doc)
        if self.directory is not None:
            self._save(doc)
        self.data = doc

    def _save(self, doc):
        temp = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.directory, prefix=".library-", delete=False) as f:
                temp = f.name
                json.dump(doc, f, allow_nan=False, separators=(",", ":"))
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, self.path)
            fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except BaseException:
            self.fault = True
            raise
        finally:
            if temp is not None:
                try:
                    os.unlink(temp)
                except FileNotFoundError:
                    pass

    def close(self):
        if self.lock is not None:
            os.close(self.lock)
            self.lock = None
