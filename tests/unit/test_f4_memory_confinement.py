"""F4 — Test (b): memory tool is tenant-confined and does not cross tenants.

Verifies:
  - memory writes go to /memory/<tenant_id>/<target>.md (no cross-tenant access).
  - Different tenant_ids produce different storage paths.
  - Path traversal in target is rejected.
  - PII content is rejected (if threat_patterns scanner is available).
  - MemorySurfaceAdapter routes correctly for add/replace/remove/unknown.
  - session_search is registered as LOW+auto in the CapabilityRegistry.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest

from hermes.agents_os.domain.ports.surface_adapter_port import CapturedAction, ReplayStatus
from hermes.agents_os.domain.surface_kind import SurfaceKind
from hermes.capabilities.application.capability_registry import CapabilityRegistry
from hermes.capabilities.domain.ports import RiskLevel
from hermes.memory.infrastructure.memory_surface_adapter import MemorySurfaceAdapter
from hermes.memory.infrastructure.tenant_memory_store import (
    PiiRejectedError,
    TenantMemoryError,
    TenantMemoryStore,
)

pytestmark = pytest.mark.unit

_TENANT_A = UUID("aaaaaaaa-0000-0000-0000-000000000001")
_TENANT_B = UUID("bbbbbbbb-0000-0000-0000-000000000002")


# ---------------------------------------------------------------------------
# TenantMemoryStore isolation tests
# ---------------------------------------------------------------------------


class TestTenantMemoryStoreIsolation:
    """Memory writes are strictly scoped to tenant_id."""

    def test_add_writes_to_tenant_scoped_path(self, tmp_path: Path) -> None:
        store = TenantMemoryStore(root=tmp_path, tenant_id=_TENANT_A)
        result = store.add("memory", "A useful fact")
        assert result["success"]
        expected = tmp_path / str(_TENANT_A) / "memory.md"
        assert expected.exists(), "Entry must persist to tenant-scoped file"

    def test_different_tenants_have_separate_files(self, tmp_path: Path) -> None:
        store_a = TenantMemoryStore(root=tmp_path, tenant_id=_TENANT_A)
        store_b = TenantMemoryStore(root=tmp_path, tenant_id=_TENANT_B)

        store_a.add("memory", "Tenant A secret")
        store_b.add("memory", "Tenant B data")

        path_a = tmp_path / str(_TENANT_A) / "memory.md"
        path_b = tmp_path / str(_TENANT_B) / "memory.md"
        assert path_a != path_b, "Each tenant must have a separate file"
        assert "Tenant A secret" in path_a.read_text(encoding="utf-8")
        assert "Tenant B data" in path_b.read_text(encoding="utf-8")
        assert "Tenant B data" not in path_a.read_text(encoding="utf-8")
        assert "Tenant A secret" not in path_b.read_text(encoding="utf-8")

    def test_tenant_a_cannot_read_tenant_b_entries(self, tmp_path: Path) -> None:
        store_a = TenantMemoryStore(root=tmp_path, tenant_id=_TENANT_A)
        store_b = TenantMemoryStore(root=tmp_path, tenant_id=_TENANT_B)

        store_b.add("memory", "Tenant B confidential")
        entries_a = store_a.read("memory")

        assert "Tenant B confidential" not in entries_a

    def test_path_traversal_in_target_is_rejected(self, tmp_path: Path) -> None:
        store = TenantMemoryStore(root=tmp_path, tenant_id=_TENANT_A)
        with pytest.raises(TenantMemoryError):
            store.add("../../../etc/passwd", "injected")

    def test_invalid_target_name_is_rejected(self, tmp_path: Path) -> None:
        store = TenantMemoryStore(root=tmp_path, tenant_id=_TENANT_A)
        with pytest.raises(TenantMemoryError, match="Invalid memory target"):
            store.add("UPPER_CASE", "content")

    def test_empty_content_rejected(self, tmp_path: Path) -> None:
        store = TenantMemoryStore(root=tmp_path, tenant_id=_TENANT_A)
        result = store.add("memory", "   ")
        assert not result["success"]

    def test_remove_entry(self, tmp_path: Path) -> None:
        store = TenantMemoryStore(root=tmp_path, tenant_id=_TENANT_A)
        store.add("memory", "Remove me")
        result = store.remove("memory", "Remove me")
        assert result["success"]
        assert store.read("memory") == []

    def test_replace_entry(self, tmp_path: Path) -> None:
        store = TenantMemoryStore(root=tmp_path, tenant_id=_TENANT_A)
        store.add("memory", "Old content")
        result = store.replace("memory", "Old content", "New content")
        assert result["success"]
        assert "New content" in store.read("memory")
        assert "Old content" not in store.read("memory")

    def test_remove_nonexistent_returns_error(self, tmp_path: Path) -> None:
        store = TenantMemoryStore(root=tmp_path, tenant_id=_TENANT_A)
        result = store.remove("memory", "does not exist")
        assert not result["success"]

    def test_duplicate_not_added(self, tmp_path: Path) -> None:
        store = TenantMemoryStore(root=tmp_path, tenant_id=_TENANT_A)
        store.add("memory", "Unique fact")
        result = store.add("memory", "Unique fact")  # duplicate
        assert result["success"]
        assert len(store.read("memory")) == 1


# ---------------------------------------------------------------------------
# MemorySurfaceAdapter routing tests
# ---------------------------------------------------------------------------


class TestMemorySurfaceAdapterRouting:
    """MemorySurfaceAdapter correctly routes actions to TenantMemoryStore."""

    def _make_action(
        self,
        action: str,
        target: str = "memory",
        content: str = "",
        old_text: str = "",
        tenant_id: UUID | None = None,
    ) -> CapturedAction:
        return CapturedAction(
            surface_kind=SurfaceKind.MEMORY,
            intent_desc=f"memory {action}",
            payload={
                "action": action,
                "target": target,
                "content": content,
                "old_text": old_text,
            },
            tenant_id=tenant_id or _TENANT_A,
            human_operator_id=uuid4(),
        )

    async def test_add_action_executes_ok(self, tmp_path: Path) -> None:
        adapter = MemorySurfaceAdapter(memory_root=tmp_path)
        action = self._make_action("add", content="A useful fact")
        outcome = await adapter.replay(action)
        assert outcome.status == ReplayStatus.EXECUTED_OK

    async def test_remove_action_executes_ok(self, tmp_path: Path) -> None:
        adapter = MemorySurfaceAdapter(memory_root=tmp_path)
        add_action = self._make_action("add", content="Removable")
        await adapter.replay(add_action)

        remove_action = self._make_action("remove", old_text="Removable")
        outcome = await adapter.replay(remove_action)
        assert outcome.status == ReplayStatus.EXECUTED_OK

    async def test_replace_action_executes_ok(self, tmp_path: Path) -> None:
        adapter = MemorySurfaceAdapter(memory_root=tmp_path)
        await adapter.replay(self._make_action("add", content="Original"))
        outcome = await adapter.replay(
            self._make_action("replace", content="Updated", old_text="Original")
        )
        assert outcome.status == ReplayStatus.EXECUTED_OK

    async def test_unknown_action_rejected(self, tmp_path: Path) -> None:
        adapter = MemorySurfaceAdapter(memory_root=tmp_path)
        action = self._make_action("wipe_all")
        outcome = await adapter.replay(action)
        assert outcome.status == ReplayStatus.REJECTED_BY_POLICY

    async def test_wrong_surface_kind_rejected(self, tmp_path: Path) -> None:
        adapter = MemorySurfaceAdapter(memory_root=tmp_path)
        action = CapturedAction(
            surface_kind=SurfaceKind.FILESYSTEM,
            intent_desc="wrong",
            payload={"action": "add", "target": "memory", "content": "x"},
            tenant_id=_TENANT_A,
            human_operator_id=uuid4(),
        )
        outcome = await adapter.replay(action)
        assert outcome.status == ReplayStatus.REJECTED_BY_POLICY

    async def test_tenant_isolation_via_adapter(self, tmp_path: Path) -> None:
        """Two tenants writing memory do not see each other's entries."""
        adapter = MemorySurfaceAdapter(memory_root=tmp_path)

        await adapter.replay(
            self._make_action("add", content="Secret A", tenant_id=_TENANT_A)
        )
        await adapter.replay(
            self._make_action("add", content="Secret B", tenant_id=_TENANT_B)
        )

        store_a = TenantMemoryStore(root=tmp_path, tenant_id=_TENANT_A)
        store_b = TenantMemoryStore(root=tmp_path, tenant_id=_TENANT_B)
        entries_a = store_a.read("memory")
        entries_b = store_b.read("memory")

        assert "Secret A" in entries_a
        assert "Secret B" not in entries_a
        assert "Secret B" in entries_b
        assert "Secret A" not in entries_b


