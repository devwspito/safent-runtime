"""AgentBrowserDriver: verifica el lazy-import (sin binario agent-browser).

CI no tiene el binario Rust `agent-browser`. El modulo debe importarse SIN
error y solo fallar al llamar AgentBrowserCli.start() si el binario falta.
AgentBrowserDriver mismo no verifica el binario en import — solo en start().
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from hermes.browser.infrastructure import (
    AgentBrowserCli,
    AgentBrowserDriver,
    AgentBrowserNotInstalledError,
)


def test_can_instantiate_agent_browser_driver_without_binary() -> None:
    """AgentBrowserDriver se puede construir con FakeAgentBrowserCli sin binario."""
    from hermes.browser.testing import FakeAgentBrowserCli

    cli = FakeAgentBrowserCli()
    driver = AgentBrowserDriver(cli=cli)
    assert driver.driver_name == "agent_browser"
    assert driver.capabilities["experimental"] is True
    assert driver.capabilities["token_efficient_snapshots"] is True


def test_can_instantiate_agent_browser_cli_without_binary() -> None:
    """AgentBrowserCli se puede construir; solo start() levanta error si binario falta."""
    cli = AgentBrowserCli(session_name="test-session")
    assert cli is not None
    assert AgentBrowserNotInstalledError is not None


@pytest.mark.asyncio
async def test_agent_browser_cli_start_raises_clearly_when_binary_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AgentBrowserCli.start() levanta AgentBrowserNotInstalledError si falta el binario."""
    import shutil

    # Simula que el binario no esta en PATH
    monkeypatch.setattr(shutil, "which", lambda _: None)

    cli = AgentBrowserCli(session_name="test-no-binary")
    with pytest.raises(AgentBrowserNotInstalledError, match=r"npm install -g agent-browser"):
        await cli.start()


@pytest.mark.asyncio
async def test_execute_before_start_fails_gracefully() -> None:
    """execute() sin start() devuelve StepOutcome.failed, nunca excepcion."""
    from hermes.browser.domain.step import Step, StepKind, StepRisk, StepStatus
    from hermes.browser.testing import FakeAgentBrowserCli

    cli = FakeAgentBrowserCli()
    driver = AgentBrowserDriver(cli=cli)
    # NO llamamos start()

    step = Step.new(
        tenant_id=uuid4(),
        session_id=uuid4(),
        kind=StepKind.NAVIGATE,
        risk=StepRisk.LOW,
        intent_desc="test",
        payload={"url": "https://example.com"},
    )
    outcome = await driver.execute(step)

    assert outcome.status == StepStatus.EXECUTED_FAILED
    assert "agent_browser_not_started" in (outcome.error or "")
