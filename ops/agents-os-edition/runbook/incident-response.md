# Agents OS Edition — Incident Response Runbook

Operational playbook for production incidents. Pair with
`specs/003-agents-os-edition/threat-model.md` for context.

## Severity matrix

| Severity | Trigger | Response time |
|---|---|---|
| SEV-1 | Tenant data breach, audit chain tamper detected, cosign signature failed at OTA | < 15 min |
| SEV-2 | OTA rollback automatic triggered, control plane unreachable, mass node enrollment failure | < 1 hour |
| SEV-3 | Single node panel crash, transient WebRTC drop, single agent skill replay failure | < 4 hours |
| SEV-4 | Cosmetic UI issue, single audit publish delayed | next business day |

---

## Playbooks

### PB-01 — Cosign signature failed at OTA verify

**Symptoms**: `bootc upgrade` returns `cosign verify` failure on a node;
`OtaAttempt.rejection_reason = signature_invalid`.

**Immediate actions**:
1. Halt all OTAs on the affected channel:
   ```
   ./ops/agents-os-edition/build/disable-channel.sh stable --reason "PB-01"
   ```
2. Inspect the rejected attempt:
   ```sql
   SELECT * FROM agents_os.ota_update_attempts
   WHERE rejection_reason = 'signature_invalid'
   ORDER BY started_at DESC LIMIT 50;
   ```
3. Check Sigstore Rekor log entry for that image digest:
   ```
   cosign tree quay.io/hermes/agents-os-<profile>:<version>
   ```
4. If the Rekor entry is missing OR the cert identity doesn't match
   the expected GitHub Actions workflow OIDC subject → **SEV-1**: a
   third-party may have pushed an unsigned image. Revoke at registry.

**Recovery**:
- If false positive (cosign infrastructure outage): wait for Sigstore
  status green, retry OTA.
- If real signing failure: rebuild image with `--push`, re-verify
  Rekor, redeploy.

---

### PB-02 — Audit chain tamper detected

**Symptoms**: `AuditHashChainSigner.verify_chain()` raises
`AuditChainCorrupted` on a node's audit_entries.

**Immediate actions**:
1. Isolate the node from operational traffic (mark
   `node_installations.state = 'decommissioned'` in the CP).
2. Preserve forensic state — DO NOT reboot. Take snapshot of:
   - `/var/lib/hermes/audit/` (chain DB)
   - `/var/log/journal/` (boot logs)
   - Boot image digest (`bootc status --json`)
3. Pull the chain to the CP for offline analysis:
   ```
   hermes audit export --since <last-known-good-ts> > chain.json
   ```
4. If hash break is at a recent entry: likely kernel-level tampering
   or signing key compromise → **SEV-1**.
5. If hash break is at an entry > 7 days old: possible disk
   corruption → verify with fsck + LUKS integrity.

**Recovery**:
- Decommission node, re-provision from fresh ISO.
- Rotate signing key (`hermes audit rotate-key --confirm`).
- File post-mortem within 24h.

---

### PB-03 — OTA auto-rollback fired (healthy_target_timeout)

**Symptoms**: `HealthyTargetWatchdog` triggered rollback because
`agents-os-healthy.target` did not reach within 600s post-boot.

**Immediate actions**:
1. Confirm rollback succeeded:
   ```
   bootc status --json | jq .status.booted.image.image
   ```
   Should show the previous version.
2. Capture the failed target boot logs:
   ```
   journalctl --boot=-1 -u agents-os-healthy.target --no-pager
   ```
3. Identify the failing unit:
   ```
   systemctl --failed
   ```

**Recovery**:
- If unit dependency issue: patch unit file, rebuild image, push as
  hotfix version with monotonic version bump.
- If transient network issue: re-trigger OTA after 10 min wait.

---

### PB-04 — Remote control session ended due to BINDING_VIOLATED

**Symptoms**: `RemoteControlSession.end_reason = binding_violated` —
the observed DTLS fingerprint differs from the one captured at issue.

**Immediate actions**:
1. **SEV-2 by default** — possible MITM on remote control channel.
2. Notify operator + revoke their session token globally:
   ```
   hermes remote-control revoke --operator-id <id> --all-sessions
   ```
3. Audit recent sessions from the same operator for anomalies.

**Recovery**:
- Operator must re-pair from a known good network (local-only first
  paircheck).
- Rotate the operator's mTLS cert if certificate-based auth was used.

---

### PB-05 — Telemetry data leaked despite opt-in OFF

**Symptoms**: Network logs show outbound telemetry traffic from a node
where `TelemetryOptInService.current().enabled == False`.

**Immediate actions**:
1. **SEV-1** — invariant FR-061 violated.
2. Identify the rogue exporter:
   ```
   journalctl -u hermes-runtime.service | grep -i telemetry
   ```
3. Block egress at firewall for affected destination immediately.

**Recovery**:
- Quarantine the rogue node, re-image.
- Audit code for any exporter that bypasses `should_emit()` gate.
- File security advisory.

---

## Verification commands

### Pre-deploy gate checklist

```
# 1. All unit tests green
pytest tests/unit/agents_os/ tests/unit/cli/ tests/unit/apps/ -q

# 2. E2E integration green
pytest tests/integration/agents_os/ -m integration -q

# 3. Threat model BLOQUEANTES referenced in code
.github/workflows/agents-os-edition.yml::threat-model-gate

# 4. Cosign verify the candidate image
cosign verify quay.io/hermes/agents-os-personal-desktop:<version>

# 5. SBOM available
cosign download attestation quay.io/hermes/agents-os-personal-desktop:<version>
```

---

## Escalation

- **SEV-1**: page on-call engineer + security lead within 5 min.
- **SEV-2**: page on-call within 30 min.
- **SEV-3/4**: file ticket, address within SLA.
