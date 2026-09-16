# Real native tool revocation — fixed and guest-verified, not release certification

## Reproduction before fix

The diagnostic guest uses a local TLS/SSE fixture to return a `terminal` tool
call. The actual `NousReasoningEngine` factory, native Hermes SDK, native hooks,
governed terminal routing, SO_PEERCRED launcher, systemd transient unit and jail
are unchanged. The two managed-execution gates are substituted **only inside the
disposable guest**, using the manifest described in the preceding boot reports.
Production gates remain closed. This is not an outer-container delivery test.

DGX scratch `/tmp/safent-managed-guest.I99YPi`, first tool run: **FAIL, 68.66 s**.
Product input is the pinned `62075c948f4e0640b8ddbe1274137a4defd886ad` archive
plus classifier overlays now integrated in `ff6bf5e`; wheel SHA256
`2720c22e924be39bb049892602c66ec2f06df538d482723f965d826795328f59`.
The input manifest contains every source, overlay and harness hash. Previous
successful inference-only evidence is preserved under `revocation-pass/`.

Observed, before cleanup:

- Native terminal created `hermes-exec-79c570d07c0e0aff.service` in
  `/agents.slice/agents-os.slice/agents-os-exec.slice`.
- Payload PID730/starttime5142 and child PID732/starttime5196 ran as
  `hermes-sandbox` (UID886 in this image).
- Opening the master key, state DB, daemon `/proc` paths and privileged exec
  socket was denied. A connection to a real listener in the **guest's root
  network namespace** was denied. No secret bytes were read, and the guest had no
  NIC or mount/socket access to the DGX host.
- Signed revocation replaced daemon PID669 with PID763, reaching actual READY
  in9.20s, blocked generation18, restart intent cleared.
- The task was correctly persisted `cancelled`, not completed.
- **Both exact tool PID/starttime identities survived the replacement.**
  The unit had `KillMode=control-group`, but no lifecycle binding and a65s
  runtime timeout. Waiting for that timeout is not revocation.
- Failure was recorded before stopping the exact verified fixture unit.
  The VM powered off. The host runner returned failure despite QEMU exiting0.

Evidence: `tool-red/runtime-serial.log` → `SAFENT_GUEST_REPORT.tool_partial`, with jail
verdicts, full transient-unit properties, PID identities, task state and timing.

## Cause and implemented boundary

`ops/agents-os-edition/scripts/hermes-exec-launcher` waits synchronously for
`systemd-run`; it does not observe caller disconnection or daemon replacement.
The transient unit has its own cgroup, so killing the runtime cgroup does not
kill tool descendants. A simple pre-launch MainPID check is insufficient against
the admission→restart→late-unit-start race. A dependency that implicitly starts
the runtime is also inappropriate for a stale request.

The launcher now checks the SO_PEERCRED PID/UID against the active runtime's
MainPID and Linux process start time, retains a pidfd and monitors that pidfd
plus the original connection. Missing pidfd or ambiguous systemd state denies
execution. The two repository callers are the native Nous bridge and
`TerminalSurfaceAdapter`, both in the runtime process; neither half-closes the
connection before receiving its response. Half-close or additional input after
the one request is therefore rejected rather than treated as another request.

A fixed Python guard runs unprivileged inside the **same transient unit and
unchanged jail**, with `-I -S`. It signals readiness, then waits for a private
stdin byte before `execv` of the supplied argv. The launcher rechecks the exact
original runtime and connection after guard startup before releasing that byte.
A late unit cannot borrow a replacement runtime's service name as authority.
The guard source, dependencies, private pipe and properties are server-owned;
none are parameters available to a tool/model. This is not another executor or
provider resolver.

`Requisite` + `After` + `PartOf` reference `hermes-runtime.service`. Unlike
`BindsTo`, the selected prerequisite does not implicitly activate an inactive
runtime. The pidfd/connection monitor covers unexpected daemon death, while
`PartOf` propagates explicit service stop/restart. The internally generated unit
uses `KillMode=control-group`, `TimeoutStopSec=2s`, `SendSIGKILL=yes`. Cleanup
addresses only its exact validated unit name. Captured output is drained while
retaining at most256KiB per stream; raw internal exceptions are not returned.

