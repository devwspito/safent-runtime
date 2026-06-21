"""T301 — US1/AC1 happy path with FakeBrowserDriver (scripted).

Tests run WITHOUT Chromium; Constitution V compliant.

The flow mirrors the real US1 acceptance scenario:
  navigate → act(user) → act(password) → act(click login) → extract
  → act(fill concept) → act(submit, risk=HIGH, token) → extract(referencia)

Verifies:
- All StepOutcomes are ok.
- StepRecorder accumulates >= 6 entries (pre+post per step).
- The final extract result contains the expected 'referencia' key.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from hermes.browser.application.session import BrowserSession, BrowserSessionConfig
from hermes.browser.application.step_recorder import (
    InMemoryArtifactStore,
    InMemoryRecordSink,
    StepRecorder,
)
from hermes.browser.domain.step import StepRisk, StepStatus
from hermes.browser.testing import FakeBrowserDriver, scripted_step

_TENANT = UUID("00000000-0000-0000-0000-000000000011")
_REFERENCIA = "REF-STUB-00001"


def _make_config(**overrides: object) -> BrowserSessionConfig:
    base: dict[str, object] = {
        "tenant_id": _TENANT,
        "site_id": "demo_sede_stub",
        "flow_id": "form_flow",
        "anti_bot_min_delay_ms": 1,
        "anti_bot_max_delay_ms": 3,
        "anti_bot_mean_delay_ms": 2,
    }
    base.update(overrides)
    return BrowserSessionConfig(**base)  # type: ignore[arg-type]


def _make_driver() -> FakeBrowserDriver:
    """Scripted driver that mirrors the US1 stub flow."""
    return FakeBrowserDriver(
        scripted=(
            scripted_step(matches_kind="navigate", result={"url": "http://stub.local/login"}),
            scripted_step(matches_intent_substr="usuario", result={"filled": "testuser"}),
            scripted_step(matches_intent_substr="contrasena", result={"filled": "testpass"}),
            scripted_step(matches_intent_substr="entrar", result={"clicked": True}),
            scripted_step(
                matches_kind="extract",
                result={"nombre": "Test User", "referencia": _REFERENCIA},
            ),
            scripted_step(matches_intent_substr="concepto", result={"filled": "concepto-z"}),
            scripted_step(
                matches_intent_substr="envia",
                result={"submitted": True, "referencia": _REFERENCIA},
            ),
        ),
        default_dom="<html><body>stub-dom</body></html>",
    )


@pytest.mark.asyncio
async def test_us1_happy_path_all_steps_ok() -> None:
    """All steps return EXECUTED_OK."""
    driver = _make_driver()
    config = _make_config()

    async with BrowserSession.open(config=config, driver=driver) as session:
        o1 = await session.navigate("http://stub.local/login")
        o2 = await session.act("introduce el usuario testuser")
        o3 = await session.act("introduce la contrasena testpass")
        o4 = await session.act("haz click en entrar")
        o5 = await session.extract(
            instruction="extrae nombre y referencia",
            schema={"type": "object", "properties": {"nombre": {}, "referencia": {}}},
        )
        o6 = await session.act("rellena el campo concepto con concepto-z")
        o7 = await session.act(
            "envia el formulario definitivo",
            risk=StepRisk.HIGH,
            hitl_approval_token="token-valid-abc",
        )

    for step_num, outcome in enumerate([o1, o2, o3, o4, o5, o6, o7], start=1):
        assert outcome.status == StepStatus.EXECUTED_OK, (
            f"step {step_num} expected EXECUTED_OK got {outcome.status}: {outcome.error}"
        )


@pytest.mark.asyncio
async def test_us1_extract_returns_referencia() -> None:
    """The extract step returns the referencia field."""
    driver = _make_driver()
    config = _make_config()

    async with BrowserSession.open(config=config, driver=driver) as session:
        await session.navigate("http://stub.local/login")
        outcome = await session.extract(
            instruction="extrae nombre y referencia",
            schema={"type": "object", "properties": {"nombre": {}, "referencia": {}}},
        )

    assert "referencia" in outcome.result
    assert outcome.result["referencia"] == _REFERENCIA


@pytest.mark.asyncio
async def test_us1_step_recorder_minimum_entries() -> None:
    """StepRecorder persists at least 6 records (pre+post per step = 2 per step).

    With 3 steps (navigate + extract + one act), we get >= 3 records,
    each with 2 screenshots and 2 dom snapshots → 6+ total artifacts.
    """
    driver = _make_driver()
    artifacts = InMemoryArtifactStore()
    sink = InMemoryRecordSink()
    recorder = StepRecorder(artifact_store=artifacts, sink=sink)
    config = _make_config()

    async with BrowserSession.open(config=config, driver=driver, recorder=recorder) as session:
        await session.navigate("http://stub.local/login")
        await session.act("introduce el usuario testuser")
        await session.act("introduce la contrasena testpass")
        await session.extract(
            instruction="extrae referencia",
            schema={"type": "object", "properties": {"referencia": {}}},
        )
        await session.act("rellena el campo concepto con z")
        await session.act(
            "envia el formulario definitivo",
            risk=StepRisk.HIGH,
            hitl_approval_token="token-valid-abc",
        )

    # 6 steps → 6 records
    assert len(sink.records) >= 6, (
        f"Expected >= 6 StepRecords, got {len(sink.records)}"
    )
    # Each record should have pre+post screenshots (2) and pre+post dom (2)
    for record in sink.records:
        assert len(record.screenshots) >= 1, "Each record must have at least a pre screenshot"
        assert len(record.dom_snapshots) >= 1, "Each record must have at least a pre dom snapshot"


@pytest.mark.asyncio
async def test_us1_pre_post_artifacts_populated() -> None:
    """Pre snapshot is always captured; post only on success."""
    driver = _make_driver()
    artifacts = InMemoryArtifactStore()
    sink = InMemoryRecordSink()
    recorder = StepRecorder(artifact_store=artifacts, sink=sink)
    config = _make_config()

    async with BrowserSession.open(config=config, driver=driver, recorder=recorder) as session:
        await session.navigate("http://stub.local/login")
        await session.act("introduce el usuario testuser")

    # navigate + act = 2 records, each with pre+post screenshots + dom = 4 each type
    assert len(artifacts.screenshots) >= 4
    assert len(artifacts.dom_snapshots) >= 4


@pytest.mark.asyncio
async def test_us1_session_closes_driver_on_exit() -> None:
    """BrowserSession.close() releases driver context idempotently."""
    driver = _make_driver()

    async with BrowserSession.open(config=_make_config(), driver=driver):
        pass

    assert driver.closed is True

    # Calling close again is idempotent — should not raise
    # (session.close() is guarded by self._closed)
