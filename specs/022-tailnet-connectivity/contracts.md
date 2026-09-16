# 022 — Tailnet lane handoff contracts

Owner-approved design in `plan.md`. This file is the binding contract between
the **ops lane** (`tailnet-ops` branch: `hermes-tailscaled.{service,path}`,
`hermes-tailscale-control.{service,path}`, `scripts/hermes-tailscale-control`,
tmpfiles) and the **egress lane** (`tailnet-egress` branch: the egress-proxy
connector + the shell-server API + the Seguridad UI, this document's author).
Both lanes MUST honor the paths/schemas below exactly — a mismatch here is a
silent runtime failure (wrong permissions, unreadable file), not a compile
error.

## 1. Runtime paths (ground truth: tmpfiles already committed on `tailnet-022-base`)

From `ops/agents-os-edition/tmpfiles/hermes.conf` (commit `d04cdcb`):

| Path | Mode | Owner | Purpose |
|---|---|---|---|
| `/var/lib/hermes/tailscale` | 0700 | hermes-tailscale | tailscaled `--statedir` (node key — persists across image pulls) |
| `/var/lib/hermes/tailscale/enabled` | — | hermes-tailscale (or root helper) | opt-in marker; `hermes-tailscaled.service` is `ConditionPathExists=` on this file |
| `/run/hermes/tailscale` | 0700 → **see §2, REQUIRED CHANGE** | hermes-tailscale | tailscaled `--socket` (`tailscaled.sock`) + `status.json` |
| `/run/hermes/tailscale-control` | 0700 | hermes (shell-server) | staging dir for the daemon → root-helper handoff (this contract, §3) |

## 2. `status.json` — ops lane writes, egress lane reads (REQUIRED tmpfiles change)

**Path:** `/run/hermes/tailscale/status.json`
**Writer:** the ops lane's status watcher (root or `hermes-tailscale`; recommend
a small oneshot/loop driven by `tailscale status --json` over the local
`tailscaled.sock`, since that socket is only reachable by `hermes-tailscale`).
**Write discipline:** atomic (`tmp` + `rename`), on every state change
(interface up/down, peer list change) and as a periodic heartbeat (recommend
≤30s) so a stale/crashed watcher is externally observable via `mtime`.

**Schema** (no secrets — names only, never keys, never node private state):

```json
{
  "node_name": "safent-agent",
  "magicdns_suffix": "tail1234.ts.net",
  "tailnet": "acme.ts.net",
  "online": true,
  "peers": [
    { "name": "laptop", "online": true },
    { "name": "server", "online": false }
  ],
  "last_attempt": { "at": "2026-09-10T14:03:00+00:00", "ok": false, "error_kind": "tailscale_up_failed" }
}
```

- `node_name` — this node's MagicDNS short name (`tailscale status --json` → `.Self.HostName` or `.Self.DNSName` sans suffix).
- `magicdns_suffix` — the tailnet's MagicDNS domain (`.MagicDNSSuffix`, or the Headscale sovereign-tier equivalent), lowercase, no leading/trailing dot.
- `tailnet` — the tailnet's display name (`.CurrentTailnet.Name`), used only for the UI's "conectado como X en Y" sentence.
- `online` — this node's own link state (`.BackendState == "Running"` / `.Self.Online`).
- `peers` — **names and online state only.** Never include peer IPs, tags, key
  fingerprints, or OS/version — those are not needed by any consumer and widen
  what a compromised reader of this file learns about the tailnet topology.
