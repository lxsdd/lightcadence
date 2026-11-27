"""Prepare and verify a local preview build; offline HA code activation only.

No service control, hardware I/O, secret provisioning or publication.
Python 3.12+, Git, Node/npm on the preparation host.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import uuid

MANIFEST = "build-manifest.json"
INTEGRATION = Path("custom_components/lightcadence")
ROOT_FILES = (".dockerignore", "Dockerfile", "compose.yaml", "compose.library.yaml")
REQUIRED = {*ROOT_FILES, "frontend-lock.json", "engine/requirements.txt",
            "engine/lightcadence_engine/server.py", str(INTEGRATION / "manifest.json"),
            str(INTEGRATION / "frontend/panel.js")}
SHA = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def inventory(root, *, ignore_manifest=False, ignore_cache=False):
    if root.is_symlink() or not root.is_dir():
        raise ValueError("expected ordinary directory")
    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        parent = Path(directory)
        for name in dirs + files:
            if (parent / name).is_symlink():
                raise ValueError("symlinks are not supported in local builds")
        if ignore_cache:
            dirs[:] = [name for name in dirs if name != "__pycache__"]
        for name in files:
            path = parent / name
            rel = path.relative_to(root).as_posix()
            if ignore_manifest and rel == MANIFEST:
                continue
            if not path.is_file():
                raise ValueError("unexpected nonregular file")
            result[rel] = digest(path)
    return result


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate manifest field")
        result[key] = value
    return result


def valid_inventory(files):
    return (isinstance(files, dict) and REQUIRED <= files.keys()
            and all(isinstance(value, str) and DIGEST.fullmatch(value)
                    and (name in ROOT_FILES or name == "frontend-lock.json"
                         or name.startswith("engine/")
                         or name.startswith(str(INTEGRATION) + "/"))
                    for name, value in files.items()))


def seal(root, sha, node, npm):
    files = inventory(root)
    if not SHA.fullmatch(sha) or not valid_inventory(files):
        raise ValueError("invalid local build layout")
    doc = {"schema_version": 1, "mode": "preview", "source_sha": sha,
           "node": node, "npm": npm, "files": files}
    (root / MANIFEST).write_text(json.dumps(doc, sort_keys=True, indent=2) + "\n",
                                 encoding="utf-8")
    return verify(root)


def verify(root):
    root = Path(root)
    if root.is_symlink() or (root / MANIFEST).is_symlink():
        raise ValueError("symlinked build")
    doc = json.loads((root / MANIFEST).read_text(encoding="utf-8"),
                     object_pairs_hook=unique_pairs)
    if (not isinstance(doc, dict)
            or set(doc) != {"schema_version", "mode", "source_sha", "node", "npm", "files"}
            or type(doc["schema_version"]) is not int or doc["schema_version"] != 1
            or doc["mode"] != "preview" or not isinstance(doc["source_sha"], str)
            or not SHA.fullmatch(doc["source_sha"])
            or not all(isinstance(doc[k], str) and doc[k] for k in ("node", "npm"))
            or not valid_inventory(doc["files"])
            or inventory(root, ignore_manifest=True) != doc["files"]):
        raise ValueError("local build manifest or file integrity mismatch")
    return doc


def output(args, cwd):
    return subprocess.check_output(args, cwd=cwd, text=True).strip()


def prepare(repo, sha, destination):
    repo = Path(repo).resolve(strict=True)
    destination = Path(destination).absolute()
    if not SHA.fullmatch(sha) or output(["git", "rev-parse", sha + "^{commit}"], repo) != sha:
        raise ValueError("use the exact full source commit SHA")
    parent = destination.parent.resolve(strict=True)
    destination = parent / destination.name
    if destination.is_relative_to(repo):
        raise ValueError("build destination must be outside the source checkout")
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("build destination already exists")
    with tempfile.TemporaryDirectory(prefix=".lightcadence-build-", dir=parent) as work:
        work = Path(work)
        source = work / "source"
        source.mkdir()
        with (work / "source.tar").open("wb") as archive:
            subprocess.run(["git", "archive", "--format=tar", sha], cwd=repo,
                           stdout=archive, check=True)
        with tarfile.open(work / "source.tar") as archive:
            if any(not (item.isfile() or item.isdir()) for item in archive.getmembers()):
                raise ValueError("source archive contains unsupported entries")
            archive.extractall(source, filter="data")
        frontend = source / "frontend"
        node, npm = output(["node", "--version"], frontend), output(["npm", "--version"], frontend)
        if (frontend / "package-lock.json").is_file():
            subprocess.run(["npm", "ci", "--no-audit", "--no-fund"], cwd=frontend, check=True)
        else:
            subprocess.run(["npm", "install", "--no-audit", "--no-fund", "--package-lock=true"],
                           cwd=frontend, check=True)
        subprocess.run(["npm", "run", "build"], cwd=frontend, check=True)
        stage = work / "payload"
        stage.mkdir()
        for folder in (Path("engine"), INTEGRATION):
            shutil.copytree(source / folder, stage / folder)
        for filename in ROOT_FILES:
            shutil.copyfile(source / filename, stage / filename)
        shutil.copyfile(frontend / "package-lock.json", stage / "frontend-lock.json")
        doc = seal(stage, sha, node, npm)
        # Exclusive reservation; never overwrite an earlier local build.
        destination.mkdir()
        try:
            os.rename(stage, destination)
        except BaseException:
            destination.rmdir()
            raise
    return doc


def activate(bundle, config, *, ha_stopped=False, previous=None, backup_dir=None):
    """Replace only the HA integration code with an explicit offline consent.

    Previous bundle must exactly match installed source (runtime pycache ignored).
    Keep old code on the same filesystem; no library, journal or HA storage writes.
    """
    if ha_stopped is not True:
        raise ValueError("stop HA and explicitly confirm offline activation")
    bundle = Path(bundle).resolve(strict=True)
    doc = verify(bundle)
    config = Path(config).resolve(strict=True)
    components = config / "custom_components"
    if components.is_symlink() or not components.is_dir():
        raise ValueError("existing ordinary custom_components directory required")
    target = components / "lightcadence"
    if bundle.is_relative_to(target) or target.is_relative_to(bundle):
        raise ValueError("build and installation must be separate")
    new_files = inventory(bundle / INTEGRATION)
    backup = None
    if target.exists() or target.is_symlink():
        current = inventory(target, ignore_cache=True)
        if current == new_files:
            return {"source_sha": doc["source_sha"], "changed": False, "backup": None}
        if previous is None or backup_dir is None:
            raise ValueError("update requires verified previous build and backup directory")
        previous = Path(previous).resolve(strict=True)
        verify(previous)
        if current != inventory(previous / INTEGRATION):
            raise ValueError("installed code does not match previous verified build")
        backups = Path(backup_dir).resolve(strict=True)
        if (not backups.is_dir() or backups.is_relative_to(components)
                or backups.is_relative_to(bundle) or backups.is_relative_to(previous)
                or backups.stat().st_dev != components.stat().st_dev):
            raise ValueError("backup directory must be separate, on the same filesystem")
        backup = backups / ("lightcadence-" + uuid.uuid4().hex)
    elif previous is not None:
        raise ValueError("previous build specified but no installed code")
    # Copy to an adjacent temporary directory before moving existing code.
    with tempfile.TemporaryDirectory(prefix=".lightcadence-stage-", dir=components) as tmp:
        staged = Path(tmp) / "lightcadence"
        shutil.copytree(bundle / INTEGRATION, staged)
        if inventory(staged) != new_files:
            raise ValueError("staged code integrity mismatch")
        if backup is not None:
            os.rename(target, backup)
        try:
            os.rename(staged, target)
        except BaseException:
            if backup is not None and not target.exists() and not target.is_symlink():
                os.rename(backup, target)
            raise
    return {"source_sha": doc["source_sha"], "changed": True,
            "backup": str(backup) if backup is not None else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare")
    prep.add_argument("--repo", type=Path, default=Path.cwd())
    prep.add_argument("--sha", required=True)
    prep.add_argument("--destination", type=Path, required=True)
    check = commands.add_parser("verify")
    check.add_argument("bundle", type=Path)
    install = commands.add_parser("activate-ha")
    install.add_argument("bundle", type=Path)
    install.add_argument("--config", type=Path, required=True)
    install.add_argument("--ha-stopped", action="store_true")
    install.add_argument("--previous", type=Path)
    install.add_argument("--backup-dir", type=Path)
    args = parser.parse_args()
    if args.command == "prepare":
        result = prepare(args.repo, args.sha, args.destination)
    elif args.command == "verify":
        result = verify(args.bundle)
    else:
        result = activate(args.bundle, args.config, ha_stopped=args.ha_stopped,
                          previous=args.previous, backup_dir=args.backup_dir)
    # Never echo file contents or owner configuration.
    print(json.dumps({key: value for key, value in result.items() if key != "files"},
                     sort_keys=True))


if __name__ == "__main__":
    main()
