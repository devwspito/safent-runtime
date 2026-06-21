"""T206 — AuthN/AuthZ obligatoria en LiveViewChannel adapter.

Verifica que InMemoryLiveViewChannel enforce el control P1 #6 del
threat-model: sin token → LiveViewUnauthorized; token rechazado por
policy → LiveViewUnauthorized; operador de otro tenant → LiveViewUnauthorized;
happy path con AuthZ correcta → OperatorInterventionRequest entregado.

Constitution IV: fail-closed. Sin token, ni siquiera se llama a la policy.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from hermes.browser.domain.ports.live_view_channel import (
    LiveViewUnauthorized,
    OperatorInterventionRequestEnvelope,
)
from hermes.browser.testing.in_memory_live_view_channel import (
    AlwaysUnauthorizedPolicy,
    InMemoryLiveViewChannel,
    OperatorInterventionRequest,
    TenantScopedPolicy,
)

_OPERATOR_A = UUID("aaaa0000-0000-0000-0000-000000000001")
_OPERATOR_B = UUID("bbbb0000-0000-0000-0000-000000000002")
_TENANT_A = UUID("aaaa0000-0000-0000-0000-aaaaaaaaaa01")
_TENANT_B = UUID("bbbb0000-0000-0000-0000-bbbbbbbbbb02")
_SESSION = UUID("cccc0000-0000-0000-0000-cccccccccc03")
_VALID_TOKEN = "tok_valid_abc123"


def _make_envelope(session_id: UUID | None = None) -> OperatorInterventionRequestEnvelope:
    return OperatorInterventionRequestEnvelope(
        request_id=uuid4(),
        session_id=session_id or _SESSION,
    )


# ---------------------------------------------------------------------------
# T206 test 1: sin token → LiveViewUnauthorized (fail-closed)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_intervention_without_token_raises_unauthorized() -> None:
    """Token vacío debe rechazarse antes de llamar a la policy (fail-closed).

    Threat-model control P1 #6: subscription_token time-bounded obligatorio.
    Constitution IV: fail-closed — token vacío nunca ejecuta la policy.
    """
    channel = InMemoryLiveViewChannel()
    envelope = _make_envelope()

    with pytest.raises(LiveViewUnauthorized, match="vacío"):
        await channel.request_intervention(
            envelope,
            timeout_s=30.0,
            operator_id=_OPERATOR_A,
            tenant_id=_TENANT_A,
            subscription_token="",
        )


# ---------------------------------------------------------------------------
# T206 test 2: token presente pero policy rechaza → LiveViewUnauthorized
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_intervention_with_expired_or_invalid_token_raises_unauthorized() -> None:
    """Una policy que devuelve False (token expirado/inválido) → unauthorized.

    Simula un token que fue válido en el pasado pero ya no lo es.
    Threat-model control P1 #6: token time-bounded ≤ 5 min.
    """
    channel = InMemoryLiveViewChannel(authz_policy=AlwaysUnauthorizedPolicy())
    envelope = _make_envelope()

    with pytest.raises(LiveViewUnauthorized):
        await channel.request_intervention(
            envelope,
            timeout_s=30.0,
            operator_id=_OPERATOR_A,
            tenant_id=_TENANT_A,
            subscription_token="tok_expired_or_invalid",
        )


# ---------------------------------------------------------------------------
# T206 test 3: operador de otro tenant → LiveViewUnauthorized
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_intervention_with_operator_from_other_tenant_raises_unauthorized() -> None:
    """Operador pertenece a tenant B, intenta acceder a sesión de tenant A.

    TenantScopedPolicy verifica el tuple (operator_id, tenant_id).
    Threat-model control P1 #6 / E1 superficie 5: sin IDOR cross-tenant.
    """
    # operator_A pertenece solo a tenant_A
    policy = TenantScopedPolicy(operator_tenants={_OPERATOR_A: {_TENANT_A}})
    channel = InMemoryLiveViewChannel(authz_policy=policy)
    envelope = _make_envelope()

    # operator_A intenta acceder bajo tenant_B → rechazado
    with pytest.raises(LiveViewUnauthorized):
        await channel.request_intervention(
            envelope,
            timeout_s=30.0,
            operator_id=_OPERATOR_A,
            tenant_id=_TENANT_B,  # tenant incorrecto
            subscription_token=_VALID_TOKEN,
        )


# ---------------------------------------------------------------------------
# T206 test 4: happy path → OperatorInterventionRequest entregado
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_happy_path_returns_intervention_when_authz_succeeds() -> None:
    """Operador autorizado con token válido → Future resuelve con intervención.

    TenantScopedPolicy confirma (operator_id, tenant_id).
    """
    policy = TenantScopedPolicy(operator_tenants={_OPERATOR_A: {_TENANT_A}})
    channel = InMemoryLiveViewChannel(authz_policy=policy)

    expected_response = OperatorInterventionRequest(
        operator_id=_OPERATOR_A,
        session_id=_SESSION,
        action="approved",
        payload={"confirmed": True},
    )
    channel.inject_response(expected_response)

    envelope = _make_envelope(session_id=_SESSION)

    future = await channel.request_intervention(
        envelope,
        timeout_s=30.0,
        operator_id=_OPERATOR_A,
        tenant_id=_TENANT_A,
        subscription_token=_VALID_TOKEN,
    )

    result = await future
    assert result.operator_id == _OPERATOR_A
    assert result.session_id == _SESSION
    assert result.action == "approved"
    # La intervención fue registrada para auditoría
    assert len(channel.intervention_requests) == 1
    recorded_envelope, recorded_op, recorded_tenant = channel.intervention_requests[0]
    assert recorded_op == _OPERATOR_A
    assert recorded_tenant == _TENANT_A
