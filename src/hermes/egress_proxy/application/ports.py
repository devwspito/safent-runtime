"""Puertos de la capa de aplicación del proxy de egress.

Define los contratos que la infraestructura debe satisfacer.
El dominio no depende de estos puertos — solo la capa de aplicación.
"""

from __future__ import annotations

from typing import Protocol

from hermes.egress_proxy.domain.policy import EgressDecision


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
