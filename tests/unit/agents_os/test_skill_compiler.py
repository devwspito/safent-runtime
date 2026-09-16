"""Tests SkillCompiler — SkillPackage build-from-steps + HMAC verify."""

from __future__ import annotations

import secrets
from dataclasses import replace
from uuid import uuid4

import pytest

from hermes.agents_os.application.skill_compiler import (
    SkillCompilationError,
    SkillCompiler,
    SkillPackageState,
    SkillStep,
)
from hermes.agents_os.domain.surface_kind import SurfaceKind

pytestmark = pytest.mark.unit


@pytest.fixture
def signing_key() -> bytes:
    return secrets.token_bytes(32)


@pytest.fixture
def compiler(signing_key: bytes) -> SkillCompiler:
    return SkillCompiler(signing_key=signing_key)


def _steps(surfaces: list[SurfaceKind]) -> list[tuple[SurfaceKind, dict, str | None]]:
    return [(sk, {"step": i}, f"paso {i}") for i, sk in enumerate(surfaces)]


class TestCompileFromSteps:
    def test_compile_signed_package(self, compiler: SkillCompiler) -> None:
        pkg = compiler.compile_from_steps(
            tenant_id=uuid4(),
            skill_id="invoice-upload",
            steps=_steps([SurfaceKind.BROWSER]),
            version=1,
        )
        assert pkg.state == SkillPackageState.SIGNED
        assert pkg.version == 1
        assert pkg.cross_domain is False
        assert SurfaceKind.BROWSER in pkg.surface_kinds

    def test_compile_without_steps_raises(self, compiler: SkillCompiler) -> None:
        with pytest.raises(SkillCompilationError):
            compiler.compile_from_steps(
                tenant_id=uuid4(), skill_id="x", steps=[], version=1
            )

    def test_compile_zero_version_raises(self, compiler: SkillCompiler) -> None:
        with pytest.raises(ValueError):
            compiler.compile_from_steps(
                tenant_id=uuid4(),
                skill_id="invoice-upload",
                steps=_steps([SurfaceKind.BROWSER]),
                version=0,
            )

    def test_intent_caption_concatenates_voice(self, compiler: SkillCompiler) -> None:
        pkg = compiler.compile_from_steps(
            tenant_id=uuid4(),
            skill_id="invoice-upload",
            steps=_steps([SurfaceKind.BROWSER, SurfaceKind.TERMINAL]),
            version=1,
        )
        assert "paso 0" in pkg.intent_caption
        assert "paso 1" in pkg.intent_caption

    def test_cross_domain_when_multiple_surfaces(self, compiler: SkillCompiler) -> None:
        pkg = compiler.compile_from_steps(
            tenant_id=uuid4(),
            skill_id="invoice-upload",
            steps=_steps([SurfaceKind.BROWSER, SurfaceKind.DESKTOP_APP]),
            version=1,
        )
        assert pkg.cross_domain is True
        assert len(pkg.surface_kinds) == 2


class TestVerify:
    def test_verify_round_trip(self, compiler: SkillCompiler) -> None:
        pkg = compiler.compile_from_steps(
            tenant_id=uuid4(),
            skill_id="invoice-upload",
            steps=_steps([SurfaceKind.BROWSER]),
            version=1,
        )
        assert compiler.verify(pkg) is True

    def test_verify_with_other_key_fails(self, signing_key: bytes) -> None:
        compiler1 = SkillCompiler(signing_key=signing_key)
        compiler2 = SkillCompiler(signing_key=secrets.token_bytes(32))
        pkg = compiler1.compile_from_steps(
            tenant_id=uuid4(),
            skill_id="invoice-upload",
            steps=_steps([SurfaceKind.BROWSER]),
            version=1,
        )
        assert compiler2.verify(pkg) is False

    def test_verify_tampered_step_fails(self, compiler: SkillCompiler) -> None:
        pkg = compiler.compile_from_steps(
            tenant_id=uuid4(),
            skill_id="invoice-upload",
            steps=_steps([SurfaceKind.BROWSER]),
            version=1,
        )
        # Tamperear: añadir step extra al paquete firmado.
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
        tenant_id = uuid4()
        steps = _steps([SurfaceKind.BROWSER])
        v1 = compiler.compile_from_steps(
            tenant_id=tenant_id, skill_id="invoice-upload", steps=steps, version=1
        )
        v2 = compiler.compile_from_steps(
            tenant_id=tenant_id, skill_id="invoice-upload", steps=steps, version=2
        )
        assert v1.signature_hex != v2.signature_hex
        assert v1.version == 1
        assert v2.version == 2
