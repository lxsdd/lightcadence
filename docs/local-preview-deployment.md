# Owner-local preview preparation and offline HA code switch

This is a development tool, not a release, installation candidate, or hardware
qualification. It prepares an explicitly selected full Git commit locally.
No packages/images are published and no service is started by the tool.

## Prepare and verify

On a separate build workspace with Python 3.12+, Git, Node/npm and Docker
Compose v2 available, after owner approval for the local build:

```sh
python3 deployment/prepare_preview.py prepare --repo . --sha FULL_COMMIT_SHA --destination /absolute/local/staging/new-build
python3 deployment/prepare_preview.py verify /absolute/local/staging/new-build
```

The destination must not exist and must be outside the checkout. The tool
builds the original Lit panel from a Git archive of exactly that commit,
not local uncommitted files. The sealed directory contains only the engine,
HA integration including its built panel, preview Compose and optional
library overlay, Dockerfile, Docker ignore file and generated frontend lock.
The SHA-256 manifest covers the actual prepared bytes, identifies the source
commit and records Node/npm versions. It is local integrity checking, not
a signature, provenance attestation or proof of bit-reproducible builds.
Until a source lockfile and wheel hashes are committed, dependency resolution
can differ between preparation environments; retain the actual prepared
directory and local image for a rollback rather than rebuilding it.

## Deployment boundary and preview-only local operation

Use the normal Compose file from the verified directory. Create a distinct
owner-local token file, readable by UID 10001, outside the source/staging
directory. Set LIGHTCADENCE_TOKEN_PATH to its absolute path. Do not put a
token in Git, CLI arguments, CI or the browser. The optional library overlay
uses a separately provisioned owner-local library directory owned by UID
10001. It is not included in the code build. Keep the engine port loopback-only.
With owner approval, build the image locally from this exact directory using
Docker Compose; record and retain the image ID. Use the default preview mode
and a matching HA preview config entry. The hardware overlay is intentionally
absent from the staged preview build. No hardware tests are enabled.

## Offline HA integration code activation and rollback

This command only installs integration code. It does not stop HA, change
its config entries, start an engine, modify devices, or touch HA storage.
Actual use requires separate owner permission. Stop all active sessions
and resolve pending restoration/recovery before stopping the engine and HA.
Back up HA configuration, LightCadence library and journal independently.
Use an existing HA custom_components directory. With HA stopped:

```sh
python3 deployment/prepare_preview.py activate-ha /absolute/local/staging/new-build --config /absolute/ha-config --ha-stopped
```

For an upgrade specify the retained, verified previous build and a separate,
existing backup directory on the same filesystem:

```sh
python3 deployment/prepare_preview.py activate-ha /absolute/local/staging/new-build --config /absolute/ha-config --ha-stopped --previous /absolute/local/staging/old-build --backup-dir /absolute/local/code-backups
```

The existing integration's bytes must match the previous manifest (runtime
Python cache is ignored). Unknown local edits, missing/modified payloads and
symlinks are refused. The old code is renamed to a unique backup, then the
prepared integration directory is renamed into place. A failed second rename
attempts to restore the previous directory; a process/power interruption
between renames may require manual recovery from the retained backup.
There is no automatic HA restart or health-check-triggered rollback.

For code rollback, stop HA and engine again, verify both retained builds,
and call activate-ha with old-build as target and new-build as previous.
Retain both local engine images and switch the engine back to the matching
old image/configuration manually. Inspect library/journal schema compatibility
before any downgrade; this tool never rewrites or resets those data files.
HA version compatibility, real OMV install, image rollback, data migration
and crash durability must still be accepted. Never use a code rollback as a
substitute for a pending physical-device recovery.
