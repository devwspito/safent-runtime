# OpenShell security layer → Hermes architecture

## North star — the "STUPID EASY + FULL AUTONOMY + SECURITY FIRST" concept

These three usually form a trilemma (more security ⇒ less power / less ease). This whole
design exists to make them COEXIST:
- **STUPID EASY** — OpenShell baked + invisible, one-command install, a sane balanced
  default; the owner never touches OpenShell's onboarding.
- **FULL AUTONOMY** — zero capability lost; the brain (Hermes/OpenClaw) keeps ALL its
  power; nothing is permanently blocked — everything is elevatable.
- **SECURITY FIRST** — a non-negotiable structural floor (credential-rewrite +
  confinement) + default-deny + mandatory MFA + human-presence on the gate.

The keystone that dissolves the trilemma is **credential-rewrite**: the agent uses every
API/integration the owner has WITHOUT ever holding the secret → Security-First costs no
autonomy. Everything below serves this concept.

---

Decision (2026-06-19): **do NOT reinvent the cage.** Adopt NVIDIA OpenShell (the
confinement substrate behind NemoClaw, built by security engineers) as our security
layer, baked into our image and pre-configured so the user never sees its onboarding.
We keep our brain + governance + skills + UX as the value on top.

Why: I hand-rolled a cage (launchers + netns + egress-proxy + Landlock) and it kept
leaking — proven e2e: the agent's native `terminal` tool ran in the privileged daemon
and read `master.key` + had open egress. The root problem is architectural (the agent
runs in the privileged process), and OpenShell already solved it. The interim fix
(route `terminal` through our exec-launcher — verified: master.key→0 bytes, egress→deny)
is the BRIDGE until OpenShell is in.

## What OpenShell COVERS (replaces our hand-rolled confinement)

| Our piece (leaks / partial) | OpenShell (proven, enforce) |
|---|---|
| exec/browser/mcp-launchers (delegation) | sandbox + `ExecSandbox` gRPC — the agent runs INSIDE |
| netns + nftables + egress-proxy | per-sandbox netns + L7 policy proxy (deny-default) |
| Landlock + seccomp (partial, fail-open in places) | Landlock + seccomp ENFORCE per sandbox |
| `master.key` in the daemon process (THE leak) | **credential-rewrite**: creds live in the gateway, NEVER in the sandbox |
| SNI/Host allowlist | per-binary + L7 (method/path) egress policy |

## What we KEEP (our differentiation — sits ON TOP)

- **Hermes/Nous brain** — the agent intelligence, persona, the "se busca la vida" loop.
- **Broker** — consent / HITL / taint→forced-HITL / kill-switch / signed audit. Our governance.
- **Security Center** — scan→score→gate for installs / hub skills / MCP. Our governance.
- **Skills** — recording (content-scanned), hub, teaching. Our value.
- **UX** — the "stupid easy" install, the elevation UI, onboarding, control plane (local + remote).

## The seam (how they compose)

```
Agent proposes a tool (terminal / browser / file / MCP / API call)
        │
        ▼
  OUR BROKER decides IF  ──►  consent · HITL (HIGH/taint) · kill-switch ·
  (governance, our value)     Security-Center scan for installs · signed audit
        │ approved
        ▼
  OpenShell decides HOW  ──►  ExecSandbox(gRPC): runs the command INSIDE the
  (confinement substrate)     sandbox (Landlock/seccomp/netns ENFORCE), egress via
                              L7 proxy (deny-default + owner-elevated domains),
                              credential-rewrite (placeholder → real cred at egress)
```

OpenShell API seam (from `/tmp/OpenShell`): gateway gRPC on `:8080` —
`CreateSandbox`, `ExecSandbox(stream stdout/exit)`, `CreateProvider`, `UpdateConfig`
(hot policy). Binaries: `openshell-gateway`, `openshell` (CLI), `openshell-sandbox`
(musl supervisor). Headless: TOML gateway config + YAML policy + pre-seeded providers,
no TUI. Build: `cargo build --release` (ARM64 ok — cargo is on the host).

## The architecture IMPROVEMENT (this is the real change)

Today the Nous brain + its tools run IN the privileged daemon that owns `master.key`.
That is the root of every leak. After:

