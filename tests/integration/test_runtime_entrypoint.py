"""B1 integration test — verifica que _run() construye el CapabilityBroker REAL.

Comprueba:
- build_runtime_components() devuelve un CapabilityBroker genuino (no stub).
- El broker tiene agent_state cableado (Paso 0 kill-switch funciona).
- No usa _FailClosedBroker ni FakeCapabilityBroker.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

pytestmark = pytest.mark.integration


def _make_audit_key() -> str:
    import secrets
    return secrets.token_bytes(32).hex()


class TestRuntimeEntrypointWiring:
    def test_build_real_broker_returns_capability_broker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_build_real_broker devuelve CapabilityBroker real — no un stub (B1)."""
        from hermes.capabilities.application.capability_broker import CapabilityBroker
        from hermes.agents_os.application.audit_hash_chain import AuditHashChainSigner
        from hermes.agents_os.infrastructure.sqlite_audit_repository import SqliteAuditRepository
        from hermes.tasks.infrastructure.sqlite_agent_state import SqliteAgentState

        monkeypatch.setenv("HERMES_AUDIT_KEY", _make_audit_key())
        db_path = tmp_path / "shell-state.db"

        signing_key = bytes.fromhex(os.environ["HERMES_AUDIT_KEY"])
        firmer = AuditHashChainSigner(signing_key=signing_key)
        audit_repo = SqliteAuditRepository(db_path=db_path)
        state = SqliteAgentState(db_path=db_path)

        import hermes.runtime.__main__ as m
        consent_manager = m._build_consent_manager()

        broker, intent_log, approval_gate, *_ = m._build_real_broker(
            db_path=db_path,
            consent_manager=consent_manager,
            firmer=firmer,
            audit_repo=audit_repo,
            agent_state=state,
        )

        assert isinstance(broker, CapabilityBroker), (
            "El broker del daemon debe ser CapabilityBroker real, no un stub (B1)."
        )

    def test_broker_has_agent_state_wired(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """El broker tiene agent_state cableado — kill-switch Paso 0 funciona (B1)."""
        import hermes.runtime.__main__ as m
        from hermes.agents_os.application.audit_hash_chain import AuditHashChainSigner
        from hermes.agents_os.infrastructure.sqlite_audit_repository import SqliteAuditRepository
        from hermes.tasks.infrastructure.sqlite_agent_state import SqliteAgentState

        monkeypatch.setenv("HERMES_AUDIT_KEY", _make_audit_key())
        db_path = tmp_path / "shell-state.db"

        signing_key = bytes.fromhex(os.environ["HERMES_AUDIT_KEY"])
        firmer = AuditHashChainSigner(signing_key=signing_key)
        audit_repo = SqliteAuditRepository(db_path=db_path)
        state = SqliteAgentState(db_path=db_path)
        consent_manager = m._build_consent_manager()

        broker, _intent_log, _gate, *_ = m._build_real_broker(
            db_path=db_path,
            consent_manager=consent_manager,
            firmer=firmer,
            audit_repo=audit_repo,
            agent_state=state,
        )

        # El broker debe tener _agent_state cableado para el kill-switch (CTRL-12).
        assert broker._agent_state is state, (
            "broker._agent_state debe ser el SqliteAgentState real — "
            "Paso 0 kill-switch requiere este cableado (B1/CTRL-12)."
        )

    async def test_broker_kill_switch_blocks_dispatch_when_paused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Con agente pausado, dispatch devuelve REJECTED_BY_POLICY sin tocar adapter."""
        import hermes.runtime.__main__ as m
        from hermes.agents_os.application.audit_hash_chain import AuditHashChainSigner
        from hermes.agents_os.infrastructure.sqlite_audit_repository import SqliteAuditRepository
        from hermes.tasks.infrastructure.sqlite_agent_state import SqliteAgentState
        from hermes.capabilities.domain.ports import ConsentContext, ExecutionStatus
        from hermes.domain.proposal import ToolCallProposal

        monkeypatch.setenv("HERMES_AUDIT_KEY", _make_audit_key())
        db_path = tmp_path / "shell-state.db"

        signing_key = bytes.fromhex(os.environ["HERMES_AUDIT_KEY"])
        firmer = AuditHashChainSigner(signing_key=signing_key)
        audit_repo = SqliteAuditRepository(db_path=db_path)
        state = SqliteAgentState(db_path=db_path)
        consent_manager = m._build_consent_manager()

        broker, _il, _gate, *_ = m._build_real_broker(
            db_path=db_path,
            consent_manager=consent_manager,
            firmer=firmer,
            audit_repo=audit_repo,
            agent_state=state,
        )

        await state.pause(by=None, reason="test pause")

        proposal = ToolCallProposal(
            proposal_id=uuid4(),
            tool_name="read_file",
            tenant_id=uuid4(),
            entity_id="e",
            entity_type="t",
            parameters={"path": "/tmp/x"},
            justification="test",
        )
        ctx = ConsentContext(tenant_id=uuid4(), operator_id=uuid4())

        outcome = await broker.dispatch(proposal, ctx)
        assert outcome.status == ExecutionStatus.REJECTED_BY_POLICY
        assert "kill-switch" in (outcome.error or "")
