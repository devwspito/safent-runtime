# agent-cage — extraction plan (roadmap Phase 2)

"The cage is the product." This plan extracts the kernel cage we hardened on Hermes
into a **generic, reusable sandbox** (`agent-cage`) that confines ANY agent runtime
(Hermes, openclaw, others). It is the result of the coupling audit (2026-06-19) and
is actionable: each step is a concrete refactor with a clear seam.

## North star

A runtime author ships an agent + a `cage.toml` + one adapter (`GovernancePort`), and
gets the full kernel cage for free: netns egress jail, default-deny proxy with
anti-pivot, root launchers (browser/exec/MCP) that drop to an isolated uid, seccomp,
Landlock, ProtectProc, per-uid DAC, and forced-HITL-on-taint. **The cage gates
EXECUTION; the runtime owns the agent's brain.** (Proven live: an LLM that fell for a
prompt-injection and even confabulated success exfiltrated nothing.)

## The seam: cage ⟂ governance

The launchers/netns/proxy are **beneath** the decision logic. The daemon consults the
runtime's governance BEFORE asking a launcher to spawn, and audits after. The single
contract:

```python
@runtime_checkable
class GovernancePort(Protocol):
    async def verify_action(self, tool: str, params: dict, ctx: ActionContext) -> ActionDecision:
        """APPROVED | PENDING_APPROVAL | REJECTED | NEEDS_INSTALL. Applies kill-switch,
        consent, risk classification, and taint→forced-HITL (untrusted web/MCP/file
        content → HIGH)."""

    async def request_approval(self, proposal_id, risk, justification) -> str | None:
        """HITL: return a one-shot approval token, or None on timeout/deny."""

    async def review_install(self, intent: InstallIntent) -> InstallDecision:
        """scan→score→gate for pip/npm/curl|sh/git-clone + hub/recorded skills."""

    async def audit(self, action, outcome) -> None:
        """Append to the tamper-evident audit chain."""
```

Hermes implements `HermesGovernance(GovernancePort)` wrapping the existing
`CapabilityBroker` + `SecurityCenter` + `ConsentManager` + `AuditHashChain`. A minimal
runtime can ship a `PermissiveGovernance` (consent=auto, audit=log) and still get the
full KERNEL cage — that is the value: the kernel layer is the hard part and is generic.

## Reuse, honest, per piece

| Piece | Generic | To parameterize / refactor |
|---|---|---|
| Launchers (browser/exec/mcp) | ~85% | only hardcoded paths/users/IPs → `cage.toml`. Logic (fork/exec, systemd-run, netns join, peercred, fd-passing) is already pure — **zero `hermes.*` imports**. |
| netns + nftables | ~95% | names + IPs are placeholders; the default-deny + anti-pivot ruleset is generic. |
| egress-proxy | ~70% | SNI/Host filter + anti-pivot resolve are generic; the policy model + socket user are Hermes. Extract `PolicyPort`. |
| seccomp | ~75% | the profile is generic; tune the allowlist per launch type. |
| Landlock | ~60% | generate the ruleset from `cage.toml` read_write/inaccessible paths. |
| users/groups + DAC | ~40% | the MODEL (daemon-uid ≠ sandbox-uid, secrets 0600, shared 2770 setgid) is generic; the uid/gid numbers + group names parameterize. |
| systemd hardening | ~80% | properties generic; values (caps, slices) → config. |
| Broker | ~30% | dispatch/gating sequence is generic; consent/intent-log/audit-signing/registry are Hermes → hide behind `GovernancePort`. |
| Security Center | ~40% | ScanService is ~drop-in; the policy model + scanners are Hermes. |

**~70% of the kernel cage is reusable as-is**; the rest is parameterization. Governance
needs an abstraction (`GovernancePort`) but not a rewrite.

## Phased extraction

1. **Config loader** — a `cage` package that loads `cage.toml` into a frozen dataclass.
   Replace the inline constants in the 3 launchers (`_SOCK_PATH`, `_NETNS_PATH`,
   `User=`, IPs, `_EXEC_PROPERTIES`/`_SCOPE_PROPERTIES`) with reads from it. Lowest
   risk (launchers already have zero `hermes.*` imports).
2. **Template the netns/nftables units** — placeholders `{netns}`, `{host_ip}`,
   `{proxy_port}`, `{veth_*}`, `{blocked_cidrs}` rendered from `cage.toml`.
3. **Define `GovernancePort`** (domain) + implement `HermesGovernance` wrapping the
   current broker/SC/consent/audit. The daemon calls the port; nothing else changes
   for Hermes (behavior-preserving).
4. **Landlock/seccomp from config** — generate rulesets from `read_write_paths` +
   `inaccessible_paths` + per-launch-type syscall sets.
5. **Package + smoke matrix** — `agent-cage` as an installable unit; a smoke suite that
   boots it with (a) `HermesGovernance`, (b) `PermissiveGovernance`, (c) an openclaw
   adapter, and runs the red-team battery (the one in
   `[[project_safent_container_egress_elevation]]`) against each.

## Invariants the extraction must preserve (verified this session)

- Agent work runs as a **distinct uid** from the daemon (no /proc/fd, no DAC, secrets 0600).
- The **broker/governance is the only choke-point** for agent actions (not reachable via
  D-Bus, skill replay, symlinks, or the launchers).
- **Egress is default-deny** in a netns, only via the audited proxy, which anti-pivots
  resolved internal IPs.
- **Untrusted content (web / MCP / file) taints the cycle → forced HITL** on HIGH actions.
- **Skill content (recorded + hub) is scanned** for trojan patterns before it is minted/run.
- **Launchers fail-closed**: any transport/confinement error denies, never downgrades to
  unconfined.
