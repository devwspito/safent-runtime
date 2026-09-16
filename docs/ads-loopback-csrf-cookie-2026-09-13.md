# Ads CSRF cookie on the native HTTP loopback origin

## Reproduced failure

Native 0.9.13 acceptance reached a healthy core and Ads, but the first-business
POST returned 403. A read-only request confirmed that the bridge forwarded
`ads_csrf` with `Secure; Path=/ads; SameSite=Strict` over HTTP loopback.
The browser therefore could not reliably return the required CSRF cookie.
`ads_session` correctly remained server-side.

The bridge's comment described a loopback exception, but its implementation
had no `Request` and always retained `Secure`. The old TLS companion fixture
also omitted `Secure`, unlike the actual Ads middleware, concealing the bug.

## Minimal change

Propagate the actual ASGI `Request` through response translation and cookie
rewriting. Only remove `Secure` from the allowed `ads_csrf` cookie when the
request scheme is exactly HTTP and its hostname is exactly `127.0.0.1`,
`localhost`, or `::1`. HTTPS and all other hosts retain upstream `Secure`.
Caller `Forwarded` / `X-Forwarded-*` headers are not consulted for this
decision. Domain is still omitted, Path remains `/ads`, and SameSite remains
the upstream value. Unknown cookies and `ads_session` are never forwarded.

No CSRF middleware, authorization rule, session handling, OAuth exchange,
route allowlist, action confirmation or POST retry policy changed.

## Evidence

- Real local TLS upstream fixture now emits `Secure`, matching Ads. The new
  loopback test failed before the fix because `Secure` remained set.
- Entire `tests/unit/shell_server/test_ads_bridge.py`: **50 passed, 2.68s**.
- Nine origin cases cover IPv4/hostname/IPv6 loopback, HTTPS, public HTTP,
  lookalike hostname, abbreviated IPv4 and wildcard bind address. Requests
  carry malicious forwarding metadata. GET populates the actual HTTPX jar;
  tests do not manually plant the CSRF cookie. Missing/mismatched tokens
  still receive 403; the companion session never enters the browser jar.
- Independent cross-repository check: **7 passed** using the actual Ads
  `CsrfMiddleware`, actual Runtime `_translate_response` and HTTPX cookie
  handling. Valid loopback/HTTPS pairs passed, while nonloopback HTTP,
  missing and mismatched CSRF remained rejected.
- Ruff and `git diff --check`: passed.

DGX logs: `/tmp/ads-bridge-secure-red.log`,
`/tmp/ads-bridge-secure-final.log`. Independent reviewer harness:
`/tmp/safent-csrf-real-middleware.LHw6yg/probe.py`.

These are isolated transport/middleware tests, not a claim of successful
onboarding against the user's Mac or advertising accounts. No Mac state,
live business, provider credentials, tags, versions or release was changed
by this patch. Signed native acceptance remains separate.
