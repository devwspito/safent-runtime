"""Conector de egress hacia la tailnet gobernada del dueño (spec 022).

Cuando el host verificado (SNI o CONNECT host, ya evaluado por
``EgressPolicyEngine``) coincide con el sufijo MagicDNS de la tailnet,
la conexión se reenvía al proxy HTTP-CONNECT loopback que expone
tailscaled en modo userspace (``127.0.0.1:1055`` — hermes-tailscaled.service).
Cualquier otro host sigue el camino directo de siempre.

No hay allowlist paralela: que la conexión llegue aquí ya implica que la
política de egress concedió el host (DENY mode, grant normal vía
``egress_api``) — este módulo solo decide CÓMO dialear, nunca SI está
permitido.

Fuente de verdad del sufijo: ``status.json``, escrito por el lane de ops
(contrato en specs/022-tailnet-connectivity/contracts.md). Se lee de forma
perezosa y se cachea por mtime — un cambio de sufijo se recoge en la
siguiente conexión posterior a la escritura, no hay watch de filesystem.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
from pathlib import Path

from hermes.egress_proxy.application.ports import UpstreamConnectError, UpstreamConnector

logger = logging.getLogger("hermes.egress_proxy.tailnet_connector")

_DEFAULT_STATUS_PATH = Path("/run/hermes/tailscale/status.json")
_DEFAULT_PROXY_HOST = "127.0.0.1"
_DEFAULT_PROXY_PORT = 1055
_HEADER_MAX_BYTES = 8192
_CONNECT_OK_MARKER = b" 200 "


# ---------------------------------------------------------------------------
# MagicDNS suffix source (cached, mtime-invalidated)
# ---------------------------------------------------------------------------


class MagicDnsSuffixSource:
    """Lee y cachea el sufijo MagicDNS desde ``status.json``.

    Fail-closed: cualquier error de lectura/parseo (fichero ausente —
    tailnet no configurada, JSON corrupto, permiso denegado, campo con
    forma inválida) se trata como "sin sufijo" (``None``) — ningún host se
    enruta a la tailnet hasta que el fichero sea legible y válido.
    """

    def __init__(self, status_path: Path | None = None) -> None:
        self._path = status_path or _DEFAULT_STATUS_PATH
        self._mtime: float | None = None
        self._suffix: str | None = None

    def current_suffix(self) -> str | None:
        """Devuelve el sufijo cacheado, releyendo el fichero si cambió de mtime."""
        try:
            mtime = self._path.stat().st_mtime
        except OSError:
            self._mtime = None
            self._suffix = None
            return None

        if mtime != self._mtime:
            self._suffix = self._read_suffix()
            self._mtime = mtime
        return self._suffix

    def _read_suffix(self) -> str | None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        suffix = data.get("magicdns_suffix") if isinstance(data, dict) else None
        if not isinstance(suffix, str) or not suffix.strip():
            return None
        return suffix.strip().lower().rstrip(".")


# ---------------------------------------------------------------------------
# Host-suffix matching (FQDN-only, v1 scope)
# ---------------------------------------------------------------------------


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def host_matches_suffix(host: str, suffix: str) -> bool:
    """True si ``host`` es el sufijo MagicDNS o un subdominio suyo.

    v1 = destinos FQDN-only (plan.md §Scope) — un literal IP nunca coincide,
    aunque por accidente termine igual que el sufijo en texto.
    """
    if not suffix or _is_ip_literal(host):
        return False
    normalized_host = host.lower().rstrip(".")
    normalized_suffix = suffix.lower().rstrip(".")
    return (
        normalized_host == normalized_suffix
        or normalized_host.endswith("." + normalized_suffix)
    )


# ---------------------------------------------------------------------------
# Tailnet upstream connector — dials via the loopback CONNECT proxy
# ---------------------------------------------------------------------------


class TailnetUpstreamConnector:
    """Dialea a través del proxy HTTP-CONNECT loopback de tailscaled.

    Solo se invoca para hosts que ya superaron ``host_matches_suffix`` en
    ``UpstreamRouter`` — no revalida el sufijo. tailscaled resuelve el host
    vía MagicDNS y dialea dentro de la tailnet; este connector no hace su
    propia resolución DNS (a diferencia de ``DirectUpstreamConnector`` — el
    espacio de nombres MagicDNS solo lo entiende tailscaled).
    """

    def __init__(
        self,
        *,
        proxy_host: str = _DEFAULT_PROXY_HOST,
        proxy_port: int = _DEFAULT_PROXY_PORT,
    ) -> None:
        self._proxy_host = proxy_host
        self._proxy_port = proxy_port

    async def connect(
        self, *, host: str, port: int, timeout: float
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        reader, writer = await self._dial_proxy(timeout)
        try:
            await self._send_connect(reader, writer, host=host, port=port, timeout=timeout)
        except UpstreamConnectError:
            _safe_close(writer)
            raise
        return reader, writer

    async def _dial_proxy(
        self, timeout: float
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        try:
            return await asyncio.wait_for(
                asyncio.open_connection(self._proxy_host, self._proxy_port),
                timeout=timeout,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise UpstreamConnectError(
                f"no se pudo conectar al proxy tailnet "
                f"{self._proxy_host}:{self._proxy_port}: {exc}"
            ) from exc

    async def _send_connect(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        host: str,
        port: int,
        timeout: float,
    ) -> None:
        request = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n"
        try:
            writer.write(request.encode("ascii"))
            await writer.drain()
            status_line = await asyncio.wait_for(reader.readline(), timeout=timeout)
            await _drain_headers(reader, timeout)
        except (OSError, asyncio.TimeoutError) as exc:
            raise UpstreamConnectError(
                f"fallo en el handshake CONNECT con el proxy tailnet: {exc}"
            ) from exc

        if not _is_connect_ok(status_line):
            raise UpstreamConnectError(
                f"el proxy tailnet rechazó CONNECT {host}:{port}: {status_line!r}"
            )


def _is_connect_ok(status_line: bytes) -> bool:
    return _CONNECT_OK_MARKER in status_line


async def _drain_headers(reader: asyncio.StreamReader, timeout: float) -> None:
    """Consume las cabeceras de la respuesta CONNECT hasta la línea vacía."""
    read = 0
    while read < _HEADER_MAX_BYTES:
        line = await asyncio.wait_for(reader.readline(), timeout=timeout)
        read += len(line)
        if not line or line in (b"\r\n", b"\n"):
            return


def _safe_close(writer: asyncio.StreamWriter) -> None:
    try:
        if not writer.is_closing():
            writer.close()
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Router — selects direct vs tailnet per host
# ---------------------------------------------------------------------------


class UpstreamRouter:
    """Selecciona el connector correcto según si ``host`` cae en la tailnet.

    Implementa el mismo puerto ``UpstreamConnector`` que sus dos delegados,
    así ``ProxyConnectionHandler`` no distingue "hay un router" de "hay un
    connector directo" — se construye una vez en ``__main__.py`` (a partir
    del fichero de sufijo) y se inyecta en el handler.
    """

    def __init__(
        self,
        *,
        suffix_source: MagicDnsSuffixSource,
        tailnet_connector: UpstreamConnector,
        default_connector: UpstreamConnector,
    ) -> None:
        self._suffix_source = suffix_source
        self._tailnet = tailnet_connector
        self._default = default_connector

    async def connect(
        self, *, host: str, port: int, timeout: float
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        suffix = self._suffix_source.current_suffix()
        if suffix is not None and host_matches_suffix(host, suffix):
            logger.info(
                "hermes.egress_proxy.tailnet_route",
                extra={"host": host, "magicdns_suffix": suffix},
            )
            return await self._tailnet.connect(host=host, port=port, timeout=timeout)
        return await self._default.connect(host=host, port=port, timeout=timeout)
