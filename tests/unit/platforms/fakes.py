"""Fakes base for platform + capability tests (T003).

All fakes are in-memory, raise domain exceptions on misses, and
satisfy the same port protocols as the real adapters.
"""

from __future__ import annotations

from hermes.capabilities.domain.agent_capability_binding import AgentCapabilityBinding
from hermes.platforms.domain.model_gap import DirectedTeachingRequest, ModelGap
from hermes.platforms.domain.platform_learning_tour import PlatformLearningTour
from hermes.platforms.domain.platform_model import PlatformModel
from hermes.platforms.domain.ports import (
    CapabilityBindingNotFound,
    ModelGapNotFound,
    PlatformModelNotFound,
    PlatformTourNotFound,
)
from hermes.platforms.domain.value_objects import CapabilityRef


class InMemoryPlatformModelRegistry:
    """Satisfies PlatformModelRegistryPort in-memory."""

    def __init__(self) -> None:
        self._models: dict[str, PlatformModel] = {}
        self._tours: dict[str, PlatformLearningTour] = {}
        self._gaps: dict[str, ModelGap] = {}
        self._requests: dict[str, DirectedTeachingRequest] = {}

    def save(self, model: PlatformModel) -> None:
        self._models[str(model.platform_model_id)] = model

    def get(self, model_id: str, tenant_id: str) -> PlatformModel:
        model = self._models.get(model_id)
        if model is None or model.tenant_id != tenant_id:
            raise PlatformModelNotFound(model_id)
        return model

    def list_by_tenant(self, tenant_id: str) -> list[PlatformModel]:
        return [m for m in self._models.values() if m.tenant_id == tenant_id]

    def save_tour(self, tour: PlatformLearningTour) -> None:
        self._tours[tour.tour_id] = tour

    def get_tour(self, tour_id: str) -> PlatformLearningTour:
        if tour_id not in self._tours:
            raise PlatformTourNotFound(tour_id)
        return self._tours[tour_id]

    def save_gap(self, gap: ModelGap) -> None:
        self._gaps[gap.gap_id] = gap

    def get_gap(self, gap_id: str) -> ModelGap:
        if gap_id not in self._gaps:
            raise ModelGapNotFound(gap_id)
        return self._gaps[gap_id]

    def list_gaps(self, model_id: str) -> list[ModelGap]:
        return [g for g in self._gaps.values() if g.platform_model_id == model_id]

    def save_teaching_request(self, request: DirectedTeachingRequest) -> None:
        self._requests[request.request_id] = request


class InMemoryCapabilityBindingRepo:
    """Satisfies CapabilityBindingRepoPort in-memory."""

    def __init__(self) -> None:
        self._bindings: dict[str, AgentCapabilityBinding] = {}

    def save(self, binding: AgentCapabilityBinding) -> None:
        self._bindings[binding.binding_id] = binding

    def get(self, binding_id: str) -> AgentCapabilityBinding:
        if binding_id not in self._bindings:
            raise CapabilityBindingNotFound(binding_id)
        return self._bindings[binding_id]

    def list_by_agent(self, agent_id: str, tenant_id: str) -> list[AgentCapabilityBinding]:
        return [
            b for b in self._bindings.values()
            if b.agent_id == agent_id and b.tenant_id == tenant_id and b.is_active
        ]

    def find_active(
        self,
        agent_id: str,
        capability_kind: str,
        capability_id: str,
        tenant_id: str,
    ) -> AgentCapabilityBinding | None:
        for b in self._bindings.values():
            if (
                b.agent_id == agent_id
                and b.capability.kind == capability_kind
                and b.capability.capability_id == capability_id
                and b.tenant_id == tenant_id
                and b.is_active
            ):
                return b
        return None

    def unbind(
        self,
        agent_id: str,
        capability_kind: str,
        capability_id: str,
        tenant_id: str,
    ) -> bool:
        changed = False
        for bid, b in list(self._bindings.items()):
            if (
                b.agent_id == agent_id
                and b.capability.kind == capability_kind
                and b.capability.capability_id == capability_id
                and b.tenant_id == tenant_id
                and b.is_active
            ):
                self._bindings[bid] = b.unbind()
                changed = True
        return changed

    def save_overlay(self, overlay) -> None:
        # Stored separately; enough for unit tests.
        pass

    def list_overlays_for_agent(self, agent_id: str, model_id: str) -> list:
        return []


class FakeTourCompiler:
    """Satisfies TourCompilerPort. Raises NotImplementedError (stub for US1)."""

    async def compile(self, tour) -> PlatformModel:
        raise NotImplementedError("TourCompilerPort not wired until US1 (T031)")


class FakePIITokenizer:
    """Satisfies PIITokenizerPort. Returns text unchanged (no-op for tests)."""

    def tokenize(self, text: str) -> str:
        return text


class FakeReadOnlyExplorer:
    """Satisfies ReadOnlyExplorerPort. Raises NotImplementedError (stub for US5)."""

    async def explore(self, site_ref: str, tenant_id: str) -> list[dict]:
        raise NotImplementedError("ReadOnlyExplorerPort not wired until US5")


class FakeSkillAnchor:
    """Satisfies SkillAnchorPort. Returns a fake skill_ref (stub for US4)."""

    async def anchor(
        self,
        task_over_model_id: str,
        platform_model_id: str,
        model_version: int,
        promoted_by: int,
    ) -> str:
        raise NotImplementedError("SkillAnchorPort not wired until US4")