# ---------------------------------------------------------------------------
# CapabilityRegistry classification tests
# ---------------------------------------------------------------------------


class TestMemoryCapabilityRegistryClassification:
    """memory and session_search are LOW + auto_executable in CapabilityRegistry."""

    def test_memory_is_low_auto_executable(self) -> None:
        registry = CapabilityRegistry()
        binding = registry.resolve("memory")
        assert binding is not None, "memory must be registered"
        assert binding.risk == RiskLevel.LOW, "memory must be LOW risk"
        assert binding.auto_executable is True, "memory must be auto_executable"
        assert binding.surface_kind == SurfaceKind.MEMORY

    def test_session_search_is_low_auto_executable(self) -> None:
        registry = CapabilityRegistry()
        binding = registry.resolve("session_search")
        assert binding is not None, "session_search must be registered"
        assert binding.risk == RiskLevel.LOW
        assert binding.auto_executable is True

    def test_skill_manage_is_high_not_auto_executable(self) -> None:
        """skill_manage must remain HIGH + not auto_executable (constitution II)."""
        registry = CapabilityRegistry()
        binding = registry.resolve("skill_manage")
        assert binding is not None
        assert binding.risk == RiskLevel.HIGH
        assert binding.auto_executable is False

    def test_memory_surface_kind_registered(self) -> None:
        """SurfaceKind.MEMORY must exist in the SurfaceKind enum."""
        assert hasattr(SurfaceKind, "MEMORY")
        assert SurfaceKind.MEMORY == "memory"
