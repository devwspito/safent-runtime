# Install Lumen on Windows

Lumen is a **Docker/OCI container** (systemd PID1 + the kernel cage:
Landlock/seccomp/netns/nftables). On Windows it runs inside a **rootful podman
machine** — a small Linux VM on the WSL2 backend — exactly like the macOS path.
The container, its run flags, and the security cage are **identical** to Linux and
macOS; only the launcher (PowerShell) and the VM bootstrap differ.

> **Podman, not Docker.** The cage needs `--systemd=always` and
> `--security-opt unmask=/sys/kernel/security`, which are Podman-only. Docker
> Desktop cannot reproduce the systemd-PID1 cage, so Windows uses Podman.

## Prerequisites
- **Windows 10/11** with **WSL2** enabled: in an admin PowerShell run `wsl --install`, then reboot.
- A **current WSL2 kernel (≥ 6.6)** — the stock Microsoft kernel (6.6 / 6.18) ships
  Landlock + securityfs, which the cage requires. Update with `wsl --update`. The
  legacy 5.15 kernel is the one configuration that will **not** run the cage.
- **Podman Desktop** (https://podman.io/) with the WSL2 backend.

The launcher checks the kernel version + mounts securityfs in the VM automatically,
and warns you if Landlock is missing.

## Install + run (one command)
In PowerShell:
```powershell
iwr -useb https://raw.githubusercontent.com/devwspito/lumen-runtime/main/get-lumen.ps1 | iex
```
It will: install the `lumen` command on your PATH, create/start a **rootful** podman
machine (4 CPU / 8 GB, first time only), pull the Lumen image, run it with the
correct flags, wait for boot, and open your browser at a ready URL **with the auth
token**:
```
http://localhost:17517/?k=<bootstrap-token>
```
Open that URL — that's Lumen. (The `?k=` token authorizes the UI's actions; without
it, config/install buttons return 401.)

### Manual (from a clone)
```powershell
git clone https://github.com/devwspito/lumen-runtime.git
cd lumen-runtime
powershell -ExecutionPolicy Bypass -File .\lumen.ps1 update
```

## Control it from the terminal
```powershell
lumen            # open it (starts it if stopped)
lumen stop       # stop
lumen start      # start without opening
lumen restart    # restart
lumen update     # pull the latest image + recreate (keeps your config)
lumen status     # running? on which port?
lumen logs       # follow the container journal
lumen pair <code>   # associate with an enterprise tenant (same image, associate mode)
lumen unpair        # remove the association (revert to community)
```

## First steps in the UI
1. **Configure your model** → "Añadir modelo propio": your OpenAI-compatible endpoint
   (base URL + model + API key). Save & activate.
2. **MCP → ruflo** is pre-installed and connects out of the box; its LLM auto-wires to
   the model you configured.
3. **Integraciones → Composio**: paste your Composio API key → connect → 250+ apps.
4. **Skills**: search the hub and install (each install is scanned by the Security Center).

## Verify the cage is actually active (parity check)
The daemon fail-closes if the cage is not real. To confirm full parity:
```powershell
podman exec lumen cat /sys/kernel/security/lsm        # must list 'landlock'
podman exec lumen systemctl is-active hermes-runtime  # must print 'active'
podman exec lumen journalctl -u hermes-runtime | Select-String confinement
```
If `/sys/kernel/security/lsm` is empty or lacks `landlock`, update the WSL2 kernel
(`wsl --update`) and restart the machine (`podman machine stop`; `podman machine start`).

## Notes
- Environment overrides match Linux/macOS: `LUMEN_IMAGE`, `LUMEN_NAME`, `LUMEN_PORT`,
  `LUMEN_CLOUD_ENDPOINT`, `LUMEN_DATA_VOLUME`, `LUMEN_SECCOMP_URL`.
- State (your model key, conversations, installed MCPs/skills) lives in the
  `lumen-data` podman volume and survives `podman rm` + image updates.
- The podman machine **must be rootful** (the cage needs root in the VM for
  netns/nftables/securityfs/cgroups). The launcher enforces this; if it reports the
  machine is rootless, convert it:
  `podman machine stop <name>; podman machine set --rootful <name>; podman machine start <name>`.
