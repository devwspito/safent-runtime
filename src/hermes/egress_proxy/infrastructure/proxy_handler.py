"""Manejador asyncio del proxy HTTP CONNECT.

Protocolo soportado:
  - CONNECT host:port HTTP/1.x → extrae host, evalúa política SNI-aware,
    abre túnel TCP pasante (passthrough de bytes, sin descifrar TLS).

Fix-4 (SNI enforcement): en DEFAULT_DENY, lee el ClientHello ANTES de abrir
el upstream, deniega si SNI ∉ whitelist o si el ClientHello no tiene SNI
(previene evasión vía ECH / CONNECT con host distinto al real). La conexión
upstream se abre al SNI verificado, no al host del header CONNECT.

Fix-7 (HTTP plano deshabilitado): en DEFAULT_DENY y en cualquier sesión con
credenciales, HTTP plano (GET/POST sin CONNECT) es rechazado — el cliente no
controla el Host: header. Solo se permite HTTP plano en OPEN_LOGGED para
CRL/OCSP via allow-list explícita.

Cada decisión se registra en el EgressAuditSink.

El session_id por conexión se resuelve con la siguiente heurística
(en ausencia de un mecanismo de identificación de sesión del navegador):
se usa la IP de origen del cliente.  Si en el futuro se añade un header
X-Hermes-Session-ID, se toma preferentemente de él.  Esto es correcto
porque en el netns hermes-browser todas las conexiones tienen la misma
IP (10.200.0.2); el session_id real lo empuja el controlador al socket
de control — la política se busca por session_id, y si no hay sesión
registrada se usa la política global.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING

from hermes.egress_proxy.application.ports import EgressAuditSink
from hermes.egress_proxy.domain.host_parser import (
    HostParseError,
    parse_connect_line,
)
from hermes.egress_proxy.domain.policy import EgressMode, EgressPolicyEngine
from hermes.egress_proxy.domain.sni_parser import SNIParseError, parse_sni

if TYPE_CHECKING:
    pass

logger = logging.getLogger("hermes.egress_proxy.handler")

_CONNECT_ESTABLISHED = b"HTTP/1.1 200 Connection established\r\n\r\n"
_CONNECT_FORBIDDEN = b"HTTP/1.1 403 Forbidden\r\n\r\n"
_BUFFER_SIZE = 65536
_HEADER_MAX_BYTES = 8192
_TUNNEL_TIMEOUT_S = 60.0
_CONNECT_TIMEOUT_S = 10.0
_SNI_PEEK_TIMEOUT_S = 2.0
_SNI_PEEK_SIZE = 4096  # enough for a full ClientHello with extensions

# Regex to detect bare IP literals in CONNECT target.
_IP_LITERAL_RE = re.compile(
    r"^(\d{1,3}\.){3}\d{1,3}$|"  # IPv4
    r"^\[?[0-9a-fA-F:]+\]?$"     # IPv6 (with or without brackets)
)


def _is_ip_literal(host: str) -> bool:
    """Return True when host is a raw IP address (no hostname to check SNI against)."""
    return bool(_IP_LITERAL_RE.match(host))


async def _resolve_external_ip(host: str) -> str | None:
    """Resolve *host* and return its IP ONLY if it is a PUBLIC address.

    Anti-SSRF (red-team finding 2026-06-19): the proxy runs in the host netns and
    can reach the container's loopback, the host gateway, RFC1918 and cloud metadata
    (169.254.169.254). Without this check, a domain that resolves INWARD — even an
    owner-granted one, or via DNS rebinding — would let the jailed browser/terminal
    pivot to internal services THROUGH the proxy. We resolve here and connect to the
    returned IP (not the hostname) so the connect can't rebind to a different address.
    Returns None (→ refuse) if any resolved address is private/loopback/link-local/
    reserved/multicast/unspecified.
    """
    import ipaddress  # noqa: PLC0415

    try:
        loop = asyncio.get_event_loop()
        infos = await loop.getaddrinfo(host, None)
    except (OSError, UnicodeError):
        return None
    if not infos:
        return None
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return None
        if (
            ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified
        ):
            return None
    return infos[0][4][0]


class ProxyConnectionHandler:
    """Gestiona una conexión de cliente en el proxy de reenvío."""

    def __init__(
        self,
        *,
        policy_engine: EgressPolicyEngine,
        audit_sink: EgressAuditSink,
    ) -> None:
        self._policy = policy_engine
        self._audit = audit_sink

    async def handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Entry point por conexión — detecta CONNECT vs HTTP plano."""
        peer = writer.get_extra_info("peername", ("unknown", 0))
        client_ip = peer[0] if isinstance(peer, tuple) else str(peer)

        try:
            await self._dispatch(reader=reader, writer=writer, client_ip=client_ip)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "hermes.egress_proxy.connection_error",
                extra={"client_ip": client_ip, "error": str(exc)},
            )
        finally:
            _safe_close(writer)

    async def _dispatch(
        self,
        *,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        client_ip: str,
    ) -> None:
        first_line_bytes = await asyncio.wait_for(
            reader.readline(), timeout=_CONNECT_TIMEOUT_S
        )
        if not first_line_bytes:
            return

        first_line = first_line_bytes.decode("ascii", errors="replace").rstrip("\r\n")
        method = first_line.split()[0].upper() if first_line.split() else ""

        if method == "CONNECT":
            await self._handle_connect(
                first_line=first_line,
                reader=reader,
                writer=writer,
                client_ip=client_ip,
            )
        else:
            await self._handle_plain_http(
                first_line=first_line,
                reader=reader,
                writer=writer,
                client_ip=client_ip,
            )

    # ------------------------------------------------------------------
    # CONNECT handler (TLS tunnel) — Fix-4 SNI enforcement
    # ------------------------------------------------------------------

    async def _handle_connect(
        self,
        *,
        first_line: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        client_ip: str,
    ) -> None:
        try:
            target = parse_connect_line(first_line)
        except HostParseError as exc:
            logger.warning(
                "hermes.egress_proxy.connect_parse_error",
                extra={"error": str(exc)},
            )
            writer.write(_CONNECT_FORBIDDEN)
            await writer.drain()
            return

        # Consume the CONNECT headers before any policy check.
        await _drain_headers(reader)

        # Resolve via policy_for so a PINNED client (the MCP source IP, default-deny +
        # registries-only, immune to control-socket pushes) is read as its own policy,
        # NOT the browser's global mode. Reading _sessions directly would have missed
        # the pin and let an open-logged global apply to the MCP (C1 PASS-2 bypass #1).
        policy = self._policy.policy_for(client_ip)
        is_deny_mode = policy.mode == EgressMode.DEFAULT_DENY

        # Fix-4: in DEFAULT_DENY, reject CONNECT to bare IP literals immediately.
        # There is no hostname to check SNI against; exfil via IP is trivially evasive.
        if is_deny_mode and _is_ip_literal(target.host):
            self._audit.record(
                self._policy.evaluate(domain=target.host, session_id=client_ip)
            )
            writer.write(_CONNECT_FORBIDDEN)
            await writer.drain()
            logger.warning(
                "hermes.egress_proxy.deny_ip_literal",
                extra={"host": target.host, "session_id": client_ip},
            )
            return

        if is_deny_mode:
            # Fix-4: read ClientHello SNI BEFORE opening the upstream connection.
            # The SNI is the authoritative identifier — the CONNECT host can be a CDN
            # alias or deliberately mismatched to evade the filter.
            await self._handle_connect_sni_enforced(
                target=target,
                reader=reader,
                writer=writer,
                client_ip=client_ip,
            )
        else:
            # OPEN_LOGGED: policy decision on CONNECT host (legacy behavior); log SNI.
            await self._handle_connect_open_logged(
                target=target,
                reader=reader,
                writer=writer,
                client_ip=client_ip,
            )

    async def _handle_connect_sni_enforced(
        self,
        *,
        target,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        client_ip: str,
    ) -> None:
        """DEFAULT_DENY path: clean 403 if the CONNECT host is denied; otherwise
        establish the tunnel, then VERIFY the SNI (anti-evasion) before any upstream.

        Order matters: an HTTP client sends its TLS ClientHello only AFTER it
        receives ``200 Connection established``. Peeking the ClientHello BEFORE the
        200 deadlocks (client waits for 200, proxy waits for ClientHello) → every
        HTTPS CONNECT timed out and was denied. We therefore (1) deny disallowed
        CONNECT hosts with a clean 403 up front, then (2) send 200, peek the
        ClientHello, and confirm the SNI is also whitelisted before opening upstream.
        """
        # (1) Pre-check the CONNECT host. Denied here → clean 403 (no tunnel yet).
        host_decision = self._policy.evaluate(domain=target.host, session_id=client_ip)
        if not host_decision.allowed:
            self._audit.record(host_decision)
            writer.write(_CONNECT_FORBIDDEN)
            await writer.drain()
            logger.info(
                "hermes.egress_proxy.deny",
                extra={"domain": target.host, "session_id": client_ip, "source": "connect-host"},
            )
            return

        # (2) Host allowed → establish the tunnel so the client emits its ClientHello.
        writer.write(_CONNECT_ESTABLISHED)
        await writer.drain()

        # Peek the ClientHello to VERIFY the SNI (the real destination — a CDN alias
        # or spoofed CONNECT host may differ). Past the 200 now, so a denial closes
        # the connection (the client's TLS handshake fails) — no upstream is opened.
        try:
            peek_bytes = await asyncio.wait_for(
                reader.read(_SNI_PEEK_SIZE), timeout=_SNI_PEEK_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            logger.warning(
                "hermes.egress_proxy.sni_timeout: no ClientHello within %ss — deny",
                _SNI_PEEK_TIMEOUT_S,
                extra={"host": target.host, "session_id": client_ip},
            )
            _safe_close(writer)
            return

        if not peek_bytes:
            logger.warning(
                "hermes.egress_proxy.sni_empty: empty ClientHello — deny",
                extra={"host": target.host, "session_id": client_ip},
            )
            _safe_close(writer)
            return

        try:
            hello = parse_sni(peek_bytes)
        except SNIParseError:
            # No SNI / ECH / GREASE — deny in default-deny (Fix-4).
            logger.warning(
                "hermes.egress_proxy.sni_missing: no SNI in ClientHello — deny",
                extra={"host": target.host, "session_id": client_ip},
            )
            _safe_close(writer)
            return

        sni_host = hello.sni
        decision = self._policy.evaluate(domain=sni_host, session_id=client_ip)
        self._audit.record(decision)

        if not decision.allowed:
            logger.info(
                "hermes.egress_proxy.deny",
                extra={"domain": sni_host, "session_id": client_ip, "source": "sni"},
            )
            _safe_close(writer)
            return

        # Open upstream to the VERIFIED SNI host (not the CONNECT host, which may differ).
        # Anti-pivot: refuse if the host resolves to an internal IP (SSRF via the proxy).
        upstream_host = sni_host
        safe_ip = await _resolve_external_ip(upstream_host)
        if safe_ip is None:
            logger.warning(
                "hermes.egress_proxy.upstream_internal_blocked",
                extra={"domain": upstream_host, "session_id": client_ip, "source": "sni"},
            )
            _safe_close(writer)
            return
        try:
            remote_reader, remote_writer = await asyncio.wait_for(
                asyncio.open_connection(safe_ip, target.port),
                timeout=_CONNECT_TIMEOUT_S,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            logger.warning(
                "hermes.egress_proxy.upstream_connect_failed",
                extra={
                    "domain": upstream_host,
                    "port": target.port,
                    "error": str(exc),
                },
            )
            _safe_close(writer)  # past the 200 — can't send 502; close the tunnel
            return

        logger.info(
            "hermes.egress_proxy.allow",
            extra={"domain": upstream_host, "session_id": client_ip, "source": "sni"},
        )

        # Re-inject the peeked bytes so the upstream receives the full ClientHello.
        remote_writer.write(peek_bytes)
        await remote_writer.drain()

        await _pipe_bidirectional(
            client_reader=reader,
            client_writer=writer,
            remote_reader=remote_reader,
            remote_writer=remote_writer,
        )

    async def _handle_connect_open_logged(
        self,
        *,
        target,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        client_ip: str,
    ) -> None:
        """OPEN_LOGGED path: policy on CONNECT host, log SNI for audit only."""
        decision = self._policy.evaluate(domain=target.host, session_id=client_ip)
        self._audit.record(decision)

        if not decision.allowed:
            writer.write(_CONNECT_FORBIDDEN)
            await writer.drain()
            logger.info(
                "hermes.egress_proxy.deny",
                extra={"domain": target.host, "session_id": client_ip},
            )
            return

        safe_ip = await _resolve_external_ip(target.host)
        if safe_ip is None:
            logger.warning(
                "hermes.egress_proxy.upstream_internal_blocked",
                extra={"domain": target.host, "session_id": client_ip, "source": "open-logged"},
            )
            writer.write(_CONNECT_FORBIDDEN)
            await writer.drain()
            return
        try:
            remote_reader, remote_writer = await asyncio.wait_for(
                asyncio.open_connection(safe_ip, target.port),
                timeout=_CONNECT_TIMEOUT_S,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            logger.warning(
                "hermes.egress_proxy.upstream_connect_failed",
                extra={"domain": target.host, "port": target.port, "error": str(exc)},
            )
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await writer.drain()
            return

        logger.info(
            "hermes.egress_proxy.dbg_upstream_connected",
            extra={"domain": target.host, "peer": str(remote_writer.get_extra_info("peername"))},
        )

        writer.write(_CONNECT_ESTABLISHED)
        await writer.drain()

        # SNI audit only in open-logged mode (no blocking). MUST forward the
        # consumed ClientHello bytes upstream or the TLS handshake breaks
        # (ERR_SSL_PROTOCOL_ERROR).
        first_bytes = await _maybe_log_sni(reader, target.host)
        if first_bytes:
            remote_writer.write(first_bytes)
            await remote_writer.drain()

        logger.info(
            "hermes.egress_proxy.allow",
            extra={"domain": target.host, "session_id": client_ip, "ch_bytes": len(first_bytes)},
        )

        await _pipe_bidirectional(
            client_reader=reader,
            client_writer=writer,
            remote_reader=remote_reader,
            remote_writer=remote_writer,
            dbg_domain=target.host,
        )

    # ------------------------------------------------------------------
    # HTTP plain handler — Fix-7: disabled in DEFAULT_DENY
    # ------------------------------------------------------------------

    async def _handle_plain_http(
        self,
        *,
        first_line: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        client_ip: str,
    ) -> None:
        """HTTP plain (non-CONNECT).

        Fix-7: rejected in DEFAULT_DENY — the Host header is client-controlled
        and cannot be authenticated against SNI. Only OPEN_LOGGED permits plain
        HTTP (needed for CRL/OCSP in discovery mode); even then, Host is
        validated on every request. The PINNED MCP client is default-deny, so this
        also blocks plain HTTP for MCP traffic (registry downloads are HTTPS CONNECT).
        """
        policy = self._policy.policy_for(client_ip)
        if policy.mode == EgressMode.DEFAULT_DENY:
            logger.warning(
                "hermes.egress_proxy.plain_http_rejected_default_deny",
                extra={"session_id": client_ip, "first_line": first_line[:80]},
            )
            writer.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            await writer.drain()
            return

        # OPEN_LOGGED: read and validate Host header.
        await self._forward_plain_http(
            first_line=first_line,
            reader=reader,
            writer=writer,
            client_ip=client_ip,
        )

    async def _forward_plain_http(
        self,
        *,
        first_line: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        client_ip: str,
    ) -> None:
        """Forward plain HTTP after Host validation (OPEN_LOGGED only)."""
        from hermes.egress_proxy.domain.host_parser import (  # noqa: PLC0415
            HostParseError,
            parse_host_header,
        )

        host_header = await _read_host_header(reader)
        if host_header is None:
            writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            await writer.drain()
            return

        try:
            plain_host = parse_host_header(host_header)
        except HostParseError as exc:
            logger.warning(
                "hermes.egress_proxy.host_parse_error",
                extra={"error": str(exc)},
            )
            writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            await writer.drain()
            return

        decision = self._policy.evaluate(domain=plain_host.host, session_id=client_ip)
        self._audit.record(decision)

        if not decision.allowed:
            writer.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            await writer.drain()
            logger.info(
                "hermes.egress_proxy.deny",
                extra={"domain": plain_host.host, "session_id": client_ip},
            )
            return

        safe_ip = await _resolve_external_ip(plain_host.host)
        if safe_ip is None:
            logger.warning(
                "hermes.egress_proxy.upstream_internal_blocked",
                extra={"domain": plain_host.host, "session_id": client_ip, "source": "plain-http"},
            )
            writer.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            await writer.drain()
            return
        try:
            remote_reader, remote_writer = await asyncio.wait_for(
                asyncio.open_connection(safe_ip, plain_host.port),
                timeout=_CONNECT_TIMEOUT_S,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            logger.warning(
                "hermes.egress_proxy.upstream_connect_failed",
                extra={"domain": plain_host.host, "error": str(exc)},
            )
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await writer.drain()
            return

        remote_writer.write(first_line.encode("ascii") + b"\r\n")
        logger.info(
            "hermes.egress_proxy.allow",
            extra={"domain": plain_host.host, "session_id": client_ip},
        )
        await _pipe_bidirectional(
            client_reader=reader,
            client_writer=writer,
            remote_reader=remote_reader,
            remote_writer=remote_writer,
        )


# ---------------------------------------------------------------------------
# Helpers de I/O
# ---------------------------------------------------------------------------


async def _drain_headers(reader: asyncio.StreamReader) -> None:
    """Consume líneas de headers HTTP hasta la línea vacía."""
    while True:
        line = await asyncio.wait_for(reader.readline(), timeout=_CONNECT_TIMEOUT_S)
        if not line or line in (b"\r\n", b"\n"):
            break


async def _read_host_header(reader: asyncio.StreamReader) -> str | None:
    """Lee headers hasta encontrar ``Host:`` o la línea vacía."""
    read = 0
    while read < _HEADER_MAX_BYTES:
        line_bytes = await asyncio.wait_for(
            reader.readline(), timeout=_CONNECT_TIMEOUT_S
        )
        read += len(line_bytes)
        if not line_bytes or line_bytes in (b"\r\n", b"\n"):
            break
        line = line_bytes.decode("ascii", errors="replace")
        if line.lower().startswith("host:"):
            return line[5:].strip()
    return None


async def _maybe_log_sni(
    reader: asyncio.StreamReader, declared_host: str
) -> bytes:
    """Lee el ClientHello para audit (OPEN_LOGGED only, best-effort) y lo DEVUELVE.

    CRÍTICO: estos bytes son el inicio del handshake TLS del cliente. Hay que
    devolverlos para que el caller los reenvíe al upstream — si se consumieran y
    descartaran, el servidor remoto nunca recibiría el ClientHello y el TLS
    fallaría con ERR_SSL_PROTOCOL_ERROR. No bloquea: si no hay datos en 0.1s,
    devuelve b"" (el pipe normal moverá el handshake).
    """
    try:
        peek_bytes = await asyncio.wait_for(reader.read(512), timeout=0.1)
    except asyncio.TimeoutError:
        return b""
    if peek_bytes:
        try:
            hello = parse_sni(peek_bytes)
            if hello.sni != declared_host:
                logger.debug(
                    "hermes.egress_proxy.sni_mismatch",
                    extra={"declared": declared_host, "sni": hello.sni},
                )
        except SNIParseError:
            pass
    return peek_bytes


async def _pipe_half(
    src: asyncio.StreamReader,
    dst: asyncio.StreamWriter,
    dbg_label: str = "",
) -> None:
    """Copia bytes de src → dst hasta EOF."""
    total = 0
    reason = "eof"
    try:
        while True:
            data = await asyncio.wait_for(src.read(_BUFFER_SIZE), timeout=_TUNNEL_TIMEOUT_S)
            if not data:
                break
            total += len(data)
            dst.write(data)
            await dst.drain()
    except asyncio.TimeoutError:
        reason = "timeout"
    except (ConnectionResetError, BrokenPipeError) as exc:
        reason = f"reset:{type(exc).__name__}"
    finally:
        if dbg_label:
            logger.info(
                "hermes.egress_proxy.dbg_pipe_end",
                extra={"dir": dbg_label, "bytes": total, "reason": reason},
            )
        _safe_close(dst)


async def _pipe_bidirectional(
    *,
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    remote_reader: asyncio.StreamReader,
    remote_writer: asyncio.StreamWriter,
    dbg_domain: str = "",
) -> None:
    """Pipe bidireccional hasta que cualquier lado cierre la conexión."""
    await asyncio.gather(
        _pipe_half(client_reader, remote_writer, f"c2r:{dbg_domain}" if dbg_domain else ""),
        _pipe_half(remote_reader, client_writer, f"r2c:{dbg_domain}" if dbg_domain else ""),
        return_exceptions=True,
    )


def _safe_close(writer: asyncio.StreamWriter) -> None:
    try:
        if not writer.is_closing():
            writer.close()
    except Exception:  # noqa: BLE001
        pass
