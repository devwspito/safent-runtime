# macOS Rust gate — bounded investigation

Follow-up to `NATIVE-MACOS-REVIEW-2026-09-12.md`. **Not resolved. No production
or test workaround retained.** Real native presentation checks remain valid;
the complete macOS Rust/release gate remains red.

## Reproduction and eliminated hypotheses

On this same macOS 15.5 arm64 host, with the isolated Rust 1.98.1 toolchain:

1. `cargo test --locked --test engine_adapter_cli_contract
   observe_maps_a_valid_facts_event -- --exact --nocapture` fails serially
   with the unchanged two-second deadline. This is not explained by parallel
   test interference alone.
2. Generated script has bytes `23 21 2f 62 69 6e 2f 73 68 0a` (`#!/bin/sh` + LF),
   owner executable mode 0700 and no `com.apple.quarantine`. Only
   `com.apple.provenance` is present. No attributes were removed or changed.
3. Explicit `/bin/sh <same-file> facts --porcelain` returns valid fixture JSON
   immediately. Direct execution can remain silent past 8/10-second limits.
   Some warmed paths later execute normally, so a single successful rerun is
   not proof of correction.
4. Fresh extensionless sh/bash/env-sh probes and a compiled C probe initially
   passed in 0.006–0.224 seconds. A `.sh` copy timed out. However removing `.sh`
   from the actual Rust fixture reproduced **the same 36 PASS / 12 FAIL**.
   The suffix hypothesis was refuted and that edit reverted.
5. `sample` of the blocked child in `stop_succeeds_when_the_cli_says_so` shows
   only `_dyld_start + 0`, physical footprint 96 KB, before shell initialization.
   `lsof`: stdin/stdout `/dev/null`, stderr pipe; no fd3 or other inherited
   descriptor explains this stop-case failure.
6. Crucially, after rebuilding a scratch-only env-sh experiment, **the native
   Mach-O test runner itself** remains at `_dyld_start + 0`, 96 KB, before
   printing `running tests`. Invoking that binary with **`--list`** also hangs:
   no test, `Command`, process group, adapter reader or pre-exec hook runs.
   Copying the same binary to another isolated path also hangs.
7. `codesign --verify --verbose=2` reports that runner valid on disk and
   satisfying its designated requirement. No quarantine attribute is present.
   A narrowly filtered syspolicyd log query for `safent` returned no events;
   the responsible OS mechanism is **not identified**.
8. A warmed script run via Python subprocess with a new session both disabled
   and enabled completed in under 0.01 seconds. This alone does not certify
   the Rust process-group path, but does not establish process-group failure.
9. The actual `safent` source was copied to the isolated repo-root location
   expected by `engine_adapter_real_cli_contract`. That test binary also
   failed to start printing test output; no real-CLI PASS is claimed.

## Why no launcher workaround

The real CLI also uses `#!/bin/sh`. Changing only fake shebangs would not prove
production works. More importantly, invoking `/bin/sh` explicitly in the
adapter cannot address a Mach-O test runner blocked before any Rust executes.
There is insufficient evidence to justify changing production subprocess
selection, weakening deadlines, serializing tests, stripping OS attributes,
disabling security services, or treating retries as a pass.

The scratch env-sh and suffix experiments did not alter canonical source.
`desktop/src-tauri/tests/engine_adapter_cli_contract.rs` has no retained diff.
No complete-suite green result supersedes the earlier report: 128 unit PASS,
36 adapter PASS / 12 FAIL remain the last completed full-command observation.

## Evidence and cleanup

Disposable root: `/tmp/safent-native-macos.LhfcCk`.

- `shebang-sample.txt`: child /bin/sh sample at `_dyld_start`.
- `test-binary-sample.txt`: native test runner sample at `_dyld_start`.
- `probe-sh`, `probe-bash`, `probe-env-sh`, `probe-bin.c`, `probe-bin`,
  `probe-facts`, `probe-facts.sh`: isolated comparison inputs.
- `probe-test-runner`: copied test Mach-O, no user binary overwritten.

Own blocked diagnostic processes were terminated by exact observed PIDs,
including cargo children. No global process kill, user setting change, broad
cleanup or additional Rust installation was performed. Evidence is retained
for host investigation. Next step is repeat on a clean supported macOS runner
or identify the host's pre-main execution delay before certifying release.
