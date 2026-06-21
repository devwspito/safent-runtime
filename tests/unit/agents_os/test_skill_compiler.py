"""Tests SkillCompiler (FR-026, FR-031 US2 → SkillPackage firmado)."""

from __future__ import annotations

import secrets
from uuid import uuid4

import pytest

from hermes.agents_os.application.skill_compiler import (
    SkillCompilationError,
    SkillCompiler,
    SkillPackageState,
)
from hermes.agents_os.application.training_session_orchestrator import (
    TrainingSessionOrchestrator,
)
from hermes.agents_os.domain.surface_kind import SurfaceKind

pytestmark = pytest.mark.unit


@pytest.fixture
def signing_key() -> bytes:
    return secrets.token_bytes(32)


@pytest.fixture
def compiler(signing_key: bytes) -> SkillCompiler:
    return SkillCompiler(signing_key=signing_key)


def _signed_session(*, surfaces: list[SurfaceKind]):
    """Crea una TrainingSession SIGNED con steps en los surfaces dados."""
    orch = TrainingSessionOrchestrator()
    sess = orch.start(
        tenant_id=uuid4(),
        human_user_id=uuid4(),
        skill_id="invoice-upload",
        surface_kinds_allowed=frozenset(surfaces),
    )
    for i, sk in enumerate(surfaces):
        orch.capture_step(
            session_id=sess.session_id,
            surface_kind=sk,
            action_payload={"step": i},
            voice_caption=f"paso {i}",
        )
    orch.request_review(session_id=sess.session_id)
    return orch.sign(session_id=sess.session_id, human_confirmed=True)


class TestCompile:
    def test_compile_signed_session(
        self, compiler: SkillCompiler
    ) -> None:
        sess = _signed_session(surfaces=[SurfaceKind.BROWSER])
        pkg = compiler.compile(session=sess, version=1)
        assert pkg.state == SkillPackageState.SIGNED
        assert pkg.version == 1
        assert pkg.cross_domain is False
        assert SurfaceKind.BROWSER in pkg.surface_kinds

    def test_compile_unsigned_raises(
        self, compiler: SkillCompiler
    ) -> None:
        from hermes.agents_os.application.training_session_orchestrator import (  # noqa: E501
            TrainingSessionOrchestrator,
        )

        orch = TrainingSessionOrchestrator()
        sess = orch.start(
            tenant_id=uuid4(),
            human_user_id=uuid4(),
            skill_id="x",
            surface_kinds_allowed=frozenset({SurfaceKind.BROWSER}),
        )
        with pytest.raises(SkillCompilationError):
            compiler.compile(session=sess, version=1)

    def test_compile_zero_version_raises(
        self, compiler: SkillCompiler
    ) -> None:
        sess = _signed_session(surfaces=[SurfaceKind.BROWSER])
        with pytest.raises(ValueError):
            compiler.compile(session=sess, version=0)

    def test_intent_caption_concatenates_voice(
        self, compiler: SkillCompiler
    ) -> None:
        sess = _signed_session(
            surfaces=[SurfaceKind.BROWSER, SurfaceKind.TERMINAL]
        )
        pkg = compiler.compile(session=sess, version=1)
        assert "paso 0" in pkg.intent_caption
        assert "paso 1" in pkg.intent_caption

    def test_cross_domain_when_multiple_surfaces(
        self, compiler: SkillCompiler
    ) -> None:
        sess = _signed_session(
            surfaces=[SurfaceKind.BROWSER, SurfaceKind.DESKTOP_APP]
        )
        pkg = compiler.compile(session=sess, version=1)
        assert pkg.cross_domain is True
        assert len(pkg.surface_kinds) == 2


class TestVerify:
    def test_verify_round_trip(
        self, compiler: SkillCompiler
    ) -> None:
        sess = _signed_session(surfaces=[SurfaceKind.BROWSER])
        pkg = compiler.compile(session=sess, version=1)
        assert compiler.verify(pkg) is True

    def test_verify_with_other_key_fails(
        self, signing_key: bytes
    ) -> None:
        compiler1 = SkillCompiler(signing_key=signing_key)
        compiler2 = SkillCompiler(signing_key=secrets.token_bytes(32))
        sess = _signed_session(surfaces=[SurfaceKind.BROWSER])
        pkg = compiler1.compile(session=sess, version=1)
        assert compiler2.verify(pkg) is False

    def test_verify_tampered_step_fails(
        self, compiler: SkillCompiler
    ) -> None:
        sess = _signed_session(surfaces=[SurfaceKind.BROWSER])
        pkg = compiler.compile(session=sess, version=1)
        # Tamperear: añadir step extra al paquete firmado.
        from dataclasses import replace

        from hermes.agents_os.application.skill_compiler import SkillStep

        tampered_steps = dict(pkg.steps_by_surface_kind)
        tampered_steps["browser"] = list(tampered_steps["browser"]) + [
            SkillStep(
                sequence_index=999,
                surface_kind=SurfaceKind.BROWSER,
                action_payload={"evil": True},
            )
        ]
        tampered = replace(pkg, steps_by_surface_kind=tampered_steps)
        assert compiler.verify(tampered) is False


class TestVersioning:
    def test_higher_version_signs_different_package(
        self, compiler: SkillCompiler
    ) -> None:
        sess = _signed_session(surfaces=[SurfaceKind.BROWSER])
        v1 = compiler.compile(session=sess, version=1)
        v2 = compiler.compile(session=sess, version=2)
        assert v1.signature_hex != v2.signature_hex
        assert v1.version == 1
        assert v2.version == 2
