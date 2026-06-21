"""Application-level ports for the Platforms bounded context (T013).

Ports are declared in the application/domain boundary. Adapters live in
infrastructure. The domain depends only on these protocol definitions.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from hermes.platforms.domain.platform_model import PlatformModel
from hermes.platforms.domain.platform_learning_tour import PlatformLearningTour
from hermes.platforms.domain.model_gap import ModelGap, DirectedTeachingRequest


# ---------------------------------------------------------------------------
# Domain exceptions (shared across ports)
# ---------------------------------------------------------------------------


class PlatformModelNotFound(LookupError):
    """No PlatformModel with the given id exists."""


class PlatformTourNotFound(LookupError):
    """No PlatformLearningTour with the given id exists."""


class ModelGapNotFound(LookupError):
    """No ModelGap with the given id exists."""


class CapabilityBindingNotFound(LookupError):
    """No AgentCapabilityBinding with the given id exists."""


# ---------------------------------------------------------------------------
# PlatformModelRegistryPort
# ---------------------------------------------------------------------------


@runtime_checkable
class PlatformModelRegistryPort(Protocol):
    """Persistence port for PlatformModel aggregates."""

    def save(self, model: PlatformModel) -> None:
        """Upsert a PlatformModel (create or update by id+version)."""
        ...

    def get(self, model_id: str, tenant_id: str) -> PlatformModel:
        """Return the model or raise PlatformModelNotFound."""
        ...

    def list_by_tenant(self, tenant_id: str) -> list[PlatformModel]:
        """Return all models for the tenant."""
        ...

    def save_tour(self, tour: PlatformLearningTour) -> None:
        """Upsert a PlatformLearningTour."""
        ...

    def get_tour(self, tour_id: str) -> PlatformLearningTour:
        """Return the tour or raise PlatformTourNotFound."""
        ...

    def save_gap(self, gap: ModelGap) -> None:
        """Upsert a ModelGap."""
        ...

    def get_gap(self, gap_id: str) -> ModelGap:
        """Return the gap or raise ModelGapNotFound."""
        ...

    def list_gaps(self, model_id: str) -> list[ModelGap]:
        """Return all gaps for a model."""
        ...

    def save_teaching_request(self, request: DirectedTeachingRequest) -> None:
        """Upsert a DirectedTeachingRequest."""
        ...


# ---------------------------------------------------------------------------
# CapabilityBindingRepoPort
# ---------------------------------------------------------------------------


@runtime_checkable
class CapabilityBindingRepoPort(Protocol):
    """Persistence port for AgentCapabilityBinding aggregates."""

    def save(self, binding) -> None:
        """Upsert a binding (idempotent by agent_id + capability)."""
        ...

    def get(self, binding_id: str) -> object:
        """Return the binding or raise CapabilityBindingNotFound."""
        ...

    def list_by_agent(self, agent_id: str, tenant_id: str) -> list:
        """Return all active bindings for an agent."""
        ...

    def find_active(
        self, agent_id: str, capability_kind: str, capability_id: str, tenant_id: str
    ) -> object | None:
        """Return the active binding if it exists, else None."""
        ...

    def unbind(
        self, agent_id: str, capability_kind: str, capability_id: str, tenant_id: str
    ) -> bool:
        """Mark the binding as unbound. Returns True if found and changed."""
        ...


# ---------------------------------------------------------------------------
# TourCompilerPort — stub for US1 (not yet implemented in Phase 1+2)
# ---------------------------------------------------------------------------


@runtime_checkable
class TourCompilerPort(Protocol):
    """Compiles a closed PlatformLearningTour into a signed PlatformModel.

    Implemented in US1 (T031). This port is declared here for dependency
    inversion; the infrastructure stub raises NotImplementedError.
    """

    async def compile(self, tour: PlatformLearningTour) -> PlatformModel:
        """Compile tour → signed model. Raises NotImplementedError until US1."""
        ...


# ---------------------------------------------------------------------------
# ModelPortionSelectorPort — stub for US2
# ---------------------------------------------------------------------------


@runtime_checkable
class ModelPortionSelectorPort(Protocol):
    """Selects the relevant portion of a PlatformModel for a given task.

    Implemented in US2 (T045). Stub raises NotImplementedError.
    """

    def select(self, model: PlatformModel, task_description: str) -> dict:
        """Return the injected portion dict for the task. Stub until US2."""
        ...


# ---------------------------------------------------------------------------
# PIITokenizerPort — reused from existing infra (Principio III)
# ---------------------------------------------------------------------------


@runtime_checkable
class PIITokenizerPort(Protocol):
    """Tokenizes PII in narration transcripts and DOM payloads BEFORE inference.

    The reverse mapping is NEVER stored in the PlatformModel or logs (SC-008).
    """

    def tokenize(self, text: str) -> str:
        """Replace PII values with opaque tokens. Returns tokenized text."""
        ...


# ---------------------------------------------------------------------------
# ReadOnlyExplorerPort — stub for US5
# ---------------------------------------------------------------------------


@runtime_checkable
class ReadOnlyExplorerPort(Protocol):
    """Autonomous read-only platform explorer (US5, gate F-1).

    CTRL-8: wrapper enforces read-only at the effector level.
    Default-deny: only operates on signed allow-list sites.
    """

    async def explore(self, site_ref: str, tenant_id: str) -> list[dict]:
        """Explore site_ref read-only and return observed area data. Stub until US5."""
        ...


# ---------------------------------------------------------------------------
# SkillAnchorPort — stub for US4
# ---------------------------------------------------------------------------


@runtime_checkable
class SkillAnchorPort(Protocol):
    """Anchors a promoted skill to a PlatformModel id + version (FR-024).

    Implemented in US4 (T059). Stub raises NotImplementedError.
    """

    async def anchor(
        self,
        task_over_model_id: str,
        platform_model_id: str,
        model_version: int,
        promoted_by: int,
    ) -> str:
        """Promote task replay to signed ReplayScript anchored to model. Returns skill_ref."""
        ...
