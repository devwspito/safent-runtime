"""Tests for value objects — T006.

Covers: TeachingModality, ZoneHash, CapabilityRef, PlatformModelId,
ModelVersion, DomainName, NavigationPath, PlatformModelSignature.
"""

from __future__ import annotations

import pytest

from hermes.platforms.domain.value_objects import (
    ActionRef,
    CapabilityRef,
    DomainName,
    EntityRelationship,
    LandmarkKind,
    LifecycleState,
    ModelVersion,
    NavigationPath,
    PlatformModelId,
    PlatformModelSignature,
    TeachingModality,
    TourOrigin,
    ZoneHash,
)


# ---------------------------------------------------------------------------
# PlatformModelId
# ---------------------------------------------------------------------------


class TestPlatformModelId:
    def test_valid(self):
        vid = PlatformModelId("model-abc")
        assert str(vid) == "model-abc"

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            PlatformModelId("")

    def test_whitespace_only_raises(self):
        with pytest.raises(ValueError):
            PlatformModelId("   ")


# ---------------------------------------------------------------------------
# ModelVersion
# ---------------------------------------------------------------------------


class TestModelVersion:
    def test_valid(self):
        v = ModelVersion(1)
        assert v.number == 1

    def test_zero_raises(self):
        with pytest.raises(ValueError):
            ModelVersion(0)

    def test_negative_raises(self):
        with pytest.raises(ValueError):
            ModelVersion(-1)

    def test_next(self):
        v = ModelVersion(3)
        assert v.next() == ModelVersion(4)

    def test_str(self):
        assert str(ModelVersion(7)) == "7"


# ---------------------------------------------------------------------------
# DomainName
# ---------------------------------------------------------------------------


class TestDomainName:
    def test_valid(self):
        dn = DomainName("Clientes")
        assert str(dn) == "Clientes"

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            DomainName("")


# ---------------------------------------------------------------------------
# NavigationPath
# ---------------------------------------------------------------------------


class TestNavigationPath:
    def test_valid(self):
        np = NavigationPath("/clientes/nuevo")
        assert str(np) == "/clientes/nuevo"

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            NavigationPath("")


# ---------------------------------------------------------------------------
# ZoneHash
# ---------------------------------------------------------------------------


class TestZoneHash:
    def test_valid_64_char_hex(self):
        h = "a" * 64
        zh = ZoneHash(h)
        assert zh.hex_digest == h

    def test_wrong_length_raises(self):
        with pytest.raises(ValueError):
            ZoneHash("abc123")

    def test_compute_deterministic(self):
        content = {"zone_id": "z1", "areas": ["a1"]}
        h1 = ZoneHash.compute(content)
        h2 = ZoneHash.compute(content)
        assert h1 == h2
        assert len(h1.hex_digest) == 64

    def test_compute_different_content_different_hash(self):
        h1 = ZoneHash.compute({"x": 1})
        h2 = ZoneHash.compute({"x": 2})
        assert h1 != h2

    def test_compute_key_order_independent(self):
        h1 = ZoneHash.compute({"a": 1, "b": 2})
        h2 = ZoneHash.compute({"b": 2, "a": 1})
        assert h1 == h2


# ---------------------------------------------------------------------------
# CapabilityRef
# ---------------------------------------------------------------------------


class TestCapabilityRef:
    def test_platform_kind(self):
        ref = CapabilityRef(kind="platform", capability_id="model-x", version="1")
        assert ref.kind == "platform"
        assert str(ref) == "platform:model-x@1"

    def test_skill_kind(self):
        ref = CapabilityRef(kind="skill", capability_id="skill-y", version="2")
        assert ref.kind == "skill"

    def test_invalid_kind_raises(self):
        with pytest.raises(ValueError, match="kind must be"):
            CapabilityRef(kind="integration", capability_id="x", version="1")

    def test_empty_id_raises(self):
        with pytest.raises(ValueError):
            CapabilityRef(kind="platform", capability_id="", version="1")

    def test_empty_version_raises(self):
        with pytest.raises(ValueError):
            CapabilityRef(kind="skill", capability_id="s", version="")

    def test_integration_kind_raises(self):
        with pytest.raises(ValueError):
            CapabilityRef(kind="integration", capability_id="cred", version="1")


# ---------------------------------------------------------------------------
# TeachingModality
# ---------------------------------------------------------------------------


class TestTeachingModality:
    def test_video_audio_is_demonstrating(self):
        m = TeachingModality.VIDEO_AUDIO
        assert m.is_demonstrating
        assert not m.is_describing
        assert m.has_audio_narration
        assert m.has_video

    def test_video_text_is_demonstrating(self):
        m = TeachingModality.VIDEO_TEXT
        assert m.is_demonstrating
        assert not m.has_audio_narration
        assert m.has_video

    def test_audio_only_is_describing(self):
        m = TeachingModality.AUDIO_ONLY
        assert m.is_describing
        assert not m.is_demonstrating
        assert m.has_audio_narration
        assert not m.has_video

    def test_text_only_is_describing(self):
        m = TeachingModality.TEXT_ONLY
        assert m.is_describing
        assert not m.has_audio_narration
        assert not m.has_video

    def test_string_values_match_contract(self):
        assert TeachingModality.VIDEO_AUDIO == "video_audio"
        assert TeachingModality.VIDEO_TEXT == "video_text"
        assert TeachingModality.AUDIO_ONLY == "audio_only"
        assert TeachingModality.TEXT_ONLY == "text_only"

    def test_from_string_video_audio(self):
        m = TeachingModality("video_audio")
        assert m == TeachingModality.VIDEO_AUDIO

    def test_demonstrating_implies_video(self):
        for m in (TeachingModality.VIDEO_AUDIO, TeachingModality.VIDEO_TEXT):
            assert m.is_demonstrating
            assert m.has_video

    def test_describing_implies_no_video(self):
        for m in (TeachingModality.AUDIO_ONLY, TeachingModality.TEXT_ONLY):
            assert m.is_describing
            assert not m.has_video


# ---------------------------------------------------------------------------
# PlatformModelSignature
# ---------------------------------------------------------------------------


class TestPlatformModelSignature:
    def test_valid_signature(self):
        sig = PlatformModelSignature(
            platform_model_id="m1",
            version=1,
            tenant_id="t1",
            origin_attribution="guided",
            content_hash="a" * 64,
            per_zone_hashes=("b" * 64,),
            signature_hex="c" * 64,
        )
        assert sig.platform_model_id == "m1"

    def test_empty_signature_hex_raises(self):
        with pytest.raises(ValueError):
            PlatformModelSignature(
                platform_model_id="m1",
                version=1,
                tenant_id="t1",
                origin_attribution="guided",
                content_hash="a" * 64,
                per_zone_hashes=(),
                signature_hex="",
            )

    def test_empty_content_hash_raises(self):
        with pytest.raises(ValueError):
            PlatformModelSignature(
                platform_model_id="m1",
                version=1,
                tenant_id="t1",
                origin_attribution="guided",
                content_hash="",
                per_zone_hashes=(),
                signature_hex="x" * 64,
            )
