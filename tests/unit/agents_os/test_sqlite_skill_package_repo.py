"""Tests SQLiteSkillPackageRepo (FR-026)."""

from __future__ import annotations

import secrets
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest

from hermes.agents_os.application.intent_router import IntentRouter
from hermes.agents_os.application.skill_compiler import (
    SkillCompiler,
    SkillPackageState,
)
from hermes.agents_os.application.training_session_orchestrator import (
    TrainingSessionOrchestrator,
)
from hermes.agents_os.domain.surface_kind import SurfaceKind
from hermes.agents_os.infrastructure.sqlite_skill_package_repo import (
    SQLiteSkillPackageRepo,
)

pytestmark = pytest.mark.unit

MIG1 = (
    Path(__file__).parents[3]
    / "ops"
    / "agents-os-edition"
    / "migrations"
    / "sqlite"
    / "001_initial_personal_desktop.sql"
)
MIG2 = (
    Path(__file__).parents[3]
    / "ops"
    / "agents-os-edition"
    / "migrations"
    / "sqlite"
    / "002_skill_package_metadata.sql"
)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    db = tmp_path / "sk.db"
    conn = sqlite3.connect(db)
    conn.executescript(MIG1.read_text(encoding="utf-8"))
    conn.executescript(MIG2.read_text(encoding="utf-8"))
    conn.close()
    return db


@pytest.fixture
def repo(db_path: Path) -> SQLiteSkillPackageRepo:
    return SQLiteSkillPackageRepo(db_path=db_path)


@pytest.fixture
def compiler() -> SkillCompiler:
    return SkillCompiler(signing_key=secrets.token_bytes(32))


def _signed_package(compiler: SkillCompiler, *, version: int, tenant_id=None):
    orch = TrainingSessionOrchestrator()
    tid = tenant_id or uuid4()
    sess = orch.start(
        tenant_id=tid,
        human_user_id=uuid4(),
        skill_id="invoice-upload",
        surface_kinds_allowed=frozenset(
            {SurfaceKind.BROWSER, SurfaceKind.DESKTOP_APP}
        ),
    )
    orch.capture_step(
        session_id=sess.session_id,
        surface_kind=SurfaceKind.BROWSER,
        action_payload={"x": version},
        voice_caption=f"step v{version}",
    )
    orch.request_review(session_id=sess.session_id)
    sess = orch.sign(session_id=sess.session_id, human_confirmed=True)
    return compiler.compile(session=sess, version=version), tid


class TestRoundTrip:
    def test_add_and_list_preserves_signature(
        self, repo: SQLiteSkillPackageRepo, compiler: SkillCompiler
    ) -> None:
        pkg, tid = _signed_package(compiler, version=1)
        repo.add(pkg)
        rows = repo.list_versions(tenant_id=tid, skill_id="invoice-upload")
        assert len(rows) == 1
        loaded = rows[0]
        assert loaded.signature_hex == pkg.signature_hex
        assert loaded.intent_caption == pkg.intent_caption
        assert loaded.source_training_session_id == pkg.source_training_session_id

    def test_verify_after_round_trip(
        self, repo: SQLiteSkillPackageRepo, compiler: SkillCompiler
    ) -> None:
        pkg, tid = _signed_package(compiler, version=1)
        repo.add(pkg)
        rows = repo.list_versions(tenant_id=tid, skill_id="invoice-upload")
        assert compiler.verify(rows[0]) is True


class TestDeprecate:
    def test_deprecate_marks_state(
        self, repo: SQLiteSkillPackageRepo, compiler: SkillCompiler
    ) -> None:
        pkg, tid = _signed_package(compiler, version=1)
        repo.add(pkg)
        repo.deprecate(package_id=pkg.package_id)
        rows = repo.list_versions(tenant_id=tid, skill_id="invoice-upload")
        assert rows[0].state == SkillPackageState.DEPRECATED


class TestIntentRouterIntegration:
    def test_router_resolves_from_sqlite_repo(
        self, repo: SQLiteSkillPackageRepo, compiler: SkillCompiler
    ) -> None:
        v1, tid = _signed_package(compiler, version=1)
        v2, _ = _signed_package(compiler, version=2, tenant_id=tid)
        repo.add(v1)
        repo.add(v2)
        # IntentRouter espera SkillPackageRepoPort — repo cumple list_versions.
        router = IntentRouter(repo=repo)
        latest = router.resolve(tenant_id=tid, skill_id="invoice-upload")
        assert latest.version == 2
