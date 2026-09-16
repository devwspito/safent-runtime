# 022 v2 — Governed SSH over the tailnet

Implements the v1 plan's deferred item: "SSH/DB (raw TCP) deferred to v2 (needs a
separate per-host TCP forwarder, not a CONNECT widening)". Owner's need: *"conectarse
por SSH no se puede hacer y no tenemos mecanismo"*.

## Contract with the ops lane (owned by the ops worktree, read-only here)

- `tailscaled --socks5-server=127.0.0.1:1056` — host netns, loopback only. The agent
  netns cannot reach it (same load-bearing invariant as v1's CONNECT proxy on 1055;
  the forward-chain DROP on `127.0.0.0/8` + `100.64.0.0/10` is unchanged by this work).
- `/run/hermes/tailscale/status.json` — `{node_name, magicdns_suffix, online,
  peers:[{name, online}]}`. tailnet_ssh only READS this file; it never writes it.

## Design

```
src/hermes/tailnet_ssh/
  tool_names.py                          TAILNET_SSH_TOOL_NAMES — zero-dep single source
  domain/            host.py, command.py, remote_path.py, limits.py, errors.py
  application/        ports.py (Protocols), host_resolution.py,
                       tailnet_ssh_use_case.py, tailnet_file_use_case.py
  infrastructure/     status_json_directory.py, json_host_allowlist_store.py,
                       ssh_subprocess_executor.py, socks_proxy_command.py,
                       hash_chain_audit_port.py
```

`TailnetSshUseCase.execute()`: validate `command`/`timeout_s`/`stdin` (domain VOs) →
resolve `host` against the live directory (`resolve_host`, rejects IP literals /
non-tailnet hosts) → `SshExecutorPort.run()` → `AuditPort.record_ssh_call()`. It
performs **zero authorization** of its own — same trust boundary as
`DelegationSurfaceAdapter`: "this adapter performs ZERO additional authorization of
its own... duplicating that check here would risk drifting from the single
choke-point." The owner-facing HITL gate runs *before* this use case is ever invoked
(see below). `TailnetFileGetUseCase`/`TailnetFilePutUseCase` reuse the SAME executor
with `cat -- <path>` / `cat > <path>` (shell-quoted) instead of adding an scp/sftp
dependency — deliberately small (5 MiB cap), not a file-transfer product.

### Execution: `ssh` + ProxyCommand

`SubprocessSshExecutor` spawns `ssh` as an **argv list, never `shell=True`** — `command`
is ONE argv element ssh forwards to the remote shell (normal ssh behaviour, not local
shell expansion). Options:

