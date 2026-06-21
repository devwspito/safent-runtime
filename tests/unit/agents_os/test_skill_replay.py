"""Tests SkillReplayer (FR-027, FR-029)."""

from __future__ import annotations

import secrets
from typing import Any
from uuid import uuid4

import pytest

from hermes.agents_os.application.skill_compiler import SkillCompiler
from hermes.agents_os.application.skill_replay import (
    InvalidSignatureError,
    MissingSurfaceAdapterError,
    ReplayFailurePolicy,
    SkillReplayer,
)
from hermes.agents_os.application.training_session_orchestrator import (
    TrainingSessionOrchestrator,
)
from hermes.agents_os.domain.surface_kind import SurfaceKind

pytestmark = pytest.mark.unit


class _FakeSurfaceAdapter:
    def __init__(self, surface_kind: SurfaceKind) -> None:
        self.surface_kind = surface_kind
        self.calls: list[dict] = []
        self.always_fail = False
        self.fail_indices: set[int] = set()
        self._invocation_count = 0

    def replay_payload(self, payload: dict[str, Any]) -> bool:
        self.calls.append(payload)
        self._invocation_count += 1
        if self.always_fail or self._invocation_count - 1 in self.fail_indices:
            return False
        return True


def _signed_session(*, surfaces: list[SurfaceKind]):
    orch = TrainingSessionOrchestrator()
    sess = orch.start(
        tenant_id=uuid4(),
        human_user_id=uuid4(),
        skill_id="upload-invoice",
        surface_kinds_allowed=frozenset(surfaces),
    )
    for i, sk in enumerate(surfaces):
        orch.capture_step(
            session_id=sess.session_id,
            surface_kind=sk,
            action_payload={"index": i},
            voice_caption=f"paso {i}",
        )
    orch.request_review(session_id=sess.session_id)
    return orch.sign(session_id=sess.session_id, human_confirmed=True)


@pytest.fixture
def signing_key() -> bytes:
    return secrets.token_bytes(32)


@pytest.fixture
def compiler(signing_key: bytes) -> SkillCompiler:
    return SkillCompiler(signing_key=signing_key)


@pytest.fixture
def browser_adapter() -> _FakeSurfaceAdapter:
    return _FakeSurfaceAdapter(SurfaceKind.BROWSER)


@pytest.fixture
def desktop_adapter() -> _FakeSurfaceAdapter:
    return _FakeSurfaceAdapter(SurfaceKind.DESKTOP_APP)


class TestHappyPath:
    def test_replay_single_surface(
        self,
        compiler: SkillCompiler,
        browser_adapter: _FakeSurfaceAdapter,
    ) -> None:
        sess = _signed_session(
            surfaces=[SurfaceKind.BROWSER, SurfaceKind.BROWSER]
        )
        pkg = compiler.compile(session=sess, version=1)
        replayer = SkillReplayer(
            _allow_ungated_replay=True,  # test-only: exercise direct adapter replay
            compiler=compiler,
            adapters_by_surface={SurfaceKind.BROWSER: browser_adapter},
        )
        run = replayer.replay(package=pkg)
        assert run.succeeded
        assert len(browser_adapter.calls) == 2

    def test_replay_cross_domain_preserves_order(
        self,
        compiler: SkillCompiler,
        browser_adapter: _FakeSurfaceAdapter,
        desktop_adapter: _FakeSurfaceAdapter,
    ) -> None:
        sess = _signed_session(
            surfaces=[
                SurfaceKind.BROWSER,
                SurfaceKind.DESKTOP_APP,
                SurfaceKind.BROWSER,
            ]
        )
        pkg = compiler.compile(session=sess, version=1)
        replayer = SkillReplayer(
            _allow_ungated_replay=True,  # test-only: exercise direct adapter replay
            compiler=compiler,
            adapters_by_surface={
                SurfaceKind.BROWSER: browser_adapter,
                SurfaceKind.DESKTOP_APP: desktop_adapter,
            },
        )
        run = replayer.replay(package=pkg)
        assert run.succeeded
        # Orden global: sequence_index 0, 1, 2 → browser, desktop, browser.
        assert [r.sequence_index for r in run.step_results] == [0, 1, 2]
        assert [r.surface_kind for r in run.step_results] == [
            SurfaceKind.BROWSER,
            SurfaceKind.DESKTOP_APP,
            SurfaceKind.BROWSER,
        ]


class TestSignatureGuard:
    def test_invalid_signature_blocks_replay(
        self, browser_adapter: _FakeSurfaceAdapter
    ) -> None:
        compiler1 = SkillCompiler(signing_key=secrets.token_bytes(32))
        compiler2 = SkillCompiler(signing_key=secrets.token_bytes(32))
        sess = _signed_session(surfaces=[SurfaceKind.BROWSER])
        pkg = compiler1.compile(session=sess, version=1)
        replayer = SkillReplayer(
            _allow_ungated_replay=True,  # test-only: exercise direct adapter replay
            compiler=compiler2,  # clave distinta
            adapters_by_surface={SurfaceKind.BROWSER: browser_adapter},
        )
        with pytest.raises(InvalidSignatureError):
            replayer.replay(package=pkg)
        # Ningún step ejecutado.
        assert browser_adapter.calls == []


class TestFailurePolicy:
    def test_stop_on_first_failure(
        self,
        compiler: SkillCompiler,
        browser_adapter: _FakeSurfaceAdapter,
    ) -> None:
        sess = _signed_session(
            surfaces=[SurfaceKind.BROWSER, SurfaceKind.BROWSER, SurfaceKind.BROWSER]
        )
        pkg = compiler.compile(session=sess, version=1)
        browser_adapter.fail_indices = {1}  # falla el segundo step
        replayer = SkillReplayer(
            _allow_ungated_replay=True,  # test-only: exercise direct adapter replay
            compiler=compiler,
            adapters_by_surface={SurfaceKind.BROWSER: browser_adapter},
        )
        run = replayer.replay(package=pkg)
        assert run.aborted_due_to_failure
        assert len(run.step_results) == 2  # step 0 ok, step 1 fail, stop

    def test_continue_and_report(
        self,
        compiler: SkillCompiler,
        browser_adapter: _FakeSurfaceAdapter,
    ) -> None:
        sess = _signed_session(
            surfaces=[SurfaceKind.BROWSER, SurfaceKind.BROWSER, SurfaceKind.BROWSER]
        )
        pkg = compiler.compile(session=sess, version=1)
        browser_adapter.fail_indices = {1}
        replayer = SkillReplayer(
            _allow_ungated_replay=True,  # test-only: exercise direct adapter replay
            compiler=compiler,
            adapters_by_surface={SurfaceKind.BROWSER: browser_adapter},
        )
        run = replayer.replay(
            package=pkg, policy=ReplayFailurePolicy.CONTINUE_AND_REPORT
        )
        assert not run.aborted_due_to_failure
        assert len(run.step_results) == 3
        assert run.succeeded is False


class TestMissingAdapter:
    def test_missing_adapter_raises(
        self, compiler: SkillCompiler
    ) -> None:
        sess = _signed_session(surfaces=[SurfaceKind.BROWSER])
        pkg = compiler.compile(session=sess, version=1)
        replayer = SkillReplayer(
            _allow_ungated_replay=True,  # test-only: exercise direct adapter replay
            compiler=compiler,
            adapters_by_surface={},
        )
        with pytest.raises(MissingSurfaceAdapterError):
            replayer.replay(package=pkg)
