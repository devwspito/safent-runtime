"""InMemoryLiveViewChannel: test double del LiveViewChannel con AuthZ inyectable.

Implementa el Protocol LiveViewChannel con un AuthzPolicy configurable.
Cada llamada a request_intervention:
  1. Rechaza subscription_token vacío → LiveViewUnauthorized (fail-closed).
  2. Llama AuthzPolicy.authorize(...); si devuelve False o lanza → LiveViewUnauthorized.
  3. Si OK, construye y registra un Future pendiente accesible via pending_futures.

Modos de resolución:
  - inject_response(response): pre-inyecta respuesta; el próximo Future la usa.
  - resolve_intervention(request_id, response): resuelve un Future pendiente
    por request_id (para tests de timeout y control de timing).

T206 (AuthZ) + T704 (HITL completo) — US5/Phase 7.
Threat-model control P1 #6 / S1 superficie 5.
Constitution IV: fail-closed por defecto — sin token → unauthorized.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from hermes.browser.domain.ports.live_view_channel import (
    LiveViewClosed,
    LiveViewUnauthorized,
    OperatorInterventionRequestEnvelope,
)


@runtime_checkable
class AuthzPolicy(Protocol):
    """Política de autorización inyectable en InMemoryLiveViewChannel.

    authorize() devuelve True si el operador está autorizado, False si no.
    Puede lanzar cualquier excepción — el channel la captura y convierte
    en LiveViewUnauthorized (fail-closed).
    """

    async def authorize(
        self,
        *,
        operator_id: UUID,
        tenant_id: UUID,
        session_id: UUID,
        subscription_token: str,
    ) -> bool: ...


class AlwaysAuthorizedPolicy:
    """AuthzPolicy de conveniencia para tests que no prueban AuthZ."""

    async def authorize(
        self,
        *,
        operator_id: UUID,  # noqa: ARG002
        tenant_id: UUID,  # noqa: ARG002
        session_id: UUID,  # noqa: ARG002
        subscription_token: str,  # noqa: ARG002
    ) -> bool:
        return True


class AlwaysUnauthorizedPolicy:
    """AuthzPolicy que siempre rechaza — para tests de AuthZ negativa."""

    async def authorize(
        self,
        *,
        operator_id: UUID,  # noqa: ARG002
        tenant_id: UUID,  # noqa: ARG002
        session_id: UUID,  # noqa: ARG002
        subscription_token: str,  # noqa: ARG002
    ) -> bool:
        return False


@dataclass
class TenantScopedPolicy:
    """Autoriza sólo si operator_id está registrado bajo el tenant_id correcto."""

    # operator_id → set de tenant_ids a los que pertenece
    operator_tenants: dict[UUID, set[UUID]] = field(default_factory=dict)

    async def authorize(
        self,
        *,
        operator_id: UUID,
        tenant_id: UUID,
        session_id: UUID,  # noqa: ARG002
        subscription_token: str,  # noqa: ARG002
    ) -> bool:
        allowed_tenants = self.operator_tenants.get(operator_id, set())
        return tenant_id in allowed_tenants


# ---------------------------------------------------------------------------
# InMemoryLiveViewChannel
# ---------------------------------------------------------------------------


@dataclass
class OperatorInterventionRequest:
    """Resultado simulado de una intervención de operador."""

    operator_id: UUID
    session_id: UUID
    action: str = "approved"
    payload: dict[str, Any] = field(default_factory=dict)
    # Optional proposals for T705 selector + rule materialisation tests.
    selector_proposal: dict[str, Any] | None = None
    rule_proposal: dict[str, Any] | None = None


class InMemoryLiveViewChannel:
    """LiveViewChannel in-memory para tests.

    La respuesta a request_intervention se inyecta via inject_response() o
    resolve_intervention(). Si no se inyecta respuesta, el Future resuelve
    con un OperatorInterventionRequest por defecto.

    resolve_intervention(request_id, response) permite tests que necesitan
    controlar el timing (e.g. para simular timeout dejando el Future sin
    resolver).
    """

    def __init__(
        self,
        *,
        authz_policy: AuthzPolicy | None = None,
        default_response_factory: Callable[
            [UUID, UUID], OperatorInterventionRequest
        ]
        | None = None,
        # If True, request_intervention keeps Future pending until
        # resolve_intervention() is called. Used for timeout tests.
        hold_intervention: bool = False,
    ) -> None:
        self._policy: AuthzPolicy = authz_policy or AlwaysAuthorizedPolicy()
        self._response_factory = default_response_factory
        self._closed = False
        self._hold = hold_intervention
        # Registro para assertions en tests.
        self.frames_sent: list[Any] = []
        InterventionRecord = tuple[OperatorInterventionRequestEnvelope, UUID, UUID]
        self.intervention_requests: list[InterventionRecord] = []
        # Respuesta inyectada explícitamente para el próximo request.
        self._next_response: OperatorInterventionRequest | None = None
        # Pending futures keyed by request_id (for resolve_intervention).
        self._pending: dict[UUID, asyncio.Future[OperatorInterventionRequest]] = {}

    def inject_response(self, response: OperatorInterventionRequest) -> None:
        """Predefine la respuesta para el próximo request_intervention."""
        self._next_response = response

    def resolve_intervention(
        self, request_id: UUID, response: OperatorInterventionRequest
    ) -> None:
        """Resuelve un Future pendiente por request_id.

        Permite tests que controlan el timing de la respuesta del operador.
        Si el Future no existe (ya resolvió o no se registró), no-op.
        """
        future = self._pending.pop(request_id, None)
        if future is not None and not future.done():
            future.set_result(response)

    async def send_frame(self, frame: Any) -> None:
        if self._closed:
            raise LiveViewClosed("canal cerrado")
        self.frames_sent.append(frame)

    async def request_intervention(
        self,
        envelope: OperatorInterventionRequestEnvelope,
        *,
        timeout_s: float,  # noqa: ARG002
        operator_id: UUID,
        tenant_id: UUID,
        subscription_token: str = "",
    ) -> asyncio.Future[OperatorInterventionRequest]:
        """Valida AuthZ y retorna un Future con la respuesta del operador.

        Raises:
            LiveViewUnauthorized: si el token está vacío o la policy rechaza.
        """
        if self._closed:
            raise LiveViewClosed("canal cerrado")

        # Control P1 #6: token vacío → fail-closed, sin llamar a la policy.
        if not subscription_token:
            raise LiveViewUnauthorized(
                "subscription_token vacío — acceso denegado (fail-closed)"
            )

        # Llamar a la policy — si falla o devuelve False → unauthorized.
        try:
            authorized = await self._policy.authorize(
                operator_id=operator_id,
                tenant_id=tenant_id,
                session_id=envelope.session_id,
                subscription_token=subscription_token,
            )
        except Exception as exc:
            raise LiveViewUnauthorized(
                f"AuthzPolicy.authorize() lanzó excepción: {exc}"
            ) from exc

        if not authorized:
            raise LiveViewUnauthorized(
                f"operator_id={operator_id} no autorizado para "
                f"tenant_id={tenant_id} session_id={envelope.session_id}"
            )

        self.intervention_requests.append((envelope, operator_id, tenant_id))

        loop = asyncio.get_event_loop()
        future: asyncio.Future[OperatorInterventionRequest] = loop.create_future()

        if self._hold:
            # Keep future pending — test will call resolve_intervention().
            self._pending[envelope.request_id] = future
            return future

        # Build response immediately.
        response: OperatorInterventionRequest
        if self._next_response is not None:
            response = self._next_response
            self._next_response = None
        elif self._response_factory is not None:
            response = self._response_factory(operator_id, envelope.session_id)
        else:
            response = OperatorInterventionRequest(
                operator_id=operator_id,
                session_id=envelope.session_id,
            )

        future.set_result(response)
        return future

    async def cleanup(self) -> None:
        self._closed = True
        # Cancel any pending futures to avoid test hangs.
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()
