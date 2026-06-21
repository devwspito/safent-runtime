"""Implementaciones del puerto EgressAuditSink.

``StructlogAuditSink`` — producción sin hash-chain.
``HashChainAuditSink`` — cablea al AuditHashChainSigner via background queue.
``InMemoryAuditSink`` — solo tests.

Fix-6: HashChainAuditSink ya no es un stub. Encola decisiones en una deque
in-memory y las drena en background via asyncio. Si el signer no está
disponible (sin clave), el sink entra en estado DEGRADED VISIBLE (no silente):
loguea ERROR en cada record() y mantiene una bandera observable ``degraded``.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from hermes.egress_proxy.domain.policy import EgressDecision

logger = logging.getLogger("hermes.egress_proxy.audit")

# Maximum buffered decisions before oldest entries are dropped (back-pressure cap).
_QUEUE_CAP = 1024


class StructlogAuditSink:
    """Emite cada decisión de egress como entrada structlog.

    No contiene PII — solo dominio, decisión, session_id y modo.

    El nivel INFO se usa para allow en open-logged (alta frecuencia en
    discovery) y para deny (siempre relevante).  El nivel WARNING se
    reserva para denegaciones en default-deny (acción requerida).
    """

    def record(self, decision: EgressDecision) -> None:
        level = logging.WARNING if not decision.allowed else logging.INFO
        logger.log(
            level,
            "hermes.egress_proxy.decision",
            extra={
                "allowed": decision.allowed,
                "domain": decision.domain,
                "session_id": decision.session_id,
                "mode": decision.mode,
                "reason": decision.reason,
            },
        )


class HashChainAuditSink:
    """Sink que encadena decisiones de egress al AuditHashChainSigner.

    Fix-6: implementación real (ya no stub). Diseño:
      - record() es síncrono (contrato del puerto EgressAuditSink).
      - Las decisiones se encolan en una deque (cap _QUEUE_CAP).
      - drain_once() es async: firma+persiste entradas pendientes bajo el
        chain_lock del signer (serializado, tamper-evident).
      - Si signer/repo no están disponibles, el sink entra en modo DEGRADED:
        ``degraded=True``, cada record() loguea ERROR, ningún entry se pierde
        silenciosamente (caen al StructlogAuditSink de fallback con WARNING).
      - Iniciado desde __main__ con start_background_drain(loop) para que el
        drain ocurra en el mismo event loop del proxy.

    Dependencias (inyectadas para no acoplar este módulo al runtime):
      - signer: AuditHashChainSigner (o None si no hay clave).
      - audit_repo: objeto con ``async def append(entry)`` (o None).
      - tenant_id: UUID del tenant activo (puede ser None).
    """

    def __init__(
        self,
        *,
        signer: Any = None,
        audit_repo: Any = None,
        tenant_id: Any = None,
        drain_interval_s: float = 1.0,
    ) -> None:
        self._signer = signer
        self._repo = audit_repo
        self._tenant_id = tenant_id
        self._drain_interval_s = drain_interval_s
        self._queue: deque[EgressDecision] = deque(maxlen=_QUEUE_CAP)
        self._fallback = StructlogAuditSink()
        self._drain_task: asyncio.Task[None] | None = None

        # Visible degraded flag: True when signer/repo are unavailable.
        self.degraded: bool = signer is None or audit_repo is None
        if self.degraded:
            logger.error(
                "hermes.egress_proxy.audit_sink_degraded: "
                "HashChainAuditSink sin signer o repo — "
                "las decisiones de egress NO se encadenarán (tamper-evident desactivado). "
                "Inyecta AuditHashChainSigner + AuditRepository para activar."
            )

    def record(self, decision: EgressDecision) -> None:
        """Encola una decisión. Síncrono; no bloquea el event loop."""
        if self.degraded:
            logger.error(
                "hermes.egress_proxy.audit_sink_degraded_record: "
                "decisión de egress NO encadenada (sink degradado) — "
                "domain=%s allowed=%s",
                decision.domain,
                decision.allowed,
            )
            self._fallback.record(decision)
            return
        self._queue.append(decision)

    def start_background_drain(self, loop: asyncio.AbstractEventLoop) -> None:
        """Arranca la tarea de drain en el event loop del proxy.

        Llamar desde el __main__ del proxy DESPUÉS de asyncio.run() ha
        arrancado el loop y el signer está disponible.
        """
        if self.degraded:
            return
        self._drain_task = loop.create_task(
            self._drain_loop(), name="egress-audit-drain"
        )

    def stop_background_drain(self) -> None:
        if self._drain_task is not None:
            self._drain_task.cancel()

    async def _drain_loop(self) -> None:
        while True:
            await asyncio.sleep(self._drain_interval_s)
            await self._drain_once()

    async def _drain_once(self) -> None:
        """Firma y persiste todas las decisiones pendientes."""
        from hermes.agents_os.application.audit_hash_chain import AuditKind  # noqa: PLC0415

        while self._queue:
            decision = self._queue.popleft()
            audit_kind = (
                AuditKind.EGRESS_ALLOWED if decision.allowed else AuditKind.EGRESS_DENIED
            )
            try:
                await self._signer.append_and_persist(
                    audit_kind=audit_kind,
                    actor="egress-proxy",
                    description=decision.reason,
                    payload={
                        "domain": decision.domain,
                        "allowed": decision.allowed,
                        "session_id": decision.session_id,
                        "mode": str(decision.mode),
                    },
                    audit_repo=self._repo,
                    tenant_id=self._tenant_id,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "hermes.egress_proxy.audit_sink_sign_failed: %s — "
                    "decisión de egress no encadenada",
                    exc,
                    extra={"domain": decision.domain},
                )
                # Re-emit via fallback so the decision is at least logged.
                self._fallback.record(decision)


@dataclass
class InMemoryAuditSink:
    """Acumula decisiones en memoria — solo para tests."""

    decisions: list[EgressDecision] = field(default_factory=list)

    def record(self, decision: EgressDecision) -> None:
        self.decisions.append(decision)

    def allowed_domains(self) -> list[str]:
        return [d.domain for d in self.decisions if d.allowed]

    def denied_domains(self) -> list[str]:
        return [d.domain for d in self.decisions if not d.allowed]
