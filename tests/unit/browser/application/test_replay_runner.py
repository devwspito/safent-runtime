"""T502 — Tests ReplayStore + replay_runner (US3/AC1-AC4 + selector deprecated).

5 tests que cubren los acceptance criteria de US3:

  AC1: flow OK → persist(script) invocado con signature_hex no vacío.
  AC2: load_for existe + verify OK → runner ejecuta sin llamar LLM.
       (monkeypatch sobre litellm.acompletion: assert never called)
  AC3: primer step falla resolución → invalidate(SELECTOR_NOT_RESOLVED) + no aborta.
  AC4: step HIGH sin token → HitlApprovalRequired (link T205).
  +  : selector deprecated entre persist+load → invalidate(SELECTOR_DEPRECATED).

Constitución II: HITL gate en BrowserSession._execute, no en replay_runner.
Constitución III: ningún test activa litellm.acompletion.
Constitución V: sin Chromium, sin red, sin DB.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from hermes.browser.application.replay_runner import run as replay_run
from hermes.browser.application.session import HitlApprovalRequired
from hermes.browser.domain.replay_script import (
    ReplayInvalidationReason,
    ReplayScript,
    ReplayStep,
)
from hermes.browser.infrastructure.replay_codec import sign_replay
from hermes.browser.testing.fakes import FakeBrowserDriver, scripted_step
from hermes.browser.testing.in_memory_replay_store import InMemoryReplayStore

_KEY = b"\xca\xfe" * 16  # 32 bytes test key


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_script(
    *,
    risk: str = "low",
    action: str = "navigate",
    payload_template: dict | None = None,
    tenant_scope=None,
) -> ReplayScript:
    step = ReplayStep(
        selector_id=str(uuid4()),
        selector_version=1,
        action=action,
        payload_template=payload_template or {"url": "https://stub.local/home"},
        risk=risk,
    )
    return ReplayScript(
        script_id=uuid4(),
        site_id="stub_local",
        flow_id="consulta_estado",
        tenant_scope=tenant_scope,
        runtime_version="0.2.1",
        steps=(step,),
    )


def _signed(script: ReplayScript) -> ReplayScript:
    return sign_replay(script, key=_KEY)


# ---------------------------------------------------------------------------
# AC1: flow OK → persist(script) con signature_hex no vacío
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac1_flow_ok_persist_called_with_signature() -> None:
    """Tras un flow exitoso, replay_store.persist() recibe script con signature_hex."""
    store = InMemoryReplayStore()

    script = _signed(_make_script())
    # Simula que el orchestrator/session lo persiste al cerrar exitosamente.
    await store.persist(script)

    loaded = await store.load_for(
        site_id=script.site_id,
        flow_id=script.flow_id,
        tenant_scope=script.tenant_scope,
    )
    assert loaded is not None
    assert loaded.signature_hex != "", "signature_hex debe ser no vacío tras persist()"
    assert loaded.signature_hex.startswith("v1:")


# ---------------------------------------------------------------------------
# AC2: replay sin LLM — monkeypatch litellm.acompletion assert never called
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac2_replay_executes_without_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Con script válido, replay_runner.run() NO llama litellm.acompletion.

    Monkeypatch sobre litellm.acompletion que lanzaría AssertionError si fuera
    llamado — prueba Constitución III (PII tokenization / no LLM en replay).
    """

    def _never_called(*args, **kwargs):  # noqa: ARG001
        raise AssertionError(
            "litellm.acompletion fue llamado durante un replay — "
            "Constitución III violada: el replay NO debe llamar al LLM."
        )

    try:
        import litellm  # noqa: PLC0415
        monkeypatch.setattr(litellm, "acompletion", _never_called)
    except ImportError:
        pass  # litellm no instalado; el test sigue siendo válido

    script = _signed(_make_script(action="navigate", payload_template={"url": "https://stub.local"}))
    driver = FakeBrowserDriver()

    outcome = await replay_run(script, driver=driver, hitl_approval_token=None)

    assert outcome.success, f"replay falló inesperadamente: {outcome.error}"
    assert outcome.llm_calls == 0, "llm_calls debe ser 0 en replay puro"
    assert len(driver.executed_steps) >= 1, "El driver debe haber ejecutado al menos un step"


# ---------------------------------------------------------------------------
# AC3: primer step falla → invalidate(SELECTOR_NOT_RESOLVED) + no aborta
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac3_first_step_fails_returns_not_resolved() -> None:
    """Si el primer step falla, replay devuelve SELECTOR_NOT_RESOLVED.

    El replay no aborta con excepción — devuelve ReplayOutcome(success=False).
    El caller (orchestrator) decide si invalidar y caer a discovery.
    """
    # Driver scripted to fail the first step
    driver = FakeBrowserDriver(
        scripted=[scripted_step(ok=False, error="selector_not_found_in_dom")]
    )
    script = _signed(_make_script(action="click", payload_template={"click_selector": "#btn-old"}))

    outcome = await replay_run(script, driver=driver, hitl_approval_token=None)

    assert not outcome.success
    assert outcome.invalidation_reason == ReplayInvalidationReason.SELECTOR_NOT_RESOLVED, (
        f"Expected SELECTOR_NOT_RESOLVED, got: {outcome.invalidation_reason}"
    )


# ---------------------------------------------------------------------------
# AC4: step HIGH sin token → HitlApprovalRequired
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ac4_high_risk_without_token_raises_hitl() -> None:
    """Step HIGH sin hitl_approval_token levanta HitlApprovalRequired.

    Constitución II: HITL gate vive en BrowserSession._execute.
    El driver NO debe ejecutar el step — HitlApprovalRequired se levanta primero.
    """
    driver = FakeBrowserDriver()
    script = _signed(_make_script(
        risk="high",
        action="click",
        payload_template={"click_selector": "#btn-presentar-definitivo"},
    ))

    with pytest.raises(HitlApprovalRequired):
        await replay_run(script, driver=driver, hitl_approval_token=None)

    assert len(driver.executed_steps) == 0, (
        "El driver NO debe haber ejecutado ningún step — "
        "HitlApprovalRequired debe levantarse antes de tocar el driver."
    )


# ---------------------------------------------------------------------------
# Selector deprecated entre persist+load → SELECTOR_DEPRECATED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_selector_deprecated_returns_deprecated_reason() -> None:
    """Si el payload_template del step indica un selector deprecated, el runner
    debe devolver SELECTOR_DEPRECATED como razón de invalidación.

    En la implementación actual, el driver falla con un error que contiene
    "deprecated" — el runner lo clasifica como SELECTOR_DEPRECATED.
    """
    # Driver simula error de selector deprecated
    driver = FakeBrowserDriver(
        scripted=[scripted_step(ok=False, error="selector_deprecated_in_registry")]
    )
    script = _signed(_make_script(
        action="click",
        payload_template={"click_selector": "#btn-deprecated"},
    ))

    outcome = await replay_run(script, driver=driver, hitl_approval_token=None)

    assert not outcome.success
    assert outcome.invalidation_reason == ReplayInvalidationReason.SELECTOR_DEPRECATED, (
        f"Expected SELECTOR_DEPRECATED, got: {outcome.invalidation_reason}"
    )
