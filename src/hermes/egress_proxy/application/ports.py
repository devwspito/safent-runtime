"""Puertos de la capa de aplicación del proxy de egress.

Define los contratos que la infraestructura debe satisfacer.
El dominio no depende de estos puertos — solo la capa de aplicación.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

from hermes.egress_proxy.domain.policy import EgressDecision


class UpstreamConnectError(Exception):
    """No se pudo abrir la conexión upstream (host verificado, dial fallido)."""


class UpstreamInternalAddressError(UpstreamConnectError):
    """El host resuelve a una dirección interna/privada — bloqueado (anti-SSRF)."""


class UpstreamConnector(Protocol):
    """Puerto de dial — abre el stream upstream para un host:port verificado.

    Implementaciones (infra):
      - ``DirectUpstreamConnector`` (proxy_handler.py): resolución DNS +
        anti-SSRF + Happy Eyeballs — el camino de hoy, sin cambios de
        comportamiento.
      - ``TailnetUpstreamConnector`` (tailnet_connector.py): reenvía la
        conexión al proxy HTTP-CONNECT loopback de tailscaled.
      - ``UpstreamRouter`` (tailnet_connector.py): selecciona entre las
        dos anteriores según el sufijo MagicDNS — es el objeto que se
        inyecta en ``ProxyConnectionHandler`` (también implementa este
        puerto, así el handler no distingue "router" de "connector").

    Lanza ``UpstreamConnectError`` (o ``UpstreamInternalAddressError`` para
    el caso anti-SSRF) si no puede abrir la conexión — nunca devuelve None.
    """

    async def connect(
        self, *, host: str, port: int, timeout: float
    ) -> "tuple[asyncio.StreamReader, asyncio.StreamWriter]":
        ...


class EgressAuditSink(Protocol):
    """Puerto de auditoría — recibe cada decisión de allow/deny."""

    def record(self, decision: EgressDecision) -> None:
        """Registra una decisión de egress.

        Implementaciones:
          - ``StructlogAuditSink`` (infra): emite a structlog.
          - ``HashChainAuditSink`` (infra): cableado al AuditHashChainSigner.
            TODO: cablear al hash-chain real (ver TODO en HashChainAuditSink).
          - ``InMemoryAuditSink`` (tests): acumula en lista para assertions.

        El llamador (proxy handler) NO hace await — el sink es síncrono y
        no bloquea el ciclo asyncio del servidor.  Implementaciones lentas
        (DB) deben encolar y persistir en background.
        """
        ...
