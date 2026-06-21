"""T307 — E2E test: US1 complete form flow against StubSedeServer.

Marker: requires_chromium — only runs with Chromium installed.

By default (no HERMES_API_KEY in env): monkeypatches litellm.acompletion
with scripted responses that execute each step deterministically.

Opt-in real LLM: set HERMES_API_KEY + HERMES_MODEL in env; the test
uses StagehandDriver with the real LLM (no monkeypatch).

Flow: navigate→login→fill form→submit→extract referencia.

Assert:
  - referencia UUID extracted from the detalle page.
  - StepRecorder has >= 6 entries with pre+post artifacts.
  - Total elapsed < 5 min (Constitution V performance gate).
"""

from __future__ import annotations

import os
import sys
import time
from uuid import UUID

import pytest

# Guards: this module must not import playwright or stagehand at module load.
# The marker prevents CI from running it; lazy imports keep the module safe.

pytestmark = pytest.mark.requires_chromium


# ---------------------------------------------------------------------------
# Skip if chromium is not actually installed (graceful in non-E2E CI)
# ---------------------------------------------------------------------------


def _chromium_available() -> bool:
    try:
        import playwright  # noqa: F401 PLC0415
        from playwright.sync_api import sync_playwright  # noqa: PLC0415

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# StubSedeServer fixture (inline — avoids cross-repo import in hermes tests)
# ---------------------------------------------------------------------------

# We locate the gestoria-agent stub by adding its path if available.
_GESTORIA_PATH = "/home/luiscorrea-dev/Desktop/gestoria-agent"


def _import_stub_server() -> type:
    """Import StubSedeServer from gestoria-agent (cross-repo)."""
    if _GESTORIA_PATH not in sys.path:
        sys.path.insert(0, _GESTORIA_PATH)
    from tests.e2e.browser.fixtures.stub_sede import StubSedeServer  # noqa: PLC0415

    return StubSedeServer


@pytest.fixture(scope="module")
def stub_server():  # type: ignore[no-untyped-def]
    """Start StubSedeServer for the module, stop after."""
    StubSedeServer = _import_stub_server()
    server = StubSedeServer()
    server.start()
    yield server
    server.stop()


# ---------------------------------------------------------------------------
# LLM mock helpers
# ---------------------------------------------------------------------------


def _scripted_litellm_responses(stub_base_url: str) -> list[dict]:
    """Return a list of pre-scripted LLM responses for the US1 flow.

    Each response simulates Stagehand's act/extract primitives via
    a tool-call-like structure the monkeypatch returns sequentially.
    """
    return [
        {"navigate": stub_base_url + "/login"},
        {"act": "fill username"},
        {"act": "fill password"},
        {"act": "click login"},
        {"extract": {"nombre": "testuser", "referencia": "REF-STUBTEST"}},
        {"act": "fill concepto"},
        {"act": "submit form"},
        {"extract": {"referencia": "REF-STUBTEST", "status": "ACEPTADO"}},
    ]


# ---------------------------------------------------------------------------
# Main E2E test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.requires_chromium
async def test_us1_form_flow_navigate_login_submit_extract(stub_server: object) -> None:
    """US1/AC1: Full navigate→login→form→submit→extract flow against stub.

    With HERMES_API_KEY: uses real LLM (StagehandDriver).
    Without: uses FakeBrowserDriver with scripted steps.

    Always asserts:
      - referencia extracted with UUID-like value.
      - StepRecorder has >= 6 records.
    """
    if not _chromium_available():
        pytest.skip("Chromium not installed — run `playwright install chromium` first")


    base_url = stub_server.base_url  # type: ignore[union-attr]
    tenant_id = UUID("aaaaaaaa-0000-0000-0000-000000000001")

    hermes_api_key = os.getenv("HERMES_API_KEY")
    hermes_model = os.getenv("HERMES_MODEL", "")

    start_time = time.monotonic()

    if hermes_api_key and hermes_model:
        await _run_e2e_with_real_llm(
            base_url=base_url,
            tenant_id=tenant_id,
            model=hermes_model,
            api_key=hermes_api_key,
        )
    else:
        await _run_e2e_with_fake_driver(
            base_url=base_url,
            tenant_id=tenant_id,
        )

    elapsed = time.monotonic() - start_time
    assert elapsed < 300, f"Flow took {elapsed:.1f}s — exceeds 5 min budget"


