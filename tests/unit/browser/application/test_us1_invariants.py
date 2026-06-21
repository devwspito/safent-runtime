"""T302 — US1 invariants: HITL gate, llm_not_configured, HTTP 500 mid-flow.

Constitution V: no Chromium, no network, deterministic.
Constitution II: HITL gate before any driver.execute.
Constitution IV: fail-closed defaults.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from hermes.browser.application.session import (
    BrowserSession,
    BrowserSessionConfig,
    HitlApprovalRequired,
)
from hermes.browser.application.step_recorder import (
    InMemoryArtifactStore,
    InMemoryRecordSink,
    StepRecorder,
)
from hermes.browser.domain.step import StepRisk, StepStatus
from hermes.browser.testing import FakeBrowserDriver

_TENANT = UUID("00000000-0000-0000-0000-000000000022")


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


# ---------------------------------------------------------------------------
# AC2: HIGH risk step without HITL token raises HitlApprovalRequired
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_high_risk_step_without_token_raises_before_driver() -> None:
    """AC2: HitlApprovalRequired raised before driver.execute for HIGH steps.

    Constitution II: the gate fires in BrowserSession._execute, not the driver.
    The driver's executed_steps list must remain empty.
    """
    driver = FakeBrowserDriver()

    async with BrowserSession.open(config=_make_config(), driver=driver) as session:
        with pytest.raises(HitlApprovalRequired):
            await session.act(
                "envia el formulario definitivo",
                risk=StepRisk.HIGH,
                hitl_approval_token=None,
            )

    assert len(driver.executed_steps) == 0, (
        "Driver must not have executed any step — HITL gate fires first"
    )


# ---------------------------------------------------------------------------
# AC3: HERMES_MODEL vacío + act → StepOutcome.failed(error="llm_not_configured")
# ---------------------------------------------------------------------------


class _LlmNotConfiguredDriver:
    """Fake driver that simulates StagehandDriver with no HERMES_MODEL.

    When act/extract/observe are called with an empty model, returns failed.
    """

    def __init__(self) -> None:
        self.executed_steps: list = []
        self.closed = False

    async def execute(self, step: object, *, hitl_approval_token: object = None) -> object:  # noqa: ARG002
        from hermes.browser.domain.step import Step, StepKind, StepOutcome

        assert isinstance(step, Step)
        self.executed_steps.append(step)
        if step.kind in (StepKind.ACT, StepKind.EXTRACT, StepKind.OBSERVE):
            return StepOutcome.failed(
                step_id=step.step_id,
                error="llm_not_configured",
            )
        return StepOutcome.ok(step_id=step.step_id, duration_ms=1)

    async def take_screenshot(self) -> bytes:
        return b""

    async def take_dom_snapshot(self) -> str:
        return ""

    async def current_url(self) -> str:
        return ""

    async def close(self) -> None:
        self.closed = True

    @property
    def driver_name(self) -> str:
        return "llm_not_configured_fake"

    @property
    def capabilities(self) -> dict:
        return {}


@pytest.mark.asyncio
async def test_llm_not_configured_returns_failed_outcome() -> None:
    """AC3: empty HERMES_MODEL → StepOutcome.failed('llm_not_configured'), no crash."""
    driver = _LlmNotConfiguredDriver()

    async with BrowserSession.open(config=_make_config(), driver=driver) as session:
        outcome = await session.act(
            "introduce el usuario testuser",
            risk=StepRisk.LOW,
        )

    assert outcome.status == StepStatus.EXECUTED_FAILED
    assert outcome.error == "llm_not_configured"
    # Process must not crash — session closed cleanly
    assert driver.closed is True


# ---------------------------------------------------------------------------
# AC4: HTTP 500 mid-flow → step failed, DOM captured, session closes cleanly
# ---------------------------------------------------------------------------


class _Http500DriverAfterN:
    """Driver that fails with a 500-like error on the nth execute call."""

    def __init__(self, *, fail_on: int = 1) -> None:
        self._fail_on = fail_on
        self._calls = 0
        self.executed_steps: list = []
        self.closed = False

    async def execute(self, step: object, *, hitl_approval_token: object = None) -> object:  # noqa: ARG002
        from hermes.browser.domain.step import Step, StepOutcome

        assert isinstance(step, Step)
        self._calls += 1
        self.executed_steps.append(step)
        if self._calls == self._fail_on:
            return StepOutcome.failed(
                step_id=step.step_id,
                error="http_500_internal_server_error",
            )
        return StepOutcome.ok(step_id=step.step_id, duration_ms=1)

    async def take_screenshot(self) -> bytes:
        return b"<png-bytes>"

    async def take_dom_snapshot(self) -> str:
        return "<html><body>Error 500</body></html>"

    async def current_url(self) -> str:
        return "http://stub.local/error"

    async def close(self) -> None:
        self.closed = True

    @property
    def driver_name(self) -> str:
        return "http_500_fake"

    @property
    def capabilities(self) -> dict:
        return {}


@pytest.mark.asyncio
async def test_http500_mid_flow_step_fails_session_closes() -> None:
    """AC4: 500 mid-flow → step EXECUTED_FAILED, session closes without exception bubbling."""
    driver = _Http500DriverAfterN(fail_on=2)
    artifacts = InMemoryArtifactStore()
    sink = InMemoryRecordSink()
    recorder = StepRecorder(artifact_store=artifacts, sink=sink)

    async with BrowserSession.open(
        config=_make_config(), driver=driver, recorder=recorder
    ) as session:
        o1 = await session.navigate("http://stub.local/login")
        o2 = await session.act("introduce el usuario", risk=StepRisk.LOW)

    assert o1.status == StepStatus.EXECUTED_OK
    assert o2.status == StepStatus.EXECUTED_FAILED
    assert o2.error == "http_500_internal_server_error"

    # Session released context cleanly
    assert driver.closed is True

    # Recorder captured the failed step
    failed_records = [r for r in sink.records if r.outcome.status == StepStatus.EXECUTED_FAILED]
    assert len(failed_records) >= 1


@pytest.mark.asyncio
async def test_http500_mid_flow_dom_captured_by_recorder() -> None:
    """AC4: Recorder captures pre-snapshot DOM even when step fails."""
    driver = _Http500DriverAfterN(fail_on=1)
    artifacts = InMemoryArtifactStore()
    sink = InMemoryRecordSink()
    recorder = StepRecorder(artifact_store=artifacts, sink=sink)

    async with BrowserSession.open(
        config=_make_config(capture_dom=True), driver=driver, recorder=recorder
    ) as session:
        outcome = await session.navigate("http://stub.local/error500")

    assert outcome.status == StepStatus.EXECUTED_FAILED

    # Pre-snapshot must have been captured regardless of failure
    assert len(sink.records) == 1
    record = sink.records[0]
    assert len(record.dom_snapshots) >= 1, "Pre-DOM must be captured even on failure"
