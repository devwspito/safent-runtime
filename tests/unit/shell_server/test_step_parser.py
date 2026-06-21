"""Regression tests for wizard/step_parser.py — covers bugs #14, #15, #16, #24.

No real LLM, no mic, no DB.  Pure unit tests against the parser functions.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure spec contracts importable (mirrors the pattern in the parser itself).
_SPEC = Path(__file__).parents[3] / "specs" / "003-agents-os-edition"
if str(_SPEC) not in sys.path:
    sys.path.insert(0, str(_SPEC))

from hermes.shell_server.wizard.step_parser import (  # noqa: E402
    StepAmbiguous,
    _WIZARD_CONSENT_CAPABILITIES,
    _extract_json,
    _human_message_after_json,
    parse_consents,
    parse_consents_deny_all,
    parse_tenant_binding,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Bug #14 — JSON extractor: nested objects / arrays / prose braces
# ---------------------------------------------------------------------------

class TestExtractJson:
    """_extract_json uses raw_decode and handles nested structures correctly."""

    def test_flat_object_parsed(self) -> None:
        text = '{"resolved": true, "value": "server"}\nHola.'
        obj, end = _extract_json(text)
        assert obj == {"resolved": True, "value": "server"}
        assert end == len('{"resolved": true, "value": "server"}')

    def test_nested_object_parsed_completely(self) -> None:
        # Regression: non-greedy regex stopped at the first '}' inside
        # the nested object and returned a truncated fragment.
        text = '{"resolved": true, "meta": {"key": "val"}}\nSiguiente paso.'
        obj, end = _extract_json(text)
        assert obj["meta"] == {"key": "val"}
        assert end == len('{"resolved": true, "meta": {"key": "val"}}')

    def test_array_of_objects_inside_json(self) -> None:
        text = '{"resolved": true, "granted": [{"name": "documents"}]}\nOk.'
        obj, end = _extract_json(text)
        # The full object is returned intact; the list element is a dict.
        assert obj["granted"] == [{"name": "documents"}]

    def test_prose_braces_after_json_do_not_affect_end_index(self) -> None:
        # Regression: the old regex found the LAST brace in prose when
        # the prose contained its own curly braces.
        text = '{"resolved": true, "value": "x"}\nElige entre {a, b, c}.'
        obj, end = _extract_json(text)
        assert obj == {"resolved": True, "value": "x"}
        # end must point to the close of the JSON, not the prose brace.
        assert end == len('{"resolved": true, "value": "x"}')

    def test_human_message_after_json_is_prose_not_brace_fragment(self) -> None:
        text = '{"resolved": true, "value": "x"}\nElige entre {a, b, c}.'
        obj, end = _extract_json(text)
        after = _human_message_after_json(text, end)
        assert after == "Elige entre {a, b, c}."

    def test_no_json_raises_step_ambiguous(self) -> None:
        with pytest.raises(StepAmbiguous):
            _extract_json("Solo texto, sin JSON.")

    def test_malformed_json_raises_step_ambiguous(self) -> None:
        with pytest.raises(StepAmbiguous):
            _extract_json('{"resolved": true, "value":}')

    def test_deeply_nested_object(self) -> None:
        text = '{"a": {"b": {"c": 1}}}'
        obj, _ = _extract_json(text)
        assert obj["a"]["b"]["c"] == 1


# ---------------------------------------------------------------------------
# Bug #15 — tenant_binding bind_now requires URL + token
# ---------------------------------------------------------------------------

class TestParseTenantBinding:
    """parse_tenant_binding must reject bind_now without URL and/or token."""

    def test_bind_now_without_url_and_token_raises(self) -> None:
        # Exact payload the bug finding confirmed parses without URL/token.
        text = '{"resolved": true, "decision": "bind_now"}\nVinculando.'
        with pytest.raises(StepAmbiguous) as exc_info:
            parse_tenant_binding(text)
        assert "URL" in exc_info.value.reason or "token" in exc_info.value.reason.lower()

    def test_bind_now_with_url_only_raises(self) -> None:
        text = (
            '{"resolved": true, "decision": "bind_now",'
            ' "tenant_endpoint_url": "https://tenant.example.com"}\nOk.'
        )
        with pytest.raises(StepAmbiguous):
            parse_tenant_binding(text)

    def test_bind_now_with_token_only_raises(self) -> None:
        text = (
            '{"resolved": true, "decision": "bind_now",'
            ' "enrollment_token": "tok-abc"}\nOk.'
        )
        with pytest.raises(StepAmbiguous):
            parse_tenant_binding(text)

    def test_bind_now_with_empty_strings_raises(self) -> None:
        text = (
            '{"resolved": true, "decision": "bind_now",'
            ' "tenant_endpoint_url": "", "enrollment_token": ""}\nOk.'
        )
        with pytest.raises(StepAmbiguous):
            parse_tenant_binding(text)

    def test_bind_now_with_full_credentials_succeeds(self) -> None:
        text = (
            '{"resolved": true, "decision": "bind_now",'
            ' "tenant_endpoint_url": "https://tenant.example.com",'
            ' "enrollment_token": "tok-xyz"}\nVinculado.'
        )
        intent, msg = parse_tenant_binding(text)
        assert intent.tenant_endpoint_url == "https://tenant.example.com"
        assert intent.enrollment_token == "tok-xyz"
        assert msg == "Vinculado."

    def test_defer_does_not_require_url_or_token(self) -> None:
        text = '{"resolved": true, "decision": "defer"}\nDiferido para más tarde.'
        intent, msg = parse_tenant_binding(text)
        from contracts.first_boot_wizard_port import TenantBindingDecision
        assert intent.decision == TenantBindingDecision.DEFER
        assert intent.tenant_endpoint_url is None
        assert intent.enrollment_token is None

    def test_unresolved_raises_with_model_reason(self) -> None:
        text = '{"resolved": false, "reason": "Falta el token."}'
        with pytest.raises(StepAmbiguous) as exc_info:
            parse_tenant_binding(text)
        assert "Falta el token." in exc_info.value.reason


# ---------------------------------------------------------------------------
# Bug #16 — consent capability names validated against whitelist
# ---------------------------------------------------------------------------

class TestParseConsentsCapabilityValidation:
    """parse_consents rejects hallucinated or malformed capability names."""

    def test_valid_subset_accepted(self) -> None:
        text = '{"resolved": true, "granted": ["documents", "microphone"]}\nOk.'
        consents, msg = parse_consents(text)
        granted_caps = {cap for cap, _ in consents.granted}
        assert granted_caps == {"documents", "microphone"}

    def test_all_known_capabilities_accepted(self) -> None:
        import json as _json
        caps = sorted(_WIZARD_CONSENT_CAPABILITIES)
        payload = _json.dumps({"resolved": True, "granted": caps})
        text = payload + "\nTodo concedido."
        # Must not raise.
        consents, _ = parse_consents(text)
        assert len(consents.granted) == len(caps)

    def test_hallucinated_capability_rejected(self) -> None:
        # 'camera' and 'filesystem' are not in the wizard prompt.
        text = '{"resolved": true, "granted": ["documents", "camera"]}\nOk.'
        with pytest.raises(StepAmbiguous) as exc_info:
            parse_consents(text)
        assert "camera" in exc_info.value.reason

    def test_unknown_capability_rejected(self) -> None:
        text = '{"resolved": true, "granted": ["filesystem"]}\nOk.'
        with pytest.raises(StepAmbiguous) as exc_info:
            parse_consents(text)
        assert "filesystem" in exc_info.value.reason

    def test_dict_element_in_granted_rejected(self) -> None:
        # LLM emits objects instead of strings — the original bug.
        text = '{"resolved": true, "granted": [{"name": "documents"}]}\nOk.'
        with pytest.raises(StepAmbiguous) as exc_info:
            parse_consents(text)
        assert "texto" in exc_info.value.reason or "inesperado" in exc_info.value.reason

    def test_missing_granted_key_raises(self) -> None:
        # granted key absent — should not silently default to [].
        text = '{"resolved": true}\nOk.'
        with pytest.raises(StepAmbiguous):
            parse_consents(text)

    def test_granted_not_a_list_raises(self) -> None:
        text = '{"resolved": true, "granted": "documents"}\nOk.'
        with pytest.raises(StepAmbiguous):
            parse_consents(text)


# ---------------------------------------------------------------------------
# Bug #24 — empty-consents (deny-all) requires explicit confirmation gate
# ---------------------------------------------------------------------------

class TestParseConsentsEmptyGranted:
    """parse_consents raises StepAmbiguous when granted is empty (deny-all)."""

    def test_empty_granted_raises_step_ambiguous(self) -> None:
        # Legitimate LLM deny-all — must NOT silently advance.
        text = '{"resolved": true, "granted": []}\nNingún permiso.'
        with pytest.raises(StepAmbiguous) as exc_info:
            parse_consents(text)
        # The error message must describe the consequence to the user.
        assert "ninguna capacidad" in exc_info.value.reason.lower()

    def test_empty_granted_error_message_is_user_facing_spanish(self) -> None:
        text = '{"resolved": true, "granted": []}\nNingún permiso.'
        with pytest.raises(StepAmbiguous) as exc_info:
            parse_consents(text)
        reason = exc_info.value.reason
        # Positive framing: describes what the user should do next.
        assert "confirma" in reason.lower()

    def test_parse_consents_deny_all_accepts_confirmed_empty(self) -> None:
        # After the user explicitly confirms deny-all, the conversation layer
        # calls parse_consents_deny_all — which accepts [] without gate.
        text = '{"resolved": true, "granted": []}\nConfirmado, ninguna.'
        consents, msg = parse_consents_deny_all(text)
        assert consents.granted == ()
        assert "Confirmado" in msg or msg  # any truthy message

    def test_parse_consents_deny_all_still_rejects_hallucinated_caps(self) -> None:
        text = '{"resolved": true, "granted": ["camera"]}\nOk.'
        with pytest.raises(StepAmbiguous):
            parse_consents_deny_all(text)

    def test_parse_consents_happy_path_not_affected(self) -> None:
        # Granting at least one valid capability must not trigger the gate.
        text = '{"resolved": true, "granted": ["screen"]}\nPantalla concedida.'
        consents, msg = parse_consents(text)
        assert len(consents.granted) == 1
        assert consents.granted[0][0] == "screen"
