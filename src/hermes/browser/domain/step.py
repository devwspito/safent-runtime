"""Step: unidad de accion del browser agent.

Cada `act` / `extract` / `observe` propuesto por el LLM se materializa como
un `Step`. El `StepRecorder` persiste screenshot + DOM snapshot pre/post +
risk_level + outcome.

Risk classification (alineado con kernel.hitl.StepRiskLevel):
    LOW    — navegacion, lectura, listado.
    MEDIUM — rellenar formulario, subir borrador.
    HIGH   — presentar definitivo, baja efectiva, pago, transferencia.

Los Steps HIGH pasan obligatoriamente por HITL gate con TOTP. El
`BrowserSession` no los ejecuta sin token de aprobacion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4


class StepRisk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class StepKind(StrEnum):
    """Verbos del browser. Mapean al stack (Stagehand / Playwright / etc.)."""

    NAVIGATE = "navigate"
    OBSERVE = "observe"  # tier 1: Stagehand observe — lista candidatos sin actuar
    EXTRACT = "extract"  # tier 1: Stagehand extract — devuelve JSON estructurado
    ACT = "act"  # tier 1: Stagehand act — click / fill / scroll / select
    SCREENSHOT = "screenshot"
    WAIT = "wait"
    UPLOAD = "upload"
    DOWNLOAD = "download"


class StepStatus(StrEnum):
    PROPOSED = "proposed"  # Step esta creado, NO ejecutado
    APPROVED = "approved"  # HITL aprobo (solo HIGH lo necesita)
    EXECUTED_OK = "executed_ok"
    EXECUTED_FAILED = "executed_failed"
    REJECTED = "rejected"  # HITL rechazo
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class Step:
    """Step inmutable. Identidad = `step_id`.

    `payload` es opaco por tipo de step:
        NAVIGATE -> {"url": str}
        ACT      -> {"instruction": str, "fill_value": str | None}
        EXTRACT  -> {"instruction": str, "schema": dict}
        OBSERVE  -> {"instruction": str}
        UPLOAD   -> {"selector_ref": str, "file_path_ref": str}
        DOWNLOAD -> {"selector_ref": str, "target_dir": str}
    """

    step_id: UUID
    step_group_id: UUID
    tenant_id: UUID
    session_id: UUID
    kind: StepKind
    risk: StepRisk
    intent_desc: str  # NL — "click submit btn del 303"
    payload: dict[str, Any]
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))

    @classmethod
    def new(
        cls,
        *,
        tenant_id: UUID,
        session_id: UUID,
        kind: StepKind,
        risk: StepRisk,
        intent_desc: str,
        payload: dict[str, Any],
        step_group_id: UUID | None = None,
    ) -> Step:
        return cls(
            step_id=uuid4(),
            step_group_id=step_group_id or uuid4(),
            tenant_id=tenant_id,
            session_id=session_id,
            kind=kind,
            risk=risk,
            intent_desc=intent_desc,
            payload=payload,
        )

    @property
    def requires_hitl(self) -> bool:
        """Solo HIGH atraviesa HITL obligatoriamente.

        MEDIUM se puede configurar por policy (defecto: bloquea solo si la
        sesion lo declara). LOW siempre auto.
        """
        return self.risk == StepRisk.HIGH


@dataclass(frozen=True, slots=True)
class StepOutcome:
    """Resultado de ejecutar un Step.

    `result` es opaco: para EXTRACT contiene el JSON; para ACT/NAVIGATE
    contiene metadata (url final, dom size, etc.). Para WAIT vacio.
    """

    step_id: UUID
    status: StepStatus
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    completed_at: datetime | None = None
    duration_ms: int = 0
    screenshot_pre_ref: str = ""  # ID en el step recorder
    screenshot_post_ref: str = ""
    dom_pre_ref: str = ""
    dom_post_ref: str = ""

    @classmethod
    def ok(
        cls,
        *,
        step_id: UUID,
        duration_ms: int,
        result: dict[str, Any] | None = None,
        screenshot_pre_ref: str = "",
        screenshot_post_ref: str = "",
        dom_pre_ref: str = "",
        dom_post_ref: str = "",
    ) -> StepOutcome:
        now = datetime.now(tz=UTC)
        return cls(
            step_id=step_id,
            status=StepStatus.EXECUTED_OK,
            result=result or {},
            started_at=now,
            completed_at=now,
            duration_ms=duration_ms,
            screenshot_pre_ref=screenshot_pre_ref,
            screenshot_post_ref=screenshot_post_ref,
            dom_pre_ref=dom_pre_ref,
            dom_post_ref=dom_post_ref,
        )

    @classmethod
    def failed(
        cls,
        *,
        step_id: UUID,
        error: str,
        duration_ms: int = 0,
        screenshot_pre_ref: str = "",
    ) -> StepOutcome:
        now = datetime.now(tz=UTC)
        return cls(
            step_id=step_id,
            status=StepStatus.EXECUTED_FAILED,
            error=error,
            started_at=now,
            completed_at=now,
            duration_ms=duration_ms,
            screenshot_pre_ref=screenshot_pre_ref,
        )

    @classmethod
    def rejected_by_hitl(cls, *, step_id: UUID, reason: str) -> StepOutcome:
        return cls(
            step_id=step_id,
            status=StepStatus.REJECTED,
            error=reason,
        )