Systemd dependency semantics were checked against the
[official systemd unit documentation](https://github.com/systemd/systemd/blob/main/man/systemd.unit.xml).

## Final verification

One final guest replay after the fix: **PASS, 68.22s total**, same isolated2CPU/
4GiB KVM guest, no NIC, no host mounts or sockets, same native engine and wheel.
The launcher is an explicit additional overlay outside the Python wheel:
SHA256 `29f9104cbaa3c18d5ce850fab2b359b8cf83e9587160b936417970b899c71302`.
The offline updater verifies the source against the input manifest and reads
back the installed bytes before booting; it never changes the host launcher.

- Actual unit `hermes-exec-48fd6b479496a152.service` had the expected effective
  Requisite/PartOf, control-group killing and2s stop timeout.
- Payload PID731/starttime5170 and child PID735/starttime5225 both ignored
  SIGTERM. Both exact identities were absent **before fixture cleanup**.
- Signed revocation replaced runtime668→783, READY in8.64s; lifecycle23 was
  blocked, booted_generation23, no pending restart intent.
- Task `3700d27e-5cb6-4cbb-9a59-8ac7b9af2bbf` persisted `cancelled`, with a
  neutral authority-change explanation, never completed.
- All six secret/process/control/network probes remained denied.
- VM powered off and its recorded host PID was absent. No user service stopped.

Final evidence is `/tmp/safent-managed-guest.I99YPi/runtime-serial.log`,
`SAFENT_GUEST_REPORT.managed_checks`; source/harness/artifact hashes are in
`input-manifest.json`. The failed original run remains under `tool-red/`.

Focal tests: **30 passed in 0.36s**, Python 3.12.3/pytest 9.0.2, scratch
`/tmp/safent-exec-lifecycle.cPaAQp`:

```sh
cd /tmp/safent-exec-lifecycle.cPaAQp
python3 -m pytest tests/unit -q --tb=short
```

This includes16 new launcher tests and14 existing harness tests: actual guard
subprocess, absent authorization, READY→restart→recheck denial, disconnection
between recheck/release, EOF/half-close/extra input, inflight disconnect,
original pidfd death, replaced/unknown systemd state, unsupported pidfd,
bounded output and exact-unit forced cleanup. No test mutates host systemd.
Ruff passes for new tests and harness; the launcher retains eight pre-existing
lint findings outside these changes. `git diff --check` passes. The full runtime
and SDK matrix were not rerun for this launcher-only cut; coordinated integrated
release regression remains a separate check.

## Current test files

- `tests/integration/managed_guest/tool_probe.py`: harmless real tool payload,
  fixed probes, workspace facts and a bounded sleeper child.
- `tests/integration/managed_guest/tool_guest_check.py`: actual tool-call fixture,
  signed revocation, PID/starttime verification, terminal status and15s bound.
- Optional fixture hooks in `guest_probe.py` and `update_probe.py`; never shipped
  as runtime product code. The passing run ignores SIGTERM in both payload
  processes to exercise forced cgroup termination.

## Limits

Revocation is bounded, not a claim of zero instructions after an authorization
change: the final admission recheck/pipe release and process-death observation
are not one kernel transaction. Monitoring checks exact process identity and
service state, then stops the unit; the observed end-to-end bound is8.64s in
this fixture. Previously delivered effects cannot be undone by process killing.

The guest proves this native terminal/launcher path and its descendants, not
all browser/MCP/service auxiliary lifecycles, or the final nested container
delivery/seccomp profile. Linux pidfd and working systemd are required; failure
to inspect them fails closed. A wholly unresponsive system manager cannot be
certified by a unit test. No managed-execution production gate is opened.

No live account, cloud service or host service has been modified.
