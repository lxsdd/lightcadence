"""Strict owner-local physical candidate configuration; construction does no I/O."""
import json
import hashlib
from pathlib import Path

from .chroma import _unique_pairs
from .enrollment import ChromaProfile
from .runtime import PreviewRuntime, fields
from .recovery import RecoveryJournal
from .library import ProjectLibrary
from .yeelight_lan import LanBinding, compose_lan_candidate


def configured_runtime(env):
    runtime = _configured_runtime(env)
    directory = env.get("LIGHTCADENCE_LIBRARY_DIR")
    if directory is not None:
        try:
            runtime.library = ProjectLibrary(directory)
        except BaseException:
            journal = getattr(getattr(runtime, "physical", None), "journal", None)
            if journal is not None:
                journal.close()
            raise ValueError("invalid owner-local library directory") from None
    return runtime


def _configured_runtime(env):
    mode = env.get("LIGHTCADENCE_MODE", "preview")
    path = env.get("LIGHTCADENCE_DEVICE_FILE")
    journal_dir = env.get("LIGHTCADENCE_JOURNAL_DIR")
    if mode == "preview" and path is None and journal_dir is None:
        return PreviewRuntime()
    if mode != "physical_candidate" or not path or not journal_dir:
        raise ValueError("invalid deployment mode or missing explicit device file")
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_unique_pairs)
        fields(raw, ("schema_version", "owner_hardware_test", "devices"))
        if type(raw["schema_version"]) is not int or raw["schema_version"] != 1 or raw["owner_hardware_test"] is not True:
            raise ValueError
        if not isinstance(raw["devices"], list) or not raw["devices"]:
            raise ValueError
        bindings = []
        for item in raw["devices"]:
            fields(item, ("ha_registry_id", "model", "firmware", "host", "interval", "lan_id"),
                   ("chroma_port", "tcp_port", "discovery_port", "transport", "white_kelvin", "ct_min_kelvin", "ct_max_kelvin"))
            # One stable registry identity in the engine and HA, not an IP or entity_id.
            transport = item.get("transport", "chroma_v2")
            if transport == "yeelight_tcp" and "chroma_port" in item:
                raise ValueError("TCP profile must not configure a Chroma endpoint")
            port = item.get("tcp_port", 55443) if transport == "yeelight_tcp" else item.get("chroma_port", 55444)
            p = ChromaProfile(item["ha_registry_id"], item["ha_registry_id"], item["model"],
                              item["firmware"], item["host"], item["interval"], port, transport,
                              item.get("white_kelvin"), item.get("ct_min_kelvin"), item.get("ct_max_kelvin"))
            bindings.append(LanBinding(p, item["lan_id"], item.get("tcp_port", 55443),
                                       item.get("discovery_port", 1982)))
        # Stable device/registry, model, firmware and protocol identity, not IP.
        keys = {b.profile.device_id: hashlib.sha256(json.dumps(
            [b.profile.ha_registry_id, b.profile.model, b.profile.firmware, b.lan_id.lower()],
            separators=(",", ":")).encode()).hexdigest() for b in bindings}
        journal = RecoveryJournal(journal_dir, keys)
        try:
            return compose_lan_candidate(bindings, enabled=True, journal=journal)
        except BaseException:
            journal.close()
            raise
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        # Do not leak local endpoints or device identifiers through startup errors.
        raise ValueError("invalid owner-local physical candidate configuration") from None
