# Private macOS Podman staging

`desktop/runtime-manifest.lock` now pins the private build's source, compiler,
patch and unsigned binary hash. `stage-runtime.sh` requires
`SAFENT_PRIVATE_PODMAN_DIR` on Darwin and verifies it before cache reuse,
download or staging. It replaces only the upstream Podman executable; existing
pinned gvproxy/vfkit/krunkit/machine-image delivery stays intact. It never runs
the supplied binary to decide whether to trust it.

Artifact contract: `podman`, `podman-private-build.json` (schema_version 1,
capability `safent-private-machine-v1`, immutable input hashes and binary_sha256),
license, patch, Go module inventory, vendor module list, CycloneDX SBOM and
third-party notices. Staging rejects linked/missing files, mismatched binary,
patch or immutable pins, stock capability and non-arm64 Mach-O. All outputs are
included in the normal runtime bundle and app signature. Sidecar alone grants
no authority; the existing sealed-bundle trust boundary remains mandatory.

The pipeline's build-only implementation lives in agents-autonomy
`.github/podman-private/`; its report is
`docs/podman-private-build-2026-09-12.md` in that repository. Source commit is
`8303f2e25b675ea7f82099d615c60969aec15870`; unsigned binary SHA256 is
`7e0a99204954942f1a1145f09a682eeb45eea543393aad9a2ee69f1b2cc10716`.
After codesign the pipeline updates binary_sha256 while preserving the unsigned
hash, before sealing the enclosing app.

Verification: seven Python staging tests PASS; six existing image digest
propagation cases PASS; bash syntax and diff checks PASS. Native Mac build was
verified against this real lock and emitted two forwarding-test PASS results.
The packaged-layout shell test skipped on this Darwin host without explicit
target. Full image downloads, fresh signed packaging and VM coexistence are not
claimed here. Runtime namespace/connection isolation is a separate coordinated
change: this patch alone does not eliminate shared TCP ports or inherited host
environment effects.

```sh
python3 -m unittest discover -s desktop/scripts/tests -p test_private_podman.py -v
python3 desktop/scripts/lib/verify-private-podman.py /path/to/private-build desktop/runtime-manifest.lock
SAFENT_PRIVATE_PODMAN_DIR=/path/to/private-build desktop/scripts/stage-runtime.sh aarch64-apple-darwin
```
