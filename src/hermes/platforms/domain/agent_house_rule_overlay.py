"""AgentHouseRuleOverlay entity (T015).

A per-agent house rule that overrides the structural shared model (FR-037).

Domain layer — pure Python, zero infra dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass

from hermes.platforms.domain.platform_model import HouseRule


@dataclass(frozen=True, slots=True)
class AgentHouseRuleOverlay:
    """Per-agent rule overlay on a shared PlatformModel.

    Invariants:
    - Specific to (agent_id, platform_model_id) pair.
    - Only affects the owning agent; the structural model is unchanged.
    """

    overlay_id: str
    agent_id: str
    platform_model_id: str
    house_rule: HouseRule

    def __post_init__(self) -> None:
        if not self.overlay_id:
            raise ValueError("AgentHouseRuleOverlay.overlay_id cannot be empty")
        if not self.agent_id:
            raise ValueError("AgentHouseRuleOverlay.agent_id cannot be empty")
        if not self.platform_model_id:
            raise ValueError("AgentHouseRuleOverlay.platform_model_id cannot be empty")
