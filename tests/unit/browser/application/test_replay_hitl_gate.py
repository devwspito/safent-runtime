"""T205 — HITL gate test en replay con step HIGH sin token.

Spec ejecutable: estos tests definen el contrato que replay_runner DEBE
cumplir cuando llegue T506. Todos están marcados con pytest.mark.skip
apuntando a T506.

Constitution II: el replay no elude HITL para HIGH — este es el test
que lo verifica. Sin él, una regresión en el runner pasa silenciosa.
Threat-model control P1 #3 / E1 superficie 3.
FR-009: HITL gate aplicado también en replay (todos los steps HIGH).
US3/AC4: replay reusa BrowserSession._execute → HITL gate heredado.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

# Activación automática cuando T506 arrive: si el módulo existe, los tests
# se activan automáticamente. Si no existe, pytest.importorskip los skippea
# con el mismo mensaje que el skip explícito abajo.
replay_runner = pytest.importorskip(
    "hermes.browser.application.replay_runner",
    reason="T506 — replay_runner real pendiente. "
    "Estos tests se activan automáticamente cuando T506 implemente el módulo.",
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_high_risk_script() -> object:
    """Construye un ReplayScript con un step risk=HIGH para test de HITL."""
    from hermes.browser.domain.replay_script import (
        ReplayScript,
        ReplayStep,
        sign_replay_script,
    )

    step = ReplayStep(
        selector_id=str(uuid4()),
        selector_version=1,
        action="click",
        payload_template={"target": "btn_presentar_definitivo"},
        risk="high",
    )
    script = ReplayScript(
        script_id=uuid4(),
        site_id="aeat_sede",
        flow_id="modelo_303_definitivo",
        tenant_scope=uuid4(),
        runtime_version="0.1.0",
        steps=(step,),
    )
    return sign_replay_script(script, key=b"\x01" * 32)


def _make_medium_risk_script() -> object:
    from hermes.browser.domain.replay_script import (
        ReplayScript,
        ReplayStep,
        sign_replay_script,
    )

    step = ReplayStep(
        selector_id=str(uuid4()),
        selector_version=1,
        action="fill",
        payload_template={"value": "{{NIF_1}}"},
        risk="medium",
    )
    script = ReplayScript(
        script_id=uuid4(),
        site_id="aeat_sede",
        flow_id="modelo_303_borrador",
        tenant_scope=uuid4(),
        runtime_version="0.1.0",
        steps=(step,),
    )
    return sign_replay_script(script, key=b"\x01" * 32)


def _make_low_risk_script() -> object:
    from hermes.browser.domain.replay_script import (
        ReplayScript,
        ReplayStep,
        sign_replay_script,
    )

    step = ReplayStep(
        selector_id=str(uuid4()),
        selector_version=1,
        action="navigate",
        payload_template={"url": "https://stub.local/home"},
        risk="low",
    )
    script = ReplayScript(
        script_id=uuid4(),
        site_id="stub_local",
        flow_id="consulta_estado",
        tenant_scope=None,
        runtime_version="0.1.0",
        steps=(step,),
    )
    return sign_replay_script(script, key=b"\x01" * 32)


# ---------------------------------------------------------------------------
# T205 test 1
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_high_risk_without_token_raises_hitl_approval_required() -> None:
    """ReplayScript con un step risk=HIGH sin hitl_approval_token levanta HitlApprovalRequired.

    Contract:
      - El runner NO debe tocar el driver antes de levantar.
      - La excepción debe levantarse en la capa de orquestación (replay_runner),
        no en el driver, porque replay_runner reutiliza BrowserSession._execute
        que ya tiene el gate (Constitution II / plan Complexity Tracking).
      - Verifica que FakeBrowserDriver.executed_steps permanece vacío.

    Threat-model control P1 #3 / E1 superficie 3.
    Constitution II: HITL gate inquebrantable, risk lo decide SiteSpec no el LLM.
    """
    from hermes.browser.application.session import HitlApprovalRequired
    from hermes.browser.testing.fakes import FakeBrowserDriver

    script = _make_high_risk_script()
    driver = FakeBrowserDriver()

    with pytest.raises(HitlApprovalRequired):
        await replay_runner.run(script, driver=driver, hitl_approval_token=None)

    assert len(driver.executed_steps) == 0, (
        "El driver NO debe haber ejecutado ningún step — "
        "HitlApprovalRequired debe levantarse antes de tocar el driver."
    )


# ---------------------------------------------------------------------------
# T205 test 2
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_medium_with_require_hitl_for_medium_raises() -> None:
    """ReplayScript con step risk=MEDIUM + require_hitl_for_medium=True levanta.

    El runner debe respetar la config de BrowserSessionConfig igual que
    BrowserSession._execute para MEDIUM risk.
    """
    from hermes.browser.application.session import HitlApprovalRequired
    from hermes.browser.testing.fakes import FakeBrowserDriver

    script = _make_medium_risk_script()
    driver = FakeBrowserDriver()

    with pytest.raises(HitlApprovalRequired):
        await replay_runner.run(
            script,
            driver=driver,
            hitl_approval_token=None,
            require_hitl_for_medium=True,
        )

    assert len(driver.executed_steps) == 0


# ---------------------------------------------------------------------------
# T205 test 3
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_low_risk_without_token_proceeds() -> None:
    """ReplayScript con step risk=LOW ejecuta sin token y sin levantar.

    LOW risk nunca atraviesa HITL gate (domain rule en BrowserSession._needs_hitl).
    """
    from hermes.browser.testing.fakes import FakeBrowserDriver

    script = _make_low_risk_script()
    driver = FakeBrowserDriver()

    # No debe levantar — LOW risk no necesita token.
    await replay_runner.run(script, driver=driver, hitl_approval_token=None)

    assert len(driver.executed_steps) >= 1, (
        "El driver DEBE haber ejecutado el step LOW — "
        "sin HITL gate para LOW risk."
    )
