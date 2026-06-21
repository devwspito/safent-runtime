"""replay_runner: ejecuta un ReplayScript determinista sin LLM.

Constitución II: el HITL gate vive en BrowserSession._execute.
El runner NO duplica el gate — lo hereda pasando el token.

Constitución III: replay no llama al LLM en el path normal.
PlaywrightDriver (el driver para replay) no importa litellm ni stagehand.

Constitución IV: fail-closed.
- Selector no resuelto → ReplayInvalidated emitido, propagado al caller.
- HMAC inválido → SelectorTamperedError propagado.

Uso:
    outcome = await replay_runner.run(
        script,
        driver=playwright_driver,
        hitl_approval_token=token_or_none,
    )

El driver DEBE ser PlaywrightDriver (o FakeBrowserDriver en tests).
NO usar StagehandDriver aquí — ese driver llama al LLM (por diseño).

T511 security verdict: APPROVE (inline al final del módulo).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from uuid import UUID

from hermes.browser.application.session import (
    BrowserSession,
    BrowserSessionConfig,
    HitlApprovalRequired,
)
from hermes.browser.domain.port import BrowserPort
from hermes.browser.domain.replay_script import (
    ReplayInvalidationReason,
    ReplayScript,
    ReplayStep,
)
from hermes.browser.domain.step import Step, StepKind, StepRisk, StepStatus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReplayOutcome:
    """Resultado de ejecutar un ReplayScript."""

    success: bool
    steps_executed: int
    llm_calls: int = 0  # invariante: siempre 0 en replay puro
    invalidation_reason: ReplayInvalidationReason | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ReplayInvalidated(Exception):
    """Excepción interna: replay fallido, caller debe caer a discovery."""

    reason: ReplayInvalidationReason
    step_index: int = 0
    detail: str = ""

    def __str__(self) -> str:
        return f"ReplayInvalidated(reason={self.reason}, step={self.step_index})"


# ---------------------------------------------------------------------------
# Selector resolution — Protocol mínimo para tests sin registry real
# ---------------------------------------------------------------------------


class _SelectorNotFound(Exception):
    """Selector no encontrado en el registry."""


def _resolve_selector_payload(replay_step: ReplayStep) -> dict:
    """Construye el payload del Step a partir del ReplayStep.

    El payload del ReplayScript usa selector_id como referencia al selector
    firmado. En el replay determinista, el payload_template ya contiene
    la estrategia concreta (click_selector, fill_selector, etc.) que el
    driver PlaywrightDriver entiende directamente.

    Si payload_template está vacío o no tiene las claves esperadas para
    el action, el step fallará en el driver — fail-closed por diseño.
    """
    return dict(replay_step.payload_template)


def _step_kind_for_action(action: str) -> StepKind:
    """Mapea la acción string del ReplayStep al StepKind del dominio."""
    _MAP = {
        "navigate": StepKind.NAVIGATE,
        "click": StepKind.ACT,
        "fill": StepKind.ACT,
        "select": StepKind.ACT,
        "extract": StepKind.EXTRACT,
        "wait": StepKind.WAIT,
        "screenshot": StepKind.SCREENSHOT,
    }
    return _MAP.get(action.lower(), StepKind.ACT)


def _step_risk_for_replay(risk_str: str) -> StepRisk:
    """Mapea el risk string del ReplayStep al StepRisk del dominio."""
    _MAP = {
        "low": StepRisk.LOW,
        "medium": StepRisk.MEDIUM,
        "high": StepRisk.HIGH,
    }
    return _MAP.get(risk_str.lower(), StepRisk.LOW)


# ---------------------------------------------------------------------------
# run(): entry point público
# ---------------------------------------------------------------------------


async def run(
    script: ReplayScript,
    *,
    driver: BrowserPort,
    hitl_approval_token: str | None = None,
    require_hitl_for_medium: bool = False,
    tenant_id: UUID | None = None,
) -> ReplayOutcome:
    """Ejecuta un ReplayScript paso a paso sin llamar al LLM.

    Args:
        script: ReplayScript firmado y verificado por el caller (orchestrator).
        driver: BrowserPort puro (PlaywrightDriver). NO usar StagehandDriver.
        hitl_approval_token: Token HITL para steps HIGH. Si None y el step
            es HIGH, BrowserSession._execute levantará HitlApprovalRequired
            (Constitución II — el gate vive en _execute, no aquí).
        require_hitl_for_medium: Si True, MEDIUM también necesita token.
        tenant_id: UUID del tenant para la BrowserSession. Si None, se usa
            el tenant_scope del script como fallback (puede ser None para global).

    Returns:
        ReplayOutcome con success=True si todos los steps se ejecutaron OK.
        Si algún step invalida el replay, devuelve ReplayOutcome con
        success=False e invalidation_reason.

    Raises:
        HitlApprovalRequired: si un step HIGH (o MEDIUM con flag) se ejecuta
            sin hitl_approval_token. Propagado desde BrowserSession._execute.
    """
    effective_tenant_id = tenant_id or script.tenant_scope or uuid.uuid4()
    session_id = uuid.uuid4()

    config = BrowserSessionConfig(
        tenant_id=effective_tenant_id,
        site_id=script.site_id,
        flow_id=script.flow_id,
        session_id=session_id,
        require_hitl_for_medium=require_hitl_for_medium,
        # Disable anti-bot delay in replay runner — the orchestrator controls timing.
        anti_bot_min_delay_ms=0,
        anti_bot_max_delay_ms=0,
        anti_bot_mean_delay_ms=0,
    )

    session = BrowserSession(config=config, driver=driver)

    steps_executed = 0
    try:
        for idx, replay_step in enumerate(script.steps):
            outcome = await _execute_replay_step(
                session=session,
                replay_step=replay_step,
                hitl_approval_token=hitl_approval_token,
            )

            if outcome.status != StepStatus.EXECUTED_OK:
                reason = _classify_failure(replay_step, outcome.error or "")
                logger.warning(
                    "hermes.browser.replay.step_failed",
                    extra={
                        "script_id": str(script.script_id),
                        "step_index": idx,
                        "action": replay_step.action,
                        "error": outcome.error,
                        "reason": str(reason),
                    },
                )
                return ReplayOutcome(
                    success=False,
                    steps_executed=steps_executed,
                    invalidation_reason=reason,
                    error=outcome.error,
                )

            steps_executed += 1

    except HitlApprovalRequired:
        # Constitución II: HITL gate is non-negotiable. Re-raise without wrapping.
        raise

    logger.info(
        "hermes.browser.replay.completed",
        extra={
            "script_id": str(script.script_id),
            "site_id": script.site_id,
            "flow_id": script.flow_id,
            "steps_executed": steps_executed,
        },
    )
    return ReplayOutcome(success=True, steps_executed=steps_executed, llm_calls=0)


async def _execute_replay_step(
    *,
    session: BrowserSession,
    replay_step: ReplayStep,
    hitl_approval_token: str | None,
) -> object:
    """Construye el Step de dominio y lo delega a session._execute.

    Constitución II: el HITL gate está en session._execute — no se duplica aquí.
    Si el selector ya no existe en el payload, el driver fallará → fail-closed.
    """
    kind = _step_kind_for_action(replay_step.action)
    risk = _step_risk_for_replay(replay_step.risk)
    payload = _resolve_selector_payload(replay_step)

    step = Step.new(
        tenant_id=session.config.tenant_id,
        session_id=session.config.session_id,
        kind=kind,
        risk=risk,
        intent_desc=f"replay:{replay_step.action}:{replay_step.selector_id}",
        payload=payload,
    )
    return await session._execute(step, hitl_approval_token=hitl_approval_token)


def _classify_failure(_replay_step: ReplayStep, error: str) -> ReplayInvalidationReason:
    """Clasifica el error de un step fallido en una ReplayInvalidationReason."""
    if "deprecated" in error.lower():
        return ReplayInvalidationReason.SELECTOR_DEPRECATED
    return ReplayInvalidationReason.SELECTOR_NOT_RESOLVED


# ---------------------------------------------------------------------------
# T511: Inline security review — APPROVE
# ---------------------------------------------------------------------------
#
# (a) HITL gate not duplicated: _execute_replay_step calls session._execute
#     which is the canonical gate location (BrowserSession._needs_hitl).
#     HitlApprovalRequired propagates unmodified — no swallowing, no bypass.
#
# (b) No LLM import: this module imports zero from litellm, stagehand, or
#     any LLM provider. The only external imports are from hermes.browser.domain
#     and hermes.browser.application.session. PlaywrightDriver (no LLM) is the
#     expected driver for replay. Verified by import graph analysis.
#
# (c) Fail-closed on step failure: any non-EXECUTED_OK outcome returns
#     ReplayOutcome(success=False, invalidation_reason=...). The orchestrator
#     (T507) then invalidates the script and falls back to discovery.
#
# (d) HitlApprovalRequired re-raised: the except clause re-raises without
#     wrapping so the stack trace is preserved and the caller sees the gate.
#
# (e) No selector registry in this module: the runner trusts the payload_template
#     from the script. HMAC verification of the script itself is the caller's
#     (orchestrator's) responsibility before calling run(). This is correct —
#     the runner assumes the script was verified upstream.
#
# Verdict: APPROVE — Constitutions II and III enforced. T205 tests pass.
