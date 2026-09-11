# Native turn result classification — 2026-09-12

Status: implementation, focused/native contract, full regression and refreshed
confined guest tests pass. Production managed inference gates remain
closed. This fixes result semantics, not credential isolation certification.

## Evidence and change

The original native guest diagnostic caught two false successes: a revoked idle
request returned interruption text and was marked completed; an exhausted TLS
failure also became a completed chat. The native SDK already provides structured
flags. Its `completed` flag can be true together with `interrupted=true`.

| Before | After | Why |
|---|---|---|
| Any nonempty final_response became normal chat | Structured result validated before stream-end/output mapping | Error prose is not successful execution evidence |
| Interrupted SDK result could complete | interrupted=true raises OperationCancelled | Cancellation is terminal and never retried |
| Provider error dictionary ignored | Shared domain NativeTurnFailedError, static safe messages | No provider body, credential, URL or prompt echoed in UI/audit |
| Every failure retried until max attempts | Optional retryable=False forces FAILED without changing attempts | Auth/billing/config errors must not burn more calls |
| Stale mark_failed UPDATE0 silently returned another state | CAS mismatch raises ClaimTokenMismatch | Do not emit failure evidence for a lost claim |
| Incomplete turn could claim narrative success | Genuine broker pending proposals preserved with empty narrative | Human approval remains the only write authority |

`hermes/domain/reasoning_failure.py` is the shared engine/application exception
contract. Application does not import the runtime SDK adapter. All preexisting
queue callers retain retryable=True by default. Both SQLite and in-memory queues
enforce strict boolean input and keep actual attempt counts unchanged.

## Actual Hermes0.21.1 contract, not mocked SDK

Executed pinned RC2 `365e584d7f5c1396db6943087d439409b811c166d862351e0dfe3f1343786f0a`,
unprivileged `--network none`, fresh fictional profile per case, loopback HTTP.
`native_turn_result_smoke.py` calls the actual Nous `_build_governed_agent` factory
and real SDK. No managed gate monkeypatch, external account or host secret.

| Case | Native fields observed | Bridge result |
|---|---|---|
| success | completed=true, failed=false, interrupted=false | completed |
| 401 | completed=false, failed=true; **no failure_reason** | safe generic failure, no retry |
| 402 | completed=false, failed=true, failure_reason=billing, failure_retryable=false | billing failure, no retry |
| 429 | completed=false, failed=true, failure_reason=rate_limit, failure_retryable=true | retryable failure |
| interruption | completed=true, failed=false, interrupted=true, turn_exit_reason=interrupted_during_api_call | cancelled |

Missing completed, non-boolean flags, malformed final_response, error-bearing or
partial results are not inferred successful from prose. No localized string matching.
401 cannot be labelled more specifically without upstream structured evidence;
the bridge deliberately does not parse or expose the provider message.

## Verification

- Focus: **85 passed, 17 deselected** for new classifier/real orchestrator + task
  queues, including persistence/restart, incorrect claim, race after snapshot,
  unchanged attempts, no completed/CHAT_REPLIED audit, safe persisted error,
  and pending approval separation. A later typed-proposal test refinement retains
  the same number of cases; full final run is authoritative below when recorded.
- Native five-case matrix: **all five passed**.
- New files Ruff clean; preexisting large Nous/orchestrator files have existing
  lint debt; no unrelated bulk formatting or fixes were applied.
- Full scratch: `/tmp/safent-native-result-full.eBrQ5d`, archive HEAD62075c9 plus
  explicit owned overlays, isolated SQLite. Log `full.log`: **5937 passed,
  19 skipped, 64 deselected, 6 warnings in251.15seconds**. Python3.12.3/pytest9.0.2.
  Skips explicitly include unavailable host Hermes/Composio, missinggitleaks,
  release-only manifest gate and parameterized Landlock templates; not hidden
  product passes. The real Hermes native contract is covered in RC2 separately.

```sh
PYTHONPATH=src python3 -m pytest tests/unit/test_native_turn_result.py tests/tasks/test_work_queue.py tests/tasks/test_agent_loop.py -q --tb=short
PYTHONPATH=src python3 -m pytest tests/unit tests/tasks -q -rs --tb=short
podman run --rm --network none -v /tmp/safent-native-result.Q32Bx6:/review:ro --workdir /review --entrypoint python3 -e PYTHONPATH=/review/src 365e584d7f5c1396db6943087d439409b811c166d862351e0dfe3f1343786f0a tests/integration/native_turn_result_smoke.py
```

Updated guest now verifies native idle revocation persists CANCELLED after restart;
see `llm-native-guest-revocation-fix-2026-09-12.md` for pinned wheel/source evidence.
This does not replace pending auxiliary/tool isolation, real Enterprise transport,
or final outer-container delivery proofs. No production gate is opened.