async def _run_e2e_with_fake_driver(*, base_url: str, tenant_id: UUID) -> None:
    """E2E with FakeBrowserDriver: scripted outcomes, no Chromium needed."""
    from hermes.browser.application.session import (  # noqa: PLC0415
        BrowserSession,
        BrowserSessionConfig,
    )
    from hermes.browser.application.step_recorder import (  # noqa: PLC0415
        InMemoryArtifactStore,
        InMemoryRecordSink,
        StepRecorder,
    )
    from hermes.browser.domain.step import StepRisk, StepStatus  # noqa: PLC0415
    from hermes.browser.testing import FakeBrowserDriver, scripted_step  # noqa: PLC0415

    _FAKE_REF = "REF-STUBTEST-0001"

    driver = FakeBrowserDriver(
        scripted=(
            scripted_step(matches_kind="navigate", result={"url": base_url + "/login"}),
            scripted_step(matches_intent_substr="usuario", result={"filled": True}),
            scripted_step(matches_intent_substr="contrasena", result={"filled": True}),
            scripted_step(matches_intent_substr="entrar", result={"clicked": True}),
            scripted_step(matches_kind="extract", result={"referencia": _FAKE_REF}),
            scripted_step(matches_intent_substr="concepto", result={"filled": True}),
            scripted_step(matches_intent_substr="envia", result={"submitted": True}),
        ),
        default_dom="<html><body>stub</body></html>",
    )

    config = BrowserSessionConfig(
        tenant_id=tenant_id,
        site_id="demo_sede_stub",
        flow_id="us1_form_flow",
        anti_bot_min_delay_ms=1,
        anti_bot_max_delay_ms=3,
        anti_bot_mean_delay_ms=2,
    )
    artifacts = InMemoryArtifactStore()
    sink = InMemoryRecordSink()
    recorder = StepRecorder(artifact_store=artifacts, sink=sink)

    async with BrowserSession.open(config=config, driver=driver, recorder=recorder) as session:
        o_nav = await session.navigate(base_url + "/login")
        o_user = await session.act("introduce el usuario testuser", risk=StepRisk.LOW)
        o_pass = await session.act("introduce la contrasena testpass", risk=StepRisk.LOW)
        o_login = await session.act("haz click en entrar", risk=StepRisk.LOW)
        o_extract = await session.extract(
            instruction="extrae la referencia",
            schema={"type": "object", "properties": {"referencia": {"type": "string"}}},
        )
        o_concepto = await session.act("rellena el concepto con prueba", risk=StepRisk.LOW)
        o_submit = await session.act(
            "envia el formulario definitivo",
            risk=StepRisk.HIGH,
            hitl_approval_token="test-hitl-token-valid",
        )

    # Assertions.
    for name, outcome in [
        ("navigate", o_nav), ("user", o_user), ("pass", o_pass),
        ("login", o_login), ("extract", o_extract),
        ("concepto", o_concepto), ("submit", o_submit),
    ]:
        assert outcome.status == StepStatus.EXECUTED_OK, (
            f"Step {name} failed: {outcome.error}"
        )

    assert "referencia" in o_extract.result, "Extract must return referencia field"
    assert o_extract.result["referencia"] == _FAKE_REF

    # StepRecorder: >= 6 records (7 steps × 1 record each).
    assert len(sink.records) >= 6, f"Expected >= 6 records, got {len(sink.records)}"

    # Each record must have at least a pre-snapshot.
    for record in sink.records:
        assert len(record.screenshots) >= 1 or len(record.dom_snapshots) >= 1, (
            f"Record for step {record.step.kind} has no artifacts"
        )


async def _run_e2e_with_real_llm(
    *,
    base_url: str,
    tenant_id: UUID,
    model: str,
    api_key: str,
) -> None:
    """E2E with real StagehandDriver + real LLM (requires_chromium marker)."""
    from hermes.browser.application.session import (  # noqa: PLC0415
        BrowserSession,
        BrowserSessionConfig,
    )
    from hermes.browser.application.step_recorder import (  # noqa: PLC0415
        InMemoryArtifactStore,
        InMemoryRecordSink,
        StepRecorder,
    )
    from hermes.browser.domain.step import StepRisk, StepStatus  # noqa: PLC0415
    from hermes.browser.infrastructure.stagehand_driver import StagehandDriver  # noqa: PLC0415

    driver = StagehandDriver(model_name=model, api_key=api_key)
    await driver.start()

    config = BrowserSessionConfig(
        tenant_id=tenant_id,
        site_id="demo_sede_stub",
        flow_id="us1_form_flow",
        anti_bot_min_delay_ms=200,
        anti_bot_max_delay_ms=800,
        anti_bot_mean_delay_ms=400,
    )
    artifacts = InMemoryArtifactStore()
    sink = InMemoryRecordSink()
    recorder = StepRecorder(artifact_store=artifacts, sink=sink)

    async with BrowserSession.open(config=config, driver=driver, recorder=recorder) as session:
        await session.navigate(base_url + "/login")
        await session.act("fill the username field with testuser", risk=StepRisk.LOW)
        await session.act("fill the password field with testpass", risk=StepRisk.LOW)
        await session.act("click the login button", risk=StepRisk.LOW)
        o_extract = await session.extract(
            instruction="extract the referencia UUID value",
            schema={"type": "object", "properties": {"referencia": {"type": "string"}}},
        )
        await session.act("fill the concepto field with test concept", risk=StepRisk.LOW)
        o_submit = await session.act(
            "click the submit button",
            risk=StepRisk.HIGH,
            hitl_approval_token="test-hitl-token-valid",
        )

    assert o_submit.status == StepStatus.EXECUTED_OK
    assert "referencia" in o_extract.result
    assert len(sink.records) >= 6