- `BatchMode=yes` — never prompts.
- `StrictHostKeyChecking=accept-new` against a Safent-owned
  `/var/lib/hermes/tailscale/known_hosts` (never the operator's own).
- `ProxyCommand=<python> -m hermes.tailnet_ssh.infrastructure.socks_proxy_command %h %p`.
- `stdin=DEVNULL` unless the caller supplied one (never the daemon's own inherited stdin).
- Output capped at 256 KiB/stream (5 MiB for file get/put), `truncated` flag returned.
- `timeout_s` (1–300) enforced via `subprocess.run(timeout=...)` →
  `RemoteCommandTimeoutError`.

**ProxyCommand choice.** `nc -X 5 -x <proxy> %h %p` was rejected: the base image's
`netcat-openbsd` (Debian/Ubuntu) does **not** build with SOCKS support, so `-X` is not
guaranteed. Instead: a small pure-Python SOCKS5 CONNECT client
(`socks_proxy_command.py`, RFC 1928 greeting + CONNECT + bidirectional stdio↔socket
shuttle), invoked via `python3 -m ...`. This needs *nothing* beyond the interpreter the
daemon already ships — no new package for the proxy hop itself.

**Containerfile gap (not edited here — out of `tailnet-ssh` lane's scope, ops-owned
file).** `ops/container/Containerfile` currently installs neither `openssh-client` nor
`netcat-openbsd` (checked both `apt-get install` blocks, lines 36 and 301). **The `ssh`
binary is not guaranteed present in the built image today.** The ops lane must add
`openssh-client` (only — `netcat-openbsd` is NOT needed, see above) to the existing
`apt-get install` list at line 36.

### Identity

Tailscale SSH (tailnet ACL authorises the node) — no keypair, no `IdentityFile`. Vault-
backed key auth is **deferred**: not trivial (needs a vault-read call inside the ssh
argv-building path plus a key-lifecycle story) and the owner's default ask ("no
mecanismo") is satisfied without it.

## Governance

`tailnet_ssh` / `tailnet_file_get` / `tailnet_file_put` are **not** native Nous tools
(absent from `nous_tool_risk_map`) and are **not** wired through `CapabilityRegistry`/
`CapabilityBroker` (that path's async pending-then-replay-later HITL cannot express
"first use per host, then flows" without a second, conflicting approval surface on
every subsequent call — see rejected alternatives below). They are gated exactly where
every other *always-ask* capability in this codebase is: `security_hook.py`'s
`_pre_tool_call_hook`, the same mechanism the per-SESSION browser consent
(`_resolve_browser_session_consent`) uses — this is a new, symmetric **Step
1.6-tailnet_ssh**.

- **Classification** (defense in depth, does not itself gate anything):
  `tool_delicacy._DELICATE_NON_NATIVE` (DELICATE, not MOST_DELICATE — simple
  Aprobar/Rechazar, no TOTP), `tool_sensitivity.SensitivityCategory.REMOTE_EXEC` (new
  category), `tool_policy.TOOL_CATALOG` (category "Tailnet / SSH" — owner can hard-
  disable via Step 1.5, independent of the HITL gate below).
- **The real gate**: `security_hook._resolve_tailnet_ssh_consent`.
  1. Resolve `host` against `status.json` (`resolve_host`) — IP literal / unknown host
     → **block, no card** (validation, not authorization).
  2. Already on `JsonHostAllowlistStore` (`/var/lib/hermes/tailscale/ssh-allowlist.json`)
     → **ALLOW, no card**.
  3. First use of this (canonical) host: block-and-resume card
     (`_resolve_native_danger_approval`, `session_key="tailnet-ssh\x00<host>"`). Approve
     → **persists** the host to the allow-list (survives daemon restart, covers every
     future conversation) and returns the SSH result inline. Deny → blocked, nothing
     persisted.
  4. **No conversation (autonomous cycle) + host never approved → fail-CLOSED.**
     Deliberately the *opposite* of the browser gate's fail-open: browsing has the
     egress allowlist as an independent governing floor underneath the session
     consent; tailnet_ssh has no other floor, so an unattended cycle must never be the
     first to reach a brand-new host. An already-approved host still flows
     autonomously — the owner already vetted it.
- **A bundle cannot set it `auto`**: Step 1.6-tailnet_ssh runs *before* the
  owner-preapproval short-circuit (mirrors Step 1.6-browser's placement) and is keyed
  purely on `tool_name`, not on `tool_delicacy`/native classification or the
  `tool_policy` enable toggle — there is no code path from a policy overlay/bundle to
  skipping it. `tests/unit/runtime/test_tailnet_ssh_consent.py` locks this.
- **Audit**: `hash_chain_audit_port.HashChainAuditPort` → the SAME WORM hash-chain
  (`AuditHashChainSigner`, new `AuditKind.TAILNET_SSH_EXECUTED`) as the rest of the
  daemon. Payload = `{host, command (full — not a secret), exit_code, duration_ms,
  output_truncated}`. Output **bodies** never reach the audit log.

### Rejected alternative: CapabilityBroker/SurfaceKind routing

`delegate_to_colleague`-style routing (`_REGISTRY_TABLE` + a new `SurfaceKind` +
`SurfaceAdapterPort` + `__main__.py` wiring) was considered and rejected for v2: the
broker's `_needs_hitl` re-evaluates on **every** call (risk=HIGH is unconditional,
"the autonomy_level NUNCA puede eximir HIGH") with its own async pending-approval
surface, keyed on the exact-params digest — it cannot express "first use per host,
persisted, then flows" without a SECOND, conflicting card on every later call to an
already-approved host. The Step-1.6-style synchronous block-and-resume gate is the
correct fit and has a direct precedent (browser).

## Owner-facing flow

1. Agent calls `tailnet_ssh(host="db1", command="systemctl status app")`.
2. Daemon resolves `db1` → `db1.<magicdns-suffix>`; not on the allow-list yet.
3. Owner sees an approval card: *"El agente quiere conectarse por SSH a
   «db1.<suffix>» en tu tailnet y ejecutar un comando ahí. Al aprobar, este host queda
   permitido para futuras conexiones."*
4. Owner approves (Aprobar/Rechazar, no MFA) → `db1.<suffix>` is written to
   `ssh-allowlist.json` → `ssh` runs through the SOCKS5 proxy → stdout/stderr/exit code
   return to the agent (capped, truncation flagged) → WORM audit entry recorded.
5. Any later `tailnet_ssh`/`tailnet_file_get`/`tailnet_file_put` call to `db1.<suffix>`,
   in any conversation, flows with no card until the owner revokes it
   (`JsonHostAllowlistStore.revoke`, exposed via `DELETE /api/v1/tailnet/ssh-hosts/{host}`
   — see Cableado below).

## Deferred / follow-ups

- **Containerfile**: add `openssh-client` (see above) — ops-owned file, not edited here.
- **Vault-backed SSH key identity**: not trivial, deferred (Tailscale SSH covers the
  owner's stated need without it).
- ~~**Owner-facing allow-list management UI**~~ — done, see Cableado below
  (`GET`/`DELETE /api/v1/tailnet/ssh-hosts`, Seguridad → Tailnet →
  "Equipos con SSH aprobado").
- ~~**Live LLM-tool-call wiring**~~ — done, see Cableado below. It turned out NOT to
  need the external `hermes-agent` native catalog after all: `tailnet_ssh` is not a
  native-shaped tool the way `ha_call_service`/`browser_*` are — it is a
  `CapabilityRegistry`-routed tool, the SAME class as `memory`/`delegate_to_colleague`,
  and this repo already had a generic mechanism (`runtime/capability_tool_specs.py`)
  for injecting THAT class of tool into the LLM schema without touching the external
  package. `TailnetSshUseCase`/`TailnetFileGetUseCase`/`TailnetFilePutUseCase` remain
  exactly the handlers this wiring calls — nothing about them changed.

## Cableado

El camino completo, chat → tool → broker → gate → executor: el modelo ve
`tailnet_ssh`/`tailnet_file_get`/`tailnet_file_put` en su esquema porque
`capability_registry.py` los registra como `ExtendedCapabilityBinding`
(`surface_kind=SurfaceKind.TAILNET_SSH`, `risk=LOW`, `auto_executable=True`)
y `runtime/capability_tool_specs.py` traduce esa entrada a un `ToolSpec` con
el JSON schema de este documento — el mismo mecanismo genérico que ya
exponía `memory` o `navigate_app`, no uno nuevo. Al llamarla, Nous invoca
primero `security_hook.make_pre_tool_call_hook`, cuyo Paso 1.6-tailnet_ssh
(`_resolve_tailnet_ssh_consent`) es el ÚNICO punto de autorización: host
nunca aprobado + conversación activa → tarjeta; sin conversación → bloqueo en
frío; host ya en `JsonHostAllowlistStore` → pasa sin fricción. Solo si este
paso lo permite se invoca el handler del `ToolSpec`, que arma un
`ToolCallProposal` (`op=<nombre de la tool>`) y lo despacha a
`CapabilityBroker.dispatch`; el binding es `risk=LOW`/`auto_executable=True`
a propósito, para que el broker NO vuelva a pedir su propio HITL por-llamada
— lo que la sección "Rejected alternative" de arriba rechazó fue dejar que
ESE HITL gobernara el permiso por-host, no el mecanismo de enrutar el
despacho ya autorizado por un `SurfaceAdapterPort`, que es el mismo patrón
de `memory`/el navegador y aquí no reintroduce una segunda tarjeta. El
broker delega en `SurfaceAdapterDispatcher`, que resuelve
`SurfaceKind.TAILNET_SSH` a `TailnetSshSurfaceAdapter` (compuesto en
`runtime/__main__.py` con `SubprocessSshExecutor`,
`StatusJsonTailnetDirectory` y `HashChainAuditPort`), y este llama — vía
`asyncio.to_thread`, porque la ejecución es síncrona y puede tardar hasta
300s — al caso de uso (`TailnetSshUseCase`/`TailnetFileGetUseCase`/
`TailnetFilePutUseCase`), que resuelve el host, ejecuta `ssh` y audita en el
hash-chain WORM (`AuditKind.TAILNET_SSH_EXECUTED`); el resultado vuelve tal
cual al modelo. Para revocar, el dueño usa "Equipos con SSH aprobado" en
Seguridad → Tailnet, que llama a `DELETE /api/v1/tailnet/ssh-hosts/{host}`
(`shell_server/tailnet/api.py`) — exige TOTP vía `require_owner_mfa` y luego
llama a `JsonHostAllowlistStore.revoke(host)` sobre el MISMO fichero que lee
el Paso 1.6, así que la siguiente llamada a ese host vuelve a pedir
aprobación; esta revocación queda logueada pero, a diferencia de la
ejecución SSH, todavía NO entra en el hash-chain WORM — `shell_server` corre
en un proceso separado del daemon que posee la clave de firma del audit
chain, y cerrar ese hueco exige un mutator D-Bus hacia el daemon o compartir
la clave entre procesos, ninguna de las dos trivial; queda como follow-up
marcado, no resuelto aquí en silencio.
