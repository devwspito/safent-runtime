# Native guest: real revocation now persists CANCELLED — 2026-09-12

**Bounded diagnostic PASS; production execution gates remain closed.** This
supersedes the interrupted-task false-COMPLETED defect observed in the prior
guest report, not its remaining release boundaries.

## Exact tested inputs

- Guest scratch `/tmp/safent-managed-guest.I99YPi`; fresh sparse copy of the
  preserved gate-intact disk from `/tmp/safent-managed-guest.tdTjqe/gates-closed/runtime.raw`.
  Same verified RC2+official Ubuntu kernel/modules/kmod provenance as the prior report.
- Full archive base **62075c948f4e0640b8ddbe1274137a4defd886ad**, including root's
  lifecycle authority revalidation12479fb, plus the exact classifier/queue
  overlays subsequently integrated as **ff6bf5e**.
- Wheel SHA256 **2720c22e924be39bb049892602c66ec2f06df538d482723f965d826795328f59**;
  built offline in unprivileged RC2 from the same snapshot that passed the full suite.
- `input-manifest.json` records all **638 Python source hashes**, explicit overlay
  hashes, wheel hash, and harness hashes. Runner validates these before QEMU;
  source revision is an input obtained from actual `git rev-parse`, not a
  hardcoded attribution. The optional diagnostic installer stops the old daemon,
  installs only the pinned wheel offline and verifies all638 installed source
  hashes before proceeding. This avoids silently testing the previously installed wheel.
- The two constant execution gates are still substituted **only in this guest**;
  before/after SHA256 and exact unified diffs are included in the report. Resolution,
  admission, real runtime factory, systemd, D-Bus, Landlock and queue remain real.

## Observed results

Full guest cycle **99.12seconds**, actual KVM, 2CPU/4GiB, no NIC or host mounts.
`updated-diagnostic-report.json` and `runtime-{command,kvm,serial,qemu}.*` retain evidence.

| Check | Observed |
|---|---|
| Native inference | Actual Nous factory → Hermes0.21.1 → loopback TLS/SSE; success task persisted completed |
| Credentials/model | Only fictional delegated token + model company; no personal fallback |
| Profile | Directory0700/config0600, UID880, no delegated token in config |
| Active request revocation | Native `stream_interrupt_abort`, tcp_force_closed=1 |
| Process transition | PID548 → managed680 → blocked772 |
| Runtime cgroup | Old PID680/starttime2097; no matching process survived replacement |
| Revocation → new READY | 9.24seconds |
| Durable lifecycle | generation13, mode blocked, booted_generation13, restart_pending0 |
| Interrupted task | 4233f0f9-1a2d-4683-a0c6-8f4c4d75f3cc persisted **cancelled** after restart |
| Safe reason | “La autorización del motor cambió o ya no se puede verificar.” |

The old false-COMPLETED observation is not erased: it remains in the prior report
as the failing reproduction. This run uses the fixed source and asserts CANCELLED.
The final QEMU exited, and its recorded host PID was verified absent. No service
on the host was restarted, no real account used and no cloud resource changed.

## Verification and replay

- Classifier/queue full: **5937 passed, 19 skipped, 64 deselected**, 251.15seconds,
  `/tmp/safent-native-result-full.eBrQ5d/full.log` (Python3.12.3/pytest9.0.2).
- Real native SDK result matrix: success/401/402/429/interruption, **5 passed**,
  actual factory in RC2 with network disabled and loopback fixture.
- Updated harness safety: **14 passed**, including source/wheel mutation detection,
  manifest overwrite rejection and report failure detection. Ruff and bash syntax pass.

For a new source snapshot, record the actual full revision at archive creation,
build its wheel offline, then on the **new disposable copy**:

```sh
python3 harness/build_bundle.py /tmp/safent-managed-guest.I99YPi --source-revision 62075c948f4e0640b8ddbe1274137a4defd886ad --record-inputs-only --overlay src/hermes/domain/reasoning_failure.py --overlay src/hermes/runtime/native_turn_result.py --overlay src/hermes/runtime/nous_engine.py --overlay src/hermes/tasks/application/agent_loop_orchestrator.py --overlay src/hermes/tasks/domain/ports.py --overlay src/hermes/tasks/domain/work_item.py --overlay src/hermes/tasks/testing/in_memory_work_queue.py --overlay src/hermes/tasks/infrastructure/sqlite_work_queue.py --overlay tests/unit/test_native_turn_result.py
python3 harness/update_probe.py /tmp/safent-managed-guest.I99YPi diagnostic wheel
python3 harness/run_guest.py /tmp/safent-managed-guest.I99YPi runtime
```

These paths already contain evidence; commands deliberately refuse overwriting
manifests/logs. Use a fresh private scratch/copy for a repeat, preserving the
original signed/checksummed inputs. No host disk mounting is required.

## What this does NOT prove

No generated tool call or subprocess ran in this scenario; cgroup membership
contained the runtime process with its native threads only. Independently
launched MCP/browser services are not covered by that assertion. Still required:
actual governed tool/subprocess work, auxiliary credential/process isolation,
real Enterprise gateway integration in the guest, and final outer-container
seccomp/delivery validation. The guest clock remains the baked-image floor with
fictional signatures/certificates consistently using guest time, not a proof of
production NTP. Generic unexpected exceptions retain prior logging behavior;
the new sanitization contract covers structured native turn failures only.

**Neither successful inference nor CANCELLED authorizes opening production gates.**
