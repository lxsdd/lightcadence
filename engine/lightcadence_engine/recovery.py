"""Local durable recovery intent; never replays device commands at startup."""
import fcntl
import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from .chroma import _unique_pairs
from .effects import LightTarget
from .sessions import DeviceState, StopPolicy


class RecoveryJournal:
    """One process per local journal. Sync only at ownership transitions, not frames.

    A failed durable update latches a fault until restart. A crash may leave
    conservative extra recovery entries, never a reason to blindly restore.
    """
    def __init__(self, directory, binding_keys):
        self.directory = Path(directory)
        self.path = self.directory / "recovery-v1.json"
        self.keys = dict(binding_keys)
        self.fault = False
        self._lock = None
        try:
            self._lock = os.open(self.directory / "recovery.lock",
                                 os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self.path.is_symlink():
                raise ValueError("invalid recovery journal")
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding="utf-8"), object_pairs_hook=_unique_pairs)
                if not isinstance(raw, dict) or set(raw) != {"schema_version", "records"} or type(raw["schema_version"]) is not int or raw["schema_version"] != 1 or not isinstance(raw["records"], dict):
                    raise ValueError("invalid recovery journal")
                self.records = raw["records"]
                for device, record in self.records.items():
                    self._validate(device, record)
            else:
                self.records = {}
                self._save({})
            self.recovery = set(self.records)
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _validate(device, record):
        if (not isinstance(device, str) or not device or not isinstance(record, dict)
                or set(record) != {"id", "binding", "original", "policy"}
                or not isinstance(record["id"], str) or not record["id"]
                or not isinstance(record["binding"], str) or not record["binding"]):
            raise ValueError("invalid recovery record")
        StopPolicy(record["policy"])
        state = record["original"]
        if not isinstance(state, dict) or set(state) not in ({"power", "target"}, {"power", "target", "kelvin"}) or not isinstance(state["target"], dict):
            raise ValueError("invalid original state")
        return DeviceState(state["power"], LightTarget(**state["target"]), state.get("kelvin"))

    def require_healthy(self):
        if self.fault or self._lock is None:
            raise OSError("recovery journal unavailable")

    def _save(self, records):
        self.require_healthy()
        name = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.directory,
                                             prefix=".recovery-", delete=False) as stream:
                name = stream.name
                json.dump({"schema_version": 1, "records": records}, stream,
                          allow_nan=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except BaseException:
            self.fault = True
            raise
        finally:
            if name is not None:
                try:
                    os.unlink(name)
                except FileNotFoundError:
                    pass
        self.records = records

    def add(self, originals, policy):
        self.require_healthy()
        if set(originals) & self.records.keys():
            raise ValueError("device already recorded")
        records = dict(self.records)
        for device, state in originals.items():
            original = asdict(state)
            if state.kelvin is None:
                original.pop("kelvin")
            records[device] = {"id": uuid4().hex, "binding": self.keys[device],
                               "original": original, "policy": policy.value}
        self._save(records)

    def remove(self, devices):
        self.require_healthy()
        devices = set(devices) & self.records.keys()
        if devices:
            self._save({d: r for d, r in self.records.items() if d not in devices})
            self.recovery.difference_update(devices)

    def prepare_recovery(self, device, record_id):
        self.require_healthy()
        record = self.records.get(device)
        if (device not in self.recovery or record is None or record["id"] != record_id
                or self.keys.get(device) != record["binding"]):
            raise ValueError("recovery identity or revision mismatch")
        state = self._validate(device, record)
        # Invalidate the old action before any write. Uncertain outcomes require
        # a fresh explicit decision rather than automatic same-ID replay.
        self._save({**self.records, device: {**record, "id": uuid4().hex}})
        return state

    def snapshot(self):
        return [{"device_id": d, "record_id": self.records[d]["id"],
                 "original": self.records[d]["original"], "policy": self.records[d]["policy"],
                 "binding_matches": self.keys.get(d) == self.records[d]["binding"]}
                for d in sorted(self.recovery)]

    def close(self):
        if self._lock is not None:
            os.close(self._lock)
            self._lock = None
