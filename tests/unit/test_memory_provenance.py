"""Tests for per-agent memory provenance (F4 follow-up).

Covers:
  (a) TenantMemoryStore:
      - add with agent_id persists provenance
      - _load tolerates v1 plain-string entries (backward-compat)
      - dedup still works across v1/v2 entries
      - remove/replace still match on content (not JSON structure)
      - read() returns plain content strings (compat)
      - read_with_provenance() returns dicts with agent_id
  (b) MemorySurfaceAdapter:
      - _provenance_agent_id in payload reaches store.add
      - missing _provenance_agent_id falls back to "unknown" (fail-soft)
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from hermes.agents_os.domain.ports.surface_adapter_port import CapturedAction, ReplayStatus
from hermes.agents_os.domain.surface_kind import SurfaceKind
from hermes.memory.infrastructure.memory_surface_adapter import MemorySurfaceAdapter
from hermes.memory.infrastructure.tenant_memory_store import (
    TenantMemoryStore,
    _ENTRY_DELIMITER,
    _LEGACY_AGENT_ID,
)

pytestmark = pytest.mark.unit

_TENANT = UUID("aaaaaaaa-0000-0000-0000-000000000001")
_AGENT_ALPHA = "agent-alpha"
_AGENT_BETA = "agent-beta"


# ---------------------------------------------------------------------------
# (a) TenantMemoryStore provenance
# ---------------------------------------------------------------------------


class TestTenantMemoryStoreProvenance:
    """add with agent_id persists structured entries; read remains backward-compat."""

    def test_add_with_agent_id_stores_provenance(self, tmp_path: Path) -> None:
        store = TenantMemoryStore(root=tmp_path, tenant_id=_TENANT)
        store.add("memory", "Fact A", agent_id=_AGENT_ALPHA)

        entries = store.read_with_provenance("memory")
        assert len(entries) == 1
        assert entries[0]["content"] == "Fact A"
        assert entries[0]["agent_id"] == _AGENT_ALPHA

    def test_add_without_agent_id_defaults_to_unknown(self, tmp_path: Path) -> None:
        store = TenantMemoryStore(root=tmp_path, tenant_id=_TENANT)
        store.add("memory", "No agent context")

        entries = store.read_with_provenance("memory")
        assert entries[0]["agent_id"] == "unknown"

    def test_read_returns_content_strings_only(self, tmp_path: Path) -> None:
        store = TenantMemoryStore(root=tmp_path, tenant_id=_TENANT)
        store.add("memory", "Plain text", agent_id=_AGENT_ALPHA)

        result = store.read("memory")
        assert result == ["Plain text"]
        assert isinstance(result[0], str)

    def test_multiple_agents_entries_tracked_separately(self, tmp_path: Path) -> None:
        store = TenantMemoryStore(root=tmp_path, tenant_id=_TENANT)
        store.add("memory", "Alpha wrote this", agent_id=_AGENT_ALPHA)
        store.add("memory", "Beta wrote this", agent_id=_AGENT_BETA)

        entries = store.read_with_provenance("memory")
        assert len(entries) == 2
        by_content = {e["content"]: e["agent_id"] for e in entries}
        assert by_content["Alpha wrote this"] == _AGENT_ALPHA
        assert by_content["Beta wrote this"] == _AGENT_BETA

    def test_provenance_entry_has_timestamp(self, tmp_path: Path) -> None:
        store = TenantMemoryStore(root=tmp_path, tenant_id=_TENANT)
        store.add("memory", "timestamped", agent_id=_AGENT_ALPHA)

        entry = store.read_with_provenance("memory")[0]
        assert entry.get("ts") is not None, "Provenance entry must include a timestamp"

    # ------------------------------------------------------------------
    # Backward-compat: v1 plain-string entries load as "legacy"
    # ------------------------------------------------------------------

    def test_load_tolerates_v1_plain_string_entries(self, tmp_path: Path) -> None:
        """Pre-provenance files (plain strings) are read as agent_id='legacy'."""
        tenant_dir = tmp_path / str(_TENANT)
        tenant_dir.mkdir(parents=True)
        (tenant_dir / "memory.md").write_text(
            "Old plain entry", encoding="utf-8"
        )

        store = TenantMemoryStore(root=tmp_path, tenant_id=_TENANT)
        entries = store.read_with_provenance("memory")
        assert len(entries) == 1
        assert entries[0]["content"] == "Old plain entry"
        assert entries[0]["agent_id"] == _LEGACY_AGENT_ID

    def test_load_mixed_v1_and_v2_entries(self, tmp_path: Path) -> None:
        """Files with both v1 and v2 entries (mid-migration) load without error."""
        v2_entry = json.dumps(
            {"content": "New structured", "agent_id": _AGENT_ALPHA, "ts": "2026-01-01T00:00:00+00:00"}
        )
        tenant_dir = tmp_path / str(_TENANT)
        tenant_dir.mkdir(parents=True)
        raw = "Old plain entry" + _ENTRY_DELIMITER + v2_entry
        (tenant_dir / "memory.md").write_text(raw, encoding="utf-8")

        store = TenantMemoryStore(root=tmp_path, tenant_id=_TENANT)
        entries = store.read_with_provenance("memory")
        assert len(entries) == 2
        assert entries[0]["content"] == "Old plain entry"
        assert entries[0]["agent_id"] == _LEGACY_AGENT_ID
        assert entries[1]["content"] == "New structured"
        assert entries[1]["agent_id"] == _AGENT_ALPHA

    def test_read_compat_from_v1_file_returns_strings(self, tmp_path: Path) -> None:
        tenant_dir = tmp_path / str(_TENANT)
        tenant_dir.mkdir(parents=True)
        (tenant_dir / "memory.md").write_text("legacy entry", encoding="utf-8")

        store = TenantMemoryStore(root=tmp_path, tenant_id=_TENANT)
        result = store.read("memory")
        assert result == ["legacy entry"]

    # ------------------------------------------------------------------
    # Dedup and mutators still work over structured entries
    # ------------------------------------------------------------------

    def test_dedup_prevents_duplicate_content_regardless_of_agent(
        self, tmp_path: Path
    ) -> None:
        store = TenantMemoryStore(root=tmp_path, tenant_id=_TENANT)
        store.add("memory", "Shared fact", agent_id=_AGENT_ALPHA)
        result = store.add("memory", "Shared fact", agent_id=_AGENT_BETA)

        assert result["success"]
        assert "already exists" in result.get("message", "")
        assert len(store.read("memory")) == 1

    def test_remove_matches_on_content_substring(self, tmp_path: Path) -> None:
        store = TenantMemoryStore(root=tmp_path, tenant_id=_TENANT)
        store.add("memory", "Remove me please", agent_id=_AGENT_ALPHA)
        result = store.remove("memory", "Remove me")
        assert result["success"]
        assert store.read("memory") == []

    def test_replace_matches_on_content_substring_and_updates_agent(
        self, tmp_path: Path
    ) -> None:
        store = TenantMemoryStore(root=tmp_path, tenant_id=_TENANT)
        store.add("memory", "Old value", agent_id=_AGENT_ALPHA)
        result = store.replace("memory", "Old value", "New value", agent_id=_AGENT_BETA)
        assert result["success"]

        entries = store.read_with_provenance("memory")
        assert entries[0]["content"] == "New value"
        assert entries[0]["agent_id"] == _AGENT_BETA

    def test_read_with_provenance_after_replace_returns_new_agent(
        self, tmp_path: Path
    ) -> None:
        store = TenantMemoryStore(root=tmp_path, tenant_id=_TENANT)
        store.add("memory", "Initial", agent_id=_AGENT_ALPHA)
        store.replace("memory", "Initial", "Revised", agent_id=_AGENT_BETA)

        entries = store.read_with_provenance("memory")
        assert entries[0]["agent_id"] == _AGENT_BETA

    def test_remove_v1_entry_by_content(self, tmp_path: Path) -> None:
        """remove() works on legacy entries loaded from v1 files."""
        tenant_dir = tmp_path / str(_TENANT)
        tenant_dir.mkdir(parents=True)
        (tenant_dir / "memory.md").write_text("old note", encoding="utf-8")

        store = TenantMemoryStore(root=tmp_path, tenant_id=_TENANT)
        result = store.remove("memory", "old note")
        assert result["success"]
        assert store.read("memory") == []


# ---------------------------------------------------------------------------
# (b) MemorySurfaceAdapter forwards agent_id from payload
# ---------------------------------------------------------------------------


class TestMemorySurfaceAdapterProvenance:
    """_provenance_agent_id in the payload reaches TenantMemoryStore.add."""

    def _make_action(
        self,
        action: str,
        content: str = "",
        old_text: str = "",
        provenance_agent_id: str | None = None,
        tenant_id: UUID | None = None,
    ) -> CapturedAction:
        payload: dict = {
            "action": action,
            "target": "memory",
            "content": content,
            "old_text": old_text,
        }
        if provenance_agent_id is not None:
            payload["_provenance_agent_id"] = provenance_agent_id
        return CapturedAction(
            surface_kind=SurfaceKind.MEMORY,
            intent_desc=f"memory {action}",
            payload=payload,
            tenant_id=tenant_id or _TENANT,
            human_operator_id=uuid4(),
        )

    async def test_add_with_provenance_stores_agent_id(self, tmp_path: Path) -> None:
        adapter = MemorySurfaceAdapter(memory_root=tmp_path)
        action = self._make_action("add", content="Agent fact", provenance_agent_id=_AGENT_ALPHA)
        outcome = await adapter.replay(action)
        assert outcome.status == ReplayStatus.EXECUTED_OK

        store = TenantMemoryStore(root=tmp_path, tenant_id=_TENANT)
        entries = store.read_with_provenance("memory")
        assert entries[0]["agent_id"] == _AGENT_ALPHA

    async def test_add_without_provenance_defaults_to_unknown(self, tmp_path: Path) -> None:
        adapter = MemorySurfaceAdapter(memory_root=tmp_path)
        action = self._make_action("add", content="No provenance")
        outcome = await adapter.replay(action)
        assert outcome.status == ReplayStatus.EXECUTED_OK

        store = TenantMemoryStore(root=tmp_path, tenant_id=_TENANT)
        entries = store.read_with_provenance("memory")
        assert entries[0]["agent_id"] == "unknown"

    async def test_replace_with_provenance_updates_agent_id(self, tmp_path: Path) -> None:
        adapter = MemorySurfaceAdapter(memory_root=tmp_path)
        await adapter.replay(
            self._make_action("add", content="Original", provenance_agent_id=_AGENT_ALPHA)
        )
        await adapter.replay(
            self._make_action(
                "replace",
                content="Replaced",
                old_text="Original",
                provenance_agent_id=_AGENT_BETA,
            )
        )

        store = TenantMemoryStore(root=tmp_path, tenant_id=_TENANT)
        entries = store.read_with_provenance("memory")
        assert entries[0]["content"] == "Replaced"
        assert entries[0]["agent_id"] == _AGENT_BETA

    async def test_provenance_does_not_block_write_when_missing(
        self, tmp_path: Path
    ) -> None:
        """Missing _provenance_agent_id must never block the write (fail-soft contract)."""
        adapter = MemorySurfaceAdapter(memory_root=tmp_path)
        action = self._make_action("add", content="Critical memory", provenance_agent_id=None)
        outcome = await adapter.replay(action)
        assert outcome.status == ReplayStatus.EXECUTED_OK

        store = TenantMemoryStore(root=tmp_path, tenant_id=_TENANT)
        assert "Critical memory" in store.read("memory")
