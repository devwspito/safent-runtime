# 022 — Native governed Tailscale connectivity (agent → owner tailnet, outbound)

Owner-approved 2026-07-13. Two specialists (software-architect + security-engineer) converged
independently on the SAME design, grounded in the code.

## Decision
Run `tailscaled --tun=userspace-networking` exposing a loopback HTTP-CONNECT proxy
(`127.0.0.1:1055`). The existing `hermes-egress-proxy` is the ONLY client of that loopback
proxy and routes owner-allowlisted tailnet FQDNs (matching the MagicDNS suffix) to it; all
other egress stays on today's direct path. **No parallel allowlist** — a tailnet host is
granted exactly like any domain (`egress_api` grant, DENY mode). **No change to run-safent.sh
caps, no `/dev/net/tun`, no NET_ADMIN, no seccomp edit.**

## Why it's safe (load-bearing invariant)
The agent planes (browser/exec/mcp) run in netns whose forward chains DROP `127.0.0.0/8` and
`100.64.0.0/10` (Tailscale CGNAT) — `browser-host.nft:38`, `mcp-host.nft:37`. So the agent
**cannot reach** tailscaled's loopback proxy; only the host-netns egress-proxy can. Agent
tailnet access is therefore forced through the audited proxy + allowlist + WORM audit + HITL.
Kernel-tun is REJECTED (would require punching the CGNAT hole = blows the cage).

## Auth key custody (top-tier secret)
Owner pastes `tskey-auth-…` in the UI → stored in `SecretsVault` (AES-GCM under master.key) →
a PAM'd root helper (clone of `hermes-remote-access-control`) writes it to tmpfs 0600 and runs
`tailscale up --auth-key=file:<tmpfs> --hostname=safent-agent --shields-up --accept-routes=false`
then SHREDS it. NEVER argv/env/`Environment=`/git/logs. Node key persists in statedir → no
re-auth on updates. Server-side: owner uses a TAGGED key (`tag:safent-agent`) with a
default-deny ACL; no exit-node, no subnet routes.

## Privilege
`tailscaled` runs as new uid `hermes-tailscale`, host netns, capless, ProtectSystem=strict,
InaccessiblePaths on secrets, ProtectProc=invisible. Off by default (`ConditionPathExists`
marker + `.path` unit for live opt-in). Agent units untouched.

## Scope
v1 = HTTPS/TLS tailnet services (SNI-verifiable), FQDN-only destinations. SSH/DB (raw TCP)
deferred to v2 (needs a separate per-host TCP forwarder, not a CONNECT widening).

## Sovereign tier
Self-hosted **Headscale** (owner-run control plane, no third party) for the sovereign claim;
Tailscale SaaS only as a disclosed convenience tier (Tailscale Inc. = coordination trust party,
cannot read WireGuard traffic). Vendor holds no key/admin.

## Files

**Done (egress lane, `tailnet-egress` branch):**
- ADD `src/hermes/egress_proxy/infrastructure/tailnet_connector.py` —
  `MagicDnsSuffixSource` (status.json reader, mtime-cached, fail-closed),
  `TailnetUpstreamConnector` (CONNECT framing to `127.0.0.1:1055`),
  `UpstreamRouter` (suffix-based selector, implements the same port as its
  two delegates).
- ADD `src/hermes/shell_server/tailnet/api.py` + `__init__.py` — `GET
  /api/v1/tailnet`, `GET /api/v1/tailnet/peers`, `POST
  /api/v1/tailnet/connect`, `POST /api/v1/tailnet/disconnect`.
- ADD `specs/022-tailnet-connectivity/contracts.md` — the ops↔egress handoff
  contract (status.json schema + required tmpfiles permission fix,
  `/run/hermes/tailscale-control/request.json` schema, loopback proxy
  protocol, API contract).
- ADD tests: `tests/unit/egress_proxy/test_tailnet_connector.py` (25 —
  suffix cache/invalidation, host-suffix matching, CONNECT framing,
  typed-error failures), `tests/unit/egress_proxy/test_tailnet_routing_integration.py`
  (2 — end-to-end `ProxyConnectionHandler` + `UpstreamRouter` against a fake
  loopback CONNECT proxy: suffix-matched host routes to it, non-suffix host
  never touches it), `tests/unit/shell_server/test_tailnet_api.py` (21),
  `frontend/src/views/TailnetSection.test.tsx` (8).
- CHANGE `egress_proxy/application/ports.py` — added `UpstreamConnector`
  port, `UpstreamConnectError`/`UpstreamInternalAddressError`.
- CHANGE `egress_proxy/infrastructure/proxy_handler.py` — added
  `DirectUpstreamConnector` (wraps the pre-existing direct-dial path
  unchanged); `ProxyConnectionHandler` takes an injectable
  `upstream_connector` (defaults to `DirectUpstreamConnector`, so the 97
  pre-existing tests pass unmodified); the two CONNECT dial sites
  (SNI-enforced + open-logged) now call `self._upstream.connect(...)`
  instead of resolving/dialing inline. The plain-HTTP dial site is
  untouched (v1 scope = HTTPS/TLS only).
- CHANGE `egress_proxy/__main__.py` — builds the `UpstreamRouter` from
  `HERMES_TAILNET_STATUS_PATH` (default `/run/hermes/tailscale/status.json`)
  and injects it into the handler.
- CHANGE `shell_server/main.py` — mounts `create_tailnet_router(vault=vault)`.
- CHANGE `frontend/src/api/types.ts` + `client.ts` — `TailnetStatus`/`TailnetPeer`
  types, `getTailnetStatus`/`getTailnetPeers`/`connectTailnet`/`disconnectTailnet`.
- CHANGE `frontend/src/views/SeguridadView.tsx` — new `TailnetSection` card
  (state: no configurado / conectando / conectado como `node_name` en
  `tailnet`; paste auth-key form; device-password disconnect; MagicDNS
  suffix + "se conceden como cualquier dominio en Egress"; peers list).

**Owed (ops lane, `tailnet-ops` branch — see contracts.md §7):** systemd
`hermes-tailscale-control.{service,path}`; `scripts/hermes-tailscale-control`
(root helper: reads/shreds the staged request, PAM-verifies disconnect,
runs `tailscale up`/`down`); the status watcher that writes `status.json`;
the tmpfiles permission fix on `/run/hermes/tailscale` (0700→0711, §2 of
contracts.md — status.json is otherwise unreachable by the shell-server and
the egress-proxy, both different uids). `hermes-tailscaled.{service,path}`
and the Containerfile bake already landed on `tailnet-022-base`.

NOT touched: `run-safent.sh`, `ops/**`, `scripts/**`,
`src/hermes/tailnet_ssh/**` (other lanes' territory).

## MUST verify before publish
1. tailscaled comes up in userspace mode on a fresh image (no /dev/net/tun, no cap added).
2. RED-TEAM INVARIANT: from EACH agent netns, `curl 127.0.0.1:1055` FAILS (unreachable).
3. egress-proxy routes suffix-matched host → tailnet connector; everything else unchanged.
4. No secret in argv/env (`systemctl show`, `/proc/<pid>/cmdline`) or logs.
Final owner-side test (real tailnet reach) needs the owner's auth key — not testable here.

## Open questions (owner)
- disconnect/connect require device-password PAM gate? (recommend yes)
- persistent node (recommend, appliance) vs ephemeral.
- v1 = HTTPS internal apps confirmed (SSH/DB deferred).
