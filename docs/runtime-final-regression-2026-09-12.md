# Community 0.9.1 — final regression, 2026-09-12

## Scope and outcome

Source: `ddd59aa67fed7ed672db3639e28b03a0557d1b0e`, archived from the clean
DGX checkout `lumen-runtime-next` into `/tmp/safent-runtime-final.cLlnB3`.
No image, release, tag, provider request, or user configuration was changed.

The complete backend run was **not green**: its failures were diagnosed and
closed with focused regressions below. Do not describe those separate runs as
one successful full run. Frontend and Linux native suites passed in full.

| Check | Evidence |
| --- | --- |
| Backend configured full suite | 7,281 passed, 10 failed, 19 skipped, 250 deselected; 273.10 s (275.28 s wall). |
| Corrected failing modules plus new regressions | 74 passed, no skips; 18.68 s. |
| Affected shell-server, configuration lock and isolated native tool module | 900 passed, 3 failed, 1 skipped; 35.87 s. The three findings were closed separately below. |
| Isolated native tool gates plus source-code guard, final | 19 passed, no skips; 0.62 s. |
| Native provider SDK checks, isolated from collection stubs | 9 passed, no skips; 4.85 s. |
| React frontend | 359 passed in 54 files, no skips; 3.47 s. `tsc --noEmit` + Vite build passed (6.23 s wall). |
| Desktop renderer | 114 passed, 1 skipped in 10 files; 0.888 s. Typecheck and build passed (1.05 s / 0.57 s wall). |
| Desktop Rust, Linux | 128 unit + 48 CLI-contract + 39 real-CLI-contract = 215 passed, no ignored tests. Compilation 12.43 s; CLI suites 2.01 s / 1.21 s. |

## Demonstrated fixes

1. Register the existing `security` pytest marker. `--strict-markers` previously
   prevented collection of ten security modules, before any test ran.
2. `create_app()` establishes the state directory through the existing
   `configuration_lock` **before** repositories initialize. Under a group-writable
   umask, earlier repositories created it as 0775 and a later protected store
   correctly refused startup. New state is now 0700; an existing unsafe directory
   still fails closed, is not chmod'ed, and receives no database. The locking
   authority and checks are unchanged.
3. Security test inventories traverse FastAPI lazy included routers and preserve
   nested prefixes. FastAPI 0.141 otherwise made the old structural sweep see only
   12 direct HTTP routes and no WebSockets. Actual HTTP/WebSocket authorization
   remains tested against the assembled app, including nested-route regression.
4. Factory-warning tests isolate model resolution and engine construction, instead
   of accessing a real missing vault/profile and swallowing every exception before
   reaching the assertion. Production admission is not patched or relaxed.
5. Two native memory/clarify tests now supply and close a live engine loop. They
   previously omitted it and the production write boundary correctly denied the
   request before the mocked broker bridge. Native tools, broker call counts and
   no-direct-effect assertions remain intact; no engine code changed.

## Environment, false starts and remaining limits

- Python 3.12 with the real editable Hermes Agent 0.21.1 from the existing
  `hermes-ci-dryrun/venv`; always `PYTHONPATH=src` first. Canonical `.venv` had no
  pytest. A private scratch dependency overlay supplies Textual and the image's
  exact optional SDK pins (`mcp==2.0.0`, `anthropic==0.87.0`, `boto3==1.42.89`,
  `azure-identity==1.25.3`). Six initial backend failures were missing MCP, not
  transport regressions. The first isolated SDK run lacked those optional SDKs;
  its corrected nine assertions pass. No shared environment was modified.
- Canonical frontend `node_modules` lacked Base UI. The initial attempt could not
  load 11 suites; `npm ci --ignore-scripts` in the isolated snapshot fixed the
  environment. The table's 359 tests use the exact lockfile, Node 24.13.1.
- A Mac tar transfer generated AppleDouble `._*.py` metadata in the scratch copy;
  a source guard detected it as non-UTF-8. Only those enumerated scratch artifacts
  were removed. They were never repository source; subsequent transfers use scp.
- Nineteen backend skips: two cross-repository integration fixtures not mounted;
  two old spec-003 wizard modules absent; two old protocol definitions absent;
  seven Landlock templates requiring parameters; gitleaks binary absent; the
  release-only manifest-key check not enabled; and four collection-time skips
  caused by legacy tests injecting global `tools`/`hermes_cli` stubs. The latter
  are separately covered by the isolated SDK/native-gate results above. The global
  test-stub pattern itself was not refactored in this release check.
- The 250 deselections are the repository's explicit opt-in integration, network,
  LLM, Chromium, external OCR, VM and OpenShell markers. No live integration or
  final-image certification is implied. `SAFENT_RELEASE=1` manifest verification
  still belongs to the real release gate, not this source regression.
- Desktop renderer's one skip checks tracked generated assets through `.git`;
  `git archive` intentionally has no Git metadata. Generated-asset existence,
  renderer build and Rust contract checks did run.
- Linux Rust success does not resolve or relabel the separately documented macOS
  host Mach-O execution limitation. No macOS binary or final image was built here.
- New small inventory/regression files pass Ruff and `git diff --check` passes.
  Existing large shell-server/legacy test files still have pre-existing lint
  findings; no repository-wide clean-lint claim is made.

## Reproduction

Use a fresh archive of the source SHA, install frontend dependencies with its
lockfile, and provide the existing pinned Hermes checkout plus image SDK pins.
Do not point test databases at an installed Community profile.

```sh
PYTHONPATH=src:<private-dependency-overlay> <hermes-venv>/bin/python -m pytest -q
cd frontend && npm ci --ignore-scripts && npm test -- --run && npm run build
cd desktop && npm test && npm run typecheck && npm run build
cd desktop/src-tauri && cargo test --locked
```

Frontend/renderer commands used `NODE_OPTIONS=--no-experimental-webstorage`.
Logs remain in the isolated DGX snapshot: `backend-final.log`,
`frontend-final.log`, `frontend-build.log`, `provider-sdk-final.log`,
`focal-final.log`, `affected-final.log`, and `native-gate-final.log`.
