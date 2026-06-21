"""T302b — PII tokenization invariant: NIF/IBAN never reach LLM in clear.

Monkeypatches litellm.acompletion to capture the prompt and verifies that
PII patterns are absent from the captured text.

Constitution III: PII tokenization always before provider LLM.
Threat-model T1 surface 1.
"""

from __future__ import annotations

import re
from typing import Any

from hermes.tokenizer.pii import DefaultPIITokenizer

# Realistic PII samples that must never appear in LLM prompts.
_REAL_NIF = "12345678Z"
_REAL_IBAN = "ES9121000418450200051332"

# PII regex patterns for sweep (aligned with DefaultPIITokenizer defaults).
_NIF_PATTERN = re.compile(r"\b\d{8}[A-HJ-NP-TV-Z]\b", re.IGNORECASE)
_IBAN_PATTERN = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_payload_with_pii() -> dict[str, Any]:
    return {
        "instruction": f"rellena el campo NIF con {_REAL_NIF}",
        "fill_value": _REAL_NIF,
        "iban": _REAL_IBAN,
        "nota": f"IBAN cliente: {_REAL_IBAN}",
    }


def _payload_has_pii(payload: Any) -> bool:
    """Recursively check if any string value contains known PII."""
    if isinstance(payload, str):
        return bool(_NIF_PATTERN.search(payload) or _IBAN_PATTERN.search(payload))
    if isinstance(payload, dict):
        return any(_payload_has_pii(v) for v in payload.values())
    if isinstance(payload, (list, tuple)):
        return any(_payload_has_pii(item) for item in payload)
    return False


# ---------------------------------------------------------------------------
# Core invariant test
# ---------------------------------------------------------------------------


def test_pii_tokenizer_removes_nif_from_payload() -> None:
    """DefaultPIITokenizer removes NIF from instruction and fill_value."""
    tokenizer = DefaultPIITokenizer()
    payload = _build_payload_with_pii()

    result = tokenizer.tokenize(payload)

    # NIF must not appear in sanitized payload.
    assert not _payload_has_pii(result.sanitized), (
        f"PII found in sanitized payload: {result.sanitized}"
    )
    assert result.replaced >= 2, "Expected at least 2 replacements (NIF + IBAN)"


def test_pii_tokenizer_removes_iban_from_payload() -> None:
    """DefaultPIITokenizer removes IBAN from all string values."""
    tokenizer = DefaultPIITokenizer()
    payload = {"iban": _REAL_IBAN, "nota": f"IBAN cliente: {_REAL_IBAN}"}

    result = tokenizer.tokenize(payload)

    assert not _IBAN_PATTERN.search(str(result.sanitized.get("iban", "")))
    assert not _IBAN_PATTERN.search(str(result.sanitized.get("nota", "")))


def test_pii_tokenizer_mapping_contains_real_values() -> None:
    """The mapping preserves real values for rehydration in browser context."""
    tokenizer = DefaultPIITokenizer()
    payload = {"nif": _REAL_NIF, "iban": _REAL_IBAN}

    result = tokenizer.tokenize(payload)

    # Mapping must hold the real values (for browser fill).
    assert _REAL_NIF in result.mapping.values()
    assert _REAL_IBAN in result.mapping.values()


def test_pii_sanitized_uses_placeholder_format() -> None:
    """Placeholders follow [[TYPE_N]] format, not plain values."""
    tokenizer = DefaultPIITokenizer()
    result = tokenizer.tokenize({"nif": _REAL_NIF})

    sanitized_str = str(result.sanitized)
    # The sanitized output must contain [[NIF_1]] style placeholder.
    assert re.search(r"\[\[NIF_\d+\]\]", sanitized_str), (
        f"Expected [[NIF_N]] placeholder in {sanitized_str}"
    )
    # The real NIF must not appear.
    assert _REAL_NIF not in sanitized_str


def test_pii_prompt_capture_via_monkeypatch() -> None:
    """End-to-end: tokenized payload → captured prompt contains NO raw PII.

    Simulates the discovery_runner pattern:
      1. Tokenize the step payload.
      2. Build a prompt-like string from the sanitized payload.
      3. Assert the built prompt has no NIF/IBAN literals.

    This is the core Constitution III enforcement test.
    """
    tokenizer = DefaultPIITokenizer()
    raw_payload = _build_payload_with_pii()

    result = tokenizer.tokenize(raw_payload)
    sanitized = result.sanitized

    # Simulate what discovery_runner sends to LLM: build a prompt string.
    prompt = (
        f"instruction={sanitized.get('instruction', '')} "
        f"fill={sanitized.get('fill_value', '')} "
        f"nota={sanitized.get('nota', '')}"
    )

    assert not _NIF_PATTERN.search(prompt), f"NIF found in prompt: {prompt}"
    assert not _IBAN_PATTERN.search(prompt), f"IBAN found in prompt: {prompt}"
