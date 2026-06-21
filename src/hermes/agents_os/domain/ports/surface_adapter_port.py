"""SurfaceAdapterPort — contrato agnóstico al dominio para capturar/replay.

FR-027..FR-029 (spec 003): cualquier superficie operable (terminal,
filesystem, api_call, desktop_app, system_settings, package_manager,
browser) implementa este Protocol para que el motor de skills (training
+ replay autónomo) sea uniforme.

Diseño:
- ``CapturedAction``: snapshot atómico de una acción del formador en esta
  superficie. Inmutable. Incluye payload tokenizado por PII.
- ``ReplayOutcome``: resultado de re-ejecutar una acción capturada.
- ``SurfaceAdapterPort``: Protocol. Tres métodos: ``capture``, ``replay``,
  ``serialize_for_signing``.

Constitución I: contratos públicos inmutables. Cualquier nueva superficie
añade un nuevo SurfaceKind + un nuevo adapter, sin romper este Protocol.
Constitución III: ``CapturedAction.payload`` SIEMPRE pasa por PII
tokenization antes de cruzar al LLM (el llamador es responsable; el
adapter en infrastructure tokeniza al capturar).
Constitución IV: ``replay`` fail-closed — si la verificación pre-replay
falla, devuelve ``ReplayOutcome.failed(...)``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable
from uuid import UUID, uuid4

from hermes.agents_os.domain.surface_kind import SurfaceKind


class ReplayStatus(StrEnum):
    EXECUTED_OK = "executed_ok"
    EXECUTED_FAILED = "executed_failed"
    HITL_REQUIRED = "hitl_required"
    REJECTED_BY_CONSENT = "rejected_by_consent"
    REJECTED_BY_POLICY = "rejected_by_policy"


@dataclass(frozen=True, slots=True)
class CapturedAction:
    """Acción capturada en una superficie. Inmutable.

    Atributos:
        action_id: UUID único.
        surface_kind: superficie de captura.
        intent_desc: descripción humana de la intención (tokenizada PII).
        payload: parámetros concretos de la acción (también tokenizados).
        captured_at: timestamp dentro del proceso del adapter (NO del
            cliente remoto — coherencia con NFR-001a heredado spec 002).
        tenant_id: multi-tenant strict.
        human_operator_id: quién hizo la demostración.
        work_item_id: ID del WorkItem que originó este replay. Optional so
            that stateless adapters remain unaffected (Liskov).
    """

    action_id: UUID = field(default_factory=uuid4)
    surface_kind: SurfaceKind = SurfaceKind.BROWSER
    intent_desc: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    captured_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    tenant_id: UUID | None = None
    human_operator_id: UUID | None = None
    work_item_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ReplayOutcome:
    """Resultado de replay. Inmutable."""

    action_id: UUID
    status: ReplayStatus
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    duration_ms: int = 0
    completed_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))

    @classmethod
    def ok(
        cls,
        action_id: UUID,
        *,
        duration_ms: int = 0,
        result: dict[str, Any] | None = None,
    ) -> ReplayOutcome:
        return cls(
            action_id=action_id,
            status=ReplayStatus.EXECUTED_OK,
            result=result or {},
            duration_ms=duration_ms,
        )

    @classmethod
    def failed(cls, action_id: UUID, *, error: str) -> ReplayOutcome:
        return cls(
            action_id=action_id,
            status=ReplayStatus.EXECUTED_FAILED,
            error=error,
        )

    @classmethod
    def rejected_by_consent(cls, action_id: UUID, *, reason: str) -> ReplayOutcome:
        return cls(
            action_id=action_id,
            status=ReplayStatus.REJECTED_BY_CONSENT,
            error=reason,
        )

    @classmethod
    def rejected_by_policy(cls, action_id: UUID, *, reason: str) -> ReplayOutcome:
        return cls(
            action_id=action_id,
            status=ReplayStatus.REJECTED_BY_POLICY,
            error=reason,
        )


@runtime_checkable
class SurfaceAdapterPort(Protocol):
    """Contrato de adapter por superficie.

    Cumplido por:
    - ``BrowserSurfaceAdapter`` (heredado spec 002).
    - ``TerminalSurfaceAdapter``.
    - ``FilesystemSurfaceAdapter``.
    - ``ApiCallSurfaceAdapter``.
    - ``DesktopAppSurfaceAdapter``.
    - ``SystemSettingsSurfaceAdapter``.
    - ``PackageManagerSurfaceAdapter``.
    """

    @property
    def surface_kind(self) -> SurfaceKind: ...

    async def capture(
        self,
        *,
        intent_desc: str,
        params: dict[str, Any],
        tenant_id: UUID,
        human_operator_id: UUID,
    ) -> CapturedAction:
        """Captura una acción demostrada por el formador.

        El adapter aplica PII tokenization (constitución III) antes de
        devolver. NO ejecuta la acción si la captura es pasiva — solo la
        registra. Algunos adapters (TERMINAL) requieren ejecutar para
        capturar el output; en esos casos la ejecución forma parte de la
        demostración del formador y queda explícita.
        """
        ...

    async def replay(
        self,
        action: CapturedAction,
        *,
        hitl_approval_token: str | None = None,
        consent_token: str | None = None,
    ) -> ReplayOutcome:
        """Re-ejecuta una acción capturada.

        Fail-closed:
        - Si ``action.surface_kind != self.surface_kind`` → REJECTED_BY_POLICY.
        - Si la acción requiere consentimiento (personal-desktop) y
          ``consent_token`` falta o es inválido → REJECTED_BY_CONSENT.
        - Si la acción es HIGH risk y ``hitl_approval_token`` falta →
          HITL_REQUIRED.
        """
        ...

    def serialize_for_signing(self, action: CapturedAction) -> bytes:
        """Serialización canonicalizada para HMAC.

        Usada por ``SkillSigner`` para firmar steps cross-surface.
        Determinista: misma action → mismos bytes siempre.
        """
        ...
