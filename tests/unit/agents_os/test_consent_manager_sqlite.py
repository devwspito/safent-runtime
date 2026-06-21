"""Regression tests: ConsentManager SQLite persistence (finding #18 / FR-054).

Verifies that consent state survives a ConsentManager restart by reading
from the SQLiteConsentRepository after a fresh instantiation.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from uuid import uuid4

import pytest

from hermes.agents_os.application.consent_manager import (
    Capability,
    ConsentDenied,
    ConsentManager,
    ConsentScope,
)
from hermes.agents_os.infrastructure.sqlite_consent_repo import (
    SQLiteConsentRepository,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "consent_test.db"


@pytest.fixture
def repo(db_path: Path) -> SQLiteConsentRepository:
    return SQLiteConsentRepository(db_path=db_path)


class TestSQLiteConsentPersistence:
    def test_grant_survives_restart(self, db_path: Path) -> None:
        """After a restart (new ConsentManager), persistent consents are still active."""
        op = uuid4()
        ten = uuid4()

        # First session: grant.
        repo1 = SQLiteConsentRepository(db_path=db_path)
        mgr1 = ConsentManager(repo=repo1)
        mgr1.grant(
            tenant_id=ten,
            human_operator_id=op,
            capability=Capability.DOCUMENTS,
            scope=ConsentScope.PERSISTENT,
        )

        # Second session: new manager from same DB.
        repo2 = SQLiteConsentRepository(db_path=db_path)
        mgr2 = ConsentManager(repo=repo2)
        consent = mgr2.assert_active(
            human_operator_id=op, capability=Capability.DOCUMENTS
        )
        assert consent.capability == Capability.DOCUMENTS

    def test_revoke_persists(self, db_path: Path) -> None:
        """A revocation in session A is visible in session B after restart."""
        op = uuid4()
        ten = uuid4()

        repo1 = SQLiteConsentRepository(db_path=db_path)
        mgr1 = ConsentManager(repo=repo1)
        mgr1.grant(
            tenant_id=ten,
            human_operator_id=op,
            capability=Capability.MICROPHONE,
            scope=ConsentScope.PERSISTENT,
        )
        mgr1.revoke(human_operator_id=op, capability=Capability.MICROPHONE)

        # Restart — revoked consent must NOT appear as active.
        repo2 = SQLiteConsentRepository(db_path=db_path)
        mgr2 = ConsentManager(repo=repo2)
        with pytest.raises(ConsentDenied):
            mgr2.assert_active(
                human_operator_id=op, capability=Capability.MICROPHONE
            )

    def test_once_consent_consumed_and_not_replayed(self, db_path: Path) -> None:
        """ONCE consent is consumed on first use and NOT reloaded on restart."""
        op = uuid4()
        ten = uuid4()

        repo1 = SQLiteConsentRepository(db_path=db_path)
        mgr1 = ConsentManager(repo=repo1)
        mgr1.grant(
            tenant_id=ten,
            human_operator_id=op,
            capability=Capability.SCREEN_CAPTURE,
            scope=ConsentScope.ONCE,
        )
        mgr1.use(human_operator_id=op, capability=Capability.SCREEN_CAPTURE)

        repo2 = SQLiteConsentRepository(db_path=db_path)
        mgr2 = ConsentManager(repo=repo2)
        with pytest.raises(ConsentDenied):
            mgr2.assert_active(
                human_operator_id=op, capability=Capability.SCREEN_CAPTURE
            )

    def test_no_repo_still_works_in_memory(self) -> None:
        """Without repo, ConsentManager behaves exactly as before (no regression)."""
        mgr = ConsentManager()
        op = uuid4()
        ten = uuid4()
        mgr.grant(
            tenant_id=ten,
            human_operator_id=op,
            capability=Capability.TERMINAL,
            scope=ConsentScope.SESSION,
        )
        consent = mgr.assert_active(
            human_operator_id=op, capability=Capability.TERMINAL
        )
        assert consent.capability == Capability.TERMINAL

    def test_load_all_includes_revoked(self, db_path: Path) -> None:
        """load_all returns both active and revoked records."""
        op = uuid4()
        ten = uuid4()
        repo = SQLiteConsentRepository(db_path=db_path)
        mgr = ConsentManager(repo=repo)
        mgr.grant(
            tenant_id=ten,
            human_operator_id=op,
            capability=Capability.CAMERA,
            scope=ConsentScope.PERSISTENT,
        )
        mgr.revoke(human_operator_id=op, capability=Capability.CAMERA)

        all_records = repo.load_all()
        # The revoked record must be in the full log.
        revoked = [r for r in all_records if r.revoked_at is not None]
        assert len(revoked) >= 1
        # Active should be empty.
        active = repo.load_active()
        assert all(r.capability != Capability.CAMERA for r in active)
