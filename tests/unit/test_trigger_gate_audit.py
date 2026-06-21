"""P0-6: TRIGGER_DENIED/ACTIVATED se firman y persisten en la hash-chain cuando
hay signer+repo (no-repudio durable); sin ellos, fallback no firmado en memoria."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from hermes.agents_os.application.audit_hash_chain import AuditEntry, AuditKind
from hermes.tasks.triggers.application.trigger_gate import TriggerGate
from hermes.tasks.triggers.domain.authorized_trigger_ports import AuthorizedTriggerType


class _FakeSigner:
    def __init__(self) -> None:
        self.calls: list[AuditKind] = []

    async def append_and_persist(
        self, *, audit_kind, actor, description, payload, audit_repo,
        node_installation_id=None, tenant_id=None, category=None,
    ) -> AuditEntry:
        self.calls.append(audit_kind)
        await audit_repo.append(None)
        return AuditEntry(
            entry_id=uuid4(),
            node_installation_id=node_installation_id,
            tenant_id=tenant_id,
            timestamp=datetime.now(tz=UTC),
            actor=actor,
            audit_kind=audit_kind,
            category=category,
            description=description,
            payload_hash_hex="ph",
            prev_entry_hash_hex="prev",
            signed_payload_hash_hex="sp",
            signature_hex="SIG",
        )


class _FakeAuditRepo:
    def __init__(self) -> None:
        self.appended = 0

    async def append(self, entry) -> None:  # noqa: ANN001
        self.appended += 1


def _gate(signer=None, repo=None) -> TriggerGate:
    return TriggerGate(
        trigger_repo=None,
        queue=None,
        agent_state=None,
        tenant_id=uuid4(),
        audit_signer=signer,
        audit_repo=repo,
    )


def test_denied_is_signed_and_persisted_with_signer():
    signer, repo = _FakeSigner(), _FakeAuditRepo()
    gate = _gate(signer, repo)
    asyncio.run(
        gate._emit_denied(  # noqa: SLF001
            trigger_type=AuthorizedTriggerType.TIMER, scope_value="x"
        )
    )
    assert signer.calls == [AuditKind.TRIGGER_DENIED]
    assert repo.appended == 1
    assert gate.audit_entries()[0].signature_hex == "SIG"  # firmada


def test_fallback_unsigned_without_signer():
    gate = _gate()  # sin signer/repo (tests)
    asyncio.run(
        gate._emit_denied(  # noqa: SLF001
            trigger_type=AuthorizedTriggerType.TIMER, scope_value="x"
        )
    )
    # Fallback: entrada en memoria SIN firma.
    assert gate.audit_entries()[0].signature_hex == ""
