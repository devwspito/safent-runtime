# Lumen — Security Posture (standard container)

Honest statement of what the Lumen cage enforces, how to run it safely, the host
it needs, and the residual risks. Validated by an adversarial red-team (penetrate
+ escape) against a fresh hardened container.

**Credits.** The agent runtime is **Hermes**, built on the **Nous Research
hermes-agent** engine — that is the brain Lumen wraps. The cage is **inspired by
NVIDIA NemoClaw**. Lumen's differentiator is **the cage itself** — a real,
defensible OS-level containment with a **semantic governance layer** (per-action
consent/HITL) on top, plus stupid-easy delivery. The cage is the product.

## Threat model

The agent (the LLM) is assumed **prompt-injectable** — that is never the security
boundary. The boundary is what the agent (or a compromised renderer/daemon) can
actually DO. Prompt injection always finds a way in; the cage controls the blast
radius.

## What is enforced (validated live)

**Network edge (penetrate):**
- HTTP control plane requires a per-install **Bearer operator token** (HKDF subkey
  of `master.key`) on every mutating `/api/v1` route. Unauthenticated `POST` →
  `401` (providers, chat, approvals, mcp, integrations). The token is delivered to
  the same-origin webui via the served `index.html`.
- Published on **`127.0.0.1` only** (`run-lumen.sh`) — the control plane never faces
  the LAN.
- Provider `base_url` rejects link-local/cloud-metadata (`169.254.0.0/16`) → no SSRF
  to instance metadata. Private ranges are allowed (local-model on host gateway).

**Semantic governance (the moat):**
- **Default-deny + consent.** `HERMES_AUTONOMOUS_DEFAULT=0` by default: the agent
  does only what is granted; anything else (HIGH / irreversible / untrusted-tainted)
  requires the owner's **HITL approval** — the owner decides per action.
- HIGH-risk and taint-forced actions require HITL **even** if full-autonomy is later
  enabled (anti-prompt-injection invariant, inappealable).
- Terminal: only 8 read-only binaries auto-run (`ls cat echo pwd whoami id date env`);
  everything else (curl, bash, python, …) → HITL.
- Skills are HMAC-signed; every broker decision is hash-chained and externally
  anchored (WORM + RFC-3161 TSA).

**Kernel cage (escape):**
- Browser (untrusted web content) jailed in a netns: egress only via the
  SNI-allowlist proxy; per-process **Landlock** FS jail; per-unit seccomp.
- **seccomp** profile (`seccomp/lumen.json`): allows `landlock_*` (so the jail
  loads) and denies `mount`/`setns`/`ptrace`/`pivot_root` — a Chromium 0-day cannot
  `clone(CLONE_NEWNS)`/`mount`/`setns` out of the netns. **Do not use
  `seccomp=unconfined`.**
- Non-root agent (uid 880), no SUID binaries. Container caps = podman's default
  (already reduced) + SYS_ADMIN / AUDIT_READ for the netns; the AGENT's own
  processes run with an EMPTY `CapabilityBoundingSet=` (per-unit) so they hold zero
  caps regardless. (We do not `--cap-drop ALL` container-wide — systemd PID1 needs
  the baseline to boot; least-privilege is per-unit.) Keystore / DBs / signing keys
  are `InaccessiblePaths` to the jailed browser AND the agent terminal (a renderer
  RCE or terminal command cannot read `master.key`).
- Agent terminal egress is netns-jailed (default-deny) via the root exec-launcher:
  commands run as hermes inside the browser netns, egress only through the audited
  proxy (the owner elevates domains via UI), no direct route, proxy-side DNS.

Red-team result on the hardened container: every unauth `POST` → 401; SSRF blocked;
loopback-only; `cat /etc/shadow`, `unshare`, `mount`, `setns`, host-fs access, privesc
all denied.

## Host requirements

- **Linux kernel with Landlock** (`CONFIG_SECURITY_LANDLOCK=y`, `lsm=landlock`). On
  macOS/Windows, Docker Desktop / Podman machine provides this. The daemon
  **fail-closes** if Landlock is absent — it will not start a credentialed browser
  unconfined.
- `nf_conntrack` + `nf_log_syslog` modules (for the netns nftables rules).

## How to run

```bash
ops/container/run-lumen.sh ghcr.io/devwspito/lumen:latest 17517
# UI at http://localhost:17517 (the operator token is injected into the page)
```

Never `docker run` bare: that would publish on `0.0.0.0` and drop the seccomp/caps
posture. Never `--privileged` or `--security-opt seccomp=unconfined`.

## Residual risks (honest — not yet closed)

- **Agent terminal egress is consent-gated, not netns-jailed.** Network commands
  (curl/wget) are not in the terminal allowlist → they require **HITL**, so they are
  not a silent exfil. But the kernel-level egress of the terminal scope is NOT jailed
  (systemd `IPAddressDeny=any` is fail-open inside a container without BPF cgroup
  delegation). A daemon-RCE that bypasses the broker could exfil data the `hermes`
  user can read (its workspace — not system secrets, not host files). **Follow-up:**
  route agent exec through a privileged netns helper (like the browser-launcher).
- **In-process broker.** The capability broker runs in the daemon process; a daemon
  RCE owns it (vs NemoClaw's out-of-process engine). The audit external-anchor makes
  tampering detectable, but moving the broker + signer out-of-process is the proper
  fix. Documented; not yet done.
- **MCP servers** run with the daemon's egress (no per-server netns/allowlist yet);
  install is operator-gated + the runner allowlist + argv[0] is rewritten to the
  trusted system binary (no path-traversal).
- **Dependencies** are floor-pinned (`aiohttp>=3.13.4`, `cryptography>=44.0.1`); a
  committed lockfile + CI `pip-audit`/`npm audit` gate is still TODO.
- CDP `--remote-allow-origins=*` (browser-only, netns-bound, not host-reachable);
  per-session isolation is a follow-up.

## Reporting

Security issues: open a private advisory on the repo. Do not file public issues for
unpatched vulnerabilities.
