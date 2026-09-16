# Update boundary audit and verified preflight correction

Scope: read-only runtime release discovery, with an explicit audit of the missing
native orchestration boundary. No installer, service, Enterprise instance or
production account was changed. No new updater was introduced or gate opened.

## What exists, and why wiring is not a small safe edit

| Layer | Observed implementation | Missing boundary |
| --- | --- | --- |
| Native desktop | `desktop/src-tauri/src/update/orchestrator.rs` sequences injected `UpdatePorts`; `plan.rs` computes the plan | No production `UpdatePorts` implementation or non-test `run_update` caller. `update/mod.rs` references a `bootstrap_service.rs` file that is absent. |
| Native capability | `update/availability.rs` and `desktop/src/native-updater.ts` report `integration_missing` | This is honest capability metadata, not an active update check or installation command. |
| Relaunch | `apply_app_and_relaunch` is documented as not returning in production | The pure orchestrator continues after it in tests, but no durable journal or next-launch continuation connects those two processes. |
| Runtime UI | `shell_server/system_update.py` reports published versions; mutations live separately in `install_requests.py` | Release discovery is not authority to replace the native app. |
| OS OTA | Python `OtaOrchestrator` keeps attempts in an in-memory dict; a historical PostgreSQL migration exists separately | No wired durable repository, drain/stage/reboot adapter sequencing or recovery consumer was found. This bootc path is not the native desktop's Podman/container updater. |
| Enterprise | No deployment updater route/adapter for this native workflow found in `src/safent_control` | An enterprise assignment cannot be treated as direct authority to mutate a worker's host. |

Additional concrete **unfixed OTA blocker**: `BootcUpdater.fetch_and_stage(image_ref)`
ignores `image_ref` and executes `bootc upgrade --apply --quiet`. Its timer entrypoint
calls it from `HERMES_UPDATE_IMAGE_REF` without the orchestrator's revocation/drain
checks. That is not a verified pinned-target staging protocol. Do not wire or
enable this path as the desktop update implementation. It needs its own small
follow-up after confirming the installed bootc command contract.

## Implemented correction

Previously a newer unsigned `VERSION` plus **any** valid signed manifest could
produce `available=true` and a target combining different releases. A manifest
for another architecture could also yield an available target without an engine.

`GET /api/v1/system/update` now requires all of:

- The displayed target version exactly matches the signed manifest version.
- Current and target versions parse safely; target has strictly higher SemVer
  precedence, including prerelease handling and ignoring build metadata.
- A canonical `sha256:` digest exists for this runtime architecture's engine.
- The optional companion digest is canonical when present.

Otherwise both availability fields are false and `to` is null. Existing verified
metadata fields remain informational; they are not install authorization. The
unsigned version read is capped at128bytes. Both blocking discovery calls run
off the request event loop, preserving the existing API/response shape.

The ordering cases are checked against the [SemVer2.0 specification](https://semver.org/).
An optional single `v` prefix is accepted by the local parser; release identity
binding remains exact, so differently spelled unsigned/signed versions fail
closed instead of being conflated.

## Verification

Full archive of runtime `f93444517eacd2b144a0cf5bbd985e175d46668b`, overlaid with
only the two owned source/test files, in isolated DGX scratch
`/tmp/safent-updater-preflight.he0jug`.

**135 passed in1.03s**, no skipped tests, Python3.12.3/pytest9.0.2:

```sh
cd /tmp/safent-updater-preflight.he0jug
PYTHONPATH=src python3 -m pytest \
  tests/unit/agents_os/test_system_update_manifest.py \
  tests/unit/agents_os/test_install_requests.py \
  tests/unit/test_bootc_updater_service.py \
  tests/unit/agents_os/test_ota_orchestrator.py \
  tests/unit/agents_os/test_bootc_updater.py -q -rs --tb=short
```

Includes real minisign compatibility tests, valid signatures over wrong-release/
wrong-architecture/bad-digest fixtures, malformed/prerelease SemVer, bounded read,
and asserting network fetches do not run on the request loop. Existing OTA tests
passing does **not** certify its missing production wiring. No full suite run.

## Minimum next integration contract — not implemented here

Extend the existing native orchestrator rather than adding an updater service.
Before introducing its real ports, its current trait needs a durable checkpoint
and recovery boundary that survives the app process exiting:

1. Record one owner-authorized plan, full signed-manifest/artifact identities,
   observed `from` VersionSet and local operation ID in host-owned storage.
   Serialize update admission across processes; do not put credentials in it.
2. Persist intent **before** each effect and verified outcome afterward. Backup
   identity and old/new digests must survive a crash; a successful CLI exit alone
   cannot mean all components are healthy.
3. Persist `relaunch_pending` before invoking the real Tauri installer/relaunch.
   On next launch, read and validate that same journal before ordinary startup;
   compare actual app/engine/companion identity before continuing remaining steps.
4. A crash between effect and checkpoint is an uncertain result: reconcile the
   observed installation against the exact plan. Never guess completion or retry
   a destructive step solely from an in-memory state.
5. On mismatch, missing/corrupt journal, invalid backup, revoked manifest or
   failed health checks, stop in explicit recovery-required state. Restore only
   the validated prior backup/digests; record rollback failure distinctly.

This requires real interrupted/restarted-process tests across app-first and
engine-first plans, failed restore, duplicate starts and disk-full checkpoints.
It cannot be honestly implemented by calling today's synchronous `run_update`
from a new button. The native capability must remain `integration_missing` until
that complete boundary exists.

Further limits: this cut does not cap the separate manifest/signature download
bodies, install artifacts, implement durable anti-rollback/revocation policy,
or validate end-to-end recovery on a user's machine. Those remain explicit work.
