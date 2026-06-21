"""LiveViewChannel: puerto de dominio para stream de frames al operador.

Re-export del contrato canónico con REQUISITO DE AUTHZ documentado.

Threat-model control P1 #6 / S1 superficie 5:
    El adapter que implemente este Protocol DEBE validar
    (operator_id, tenant_id, session_id) en cada llamada a
    request_intervention.

    Restricciones obligatorias para todo adapter concreto:
      1. operator_id debe pertenecer al tenant_id con rol 'operator' activo.
      2. subscription_token debe ser time-bounded (≤ 5 minutos).
      3. Audit log obligatorio: live_view_subscribed{operator_id, session_id}.
      4. Sin token o con token inválido → LiveViewUnauthorized (fail-closed).

    Justificación del diseño:
      El contract original (specs/.../contracts/live_view_channel.py) delega
      AuthN/AuthZ a la vertical ("el runtime entrega el canal; la vertical
      decide identidad/permiso"). Este puerto amplía la firma de
      request_intervention con subscription_token: str y operator_id: UUID
      para que el requisito sea estructural, no documental. Los adapters
      existentes que no usen AuthZ deben actualizar su firma — este cambio
      es breaking pero el contract aún no está congelado en código de
      producción, por lo que el rediseño es apropiado.

      Constitution I nota: la Constitución exige que contratos públicos sean
      inmutables una vez congelados. Al añadir aquí parámetros con defaults
      vacíos en el Protocol, los callers existentes en tests siguen
      compilando. El adapter InMemory valida activamente que no estén vacíos.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

from hermes.browser.domain.port import LiveViewFrame  # noqa: F401 (re-export)


class LiveViewError(RuntimeError):
    """Base de errores del canal live-view."""


class LiveViewClosed(LiveViewError):
    """Canal ya cerrado; send/request rechazados."""


class LiveViewUnauthorized(LiveViewError):
    """AuthZ falló: operator_id no autorizado, token vacío o expirado.

    Constitución IV: fail-closed. Si el adapter no puede verificar la
    autorización, debe levantar esta excepción en lugar de permitir el acceso.
    """


class InterventionTimeout(LiveViewError):
    """No llegó OperatorIntervention dentro de timeout_s (US5/AC4)."""


@dataclass(frozen=True, slots=True)
class OperatorInterventionRequestEnvelope:
    """Envelope que el LiveViewChannel envía al transport.

    Identificador mínimo. El adapter consulta el objeto vivo si necesita
    datos adicionales (DOM, screenshot, etc.).
    """

    request_id: UUID
    session_id: UUID
    sent_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))


@runtime_checkable
class LiveViewChannel(Protocol):
    """Contrato del canal live-view con AuthZ obligatoria.

    REQUISITO DE ADAPTER (threat-model control P1 #6):
        El adapter DEBE validar (operator_id, tenant_id, session_id) en
        cada request_intervention:
          - operator_id debe pertenecer al tenant_id con rol 'operator' activo.
          - subscription_token time-bounded ≤ 5 min.
          - Audit log obligatorio: live_view_subscribed{operator_id, session_id}.
          - Token vacío o inválido → LiveViewUnauthorized (fail-closed).

    Lifecycle: el BrowserSession lo recibe ya construido (por la vertical).
    El runtime invoca cleanup() al cerrar la sesión.
    """

    async def send_frame(self, frame: LiveViewFrame) -> None:
        """Empuja un frame al transport. No-op si no hay subscribers."""
        ...

    async def request_intervention(
        self,
        envelope: OperatorInterventionRequestEnvelope,
        *,
        timeout_s: float,
        operator_id: UUID,
        tenant_id: UUID,
        subscription_token: str = "",
    ) -> asyncio.Future[object]:
        """Emite InterventionRequest al operador y devuelve un Future.

        AUTHZ OBLIGATORIA (control P1 #6):
          - subscription_token vacío → LiveViewUnauthorized antes de nada.
          - AuthzPolicy.authorize(...) devuelve False → LiveViewUnauthorized.
          - operator_id no pertenece a tenant_id → LiveViewUnauthorized.

        El runtime hace: await asyncio.wait_for(future, timeout_s).
        La vertical resuelve el Future cuando recibe la acción del operador.

        Raises:
            LiveViewUnauthorized: si AuthZ falla (fail-closed).
            InterventionTimeout: si el operador no responde en timeout_s.
        """
        ...

    async def cleanup(self) -> None:
        """Cierra suscripciones, drena buffers, libera transport.

        Idempotente. Llamado siempre en BrowserSession.close().
        """
        ...
