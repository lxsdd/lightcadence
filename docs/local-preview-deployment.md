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

## Explicit local hardware-test preparation (no activation)

Prepare with the same full source SHA and `--hardware-candidate` in a separate
staging directory. The sealed mode is `hardware_candidate` and the manifest
requires the exact hardware overlay. The regular preview remains unchanged.
Verification, offline activation and previous-code backup rules remain the
same. No profile, journal, token, library, or actual local configuration is
copied into the bundle. Only with separate owner approval, provide the
existing owner-local token, verified enrolled device JSON and distinct
persistent recovery and library directories readable/writable as appropriate
by UID 10001, then use the three explicit Compose files (base, hardware-test,
library). HA configuration must select physical candidate mode and the same
loopback port/token. The preparation and CI do not start this stack or send
hardware commands. Stop/restore and inspect recovery before shutdown or
rollback. See hardware-test-boundary.md for enrollment requirements. The
calibration assistant supports enrolled RGB lamps and native Kelvin comparison
on explicitly enrolled Yeelight TCP devices with verified CT ranges.

## Read-only check before physical calibration

From the verified hardware build directory, with the four owner-local Compose
paths already set and the matching local image built, run as the configured
container UID:

```sh
docker compose -f compose.yaml -f compose.hardware-test.yaml -f compose.library.yaml \
  run --rm --no-deps --pull never lightcadence \
  python -m engine.lightcadence_engine.server --check-config
```

This uses the production profile constructors, token and port validation, and
the original recovery/library readers. It opens no device sockets or HTTP
listener, takes no storage locks, and creates no journal/library files. It
reports only mode, counts, library persistence and hardware_qualified=false;
invalid input returns a nonzero exit with no secret, identifier or raw error.
The persistence paths must exist and be accessible to UID 10001. Provision
them separately before this check; Compose itself may create missing bind
source directories, so do not invoke it until those paths exist. A nonzero
recovery count requires inspection before an upgrade or new comparison; never
delete the recovery journal. An unmatched count indicates changed identity
binding. A persistent library and at least one native Kelvin device are
necessary for the mixed Kelvin calibration workflow, not for all RGB shows.

This advisory check does not acquire exclusive locks, prove actual write/fsync
permission, test network reachability, resolve HA entity identity, or establish
hardware support. It cannot guarantee that config files remain unchanged after
checking. The normal runtime still validates the configuration and takes its
own exclusive locks before any physical output. No installation or lamp
operation is authorized by a successful check.
