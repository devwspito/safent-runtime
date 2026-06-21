"""Stagehand driver: verifica el lazy-import (sin stagehand instalado).

CI no instala `stagehand-py`. El modulo debe importarse SIN error y solo
fallar al llamar `.start()`. Asi cualquier consumer puede importar el
adapter aunque no use el driver real.
"""
from __future__ import annotations

import pytest

from hermes.browser.infrastructure import (
    StagehandDriver,
    StagehandNotInstalledError,
)


def test_can_instantiate_without_stagehand_installed() -> None:
    # Construir el driver NO levanta error — solo start() lo hace.
    driver = StagehandDriver(model_name="anthropic/claude-3-5-haiku-20241022")
    assert driver.driver_name == "stagehand"
    caps = driver.capabilities
    assert caps["stagehand_model"] == "anthropic/claude-3-5-haiku-20241022"
    assert caps["supports_action_caching"] is True


@pytest.mark.asyncio
async def test_start_raises_clearly_when_stagehand_missing() -> None:
    driver = StagehandDriver(model_name="anthropic/claude-3-5-haiku-20241022")
    with pytest.raises(StagehandNotInstalledError, match=r"hermes-runtime\[browser\]"):
        await driver.start()


@pytest.mark.asyncio
async def test_execute_before_start_returns_failed_outcome() -> None:
    from uuid import uuid4

    from hermes.browser import Step, StepKind, StepRisk, StepStatus

    driver = StagehandDriver(model_name="anthropic/claude-3-5-haiku-20241022")
    step = Step.new(
        tenant_id=uuid4(),
        session_id=uuid4(),
        kind=StepKind.NAVIGATE,
        risk=StepRisk.LOW,
        intent_desc="x",
        payload={"url": "https://example.com"},
    )
    outcome = await driver.execute(step)
    assert outcome.status == StepStatus.EXECUTED_FAILED
    assert "stagehand_not_started" in (outcome.error or "")
