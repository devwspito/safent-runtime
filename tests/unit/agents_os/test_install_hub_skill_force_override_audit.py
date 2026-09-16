"""Regression (025 Top-4): the hub installer's `force=True` sovereign override
must persist through the SAME mutator POST /security/decisions calls
(record_install_decision), not a private ScanService.allow_target() shortcut.

Bug: `_apply_owner_override_and_rescan` called `scan_svc.allow_target(scan_id)`
directly — it cleared the gate but left NO row in `install_reviews`, the audit
table `/security/decisions` writes to for every other sovereign override. A
force-installed FAIL-verdict skill was therefore indistinguishable, in the
audit trail, from a skill nobody ever reviewed.

Fix: `_apply_owner_override_and_rescan` now calls `self.record_install_decision`
(decision="allow_once") — the exact method the REST `/security/decisions` route
invokes — so the override is recorded in `install_reviews` AND flips
`scan_records.decision=ALLOWED`, from one implementation.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID

import pytest

from hermes.agents_os.infrastructure.dbus_runtime_service import DbusRuntimeServiceWiring
from hermes.security_center.application.scan_service import ScanService
from hermes.security_center.domain.install_target import InstallTarget
from hermes.security_center.domain.policy import SecurityPolicy
from hermes.security_center.domain.scan_score import Risk, Severity
from hermes.security_center.infrastructure.sqlite_scan_repo import SQLiteScanRepo
from hermes.shell_server.providers.repo import SQLiteProviderRepository
from hermes.shell_server.security.secrets import SecretsVault
from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState

pytestmark = pytest.mark.unit

_OPERATOR_UID = 1000


class _AlwaysFailScanner:
    """Guarantees a FAIL verdict — every install gets auto-blocked."""

    name = "cve"

    async def scan(self, target: InstallTarget) -> list[Risk]:
        return [
            Risk(
                category="cve",
                severity=Severity.CRITICAL,
                message="CVE-2099-0002 — simulated for test",
                evidence_ref="",
            )
        ]


class _NullApprovalGate:
    async def register_pending(self, *, proposal_id, **_) -> None: ...
    async def approve(self, *, proposal_id, approved_by) -> str:
        return ""
    async def reject(self, *, proposal_id, rejected_by, reason) -> None: ...
    async def verify_token(self, *, proposal_id, token) -> bool:
        return False
    async def approved_token_for(self, proposal_id) -> str | None:
        return None


class _InMemoryPolicyRepo:
    def __init__(self) -> None:
        self._policy = SecurityPolicy.default()

    def load(self) -> SecurityPolicy:
        return self._policy

    def save(self, policy: SecurityPolicy) -> None:
        self._policy = policy


def _make_wiring(tmp_path: Path) -> tuple[DbusRuntimeServiceWiring, SQLiteScanRepo]:
    vault = SecretsVault(master_key=os.urandom(32))
    provider_repo = SQLiteProviderRepository(db_path=tmp_path / "shell-state.db", vault=vault)
    wiring = DbusRuntimeServiceWiring(
        agent_state=InMemoryAgentState(),
        approval_gate=_NullApprovalGate(),
        authorized_uids=frozenset({_OPERATOR_UID}),
        provider_repo=provider_repo,
    )
    scan_repo = SQLiteScanRepo(db_path=tmp_path / "scans.db")
    # auto_block_fail=True (SecurityPolicy default): FAIL raises ScanBlockedError.
    wiring._scan_service = ScanService(
        scanners=[_AlwaysFailScanner()],
        history_repo=scan_repo,
        policy_repo=_InMemoryPolicyRepo(),
    )
    return wiring, scan_repo


@pytest.fixture
def wiring(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[DbusRuntimeServiceWiring, SQLiteScanRepo]:
    w, scan_repo = _make_wiring(tmp_path)
    # record_install_decision's ALLOWED-marking step imports SQLiteScanRepo fresh
    # (no DI slot) — point it at the SAME tmp db the ScanService's history_repo
    # uses, mirroring production where both default-construct SQLiteScanRepo().
    monkeypatch.setattr(
        "hermes.security_center.infrastructure.sqlite_scan_repo.SQLiteScanRepo",
        lambda: SQLiteScanRepo(db_path=tmp_path / "scans.db"),
    )
    return w, scan_repo


class TestForceOverrideAuditsThroughRecordInstallDecision:
    def test_plain_block_writes_no_install_review_row(
        self, wiring: tuple[DbusRuntimeServiceWiring, SQLiteScanRepo]
    ) -> None:
        """No force, no MFA context — a FAIL verdict blocks but audits nothing yet."""
        w, _scan_repo = wiring

        result = w.install_hub_skill(identifier="evil-skill", sender_uid=_OPERATOR_UID, force=False)

        assert result.get("blocked") is True
        rows = w._security_db_conn().execute("SELECT * FROM install_reviews").fetchall()
        assert rows == []

    def test_force_true_clears_the_block_and_installs(
        self, wiring: tuple[DbusRuntimeServiceWiring, SQLiteScanRepo]
    ) -> None:
        w, _scan_repo = wiring
        w.install_hub_skill(identifier="evil-skill", sender_uid=_OPERATOR_UID, force=False)

        result = w.install_hub_skill(identifier="evil-skill", sender_uid=_OPERATOR_UID, force=True)

        assert not result.get("blocked"), f"force=True must clear the FAIL block, got {result}"
        assert "op_id" in result

    def test_force_true_writes_exactly_one_install_review_row(
        self, wiring: tuple[DbusRuntimeServiceWiring, SQLiteScanRepo]
    ) -> None:
        """The override lands in the SAME audit table /security/decisions writes to."""
        w, _scan_repo = wiring
        blocked = w.install_hub_skill(identifier="evil-skill", sender_uid=_OPERATOR_UID, force=False)
        scan_id = blocked["scan_id"]

        w.install_hub_skill(identifier="evil-skill", sender_uid=_OPERATOR_UID, force=True)

        rows = w._security_db_conn().execute(
            "SELECT scan_id, identifier, kind, decision FROM install_reviews"
        ).fetchall()
        assert len(rows) == 1
        row = rows[0]
        assert row["scan_id"] == scan_id
        assert row["identifier"] == "evil-skill"
        assert row["kind"] == "skill"
        assert row["decision"] == "allow_once"

    def test_force_true_flips_scan_decision_to_allowed(
        self, wiring: tuple[DbusRuntimeServiceWiring, SQLiteScanRepo]
    ) -> None:
        w, scan_repo = wiring
        blocked = w.install_hub_skill(identifier="evil-skill", sender_uid=_OPERATOR_UID, force=False)
        scan_id = UUID(blocked["scan_id"])

        w.install_hub_skill(identifier="evil-skill", sender_uid=_OPERATOR_UID, force=True)

        persisted = scan_repo.get(scan_id)
        assert persisted is not None
        assert str(persisted.decision) == "ALLOWED"
