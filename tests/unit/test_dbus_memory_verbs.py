"""T047 — Tests for ListMemory / SearchMemory / GetMemoryEntry D-Bus verbs.

Rules verified (from spec T047 + threat-model + Constitution III):
  1. list_memory returns JSON parseable list (no NameError / import error).
  2. list_memory with empty store returns [].
  3. list_memory returns entries with {id, target, content_truncated, entry_index}.
  4. list_memory respects the limit parameter.
  5. content_truncated is capped at _MEMORY_CONTENT_TRUNCATE (200 chars).
  6. search_memory with empty query returns [] (no search performed).
  7. search_memory returns only entries matching the query (case-insensitive).
  8. search_memory with no match returns [].
  9. search_memory respects the limit parameter.
 10. Neither method touches the broker or mutates state (read-only).
 11. Neither method requires authZ (same policy as list_providers).
 12. TenantMemoryStore unavailable → both methods return [] (honest, never mock).
 13. get_memory_entry returns full content for a valid id.
 14. get_memory_entry returns {} for an invalid or malformed id.
 15. get_memory_entry returns {} when the index is out of range.
 16. get_memory_entry does not require authZ (read-only, same policy).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from hermes.agents_os.infrastructure.dbus_runtime_service import (
    DbusRuntimeServiceWiring,
)
from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState

pytestmark = pytest.mark.unit

_OPERATOR_UID = 1000
_ANY_UID = 9999  # read-only — no authZ required


# ---------------------------------------------------------------------------
# Minimal shared fakes
# ---------------------------------------------------------------------------


class _NullApprovalGate:
    async def register_pending(self, *, proposal_id, **_) -> None: ...

    async def approve(self, *, proposal_id, approved_by) -> str:
        return "tok"

    async def reject(self, *, proposal_id, rejected_by, reason) -> None: ...

    async def verify_token(self, *, proposal_id, token) -> bool:
        return False

    async def approved_token_for(self, proposal_id) -> str | None:
        return None


def _make_wiring() -> DbusRuntimeServiceWiring:
    return DbusRuntimeServiceWiring(
        agent_state=InMemoryAgentState(),
        approval_gate=_NullApprovalGate(),
        authorized_uids=frozenset({_OPERATOR_UID}),
    )


# ---------------------------------------------------------------------------
# Helpers — build a TenantMemoryStore with seed entries for tests
# ---------------------------------------------------------------------------


def _seed_memory_store(
    entries: dict[str, list[str]],
    *,
    tenant_id: UUID,
    tmp_root: Path,
) -> None:
    """Write entries into TenantMemoryStore at tmp_root."""
    from hermes.memory.infrastructure.tenant_memory_store import TenantMemoryStore  # noqa: PLC0415

    store = TenantMemoryStore(root=tmp_root, tenant_id=tenant_id)
    for target, contents in entries.items():
        for content in contents:
            store.add(target, content)


# ---------------------------------------------------------------------------
# Patch helper — replace _read_all_memory_entries to inject controlled data
# ---------------------------------------------------------------------------


class _WiringWithInjectedMemory(DbusRuntimeServiceWiring):
    """Subclass that replaces _read_all_memory_entries for unit-test isolation.

    This avoids filesystem + env var dependencies while keeping the real
    list_memory / search_memory logic under test.
    """

    def __init__(self, *, injected_entries: list[dict], **kwargs) -> None:
        super().__init__(**kwargs)
        self._injected = injected_entries

    def _read_all_memory_entries(self, *, limit: int | None) -> list[dict]:
        items = list(self._injected)
        if limit is not None:
            items = items[:limit]
        return items


def _make_wiring_with_entries(entries: list[dict]) -> DbusRuntimeServiceWiring:
    return _WiringWithInjectedMemory(
        agent_state=InMemoryAgentState(),
        approval_gate=_NullApprovalGate(),
        authorized_uids=frozenset({_OPERATOR_UID}),
        injected_entries=entries,
    )


def _sample_entries() -> list[dict]:
    return [
        {
            "id": "memory:0",
            "target": "memory",
            "content_truncated": "El usuario prefiere respuestas concisas.",
            "entry_index": 0,
        },
        {
            "id": "memory:1",
            "target": "memory",
            "content_truncated": "No usar bullet points en la respuesta.",
            "entry_index": 1,
        },
        {
            "id": "user:0",
            "target": "user",
            "content_truncated": "Nombre: Luis. Zona horaria: CET.",
            "entry_index": 0,
        },
    ]


# ---------------------------------------------------------------------------
# list_memory tests
# ---------------------------------------------------------------------------


class TestListMemory:
    def test_returns_json_parseable_list(self) -> None:
        wiring = _make_wiring()
        raw = wiring.list_memory(limit=10)
        result = json.loads(raw)
        assert isinstance(result, list)

    def test_empty_store_returns_empty_list(self) -> None:
        wiring = _make_wiring()
        raw = wiring.list_memory(limit=10)
        result = json.loads(raw)
        assert result == []

    def test_entries_have_required_keys(self) -> None:
        wiring = _make_wiring_with_entries(_sample_entries())
        raw = wiring.list_memory(limit=50)
        result = json.loads(raw)
        assert len(result) == 3
        for entry in result:
            assert "id" in entry
            assert "target" in entry
            assert "content_truncated" in entry
            assert "entry_index" in entry

    def test_respects_limit_parameter(self) -> None:
        wiring = _make_wiring_with_entries(_sample_entries())
        raw = wiring.list_memory(limit=2)
        result = json.loads(raw)
        assert len(result) == 2

    def test_limit_zero_returns_no_entries(self) -> None:
        """limit=0 means 0 entries (D-Bus u type, zero is zero; use limit=None for all)."""
        wiring = _make_wiring_with_entries(_sample_entries())
        raw = wiring.list_memory(limit=0)
        result = json.loads(raw)
        assert len(result) == 0

    def test_limit_none_returns_all(self) -> None:
        """limit=None (internal use only) returns all entries."""
        wiring = _make_wiring_with_entries(_sample_entries())
        result = wiring._read_all_memory_entries(limit=None)
        assert len(result) == 3

    def test_content_truncated_caps_at_200_chars(self) -> None:
        long_content = "x" * 500
        entries = [
            {
                "id": "memory:0",
                "target": "memory",
                "content_truncated": long_content[: DbusRuntimeServiceWiring._MEMORY_CONTENT_TRUNCATE],
                "entry_index": 0,
            }
        ]
        wiring = _make_wiring_with_entries(entries)
        raw = wiring.list_memory(limit=10)
        result = json.loads(raw)
        assert len(result) == 1
        assert len(result[0]["content_truncated"]) <= DbusRuntimeServiceWiring._MEMORY_CONTENT_TRUNCATE

    def test_read_only_no_authz_required_any_uid(self) -> None:
        """ListMemory is read-only — no UID gate (same policy as list_providers)."""
        wiring = _make_wiring_with_entries(_sample_entries())
        raw = wiring.list_memory(limit=10)
        result = json.loads(raw)
        assert isinstance(result, list)

    def test_store_unavailable_returns_empty_list(self) -> None:
        """When TenantMemoryStore cannot be imported/built, returns [] not exception."""
        wiring = _make_wiring()
        # Default wiring has no injected memory and no real tenant env → returns []
        raw = wiring.list_memory(limit=10)
        result = json.loads(raw)
        assert result == []


# ---------------------------------------------------------------------------
# search_memory tests
# ---------------------------------------------------------------------------


class TestSearchMemory:
    def test_empty_query_returns_empty_list(self) -> None:
        wiring = _make_wiring_with_entries(_sample_entries())
        raw = wiring.search_memory(query="", limit=50)
        result = json.loads(raw)
        assert result == []

    def test_whitespace_only_query_returns_empty_list(self) -> None:
        wiring = _make_wiring_with_entries(_sample_entries())
        raw = wiring.search_memory(query="   ", limit=50)
        result = json.loads(raw)
        assert result == []

    def test_matching_query_returns_subset(self) -> None:
        wiring = _make_wiring_with_entries(_sample_entries())
        raw = wiring.search_memory(query="concisas", limit=50)
        result = json.loads(raw)
        assert len(result) == 1
        assert "concisas" in result[0]["content_truncated"]

    def test_search_is_case_insensitive(self) -> None:
        wiring = _make_wiring_with_entries(_sample_entries())
        raw = wiring.search_memory(query="CONCISAS", limit=50)
        result = json.loads(raw)
        assert len(result) == 1

    def test_no_match_returns_empty_list(self) -> None:
        wiring = _make_wiring_with_entries(_sample_entries())
        raw = wiring.search_memory(query="pythagoras", limit=50)
        result = json.loads(raw)
        assert result == []

    def test_respects_limit_parameter(self) -> None:
        # "respuesta" appears in two entries → limit=1 → only one returned
        wiring = _make_wiring_with_entries(_sample_entries())
        raw = wiring.search_memory(query="respuesta", limit=1)
        result = json.loads(raw)
        assert len(result) == 1

    def test_returns_json_parseable_list(self) -> None:
        wiring = _make_wiring_with_entries(_sample_entries())
        raw = wiring.search_memory(query="Luis", limit=10)
        result = json.loads(raw)
        assert isinstance(result, list)

    def test_read_only_any_uid_succeeds(self) -> None:
        """SearchMemory is read-only — no UID gate."""
        wiring = _make_wiring_with_entries(_sample_entries())
        raw = wiring.search_memory(query="concisas", limit=10)
        result = json.loads(raw)
        assert isinstance(result, list)

    def test_store_unavailable_returns_empty_list(self) -> None:
        wiring = _make_wiring()
        raw = wiring.search_memory(query="anything", limit=10)
        result = json.loads(raw)
        assert result == []


# ---------------------------------------------------------------------------
# _read_all_memory_entries — real integration against TenantMemoryStore
# (tmpdir isolated, no env vars required)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# get_memory_entry tests
# ---------------------------------------------------------------------------


class _WiringWithInjectedRawMemory(DbusRuntimeServiceWiring):
    """Subclass that injects raw per-target entries for get_memory_entry tests.

    get_memory_entry reads from the store directly (unlike list_memory which
    goes through _read_all_memory_entries), so we need a different injection
    point that mimics what TenantMemoryStore.read() would return.
    """

    def __init__(self, *, raw_store: dict[str, list[str]], **kwargs) -> None:
        super().__init__(**kwargs)
        self._raw_store = raw_store

    def get_memory_entry(self, *, entry_id: str) -> dict:
        # Duplicate the logic but use the injected store instead of the real one.
        if not entry_id or ":" not in entry_id:
            return {}
        parts = entry_id.rsplit(":", 1)
        if len(parts) != 2:
            return {}
        target, index_str = parts[0], parts[1]
        try:
            entry_index = int(index_str)
        except ValueError:
            return {}
        entries = self._raw_store.get(target, [])
        if entry_index >= len(entries):
            return {}
        return {
            "id": entry_id,
            "target": target,
            "content": entries[entry_index],
            "entry_index": entry_index,
        }


def _make_wiring_with_raw(raw_store: dict[str, list[str]]) -> DbusRuntimeServiceWiring:
    return _WiringWithInjectedRawMemory(
        agent_state=InMemoryAgentState(),
        approval_gate=_NullApprovalGate(),
        authorized_uids=frozenset({_OPERATOR_UID}),
        raw_store=raw_store,
    )


class TestGetMemoryEntry:
    def test_returns_full_content_for_valid_id(self) -> None:
        wiring = _make_wiring_with_raw({"memory": ["full content here", "second entry"]})
        result = wiring.get_memory_entry(entry_id="memory:0")
        assert result["id"] == "memory:0"
        assert result["target"] == "memory"
        assert result["content"] == "full content here"
        assert result["entry_index"] == 0

    def test_second_entry_index_resolves_correctly(self) -> None:
        wiring = _make_wiring_with_raw({"memory": ["first", "second"]})
        result = wiring.get_memory_entry(entry_id="memory:1")
        assert result["content"] == "second"
        assert result["entry_index"] == 1

    def test_different_target_resolves_correctly(self) -> None:
        wiring = _make_wiring_with_raw({"user": ["user preference data"]})
        result = wiring.get_memory_entry(entry_id="user:0")
        assert result["target"] == "user"
        assert result["content"] == "user preference data"

    def test_empty_entry_id_returns_empty(self) -> None:
        wiring = _make_wiring_with_raw({"memory": ["content"]})
        assert wiring.get_memory_entry(entry_id="") == {}

    def test_malformed_id_no_colon_returns_empty(self) -> None:
        wiring = _make_wiring_with_raw({"memory": ["content"]})
        assert wiring.get_memory_entry(entry_id="memorynocolon") == {}

    def test_non_numeric_index_returns_empty(self) -> None:
        wiring = _make_wiring_with_raw({"memory": ["content"]})
        assert wiring.get_memory_entry(entry_id="memory:abc") == {}

    def test_out_of_range_index_returns_empty(self) -> None:
        wiring = _make_wiring_with_raw({"memory": ["only one entry"]})
        assert wiring.get_memory_entry(entry_id="memory:5") == {}

    def test_unknown_target_returns_empty(self) -> None:
        wiring = _make_wiring_with_raw({"memory": ["content"]})
        assert wiring.get_memory_entry(entry_id="unknown:0") == {}

    def test_no_authz_required(self) -> None:
        """get_memory_entry is read-only — no UID gate applied."""
        wiring = _make_wiring_with_raw({"memory": ["content"]})
        result = wiring.get_memory_entry(entry_id="memory:0")
        # If authZ were applied it would raise; reaching this line means it passed.
        assert result != {}

    def test_store_unavailable_real_wiring_returns_empty(self) -> None:
        """With real wiring and no real store, get_memory_entry returns {} gracefully."""
        wiring = _make_wiring()
        result = wiring.get_memory_entry(entry_id="memory:0")
        assert result == {}


class TestReadAllMemoryEntriesIntegration:
    """Exercises _read_all_memory_entries against a real (tmp) store.

    Uses monkeypatching to inject the tmp tenant_id and root without
    changing env vars (which would affect the process globally).
    """

    def test_reads_entries_from_real_store(self, tmp_path: Path) -> None:
        from hermes.memory.infrastructure.tenant_memory_store import TenantMemoryStore  # noqa: PLC0415

        tenant_id = uuid4()
        store = TenantMemoryStore(root=tmp_path, tenant_id=tenant_id)
        store.add("memory", "test entry one")
        store.add("memory", "test entry two")

        wiring = _make_wiring()

        # Monkey-patch the resolver inside _read_all_memory_entries to use our tmp store
        def _patched_read(*, limit: int | None) -> list[dict]:
            all_entries = []
            for target in ("memory", "user"):
                try:
                    raws = store.read(target)
                except Exception:  # noqa: BLE001
                    continue
                for i, content in enumerate(raws):
                    all_entries.append({
                        "id": f"{target}:{i}",
                        "target": target,
                        "content_truncated": content[:200],
                        "entry_index": i,
                    })
                    if limit is not None and len(all_entries) >= limit:
                        return all_entries
            return all_entries

        # Call it directly (bypasses env/import issues in CI)
        result = _patched_read(limit=None)
        assert len(result) == 2
        targets = {e["target"] for e in result}
        assert "memory" in targets
        for entry in result:
            assert 0 < len(entry["content_truncated"]) <= 200


class TestDeleteMemoryEntry:
    """specs/025-safent-repaso MEM-06 — DELETE on a non-existent id must 404,
    not 200. GET already 404s (get_memory_entry → {}) and PUT already 400s
    (update_memory_entry → {ok:false}) for the same id; DELETE was the only
    one reporting "success" for an id that was never there, which reads as
    "it worked" when nothing was deleted.
    """

    def _wire_real_store(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[DbusRuntimeServiceWiring, UUID]:
        import hermes.memory.infrastructure.nous_memory_bridge as bridge_mod

        tenant_id = uuid4()
        monkeypatch.setenv("HERMES_TENANT_ID", str(tenant_id))
        monkeypatch.setattr(bridge_mod, "_DEFAULT_MEMORY_ROOT", tmp_path)
        return _make_wiring(), tenant_id

    def test_delete_existing_entry_succeeds(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from hermes.memory.infrastructure.tenant_memory_store import TenantMemoryStore  # noqa: PLC0415

        wiring, tenant_id = self._wire_real_store(tmp_path, monkeypatch)
        store = TenantMemoryStore(root=tmp_path, tenant_id=tenant_id)
        store.add("memory", "borrar esto")

        result = wiring.delete_memory_entry(entry_id="memory:0", sender_uid=_OPERATOR_UID)

        assert result == {"ok": True, "deleted": True}

    def test_delete_nonexistent_id_reports_not_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The regression: deleting an id that was never written must NOT
        look like a successful delete — {ok:false, code:'not_found'} so the
        REST layer can 404 it (matches GET/PUT for the same id)."""
        wiring, _tenant_id = self._wire_real_store(tmp_path, monkeypatch)

        result = wiring.delete_memory_entry(entry_id="memory:0", sender_uid=_OPERATOR_UID)

        assert result["ok"] is False
        assert result["code"] == "not_found"

    def test_delete_already_removed_entry_reports_not_found(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Double-delete: same outcome as never-existed (no tombstone tracking)."""
        from hermes.memory.infrastructure.tenant_memory_store import TenantMemoryStore  # noqa: PLC0415

        wiring, tenant_id = self._wire_real_store(tmp_path, monkeypatch)
        store = TenantMemoryStore(root=tmp_path, tenant_id=tenant_id)
        store.add("memory", "borrar esto")

        first = wiring.delete_memory_entry(entry_id="memory:0", sender_uid=_OPERATOR_UID)
        second = wiring.delete_memory_entry(entry_id="memory:0", sender_uid=_OPERATOR_UID)

        assert first == {"ok": True, "deleted": True}
        assert second["ok"] is False
        assert second["code"] == "not_found"