```
BEFORE:  daemon(hermes, owns master.key) = brain + tools + governance   ← agent never caged
AFTER:   ┌ GATEWAY (privileged): creds + L7 proxy + policy + OUR governance host
         └ SANDBOX (OpenShell): Nous brain + ALL tools run as `sandbox` user,
                                no secrets, egress only via the proxy
```

This is the north star from memory ("[el agente es el proceso primario, runs confined]"
/ "[Hermes OS hardening estilo NemoClaw]"). The agent's brain itself runs confined; the
secret-holder is a separate, thin, privileged gateway.

### Credential-rewrite = the ROOT fix (not whack-a-mole)
`master.key` + provider keys live ONLY in the gateway. The agent uses placeholders;
the L7 proxy injects the real credential at the network boundary. So "read master.key"
returns NOTHING — it isn't in the sandbox. We stop blocking reads (which I kept failing
at) and instead ensure the secret is never in the agent's reach. Functionality is
preserved (the agent still uses every API/integration — it just never holds the token).

## Functionality: NOT limited (parity is the goal)

Confinement here removes danger, not capability (the "two partners"):
- Every tool kept (terminal/browser/file/MCP), run inside the sandbox.
- Every owner integration usable via credential-rewrite (no token exposure).
- New egress domain = one owner approval (our elevation UX) — durable.
- Only blocked: reading the owner's secrets, raw sockets, mount/ptrace/escape,
  uncontrolled egress. None are needed for real work.

## THE ELEVATION CONTRACT — ultra-secure AND ultra-powerful (core UX law)

OpenShell is the SHIELD, but it must NEVER impede the operation or make the brain
(Hermes / OpenClaw / whichever) useless. The law:

> OpenShell **retains** (default-deny). It NEVER dead-ends. Every block becomes an
> **elevation request surfaced in OUR UI**. Owner approves (one click) → it passes and
> is remembered. Owner ignores / declines → it stays blocked. **Nothing is permanently
> forbidden — everything is elevatable by the owner.**

So: full power (any action/endpoint is reachable once the owner approves) + ultra
security (nothing dangerous runs unapproved). The agent is never crippled — it is
*gated*, and the owner is the gate.

