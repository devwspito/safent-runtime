# Lumen runtime — instructions for AI assistants

## THE PRODUCT IS A PLAYWRIGHT DOCKER CONTAINER. Read this before doing anything.

- Built from **`ops/container/Containerfile`** → `FROM mcr.microsoft.com/playwright:v1.59.0-noble`.
- Run with **`docker/podman run --systemd=always`**. Distribution model = **Docker Desktop / Colima** on the user's machine.
- This is **NOT** qemu / qcow2 / Apple-VZ / bootc / a custom OS image. That delivery was **removed** during the migration to this clean repo. **Do NOT reintroduce it.** If you find yourself building a qcow2, running `bootc-image-builder`, or touching Apple VZ — STOP, you are on the wrong path.

## Build & run

```sh
# wheel (the daemon source → src/hermes)
python3 -m pip wheel . --no-deps -w dist/
# the container
podman build -f ops/container/Containerfile -t lumen-runtime:clean .
# run it (canonical launcher — correct caps + seccomp + securityfs)
NAME=lumen HOST_PORT=17517 ./ops/container/run-lumen.sh
```

**Run flags — critical:** do NOT `--cap-drop ALL` and do NOT use container-wide
`--security-opt no-new-privileges`. systemd PID1 needs SETUID/SETGID to start the
per-unit services (else every service dies with exit 216/GROUP); the hardened units
set NoNewPrivileges PER-UNIT. Use `ops/container/run-lumen.sh` (or replicate its flags:
`--cap-add NET_ADMIN,SYS_ADMIN,AUDIT_READ` + `seccomp=ops/container/seccomp/lumen.json`
+ `unmask=/sys/kernel/security` + `-v /sys/kernel/security:ro` + `--shm-size=1g`).

## What's where

- `src/hermes/` — the entire product (one Python package, `hermes`):
  - daemon + **Nous** reasoning engine (`runtime/`),
  - **Lumen UI** (`lumen/` — QML compositor + apps),
  - **React web app** (`frontend/` → built to `/opt/lumen-webapp`, served at `/app/`): chat, Office "agent floor" (`frontend/src/views/OfficeView.tsx`), security, skills, MCP, providers, memory. The single official UI (the legacy vanilla `shell_server/webui/` was removed),
  - MCP / skills / composio (`tool_search`/`tool_call`, **ruflo** swarm).
- `ops/container/` — the Playwright Containerfile (the delivery).
- `ops/agent-cage/` — OpenShell cage (binary + systemd drop-in).
- `ops/agents-os-edition/` — systemd units, dbus policy, netns/nftables, launcher scripts, MCP seed (the Containerfile COPIES these subdirs).

## Testing

- Unit gate (what CI must keep green): `pytest tests/unit/agents_os/ tests/unit/cli/ tests/unit/apps/ -q`
- E2E: build the container, `podman run --systemd=always`, then exercise chat / MCP(ruflo) / skills / composio / terminal against the running daemon and read its journal.
- **Always verify against the running container, not against assumptions or stale comments.**

## Conventions

- Python package name stays `hermes` (repo name `lumen-runtime` ≠ package name). The wheel is `hermes-runtime`.
- The terminal cage: in CI / unprivileged dev, terminal runs raw (`HERMES_TERMINAL_SCOPE=0`); the real hardening is the exec-launcher / OpenShell inside the container.
