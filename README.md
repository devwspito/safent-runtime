<div align="center">

# Safent

### Hire a governed AI workforce — jailed at the kernel, running on your team's own machines, managed from one console.

**Safent turns any employee's computer into a safe home for an autonomous AI agent.**
The agent runs *locally* (their CPU, their browser, their apps) so it scales to 15 or 15,000 people at near-zero central cost — while a central control plane holds every agent inside a kernel-level jail and enforces exactly what each one is allowed to do.

Upload your staff roster in *any* format → get back a fully provisioned org with per-role security and one personalized install link per employee. It should feel like magic. That's the point.

`safe agent` → **Safent**.

[Quick start](#-quick-start) · [Why Safent](#-why-safent) · [Architecture](#-architecture) · [Security model](#-the-jail--why-you-can-trust-an-agent-with-real-credentials) · [Built with](#-built-with--acknowledgements) · [License](#-license)

</div>

---

## What is this?

Safent is an **open-core AI-workforce platform**. This repository is **Safent Community** — the open-source local runtime that every employee installs. It is a hardened container that runs one autonomous agent under a kernel-enforced cage: Landlock + seccomp + a private network namespace + a default-deny egress proxy + an unprivileged uid. Inside that cage the agent can chat, browse the web like a human, drive desktop apps, run tools, and call MCP servers — but it **cannot** escape, exfiltrate, or do anything its owner hasn't allowed.

The paid **Safent Enterprise** control plane (a separate, closed component) is the *management* layer: it provisions employees, signs per-agent and per-role security policy with an Ed25519 tenant key, routes human approvals, and relays agent-to-agent delegation. Every associate verifies that signed policy locally before applying it. **The heavy compute is always local; the cloud only governs.** That is the whole magic — and the whole moat.

> **Community is fully usable on its own** (a single local agent, no cloud). Enterprise adds fleet management, signed governance, and cross-human orchestration. You can run 150 Community agents governed by one Enterprise tenant, and no one can *administer* them as a fleet without the tenant's private key — even though the runtime they're built on is open source.

---

## ✨ Why Safent

Most "AI agent" products are a chat box wired to some tools. Safent is built for the part that actually matters when you put an agent on a real employee's machine with real credentials:

- 🔒 **A real jail, not a prompt.** The agent is confined by the Linux kernel — Landlock filesystem rules, a seccomp-bpf syscall floor, a netns with no route except an audited egress proxy, and uid-separation. A jailbreak *in the prompt* does nothing; the floor is model-independent and holds even if the model is actively hostile (we red-teamed it with an adversarial LLM and it held on all three layers: model refusal, hooks, kernel).
- 🧩 **Governance that's real — per-agent *and* per-role.** Every agent gets a signed access scope: an allow-list of native tools + an approval tier. Define roles (e.g. *Coordinator* vs *Standard*) once and govern 150 agents by role; override any single agent when you need to. A *Coordinator* self-resolves sensitive actions locally; a *Standard* agent escalates them to a remote approver. The agent can never pick its own role — it only ever arrives inside an Ed25519-signed bundle.
- 🪄 **Magic onboarding.** Point Safent at a messy roster (a spreadsheet, a phone-extension dump, an org chart) → an LLM normalizes it into departments + employees + suggested roles → you review → deploy → **N personalized install links**, each carrying that employee's role and permissions. One click per employee, zero manual setup on their side.
- 🌐 **Works with software that has no API.** When there's no CRM integration to call, the agent operates the software *like a human* — through a jailed, headful browser. This is the long tail of the real world (legacy portals, internal tools, government sites), covered.
- 👁️ **Watch it work — and audit it later.** A jailed headful Chromium is streamed over standard VNC (Xvfb + x11vnc + noVNC) so a manager can watch live and trust what's happening — and every action is recorded for the audit trail.
- 🏠 **Local-first, cloud-governed.** Nothing heavy runs in your datacenter. Each agent uses the hardware it already sits on. The control plane is tiny.
- 🔌 **Bring your own model.** Route to a local model, GLM, Claude, or GPT — per agent, per task — through a single provider abstraction.

### Where it fits

Safent is **not** trying to out-Claude-Code Claude Code. Claude Code is a brilliant pair-programmer for *developers* in a *terminal*. Safent is a **governed AI workforce for a whole company** — including the non-technical majority — with the safety and management layer an enterprise actually needs before it lets an agent touch real systems. Different arena, different job.

---

## 🚀 Quick start

**Prerequisite:** a container runtime. On macOS, either [Podman](https://podman.io) (`brew install podman`) or [Docker Desktop](https://www.docker.com/products/docker-desktop/); on Linux, Podman or Docker with `systemd` support.

**One line — install, cage, and run a single local agent (Community):**

```sh
curl -fsSL https://raw.githubusercontent.com/devwspito/safent-runtime/main/get-safent.sh | sh
```

This downloads the `safent` CLI, pulls the hardened image, creates the cage (on macOS it provisions a rootful Podman machine for you), and opens the UI at `http://localhost:17517/app/`.

**Join an Enterprise fleet** — every employee runs their own personalized one-liner:

```sh
curl -fsSL https://raw.githubusercontent.com/devwspito/safent-runtime/main/get-safent.sh | \
  SAFENT_CLOUD_ENDPOINT=https://<your-tenant>.safent.example \
  SAFENT_PAIR_CODE=<their-code> sh
```

The install pairs the agent, config-sync pulls the **signed** policy bundle, the associate verifies the Ed25519 signature against the tenant's public key, and the agent comes up already governed by its role. No manual configuration on the employee's side.

**Everyday commands:**

```sh
safent start        # run the caged daemon
safent pair <code>  # associate with an Enterprise tenant
safent update       # pull the latest hardened image
safent stop         # stop the daemon
```

**Build from source:**

```sh
python3 -m pip wheel . --no-deps -w dist/
podman build -f ops/container/Containerfile -t safent-runtime:dev .
NAME=safent HOST_PORT=17517 ./ops/container/run-safent.sh safent-runtime:dev
```

---

## 🏗️ Architecture

```
        ┌───────────────────────────────────────────────┐
        │  Safent Enterprise  (closed, cloud — GOVERNS)  │
        │  • provisions employees, roles, licenses       │
        │  • signs policy with the tenant Ed25519 key    │
        │  • routes human approvals (step-up MFA)        │
        │  • relays agent↔agent delegation (notary)      │
        └───────────────────────────────────────────────┘
             ▲ signed policy bundles          ▲ pull-only
             │ (Ed25519, verify-first)        │ (associates never accept inbound)
   ┌─────────┴──────────┐   ┌─────────────────┴────┐   (…one per employee, their own machine)
   │ Community runtime  │   │ Community runtime    │
   │  (THIS REPO)       │   │                      │
   │ ┌────────────────┐ │   │  each agent runs on  │
   │ │ THE KERNEL CAGE│ │   │  the EMPLOYEE's own  │
   │ │ Landlock       │ │   │  CPU / browser / apps│
   │ │ seccomp-bpf    │ │   │  → near-zero central │
   │ │ netns + egress │ │   │    cost, scales flat │
   │ │ uid separation │ │   │                      │
   │ └──────┬─────────┘ │   └──────────────────────┘
   │   Nous reasoning   │
   │   engine (LiteLLM) │  chat · jailed browser (Playwright/CDP) · desktop ·
   │   + tools + MCP    │  MCP servers · Composio tools · skills · live-view (VNC)
   └────────────────────┘
```

- **The daemon** (`src/hermes/`, Python package `hermes`) — the reasoning engine (Nous), the security hook that gates every tool call, config-sync, the pairing client, MCP + Composio integration, and the web UI.
- **The cage** (`ops/`, `src/hermes/security/`) — the systemd units, D-Bus policy, netns/nftables, seccomp profiles and launchers that confine every subprocess (browser included) at the kernel level.
- **The UIs** — a React web app (chat, an "agent floor" office view, security center, skills, MCP, providers), a native desktop shell, and a terminal UI.
- **Delivery** — a single hardened container built from `ops/container/Containerfile` (`FROM mcr.microsoft.com/playwright`), run with `--systemd=always`. Not a VM, not a custom OS.

---

## 🛡️ The jail — why you can trust an agent with real credentials

An agent that logs into your CRM is a huge attack surface. Safent's answer is defense in depth, with the kernel as the floor:

1. **Model layer** — the agent refuses obvious malice.
2. **Hook layer** — every tool call passes a security hook: a hardline-command detector, a self-jailbreak / denylist check, and the signed per-agent / per-role access scope. Sensitive actions require human approval (owner MFA locally, or a remote Enterprise approver).
3. **Kernel layer (inviolable)** — Landlock restricts the filesystem, seccomp-bpf restricts syscalls, a private network namespace routes all traffic through an **audited default-deny egress proxy**, and the browser + every launcher run under an unprivileged uid. This layer is **model-independent**: it holds even against an actively adversarial LLM.

The Enterprise approval flow can *relax which human signs off* on a dangerous action (a coordinator instead of the employee) — but **nothing** relaxes the kernel floor. Not the owner, not the cloud, not a coordinator. Governance decides *who approves*; the cage decides *what is even possible*.

Policy travels only inside Ed25519-signed bundles, verified **before** any field is trusted (signature-first). A compromised associate cannot forge a wider scope or a higher role for itself.

---

## 🧠 A note on honesty

Autonomous browser control on a *novel* portal is genuinely hard — for every model, not just ours. Safent does the **human-in-the-loop** browser path (live-view, teach-a-flow-once, approve sensitive steps) really well; fully-autonomous, unattended operation of an unseen UI is where you should measure before you trust. We'd rather tell you that than sell you a demo that breaks on the first real form. Do it *really well* on the handful of systems you actually use, or don't promise it.

---

## 🙏 Built with — Acknowledgements

Safent stands on an enormous amount of open-source work. We use these projects gratefully and want to give credit where it's due — Safent would not exist without them:

### The agent & model layer
- **[NousResearch — Hermes](https://github.com/NousResearch)** — the reasoning-agent lineage Safent's engine grew from.
- **[LiteLLM](https://github.com/BerriAI/litellm)** (BerriAI) — the provider abstraction that lets one agent route to any model.
- **[Model Context Protocol](https://modelcontextprotocol.io)** (Anthropic) — the open standard for tool/context servers, and the **[MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)**.
- **[Composio](https://composio.dev)** — managed authentication + hundreds of tool integrations.
- The model makers whose weights power real deployments: **GLM** (Zhipu AI / Z.ai), **Qwen** (Alibaba), and any Claude / GPT / open model reached through LiteLLM.

### The browser & desktop layer
- **[Playwright](https://playwright.dev)** (Microsoft) — the base image and browser-automation core, and **[@playwright/mcp](https://github.com/microsoft/playwright-mcp)**.
- **[agent-browser](https://www.npmjs.com/package/agent-browser)** — accessibility-tree browser control with stable element refs.
- **[Chromium](https://www.chromium.org)** / Chrome for Testing (Google) — the actual browser.
- **[noVNC](https://novnc.com)**, **[x11vnc](https://github.com/LibVNC/x11vnc)**, and **Xvfb** (X.Org) — the headful live-view stack.
- **[Tesseract OCR](https://github.com/tesseract-ocr/tesseract)** (via `pytesseract`) and **[selectolax](https://github.com/rushter/selectolax)** — perception helpers.

### The runtime, API & data layer
- **[FastAPI](https://fastapi.tiangolo.com)** + **[Starlette](https://www.starlette.io)** + **[Uvicorn](https://www.uvicorn.org)** (Sebastián Ramírez & encode) — the daemon's HTTP surface.
- **[Pydantic](https://docs.pydantic.dev)** — typed models and the signed-policy schema.
- **[cryptography](https://cryptography.io)** (PyCA) — Ed25519 signing/verification.
- **[dbus-fast](https://github.com/Bluetooth-Devices/dbus-fast)**, **[structlog](https://www.structlog.org)**, **[tenacity](https://github.com/jd/tenacity)**, **[aiohttp](https://docs.aiohttp.org)**, **[rfc3161ng](https://pypi.org/project/rfc3161ng/)** (RFC 3161 trusted timestamps), **[fastembed](https://github.com/qdrant/fastembed)** (Qdrant).
- **[uv](https://github.com/astral-sh/uv)** (Astral) — Python packaging.

### The web UI
- **[React](https://react.dev)**, **[Vite](https://vitejs.dev)**, **[TypeScript](https://www.typescriptlang.org)**, **[Vitest](https://vitest.dev)**.
- **[Three.js](https://threejs.org)** + **[react-force-graph](https://github.com/vasturiano/react-force-graph)** — the 3D agent-swarm view.
- **[Recharts](https://recharts.org)**, **[lucide-react](https://lucide.dev)**, **[marked](https://marked.js.org)**, **[DOMPurify](https://github.com/cure53/DOMPurify)**, **[Motion](https://motion.dev)**, **[qrcode.react](https://github.com/zpao/qrcode.react)**.

### The cage & platform
- **The Linux kernel** — **Landlock**, **seccomp-bpf**, **network namespaces**, and **nftables** are the reasons this is a real jail and not a promise.
- **[systemd](https://systemd.io)** — PID 1 and per-service hardening inside the container.
- **[Podman](https://podman.io)** / **[Docker](https://www.docker.com)** — the delivery runtime.
- **[Trivy](https://github.com/aquasecurity/trivy)** (Aqua Security) — vulnerability scanning baked into the image.

If we've used your work and missed you here, that's a bug — please open an issue and we'll fix it. Credit matters.

---

## 🤝 Contributing

Issues and PRs are welcome. Please run the unit gate before opening a PR:

```sh
PYTHONPATH=src python3 -m pytest tests/unit/agents_os/ tests/unit/cli/ tests/unit/apps/ -q
```

The kernel cage is the crown jewel — changes that touch `src/hermes/security/`, `src/hermes/runtime/security_hook.py`, or `ops/agents-os-edition/` (netns/seccomp/dbus) get extra scrutiny. **Never weaken the floor.**

---

## 📄 License

Safent is **open-core**:

- **Safent Community** (this repository) — the local runtime. **Source-available under the [Business Source License 1.1](LICENSE).** Free to run, self-host, modify, and use internally (including in production for your own organization). The one thing you may **not** do is offer it to third parties as a hosted/managed service that competes with Safent Enterprise. Each version converts to **AGPL-3.0-or-later** on its Change Date (four years after release), so the code always ends up fully open — and any future hosted fork must then publish its changes.
- **Safent Enterprise** — the cloud management / control plane. Commercial, closed-source.

The security model is designed so that **anyone can run Community, but no one can administer a fleet as an Enterprise without the tenant's private key** — the source-available runtime and the business coexist by construction.

> BSL 1.1 is *source-available*, not OSI "open source." For a use beyond the Additional Use Grant, contact the Licensor for a commercial license.

---

<div align="center">

**Built by [devwspito](https://github.com/devwspito).**
Hire your AI workforce. Keep it caged. Manage it from one place.

</div>
