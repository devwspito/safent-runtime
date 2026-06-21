"""FakeCapabilityRegistry — fake de CapabilityRegistryPort para tests unitarios."""

from __future__ import annotations

from hermes.capabilities.domain.ports import (
    CapabilityBinding,
    CapabilityRegistryPort,
    RiskLevel,
)


class FakeCapabilityRegistry:
    """Fake de CapabilityRegistryPort con bindings scriptables."""

    def __init__(self) -> None:
        self._bindings: dict[str, CapabilityBinding] = {}

    def register(self, binding: CapabilityBinding) -> None:
        """Registra un binding para un tool_name."""
        self._bindings[binding.tool_name] = binding

    def register_low(self, tool_name: str) -> None:
        """Registra un tool como LOW risk (auto-executable)."""
        self.register(
            CapabilityBinding(
                tool_name=tool_name,
                surface_kind=None,
                required_capability=None,
                risk=RiskLevel.LOW,
                auto_executable=True,
            )
        )

    def register_high(self, tool_name: str) -> None:
        """Registra un tool como HIGH risk (requiere HITL)."""
        self.register(
            CapabilityBinding(
                tool_name=tool_name,
                surface_kind=None,
                required_capability=None,
                risk=RiskLevel.HIGH,
                auto_executable=False,
            )
        )

    def resolve(self, tool_name: str) -> CapabilityBinding | None:
        return self._bindings.get(tool_name)


# Satisface CapabilityRegistryPort structural check
assert isinstance(FakeCapabilityRegistry(), CapabilityRegistryPort)
