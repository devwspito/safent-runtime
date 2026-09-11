# Secret scan — 2026-09-11

## Implemented

The CI downloader verifies the pinned Gitleaks 8.18.4 Linux x64 archive against
the official release SHA-256 before extraction or installation. The regression
test checks the pin and ordering. A failed checksum stops the job.

## Evidence

- Official release checksums: https://github.com/gitleaks/gitleaks/releases/download/v8.18.4/gitleaks_8.18.4_checksums.txt
- On the DGX, the Linux arm64 release was downloaded into an isolated temporary
  directory and verified against the same official checksum manifest before use.
- Actual full-history scan of runtime at `e572eeb`: 586 commits, exit 0, no findings
  under the repository configuration. Output was redacted.
- Focused tests before integration: 4 passed, 1 skipped because Gitleaks was not
  on the test process PATH. The real CLI scan above was run separately.

## Limits

This is evidence for the scanned revision and configured rules, not a guarantee
that all credentials everywhere are absent. Re-run on the final integrated head.
No user credentials were printed, rotated or modified. No release was published.
