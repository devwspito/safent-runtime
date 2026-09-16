# LLM lifecycle — implementation contract and checkpoints

Status: implemented and regression-verified; **managed execution remains
closed**. This is a tested lifecycle foundation, not certification of the full
production daemon launch or permission to enable inherited inference.

## Verified starting point

- Pinned image `ghcr.io/devwspito/safent:v0.9.0-rc2`, image ID `365e584d7f5c`,
  Hermes 0.21.1: inspected `_publish_runtime_main` and `set_runtime_main` in a
  disposable `podman run --rm --network none` process. Native Hermes updates both
  context-local routing and legacy process-global auxiliary credential mirrors.
- `managed_llm_profile.py` already builds a secret-free native profile with one
  gateway/model, explicit auxiliary routes and no personal fallbacks. Its current
  environment helper is for inference tests, **not** a full daemon launch contract.
- `NousReasoningEngine.run_cycle` executes the synchronous native conversation
  using `loop.run_in_executor`. Cancelling the asyncio future does not stop the
  running Python thread, including a stalled upstream request.
- Existing daemon SIGTERM closes intake and cancels async tasks after 5 seconds.
  `asyncio.run` still waits for executor shutdown. Existing systemd unit uses
  `Restart=always` and `KillMode=control-group`, without an explicit stop deadline.
- Existing `managed_native_profile_matrix.py` supplies fresh-process native chat
  and auxiliary routing tests, personal-decoy positive control, 401/402/429,
  timeout and cancellation cases. These do not yet prove production lifecycle.

## Authority and lifecycle contract

1. One corporate identity per **whole daemon process**, never personal and
   corporate profiles concurrently in the same interpreter. Keep the existing
   daemon, native resolver, GovernedAIAgent and jaula; no worker service or second
   inference SDK/resolver.
2. Persist a secret-free desired generation and booted generation alongside the
   existing SQLite state. Derive desired authority from the current signed policy
   and association while holding `configuration_lock`: instance, tenant, signing
   key identity, origin, policy digest/version and state. Do not copy tokens into
   generation state, config files, environment, subprocess arguments or logs.
3. Reconcile the current source of truth at startup, on admission and while idle.
   A crash before the restart request is recorded must be recoverable by deriving
   the desired generation again. A pending or failed restart cannot authorize
   local fallback. Persistence failure denies admission.
4. Bootstrap before native Hermes imports: obtain a clean environment and a
   private corporate profile; retain only explicitly needed daemon infrastructure
   settings. No inherited provider variables, `.env`, OAuth stores, Python paths,
   proxies or personal hooks. Use the same daemon executable and revalidate the
   desired generation after clean exec; an environment flag alone is not proof
   of fresh-process initialization.
5. Admissions must match that process's booted generation and current authority.
   Detecting any mismatch permanently closes that process's admission latch.
   Revoke, rotation, different assignment and unpair require a fresh process;
   changing back to an earlier assignment cannot reopen the old one. Revalidate
   immediately before constructing/invoking the native agent, not only at UI time.
6. Request existing daemon shutdown and durable restart, not a tool-driven
   `systemctl` operation. During drain use native interruption where available;
   a bounded hard process exit is still required for stuck inference threads.
   The existing service manager must reap the complete cgroup before replacement.
   No real user services are restarted in tests.
7. Gateway authorization remains server-side on every request. Local lifecycle
   limits stale process activity but does not claim to undo already executed
   remote operations or delivered tokens.

## Implemented surface

- `src/hermes/runtime/managed_llm_profile.py`: durable private profile preparation
  and explicit full-daemon environment projection.
- New `src/hermes/runtime/managed_llm_lifecycle.py`: generation reconciliation,
  boot evidence, admission latch and restart intention; reuse existing SQLite
  database and cross-process configuration lock.
- `src/hermes/runtime/__main__.py`: earliest bootstrap and existing shutdown loop.
- `src/hermes/runtime/nous_engine.py`: admission and interruption boundaries only.
- `src/hermes/runtime/managed_llm.py`: verifies the current association trust
  fingerprint as well as scope; existing managed execution gate remains closed.
- Existing runtime systemd unit: explicit bounded stop contract, subject to
  integrator approval; no new unit or production restart.
- New unit/process and disposable-image lifecycle tests; this report.

### Concrete guarantees in this cut

- `managed_llm_lifecycle` shares the existing SQLite database. Configuration
  writers record generation transitions in their own transaction. A revoke and
  restore between polls still invalidates the old generation. Startup reconciles
  older writers and failed notification; boot acknowledgement uses a fresh CAS.
- Policy metadata binds the verified signer and Enterprise origin. Rotation
  blocks the old binding until a policy verified by the current key is applied;
  replay/version protection is not reset by rotation.
