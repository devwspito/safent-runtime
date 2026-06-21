"""Tests for PlatformModel aggregate — T005.

Covers: invariants, lifecycle state machine, transitions, forbidden transitions.
"""

from __future__ import annotations

import pytest

from hermes.platforms.domain.platform_model import (
    BusinessEntity,
    HouseRule,
    HouseRuleKind,
    InvalidLifecycleTransition,
    LandmarkKind,
    ModelHasUnlabeledAreas,
    NavigationLandmark,
    PlatformArea,
    PlatformModel,
    StalenessMark,
    Zone,
)
from hermes.platforms.domain.value_objects import (
    DomainName,
    LifecycleState,
    ModelVersion,
    NavigationPath,
    PlatformModelId,
    TourOrigin,
    ZoneHash,
)
from datetime import UTC, datetime


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_zone(zone_id: str = "z1") -> Zone:
    return Zone(
        zone_id=zone_id,
        zone_hash=ZoneHash.compute({"zone_id": zone_id}),
        member_refs=("area-1",),
    )


def _make_area(area_id: str = "a1", needs_label: bool = False) -> PlatformArea:
    return PlatformArea(
        area_id=area_id,
        navigation_path=NavigationPath("/clientes"),
        zone_id="z1",
        domain_name=DomainName("Clientes") if not needs_label else None,
        needs_label=needs_label,
    )


def _make_model(
    lifecycle_state: LifecycleState = LifecycleState.PROVISIONAL,
    areas: tuple = None,
    staleness_marks: tuple = (),
) -> PlatformModel:
    areas = areas if areas is not None else (_make_area(),)
    return PlatformModel(
        platform_model_id=PlatformModelId("model-1"),
        version=ModelVersion(1),
        tenant_id="tenant-a",
        site_ref="site-crm",
        lifecycle_state=lifecycle_state,
        origin=TourOrigin.GUIDED,
        areas=areas,
        entities=(),
        landmarks=(),
        house_rules=(),
        zones=(_make_zone(),),
        staleness_marks=staleness_marks,
        signature=None,
    )


# ---------------------------------------------------------------------------
# Invariant: tenant_id
# ---------------------------------------------------------------------------


class TestPlatformModelInvariants:
    def test_empty_tenant_raises(self):
        with pytest.raises(ValueError, match="tenant_id"):
            PlatformModel(
                platform_model_id=PlatformModelId("m1"),
                version=ModelVersion(1),
                tenant_id="",
                site_ref="site-x",
                lifecycle_state=LifecycleState.PROVISIONAL,
                origin=TourOrigin.GUIDED,
                areas=(),
                entities=(),
                landmarks=(),
                house_rules=(),
                zones=(),
                staleness_marks=(),
                signature=None,
            )

    def test_area_must_have_domain_name_or_needs_label(self):
        with pytest.raises(ValueError, match="DomainName or needs_label"):
            PlatformArea(
                area_id="a1",
                navigation_path=NavigationPath("/path"),
                zone_id="z1",
                domain_name=None,
                needs_label=False,
            )

    def test_zone_hash_deterministic(self):
        z = _make_zone()
        z2 = Zone(
            zone_id="z1",
            zone_hash=ZoneHash.compute({"zone_id": "z1"}),
            member_refs=("area-1",),
        )
        assert z.zone_hash == z2.zone_hash

    def test_house_rule_phrasing_required(self):
        with pytest.raises(ValueError, match="phrasing"):
            HouseRule(
                rule_id="r1",
                kind=HouseRuleKind.NEVER_TOUCH,
                target_area_ref="a1",
                phrasing="",
            )


# ---------------------------------------------------------------------------
# State machine: allowed transitions
# ---------------------------------------------------------------------------


class TestLifecycleStateMachine:
    def test_provisional_to_aprendida(self):
        model = _make_model(LifecycleState.PROVISIONAL)
        confirmed = model.confirm()
        assert confirmed.lifecycle_state == LifecycleState.APRENDIDA

    def test_aprendida_to_habilitada(self):
        model = _make_model(LifecycleState.APRENDIDA)
        enabled = model.enable()
        assert enabled.lifecycle_state == LifecycleState.HABILITADA

    def test_habilitada_to_aprendida_via_disable(self):
        model = _make_model(LifecycleState.HABILITADA)
        disabled = model.disable()
        assert disabled.lifecycle_state == LifecycleState.APRENDIDA

    def test_habilitada_to_stale(self):
        model = _make_model(LifecycleState.HABILITADA)
        mark = StalenessMark(
            zone_id="z1",
            detected_at=datetime.now(tz=UTC),
            reason="landmark_unmatched",
            relearn_request_id="req-1",
        )
        stale = model.mark_zone_stale(mark)
        assert stale.lifecycle_state == LifecycleState.STALE
        assert len(stale.staleness_marks) == 1

    def test_stale_to_habilitada(self):
        model = _make_model(LifecycleState.STALE)
        restored = model.restore_from_stale()
        assert restored.lifecycle_state == LifecycleState.HABILITADA

    def test_any_to_deprecada(self):
        for state in (
            LifecycleState.PROVISIONAL,
            LifecycleState.APRENDIDA,
            LifecycleState.HABILITADA,
            LifecycleState.STALE,
        ):
            model = _make_model(state)
            deprecated = model.deprecate()
            assert deprecated.lifecycle_state == LifecycleState.DEPRECADA


