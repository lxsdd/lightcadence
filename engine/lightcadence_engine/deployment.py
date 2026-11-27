"""Strict owner-local physical candidate configuration; construction does no I/O."""
import json
import hashlib
import os
from pathlib import Path

from .chroma import _unique_pairs
from .enrollment import ChromaProfile
from .runtime import PreviewRuntime, fields
from .recovery import RecoveryJournal
from .library import ProjectLibrary
from .yeelight_lan import LanBinding, compose_lan_candidate
from .tuya_lan import TuyaBinding, compose_mixed_candidate


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
        bindings, tuya, keys = read_bindings(path)
        journal = RecoveryJournal(journal_dir, keys)
        try:
            return compose_candidate(bindings, tuya, journal)
        except BaseException:
            journal.close()
            raise
    except (OSError, ValueError, TypeError, OverflowError, RecursionError):
        # Do not leak local endpoints or device identifiers through startup errors.
        raise ValueError("invalid owner-local physical candidate configuration") from None


def read_bindings(path):
    """Parse exactly the same owner-local profile contract for startup and dry checks."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_unique_pairs)
    fields(raw, ("schema_version", "owner_hardware_test", "devices"))
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1 or raw["owner_hardware_test"] is not True:
        raise ValueError
    if not isinstance(raw["devices"], list) or not raw["devices"]:
        raise ValueError
    bindings = []
    tuya = []
    for item in raw["devices"]:
        fields(item, ("ha_registry_id", "model", "firmware", "host", "interval", "lan_id"),
               ("chroma_port", "tcp_port", "discovery_port", "transport", "white_kelvin", "ct_min_kelvin", "ct_max_kelvin", "tuya"))
        # One stable registry identity in the engine and HA, not an IP or entity_id.
        transport = item.get("transport", "chroma_v2")
        if transport == "tuya_v35":
            if set(item) & {"chroma_port", "discovery_port", "white_kelvin", "ct_min_kelvin", "ct_max_kelvin"}:
                raise ValueError("Tuya profile must not configure Yeelight endpoints or CT")
            settings = fields(item.get("tuya"), ("local_key", "power_dp", "mode_dp", "colour_dp"))
            p = ChromaProfile(item["ha_registry_id"], item["ha_registry_id"], item["model"],
                              item["firmware"], item["host"], item["interval"],
                              item.get("tcp_port", 6668), transport)
            tuya.append(TuyaBinding(p, item["lan_id"], settings["local_key"],
                                    settings["power_dp"], settings["mode_dp"], settings["colour_dp"]))
            continue
        if "tuya" in item:
            raise ValueError("Tuya settings require explicit Tuya profile")
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
    for b in tuya:
        if b.profile.device_id in keys:
            raise ValueError("duplicate stable binding")
        keys[b.profile.device_id] = hashlib.sha256(json.dumps(
            [b.profile.ha_registry_id, b.profile.model, b.profile.firmware, "tuya_v35", b.lan_id,
             b.power_dp, b.mode_dp, b.colour_dp], separators=(",", ":")).encode()).hexdigest()
    return bindings, tuya, keys


def compose_candidate(bindings, tuya, journal=None):
    if tuya:
        return compose_mixed_candidate(bindings, tuya, enabled=True, journal=journal)
    return compose_lan_candidate(bindings, enabled=True, journal=journal)


def readable_file(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("ordinary local file required")
    return path


def check_directory(value, lock_name):
    directory = Path(value)
    if directory.is_symlink() or not directory.is_dir() or not os.access(directory, os.R_OK | os.W_OK | os.X_OK):
        raise ValueError("existing accessible persistence directory required")
    lock = directory / lock_name
    if lock.exists() or lock.is_symlink():
        readable_file(lock)
        if not os.access(lock, os.R_OK | os.W_OK):
            raise ValueError("inaccessible persistence lock file")
    return directory


def check_local_configuration(env):
    """Read-only advisory check; no locks, runtime startup, journal creation or I/O to lamps."""
    mode = env.get("LIGHTCADENCE_MODE", "preview")
    path = env.get("LIGHTCADENCE_DEVICE_FILE")
    journal_dir = env.get("LIGHTCADENCE_JOURNAL_DIR")
    library_dir = env.get("LIGHTCADENCE_LIBRARY_DIR")
    if mode not in ("preview", "physical_candidate"):
        raise ValueError("invalid mode")
    library = None
    if library_dir is not None:
        library = check_directory(library_dir, "library.lock")
        ProjectLibrary.read_document(library / "library-v1.json")
    result = {"mode": mode, "persistent_library": library is not None,
              "devices": 0, "native_kelvin_devices": 0, "recovery_records": 0,
              "unmatched_recovery_records": 0, "hardware_qualified": False}
    if mode == "preview":
        if path is not None or journal_dir is not None:
            raise ValueError("preview must not contain physical configuration")
        return result
    if not path or not journal_dir:
        raise ValueError("explicit device file and journal required")
    bindings, tuya, keys = read_bindings(readable_file(path))
    # The original production constructors perform cross-profile uniqueness and
    # endpoint validation. With no journal they allocate only in-memory state.
    compose_candidate(bindings, tuya)
    journal = check_directory(journal_dir, "recovery.lock")
    if library is not None and os.path.samefile(journal, library):
        raise ValueError("journal and library must be separate directories")
    records = RecoveryJournal.read_records(journal / "recovery-v1.json")
    result.update(devices=len(keys), native_kelvin_devices=sum(
        b.profile.transport == "yeelight_tcp" and b.profile.ct_min_kelvin is not None for b in bindings),
        recovery_records=len(records), unmatched_recovery_records=sum(
            keys.get(device) != record["binding"] for device, record in records.items()))
    return result
