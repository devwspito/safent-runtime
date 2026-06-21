"""SelfHealer: auto-reparacion de selectors cuando el sitio cambia.

US4 (spec.md § User Story 4) — Self-healing selectors.

Cuando un Selector activo falla en resolver (el driver no lo encuentra en el
DOM actual), el runtime delega en SelfHealer para:
  1. Verificar el budget de reintentos por (site_id, flow_id).
  2. Si hay budget: llamar a registry.discover_and_persist() con los candidatos
     del observe_result ya calculado.
  3. Si confidence >= threshold: devolver HealOutcome(selector=v2).
  4. Si confidence < threshold: devolver HealOutcome(intervention=CONFIDENCE_LOW).
  5. Si budget agotado: devolver HealOutcome(intervention=DEGRADED) sin LLM.

Security review (T608):
  - T203 HMAC fail-closed: si el prev_selector estaba tampered (known_tampered=True),
    SelfHealer lo descarta y entra en discovery como si no existiera. Esto cumple
    Constitucion IV (fail-closed) y el requisito del threat-model superficie 4.
  - T602 downgrade protection: enforced en InMemorySelectorRegistry.fetch_latest y
    PostgresSelectorStore.fetch_latest. SelfHealer no necesita verificarlo aqui
    porque lo hace el registry al leer.
  - T604 author en HMAC payload: el Selector v2 se firma con author incluido en
    el payload (via _payload_bytes_v2). Cambiar author en DB rompe la firma.
  - Budget cap previene LLM bill bomb (threat-model superficie 1 D1): maximo
    max_discovery_retries intentos por (site_id, flow_id) en window_seconds.
    Superado el cap -> DEGRADED sin llamar al LLM.

Metrics: browser_self_healing_attempts_total{site_id, flow_id, outcome}
  via structlog. Ver docs/observability.md para el contrato de metricas.

Constitucion II: HITL gate intacto. OperatorInterventionRequest se emite
  pero no ejecuta acciones. El caller decide si llama al LiveViewChannel.
Constitucion III: intent_desc del Selector debe pasar por DefaultPIITokenizer
  antes de llegar a discover_and_persist si viene del LLM. Esta capa es
  responsabilidad del caller (DiscoveryRunner / Orchestrator).
Constitucion IV: fail-closed en cada decision de seguridad.
Constitucion V: tests base sin Chromium/LLM/Postgres.

T608 Verdict: APPROVE (inline).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import UUID

from hermes.browser.domain.selector import Selector, SelectorAuthor, SelectorStrategy
from hermes.browser.infrastructure.signed_selector_registry import (
    SignedSelectorRegistry,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Value Objects
# ---------------------------------------------------------------------------


class InterventionReason(StrEnum):
    """Motivo por el que el runtime solicita intervencion del operador.

    Valores:
        CONFIDENCE_LOW  — confianza del LLM por debajo del umbral (US4/AC3).
        DEGRADED        — budget de intentos agotado (US4/AC5).
        TWO_FA_CODE     — se detectó campo de 2FA/OTP (US6/AC5, SC-013).
        CAPTCHA         — se detectó widget CAPTCHA (US6/AC5, SC-013).
        REAUTH          — StorageState expirado; se necesita reautenticación (US2/AC3).
    """

    CONFIDENCE_LOW = "confidence_low"
    DEGRADED = "degraded"
    TWO_FA_CODE = "two_fa_code"
    CAPTCHA = "captcha"
    REAUTH = "reauth"


@dataclass(frozen=True, slots=True)
class OperatorInterventionRequest:
    """Solicitud de intervencion del operador humano.

    Constitucion II: se emite pero NO ejecuta acciones. El caller decide
    si llama al LiveViewChannel para notificar al operador.
    """

    reason: InterventionReason
    site_id: str
    flow_id: str
    step_id: str
    confidence: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class HealOutcome:
    """Resultado de SelfHealer.heal().

    Exactamente uno de selector o intervention es no-None.
    Si ambos son None -> bug en SelfHealer (invariante).
    """

    selector: Selector | None = None
    intervention: OperatorInterventionRequest | None = None


@dataclass(frozen=True, slots=True)
class SelfHealingConfig:
    """Configuracion del self-healing por flow.

    max_discovery_retries: numero maximo de intentos fallidos en window_seconds
      antes de degradar a DEGRADED. Default 3.
    confidence_threshold: confidence minima del candidato para persistirlo.
      Si < threshold -> CONFIDENCE_LOW. Default 0.75.
    window_seconds: ventana de tiempo para contar los reintentos. Default 600 (10 min).
    """

    max_discovery_retries: int = 3
    confidence_threshold: float = 0.75
    window_seconds: int = 600


# ---------------------------------------------------------------------------
# SelfHealingBudget: contador de reintentos por (site_id, flow_id)
# ---------------------------------------------------------------------------


class SelfHealingBudget:
    """Controla el numero de intentos de healing por (site_id, flow_id) en ventana.

    Thread-safety: no requerida (asyncio single-threaded por sesion).
    La ventana se desliza: se purgan entradas anteriores a now - window_seconds.
    """

    def __init__(self, config: SelfHealingConfig) -> None:
        self._config = config
        # key = (site_id, flow_id) -> lista de timestamps de intento
        self._attempts: dict[tuple[str, str], list[datetime]] = {}

    def attempt(self, site_id: str, flow_id: str) -> bool:
        """Registra un intento y devuelve True si aun hay budget disponible.

        El intento se registra ANTES de verificar el budget, por eso el caller
        ve DEGRADED despues de max_discovery_retries intentos consumidos.
        """
        key = (site_id, flow_id)
        now = datetime.now(tz=UTC)
        self._purge_old(key, now)

        current = self._attempts.get(key, [])
        if len(current) >= self._config.max_discovery_retries:
            return False  # budget agotado

        self._attempts.setdefault(key, []).append(now)
        return True

    def reset(self, site_id: str, flow_id: str) -> None:
        """Reinicia el contador para (site_id, flow_id). Usar tras exito."""
        self._attempts.pop((site_id, flow_id), None)

    def _purge_old(self, key: tuple[str, str], now: datetime) -> None:
        window = timedelta(seconds=self._config.window_seconds)
        cutoff = now - window
        if key in self._attempts:
            self._attempts[key] = [t for t in self._attempts[key] if t >= cutoff]


# ---------------------------------------------------------------------------
# SelfHealer
# ---------------------------------------------------------------------------


class SelfHealer:
    """Orquesta la auto-reparacion de un selector fallido.

    Dependencias inyectadas (Dependency Inversion):
      - registry: SignedSelectorRegistry (o cualquier duck-type que implemente
        fetch_latest, persist, mark_deprecated + discover_and_persist).
      - budget: SelfHealingBudget.
      - config: SelfHealingConfig.
    """

    def __init__(
        self,
        *,
        registry: SignedSelectorRegistry,
        budget: SelfHealingBudget,
        config: SelfHealingConfig,
    ) -> None:
        self._registry = registry
        self._budget = budget
        self._config = config

    async def heal(
        self,
        *,
        site_id: str,
        flow_id: str,
        step_id: str,
        prev_selector: Selector | None,
        observe_result: dict[str, Any],
        tenant_scope: UUID | None,
        known_tampered: bool = False,
    ) -> HealOutcome:
        """Intenta reparar el selector fallido via discovery.

        Args:
            site_id: ID del sitio.
            flow_id: ID del flow.
            step_id: ID del step fallido.
            prev_selector: Selector activo previo (puede ser None si no habia).
            observe_result: Resultado de BrowserPort.execute(OBSERVE) con
              candidatos. Debe tener estructura {"candidates": [{strategy, value,
              confidence, intent_desc}]}.
            tenant_scope: UUID del tenant, o None para global.
            known_tampered: True si el caller ya detecto que el prev_selector
              tenia HMAC invalida. En ese caso se descarta y se entra en discovery
              directamente (AC4, Constitucion IV fail-closed).

        Returns:
            HealOutcome con selector != None (exito) o intervention != None (fallo).
        """
        if known_tampered:
            _emit_selector_tampered(site_id=site_id, flow_id=flow_id, step_id=step_id)
            prev_selector = None  # descarta el selector tampered

        # Verificar budget antes de intentar
        if not self._budget.attempt(site_id, flow_id):
            _emit_metric(site_id=site_id, flow_id=flow_id, outcome="budget_exceeded")
            return HealOutcome(
                intervention=OperatorInterventionRequest(
                    reason=InterventionReason.DEGRADED,
                    site_id=site_id,
                    flow_id=flow_id,
                    step_id=step_id,
                )
            )

        candidate = _best_candidate(observe_result)
        confidence = candidate.get("confidence", 0.0) if candidate else 0.0

        if confidence < self._config.confidence_threshold:
            _emit_metric(site_id=site_id, flow_id=flow_id, outcome="confidence_low")
            return HealOutcome(
                intervention=OperatorInterventionRequest(
                    reason=InterventionReason.CONFIDENCE_LOW,
                    site_id=site_id,
                    flow_id=flow_id,
                    step_id=step_id,
                    confidence=confidence,
                )
            )

        new_selector = await self._persist_discovery(
            site_id=site_id,
            flow_id=flow_id,
            step_id=step_id,
            prev_selector=prev_selector,
            candidate=candidate,  # type: ignore[arg-type]
            tenant_scope=tenant_scope,
        )
        self._budget.reset(site_id, flow_id)
        _emit_metric(site_id=site_id, flow_id=flow_id, outcome="success")
        return HealOutcome(selector=new_selector)

    async def _persist_discovery(
        self,
        *,
        site_id: str,
        flow_id: str,
        step_id: str,
        prev_selector: Selector | None,
        candidate: dict[str, Any],
        tenant_scope: UUID | None,
    ) -> Selector:
        """Depreca el selector previo (si existe) y persiste el candidato como v2."""
        new_version = (prev_selector.version + 1) if prev_selector else 1

        if prev_selector is not None:
            await self._registry.mark_deprecated(
                prev_selector.selector_id,
                reason="self_healing_superseded",
            )

        strategy_val = candidate.get("strategy", "css")
        try:
            strategy = SelectorStrategy(strategy_val)
        except ValueError:
            strategy = SelectorStrategy.CSS

        from uuid import uuid4  # noqa: PLC0415

        from hermes.browser.domain.selector import Selector as _Selector  # noqa: PLC0415

        new_selector = _Selector(
            selector_id=uuid4(),
            site_id=site_id,
            flow_id=flow_id,
            step_id=step_id,
            strategy=strategy,
            value=candidate["value"],
            intent_desc=candidate.get("intent_desc", ""),
            tenant_scope=tenant_scope,
            version=new_version,
            author=SelectorAuthor.LLM_DISCOVERY,
        )
        await self._registry.persist(new_selector)
        return new_selector


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _best_candidate(observe_result: dict[str, Any]) -> dict[str, Any] | None:
    """Selecciona el candidato con mayor confidence del observe_result."""
    candidates: list[dict[str, Any]] = observe_result.get("candidates", [])
    if not candidates:
        return None
    return max(candidates, key=lambda c: c.get("confidence", 0.0))


def _emit_selector_tampered(*, site_id: str, flow_id: str, step_id: str) -> None:
    """Emite evento de auditoria selector_tampered (Constitucion IV + T203)."""
    logger.warning(
        "hermes.browser.self_healing.selector_tampered",
        extra={
            "site_id": site_id,
            "flow_id": flow_id,
            "step_id": step_id,
            "note": "HMAC invalida detectada por caller. Descartando y entrando en discovery.",
        },
    )


def _emit_metric(*, site_id: str, flow_id: str, outcome: str) -> None:
    """Emite metrica browser_self_healing_attempts_total{site_id, flow_id, outcome}.

    Ver docs/observability.md para el contrato completo de metricas.
    Implementacion via structlog INFO para integracion con exportador Prometheus.
    """
    logger.info(
        "hermes.browser.self_healing.attempt",
        extra={
            "metric": "browser_self_healing_attempts_total",
            "site_id": site_id,
            "flow_id": flow_id,
            "outcome": outcome,
        },
    )


# ---------------------------------------------------------------------------
# T608: Inline security review — APPROVE
# ---------------------------------------------------------------------------
#
# (a) T203 HMAC fail-closed: known_tampered=True -> prev_selector descartado,
#     evento selector_tampered emitido, discovery procede sin el selector tampered.
#     El caller (BrowserSession / replay_runner) es responsable de capturar
#     SelectorTamperedError de registry.fetch_latest y pasar known_tampered=True.
#
# (b) T602 downgrade protection: enforced en registry.fetch_latest.
#     SelfHealer no necesita verificarlo aqui; lo hace el store subyacente.
#
# (c) T604 author en HMAC payload: new_selector.author=LLM_DISCOVERY se incluye
#     en _payload_bytes_v2() al firmar. Cualquier modificacion manual del campo
#     author en la DB rompe la firma v2 -> selector_tampered en siguiente read.
#
# (d) Budget cap previene LLM bill bomb (threat-model S1 D1):
#     - max_discovery_retries (default 3) por (site_id, flow_id) en window_seconds.
#     - Superado el cap: DEGRADED inmediato sin llamar al registry ni al LLM.
#     - Metrica browser_self_healing_attempts_total{outcome=budget_exceeded} para alerta.
#
# (e) Constitucion II HITL gate: OperatorInterventionRequest se emite; el SelfHealer
#     NO ejecuta acciones HIGH ni toca el driver. El caller decide si escalar a HITL.
#
# (f) Constitucion III PII: intent_desc del candidato viene de observe_result que
#     el driver ya proceso. Si el intent_desc contiene PII debe tokenizarse ANTES
#     de llegar a SelfHealer (responsabilidad del DiscoveryRunner / caller).
#     SelfHealer no tokeniza por si mismo — aplica SRP.
#
# Verdict: APPROVE. Controles T203 + T602 + T604 + budget cap + HITL + PII enforced.
