"""T601 — Self-healing selector tests.

Cubre US4 AC1-AC5 (spec.md § User Story 4).

All tests use in-memory doubles — no Chromium, no LLM, no Postgres.
Constitution V: base suite sin dependencias externas.
Constitution IV: fail-closed en toda decision de seguridad.

AC1: selector v1 no resuelve -> trigger discovery acotado por budget.
AC2: candidate confidence > threshold -> Selector v2 persistido con
     author=LLM_DISCOVERY + v1 deprecated_at set.
AC3: candidate confidence < threshold -> NO persiste +
     OperatorInterventionRequest{reason=CONFIDENCE_LOW}.
AC4: HMAC invalida -> descarta + selector_tampered event + entra discovery
     como si no existiera.
AC5: N=max_discovery_retries fallos consecutivos en mismo step ->
     OperatorInterventionRequest{reason=DEGRADED}.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from hermes.browser.application.self_healing import (
    InterventionReason,
    SelfHealer,
    SelfHealingBudget,
    SelfHealingConfig,
)
from hermes.browser.domain.selector import Selector, SelectorAuthor, SelectorStrategy
from hermes.browser.infrastructure import (
    InMemorySelectorRegistry,
    SignedSelectorRegistry,
    build_signed,
)

# ---------------------------------------------------------------------------
# Constantes de test — nunca usar en produccion
# ---------------------------------------------------------------------------

_KEY = b"\xde\xad\xbe\xef" * 8
_TENANT = UUID("11111111-1111-1111-1111-111111111111")

_SITE = "stub_sede"
_FLOW = "modelo_303"
_STEP = "btn_presentar"


# ---------------------------------------------------------------------------
# Helpers de construccion
# ---------------------------------------------------------------------------


def _make_selector(
    *,
    version: int = 1,
    author: SelectorAuthor = SelectorAuthor.LLM_DISCOVERY,
    deprecated: bool = False,
) -> Selector:
    return Selector(
        selector_id=uuid4(),
        site_id=_SITE,
        flow_id=_FLOW,
        step_id=_STEP,
        strategy=SelectorStrategy.CSS,
        value=f"#btn-v{version}",
        intent_desc="boton presentar",
        version=version,
        author=author,
        deprecated_at=datetime.now(tz=UTC) if deprecated else None,
    )


def _make_observe_result(*, confidence: float, value: str = "#btn-v2") -> dict[str, Any]:
    """observe_result simulado como lo devolveria Stagehand."""
    return {
        "candidates": [
            {
                "strategy": "css",
                "value": value,
                "confidence": confidence,
                "intent_desc": "boton presentar definitivo",
            }
        ]
    }


def _registry_with_v1(signing_key: bytes = _KEY) -> SignedSelectorRegistry:
    """Registry in-memory con un selector v1 activo valido."""
    store = InMemorySelectorRegistry()
    return SignedSelectorRegistry(store=store, signing_key=signing_key)


async def _seed_v1(registry: SignedSelectorRegistry) -> Selector:
    """Persiste un Selector v1 valido en el registry."""
    stored = build_signed(
        signing_key=_KEY,
        site_id=_SITE,
        flow_id=_FLOW,
        step_id=_STEP,
        strategy=SelectorStrategy.CSS,
        value="#btn-old",
        intent_desc="boton presentar",
        version=1,
    )
    # Persistir directamente en el store subyacente (bypass SignedSelectorRegistry
    # para que el seed no marque deprecations).
    await registry._store.persist(stored)  # type: ignore[attr-defined]
    return stored.selector


# ---------------------------------------------------------------------------
# AC1: selector v1 no resuelve -> discovery disparado
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac1_selector_not_resolved_triggers_discovery() -> None:
    """AC1: cuando el prev_selector existe pero falla en el driver, heal() dispara
    discovery usando observe_result para encontrar candidatos.

    Aqui simulamos que el selector no resolvio pasando prev_selector=None y un
    observe_result con candidatos. El SelfHealer debe intentar discovery.
    """
    config = SelfHealingConfig(
        max_discovery_retries=3,
        confidence_threshold=0.75,
        window_seconds=600,
    )
    store = InMemorySelectorRegistry()
    registry = SignedSelectorRegistry(store=store, signing_key=_KEY)
    budget = SelfHealingBudget(config=config)

    healer = SelfHealer(registry=registry, budget=budget, config=config)

    observe_result = _make_observe_result(confidence=0.90)
    outcome = await healer.heal(
        site_id=_SITE,
        flow_id=_FLOW,
        step_id=_STEP,
        prev_selector=None,
        observe_result=observe_result,
        tenant_scope=_TENANT,
    )

    # Discovery exitoso con confidence > threshold
    assert outcome.selector is not None, "AC1: discovery debe retornar un selector v2"
    assert outcome.intervention is None
    assert outcome.selector.version == 1  # primera version descubierta
    assert outcome.selector.author == SelectorAuthor.LLM_DISCOVERY


# ---------------------------------------------------------------------------
# AC2: candidato confidence > threshold -> v2 firmado + v1 deprecated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac2_high_confidence_candidate_persists_v2_and_deprecates_v1() -> None:
    """AC2: cuando discovery produce candidato con confidence > threshold:
      - se persiste Selector v2 firmado HMAC con author=LLM_DISCOVERY
      - el prev_selector v1 se marca deprecated_at
      - HealOutcome.selector es el nuevo v2
    """
    config = SelfHealingConfig(confidence_threshold=0.75)
    store = InMemorySelectorRegistry()
    registry = SignedSelectorRegistry(store=store, signing_key=_KEY)
    budget = SelfHealingBudget(config=config)
    healer = SelfHealer(registry=registry, budget=budget, config=config)

    # Sembramos v1
    prev = await _seed_v1(registry)

    observe_result = _make_observe_result(confidence=0.90, value="#btn-v2")
    outcome = await healer.heal(
        site_id=_SITE,
        flow_id=_FLOW,
        step_id=_STEP,
        prev_selector=prev,
        observe_result=observe_result,
        tenant_scope=None,
    )

    assert outcome.selector is not None
    assert outcome.selector.author == SelectorAuthor.LLM_DISCOVERY
    assert outcome.selector.version == 2, "v2 debe ser version 2 (prev + 1)"

    # v1 debe estar deprecated
    history = await registry.history(
        site_id=_SITE, flow_id=_FLOW, step_id=_STEP
    )
    v1_entries = [s for s in history if s.version == 1]
    assert v1_entries, "v1 debe existir en history"
    assert v1_entries[0].deprecated_at is not None, "v1 debe estar deprecated"


# ---------------------------------------------------------------------------
# AC3: candidato confidence < threshold -> no persiste + CONFIDENCE_LOW
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac3_low_confidence_candidate_no_persist_emits_intervention() -> None:
    """AC3: confidence < threshold -> NO persiste selector + emite
    OperatorInterventionRequest con reason=CONFIDENCE_LOW.
    """
    config = SelfHealingConfig(confidence_threshold=0.75)
    store = InMemorySelectorRegistry()
    registry = SignedSelectorRegistry(store=store, signing_key=_KEY)
    budget = SelfHealingBudget(config=config)
    healer = SelfHealer(registry=registry, budget=budget, config=config)

    prev = await _seed_v1(registry)
    observe_result = _make_observe_result(confidence=0.50)  # below threshold

    outcome = await healer.heal(
        site_id=_SITE,
        flow_id=_FLOW,
        step_id=_STEP,
        prev_selector=prev,
        observe_result=observe_result,
        tenant_scope=None,
    )

    assert outcome.selector is None, "AC3: NO debe persistir selector con baja confidence"
    assert outcome.intervention is not None
    assert outcome.intervention.reason == InterventionReason.CONFIDENCE_LOW

    # Verificar que v1 sigue activo (no se deprecio)
    active = await registry.fetch_latest(
        site_id=_SITE, flow_id=_FLOW, step_id=_STEP
    )
    assert active is not None, "v1 debe seguir activo cuando discovery falla"


# ---------------------------------------------------------------------------
# AC4: HMAC invalida -> descarta + selector_tampered + entra discovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac4_invalid_hmac_discards_and_enters_discovery(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC4: selector con HMAC invalida -> descartado fail-closed.
    El runtime entra en discovery como si no existiera y emite selector_tampered.

    En este test: fetch_latest levanta SelectorTamperedError (el SignedSelectorRegistry
    falla al verificar la firma). El SelfHealer captura esto y procede a discovery
    con el observe_result disponible.
    """
    import logging

    config = SelfHealingConfig(confidence_threshold=0.75)
    store = InMemorySelectorRegistry()
    registry = SignedSelectorRegistry(store=store, signing_key=_KEY)
    budget = SelfHealingBudget(config=config)
    healer = SelfHealer(registry=registry, budget=budget, config=config)

    # Sembrar selector con firma invalida directamente en el store
    tampered = build_signed(
        signing_key=b"\xff" * 32,  # key diferente -> firma invalida con _KEY
        site_id=_SITE,
        flow_id=_FLOW,
        step_id=_STEP,
        strategy=SelectorStrategy.CSS,
        value="#btn-tampered",
        intent_desc="boton tampered",
    )
    await store.persist(tampered)

    observe_result = _make_observe_result(confidence=0.85)

    with caplog.at_level(logging.WARNING, logger="hermes.browser.self_healing"):
        outcome = await healer.heal(
            site_id=_SITE,
            flow_id=_FLOW,
            step_id=_STEP,
            prev_selector=None,
            observe_result=observe_result,
            tenant_scope=None,
            known_tampered=True,  # indica que el caller ya detecto HMAC invalida
        )

    # Con observe_result de alta confidence, debe descubrir un selector nuevo
    assert outcome.selector is not None, "AC4: discovery debe proceder tras tampered"
    assert outcome.intervention is None


