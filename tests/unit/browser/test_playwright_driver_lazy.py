"""Test del lazy-import del PlaywrightDriver.

Si Playwright NO esta instalado: el modulo importa OK, solo start() falla.
Si SI esta instalado: start() abre Chromium (este test no lo hace para no
depender del binario; ese caso vive en tests E2E con marker requires_chromium).
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from hermes.browser import Step, StepKind, StepRisk, StepStatus
from hermes.browser.infrastructure import PlaywrightDriver


def test_can_instantiate_without_starting() -> None:
    driver = PlaywrightDriver(headless=True)
    assert driver.driver_name == "playwright"
    assert driver.capabilities["playwright_headless"] is True
    assert driver.capabilities["supports_cert_client"] is False


def test_capabilities_reflect_cert_client_path() -> None:
    driver = PlaywrightDriver(cert_pem_path="/tmp/fake-cert.pem")
    assert driver.capabilities["supports_cert_client"] is True


@pytest.mark.asyncio
async def test_execute_before_start_fails_gracefully() -> None:
    driver = PlaywrightDriver()
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
    assert "playwright_not_started" in (outcome.error or "")
