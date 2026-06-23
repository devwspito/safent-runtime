# lumen-runtime

**Lumen** — agentic runtime delivered as a **Playwright/Ubuntu Docker container** (systemd PID1).

> ⚠️ This is the **clean product repo**. It is a **Docker container**, **NOT** qemu / qcow2 / Apple-VZ / bootc.
> That VM delivery is **legacy** and is intentionally **not** in this repo.

## Install (one line)

macOS (Apple Silicon / Intel) and Linux (amd64 / arm64):

```sh
curl -fsSL https://raw.githubusercontent.com/devwspito/lumen-runtime/main/get-lumen.sh | sh
```

This pulls the public hardened image, runs it with the security cage, and opens your
browser at a per-boot unique token. The model, Composio, Brave, agents and skills are all
configured in the UI. Requires **podman** or **docker** (on macOS, a **rootful** podman
machine). Windows: experimental via WSL2.

## What's inside (the whole product, one container)

- **Daemon** — the Lumen runtime + the **Nous** reasoning engine.
- **The cage** — OpenShell confinement substrate (`ops/agent-cage/`) + netns/egress moat + Landlock + seccomp. The agent's terminal/browser/MCP run sandboxed.
- **Lumen UI** — QML compositor + apps (`src/hermes/lumen/`: chat, tasks, security, skills, integrations, memory).
- **Office UI** — live "agent floor" web view (`src/hermes/shell_server/webui/js/office.js`), part of the Lumen Cowork web UI.
- **MCP / skills / composio** — `tool_search`/`tool_call` discovery (incl. **ruflo** multi-agent swarm), skills, composio.

## Build

```sh
./scripts/build.sh            # wheel + container image → lumen-runtime:clean
```

Or manually:

```sh
python3 -m pip wheel . --no-deps -w dist/
podman build -f ops/container/Containerfile -t lumen-runtime:clean .
```

## Run

Use the canonical launcher (correct caps + seccomp + securityfs):

```sh
NAME=lumen HOST_PORT=17517 ./ops/container/run-lumen.sh
# UI: http://localhost:17517
```

Or the equivalent raw command:

```sh
podman run -d --name lumen --systemd=always \
  -p 127.0.0.1:17517:7517 \
  --cap-add NET_ADMIN --cap-add SYS_ADMIN --cap-add AUDIT_READ \
  --security-opt seccomp=ops/container/seccomp/lumen.json \
  --security-opt unmask=/sys/kernel/security \
  -v /sys/kernel/security:/sys/kernel/security:ro \
  -v lumen-data:/var/lib/hermes \
  --shm-size=1g \
  lumen-runtime:clean
```

> ⚠️ Do **NOT** use `--cap-drop ALL` or container-wide `--security-opt no-new-privileges`:
> systemd (PID1) needs SETUID/SETGID to start the per-unit services, and the hardened
> units set `NoNewPrivileges` **per-unit** — a container-wide one breaks dbus/login setuid
> and the boot fails (exit 216/GROUP). `--systemd=always` is required (the daemon
> fail-closes via `_assert_confinement_active()` checking the netns/egress units).

## Layout

| Path | What |
|---|---|
| `src/hermes/` | The product: daemon, Nous, Lumen UI, Office UI, MCP/skills/composio |
| `ops/container/` | The **Playwright Containerfile** (the delivery) + run script + seccomp + dropins |
| `ops/agent-cage/` | OpenShell confinement substrate (binary + systemd) |
| `ops/agents-os-edition/` | systemd units, dbus, netns, scripts, seed — baked into the container |
| `tests/` | Test suite (gate: `tests/unit/{agents_os,cli,apps}`) |

## Tests

```sh
pytest tests/unit/agents_os/ tests/unit/cli/ tests/unit/apps/ -q
```

## License

**[PolyForm Noncommercial License 1.0.0](LICENSE)** — free to use, study, modify and share
for **any noncommercial purpose** (personal, research, education, nonprofits, hobby).
**Commercial use is not permitted** under this license. For a commercial license, contact
the author.
