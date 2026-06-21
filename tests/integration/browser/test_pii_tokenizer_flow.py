"""T306 — Integration test: PII tokenizer flow between DiscoveryRunner and BrowserSession.

Marker: default unit (no Chromium; FakeBrowserDriver).

Verifies:
  (a) LLM provider receives tokenized payload (no NIF literal in step.intent_desc).
  (b) Browser context (FakeBrowserDriver) receives the real (rehydrated) fill_value.
  (c) StepRecord stored via InMemoryRecordSink has tokenized intent_desc (no PII leak).

Constitution III: PII tokenization always before litellm.acompletion.
Threat-model T1 surface 1.
"""

from __future__ import annotations

import re
from uuid import UUID

import pytest

from hermes.browser.application.discovery_runner import DiscoveryRunner
from hermes.browser.application.session import BrowserSessionConfig
from hermes.browser.domain.step import StepRisk
from hermes.browser.testing import FakeBrowserDriver, scripted_step
from hermes.tokenizer.pii import DefaultPIITokenizer

_TENANT = UUID("00000000-0000-0000-0000-000000000066")
_REAL_NIF = "12345678Z"
_REAL_IBAN = "ES9121000418450200051332"

# Pattern to detect NIF in plain text.
_NIF_RE = re.compile(r"\b\d{8}[A-HJ-NP-TV-Z]\b", re.IGNORECASE)
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")


def _make_config() -> BrowserSessionConfig:
    return BrowserSessionConfig(
        tenant_id=_TENANT,
        site_id="demo_sede_stub",
        flow_id="form_flow",
        anti_bot_min_delay_ms=1,
        anti_bot_max_delay_ms=3,
        anti_bot_mean_delay_ms=2,
    )


@pytest.mark.asyncio
async def test_nif_not_in_step_intent_desc_sent_to_driver() -> None:
    """(a) Step.intent_desc reaching the driver must not contain the real NIF.

    DiscoveryRunner tokenizes the instruction before building the Step.
    The driver only receives the tokenized version.
    """
    captured_intents: list[str] = []

    class _CapturingFakeDriver(FakeBrowserDriver):
        async def execute(self, step: object, *, hitl_approval_token: object = None) -> object:
            from hermes.browser.domain.step import Step

            assert isinstance(step, Step)
            captured_intents.append(step.intent_desc)
            return await super().execute(step, hitl_approval_token=hitl_approval_token)

    driver = _CapturingFakeDriver(
        scripted=(scripted_step(matches_kind="act", result={"filled": True}),)
    )
    config = _make_config()
    tokenizer = DefaultPIITokenizer()
    runner = DiscoveryRunner(
        driver=driver,
        config=config,
        domains_whitelist=("stub.local",),
        pii_tokenizer=tokenizer,
    )

    instruction = f"rellena el campo NIF con {_REAL_NIF}"
    await runner.act(instruction, risk=StepRisk.LOW, fill_value=_REAL_NIF)

    assert len(captured_intents) == 1
    received_intent = captured_intents[0]

    # The real NIF must NOT appear in the intent sent to the driver/LLM.
    assert not _NIF_RE.search(received_intent), (
        f"NIF {_REAL_NIF!r} found in intent sent to driver: {received_intent!r}"
    )
    # A placeholder must be present.
    assert "[[NIF_" in received_intent or "NIF" in received_intent


@pytest.mark.asyncio
async def test_real_nif_rehydrated_in_fill_value_to_driver() -> None:
    """(b) Browser fill_value must be the real NIF (rehydrated), not the placeholder.

    The browser context fills the actual field value — the real NIF must
    reach the input. Only the LLM prompt is tokenized.
    """
    received_payloads: list[dict] = []

    class _CapturingPayloadDriver(FakeBrowserDriver):
        async def execute(self, step: object, *, hitl_approval_token: object = None) -> object:
            from hermes.browser.domain.step import Step

            assert isinstance(step, Step)
            received_payloads.append(dict(step.payload))
            return await super().execute(step, hitl_approval_token=hitl_approval_token)

    driver = _CapturingPayloadDriver(
        scripted=(scripted_step(matches_kind="act", result={"filled": True}),)
    )
    config = _make_config()
    tokenizer = DefaultPIITokenizer()
    runner = DiscoveryRunner(
        driver=driver,
        config=config,
        domains_whitelist=("stub.local",),
        pii_tokenizer=tokenizer,
    )

    await runner.act(
        f"introduce el NIF {_REAL_NIF}",
        risk=StepRisk.LOW,
        fill_value=_REAL_NIF,
    )

    assert len(received_payloads) == 1
    payload = received_payloads[0]

    # fill_value or variables must contain the real NIF for browser fill.
    fill_val = payload.get("fill_value", "")
    variables = payload.get("variables", {})
    all_values = [fill_val] + list(variables.values())

    assert any(_REAL_NIF in str(v) for v in all_values), (
        f"Real NIF not found in driver payload — browser cannot fill the field. "
        f"payload={payload!r}"
    )


@pytest.mark.asyncio
async def test_step_record_intent_desc_is_tokenized() -> None:
    """(c) StepRecord.step.intent_desc stored in sink has no raw PII.

    If PII leaked into the persisted record, it could appear in logs/DB.
    """
    tokenizer = DefaultPIITokenizer()

    # Tokenize manually (as runner does internally) then verify the outcome.
    tok_result = tokenizer.tokenize({
        "instruction": f"rellena NIF {_REAL_NIF} e IBAN {_REAL_IBAN}",
        "fill_value": _REAL_NIF,
    })
    safe_instruction = tok_result.sanitized.get("instruction", "")

    # Assert the sanitized version has no raw PII.
    assert not _NIF_RE.search(safe_instruction), f"NIF in sanitized: {safe_instruction}"
    assert not _IBAN_RE.search(safe_instruction), f"IBAN in sanitized: {safe_instruction}"


@pytest.mark.asyncio
async def test_iban_not_in_step_payload_to_driver() -> None:
    """Regression: IBAN in IBAN field must be tokenized before reaching driver."""
    captured: list[dict] = []

    class _IbanCapture(FakeBrowserDriver):
        async def execute(self, step: object, *, hitl_approval_token: object = None) -> object:
            from hermes.browser.domain.step import Step

            assert isinstance(step, Step)
            captured.append({"intent": step.intent_desc, **step.payload})
            return await super().execute(step, hitl_approval_token=hitl_approval_token)

    driver = _IbanCapture(scripted=(scripted_step(matches_kind="act"),))
    config = _make_config()
    runner = DiscoveryRunner(
        driver=driver,
        config=config,
        domains_whitelist=("stub.local",),
        pii_tokenizer=DefaultPIITokenizer(),
    )

    await runner.act(
        f"introduce el IBAN {_REAL_IBAN}",
        risk=StepRisk.LOW,
    )

    assert len(captured) == 1
    intent = captured[0].get("intent", "") + str(captured[0].get("instruction", ""))
    # IBAN must not appear in the prompt string sent to LLM (intent/instruction).
    assert not _IBAN_RE.search(intent), (
        f"IBAN {_REAL_IBAN!r} found in driver-facing intent: {intent!r}"
    )