- `managed_llm_bootstrap.initialize_process` is metadata-only before confinement.
  It does not decrypt the vault, call a profile factory, import native Hermes,
  acknowledge boot or install admission. The existing daemon clean-execs itself,
  passing only a sealed Linux memfd receipt with PID/generation/profile/hash.
- `_run` preserves the existing Landlock ordering. Only after that succeeds does
  `complete_process_bootstrap` revalidate authority, construct the secret-free
  native profile, acknowledge the generation and install admission. Failure
  leaves admission unavailable and restart pending.
- Private profile directories are fresh for each boot, owned by the daemon and
  mode0700; config is exclusive/no-follow mode0600. Tokens are not written into
  profile/config/environment/argv/receipt/generation metadata. The existing vault
  remains the source of the scoped inference credential after confinement.
- The process latch never reopens after a mismatch, including after unpair or
  returning to an earlier configuration. No-policy/local fallback checks the
  process latch too. A freshly booted revoked process can remain idle/manageable
  without spinning through restarts, but cannot admit inference.
- `check()` does not hold the in-process gate while waiting for the configuration
  lock or DB. Idle monitoring and async turn admission use the existing executor,
  preserving event-loop responsiveness and ContextVars. Logical `close()` never
  invokes native callbacks; no DB-path/mode mismatch can hang on hard_interrupt.
- Shutdown arms an independent daemon-thread deadline **before** invoking native
  interruption. A stuck SDK thread or stuck interruption callback results in
  whole-process exit75 after8s. Existing systemd uses Restart=always and
  KillMode=control-group, now with TimeoutStopSec=10 and SendSIGKILL=yes. The unit
  was not deployed or restarted in any real service.
- Native interruption registration lasts until the actual executor worker exits,
  not until its asyncio await finishes. Cancelling that await cannot unregister
  a still-running SDK thread. Cancellation before admission completes prevents
  invocation; later shutdown can still interrupt an orphaned running call.

### Evidence and exact reproduction

- DGX host `/usr/bin/python3`, Python 3.12.3 / pytest 9.0.2. Initial focus of
  lifecycle/bootstrap/deadline, prior lock/profile/gateway/association and native
  compatibility plus cancellation/rebinding and native-import negative case:
  **127 passed in 10.53s**, log `focus-lifecycle.log` in the final snapshot.
  Final full: **5816 passed, 19 skipped, 64 deselected, 7 warnings in 246.87s**.
  Skips are the existing host gaps (native Hermes absent on the host, Composio
  host SDK drift, missing legacy spec003 fixtures, parameterized Landlock
  templates, absent gitleaks and release-only gate). Native Hermes and the pinned
  Composio image are distinct test environments; this result does not silently
  relabel those skips as passes. Warnings include the existing pytest fixture/
  Pydantic/runtime coroutine warnings and the explicit fork-with-threads test.
- The first full selection returned 5791 PASS and 2 failures in bootstrap fixtures:
  earlier tests leave native stub modules in `sys.modules`. Product bootstrap
  correctly refused this non-clean interpreter. Unit fixture now isolates and
  restores those stubs; an explicit native-import negative case preserves that
  security assertion. No product check was weakened to pass the suite.
- Final isolated snapshot `/tmp/safent-lifecycle-final.9NNxQ7` is archive of
  `0c1188a0d790dd53e54b68e710b70c87c138651a` plus this block's owned files only.
  No canonical DB, service or unrelated working-tree changes were copied.

```sh
ssh DGX-remote 'cd /tmp/safent-lifecycle-final.9NNxQ7 && PYTHONPATH=src /usr/bin/python3 -m pytest tests/unit tests/tasks -q -rs --tb=short'
```

- Native fixture image `ghcr.io/devwspito/safent:v0.9.0-rc2`, ID365e584d7f5c,
  Hermes 0.21.1. `tests/integration/managed_lifecycle_bootstrap_smoke.py` performs
  a genuine exec, uses the existing resolver and real GovernedAIAgent/SDK, and
  serves only an HTTPS loopback gateway. Success exits0; revocation during a
  deliberately stalled request exits75; a fresh replacement reports blocked.
  Final clean run (including the actual worker-lifetime helper): **PASS,
  6 requests, production_gate=closed**.
- The fixture's execution entrypoint is test-specific after exec; it does not
  start real D-Bus/systemd services or pretend to prove `_run`'s complete launch.
  Its external boundary is a disposable `--network none` container. Its fake CA
  is added to certifi **and** the OS trust store only inside that writable image
  layer. An initial certifi-only fixture failed native TLS; no product TLS checks
  were weakened, no verify=False/env bypass was introduced. All keys/prompts are
  fictional and no host state or credentials are mounted.