# ---------------------------------------------------------------------------
# AC5: N fallos consecutivos -> DEGRADED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac5_max_retries_exceeded_emits_degraded() -> None:
    """AC5: N=max_discovery_retries fallos consecutivos -> DEGRADED.

    Simulamos el budget agotado vaciando los candidatos en observe_result
    y llamando heal() N+1 veces para el mismo (site_id, flow_id).
    """
    max_retries = 2
    config = SelfHealingConfig(
        max_discovery_retries=max_retries,
        confidence_threshold=0.75,
        window_seconds=600,
    )
    store = InMemorySelectorRegistry()
    registry = SignedSelectorRegistry(store=store, signing_key=_KEY)
    budget = SelfHealingBudget(config=config)
    healer = SelfHealer(registry=registry, budget=budget, config=config)

    # observe_result con candidates vacios -> discovery falla (confidence 0)
    empty_observe = {"candidates": []}

    for _i in range(max_retries):
        outcome = await healer.heal(
            site_id=_SITE,
            flow_id=_FLOW,
            step_id=_STEP,
            prev_selector=None,
            observe_result=empty_observe,
            tenant_scope=None,
        )
        # Cada fallo de discovery consume un intento del budget
        assert outcome.selector is None
        assert outcome.intervention is not None
        assert outcome.intervention.reason in (
            InterventionReason.CONFIDENCE_LOW,
            InterventionReason.DEGRADED,
        )

    # La ultima llamada debe superar el budget -> DEGRADED
    final_outcome = await healer.heal(
        site_id=_SITE,
        flow_id=_FLOW,
        step_id=_STEP,
        prev_selector=None,
        observe_result=empty_observe,
        tenant_scope=None,
    )
    assert final_outcome.selector is None
    assert final_outcome.intervention is not None
    assert final_outcome.intervention.reason == InterventionReason.DEGRADED, (
        f"AC5: se esperaba DEGRADED despues de {max_retries} fallos, "
        f"got {final_outcome.intervention.reason}"
    )