- `last_attempt` — **optional** (absent/`null` until a connect has ever been
  staged). The verdict of the most recent `POST /connect` (025 hallazgo D:
  `hermes-tailscale-control` fails 5/5 on a rejected key, but this script does
  NOT write status.json — so without this field a rejected key was
  indistinguishable from "never tried", and the OLD `configured` definition
  ["status.json exists"] read `true` even though the node never logged in).
  Written by `hermes-tailscale-control` to a SEPARATE file,
  `/run/hermes/tailscale/last-attempt.json` (`{"at", "ok", "error_kind"}`,
  0644, no key material — see §3.1), and mirrored verbatim into status.json
  by the status watcher on its next tick (single-writer discipline: the
  control script never touches status.json itself).
  - `at` — ISO 8601 timestamp of the attempt.
  - `ok` — `true` if `tailscale up` succeeded, `false` otherwise.
  - `error_kind` — `null` on success; `"tailscale_up_failed"` on failure (the
    control script does not currently distinguish rejected-key from
    network/timeout failures — all collapse to this one value).

**`configured` (REQUIRED semantics — 025 hallazgo D):** `configured` means
**logged in**, i.e. `configured == online`, as reported by the status
watcher. It does **not** mean "status.json exists" — the watcher writes this
file as soon as `hermes-tailscaled.service` starts, independent of whether
`tailscale up` ever succeeded, so "file exists" was never a safe proxy for
"connected". A caller wanting to distinguish "never configured" from "a
connect attempt is pending" from "a connect attempt failed" must look at
`last_attempt`, not `configured` alone (see `hermes.shell_server.tailnet.api`
`_read_status` / the egress lane's `tailnetUiState`).

**REQUIRED ops-lane tmpfiles change:** `/run/hermes/tailscale` is currently
0700 `hermes-tailscale:hermes-tailscale`. Under that mode, **no other uid can
even traverse the directory** (the `x` bit is owner-only) — so the
shell-server (`hermes`) and the egress-proxy (`hermes-egress`) cannot reach
`status.json` no matter what mode the file itself has. This is a hard
permission fact I verified empirically against the committed tmpfiles, not a
hypothetical.

Fix: relax the **directory** to `0711` (owner rwx, group none, other
**execute-only** — traversal of a *known* path, no `ls`/readdir, no write).
`tailscaled.sock` inside stays protected by its own socket-file mode
(AF_UNIX `connect()` permission is governed by the socket inode's own mode
bits, not the parent directory's), so this does not widen who can drive
tailscaled — only who can `open()` the one file named `status.json` if they
already know its full path.

```
# ops/agents-os-edition/tmpfiles/hermes.conf — change this line:
d /run/hermes/tailscale 0700 hermes-tailscale hermes-tailscale - -
# to:
d /run/hermes/tailscale 0711 hermes-tailscale hermes-tailscale - -
```

`status.json` itself: mode `0644` (world-readable — it carries no secret, see
schema above).

**Readers (this branch, already implemented against this exact path):**
- `hermes.egress_proxy.infrastructure.tailnet_connector.MagicDnsSuffixSource` —
  reads `magicdns_suffix` only, cached by `mtime`, re-read lazily per
  connection (no filesystem watch). Env override: `HERMES_TAILNET_STATUS_PATH`.
- `hermes.shell_server.tailnet.api` (`GET /api/v1/tailnet`, `GET
  /api/v1/tailnet/peers`) — reads the full document. Env override:
  `HERMES_TAILNET_STATUS_PATH`.

Both readers are **fail-closed on any read error** (missing file, corrupt
JSON, wrong type): they treat it as "tailnet not configured" — never as
"route everything" or "assume online".

## 3. Daemon → root-helper handoff (connect / disconnect)

**Path:** `/run/hermes/tailscale-control/request.json` (single file, `action`
field distinguishes the two operations — mirrors
`remote_access_tunnel`'s `/run/hermes/remote-control/request.json` exactly,
same reasoning: the shell-server (`NoNewPrivileges=yes`, no capabilities)
cannot call `tailscale`/`systemctl` itself or verify a PAM password, so it
stages a validated request and a root oneshot, triggered by a `.path` unit
watching this exact path, performs the privileged action and **shreds the
file** — see `hermes-remote-access-control.path`'s doc comment for the
shred/verify contract this one must replicate).

**Directory:** already provisioned by the shared tmpfiles
(`/run/hermes/tailscale-control` 0700 `hermes:hermes`) — no ops-lane change
needed here. The egress lane's API does **not** create this directory (same
policy as `remote_access_tunnel/api.py`: if it's absent in production, OS
config is wrong and the write must fail loudly, not paper over it).

**Write discipline:** atomic (`tmp` + `rename`), mode `0600`, written by
`hermes.shell_server.tailnet.api` (`src/hermes/shell_server/tailnet/api.py`).

### 3.1 `action: "connect"`

```json
{
  "action": "connect",
  "requested_at": "2026-09-10T14:03:00+00:00",
  "auth_key": "tskey-auth-kXXXXXXXXXX-XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
}
```

Root-helper responsibilities (ops lane, `scripts/hermes-tailscale-control`):
1. Read + **immediately shred** this file (same discipline as
   `hermes-remote-access-control`).
2. Write `auth_key` to a private tmpfs location the helper itself controls
   (e.g. under its own `PrivateTmp=yes`) — **never** back into
   `/run/hermes/tailscale` verbatim from here; the helper has
   `CAP_DAC_OVERRIDE` (see §4) so it does not need the daemon to have staged
   it there.
3. Create `/var/lib/hermes/tailscale/enabled` (idempotent) so
   `hermes-tailscaled.path` brings `hermes-tailscaled.service` up if it
   isn't already running.
4. Run `tailscale up --auth-key=file:<private-tmp-path> --hostname=safent-agent
   --shields-up --accept-routes=false` against the node's own
   `tailscaled.sock`.
5. Shred the private tmp file (`finally`, always — even on `tailscale up`
   failure).
6. **Never** put the key in argv, `Environment=`, or any log line — the
   `file:<path>` auth-key form exists in `tailscale up` exactly so the key
   never appears in `ps`/`journalctl`.
7. Write the verdict (success or failure) to
   `/run/hermes/tailscale/last-attempt.json` — `{"at", "ok", "error_kind"}`,
   0644, **no key material** — for the status watcher to mirror into
   status.json's `last_attempt` (§2, 025 hallazgo D). This script does NOT
   write status.json directly (single-writer discipline stays with the
   watcher).

The egress lane **additionally** persists an encrypted copy in the
`SecretsVault` (AES-GCM under `master.key`) at
`/var/lib/hermes/tailscale-authkey.enc` before staging the handoff file —
**audit/recovery only**. The running node re-authenticates from its own
persisted `tailscaled` state (`/var/lib/hermes/tailscale`), not from this
blob; no lane needs to read it back at runtime. Env override:
`HERMES_TAILNET_AUTHKEY_VAULT_PATH`.

### 3.2 `action: "disconnect"`

```json
{
  "action": "disconnect",
  "requested_at": "2026-09-10T14:10:00+00:00",
  "password": "<device password, plaintext, shredded on read>"
}
```

Root-helper responsibilities: PAM-verify `password` against the device
account (identical mechanism to `hermes-remote-access-control`'s disable
path — `pam_unix` as root, `CAP_DAC_OVERRIDE` to read `/etc/shadow`). On
success: `tailscale down`, remove `/var/lib/hermes/tailscale/enabled`, stop
`hermes-tailscaled.service`. On failure: shred and abort — **no** `systemctl`
call, **no** `tailscale down`. The egress lane's UI polls `GET
/api/v1/tailnet` to observe the outcome (same pattern as remote-access's
disable — the shell-server never learns pass/fail synchronously).

The egress lane's API never verifies the password itself and never learns
whether it was correct — same trust boundary as
`remote_access_tunnel/api.py`.

## 4. Recommended root-helper unit shape (ops lane; not authored here)

Mirror `hermes-remote-access-control.service` exactly:
`Type=oneshot`, `User=root`, `NoNewPrivileges=no`,
`CapabilityBoundingSet=CAP_DAC_OVERRIDE CAP_SYS_ADMIN CAP_SETUID CAP_SETGID`,
`RestrictAddressFamilies=AF_UNIX`, `ProtectSystem=strict`, `PrivateTmp=yes`,
`ReadWritePaths=/run/hermes/tailscale-control /run/hermes/tailscale
/var/lib/hermes/tailscale`. `CAP_DAC_OVERRIDE` is what lets this root process
read the `hermes`-owned 0600 staged file and write into the
`hermes-tailscale`-owned state dir despite running capless-root in every
other respect — see the existing unit's own comment block for why plain
`root` does **not** imply DAC bypass in this repo's hardened units.

## 5. Loopback CONNECT proxy (tailscaled → egress-proxy)

**Address:** `127.0.0.1:1055` (from `hermes-tailscaled.service`'s
`--outbound-http-proxy-listen`). Fixed; not configurable per-install (the
egress-proxy connector defaults to it but honors
`HERMES_TAILNET_PROXY_HOST`/`HERMES_TAILNET_PROXY_PORT` if ever needed).

**Protocol:** plain HTTP CONNECT — `CONNECT host:port HTTP/1.1\r\nHost:
host:port\r\n\r\n`, expects a `... 200 ...` status line back, then raw
bidirectional passthrough (tailscaled dials `host` via MagicDNS and
WireGuard on the other side). Implemented in
`hermes.egress_proxy.infrastructure.tailnet_connector.TailnetUpstreamConnector`.

**Reachability invariant (already true today, verified in `plan.md` §"Why
it's safe"):** only the host-netns egress-proxy can reach `127.0.0.1:1055` —
the agent netns forward chains drop `127.0.0.0/8` and `100.64.0.0/10`. This
contract does not change that; it only defines what the egress-proxy sends
once it *is* the one dialing.

## 6. API contract (`src/hermes/shell_server/tailnet/api.py`, mounted in `main.py`)

| Method | Path | Body | Response | Notes |
|---|---|---|---|---|
| GET | `/api/v1/tailnet` | — | `{configured, online, node_name, magicdns_suffix, tailnet, peers:[{name,online}]}` | fail-soft "not configured" on any read error |
| GET | `/api/v1/tailnet/peers` | — | `{peers:[{name,online}]}` | subset of the above |
| POST | `/api/v1/tailnet/connect` | `{auth_key}` | `202 {staged: true}` | shape-validated (`^tskey-auth-[A-Za-z0-9_-]{8,200}$`), never echoes the key, vault + handoff per §3.1 |
| POST | `/api/v1/tailnet/disconnect` | `{password}` | `200 {staged: true}` | PAM-gated per §3.2, rate-limited (`PasswordRateLimiter`, 5/60s), never echoes the password |

Errors use the API's standard `{"detail": {"code": ..., "message": ...}}`
body via `HTTPException` (never `{"ok": false}` under a 2xx) — see
`owner_mfa_gate.require_owner_mfa` for the precedent this mirrors.

**No parallel allowlist:** granting an agent access to a tailnet host is done
through the pre-existing `POST /api/v1/egress/domains/grant` (DENY mode) —
the same HITL/grant flow as any other domain. This API only controls whether
the node is *joined* to the tailnet at all and reports its status; it never
touches `egress-grants.json` or the egress-proxy's policy engine.

## 7. What each lane still owes (tracked here, not invented by either side)

- **Ops lane:** the tmpfiles mode change in §2, the status watcher, the
  `hermes-tailscale-control.{service,path}` units + `scripts/hermes-tailscale-control`
  per §3/§4, and the `hermes-tailscaled.service` wiring already on
  `tailnet-022-base`.
- **Egress lane (this branch, done):** `tailnet_connector.py`,
  `proxy_handler.py`'s upstream selector, `__main__.py`'s router wiring,
  `shell_server/tailnet/api.py`, the Seguridad UI card, and this document.
- **Neither lane, explicitly out of v1 scope (plan.md §Scope):** SSH/DB raw
  TCP forwarding, subnet routes, exit-node, non-FQDN (bare IP) tailnet
  destinations.
