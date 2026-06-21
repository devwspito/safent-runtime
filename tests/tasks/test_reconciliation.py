"""T031 — Tests de reconciliación idempotente (CTRL-11/RECON-1/TOP-7).

Requisitos (threat-model §2.7, RECON-1):
- No re-ejecuta si idempotency_key ya está registrado como ejecutado.
- Intent registrado sin outcome => marca needs_human_review (no re-ejecuta a
  ciegas).

Estos tests deben FALLAR hasta que se implemente IntentLog.
"""

from __future__ import annotations

import hashlib
import json
from uuid import UUID, uuid4

import pytest

from hermes.capabilities.application.intent_log import IntentLog
from hermes.capabilities.domain.ports import ExecutionOutcome, ExecutionStatus
from hermes.domain.proposal import ToolCallProposal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_proposal(tool_name: str = "write_file") -> ToolCallProposal:
    tenant = uuid4()
    return ToolCallProposal(
        proposal_id=uuid4(),
        tool_name=tool_name,
        tenant_id=tenant,
        entity_id="doc-001",
        entity_type="document",
        parameters={"path": "/tmp/test.txt", "content": "hello"},
        justification="test",
    )


def _make_outcome(proposal_id: UUID, *, executed: bool = True) -> ExecutionOutcome:
    return ExecutionOutcome(
        proposal_id=proposal_id,
        status=ExecutionStatus.EXECUTED if executed else ExecutionStatus.FAILED,
        audit_entry_id=uuid4() if executed else None,
    )


def _idempotency_key(proposal: ToolCallProposal) -> str:
    """Mismo algoritmo que IntentLog — SHA-256 de la serialización estable."""
    stable = json.dumps(
        {
            "proposal_id": str(proposal.proposal_id),
            "tool_name": proposal.tool_name,
            "tenant_id": str(proposal.tenant_id),
            "parameters": proposal.parameters,
        },
        sort_keys=True,
    ).encode()
    return hashlib.sha256(stable).hexdigest()


# ---------------------------------------------------------------------------
# T031-A: no re-ejecuta si idempotency_key ya está ejecutado
# ---------------------------------------------------------------------------


def test_was_executed_false_before_any_record() -> None:
    """Propuesta no registrada => was_executed retorna False."""
    log = IntentLog()
    proposal = _make_proposal()
    key = _idempotency_key(proposal)
    assert log.was_executed(key) is False


def test_was_executed_false_after_intent_only() -> None:
    """Intent registrado sin outcome => was_executed retorna False."""
    log = IntentLog()
    proposal = _make_proposal()
    key = _idempotency_key(proposal)
    log.record_intent(key, proposal)
    # Solo intent, sin outcome => todavía no ejecutado.
    assert log.was_executed(key) is False


def test_was_executed_true_after_successful_outcome() -> None:
    """Tras record_outcome con EXECUTED => was_executed retorna True."""
    log = IntentLog()
    proposal = _make_proposal()
    key = _idempotency_key(proposal)
    log.record_intent(key, proposal)
    log.record_outcome(key, _make_outcome(proposal.proposal_id, executed=True))
    assert log.was_executed(key) is True


def test_was_executed_false_after_failed_outcome() -> None:
    """Outcome con status FAILED => was_executed retorna False (no cuenta como ejecutado)."""
    log = IntentLog()
    proposal = _make_proposal()
    key = _idempotency_key(proposal)
    log.record_intent(key, proposal)
    log.record_outcome(key, _make_outcome(proposal.proposal_id, executed=False))
    assert log.was_executed(key) is False


def test_idempotent_record_intent() -> None:
    """record_intent es idempotente — doble llamada no lanza."""
    log = IntentLog()
    proposal = _make_proposal()
    key = _idempotency_key(proposal)
    log.record_intent(key, proposal)
    log.record_intent(key, proposal)  # no debe lanzar
    assert log.was_executed(key) is False


# ---------------------------------------------------------------------------
# T031-B: intent sin outcome => needs_human_review
# ---------------------------------------------------------------------------


def test_pending_intents_empty_initially() -> None:
    """Sin intents registrados, pending_intents retorna lista vacía."""
    log = IntentLog()
    assert log.pending_intents() == []


def test_pending_intents_includes_intent_without_outcome() -> None:
    """Intent sin outcome aparece en pending_intents."""
    log = IntentLog()
    proposal = _make_proposal()
    key = _idempotency_key(proposal)
    log.record_intent(key, proposal)
    pending = log.pending_intents()
    assert len(pending) == 1
    assert pending[0] == key


