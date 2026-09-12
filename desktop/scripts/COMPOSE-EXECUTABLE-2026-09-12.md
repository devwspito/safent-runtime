# Compose executable packaging regression

Base Runtime `dfc1fed`, pipeline `8564ed00`. No bump, commit, release or installed
application changes in this correction.

## Cause and correction

The real macOS v0.9.5 app contains a signed arm64 Mach-O `docker-compose` at
0644; its runtime-bundle entry also records 0644. `stage_compose_provider` sets
0755 correctly, but the subsequent `normalize_staged_tree` omitted Compose from
its executable allowlist and reverted it to 0644 before generating the manifest.
Codesign authenticates the binary; it does not make the provider executable.

- Add `docker-compose` to the normalization allowlist. This occurs before
  manifest generation and all signing, not as a patch to an already signed app.
- Extend the macOS distribution validator to require a regular, non-symlink
  Compose provider with exact 0755 permissions and exactly one manifest entry
  recording 0755. A valid hash cannot override this launch contract.
- Pipeline validates staged resources before codesigning. The existing final
  GUI-app validator checks them again after bundling/signature verification.
  Both native gates run only `version --short`, expect the lock-reviewed 5.5.1,
  use a temporary HOME/DOCKER_CONFIG with a nonexistent engine socket, and time
  out after 15 seconds. No Podman machine or Compose project is started.
- Loader/version errors reject the release. The final validator never chmods,
  refreshes manifests or re-signs the app.

## Evidence

- RED regression: production downloader → Compose stager → normalizer produced
  **1 FAIL / 2 PASS** before the fix (actual mode 0644).
- Corrected focused DGX Python suite: **14 PASS + 13 subtests**.
  `test_compose_provider.py` executes the harmless staged fixture after the
  actual normalizer, instead of only checking its hash or pre-normalizer mode.
- macOS distribution tests: **11 PASS** locally. Cases reject mode 0644/0744/
  0777/setuid, wrong/duplicate manifest entries, symlink and failing loader.
- Pipeline binding tests: **13 PASS**, including pre-sign/final gate order.
- `test-normalize-staged-tree.sh`: PASS. Real Linux arm64
  `test-packaged-layout.sh`: PASS (pinned 30 MiB upstream download, scratch only).
  An initial incomplete scratch lacked app/background fixtures; those harness
  failures were corrected before these final results, not counted as passes.
- Ruff for the modified pytest provider test, Python compile and shell syntax:
  PASS. No full runtime suite (no engine/CLI changes).
- New validator against `/Applications/Safent.app/Contents/Resources/runtime`:
  **expected rejection** of the real 0644 provider, without mutating the app.
- Real signed arm64 Compose copied to an isolated Mac scratch and normalized to
  0755: `codesign --verify --strict` PASS; isolated `version --short` returns
  **5.5.1**. This proves the executable/loader regression specifically; it does
  not certify a newly rebuilt DMG, notarization, VM startup or provisioning.

Scratch: Mac `/tmp/safent-compose-mode.y78eZg`; DGX
`/tmp/safent-compose-mode-test.Umpjee`.

```sh
python3 -m pytest desktop/scripts/tests/test_compose_provider.py \
  desktop/scripts/tests/test_macos_distribution.py -q --tb=short
bash desktop/scripts/tests/test-normalize-staged-tree.sh
bash desktop/scripts/tests/test-packaged-layout.sh
python3 -m unittest discover -s .github/scripts/tests -p test_release_input_binding.py
```

## Release/recovery boundary

Rebuild and validate the signed DMG through the updated gates. Existing
`~/.safent/runtime/6.1.1` may still contain the old 0644 copy: bootstrap must
reconcile it from the verified new bundle rather than treating the unchanged
Podman version/hash as proof of correct modes. That CLI/Rust cache-recovery
work belongs to the parallel bootstrap owner and is not implemented here.
