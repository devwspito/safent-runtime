"""Tests PostgresSkillPackageRepo con fakes asyncpg (FR-026)."""

from __future__ import annotations

import json
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from hermes.agents_os.application.skill_compiler import (
    SkillCompiler,
    SkillPackageState,
)
from hermes.agents_os.application.training_session_orchestrator import (
    TrainingSessionOrchestrator,
)
from hermes.agents_os.domain.surface_kind import SurfaceKind
from hermes.agents_os.infrastructure.postgres_skill_package_repo import (
    PostgresSkillPackageRepo,
)

pytestmark = pytest.mark.unit


class _FakeConn:
    def __init__(self, store: dict) -> None:
        self._store = store

    async def execute(self, query: str, *args):
        q = " ".join(query.split())
        if q.startswith("INSERT INTO agents_os.skill_packages"):
            (
                package_id,
                tenant_id,
                skill_id,
                version,
                state,
                signature_hex,
                surface_kinds,
                cross_domain,
                steps_json,
                intent_caption,
                source_session_id,
                created_at,
            ) = args
            self._store[package_id] = {
                "package_id": package_id,
                "tenant_id": tenant_id,
                "skill_id": skill_id,
                "skill_version": version,
                "state": state,
                "signature_hex": signature_hex,
                "surface_kinds": list(surface_kinds),
                "cross_domain": cross_domain,
                "steps_by_surface_kind": steps_json,
                "intent_caption": intent_caption,
                "source_training_session_id": source_session_id,
                "created_at": created_at,
            }
            return "INSERT 0 1"
        if q.startswith(
            "UPDATE agents_os.skill_packages SET state ="
        ):
            new_state, package_id = args
            if package_id not in self._store:
                return "UPDATE 0"
            self._store[package_id]["state"] = new_state
            return "UPDATE 1"
        raise NotImplementedError(query)

    async def fetch(self, query, *args):
        tenant_id, skill_id = args
        rows = [
            row
            for row in self._store.values()
            if row["tenant_id"] == tenant_id and row["skill_id"] == skill_id
        ]
        rows.sort(key=lambda r: r["skill_version"])
        return rows


class _FakePool:
    def __init__(self) -> None:
        self.store: dict[UUID, dict] = {}

    def acquire(self):
        @asynccontextmanager
        async def _mgr():
            yield _FakeConn(self.store)

        return _mgr()


@pytest.fixture
def pool() -> _FakePool:
    return _FakePool()


@pytest.fixture
def repo(pool: _FakePool) -> PostgresSkillPackageRepo:
    return PostgresSkillPackageRepo(pool=pool)


@pytest.fixture
def compiler() -> SkillCompiler:
    return SkillCompiler(signing_key=secrets.token_bytes(32))


def _signed_package(compiler, *, version, tenant_id=None):
    orch = TrainingSessionOrchestrator()
    tid = tenant_id or uuid4()
    sess = orch.start(
        tenant_id=tid,
        human_user_id=uuid4(),
        skill_id="invoice-upload",
        surface_kinds_allowed=frozenset({SurfaceKind.BROWSER}),
    )
    orch.capture_step(
        session_id=sess.session_id,
        surface_kind=SurfaceKind.BROWSER,
        action_payload={"v": version},
        voice_caption=f"v{version}",
    )
    orch.request_review(session_id=sess.session_id)
    sess = orch.sign(session_id=sess.session_id, human_confirmed=True)
    return compiler.compile(session=sess, version=version), tid


class TestRoundTrip:
    def test_add_and_verify(
        self, repo: PostgresSkillPackageRepo, compiler: SkillCompiler
    ) -> None:
        pkg, tid = _signed_package(compiler, version=1)
        repo.add(pkg)
        loaded = repo.list_versions(
            tenant_id=tid, skill_id="invoice-upload"
        )
        assert len(loaded) == 1
        assert compiler.verify(loaded[0]) is True

    def test_versions_sorted_by_version_asc(
        self, repo: PostgresSkillPackageRepo, compiler: SkillCompiler
    ) -> None:
        v3, tid = _signed_package(compiler, version=3)
        v1, _ = _signed_package(compiler, version=1, tenant_id=tid)
        v2, _ = _signed_package(compiler, version=2, tenant_id=tid)
        repo.add(v3)
        repo.add(v1)
        repo.add(v2)
        rows = repo.list_versions(
            tenant_id=tid, skill_id="invoice-upload"
        )
        assert [r.version for r in rows] == [1, 2, 3]


class TestDeprecate:
    def test_deprecate_persists(
        self, repo: PostgresSkillPackageRepo, compiler: SkillCompiler
    ) -> None:
        pkg, tid = _signed_package(compiler, version=1)
        repo.add(pkg)
        repo.deprecate(package_id=pkg.package_id)
        rows = repo.list_versions(
            tenant_id=tid, skill_id="invoice-upload"
        )
        assert rows[0].state == SkillPackageState.DEPRECATED


class TestIntentRouter:
    def test_router_resolves_from_postgres_repo(
        self, repo: PostgresSkillPackageRepo, compiler: SkillCompiler
    ) -> None:
        from hermes.agents_os.application.intent_router import IntentRouter

        v1, tid = _signed_package(compiler, version=1)
        v2, _ = _signed_package(compiler, version=2, tenant_id=tid)
        repo.add(v1)
        repo.add(v2)
        router = IntentRouter(repo=repo)
        latest = router.resolve(tenant_id=tid, skill_id="invoice-upload")
        assert latest.version == 2
