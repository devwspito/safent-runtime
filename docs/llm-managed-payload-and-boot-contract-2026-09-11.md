# Managed factory payload and confined boot contract

Status: payload implemented, native/Enterprise boundary and full regression
verified; production managed execution remains unconditionally closed.

## Payload change

`NousReasoningEngine._build_governed_agent` now checks the process admission mode
against `ModelConfig.managed`. Its private `_assert_managed_execution_ready()`
always rejects in production. It accepts no flag, argument, profile field, tool
input or environment variable to enable managed execution.

For the eventually authorized managed path, arbitrary `ModelConfig.extra` is
rejected before native resolution. No local Qwen `chat_template_kwargs` or
`request_overrides` is injected. Explicit `max_tokens` is forwarded unchanged;
an omitted cap remains omitted so the Enterprise grant supplies its authoritative
limit and provider-appropriate field. A larger explicit cap is rejected by the
gateway, not silently increased or converted locally. Local Qwen behavior remains
unchanged, including explicit `enable_thinking=true/false` overrides.

## Executed evidence

- Unit tests traverse the actual factory with only the native constructor mocked:
  factory gate stays closed; a profile cannot enable it; arbitrary managed
  overrides rejected; token omission/16/4096/4097 and local Qwen defaults/overrides.
  Together with existing Hermes0.21 compatibility and provider isolation tests:
  **32 PASS in 0.15s**.
- `tests/integration/managed_factory_gateway_smoke.py` uses the existing sealed
  clean-exec fixture, real private corporate profile, real native resolver, actual
  **NousReasoningEngine factory**, GovernedAIAgent and Hermes0.21.1 SDK. The only
  production-code substitution is the private constant gate, in that disposable
  diagnostic worker. Bootstrap, process authority and native routing are not
  mocked. There is no product activation mechanism.
- The HTTPS loopback receiver calls **Enterprise InferenceService.prepare** with
  actual SQLite org/employee/template/instance/license/provider/grant records.
  It exercises the real closed request schema, credential checks, effective
  assignment and durable quota admission. The upstream response is a stub; this
  is not a full FastAPI-router/transport end-to-end test or an external LLM call.
- Result: **PASS**, success exit0, revocation exit75, replacement blocked;
  6 requests. Real factory sends no Qwen extra fields, uses the scoped credential
  and selected company model. Mutations of that captured payload reject over-cap
  4097, boolean tokens, another model, pairing bearer, revoked grant and local-only
  Qwen field. Omission receives server-owned `max_completion_tokens=4096` for the
  seeded official OpenAI provider. No upstream master key reaches Community.
- Fictional grant IDs/tokens and identities only. Enterprise source is mounted
  read-only; no Enterprise product file was changed. Fixture TLS CA is trusted
  only in the disposable image layer; verification remains enabled.

Runner snapshot: DGX `/tmp/safent-managed-payload.zCOLWM`, runtime archive of
commit4577987 plus the listed changes; Enterprise source under `enterprise-src`.
Image `ghcr.io/devwspito/safent:v0.9.0-rc2`, Hermes0.21.1, no external network.

```sh
ssh DGX-remote 'podman run --rm --network none --add-host enterprise.fixture.test:127.0.0.1 -v /tmp/safent-managed-payload.zCOLWM:/review:ro --entrypoint python3 ghcr.io/devwspito/safent:v0.9.0-rc2 /review/tests/integration/managed_factory_gateway_smoke.py'
ssh DGX-remote 'cd /tmp/safent-managed-payload.zCOLWM && PYTHONPATH=src python3 -m pytest tests/unit tests/tasks -q -rs --tb=short'
```

Logs: `native-factory-gateway-final.log`, `focus-payload.log`,
`full-runtime-payload-final.log` in that snapshot. The first full run had
5827 PASS and one error-message regression: checking bootstrap before the
constant gate changed the existing unavailable error. Reordered to preserve
the always-reject production gate first, without weakening the subsequent
authority check and without changing the existing regression test.

Final full regression: **5828 passed, 19 skipped, 64 deselected, 7 warnings in
242.62s**, DGX Python3.12.3 / pytest9.0.2. Existing host skips remain documented in
the preceding lifecycle report, not treated as passed native/VM coverage. Source
and unit-test SHA256 matched the local files in the tested snapshot. Ruff passes
on this cut's tests; `git diff --check` passes. The existing large engine module
retains unrelated pre-existing lint debt.

Owned paths, all in runtime (no Enterprise product changes):

```text
src/hermes/runtime/nous_engine.py
tests/unit/test_managed_factory_payload.py
tests/integration/managed_lifecycle_bootstrap_smoke.py
tests/integration/managed_factory_gateway_smoke.py
docs/llm-managed-payload-and-boot-contract-2026-09-11.md
```

## Read-only audit: actual boot dependencies

The existing `ops/container/Containerfile` requires systemd as PID1; the daemon
checks actual browser-netns and egress services before allowing operation.
`test_boot_graph.py` parses dependencies and optionally verifies units, while
`test_runtime_entrypoint.py` checks real broker wiring. Neither currently proves
managed clean exec, READY, D-Bus authority, cgroup drain and replacement together.

