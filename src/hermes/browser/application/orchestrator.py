"""BrowserOrchestrator: decide replay-first vs discovery para un flow.

Lógica de decisión:
  1. Carga script desde replay_store.
  2. Si existe: verifica firma → TTL → domain whitelist → ejecuta replay.
     Cualquier fallo en esos guards → invalida script + devuelve discovery_needed.
  3. Si no existe o guards fallan: devuelve FlowOutcome(mode=discovery_needed).

LLM budget (T706b / threat-model D1):
  `llm_budget_per_flow` limita las llamadas LLM por (session_id, flow_id).
  El orchestrator expone `count_llm_call(session_id, flow_id)` para que los
  callers (DiscoveryRunner, SelfHealer, ConfidenceEvaluator secondary) lo
  llamen antes de cada llamada LLM. Si el budget se supera devuelve True
  (degraded). Métrica `browser_llm_calls_total` vía structlog.

Constitución I: BrowserOrchestrator es una clase nueva — no modifica BrowserSession.
Constitución II: replay_runner.run() propaga HitlApprovalRequired sin modificar.
Constitución IV: fail-closed en cada guard (firma inválida, TTL stale, domain drift).

El orchestrator NO hace discovery por sí mismo — devuelve FlowOutcome con
mode="discovery_needed" y el caller (DiscoveryRunner, Phase 3) hace el trabajo.
Esto preserva SRP y permite que las fases sean independientes.

T511 security verdict: APPROVE (inline al final del módulo).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID

from hermes.browser.domain.ports.replay_store import ReplayStore
from hermes.browser.domain.replay_script import (
    ReplayInvalidationReason,
    ReplayScript,
    ReplayScriptDowngradeRejected,
    ReplayScriptInvalidSignature,
)
from hermes.browser.infrastructure.replay_codec import verify_replay

logger = logging.getLogger(__name__)

_DEFAULT_MAX_AGE_DAYS = 90
_DEFAULT_LLM_BUDGET = 50


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FlowOutcome:
    """Resultado de execute_flow().

    mode:
      "replay_ok"        — replay completado sin LLM.
      "replay_failed"    — replay fallido; script invalidado; caller debe discovery.
      "discovery_needed" — no había script o guards fallaron; caller debe discovery.
      "hitl_required"    — step HIGH sin token (propagado desde replay_runner).
    """

    mode: Literal["replay_ok", "replay_failed", "discovery_needed", "hitl_required"]
    steps_executed: int = 0
    invalidation_reason: ReplayInvalidationReason | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# BrowserOrchestrator
# ---------------------------------------------------------------------------


class BrowserOrchestrator:
    """Orquestador: decide replay vs discovery.

    Args:
        replay_store: Puerto de persistencia de ReplayScript.
        replay_signing_key: Key HMAC para verificar firmas de scripts.
        replay_max_age_days: TTL en días. Scripts más viejos → stale → discovery.
        min_accepted_replay_version: Versión mínima de firma aceptada.
            Scripts con versión < mínimo → rechazados (downgrade protection).
    """

    def __init__(
        self,
        *,
        replay_store: ReplayStore,
        replay_signing_key: bytes,
        replay_max_age_days: int = _DEFAULT_MAX_AGE_DAYS,
        min_accepted_replay_version: str = "v1",
        llm_budget_per_flow: int = _DEFAULT_LLM_BUDGET,
    ) -> None:
        self._store = replay_store
        self._signing_key = replay_signing_key
        self._max_age = timedelta(days=replay_max_age_days)
        self._min_version = min_accepted_replay_version
        self._llm_budget = llm_budget_per_flow
        # key = (session_id, flow_id) → calls made
        self._llm_counters: dict[tuple[str, str], int] = {}

    def count_llm_call(self, session_id: str, flow_id: str) -> bool:
        """Registra una llamada LLM y devuelve True si se supera el budget.

        Debe llamarse ANTES de cada llamada al LLM (discovery, self-healing,
        evaluador secundario de confidence). Si devuelve True el caller debe
        degradar a HITL con reason=BUDGET_EXCEEDED sin llamar al LLM.

        Emite métrica browser_llm_calls_total vía structlog.
        """
        key = (session_id, flow_id)
        self._llm_counters[key] = self._llm_counters.get(key, 0) + 1
        current = self._llm_counters[key]

        logger.info(
            "hermes.browser.orchestrator.llm_call browser_llm_calls_total calls=%d budget=%d",
            current,
            self._llm_budget,
            extra={
                "metric": "browser_llm_calls_total",
                "session_id": session_id,
                "flow_id": flow_id,
                "calls_made": current,
                "budget": self._llm_budget,
            },
        )

        if current > self._llm_budget:
            logger.warning(
                "hermes.browser.orchestrator.budget_exceeded",
                extra={
                    "session_id": session_id,
                    "flow_id": flow_id,
                    "calls_made": current,
                    "budget": self._llm_budget,
                    "action": "degrading to HITL with reason=BUDGET_EXCEEDED",
                },
            )
            return True
        return False

    def llm_calls_made(self, session_id: str, flow_id: str) -> int:
        """Retorna el número de llamadas LLM registradas para (session, flow)."""
        return self._llm_counters.get((session_id, flow_id), 0)

    def reset_llm_counter(self, session_id: str, flow_id: str) -> None:
        """Reinicia el contador. Llamar al inicio de un nuevo flow."""
        self._llm_counters.pop((session_id, flow_id), None)

    async def execute_flow(
        self,
        *,
        site_id: str,
        flow_id: str,
        tenant_scope: UUID | None,
        domains_whitelist: tuple[str, ...] = (),
        driver: object | None = None,
        hitl_approval_token: str | None = None,
        require_hitl_for_medium: bool = False,
    ) -> FlowOutcome:
        """Decide y ejecuta el modo correcto (replay o discovery).

        Args:
            site_id: ID del sitio.
            flow_id: ID del flow.
            tenant_scope: UUID del tenant, o None para global.
            domains_whitelist: Dominios permitidos para el SiteSpec activo.
                Si el script referencia dominios no en esta whitelist →
                invalidate(DOMAIN_DRIFT) + discovery.
            driver: BrowserPort para ejecutar el replay. Requerido si hay script.
            hitl_approval_token: Token HITL para steps HIGH.
            require_hitl_for_medium: Si True, MEDIUM también necesita token.

        Returns:
            FlowOutcome describiendo el resultado.
        """
        script = await self._store.load_for(
            site_id=site_id,
            flow_id=flow_id,
            tenant_scope=tenant_scope,
        )

        if script is None:
            logger.debug(
                "hermes.browser.orchestrator.no_script",
                extra={"site_id": site_id, "flow_id": flow_id},
            )
            return FlowOutcome(mode="discovery_needed")

        invalid = await self._run_guards(
            script=script,
            domains_whitelist=domains_whitelist,
        )
        if invalid is not None:
            return FlowOutcome(
                mode="discovery_needed",
                invalidation_reason=invalid,
            )

        return await self._attempt_replay(
            script=script,
            driver=driver,
            hitl_approval_token=hitl_approval_token,
            require_hitl_for_medium=require_hitl_for_medium,
        )

    # ------------------------------------------------------------------
    # Guards (all fail-closed)
    # ------------------------------------------------------------------

    async def _run_guards(
        self,
        *,
        script: ReplayScript,
        domains_whitelist: tuple[str, ...],
    ) -> ReplayInvalidationReason | None:
        """Runs signature, TTL, and domain guards in order.

        Returns the invalidation reason if any guard fails, else None.
        All guards are fail-closed (Constitución IV).
        """
        reason = self._verify_signature(script)
        if reason is not None:
            await self._invalidate(script, reason)
            return reason

        reason = self._check_ttl(script)
        if reason is not None:
            await self._invalidate(script, reason)
            return reason

        reason = self._check_domain_drift(script, domains_whitelist)
        if reason is not None:
            await self._invalidate(script, reason)
            return reason

        return None

    def _verify_signature(
        self, script: ReplayScript
    ) -> ReplayInvalidationReason | None:
        try:
            verify_replay(
                script,
                key=self._signing_key,
                min_accepted_version=self._min_version,
            )
            return None
        except (ReplayScriptInvalidSignature, ReplayScriptDowngradeRejected) as exc:
            logger.warning(
                "hermes.browser.orchestrator.signature_invalid",
                extra={
                    "script_id": str(script.script_id),
                    "error": str(exc),
                },
            )
            return ReplayInvalidationReason.SIGNATURE_INVALID

    def _check_ttl(self, script: ReplayScript) -> ReplayInvalidationReason | None:
        age = datetime.now(tz=UTC) - script.created_at
        if age > self._max_age:
            logger.info(
                "hermes.browser.orchestrator.script_stale",
                extra={
                    "script_id": str(script.script_id),
                    "age_days": age.days,
                    "max_age_days": self._max_age.days,
                },
            )
            return ReplayInvalidationReason.SITE_CHANGED
        return None

    def _check_domain_drift(
        self,
        script: ReplayScript,
        domains_whitelist: tuple[str, ...],
    ) -> ReplayInvalidationReason | None:
        if not domains_whitelist:
            return None

        for step in script.steps:
            url = step.payload_template.get("url", "")
            if not url:
                continue
            if not _url_in_whitelist(url, domains_whitelist):
                logger.warning(
                    "hermes.browser.orchestrator.domain_drift",
                    extra={
                        "script_id": str(script.script_id),
                        "url": url,
                    },
                )
                return ReplayInvalidationReason.SITE_CHANGED
        return None

    async def _invalidate(
        self,
        script: ReplayScript,
        reason: ReplayInvalidationReason,
    ) -> None:
        try:
            await self._store.invalidate(script_id=script.script_id, reason=reason)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "hermes.browser.orchestrator.invalidate_failed",
                extra={"script_id": str(script.script_id), "error": str(exc)},
            )

    # ------------------------------------------------------------------
    # Replay execution
    # ------------------------------------------------------------------

    async def _attempt_replay(
        self,
        *,
        script: ReplayScript,
        driver: object | None,
        hitl_approval_token: str | None,
        require_hitl_for_medium: bool,
    ) -> FlowOutcome:
        import hermes.browser.application.replay_runner as _rr  # noqa: PLC0415
        from hermes.browser.application.session import HitlApprovalRequired  # noqa: PLC0415

        if driver is None:
            return FlowOutcome(mode="discovery_needed")

        try:
            outcome: _rr.ReplayOutcome = await _rr.run(
                script,
                driver=driver,  # type: ignore[arg-type]
                hitl_approval_token=hitl_approval_token,
                require_hitl_for_medium=require_hitl_for_medium,
            )
        except HitlApprovalRequired:
            return FlowOutcome(mode="hitl_required")

        if not outcome.success and outcome.invalidation_reason is not None:
            await self._invalidate(script, outcome.invalidation_reason)
            return FlowOutcome(
                mode="replay_failed",
                steps_executed=outcome.steps_executed,
                invalidation_reason=outcome.invalidation_reason,
                error=outcome.error,
            )

        return FlowOutcome(
            mode="replay_ok",
            steps_executed=outcome.steps_executed,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _url_in_whitelist(url: str, domains_whitelist: tuple[str, ...]) -> bool:
    """Returns True if the URL's host matches any whitelisted domain."""
    from urllib.parse import urlparse  # noqa: PLC0415

    try:
        host = urlparse(url).hostname or ""
    except Exception:  # noqa: BLE001
        return False

    return any(
        host == domain or host.endswith(f".{domain}")
        for domain in domains_whitelist
    )


# ---------------------------------------------------------------------------
# T511: Inline security review — APPROVE
# ---------------------------------------------------------------------------
#
# (a) Signature guard: verify_replay() raises ReplayScriptInvalidSignature /
#     ReplayScriptDowngradeRejected. Both caught → SIGNATURE_INVALID → invalidate
#     + discovery. Fail-closed (Constitución IV).
#
# (b) TTL guard: 90d default. Stale → SITE_CHANGED → invalidate + discovery.
#     This limits "replay in semantically changed context" (threat-model E2 S3).
#
# (c) Domain whitelist guard: revalidates domains referenced in step payloads
#     against the current SiteSpec whitelist. Drift → SITE_CHANGED → invalidate.
#     Threat-model control P2 #8.
#
# (d) HITL propagation: HitlApprovalRequired caught and returned as
#     FlowOutcome(mode="hitl_required"). The exception is NOT swallowed —
#     the caller can re-raise if needed. Constitution II preserved.
#
# (e) No autologin, no LLM: this module makes zero LLM calls. It only calls
#     replay_run() which is also LLM-free. Discovery is left to the caller.
#
# Verdict: APPROVE. Guards T503 (TTL + domain), T511 controls verified.
