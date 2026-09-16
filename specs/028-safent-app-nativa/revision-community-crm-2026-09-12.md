# Community CRM — Enterprise read adapter and UI

## Delivered boundary

Community can list the CRM connections assigned by Enterprise and explicitly
query a reviewed static GET operation in **Integraciones → CRM de Enterprise**.
Enterprise remains the only component contacting the CRM and holding its
credential. This is not universal CRM support: it implements the existing
Enterprise `GET /v1/crm` / `POST /v1/crm/{id}/read` contract only.

No CRM is connected in production. No write, OAuth, SSH, webhook, RAG or new
executor was introduced. A native Hermes CRM tool is **not registered yet**:
this cut is the authenticated backend/UI consumer, not a claim that the agent
can automatically query CRM from chat. Wiring such a tool must preserve the
existing tool broker/jaula; those modules were deliberately not modified.

## Contract and safeguards

- `GET /api/v1/crm` projects `{connections,limit,context}`. Metadata is
  allowlisted; unknown upstream fields never reach the UI. `context` is a
  domain-separated HMAC of the pairing identity, not a credential.
- `POST /api/v1/crm/{UUID}/read` accepts exactly `{context,path}`. No arbitrary
  server, token, HTTP verb or URL input. Enterprise validates the current
  instance, assignment, connection state and selected operation every time.
- The stored pairing endpoint and secret are captured under the existing
  configuration lock. Old context is rejected **before any outgoing call**;
  pairing/secret changes or local revocation during the call discard its
  response. Already-started external reads cannot be undone.
- HTTPS paired endpoint only, no URL credentials/query/fragment or alternate
  port. No redirects/environment proxies; bounded total timeout and response
  size; reject compressed, duplicate-key, excessively deep or non-finite JSON.
  No new arbitrary egress allowance or MCP-server access was added.
- Both routes reuse the actual shell bearer middleware (GET is authenticated
  too). Associate licensing maps CRM to `integraciones`, not an always-allowed
  route. No Community MFA introduced.
- Reads return `untrusted_external_data:true` and the verified operation.
  Preview uses React text, not HTML/Markdown execution; capped at 64,000 visible
  characters with an explicit truncation note. Literal instance-token echoes
  are rejected; this is not universal DLP against transformed data.
- No-store, generic error codes, no raw upstream text. Unpaired, unavailable,
  empty verified inventory and revoked/stale intent have distinct states.
  Neither HTTP failures nor malformed responses become an empty success.
- Client timeout/abort covers the response body, checks cancellation after
  parsing, and never automatically replays a read (including 401). Selection,
  refresh, focus return and unmount invalidate late results. “Dejar de esperar”
  explicitly does not promise cancellation of a read already in Enterprise.

## Emil review

| Before | After | Why |
| --- | --- | --- |
| Community had no consumer for its Enterprise CRM assignments. | Compact section inside the existing Integrations view. | Use the existing navigation; no disconnected dashboard. |
| No explicit selection of allowed operation. | Labeled connection and approved-query selectors; disabled action until both are selected. | Make the exact intent legible without an arbitrary URL editor. |
| No safe presentation of CRM records. | Focused result heading and bounded text preview with external-data notice. | External content must not become instructions or active HTML. |
| No stale-pairing or revocation recovery. | Clear prior result/selection on invalidation; explicit refresh/review. | Never redirect old intent to a new organisation. |
| Repeated keyboard workflow had no defined behaviour. | Immediate keyboard controls, visible focus, no added animation; single-column controls at 390px. | Interaction speed and state clarity, not ornament. |

## Evidence

- **85 PASS**, 4 warnings: CRM unit boundary (28), feature guard, association
  store and the real `create_app()` authentication route sweep. No skips.
  Early isolated environments lacked python-multipart/aiohttp; those setup
  failures were corrected in the temporary runner, not hidden or worked around
  in production. Optional unrelated dependency logs are not CRM evidence.