A bounded preflight was executed in another disposable rc2 `--network none`
container: the image's actual `apply_runtime_landlock("runtime")` returned
`outcome=applied`, and attempting `os.listdir("/boot")` raised PermissionError.
It reported the absent `/lib64` optional rule on this architecture. This proves
Landlock availability/enforcement in that child only, **not** the current source
image's complete systemd service or post-exec import/profile compatibility.

| Required surface | Existing contract | Required observation in isolated boot |
| --- | --- | --- |
| Runtime identity | user/group hermes; supplementary hermes-egress/hermes-work | Actual UID/groups and no added capabilities |
| Kernel confinement | `_apply_runtime_landlock`; `/boot` denial; no degrade flag | Enforcement after exec and before vault/native imports |
| Systemd hardening | ProtectSystem=strict, ProtectHome, empty capability set, seccomp incl. memfd_create | Effective service properties, not merely source text |
| Private state | `/var/lib/hermes`, `/run/hermes`; managed-profiles beneath DB parent | 0700 homes, 0600 secret-free config, owned lock/SQLite state |
| Native imports | Hermes package under `/usr/lib/hermes-agent` | Import succeeds under actual Landlock, no /review/PYTHONPATH bypass |
| D-Bus | org.hermes.Runtime1 on container system bus | Signed ApplyManagedLlmGateway accepted only via existing authorized caller |
| Liveness | Type=notify, NotifyAccess=main, existing READY/watchdog graph | MainPID reaches READY and watcher remains responsive |
| Network helpers | real netns/nftables/egress units | All active and enforcing; no fake systemctl answers |
| Shutdown | deadline8s, systemd stop10s, KillMode=control-group | Old MainPID and descendants gone before replacement admits work |

## Secret-free corporate profile manifest (contract v1)

- Scope: exactly one `(instance, tenant, signer, Enterprise origin, policy digest,
  generation)` per daemon process; only hashes/generation in lifecycle receipt.
- Native model: `provider=custom`, selected company model, HTTPS instance gateway,
  `api_mode=chat_completions`. No upstream API key in profile.
- Every dictionary-valued task in pinned `DEFAULT_CONFIG.auxiliary`: same gateway,
  same model, blank stored key, empty fallback_chain, transient_retries=0.
- Empty fallback_providers/custom_providers/mcp_servers/plugins. Local profiles,
  OAuth stores, `.env`, Python paths and provider/proxy env are never merged.
- Environment is the existing explicit `daemon_environment` projection. HOME,
  HERMES_HOME and XDG paths become fresh private paths; daemon infrastructure
  variables are enumerated, not accepted by a prefix wildcard.
- Tokens are loaded from the existing vault only after confinement and remain
  instance-scoped. A malformed/changed manifest denies admission; config-sync
  does not directly write an unsigned runtime token.
- Before enabling real use, governed tool/MCP manifests must be projected and
  tested through existing broker/launcher boundaries; an empty tool manifest
  does not certify a complete functioning corporate assistant.

## Next executable full-boot proof: isolated guest, not user's services

Use a disposable Linux VM (or a pre-approved dedicated CI worker) with a kernel
supporting Landlock and private cgroups. Do not run privileged host networking,
mount host `/run`, D-Bus, home, canonical DBs or Docker/Podman sockets into the
fixture. The nested systemd container has its own network namespace and ephemeral
state. Do not use `--network host`, `--pid host`, `--privileged`, or Landlock
disable/degrade switches as a shortcut to a passing result.

1. Build an ephemeral image from the current repository so modules live in their
   normal `/usr` paths; record source/image/native-Hermes hashes. First run with
   the production gate intact to prove it still denies inference after READY.
2. In a separately labelled diagnostic image only, replace the same constant gate
   used by the native factory fixture. Install a fictional CA and gateway fixture
   inside the guest. No real credentials, public network or personal state.
3. Seed a fictional signed association using normal fixture provisioning, then
   start the actual existing workspace target inside the disposable container.
   Record systemd effective unit properties, MainPID, cgroup, generation and
   kernel denial evidence; do not replace _run, Landlock or D-Bus with stubs.
4. Apply a signed assignment through the existing authorized D-Bus path. Assert
   the old PID refuses new work and exits, and new PID/profile generation matches
   the signed binding before native inference reaches the fixture.
5. Exercise success, tools/SSE, 401/402/429, idle stream and stuck synchronous call.
   Revoke/change assignment/unpair while running. Assert server-side grant checks
   reject subsequent requests and cgroup has no surviving old workers/children
   before replacement admission. Test a deliberately stuck interrupt callback.
6. Repeat with kill/crash during bootstrap, stale receipt/signature, concurrent
   config-sync/local setter, unavailable vault and unavailable kernel enforcement.
   All failures must remain blocked, never route through a reachable personal
   decoy (first prove its positive control in a distinct process).
7. Stop/remove only the recorded fixture IDs and its ephemeral volumes. Export
   sanitized evidence before cleanup. No `systemctl` invocation targets the host.

This recipe is a concrete execution contract, **not an already executed full-boot
test**. It requires a dedicated guest boundary and a bounded test orchestrator;
do not mislabel the HTTPS factory smoke as systemd/Landlock certification. Safe
profile retention/cleanup and full governed tool-manifest integration also remain
open from the prior lifecycle report. The production gate therefore stays closed.