def test_pending_intents_excludes_completed_intent() -> None:
    """Intent con outcome EXECUTED no aparece en pending_intents."""
    log = IntentLog()
    proposal = _make_proposal()
    key = _idempotency_key(proposal)
    log.record_intent(key, proposal)
    log.record_outcome(key, _make_outcome(proposal.proposal_id, executed=True))
    assert log.pending_intents() == []


def test_pending_intents_excludes_failed_intent() -> None:
    """Intent con outcome FAILED tampoco aparece en pending_intents.

    No re-ejecuta a ciegas — el operador debe revisar.
    """
    log = IntentLog()
    proposal = _make_proposal()
    key = _idempotency_key(proposal)
    log.record_intent(key, proposal)
    log.record_outcome(key, _make_outcome(proposal.proposal_id, executed=False))
    # Con outcome (aunque fallido) ya no está pendiente.
    assert log.pending_intents() == []


def test_pending_intents_multiple_mixed() -> None:
    """Solo los intents sin outcome aparecen en pending_intents."""
    log = IntentLog()

    p1 = _make_proposal("read_file")
    p2 = _make_proposal("write_file")
    p3 = _make_proposal("delete_file")

    k1, k2, k3 = (
        _idempotency_key(p1),
        _idempotency_key(p2),
        _idempotency_key(p3),
    )

    log.record_intent(k1, p1)
    log.record_intent(k2, p2)
    log.record_intent(k3, p3)

    # p1 se completa, p2 y p3 quedan pendientes.
    log.record_outcome(k1, _make_outcome(p1.proposal_id, executed=True))

    pending = log.pending_intents()
    assert k1 not in pending
    assert k2 in pending
    assert k3 in pending


# ---------------------------------------------------------------------------
# T031-C: idempotency_key como hash estable del proposal
# ---------------------------------------------------------------------------


def test_idempotency_key_stable_for_same_proposal() -> None:
    """La misma propuesta produce siempre el mismo idempotency_key."""
    proposal_id = uuid4()
    tenant_id = uuid4()
    proposal = ToolCallProposal(
        proposal_id=proposal_id,
        tool_name="write_file",
        tenant_id=tenant_id,
        entity_id="doc-001",
        entity_type="document",
        parameters={"path": "/tmp/f.txt"},
        justification="j",
    )
    k1 = _idempotency_key(proposal)
    k2 = _idempotency_key(proposal)
    assert k1 == k2


def test_idempotency_key_differs_for_different_parameters() -> None:
    """Propuestas con parámetros distintos producen keys distintas."""
    tenant_id = uuid4()
    base = {
        "proposal_id": uuid4(),
        "tool_name": "write_file",
        "tenant_id": tenant_id,
        "entity_id": "doc-001",
        "entity_type": "document",
        "justification": "j",
    }
    p1 = ToolCallProposal(**{**base, "parameters": {"path": "/tmp/a.txt"}})
    p2 = ToolCallProposal(**{**base, "parameters": {"path": "/tmp/b.txt"}})
    assert _idempotency_key(p1) != _idempotency_key(p2)


# ---------------------------------------------------------------------------
# Integration marker: SQLite backend
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_intent_log_sqlite_persistence(tmp_path: pytest.TempPathFactory) -> None:
    """Persistencia SQLite: intent sobrevive recreación del IntentLog."""
    db_path = tmp_path / "shell-state.db"  # type: ignore[operator]
    proposal = _make_proposal()
    key = _idempotency_key(proposal)

    log1 = IntentLog(db_path=str(db_path))
    log1.record_intent(key, proposal)

    log2 = IntentLog(db_path=str(db_path))
    # Intent debe sobrevivir entre instancias.
    assert key in log2.pending_intents()


@pytest.mark.integration
def test_intent_log_sqlite_outcome_persistence(tmp_path: pytest.TempPathFactory) -> None:
    """Persistencia SQLite: outcome ejecutado sobrevive recreación."""
    db_path = tmp_path / "shell-state.db"  # type: ignore[operator]
    proposal = _make_proposal()
    key = _idempotency_key(proposal)

    log1 = IntentLog(db_path=str(db_path))
    log1.record_intent(key, proposal)
    log1.record_outcome(key, _make_outcome(proposal.proposal_id, executed=True))

    log2 = IntentLog(db_path=str(db_path))
    assert log2.was_executed(key) is True
    assert log2.pending_intents() == []
