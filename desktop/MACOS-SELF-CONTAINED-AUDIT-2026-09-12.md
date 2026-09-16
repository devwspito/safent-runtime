# macOS self-contained contract — 0.9.5 delta

Source: canonical DGX `lumen-runtime-next`, HEAD `5addd59`, with concurrent CLI fixes intentionally left untouched. This is source/contract verification, not certification of a newly packaged app or clean-machine installation.

## Independent correction

`desktop/src-tauri/src/engine_adapter.rs` now gives macOS CLI invocations only `/usr/bin:/bin:/usr/sbin:/sbin`, regardless of inherited PATH. Previously the launcher preserved personal executables and added Homebrew/local locations. The engine and its helpers remain pinned separately to the bundled paths. Linux PATH behavior is unchanged. `desktop/src-tauri/src/main.rs` uses the same system PATH for macOS clipboard commands and addresses `/usr/bin/pbcopy` and `/usr/bin/pbpaste` absolutely.

Tests inject a decoy PATH containing personal, Homebrew, third-party Podman and current-directory entries; none survives macOS selection. Tests also cover absent/empty PATH and preservation of the non-macOS contract. A macOS-only clipboard regression checks absolute paths.

## Contract matrix

| Surface | Evidence | Result / limit |
| --- | --- | --- |
| Native engine selection | `boot.rs:1035–1058`; `engine_adapter.rs:89–95` | Bundled CLI and Podman selected by exact path; no package-manager installation command. Development overrides remain explicit; this audit does not certify arbitrary override contents. |
| Host command resolution | `engine_adapter.rs::native_command_path`; `main.rs::clipboard_*` | Fixed in this delta: stock macOS PATH and absolute clipboard executables. No Linux discovery change. |
| Engine/helper isolation | `safent:163–270` | Native namespace and pinned Podman shim use private HOME/config/socket state, bundled helper path and compose provider; no host Docker daemon prerequisite. Bare CLI fallback is distinct from the native branch. |
| Seccomp guest accessibility | `safent::_ensure_seccomp` | HEAD already copies bundled policy into state atomically; no `/Applications` policy path handed directly to the VM. Unchanged here. |
| First-boot companion metadata | `safent::_fetch_companion_file`; `provision.sh::write_companions_json` | HEAD already prefers staged files and computes SHA-256 with OpenSSL. No mandatory GNU `sha256sum`. Unchanged here; another agent owns regression additions. |
| TLS/bootstrap host tools | `provision.sh::ensure_tls` | Uses shell/awk/OpenSSL available on stock macOS. Local `/usr/bin/openssl` is LibreSSL 3.3.6 and supports `req -addext`; no unsupported-option defect reproduced. This check alone is not a complete TLS install test. |
| Python / compose | `provision.sh::ensure_secrets`, `ensure_sso_keypair`; `safent` pair/approval verbs | Python commands run through the pinned container runtime, not host Python. macOS compose executable is staged and selected by `PODMAN_COMPOSE_PROVIDER`. |
| Staging tools | `desktop/scripts/stage-runtime.sh`, `runtime-manifest.lock` | jq/Go/package extraction are build-time dependencies, not requirements imposed on installed app users. Staging includes engine/helpers, compose, machine image, CLI and companion templates. |
| Recovery | `boot.rs`, `reconcile.rs`, `engine_adapter.rs` | Recovery goes through the same bundled CLI driver/PATH. Missing/incompatible artifacts remain an error, not an instruction to install Homebrew or another engine. No claim of automatic repair for every corrupt state. |
| Update | `update/native.rs:1,199–246`; `update/tauri_updater.rs` | Native updater downloads/verifies through Tauri, installs the signed app and restarts. It is explicitly app-only; it does not perform the separate atomic engine+Ads update transaction. New app bootstrap resolves its bundled engine manifest. No unsigned shell updater added. |

## Verification

DGX commands from `desktop/src-tauri`:

```sh
cargo test augmented_path_tests -- --nocapture
cargo test --test engine_adapter_cli_contract --test engine_adapter_real_cli_contract
```

- PATH tests: 3 unique tests passed in each of three test targets (9 executions).
- Fake CLI contract: 50 passed; real CLI contract: 41 passed. No skips or failures.
- Cargo compiled the changed desktop sources successfully on Linux; `git diff --check` passed.
- macOS-specific clipboard test is included but not executed by the DGX Linux run. No new macOS binary/DMG, full boot, user installation, signing or publication was performed by this delta.

Release gate remains an actual 0.9.5 macOS bundle with stock PATH: first boot, interruption/retry, companion install and update/relaunch. Preserve existing user applications and do not count fixture or Linux contract coverage as that clean-machine certification.
