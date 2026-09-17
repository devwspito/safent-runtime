"""HashChainAuditPort — WORM audit entries for executed AND denied governed-
SSH calls (spec 002 US3, D-4 for the denial half)."""

from __future__ import annotations

import os

import pytest

from hermes.agents_os.application.audit_hash_chain import AuditHashChainSigner, AuditKind
from hermes.tailnet_ssh.infrastructure.hash_chain_audit_port import HashChainAuditPort

pytestmark = pytest.mark.unit


class _FakeAuditRepo:
    def __init__(self) -> None:
        self.entries: list = []

    async def append(self, entry) -> None:
        self.entries.append(entry)


def _port(repo: _FakeAuditRepo) -> HashChainAuditPort:
    signer = AuditHashChainSigner(signing_key=os.urandom(32))
    return HashChainAuditPort(signer=signer, audit_repo=repo)


class TestRecordSshCall:
    def test_persists_one_entry_with_tailnet_ssh_executed_kind(self) -> None:
        repo = _FakeAuditRepo()
        port = _port(repo)

        port.record_ssh_call(
            host="db1.tailxxxx.ts.net", command="uptime", exit_code=0,
            duration_ms=42, truncated=False,
        )

        assert len(repo.entries) == 1
        assert repo.entries[0].audit_kind == AuditKind.TAILNET_SSH_EXECUTED


class TestRecordSshDenied:
    def test_persists_one_entry_with_tailnet_ssh_denied_kind(self) -> None:
        repo = _FakeAuditRepo()
        port = _port(repo)

        port.record_ssh_denied(
            host="db1.tailxxxx.ts.net", capability="exec", identity="auditor",
            reason="capability_denied",
        )

        assert len(repo.entries) == 1
        entry = repo.entries[0]
        assert entry.audit_kind == AuditKind.TAILNET_SSH_DENIED
        assert entry.category == "tailnet_ssh"

    def test_payload_carries_host_capability_identity_reason(self) -> None:
        import json  # noqa: PLC0415

        repo = _FakeAuditRepo()
        port = _port(repo)

        port.record_ssh_denied(
            host="db1.tailxxxx.ts.net", capability="file_write", identity="auditor",
            reason="capability_denied",
        )

        payload = json.loads(repo.entries[0].payload_json)
        assert payload == {
            "host": "db1.tailxxxx.ts.net",
            "capability": "file_write",
            "identity": "auditor",
            "reason": "capability_denied",
        }

    def test_denied_and_executed_entries_are_distinguishable(self) -> None:
        """A denial must never be mistaken for an executed call downstream."""
        repo = _FakeAuditRepo()
        port = _port(repo)

        port.record_ssh_call(
            host="db1.tailxxxx.ts.net", command="uptime", exit_code=0,
            duration_ms=1, truncated=False,
        )
        port.record_ssh_denied(
            host="db1.tailxxxx.ts.net", capability="exec", identity="auditor",
            reason="capability_denied",
        )

        kinds = {e.audit_kind for e in repo.entries}
        assert kinds == {AuditKind.TAILNET_SSH_EXECUTED, AuditKind.TAILNET_SSH_DENIED}

    def test_audit_failure_is_fail_soft_never_raises(self) -> None:
        class _BrokenRepo:
            async def append(self, _entry) -> None:
                raise RuntimeError("db down")

        signer = AuditHashChainSigner(signing_key=os.urandom(32))
        port = HashChainAuditPort(signer=signer, audit_repo=_BrokenRepo())

        port.record_ssh_denied(
            host="db1.tailxxxx.ts.net", capability="exec", identity="auditor",
            reason="capability_denied",
        )  # must not raise
