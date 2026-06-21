from __future__ import annotations

from uuid import UUID, uuid4

from hermes import DefaultPolicyLayer, PolicyVerdict, ToolCallProposal
from hermes.policies.layer import (
    ImporteBound,
    UrlAllowlistRule,
    VerdictKind,
)

_TENANT = UUID("00000000-0000-0000-0000-000000000001")


def _make_proposal(**kw: object) -> ToolCallProposal:
    base: dict[str, object] = {
        "proposal_id": uuid4(),
        "tool_name": "pause_campaign",
        "tenant_id": _TENANT,
        "entity_id": "c-1",
        "entity_type": "campaign",
        "parameters": {},
        "justification": "",
    }
    base.update(kw)
    return ToolCallProposal(**base)  # type: ignore[arg-type]


def test_accept_when_no_rules() -> None:
    layer = DefaultPolicyLayer()
    verdict = layer.evaluate(_make_proposal())
    assert verdict.kind == VerdictKind.ACCEPT


def test_reject_when_tenant_validator_fails() -> None:
    layer = DefaultPolicyLayer(tenant_validator=lambda *_: False)
    verdict = layer.evaluate(_make_proposal())
    assert verdict.kind == VerdictKind.REJECT
    assert verdict.policy_name == "tenant_scope"


def test_accept_when_tenant_validator_passes() -> None:
    layer = DefaultPolicyLayer(tenant_validator=lambda *_: True)
    verdict = layer.evaluate(_make_proposal())
    assert verdict.kind == VerdictKind.ACCEPT


def test_reject_when_amount_exceeds_max() -> None:
    layer = DefaultPolicyLayer(
        importe_bounds=(
            ImporteBound(
                tool_name="update_budget", field_name="amount", min_value=0, max_value=1000
            ),
        )
    )
    verdict = layer.evaluate(
        _make_proposal(tool_name="update_budget", parameters={"amount": 5000})
    )
    assert verdict.kind == VerdictKind.REJECT
    assert verdict.policy_name == "importe_bounds"


def test_accept_amount_within_bounds() -> None:
    layer = DefaultPolicyLayer(
        importe_bounds=(
            ImporteBound(
                tool_name="update_budget", field_name="amount", min_value=0, max_value=1000
            ),
        )
    )
    verdict = layer.evaluate(
        _make_proposal(tool_name="update_budget", parameters={"amount": 500})
    )
    assert verdict.kind == VerdictKind.ACCEPT


def test_reject_when_url_not_allowlisted() -> None:
    layer = DefaultPolicyLayer(
        url_allowlist=(
            UrlAllowlistRule(
                tool_name="navigate",
                field_name="url",
                allowed_hosts=("sede.agenciatributaria.gob.es",),
            ),
        )
    )
    verdict = layer.evaluate(
        _make_proposal(tool_name="navigate", parameters={"url": "https://evil.example.com/fish"})
    )
    assert verdict.kind == VerdictKind.REJECT
    assert verdict.policy_name == "url_allowlist"


def test_accept_url_in_allowlist_subdomain() -> None:
    layer = DefaultPolicyLayer(
        url_allowlist=(
            UrlAllowlistRule(
                tool_name="navigate",
                field_name="url",
                allowed_hosts=("agenciatributaria.gob.es",),
            ),
        )
    )
    verdict = layer.evaluate(
        _make_proposal(
            tool_name="navigate",
            parameters={"url": "https://sede.agenciatributaria.gob.es/Sede"},
        )
    )
    assert verdict.kind == VerdictKind.ACCEPT


def test_reject_inventado_placeholder() -> None:
    layer = DefaultPolicyLayer(placeholder_mapping={"[[NIF_1]]": "12345678Z"})
    verdict = layer.evaluate(
        _make_proposal(
            tool_name="presentar_303",
            parameters={"cliente": "[[NIF_2]]"},  # 2 no esta en mapping
        )
    )
    assert verdict.kind == VerdictKind.REJECT
    assert verdict.policy_name == "placeholder_consistency"


def test_accept_real_placeholder() -> None:
    layer = DefaultPolicyLayer(placeholder_mapping={"[[NIF_1]]": "12345678Z"})
    verdict = layer.evaluate(
        _make_proposal(
            tool_name="presentar_303",
            parameters={"cliente": "[[NIF_1]]"},
            justification="Listo para [[NIF_1]]",
        )
    )
    assert verdict.kind == VerdictKind.ACCEPT


def test_verdict_helpers() -> None:
    assert PolicyVerdict.accept().kind == VerdictKind.ACCEPT
    rej = PolicyVerdict.reject(reason="x", policy_name="p")
    assert rej.kind == VerdictKind.REJECT
    assert rej.reason == "x"
