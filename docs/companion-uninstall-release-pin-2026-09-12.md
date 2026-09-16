# Companion cleanup and immutable release refresh — 2026-09-12

Base: runtime `5addd59`, `fix/safent-review-20260911`. No release, tag,
provider calls or real resource deletion was performed for this change.

## Cleanup

`uninstall [--purge]` and `companion remove [--purge]` no longer resolve an
image or invoke Compose. Missing image/compose files are valid partial-install
states, not a reason to abort the rest of uninstall.

Container deletion uses a full immutable ID after checking Compose project
`safent-ads`, the five explicit service names, and `config_files` equal to this
installation's compose path. Names or a shared project label alone do not grant
ownership. Purge considers only the three fixed companion volumes with matching
project/volume labels and never uses force; attached volumes remain protected.
An unreadable network membership listing is not evidence that a network is empty.
Unknown/unlabelled legacy resources are conservatively retained, not guessed.

## Release refresh

`companion update` refreshes only the persisted reference
`ghcr.io/devwspito/safent-ads@sha256:<64 lowercase hex characters>`.
Missing/empty/legacy tag markers and a different environment override fail before
pull, migration or recreation. Status/rotate also reject non-digest markers;
rotate continues to ignore an ambient image in favour of the persisted digest.
Install/repair require an explicit valid pinned bootstrap image before fetching
files or provisioning. Removal remains independent of these image requirements.

The native bootstrap owns signed VersionSet compatibility and supplies the image;
the CLI does not implement a second signature/manifest resolver. Its digest
validation alone is not proof that a manually supplied image belongs to a signed
release. The developer provisioning script is not changed by this patch.

## Verification

Regression-first: 11 cleanup failures and 10 refresh failures reproduced on the
previous implementation. Cleanup checkpoint: 241 tests passed. Final targeted
provider/agent tests: 90 passed; final uninstall tests: 41 passed. Shell syntax and
`git diff --check` pass. All resource actions use fake Podman in temporary test
directories; no account, OAuth credential, VM or running application was touched.

Combined final command: **266 passed in 128.62 seconds**, no skips or failures:

```sh
PYTHONPATH=src /home/luiscorrea-dev/Desktop/safent-ads/.venv/bin/python -m pytest \
  tests/unit/cli tests/unit/ops/test_companion_provision.py \
  tests/unit/ops/test_safent_cli_backup_restore.py -q --tb=short
```

## Integrated release 0.9.5 gate

All three runtime changes were reviewed together on the canonical DGX branch.
Product metadata is 0.9.5, CLI revision is 14, and the official staging script
regenerated `runtime-manifest.lock.app_files` from the final source.

- Python CLI/ops/agents_os/healthz: **1423 passed, 11 skipped**, 135.12 seconds.
- Rust `cargo check --locked`, Clippy all-targets with `-D warnings`, fmt: pass.
- Rust `cargo test --locked`: **229 passed** (138 unit + 50 fake CLI + 41 real CLI).
- Desktop TypeScript: **156 passed**; typecheck and build pass using Node 24.13.1.
- Release/version/manifest/Compose focused tests: **12 passed**.
- Release publisher fixtures: **6 passed**; no actual publication by those tests.
- Fresh Linux arm64 packaged-layout test: pass, all 6 app files and 15 pinned
  toolchain files verified; no archives/cache under resources.
- Shell syntax and `git diff --check`: pass.

The 11 Python skips are explicit test/environment limitations, not failed release
metadata/packaging gates: two dbus_fast-dependent collection/cases (dependency
absent), one gitleaks binary test (binary absent), one spec003 contract fixture
(not present), and seven Landlock resources requiring template parameters
(screen, system_services, system_info, udev_devices, audio_devices, scheduler,
input_control). They are not counted as passes. No host tooling was installed to
hide these skips. Linux tests do not certify a newly packaged macOS first boot;
that remains the downstream desktop artifact gate.
