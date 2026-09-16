# Runtime 0.9.14 — immutable Python base suite

## Result

**7572 passed, zero failed, 21 skipped, 250 deselected, eight warnings**.
Pytest: **466.28 seconds**; wall time: **468.64 seconds**; command exit: **0**.

- Runtime source: `c3c91b5638b344d200ec39760f093cd9084a2115`, version 0.9.14.
- Ads source: `d66ba48b76c5aaefc52d916685ad98281145aca0`, separately archived.
- Snapshot: `/tmp/safent-python-c3c91b5.i2Dl7n` on DGX.
- Log: `/tmp/safent-python-c3c91b5-full.log`.
- Interpreter: `/tmp/safent-python-global.gGvi7u/venv/bin/python` (Python 3.12.3).

The snapshot includes the final loopback CSRF-cookie correction, canonical
SPA reload allowlist, restore failure diagnostic, calendar focus correction
and 0.9.14 metadata. No source changed during the run. Frontend and Rust
suites were not repeated as part of this task.

## Reproduction

From the immutable snapshot directory:

```sh
env -i \
  HOME=/tmp/safent-python-c3c91b5.i2Dl7n/test-home \
  HERMES_HOME=/tmp/safent-python-c3c91b5.i2Dl7n/test-home/.hermes \
  PATH=/usr/local/bin:/usr/bin:/bin LANG=C.UTF-8 PYTHONPATH=src \
  HF_HUB_OFFLINE=1 \
  SAFENT_ADS_REPO=/tmp/safent-python-c3c91b5.i2Dl7n/ads-source \
  /usr/bin/time -p /tmp/safent-python-global.gGvi7u/venv/bin/python -m pytest -q
```

No credentials or advertising calls were configured. A secondary SSH
progress query briefly timed out. The original run completed normally and
returned its full summary and exit 0; a later independent log read confirmed
the same result. No retry or duplicate full suite was launched.

## Explicit exclusions and skips

This is the complete **configured base suite**, not all opt-in system tests.
The unchanged `pyproject.toml` selection excludes 250 tests marked
`requires_chromium`, `requires_llm`, `requires_external_ocr`, `integration`,
`requires_network`, `requires_vm` or `requires_openshell`.

The 21 skips, unchanged in category from the preceding baseline:

| Count | Reason / source |
| --- | --- |
| 2 | Explicit cross-repository managed Ads child-schema / Enterprise LLM contract fixtures absent at collection. |
| 2 | Legacy first-boot wizard contracts absent (`test_first_boot_wizard`, `test_wizard_conversation`). |
| 2 | Unmigrated `ReplayPreviewPort` / `WorkspaceLifecyclePort` protocols. |
| 4 | Provider-SDK import and memory/clarify tool tests affected by suite-global SDK stubs; emitted reasons say unavailable/not installed. |
| 7 | Landlock templates require parameters: screen, system_services, system_info, udev_devices, audio_devices, scheduler, input_control. |
| 1 | gitleaks binary not on PATH. |
| 2 | Real projection-volume QA requires explicit isolated-engine opt-in. |
| 1 | Runtime manifest public-key release gate requires `SAFENT_RELEASE=1`. |

Eight warnings cover the existing Starlette/AnyIO deprecation, AgentDraft
field shadowing, two unawaited mock-coroutine reports, an async marker on a
sync test, two duplicate OpenAPI IDs and fork in a multithreaded process.
The full log preserves their exact locations.

## Boundaries

No tags, version bumps, images, Mac installation or user data were modified
by the suite. This result does not replace signed macOS/WKWebView acceptance,
real-provider/OAuth consent, VM certification or the excluded integration
tests. The earlier 0.9.13 red run and fixture-only green rerun remain recorded
in `docs/runtime-python-ed41934-2026-09-13.md`; they are not relabelled as
0.9.14 evidence.