# ---------------------------------------------------------------------------
# State machine: FORBIDDEN transitions (fail-closed)
# ---------------------------------------------------------------------------


class TestForbiddenTransitions:
    def test_provisional_cannot_go_directly_to_habilitada(self):
        """Key invariant from data-model.md: NO provisional → habilitada."""
        model = _make_model(LifecycleState.PROVISIONAL)
        with pytest.raises(InvalidLifecycleTransition):
            model.enable()

    def test_deprecada_cannot_transition(self):
        model = _make_model(LifecycleState.DEPRECADA)
        with pytest.raises(InvalidLifecycleTransition):
            model.confirm()

    def test_provisional_cannot_disable(self):
        model = _make_model(LifecycleState.PROVISIONAL)
        with pytest.raises(InvalidLifecycleTransition):
            model.disable()

    def test_aprendida_cannot_mark_stale(self):
        model = _make_model(LifecycleState.APRENDIDA)
        mark = StalenessMark(
            zone_id="z1",
            detected_at=datetime.now(tz=UTC),
            reason="landmark_unmatched",
            relearn_request_id="req-1",
        )
        with pytest.raises(InvalidLifecycleTransition):
            model.mark_zone_stale(mark)


# ---------------------------------------------------------------------------
# Enable fail-closed on needs_label (FR-004/FR-013)
# ---------------------------------------------------------------------------


class TestEnableFailClosedOnNeedsLabel:
    def test_enable_with_needs_label_area_raises(self):
        model = _make_model(
            lifecycle_state=LifecycleState.APRENDIDA,
            areas=(_make_area(needs_label=True),),
        )
        with pytest.raises(ModelHasUnlabeledAreas):
            model.enable()

    def test_enable_after_all_areas_labeled_succeeds(self):
        model = _make_model(
            lifecycle_state=LifecycleState.APRENDIDA,
            areas=(_make_area(needs_label=False),),
        )
        enabled = model.enable()
        assert enabled.lifecycle_state == LifecycleState.HABILITADA


# ---------------------------------------------------------------------------
# Additive amend: unaffected zone hashes preserved (FR-022)
# ---------------------------------------------------------------------------


class TestAdditiveAmend:
    def test_amend_preserves_unaffected_zone_hashes(self):
        import dataclasses  # noqa: PLC0415
        from hermes.platforms.domain.value_objects import PlatformModelSignature  # noqa: PLC0415

        zone_a = Zone(
            zone_id="z_a",
            zone_hash=ZoneHash.compute({"zone_id": "z_a", "content": "original"}),
            member_refs=("a1",),
        )
        zone_b = Zone(
            zone_id="z_b",
            zone_hash=ZoneHash.compute({"zone_id": "z_b", "content": "other"}),
            member_refs=("a2",),
        )
        base_model = _make_model(LifecycleState.APRENDIDA)
        model = dataclasses.replace(base_model, zones=(zone_a, zone_b))

        new_zone_a = Zone(
            zone_id="z_a",
            zone_hash=ZoneHash.compute({"zone_id": "z_a", "content": "updated"}),
            member_refs=("a1", "a3"),
        )
        sig = PlatformModelSignature(
            platform_model_id="model-1",
            version=2,
            tenant_id="tenant-a",
            origin_attribution="guided",
            content_hash="x" * 64,
            per_zone_hashes=(),
            signature_hex="y" * 64,
        )
        amended = model.amend(new_zones=(new_zone_a,), new_signature=sig)

        # z_b is preserved unchanged
        z_b_amended = next(z for z in amended.zones if z.zone_id == "z_b")
        assert z_b_amended.zone_hash == zone_b.zone_hash

        # z_a is updated
        z_a_amended = next(z for z in amended.zones if z.zone_id == "z_a")
        assert z_a_amended.zone_hash != zone_a.zone_hash

        # Version bumped
        assert amended.version.number == 2


# ---------------------------------------------------------------------------
# Summary dict (no PII, no selectors)
# ---------------------------------------------------------------------------


class TestSummaryDict:
    def test_summary_has_no_pii_fields(self):
        model = _make_model()
        summary = model.to_summary_dict()
        assert "model_id" in summary
        assert "lifecycle_state" in summary
        assert "area_count" in summary
        # Raw selectors must NOT be present
        assert "locator_ref" not in summary
        assert "navigation_path" not in summary
