"""Integration tests for SqlitePlatformModelRegistry (T017).

Tests: persist/read roundtrip, atomicity, tenant isolation.
No network, no Chromium, no LLM required.
"""

from __future__ import annotations

import pytest
from pathlib import Path
from datetime import UTC, datetime

from hermes.platforms.domain.platform_model import (
    BusinessEntity,
    HouseRule,
    HouseRuleKind,
    NavigationLandmark,
    PlatformArea,
    PlatformModel,
    StalenessMark,
    Zone,
)
from hermes.platforms.domain.platform_learning_tour import (
    PlatformLearningTour,
    TourOrigin,
)
from hermes.platforms.domain.model_gap import ModelGap
from hermes.platforms.domain.ports import PlatformModelNotFound, PlatformTourNotFound
from hermes.platforms.domain.value_objects import (
    DomainName,
    EntityRelationship,
    LandmarkKind,
    LifecycleState,
    ModelVersion,
    NavigationPath,
    PlatformModelId,
    TeachingModality,
    TourOrigin as TourOriginVO,
    ZoneHash,
)
from hermes.platforms.infrastructure.sqlite_platform_model_registry import (
    SqlitePlatformModelRegistry,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def registry(tmp_path: Path) -> SqlitePlatformModelRegistry:
    return SqlitePlatformModelRegistry(db_path=tmp_path / "test.db")


def _make_minimal_model(
    model_id: str = "m1",
    tenant_id: str = "tenant-a",
    state: LifecycleState = LifecycleState.PROVISIONAL,
) -> PlatformModel:
    zone = Zone(
        zone_id="z1",
        zone_hash=ZoneHash.compute({"zone_id": "z1", "areas": [model_id]}),
        member_refs=("a1",),
    )
    area = PlatformArea(
        area_id="a1",
        navigation_path=NavigationPath("/clientes"),
        zone_id="z1",
        domain_name=DomainName("Clientes"),
    )
    return PlatformModel(
        platform_model_id=PlatformModelId(model_id),
        version=ModelVersion(1),
        tenant_id=tenant_id,
        site_ref="site-crm",
        lifecycle_state=state,
        origin=TourOriginVO.GUIDED,
        areas=(area,),
        entities=(),
        landmarks=(),
        house_rules=(),
        zones=(zone,),
        staleness_marks=(),
        signature=None,
    )


# ---------------------------------------------------------------------------
# Basic persist / read roundtrip
# ---------------------------------------------------------------------------


class TestPersistReadRoundtrip:
    def test_save_and_get(self, registry):
        model = _make_minimal_model()
        registry.save(model)
        loaded = registry.get("m1", "tenant-a")
        assert str(loaded.platform_model_id) == "m1"
        assert loaded.tenant_id == "tenant-a"
        assert loaded.lifecycle_state == LifecycleState.PROVISIONAL

    def test_get_unknown_raises(self, registry):
        with pytest.raises(PlatformModelNotFound):
            registry.get("no-such-model", "tenant-a")

    def test_area_roundtrip(self, registry):
        model = _make_minimal_model()
        registry.save(model)
        loaded = registry.get("m1", "tenant-a")
        assert len(loaded.areas) == 1
        assert loaded.areas[0].area_id == "a1"
        assert str(loaded.areas[0].domain_name) == "Clientes"
        assert str(loaded.areas[0].navigation_path) == "/clientes"

    def test_zone_hash_preserved(self, registry):
        model = _make_minimal_model()
        registry.save(model)
        loaded = registry.get("m1", "tenant-a")
        assert loaded.zones[0].zone_hash == model.zones[0].zone_hash

    def test_lifecycle_state_updated(self, registry):
        model = _make_minimal_model()
        registry.save(model)
        confirmed = model.confirm()
        registry.save(confirmed)
        loaded = registry.get("m1", "tenant-a")
        assert loaded.lifecycle_state == LifecycleState.APRENDIDA

    def test_model_with_house_rule(self, registry):
        model = _make_minimal_model()
        import dataclasses  # noqa: PLC0415
        rule = HouseRule(
            rule_id="r1",
            kind=HouseRuleKind.NEVER_TOUCH,
            target_area_ref="a1",
            phrasing="Nunca toques el botón de borrado",
        )
        model = dataclasses.replace(model, house_rules=(rule,))
        registry.save(model)
        loaded = registry.get("m1", "tenant-a")
        assert len(loaded.house_rules) == 1
        assert loaded.house_rules[0].rule_id == "r1"

    def test_staleness_mark_roundtrip(self, registry):
        import dataclasses  # noqa: PLC0415
        model = _make_minimal_model(state=LifecycleState.HABILITADA)
        mark = StalenessMark(
            zone_id="z1",
            detected_at=datetime.now(tz=UTC),
            reason="landmark_unmatched",
            relearn_request_id="req-1",
        )
        stale_model = model.mark_zone_stale(mark)
        registry.save(stale_model)
        loaded = registry.get("m1", "tenant-a")
        assert loaded.lifecycle_state == LifecycleState.STALE
        assert len(loaded.staleness_marks) == 1


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------


class TestTenantIsolation:
    def test_different_tenants_isolated(self, registry):
        model_a = _make_minimal_model(model_id="m1", tenant_id="tenant-a")
        model_b = _make_minimal_model(model_id="m1", tenant_id="tenant-b")
        registry.save(model_a)
        registry.save(model_b)

        loaded_a = registry.get("m1", "tenant-a")
        loaded_b = registry.get("m1", "tenant-b")
        assert loaded_a.tenant_id == "tenant-a"
        assert loaded_b.tenant_id == "tenant-b"

    def test_list_by_tenant_only_returns_own(self, registry):
        registry.save(_make_minimal_model(model_id="m1", tenant_id="tenant-a"))
        registry.save(_make_minimal_model(model_id="m2", tenant_id="tenant-b"))
        results_a = registry.list_by_tenant("tenant-a")
        assert all(m.tenant_id == "tenant-a" for m in results_a)
        assert len(results_a) == 1

    def test_cross_tenant_get_raises(self, registry):
        registry.save(_make_minimal_model(model_id="m1", tenant_id="tenant-a"))
        with pytest.raises(PlatformModelNotFound):
            registry.get("m1", "tenant-b")


# ---------------------------------------------------------------------------
# PlatformLearningTour persistence
# ---------------------------------------------------------------------------


class TestTourPersistence:
    def test_save_and_get_tour(self, registry):
        tour = PlatformLearningTour(
            tour_id="tour-1",
            tenant_id="tenant-a",
            target_site_ref="site-crm",
            origin=TourOriginVO.GUIDED,
            modality=TeachingModality.TEXT_ONLY,
        )
        registry.save_tour(tour)
        loaded = registry.get_tour("tour-1")
        assert loaded.tour_id == "tour-1"
        assert str(loaded.modality) == "text_only"

    def test_get_unknown_tour_raises(self, registry):
        with pytest.raises(PlatformTourNotFound):
            registry.get_tour("no-such-tour")


# ---------------------------------------------------------------------------
# ModelGap persistence
# ---------------------------------------------------------------------------


class TestModelGapPersistence:
    def test_save_and_get_gap(self, registry):
        gap = ModelGap(
            gap_id="gap-1",
            platform_model_id="m1",
            task_ref="task-1",
            missing_descriptor="área de facturación",
            context="tarea: crear factura",
            teaching_request_id="req-1",
        )
        registry.save_gap(gap)
        loaded = registry.get_gap("gap-1")
        assert loaded.gap_id == "gap-1"
        assert loaded.missing_descriptor == "área de facturación"

    def test_list_gaps_for_model(self, registry):
        for i in range(3):
            gap = ModelGap(
                gap_id=f"gap-{i}",
                platform_model_id="m1",
                task_ref="task-1",
                missing_descriptor=f"área {i}",
                context="ctx",
                teaching_request_id=f"req-{i}",
            )
            registry.save_gap(gap)
        gaps = registry.list_gaps("m1")
        assert len(gaps) == 3
