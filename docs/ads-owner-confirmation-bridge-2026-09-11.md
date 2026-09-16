# Community Ads confirmation through the native bridge

Companion commit `ac2182a` removed Community MFA and uses exact, one-shot owner
confirmations. The native proxy whitelist still admitted only the retired
`X-Reauth-Token`: the new panel would receive another 428 instead of confirming.

The bridge now forwards `X-Action-Confirmation`, not the old OTP header. It
preserves body bytes, method, query and CSRF pair; it does not mint a proof,
authorize an effect or expose the companion session to the browser. If a
confirmed request receives 401, the cached session is cleared and that response
is returned without a hidden identity exchange or mutation replay. The existing
single retry for ordinary requests remains independently tested.

**29 focused tests PASS**, real loopback TLS companion transport with fixed-IP
resolution and a test CA. Only companion application behavior and D-Bus minting
are doubles: this is not a live-provider or native-installation test. Final full
runtime regression: **5830 PASS, 19 SKIP, 64 deselected**, 248.32s, seven warnings.
The host skips (SDK drift, kernel templates and explicit release-only gates) are
recorded in the full log, not counted as passing image coverage.

Snapshot `/tmp/safent-bridge-confirm.fkOIVh`, full archive `74af483` plus the two
owned source/test paths; logs `bridge-focus.log` and `full-bridge.log`. Ruff and
diff checks pass. No credentials, provider operations, migrations or services
were changed. This bridge change and the companion API/panel/migration must be
included together in the future release; corporate account grants remain a
separate incomplete capability.
