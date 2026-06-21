# lumen-runtime

**Lumen** — agentic runtime delivered as a **Playwright/Ubuntu Docker container** (systemd PID1).

> ⚠️ This is the **clean product repo**. It is a **Docker container**, **NOT** qemu / qcow2 / Apple-VZ / bootc.
> That VM delivery is **legacy** and is intentionally **not** in this repo.

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

```sh
podman run -d --name lumen --systemd=always \
  --cap-drop ALL --cap-add NET_ADMIN --cap-add SYS_ADMIN --cap-add AUDIT_READ \
  --security-opt no-new-privileges --shm-size=1g -p 17517:7517 \
  lumen-runtime:clean
# UI: http://localhost:17517
```

`--systemd=always` is **required**: the daemon fail-closes via `_assert_confinement_active()`
(it checks the netns/egress units are active). A plain entrypoint = silent loss of the cage.

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
