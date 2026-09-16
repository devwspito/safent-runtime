# Native OAuth acceptance — 0.9.18 / Ads 0.2.13

## Installed and verified on the Mac

- Workflow `devwspito/agents-autonomy` run `34772049792` completed successfully.
  Candidate `v0.9.18` is published as prerelease, not stable/latest.
- DMG SHA-256:
  `29cdad1ac4746bad1639abc4d65b4bc1a110f9bcdb9c4302a9e5e0cc2521623a`.
  Matches GitHub metadata and SHA256SUMS. `codesign --verify --deep --strict`
  passes; `spctl` accepts it as Notarized Developer ID, team `JBMBA58A8X`.
- Installed `/Applications/Safent.app` 0.9.18 with backups of both databases
  and the previous signed app. No account/credential/conversation/volume wipe.
- Running engine ARM digest:
  `sha256:fed00d32772d4b3a180abb94cf67036f18895995edcc7a3608a238428151d048`.
- Running Ads ARM digest:
  `sha256:045650606b5ff18ec7f86ac2ea01a54b7d3664af0dd3b84c8078165e6b131f27`.
  API healthy, broker/worker active, migration completed. Main chat and Ads
  panel render. Shared Composio configuration retained; automatic lease
  renewal accepted and fresh, recipient pin and SSO trust checks pass.
- Clicked Google Connect in the actual native Ads panel: Chrome opened the
  hosted connection automatically. No copying an URL or context-menu action.
- Entered the already-provided Google account number; reached Google's
  account chooser and then its passkey identity-verification screen.

## Acceptance not yet complete

The user must complete Google's passkey/Touch ID verification and consent.
Do not mark the Google connection successful until the fresh callback admits
the intended account and actual campaign inventory renders in Safent. Do not
resurrect old consumed sessions by editing the database. No advertising writes
or campaign activation were performed. Meta config is still absent. Google
advertiser verification remains separate from OAuth.

The Composio hosted form still asked for Customer ID despite the request's
connection_data prefill. That extra provider step is observed, not assumed
fixed. Investigate supported prefill semantics separately; never claim that
OAuth alone provisions an advertising account or bypasses provider checks.

## Image-cache corner case discovered during acceptance

Manual pre-pull by a multiarch tag caused Podman container ImageDigest to
retain the index digest even when Config.Image was the exact signed platform
digest. The old observer therefore recreated the correct engine repeatedly.
Stopped the native retry loop; removed only the replaceable engine container
(persistent volume verified) and the two newly downloaded image caches;
pulled the signed platform refs directly. Standard signed startup then
converged, retaining all data and old rollback images.

Follow-up source fix (NOT in the immutable 0.9.18 release): facts and up share
one digest helper. A differing digest is normalized only when a lookup of the
exact pinned image reference and the running container return the identical
valid 64-hex content ID. Tags, labels, unknown/mismatched IDs and failed
lookups cannot satisfy this check. The porcelain suite passes 77 tests,
including six alias/mismatch/error regressions; shell syntax and diff checks
pass. Broader install/backup run: 93 pass, 21 fail in the existing recipient-pin
fixture path. The first failure also reproduces on untouched v0.9.18 in an
isolated worktree; these fixtures need correction before claiming the broader
suite green or publishing this follow-up.

Earlier full Ads verification is recorded in safent-ads:
`docs/oauth-return-2026-09-13.md`. Keep all private diagnostic exports and
backups outside Git. Cached-image retention and VM-internal disk preflight
remain follow-up hardening; never use a broad system prune or volume reset.
