# Install Safent on macOS (Apple Silicon)

Safent is a **Docker/OCI container** (systemd PID1 + the kernel cage). On macOS it
runs inside a **podman machine** (a tiny Linux VM) — podman supports the systemd +
capabilities + seccomp the cage needs.

## Prerequisites
- macOS on **Apple Silicon** (arm64).
- **Homebrew** (https://brew.sh). The installer installs podman via brew if missing.
- **python3** (`brew install python`) — used to build the wheel.

## Install + run (one command)
```sh
git clone https://github.com/devwspito/safent-runtime.git
cd safent-runtime
./dist-mac/install-safent-mac.sh
```
It will: install/start a podman machine (4 CPU / 8 GB), **build the Safent image from this
repo** (one-time, ~15-20 min — pulls the public Playwright base + npm + pip; nothing
private leaves your machine), run Safent with the correct flags, wait for boot, and print
a ready-to-open URL **with the auth token**:
```
http://localhost:17517/?k=<bootstrap-token>
```
Open that URL — that's Safent. (The `?k=` token authorizes the UI's actions; without it,
config/install buttons return 401.)

## First steps in the UI
1. **Configure your model** (the "Configura un modelo" button → "Añadir modelo propio"):
   your OpenAI-compatible endpoint (base URL + model + API key). Save & activate.
2. **MCP → ruflo** is pre-installed and connects out of the box (302 tools); its LLM
   auto-wires to the model you just configured. Ask the agent to "use ruflo to plan a
   project" → it discovers + invokes ruflo (you approve the HITL card).
3. **Integraciones → Composio**: paste your Composio API key → connect → 250+ apps.
4. **Skills**: search the hub and install (each install is scanned by the Security Center).

## Manage
```sh
podman logs -f safent        # daemon logs
podman stop safent           # stop
podman start safent          # start again
podman rm -f safent          # remove (state persists in the 'safent-data' volume)
```

## Notes
- Do **NOT** run with `--cap-drop ALL` or container-wide `--security-opt no-new-privileges`
  — systemd PID1 needs SETUID/SETGID, and the hardened units set NoNewPrivileges per-unit.
  The installer uses the correct flags (see `ops/container/run-safent.sh`).
- State (your model key, conversations, installed MCPs/skills) lives in the `safent-data`
  podman volume and survives `podman rm` + image updates.
