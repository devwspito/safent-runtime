"""Entrypoint del proxy de egress filtrante.

Arrancado por la unit systemd ``hermes-egress-proxy.service``:

    python3 -m hermes.egress_proxy [--systemd-notify]

Variables de entorno:
    HERMES_EGRESS_LISTEN_HOST   Dirección de escucha del proxy (def. 10.200.0.1)
    HERMES_EGRESS_LISTEN_PORT   Puerto del proxy (def. 3128)
    HERMES_EGRESS_CONTROL_SOCK  Ruta del socket de control root
                                (def. /run/hermes/egress-proxy.sock)
    HERMES_EGRESS_MODE          Modo global inicial: open-logged | default-deny
                                (def. default-deny — Fix-5: fail-closed)
    HERMES_EGRESS_MCP_GATEWAY   IP del gateway (host side) del netns hermes-mcp
                                donde el proxy también escucha (def. 10.200.1.1)
    HERMES_EGRESS_MCP_CLIENT_IP IP de origen de los hijos MCP (netns hermes-mcp).
                                Se PINEA con default-deny + hosts curados/concedidos,
                                inmune a replace_global() del navegador (def. 10.200.1.2)
    HERMES_EGRESS_MCP_GRANTS    Ruta del fichero JSON de hosts MCP concedidos por el
                                dueño (def. /var/lib/hermes/mcp-egress-grants.json). Se
                                lee al arrancar y se fusiona con los hosts curados.

Fix-5: el default global es DEFAULT_DENY (deny-all, whitelist vacía) hasta
que el daemon empuje política via el socket de control.  OPEN_LOGGED es
un opt-in explícito para discovery — NUNCA el default de arranque de un
SO público.  Configurar HERMES_EGRESS_MODE=open-logged para discovery.

C1 PASS-2 (2026-06-19): los hijos MCP corren en un netns SEPARADO (hermes-mcp,
IP 10.200.1.2) y llegan al proxy por un SEGUNDO gateway (10.200.1.1:3128). Su
política se PINEA a default-deny y es inmune al socket de control del navegador:
una sesión teaching del navegador que empuje open-logged via replace_global() NO
amplía el egress del MCP (bypass #1 cerrado). El MCP y el navegador ya no
comparten ni netns ni IP de origen ni plano de política.

C1 PASS-4 (2026-06-19): el pin del MCP ya NO nace con whitelist vacía e inmutable
(eso dejaba MUERTOS los MCP de red). Al arrancar fusionamos (a) los hosts CURADOS
de los servidores BYOK que enviamos y verificamos (Open Design, Replicate,
Context7) — pre-concedidos para que funcionen out-of-the-box — con (b) los hosts
que el dueño haya concedido y persistido en el fichero de grants. El plano sigue
default-deny: sólo esos hosts pasan; todo lo demás (evil.com, npm/PyPI, un MCP no
concedido) se deniega. El dueño puede conceder más en caliente vía el socket de
control con el marcador reservado MCP_GRANT_SESSION (API de elevación de egress).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys

from hermes.egress_proxy.domain.policy import EgressMode, EgressPolicyEngine, SessionPolicy
from hermes.egress_proxy.infrastructure.audit_sink import StructlogAuditSink
from hermes.egress_proxy.infrastructure.control_socket import ControlSocketServer
from hermes.egress_proxy.infrastructure.proxy_handler import ProxyConnectionHandler

_LISTEN_HOST = os.environ.get("HERMES_EGRESS_LISTEN_HOST", "10.200.0.1")
_LISTEN_PORT = int(os.environ.get("HERMES_EGRESS_LISTEN_PORT", "3128"))
_CONTROL_SOCK = os.environ.get(
    "HERMES_EGRESS_CONTROL_SOCK", "/run/hermes/egress-proxy.sock"
)
# Fix-5: default = DEFAULT_DENY (fail-closed). Use HERMES_EGRESS_MODE=open-logged
# explicitly for discovery mode — must never be the production default.
_EGRESS_MODE_RAW = os.environ.get("HERMES_EGRESS_MODE", EgressMode.DEFAULT_DENY)

# C1 PASS-2: the MCP children live in a SEPARATE netns (hermes-mcp). The proxy also
# listens on that netns' host-side gateway and PINS the MCP source IP to an immutable
# default-deny (registries-only) policy. Browser control-socket pushes target
# __global__ (governs the browser's 10.200.0.2) — never this pinned client.
_MCP_GATEWAY_HOST = os.environ.get("HERMES_EGRESS_MCP_GATEWAY", "10.200.1.1")
_MCP_CLIENT_IP = os.environ.get("HERMES_EGRESS_MCP_CLIENT_IP", "10.200.1.2")

# C1 PASS-4: owner-granted MCP hosts persist here and are merged into the MCP pin at boot.
# Written by the shell-server elevation API (egress_api /api/v1/egress/mcp/*); read
# here as the daemon `hermes-egress` (group hermes → it can read the file under
# /var/lib/hermes written 0640 group hermes).
_MCP_GRANTS_PATH = os.environ.get(
    "HERMES_EGRESS_MCP_GRANTS", "/var/lib/hermes/mcp-egress-grants.json"
)

# CURATED BYOK servers WE ship + vet (mirror of the BYOK keys in hermes-mcp-launcher and
# dbus_runtime_service._MCP_BYOK_ENV_KEYS). Their known API hosts are PRE-GRANTED so the
# servers work out-of-the-box; everything else stays default-deny. Open Design's host is
# NOT here: OD_DAEMON_URL is owner-supplied (could be any self-hosted daemon), so the
# owner grants that specific host explicitly via the elevation API — we cannot vet an
# unknown URL up front. Subdomain matching applies (see policy._matches_whitelist), so
# `replicate.com` also covers `api.replicate.com`, and `context7.com` covers its API host.
_CURATED_MCP_HOSTS: frozenset[str] = frozenset({
    "replicate.com",      # Replicate MCP (replicate-mcp): api.replicate.com
    "context7.com",       # Context7 MCP: docs endpoint
})

logger = logging.getLogger("hermes.egress_proxy")


def _load_mcp_grants() -> frozenset[str]:
    """Read the owner-granted MCP hosts from the persisted grants file (best-effort).

    Returns an empty set on any error (missing file at first boot, malformed JSON) —
    fail-closed: a grant we cannot read simply does not widen the MCP plane. The file is
    written by the shell-server elevation API as ``{"domains": [...]}``.
    """
    try:
        with open(_MCP_GRANTS_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return frozenset()
    domains = data.get("domains", []) if isinstance(data, dict) else []
    return frozenset(
        d.lower().strip().rstrip(".")
        for d in domains
        if isinstance(d, str) and d.strip()
    )


def _sd_notify(message: str) -> None:
    notify_socket = os.environ.get("NOTIFY_SOCKET")
    if not notify_socket:
        return
    import socket  # noqa: PLC0415

    if notify_socket.startswith("@"):
        notify_socket = "\0" + notify_socket[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(notify_socket)
            sock.sendall(message.encode())
    except OSError as exc:
        logger.warning("sd_notify failed: %s", exc)


def _configure_logging() -> None:
    try:
        from hermes.logging_setup import configure_structured_logging  # noqa: PLC0415

        configure_structured_logging(service="hermes-egress-proxy", version="0.4.0")
    except ImportError:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
            stream=sys.stderr,
        )


def _resolve_global_mode() -> EgressMode:
    try:
        return EgressMode(_EGRESS_MODE_RAW)
    except ValueError:
        # Fix-5: fail-closed on invalid mode — default to DEFAULT_DENY, not open.
        logger.warning(
            "HERMES_EGRESS_MODE valor inválido %r — usando default-deny (fail-closed)",
            _EGRESS_MODE_RAW,
        )
        return EgressMode.DEFAULT_DENY


async def _run(*, systemd_notify: bool) -> None:
    _configure_logging()
    logger.info("hermes.egress_proxy.starting")

    global_mode = _resolve_global_mode()
    global_policy = SessionPolicy(
        session_id="__global__",
        mode=global_mode,
    )
    policy_engine = EgressPolicyEngine(global_policy=global_policy)

    # C1 PASS-4/5: PIN the MCP source IP to default-deny + (curated BYOK hosts ∪ owner
    # grants). The MCP plane stays DEFAULT_DENY — ONLY those explicit hosts pass; a
    # browser open-logged push to __global__ can NEVER widen it (the pin is resolved
    # before the global and the control socket cannot replace a pinned entry wholesale).
    # PASS-3's empty-whitelist pin made network-MCPs DEAD (403 on every host with no
    # grant path); pre-granting the curated hosts WE vet + loading the owner's persisted
    # grants restores functionality without reopening the hole. The owner can grant more
    # at runtime via the control socket's reserved MCP_GRANT_SESSION marker.
    #
    # C1 PASS-5 class fix: the CURATED set is passed as the immutable ``seed`` (floor),
    # the owner grants as the policy whitelist. The engine ALWAYS recomputes the pinned
    # whitelist as seed ∪ owner_grants, so a later re-push of the owner-only grants file
    # (apply_persisted_grants, or any grant/revoke cycle) can NEVER wipe the curated hosts.
    owner_grants = _load_mcp_grants()
    policy_engine.pin_policy(
        client_id=_MCP_CLIENT_IP,
        policy=SessionPolicy(
            session_id="__mcp__",
            mode=EgressMode.DEFAULT_DENY,
            domains_whitelist=owner_grants,
        ),
        seed=_CURATED_MCP_HOSTS,
    )
    logger.info(
        "hermes.egress_proxy.mcp_pinned",
        extra={
            "mcp_client_ip": _MCP_CLIENT_IP,
            "policy": "default-deny",
            "curated_hosts": len(_CURATED_MCP_HOSTS),
            "owner_granted_hosts": len(owner_grants),
            "granted_hosts": len(policy_engine.pinned_whitelist(_MCP_CLIENT_IP)),
        },
    )

    audit_sink = StructlogAuditSink()
    handler = ProxyConnectionHandler(
        policy_engine=policy_engine,
        audit_sink=audit_sink,
    )

    # Two listeners share ONE policy engine: the browser gateway (10.200.0.1) and the
    # MCP gateway (10.200.1.1). The handler keys policy by client IP, so the MCP's
    # 10.200.1.2 resolves to the pinned policy regardless of which socket it arrived on.
    proxy_server = await asyncio.start_server(
        handler.handle,
        host=_LISTEN_HOST,
        port=_LISTEN_PORT,
    )
    mcp_proxy_server = await _start_mcp_listener(handler)

    control = ControlSocketServer(
        socket_path=_CONTROL_SOCK,
        policy_engine=policy_engine,
    )

    loop = asyncio.get_event_loop()
    loop.add_signal_handler(
        signal.SIGTERM, _request_shutdown, proxy_server, mcp_proxy_server, control
    )

    logger.info(
        "hermes.egress_proxy.ready",
        extra={
            "listen": f"{_LISTEN_HOST}:{_LISTEN_PORT}",
            "mcp_listen": (
                f"{_MCP_GATEWAY_HOST}:{_LISTEN_PORT}" if mcp_proxy_server else "disabled"
            ),
            "control_sock": _CONTROL_SOCK,
            "global_mode": global_mode,
        },
    )

    if systemd_notify:
        _sd_notify("READY=1\nSTATUS=hermes-egress-proxy ready\n")

    tasks = [
        _serve_proxy(proxy_server),
        control.serve_forever(),
    ]
    names = ["proxy_server", "control_socket"]
    if mcp_proxy_server is not None:
        tasks.append(_serve_proxy(mcp_proxy_server))
        names.append("mcp_proxy_server")

    _results = await asyncio.gather(*tasks, return_exceptions=True)
    # Make a control-socket failure LOUD: a swallowed bind error here left the
    # proxy at default-deny forever (no policy push possible → browser/terminal
    # reached no domain) with zero diagnostics. Surface any task exception.
    for _name, _res in zip(names, _results):
        if isinstance(_res, BaseException) and not isinstance(_res, asyncio.CancelledError):
            logger.error("hermes.egress_proxy.%s_failed: %r", _name, _res)

    logger.info("hermes.egress_proxy.stopped")


async def _start_mcp_listener(
    handler: ProxyConnectionHandler,
) -> asyncio.AbstractServer | None:
    """Bind a SECOND proxy listener on the MCP netns gateway (10.200.1.1:3128).

    FAIL-SOFT, never fail-OPEN: if the MCP gateway IP is not present (the hermes-mcp
    netns/veth is not up on this boot), we log and return None — the MCP simply has no
    egress path (its netns nft is default-deny and there is no proxy to reach), which is
    the secure outcome. We do NOT fall back to sharing the browser listener (that would
    re-merge the two egress identities and reopen bypass #1). Binding to a specific IP
    that does not exist raises OSError(EADDRNOTAVAIL); we treat that as "MCP netns
    absent" rather than crashing the whole proxy (the browser jail must keep working).
    """
    try:
        return await asyncio.start_server(
            handler.handle,
            host=_MCP_GATEWAY_HOST,
            port=_LISTEN_PORT,
        )
    except OSError as exc:
        logger.warning(
            "hermes.egress_proxy.mcp_listener_unavailable: %s (MCP egress disabled "
            "until hermes-mcp netns is up — fail-soft, NOT fail-open)",
            exc,
        )
        return None


def _request_shutdown(
    proxy_server: asyncio.AbstractServer,
    mcp_proxy_server: asyncio.AbstractServer | None,
    control: "ControlSocketServer",
) -> None:
    proxy_server.close()
    if mcp_proxy_server is not None:
        mcp_proxy_server.close()
    control.close()


async def _serve_proxy(server: asyncio.AbstractServer) -> None:
    async with server:
        await server.serve_forever()


def main() -> int:
    args = sys.argv[1:]
    systemd_notify = "--systemd-notify" in args
    try:
        asyncio.run(_run(systemd_notify=systemd_notify))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
