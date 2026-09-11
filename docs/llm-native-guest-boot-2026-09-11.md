# Native managed guest boot proof — 2026-09-11

Status: real confined baseline, managed gates-closed assignment/revocation and
diagnostic native TLS inference verified. A real task-status defect remains.
**Neither production inference gate is enabled.** This is
not certification of the final outer-container/seccomp delivery image.

## Isolated boundary and source provenance

- DGX disposable scratch: `/tmp/safent-managed-guest.tdTjqe`.
- Full runtime archive `66da720`; no uncommitted CLI/backup or UI overlays.
- Existing RC2 OCI image ID
  `365e584d7f5c1396db6943087d439409b811c166d862351e0dfe3f1343786f0a`.
  Rootfs export only from our **created, never started** container
  `90cbfc9e0e1ef85010a8f96666c8a6a49f9c3719a509ecc37603730fcf64a5e6`.
- Current-source wheel built offline in unprivileged RC2, SHA256
  `c1f1df5c712ba7c748dd53ab681ad0eb70f04300a23bd125a4ff275a81db57c6`.
- [Official Ubuntu Minimal Noble image directory](https://cloud-images.ubuntu.com/minimal/releases/noble/release/),
  release20260905, `ubuntu-24.04-minimal-cloudimg-arm64.img` (219MiB).
  Image SHA256 `8b6e0e145ae2ce681d959b2b3aabcf724b027cbffff9ec8ba4d7dc789a3e6a98`.
- `SHA256SUMS.gpg` verified with DGX's root-owned Ubuntu cloudimage keyring;
  GPG VALIDSIG **D2EB44626FDDC30B513D5BB71A5D6C4C7DB87C81**,
  UEC Image Automatic Signing Key, signature2026-09-05. Manifest, signature
  and `ubuntu-signature.log` retained. This uses the cloud-image key, not an
  unverified key downloaded beside the artifact.

One QEMU8.2.2 KVM VM at a time, **2CPU / 4GiB**, 20GiB sparse runtime disk,
read-only data/NoCloud ISOs; no NIC, no host directories, 9p, virtiofs, host
sockets or canonical state mounted in the guest. No privileged containers,
host systemd/network changes, apt, external inference, GCP or paid CI.
QMP `query-kvm` independently reports `enabled=true,present=true`. ARM direct
kernel boot reports `systemd-detect-virt=qemu` without firmware SMBIOS; that
label is not used to infer acceleration.

## Reproducible native guest delta

The preparatory Ubuntu VM formats only the disk whose fixed virtio serial and
exact20GiB size match the fixture. It extracts RC2 preserving numeric UIDs,
installs the current wheel without dependencies/network, copies current runtime
unit and D-Bus policy, then copies Ubuntu kernel6.8.0-139-generic and its modules.
Ubuntu Minimal boots without an initrd here, using built-in root disk drivers.

**kmod is an explicit guest-only dependency:** RC2 is a container rootfs and
does not contain `/sbin/modprobe`; its normal container delivery uses the host's
kernel module loader. Native guest autoload consequently could not create
veth, despite `veth.ko.zst` being present. Copying the verified Ubuntu `/usr/bin/kmod`
and its modprobe/depmod symlinks provides the missing native-host function.
Its required libc/crypto/lzma/zstd ABI dependencies are already in RC2. This is
not a product workaround, added capability, fake netns or weakened unit.

The exported container marker is removed only on the new fixture disk and its
machine-id initialized for this guest. Runtime uses its actual entrypoint and
installed `/usr` packages, without `/review`, PYTHONPATH, a custom factory or
Landlock degradation flags. Image-specific macOS/cgroup drop-ins remain visible
in the recorded effective unit: no claim is made that they equal the OS image.

## Executed baseline

`runtime-baseline-report.json` and `baseline/` logs record a **9.05second full
guest cycle**, including successful runtime READY and guest shutdown:

- PID1 systemd; runtime MainPID270, Type=notify, NotifyAccess=main,
  ActiveState=active/SubState=running/Result=success, ConditionResult=yes.
- UID/group hermes; supplementary hermes-egress/hermes-work; empty capabilities;
  ProtectSystem=strict, ProtectHome=yes, NoNewPrivileges=yes, seccomp enabled.
- Actual Landlock ABI4 rules applied; `_run` self-test reported
  `enforcing=True` (`/boot` denied). No replacement of that function.
- Actual browser and MCP netns/nftables setup completed; egress proxy active;
  runtime confinement check passed. No failed systemd units.
- KillMode=control-group and TimeoutStopSec=10 are effective properties,
  **not yet evidence of every in-flight native/revocation case**.

The first negative boot is retained in `runtime-failure-report.json`: missing
modprobe prevented veth, egress stayed down and runtime **refused READY**.
Initial preparatory harness mistakes are retained too: too-long virtio serial
was rejected before formatting, and mandatory-initrd assumption aborted safely.
These failed attempts are not counted as successful product tests.

## Managed proof and remaining boundaries

`managed_checks.py` uses only a fictional association and signed envelopes,
real D-Bus UID resolution and existing apply writer. It waits for the bus owner's
PID to equal the actual runtime MainPID: ServiceUnknown is **not** an ACL denial.
Root must receive a real authorization denial; hermes-user supplies the signed
assignment. Production config-source and factory gates both remain intact.

`managed-final-report.json` records a **65.53second** gates-closed guest cycle:

- Real root D-Bus call denied; `hermes-user` signed assignment accepted as version1.
- Actual daemon PIDs **546 → 677 → 739**; previous MainPID absent before accepting replacement.
- Managed generation6, booted_generation6, restart_pending0; profile directory0700,
  config0600 owned by UID880; no fictional delegated token in the profile config.
- Real enqueued task hit `ManagedProviderUnavailableError` from the unchanged
  execution gate. The queue retained it **pending for retry**, not completed.
  Better classification of unavailable configuration remains a UX follow-up.
- Signed tombstone version2 accepted; replacement READY in blocked generation8,
  booted_generation8, restart_pending0. No personal fallback/inference occurred.

The gate-intact disk and final logs are preserved in `gates-closed/`. A separate
copy runs `diagnostic_check.py`: two exact source substitutions with before/after
SHA256 and unified diff recorded inside the disposable guest. Only constant final
raises are substituted, not policy resolution, admission, D-Bus, systemd or Landlock.
The fixture provides loopback TLS and fictional credentials; **not a real Enterprise
upstream**. This still does not certify tool/MCP manifests, provider-key migration,
retention cleanup, outer container delivery or every shutdown/error/stream scenario.

Direct kernel boot has no externally synchronized RTC: the guest clock starts at
the baked image floor (July28). Signed fixtures and temporary TLS certificates use
guest time consistently. This is not evidence of production clock synchronization.
Earlier ServiceUnknown calls occurred before D-Bus registration even though READY
was emitted. The harness now checks the actual bus owner PID; those calls are not
counted as authorization denials. A first30second queue probe also timed out while
offline audit timestamping was pending; the final bounded60second probe passed.

## Diagnostic inference and release blocker

`diagnostic-report.json` / `diagnostic-first/` record the first **95.51second**
diagnostic run. Actual `NousReasoningEngine` builds `GovernedAIAgent`, Hermes0.21.1
calls the fixture over verified loopback TLS with SSE and function tool schemas.
The requested success task reaches persisted `completed` with no last_error.
Only the fictional delegated token and model `company` reach the fixture; payload
fields are `messages,model,stream,stream_options,tools`. No tool execution is requested.

A second request deliberately stalls before upstream response headers. Signed
revocation invokes the native stream interrupt (`tcp_force_closed=1` in journal).
Daemon PIDs **538 → 668 → 761**; old managed MainPID disappears and blocked
generation13 reaches READY **8.38seconds** after revocation. This proves actual
native interruption and clean replacement, not the semantic success of that task.

**BLOCKER:** native interruption returns a nonempty `final_response` (journal
reason `interrupted_during_api_call`, response length65). `run_admitted_native`
returns it without post-call authority validation; `NousReasoningEngine.run_cycle`
passes it to `_map_result_to_output`, which treats it as normal narrative.
`AgentLoopOrchestrator._handle_chat_narrative_reply` then persists CHAT_REPLIED
and marks the interrupted task **completed**. This is incorrect; the successful
TCP shutdown does not justify a successful task/result. Root is coordinating the
separate fix and regression. These source files were not changed by this harness.

The retained second diagnostic run (`diagnostic-cgroup-attempt-report.json`)
correctly failed because a regenerated fixture CA reused the previous subject;
TLS verification rejected it. The fixture now generates unique CA subjects rather
than disabling verification. That negative run also exposed `completed` after
exhausted native APIConnectionError, with zero accepted TLS requests. Native
`turn_recovery._failed_turn_result` already returns `failed=true,completed=false`;
the bridge discards those flags too. A subsequent cgroup probe rejected the
harness's incorrect `system.slice` assumption; it now checks the exact ControlGroup
reported by systemd (`/agents.slice/agents-os.slice/hermes-runtime.service`).
Neither failed repetition is counted as a passing guest test.

**Final repeated diagnostic: PASS for its bounded lifecycle assertions, not release.**
`diagnostic-final-report.json` and top-level `runtime-*` logs record **95.91seconds**.
Actual unit cgroup membership before revocation contained PID675/starttime1993;
after replacement no member with that PID/starttime survived. PIDs544→675→770,
blocked generation24/booted24/restart_pending0, revoke-to-READY8.38seconds.
The fixture did not launch an additional test subprocess: this proves disappearance
of all observed runtime cgroup members and native threads with their process, not
an untested arbitrary escaping-child scenario. The interrupted task was again
observed as `["completed", null]`, explicitly retaining the semantic release blocker.

Remaining certification: no real Enterprise transport in this guest, no live
provider, no auxiliary/tool credential isolation matrix, no generated tool calls,
no 401/402/429 matrix, no outer-container/seccomp image run. Independently launched
MCP/browser services are outside the runtime cgroup and are not certified by old
runtime MainPID disappearance. Production execution stays closed.

## Commands and files

The SSH transport is `ssh -o ControlMaster=no -o ControlPath=none DGX-remote`.
Preparation inputs must first be verified/exported/built as above; then:

```sh
python3 harness/build_bundle.py /tmp/safent-managed-guest.tdTjqe
python3 harness/run_guest.py /tmp/safent-managed-guest.tdTjqe prepare
python3 harness/run_guest.py /tmp/safent-managed-guest.tdTjqe runtime
```

The host never mounts/formats a disk. `debugfs` reads offline artifacts; the
optional updater changes only named fixture scripts/Ubuntu kmod on an owned
offline disk while retaining a whole-file lock. Logs/manifests refuse overwrite;
prior attempts were moved into explicit attempt directories. QEMU control targets
only this invocation's process and private QMP socket, with bounded deadlines.

Owned source: `tests/integration/managed_guest/`,
`tests/unit/test_managed_guest_harness.py`, this report. No product sources changed.
Ten focused host-runner safety tests passed on DGX; Ruff and shell syntax checks
pass. Those tests are not substitutes for guest evidence.

Final focused command (Python3.12.3 / pytest9.0.2, no canonical DB):

```sh
cd /tmp/safent-managed-guest.tdTjqe/source
PYTHONPATH=src python3 -m pytest tests/unit/test_managed_guest_harness.py -q --tb=short
```

The runner now fails on missing/duplicate/negative reports even when QEMU exits0;
offline fixture updates compare actual read-back bytes because debugfs may return0
on failed operations. Original source execution gates still match repository HEAD.
Two redundant generated attempt ISOs were removed to retain the original
gate-intact disk plus diagnostic copy at ~31GiB total; all reports/logs and inputs
needed to regenerate those ISOs remain. No user data was removed.
Our never-started export container was removed by its exact verified ID after the
export; the original RC2 image and rootfs archive remain. Final QEMU exited and
its recorded host PID is absent. No guest/host background VM was left running.
