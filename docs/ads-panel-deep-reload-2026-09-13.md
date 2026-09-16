# Canonical Ads panel reloads through the native bridge

Actual native acceptance found `GET /ads/cockpit` rejected with 403
`PATH_NOT_BRIDGED`, although React navigation already used that URL.
The bridge previously allowed only root, assets and REST API paths.

## Closed allowlist

The existing bridge now allows **GET/HEAD only** for root and the ten
canonical panel routes declared in Ads `panel/src/App.tsx`:

`cockpit`, `cartera`, `campanas`, `senales`, `propuestas`, `creatividades`,
`reglas`, `registro`, `conexiones`, `ajustes`.

POST/PUT/PATCH/DELETE to those SPA paths are rejected before session exchange
or an upstream request. Login, MCP, unknown routes and arbitrary children
remain denied. Existing REST authorization, CSRF, bridge-cookie requirement
and upstream session isolation are unchanged. No catch-all or local HTML
response is introduced: the existing TLS transport returns upstream status,
headers and body.

Ads `_mount_panel_spa` in `composition/app.py` already serves the real index
for GET client routes. It currently registers GET, not HEAD; the bridge
permits HEAD but transparently preserves an upstream 405 if that is what
the companion returns. It does not manufacture HEAD success.

## Explicit limitation

Campañas drill-down in `CampanasPage.tsx` serializes an arbitrary sequence of
encoded EntityRefs. `shared/ids.py` accepts a free-form external identifier
and optional scope components. This patch does not invent a duplicate or
broader parser in Runtime. Reloads of `/ads/campanas/<references>` therefore
remain denied pending a separately bounded contract; existing client-side
navigation is not changed. Root-level `/ads/campanas` reload is supported.

## Tests and scope

- Regression reproduced before the change: real router/TLS companion test
  returned 403 for GET cockpit where an HTML response was expected.
- Complete `tests/unit/shell_server/test_ads_bridge.py`: **93 passed, 2.95s**.
- Tests cover all ten routes GET/HEAD using an explicit upstream HTML
  fixture, unchanged response headers, all canonical POST denials, other
  mutation methods, unknown/child/login/MCP denials and missing bridge cookie.
- Ruff and `git diff --check`: passed.
- Logs: `/tmp/ads-bridge-spa-red.log`, `/tmp/ads-bridge-spa-final.log` on DGX.

This is an isolated bridge regression result, not signed native GUI
acceptance. No Mac state, account, version, tag, image or release was modified
by this patch.