```sh
ssh DGX-remote 'podman run --rm --network none --add-host enterprise.fixture.test:127.0.0.1 -v /tmp/safent-llm-lifecycle.GkAoZF:/review:ro --entrypoint python3 ghcr.io/devwspito/safent:v0.9.0-rc2 /review/tests/integration/managed_lifecycle_bootstrap_smoke.py'
```

- Repeated the pre-existing native profile matrix after this implementation:
  **30 cases, 43 corporate requests, 0 personal-decoy requests** after a successful
  native positive control proving the decoy was reachable. Chat plus compression,
  vision, review and memory_query_rewrite each exercise success/401/402/429/
  timeout/cancellation. Negative cases fail on the corporate route, not fallback.

```sh
ssh DGX-remote 'podman run --rm --network none -e PYTHONPATH=/review/src -v /tmp/safent-llm-lifecycle.GkAoZF:/review:ro --entrypoint python3 ghcr.io/devwspito/safent:v0.9.0-rc2 /review/tests/integration/managed_native_profile_matrix.py'
```

Logs: `/tmp/safent-llm-lifecycle.GkAoZF/native-bootstrap-smoke.log`,
`/tmp/safent-llm-lifecycle.GkAoZF/native-profile-matrix.log`,
`/tmp/safent-lifecycle-final.9NNxQ7/full-runtime-lifecycle.log`.

Ruff passes for all new modules/tests plus modified `managed_llm.py`,
`managed_llm_profile.py` and `association_store.py`; `git diff --check` passes.
The two large legacy daemon/engine modules retain pre-existing lint debt; this
cut does not claim a clean whole-repository lint run or reformat those files.

Exact owned paths (all relative to the runtime repository):

```text
ops/agents-os-edition/systemd/hermes-runtime.service
src/hermes/instance/association_store.py
src/hermes/runtime/__main__.py
src/hermes/runtime/managed_llm.py
src/hermes/runtime/managed_llm_profile.py
src/hermes/runtime/managed_llm_bootstrap.py
src/hermes/runtime/managed_llm_lifecycle.py
src/hermes/runtime/shutdown_deadline.py
src/hermes/runtime/nous_engine.py
tests/unit/test_managed_llm_bootstrap.py
tests/unit/test_managed_llm_lifecycle.py
tests/unit/test_runtime_shutdown_deadline.py
tests/integration/managed_lifecycle_bootstrap_smoke.py
docs/llm-lifecycle-2026-09-11.md
```

### Explicit remaining release blockers

1. Full real daemon launch under its actual systemd/Landlock/D-Bus confinement,
   clean exec, READY/watchdog, policy change, stop, cgroup reaping and replacement
   admission has **not** been certified. Unit ordering checks and disposable
   native subprocess evidence are not a substitute.
2. The production `_build_governed_agent` currently adds local Qwen
   `chat_template_kwargs` to extra_body. Before opening managed execution, its
   managed request must be tested against the Enterprise gateway's closed schema
   without blindly sending local-only fields. This cut does not change the gate.
3. Native tool/MCP/plugin configuration for a corporate profile must be validated
   with the actual governed manifests and startup environment. This cut copies
   no personal plugins or tool configuration to claim false compatibility.
4. Profile directories are fresh and retained; safe bounded retention/cleanup
   after process exit is still required for long-running deployment. No broad
   directory deletion was added to this security cut.
5. Linux sealed descriptors are required for managed bootstrap. Cross-process
   configuration locking is POSIX/local-filesystem scoped; this is not a claim of
   Windows runtime or NFS/distributed-lock support.
6. Previously exported upstream keys in older Community bundles/backups cannot be
   erased retroactively by this code. Rotate those master keys when migrating.

Do not turn the production gate into an environment flag to bypass these checks.

## Acceptance checkpoints before opening execution

- Real SQLite generation races, crash recovery and admission fail-closed tests.
- Revoke/unpair/rotation invalidate old processes permanently, no personal fallback.
- Secret-free config/environment/argv and profile path/link permission checks.
- Fresh real native GovernedAIAgent in the pinned image through the new bootstrap,
  with gateway fixture and proven reachable personal decoy receiving no requests.
- 401/402/429/timeouts/native cancellation remain on the corporate gateway.
- Controlled start/stop/restart fixture proves no lingering inference thread or
  child can remain active when a replacement process admits work.
- Existing LLM suites and full runtime regression on an isolated DGX snapshot.

Until those checkpoints pass, the current execution gate stays closed. A ready
profile or persisted restart intention alone is not a completed live feature.
