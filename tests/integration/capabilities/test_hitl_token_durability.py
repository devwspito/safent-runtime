"""I1 integration — token single-use DURABLE (cross-instance, cross-restart).

Verifica que:
- Un token consumido en una instancia del gate no puede usarse en otra
  instancia del gate/minter que comparte la misma DB.
- El consumed_at se persiste en DB, no solo en memoria.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

pytestmark = pytest.mark.integration


def _make_gate(db_path: Path, signing_key: bytes):
    """Construye una instancia fresca de SqliteApprovalGate con su propio minter."""
    from hermes.capabilities.application.hitl_approval_minter import HitlApprovalMinter
    from hermes.capabilities.infrastructure.sqlite_approval_gate import SqliteApprovalGate
    from hermes.agents_os.application.audit_hash_chain import AuditHashChainSigner

    minter = HitlApprovalMinter(signing_key=signing_key)
    signer = AuditHashChainSigner(signing_key=signing_key)
    return SqliteApprovalGate(
        db_path=db_path,
        minter=minter,
        signer=signer,
    ), minter


class TestHitlTokenDurability:
    async def test_token_consumed_in_first_instance_rejected_by_second(
        self, tmp_path: Path
    ) -> None:
        """Token consumido en gate-1 ⇒ gate-2 (misma DB) rechaza el mismo token (I1)."""
        signing_key = os.urandom(32)
        db_path = tmp_path / "shell.db"
        proposal_id = uuid4()
        tenant_id = uuid4()
        operator_id = uuid4()

        from hermes.capabilities.domain.ports import ConsentContext, RiskLevel

        # === Gate 1: registrar + aprobar ===
        gate1, minter1 = _make_gate(db_path, signing_key)
        ctx = ConsentContext(tenant_id=tenant_id, operator_id=operator_id)
        await gate1.register_pending(
            proposal_id=proposal_id,
            work_item_id=uuid4(),
            consent_context=ctx,
            risk=RiskLevel.HIGH,
            justification="test I1",
            parameters_redacted={},
        )
        token = await gate1.approve(proposal_id=proposal_id, approved_by=operator_id)

        # === Gate 1: consume el token (primera llamada) ===
        result1 = await gate1.verify_token(proposal_id=proposal_id, token=token)
        assert result1 is True, "Primera verificación debe tener éxito."

        # === Gate 2: instancia separada, misma DB ===
        gate2, _minter2 = _make_gate(db_path, signing_key)

        # El minter2 no tiene el nonce en memoria (nueva instancia).
        # Sin la persistencia en DB, verificaría OK. Con DB, debe fallar.
        result2 = await gate2.verify_token(proposal_id=proposal_id, token=token)
        assert result2 is False, (
            "Token ya consumido en gate1 — gate2 (misma DB) debe rechazarlo (I1). "
            "El consumed_at debe persistirse en BD, no solo en memoria."
        )

    async def test_first_verify_succeeds_second_fails_same_instance(
        self, tmp_path: Path
    ) -> None:
        """Segunda llamada verify_token en la misma instancia también falla (I1)."""
        signing_key = os.urandom(32)
        db_path = tmp_path / "shell.db"
        proposal_id = uuid4()
        tenant_id = uuid4()
        operator_id = uuid4()

        from hermes.capabilities.domain.ports import ConsentContext, RiskLevel

        gate, _minter = _make_gate(db_path, signing_key)
        ctx = ConsentContext(tenant_id=tenant_id, operator_id=operator_id)
        await gate.register_pending(
            proposal_id=proposal_id,
            work_item_id=uuid4(),
            consent_context=ctx,
            risk=RiskLevel.HIGH,
            justification="test I1 same instance",
            parameters_redacted={},
        )
        token = await gate.approve(proposal_id=proposal_id, approved_by=operator_id)

        first = await gate.verify_token(proposal_id=proposal_id, token=token)
        second = await gate.verify_token(proposal_id=proposal_id, token=token)

        assert first is True
        assert second is False, "Segundo uso del mismo token debe fallar (single-use)."
