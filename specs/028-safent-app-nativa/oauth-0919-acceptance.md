# OAuth usability follow-up — 0.9.19 candidate

## Problem and changes

0.9.18 opened Ads consent but not Codex device login. A separate
`open_provider_oauth` native permission now admits only the exact official
`https://auth.openai.com/codex/device` endpoint, from the current boot origin
and main window. Both automatic launch and the retry link use it. The native
Hermes token exchange and provider resolver are unchanged; a pending login
must not be presented as successful.

Integrations now includes owner-only Meta setup. The administrator registers
the exact Composio callback in their Meta app and submits its App ID/Secret
through Safent. The backend creates/reuses a named custom OAuth configuration
in the existing Composio project, validates its toolkit/status, and stores only
the configuration ID. It never saves the Meta app secret locally. Credential
rotation uses compare-and-set to avoid linking a result to a different project.
Provider failures and input validation are redacted; the request body is bounded.
No generic tool-router access is enabled. Account consent remains a separate
step in Ads and all advertising mutations remain subject to the cage.

The prior image-content alias fix is included. The companion CLI fixture now
models the public recipient pin, without weakening the production check.

Native builds now always show an app-update check action and app version in
the product sidebar. `show_native_updater` only opens the existing host-owned
review/confirmation flow, with no URL or install parameters, and requires the
current main-window boot origin. Direct check/install permissions remain local
to the bundled UI. Native mode does not poll or offer independent engine updates.
This is a manual check action, not an automatic new-version notification.

## Verification before publication

- Frontend: 470 tests passed; TypeScript passed. Production build passed before
  the final native-footer addition; release CI must build the final version.
- Native: 160 tests passed, including exact Meta help and updater caller checks.
- Backend Composio/configuration/lease/security: 198 tests passed.
- CLI installation/porcelain/backup: 192 tests passed, including the previously
  failing recipient-pin fixtures.
- No real campaign writes, publication, or budget changes were performed.

## Real provider acceptance (not interchangeable with unit tests)

Google's fresh OAuth callback completed successfully. The actual Ads database
contains account `1677791325` as ACTIVE, EUR, Europe/Madrid, and its new OAuth
session is `ok` without an error. Advertiser verification is a separate Google
requirement; ACTIVE here is connection state, not a claim that ads can serve.

Meta app `1063816289878236` is accessible in the user's normal Chrome profile.
With explicit authorization, the exact callback
`https://backend.composio.dev/api/v1/auth-apps/add` was added and Meta displayed
“Se han guardado los cambios”. No other security toggle was changed.
Meta then requested the user's password again before revealing the App Secret.
That user verification, app submission through the new Safent UI, account
consent and a real account inventory check are still pending.

The official Codex device page was opened manually in Chrome to unblock login
independently of the binary fix. It requests the user's OpenAI sign-in. A real
approved device flow and real chat response are still pending. Never silently
copy credentials from the host's Codex installation or switch to billable API
credentials as a fallback.

Publication and installation must be recorded once verified. Do not claim that
the currently installed 0.9.18 contains these changes.

## Updater acceptance and release channel

The installed 0.9.18 app already has a native tray action, “Buscar actualizaciones
de la app…”. Its fixed endpoint uses the GitHub latest release. At inspection,
that release was v0.9.5: newer published builds were prereleases and therefore
not offered by this feed. Successful candidate CI does not promote stable/latest.

Publish v0.9.19 as a signed candidate first. Promotion changes the update channel
for all users; record that decision separately and do not claim GUI acceptance
from a successful build alone. The user specifically wants to test the updater:
do not replace /Applications/Safent.app manually or erase their data. The first
update must be initiated by them from the existing native tray action. The new
sidebar action becomes available after the signed 0.9.19 update is installed.
