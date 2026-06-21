"""Tests del BrowserSession: HITL gate + anti-bot delay + step recorder."""
from __future__ import annotations

from uuid import UUID

import pytest

from hermes.browser import BrowserSession, BrowserSessionConfig, StepRisk, StepStatus
from hermes.browser.application import HitlApprovalRequired
from hermes.browser.application.step_recorder import (
    InMemoryArtifactStore,
    InMemoryRecordSink,
    StepRecorder,
)
from hermes.browser.testing import FakeBrowserDriver, scripted_step

_TENANT = UUID("00000000-0000-0000-0000-0000000000aa")


def _make_config(**overrides: object) -> BrowserSessionConfig:
    base: dict[str, object] = {
        "tenant_id": _TENANT,
        "site_id": "aeat_sede",
        "flow_id": "modelo_303_borrador",
        "anti_bot_min_delay_ms": 1,  # tests rapidos
        "anti_bot_max_delay_ms": 5,
        "anti_bot_mean_delay_ms": 2,
    }
    base.update(overrides)
    return BrowserSessionConfig(**base)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_session_navigate_uses_driver() -> None:
    driver = FakeBrowserDriver()
    async with BrowserSession.open(config=_make_config(), driver=driver) as session:
        outcome = await session.navigate("https://prewww10.aeat.es/")
    assert outcome.status == StepStatus.EXECUTED_OK
    assert len(driver.executed_steps) == 1
    assert driver.executed_steps[0].kind.value == "navigate"


@pytest.mark.asyncio
async def test_session_act_low_no_hitl_required() -> None:
    driver = FakeBrowserDriver()
    async with BrowserSession.open(config=_make_config(), driver=driver) as session:
        outcome = await session.act("scroll down", risk=StepRisk.LOW)
    assert outcome.status == StepStatus.EXECUTED_OK


@pytest.mark.asyncio
async def test_session_act_high_without_token_raises() -> None:
    driver = FakeBrowserDriver()
    async with BrowserSession.open(config=_make_config(), driver=driver) as session:
        with pytest.raises(HitlApprovalRequired):
            await session.act("submit definitivo", risk=StepRisk.HIGH)


@pytest.mark.asyncio
async def test_session_act_high_with_token_executes() -> None:
    driver = FakeBrowserDriver()
    async with BrowserSession.open(config=_make_config(), driver=driver) as session:
        outcome = await session.act(
            "submit definitivo",
            risk=StepRisk.HIGH,
            hitl_approval_token="hmac:approval:abc123",
        )
    assert outcome.status == StepStatus.EXECUTED_OK
    assert driver.executed_with_token[-1] == "hmac:approval:abc123"


@pytest.mark.asyncio
async def test_session_medium_gated_only_when_configured() -> None:
    driver = FakeBrowserDriver()
    # default = MEDIUM no gated
    async with BrowserSession.open(
        config=_make_config(require_hitl_for_medium=False), driver=driver
    ) as session:
        outcome = await session.act("fill cuota", risk=StepRisk.MEDIUM)
    assert outcome.status == StepStatus.EXECUTED_OK

    # con flag MEDIUM si requiere
    driver2 = FakeBrowserDriver()
    async with BrowserSession.open(
        config=_make_config(require_hitl_for_medium=True), driver=driver2
    ) as session:
        with pytest.raises(HitlApprovalRequired):
            await session.act("fill cuota", risk=StepRisk.MEDIUM)


@pytest.mark.asyncio
async def test_session_extract_returns_result_dict() -> None:
    driver = FakeBrowserDriver(
        scripted=(
            scripted_step(
                matches_kind="extract",
                result={"saldo_libros": 4812.33},
            ),
        )
    )
    async with BrowserSession.open(config=_make_config(), driver=driver) as session:
        outcome = await session.extract(
            instruction="lee el saldo de libros",
            schema={"type": "object", "properties": {"saldo_libros": {"type": "number"}}},
        )
    assert outcome.status == StepStatus.EXECUTED_OK
    assert outcome.result["saldo_libros"] == 4812.33


@pytest.mark.asyncio
async def test_session_closes_driver_on_exit() -> None:
    driver = FakeBrowserDriver()
    async with BrowserSession.open(config=_make_config(), driver=driver):
        pass
    assert driver.closed is True


@pytest.mark.asyncio
async def test_session_records_step_with_recorder() -> None:
    driver = FakeBrowserDriver()
    artifacts = InMemoryArtifactStore()
    sink = InMemoryRecordSink()
    recorder = StepRecorder(artifact_store=artifacts, sink=sink)

    async with BrowserSession.open(
        config=_make_config(), driver=driver, recorder=recorder
    ) as session:
        await session.navigate("https://prewww10.aeat.es/")

    assert len(sink.records) == 1
    record = sink.records[0]
    assert record.step.kind.value == "navigate"
    assert record.outcome.status == StepStatus.EXECUTED_OK
    # pre + post screenshot + dom
    assert len(record.screenshots) == 2
    assert len(record.dom_snapshots) == 2
    # artifacts persistidos
    assert len(artifacts.screenshots) == 2
    assert len(artifacts.dom_snapshots) == 2


@pytest.mark.asyncio
async def test_session_propagates_driver_failure_as_outcome() -> None:
    driver = FakeBrowserDriver(
        scripted=(scripted_step(matches_kind="act", ok=False, error="selector_not_found"),)
    )
    async with BrowserSession.open(config=_make_config(), driver=driver) as session:
        outcome = await session.act("click submit", risk=StepRisk.LOW)
    assert outcome.status == StepStatus.EXECUTED_FAILED
    assert outcome.error == "selector_not_found"


@pytest.mark.asyncio
async def test_session_observe_returns_candidates() -> None:
    driver = FakeBrowserDriver(
        scripted=(
            scripted_step(
                matches_kind="observe",
                result={"candidates": [{"selector": "#btn-1"}, {"selector": "#btn-2"}]},
            ),
        )
    )
    async with BrowserSession.open(config=_make_config(), driver=driver) as session:
        outcome = await session.observe("encuentra el boton de presentar")
    assert outcome.status == StepStatus.EXECUTED_OK
    assert len(outcome.result["candidates"]) == 2
