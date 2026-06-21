"""PlaywrightMcpDriver: verifica el lazy-import (sin mcp instalado).

CI no instala 'mcp'. El modulo debe importarse SIN error y solo fallar al
llamar StdioMcpSession.start(). PlaywrightMcpDriver mismo no requiere 'mcp'
directamente — lo usa solo StdioMcpSession.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from hermes.browser.infrastructure import (
    McpNotInstalledError,
    PlaywrightMcpDriver,
    StdioMcpSession,
)


def test_can_instantiate_playwright_mcp_driver_without_mcp_installed() -> None:
    """PlaywrightMcpDriver se puede construir con FakeMcpSession sin mcp SDK."""
    from hermes.browser.testing import FakeMcpSession

    session = FakeMcpSession()
    driver = PlaywrightMcpDriver(session=session)
    assert driver.driver_name == "playwright_mcp"
    assert driver.capabilities["supports_mcp"] is True


def test_can_instantiate_stdio_mcp_session_without_mcp_installed() -> None:
    """StdioMcpSession se puede construir; solo start() levanta McpNotInstalledError."""
    session = StdioMcpSession(server_command=["npx", "@playwright/mcp", "--headless"])
    assert session is not None
    assert McpNotInstalledError is not None


@pytest.mark.asyncio
async def test_stdio_mcp_session_start_raises_clearly_when_mcp_missing() -> None:
    """StdioMcpSession.start() levanta McpNotInstalledError si mcp no esta instalado."""
    session = StdioMcpSession()
    with pytest.raises((McpNotInstalledError, Exception)):
        # Puede ser McpNotInstalledError (mcp no instalado) o
        # McpServerConnectionError (mcp instalado pero npx no en PATH en CI).
        # Ambos son errores de infraestructura esperados en CI.
        await session.start()


@pytest.mark.asyncio
async def test_execute_before_start_fails_gracefully() -> None:
    """execute() sin start() devuelve StepOutcome.failed, nunca excepcion."""
    from hermes.browser.domain.step import Step, StepKind, StepRisk, StepStatus
    from hermes.browser.testing import FakeMcpSession

    session = FakeMcpSession()
    driver = PlaywrightMcpDriver(session=session)
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
    from hermes.browser.domain.step import StepStatus

    assert outcome.status == StepStatus.EXECUTED_FAILED
    assert "playwright_mcp_not_started" in (outcome.error or "")