- **1 PASS** cross-repository test: real Community router + encrypted SQLite
  association store → HTTP ASGI transport → real Enterprise instance router,
  authentication and CRM service/SQLite. Verified approved read, unapproved
  path rejection, grant revocation, empty verified inventory after revocation,
  and re-pair rejection without another external call. Only external CRM
  transport and initial human step-up seeding are fixtures; this is **not** an
  MFA or public-network TLS certification. No real secrets or customer data.
- **17 PASS** frontend: API/body abort, no auto-retry, wrong pairing response,
  explicit selection, text escaping, focus, errors/empty, revoked selection,
  late response after stop/refresh, unmount and existing Integrations regression.
- TypeScript + Vite production build **PASS**. Ruff focused source/tests and
  `git diff --check` **PASS**. An ES-target-incompatible `Object.hasOwn` use
  found by the first build was replaced with the compatible intrinsic.
- Chrome headless fixture at **1280×844 and 390×844**, reduced motion: keyboard
  submission, response, revoke/error and response clearing; zero JS errors and
  zero horizontal overflow. Inspecting mobile response and desktop revoked
  screenshots confirmed layout/focus. Screenshots and runner are in
  `/tmp/safent-crm-ui.VZpUsu/`; `.ui-crm-review.*` are disposable, not product.
  This visual test is browser QA of real Community components, **not** a new
  Tauri binary certification or a live CRM connection.

The independent macOS pre-main execution issue is documented in
`desktop/RUST-MACOS-GATE-2026-09-12.md`. No Rust green/release claim is made here.

## Reproduction

From Runtime, using an isolated Python 3.12 environment with pytest,
pytest-asyncio, FastAPI, httpx, cryptography, python-multipart and aiohttp:

```sh
PYTHONPATH=src python -m pytest \
  tests/unit/shell_server/test_crm_api.py \
  tests/unit/shell_server/instance/test_feature_guard.py \
  tests/unit/instance/test_association_store.py \
  tests/unit/shell_server/test_api_v1_authorization.py -q
```

Cross-repo additionally requires Enterprise's dependencies (`fastapi-mcp>=0.4,<0.5`,
`mcp>=1.28.1,<2`, psycopg2-binary) and its source on PYTHONPATH:

```sh
PYTHONPATH=src:<enterprise>/src:<enterprise> python -m pytest \
  tests/integration/test_crm_enterprise_boundary.py -m integration -q
cd frontend
NODE_OPTIONS=--no-experimental-webstorage npm test -- \
  src/api/crm.test.ts src/views/EnterpriseCrm.test.tsx src/views/IntegrationsView.test.tsx
NODE_OPTIONS=--no-experimental-webstorage npm run build
```

The explicit `-m integration` matters: repository defaults deselect these
tests; an earlier deselected invocation was not counted as a pass.

## Exact integration paths

- `src/hermes/shell_server/cowork/crm_api.py`
- `src/hermes/shell_server/instance/feature_guard.py` (one prefix)
- `src/hermes/shell_server/main.py` (CRM mount; **also preserves the coordinated
  Ads mount `create_ads_bridge_router(_DB_PATH, vault)` from the other agent**)
- `tests/unit/shell_server/test_crm_api.py`
- `tests/integration/test_crm_enterprise_boundary.py`
- `frontend/src/api/crm.ts`, `crm.test.ts`
- `frontend/src/views/EnterpriseCrm.tsx`, `EnterpriseCrm.module.css`, `EnterpriseCrm.test.tsx`
- `frontend/src/views/IntegrationsView.tsx`, `IntegrationsView.test.tsx`
- `frontend/src/lib/i18n.ts` (only CRM ES/EN keys)
- This report.

Keep `desktop/RUST-MACOS-GATE-2026-09-12.md` as a separate diagnostic commit.
Exclude every `.ui-*` file, temporary database, screenshot and generated preview.
No stage/commit/push was performed by this agent.
