"""Tests IntentRouter (FR-028)."""

from __future__ import annotations

import secrets
from uuid import uuid4

import pytest

from hermes.agents_os.application.intent_router import (
    InMemorySkillPackageRepo,
    IntentRouter,
    SkillDeprecated,
    SkillNotAvailable,
)
from hermes.agents_os.application.skill_compiler import SkillCompiler
from hermes.agents_os.application.training_session_orchestrator import (
    TrainingSessionOrchestrator,
)
from hermes.agents_os.domain.surface_kind import SurfaceKind

pytestmark = pytest.mark.unit


def _make_signed_package(
    compiler: SkillCompiler, version: int, tenant_id=None, skill_id="invoice-upload"
):
    orch = TrainingSessionOrchestrator()
    tid = tenant_id or uuid4()
    sess = orch.start(
        tenant_id=tid,
        human_user_id=uuid4(),
        skill_id=skill_id,
        surface_kinds_allowed=frozenset({SurfaceKind.BROWSER}),
    )
    orch.capture_step(
        session_id=sess.session_id,
        surface_kind=SurfaceKind.BROWSER,
        action_payload={"v": version},
    )
    orch.request_review(session_id=sess.session_id)
    sess = orch.sign(session_id=sess.session_id, human_confirmed=True)
    return compiler.compile(session=sess, version=version), tid


@pytest.fixture
def compiler() -> SkillCompiler:
    return SkillCompiler(signing_key=secrets.token_bytes(32))


@pytest.fixture
def repo() -> InMemorySkillPackageRepo:
    return InMemorySkillPackageRepo()


@pytest.fixture
def router(repo: InMemorySkillPackageRepo) -> IntentRouter:
    return IntentRouter(repo=repo)


class TestResolve:
    def test_no_versions_raises(
        self, router: IntentRouter
    ) -> None:
        with pytest.raises(SkillNotAvailable):
            router.resolve(tenant_id=uuid4(), skill_id="x")

    def test_returns_highest_version(
        self,
        compiler: SkillCompiler,
        repo: InMemorySkillPackageRepo,
        router: IntentRouter,
    ) -> None:
        v1, tid = _make_signed_package(compiler, 1)
        v3, _ = _make_signed_package(compiler, 3, tenant_id=tid)
        v2, _ = _make_signed_package(compiler, 2, tenant_id=tid)
        repo.add(v1)
        repo.add(v3)
        repo.add(v2)
        latest = router.resolve(tenant_id=tid, skill_id="invoice-upload")
        assert latest.version == 3

    def test_deprecation_blocks_resolve(
        self,
        compiler: SkillCompiler,
        repo: InMemorySkillPackageRepo,
        router: IntentRouter,
    ) -> None:
        v1, tid = _make_signed_package(compiler, 1)
        v2, _ = _make_signed_package(compiler, 2, tenant_id=tid)
        repo.add(v1)
        repo.add(v2)
        repo.deprecate(v2.package_id)
        # Política: NO fallback a v1 — deprecation = stop.
        with pytest.raises(SkillDeprecated):
            router.resolve(tenant_id=tid, skill_id="invoice-upload")

    def test_isolates_by_tenant(
        self,
        compiler: SkillCompiler,
        repo: InMemorySkillPackageRepo,
        router: IntentRouter,
    ) -> None:
        v1, tid_a = _make_signed_package(compiler, 1)
        v2, tid_b = _make_signed_package(compiler, 2)
        repo.add(v1)
        repo.add(v2)
        # tenant_a tiene solo v1.
        latest = router.resolve(tenant_id=tid_a, skill_id="invoice-upload")
        assert latest.version == 1
        # tenant_b tiene solo v2.
        latest = router.resolve(tenant_id=tid_b, skill_id="invoice-upload")
        assert latest.version == 2

    def test_isolates_by_skill_id(
        self,
        compiler: SkillCompiler,
        repo: InMemorySkillPackageRepo,
        router: IntentRouter,
    ) -> None:
        v1, tid = _make_signed_package(compiler, 1, skill_id="a")
        v2, _ = _make_signed_package(compiler, 5, tenant_id=tid, skill_id="b")
        repo.add(v1)
        repo.add(v2)
        assert router.resolve(tenant_id=tid, skill_id="a").version == 1
        assert router.resolve(tenant_id=tid, skill_id="b").version == 5


class TestRepo:
    def test_add_and_list(
        self, compiler: SkillCompiler, repo: InMemorySkillPackageRepo
    ) -> None:
        v1, tid = _make_signed_package(compiler, 1)
        repo.add(v1)
        rows = repo.list_versions(tenant_id=tid, skill_id="invoice-upload")
        assert len(rows) == 1