This is a NATURAL fit — OpenShell already has the operator-approval flow (agent hits an
unlisted endpoint → blocked → approval prompt → approve → durable). We DON'T use its
TUI; we bridge its approval events to OUR elevation UI (the egress-elevation feature
already built + the broker's HITL cards). One coherent surface:

```
Agent action / egress that OpenShell would deny
        │
        ▼  OpenShell default-deny → emits "approval needed" (NOT a hard fail)
        ▼  Gateway event → OUR UI: "El agente quiere ejecutar X / alcanzar Y — ¿permitir?"
        │
   ┌────┴─────────────┐
   ▼ owner APPROVES    ▼ owner ignores / declines
   policy hot-update   stays denied; agent gets a clean "pending/denied" result and
   (UpdateConfig /      keeps working on the rest — no crash, no dead-end.
    durable allow)
   → action proceeds
```

Implementation seam: subscribe to OpenShell gateway approval events → render via our
HITL/elevation cards → on approve call OpenShell policy hot-update (`UpdateConfig` /
`openshell policy set` / approve-request). The owner's "more or less conservative"
posture is THEIR choice (our Security-First default is deny-until-approved); we never
decide capability for them — we only make the gate friendly and one-click.

**Auth model: NO password (local, single-owner). Mandatory MFA + tiered humanity.**
It's a local product → no username/password. Instead the UI FORCES MFA enrollment on
first run (obligatory, not skippable): scan QR → TOTP secret stored in the GATEWAY
(Layer 1, agent never sees it). The gate is then TIERED by sensitivity:
- **Normal elevation / enable a capability** → 6-digit TOTP code (RFC 6238).
- **Really delicate op** (e.g., reaching a new external endpoint with creds, broad file
  access) → TOTP **+ humanity check** (an interactive UI challenge the agent — which has
  only programmatic access, no human I/O — cannot complete).
- **Most delicate** (loosen to `Permisivo total`, disable a protection, export data, key
  ops) → TOTP **+ human riddle** (a cognitive challenge, owner-set at enrollment / generated).

Why it matters: this makes elevation TRULY owner-only. A compromised agent or a
prompt-injection that DRIVES the approval UI **still cannot self-approve** — it can't
produce the TOTP (secret is out of reach) AND can't pass the humanity/riddle challenge
(no human at the keyboard). The human is the only key. (Defends the exact "agent
manipulates its own approval surface" attack — the last hole in any HITL.)

Verification (phase 7): an action the owner has NOT approved → denied (ground truth);
the SAME action after a one-click approve **+ valid TOTP code** → proceeds; a forged/absent
code → stays denied. Power preserved, danger gated, elevation owner-only.

## The Security / Policies UI — user sovereignty (TWO layers)

The product is full-custom. There is ALWAYS a "Seguridad / Políticas" section in the UI.
Two distinct layers — the user owns layer 2 entirely:

**Layer 1 — Structural security floor (NON-NEGOTIABLE, always on, NOT a toggle).**
This is ARCHITECTURE, not policy, so even "ultra-permissive" cannot disable it:
- credential-rewrite → the agent never holds master.key / provider secrets (they live in
  the gateway; the agent only ever sees placeholders). Reading them returns nothing.
- sandbox confinement → no container escape, no raw caps, no reaching the daemon's /proc.
- egress only ever flows through the audited proxy.
- the **MFA/TOTP secret lives in the gateway**, NEVER in the agent's sandbox — like
  master.key, the agent can't read it, so it can't forge an approval code.
→ The owner can be as free as they want WITHOUT being able to leak the crown jewels or
  brick the OS by accident. The floor holds regardless of policy.

**Layer 2 — Autonomy / capability policy (100% the user's, via the UI).**
Controls what the agent may do WITHOUT per-action approval. The user is sovereign here:
- **List EVERY command / tool / capability / endpoint** the agent can use, each with an
  enable/disable **checkbox** + **Save**. (Maps to OpenShell policy: endpoints/binaries/
  rules + our broker per-capability policy + egress grants — all surfaced as toggles.)
- **Presets**: `Equilibrado` (OUR default — usable + autonomous + secure floor),
  `Permisivo total` (full autonomy, no approval prompts — the user's choice & responsibility),
  `Bloqueado` (max friction). Plus granular per-item overrides on top of any preset.
- Anything OFF that the agent later needs → surfaces as a one-click **elevation card**
  (the contract above). Anything the user pre-enables → runs without asking.

**The user is completely free** — maybe their tasks are zero-risk and they want full
autonomy; their decision, their responsibility. We only provide the switches + a
Security-First DEFAULT (`Equilibrado`). We never decide their posture for them — and the
structural floor (Layer 1) means even maximum autonomy can't become catastrophe.

## Phased migration (security-first, red-team e2e each phase)

0. **Build OpenShell** (cargo --release, ARM64). ← running.
1. **Bake + headless gateway**: gateway in the image, TOML+YAML pre-seeded, systemd unit.
   Invisible to the user. `ExecSandbox` reachable on localhost.
2. **Terminal → ExecSandbox** (the proven hole first): the agent's `terminal` runs in
   an OpenShell sandbox. RED-TEAM e2e: real agent reads master.key → must get NOTHING.
3. **Credential-rewrite**: provider/master keys → gateway store; agent gets placeholders.
   RED-TEAM e2e: agent can call the API (works) but `cat`-ing any secret → nothing.
4. **Brain into the sandbox**: Nous runs as `sandbox` user; gateway = privileged host.
5. **Parity**: browser, MCP, file, skills all through the sandbox; verify every existing
   capability still works (no regression).
6. **Governance in front**: broker + Security Center gate before `ExecSandbox`.
7. **Full red-team battery** (the whole session's exploits) e2e against the new stack —
   ground truth only (never the agent's narration).

## Invariants (verified the hard way this session)
- Test ONLY e2e against the real agent path; never a component in isolation.
- Trust ground truth (logs, perms, the real key bytes), NEVER the LLM's self-report
  (qwen lied that it read the key AND that curl worked — both false, twice).
- Fail-closed everywhere; the cage gates EXECUTION, not the LLM's credulity.
- Stay "stupid easy": OpenShell baked + pre-configured; one-command install.
