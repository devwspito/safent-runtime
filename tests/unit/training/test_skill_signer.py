"""Tests de SkillSigner + verify_skill_signature (T100, FR-015).

Firma + verify roundtrip.
"""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest

from hermes.training.application.skill_signer import (
    KmsSigningKeyPort,
    SignatureVerificationError,
    SkillSigner,
    build_canonical_payload,
    verify_skill_signature,
)
from hermes.training.domain.skill_package import SkillPackage
from hermes.training.domain.skill_state import SkillState

pytestmark = pytest.mark.unit

_KEY_ID = "test-key-v1"
_KEY_BYTES = b"hermes-test-signing-key-32bytes!"


class InMemoryKms:
    """Fake KMS para tests."""

    def __init__(self, keys: dict[str, bytes]) -> None:
        self._keys = keys

    async def get_signing_key(self, *, tenant_id: object, key_id: str) -> bytes:
        if key_id not in self._keys:
            from hermes.training.application.skill_signer import SigningKeyError

            raise SigningKeyError(f"key_id {key_id!r} not found")
        return self._keys[key_id]


def _package(tenant_id=None, replay_id=None) -> SkillPackage:
    return SkillPackage(
        package_id=uuid4(),
        skill_id=uuid4(),
        skill_version=1,
        tenant_id=tenant_id or uuid4(),
        replay_script_id=replay_id or uuid4(),
        voice_narrative_id=uuid4(),
        decision_rule_ids=(uuid4(), uuid4()),
        state=SkillState.DRAFT,
        compiled_by_operator_id=uuid4(),
        runtime_version="0.0.1-test",
        # FR-015 addendum: content_hash required for signing.
        content_hash="a" * 64,
    )


class TestSignAndVerify:
    @pytest.fixture
    def kms(self):
        return InMemoryKms({_KEY_ID: _KEY_BYTES})

    @pytest.fixture
    def signer(self, kms: InMemoryKms) -> SkillSigner:
        return SkillSigner(kms=kms)

    async def test_sign_produces_64_char_hex(self, signer: SkillSigner) -> None:
        pkg = _package()
        signed = await signer.sign(package=pkg, signing_key_id=_KEY_ID)
        assert len(signed.signature_hex) == 64

    async def test_verify_passes_after_sign(self, signer: SkillSigner, kms) -> None:
        pkg = _package()
        signed = await signer.sign(package=pkg, signing_key_id=_KEY_ID)
        # No debe levantar excepción
        await verify_skill_signature(package=signed, kms=kms)

    async def test_verify_fails_if_signature_tampered(self, signer: SkillSigner, kms) -> None:
        pkg = _package()
        signed = await signer.sign(package=pkg, signing_key_id=_KEY_ID)
        # Tamper la firma
        tampered = replace(signed, signature_hex="a" * 64)
        with pytest.raises(SignatureVerificationError):
            await verify_skill_signature(package=tampered, kms=kms)

    async def test_verify_fails_if_no_signature(self, kms) -> None:
        pkg = _package()  # sin firma
        with pytest.raises(SignatureVerificationError):
            await verify_skill_signature(package=pkg, kms=kms)

    async def test_sign_is_deterministic_for_same_package(self, signer: SkillSigner) -> None:
        """Misma canonical → misma firma (HMAC es determinista)."""
        pkg = _package()
        s1 = await signer.sign(package=pkg, signing_key_id=_KEY_ID)
        s2 = await signer.sign(package=pkg, signing_key_id=_KEY_ID)
        assert s1.signature_hex == s2.signature_hex

    async def test_different_packages_produce_different_signatures(
        self, signer: SkillSigner
    ) -> None:
        pkg_a = _package()
        pkg_b = _package()
        s_a = await signer.sign(package=pkg_a, signing_key_id=_KEY_ID)
        s_b = await signer.sign(package=pkg_b, signing_key_id=_KEY_ID)
        assert s_a.signature_hex != s_b.signature_hex


class TestCanonicalPayload:
    def test_payload_is_bytes(self) -> None:
        pkg = _package()
        canonical = build_canonical_payload(pkg)
        assert isinstance(canonical, bytes)

    def test_payload_is_json_deterministic(self) -> None:
        """Mismo package → misma canonical en dos llamadas consecutivas."""
        pkg = _package()
        c1 = build_canonical_payload(pkg)
        c2 = build_canonical_payload(pkg)
        assert c1 == c2

    def test_payload_changes_when_rule_ids_change(self) -> None:
        pkg_a = _package()
        pkg_b = replace(pkg_a, decision_rule_ids=(uuid4(),))
        c_a = build_canonical_payload(pkg_a)
        c_b = build_canonical_payload(pkg_b)
        assert c_a != c_b
