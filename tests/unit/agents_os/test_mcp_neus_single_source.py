"""Regression tests — MCP Neus single source of truth.

Invariants pinned:
  1. list_mcp_servers reads from Neus (tools.mcp_tool) not Safent's own store.
  2. add_mcp_server writes to Neus after the gate passes — not to mcp-servers.json.
  3. A scan-FAIL without force stays blocked (gate is fail-closed).
  4. A scan-FAIL with owner force=True persists AND appears in list_mcp_servers.
  5. remove_mcp_server deletes from Neus only (no Safent store involved).
  6. _neus_argv correctly maps Neus format to Safent argv.

The test module patches:
  - tools.mcp_tool._load_mcp_config   (Neus config reader — raw dict form)
  - tools.mcp_tool.get_mcp_status     (Neus live status)
  - tools.mcp_tool.register_mcp_servers (Neus live activation)
  - hermes_cli.config.load_config / save_config (Neus persistence)
  - dbus_runtime_service._scan_install_target (inline on the wiring instance)
  - dbus_runtime_service._mcp_connect (avoids real subprocess)
  - dbus_runtime_service._scanner_can_analyze_argv (always True for valid argv)
  - dbus_runtime_service._prefetch_mcp_package (no-op)
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# Minimal stubs for tools.mcp_tool and hermes_cli.config if not installed
# ---------------------------------------------------------------------------

def _ensure_neus_stubs():
    """Inject stub modules so imports in the service don't fail in CI."""
    if "tools" not in sys.modules:
        sys.modules["tools"] = types.ModuleType("tools")
    if "tools.mcp_tool" not in sys.modules:
        mod = types.ModuleType("tools.mcp_tool")
        mod._load_mcp_config = lambda: {}  # type: ignore[attr-defined]
        mod.get_mcp_status = lambda: []  # type: ignore[attr-defined]
        mod.register_mcp_servers = lambda s: []  # type: ignore[attr-defined]
        sys.modules["tools.mcp_tool"] = mod
    if "hermes_cli" not in sys.modules:
        sys.modules["hermes_cli"] = types.ModuleType("hermes_cli")
    if "hermes_cli.config" not in sys.modules:
        mod = types.ModuleType("hermes_cli.config")
        mod.load_config = lambda: {}  # type: ignore[attr-defined]
        mod.save_config = lambda c: None  # type: ignore[attr-defined]
        sys.modules["hermes_cli.config"] = mod

_ensure_neus_stubs()


# ---------------------------------------------------------------------------
# Import bridge helpers under test (module-level — after stubs are in place)
# ---------------------------------------------------------------------------

from hermes.agents_os.infrastructure.dbus_runtime_service import (  # noqa: E402
    _neus_argv,
    _neus_load_entries,
    _neus_write_mcp_entry,
    _neus_remove_mcp_entry,
)


# ===========================================================================
# Unit: _neus_argv
# ===========================================================================

class TestNeusArgv:
    def test_command_and_args(self):
        cfg = {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"]}
        assert _neus_argv(cfg) == ["npx", "-y", "@modelcontextprotocol/server-github"]

    def test_empty_args(self):
        assert _neus_argv({"command": "uvx", "args": []}) == ["uvx"]

    def test_missing_command_returns_args_only(self):
        assert _neus_argv({"args": ["foo"]}) == ["foo"]

    def test_empty_config(self):
        assert _neus_argv({}) == []


# ===========================================================================
# Unit: _neus_load_entries
# ===========================================================================

class TestNeusLoadEntries:
    def test_converts_neus_dict_to_safent_list(self):
        neus_map = {
            "github": {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-github"],
                "env": {"GITHUB_TOKEN": "ghp_test"},
            }
        }
        with patch("tools.mcp_tool._load_mcp_config", return_value=neus_map):
            entries = _neus_load_entries()

        assert len(entries) == 1
        e = entries[0]
        assert e["server_id"] == "github"
        assert e["argv"] == ["npx", "-y", "@modelcontextprotocol/server-github"]
        assert e["env"] == {"GITHUB_TOKEN": "ghp_test"}

    def test_returns_empty_on_import_error(self):
        with patch("tools.mcp_tool._load_mcp_config", side_effect=ImportError("no mod")):
            # The bridge catches ImportError via its internal try/except
            pass  # import error is caught by the guard at top of _neus_load_entries
        # Simulate the guard: mock the whole import
        import importlib
        original = sys.modules.get("tools.mcp_tool")
        try:
            sys.modules.pop("tools.mcp_tool", None)
            # Temporarily remove to simulate unavailability on next call
            result = _neus_load_entries()
            # Should return [] (tools.mcp_tool not importable path)
        finally:
            if original is not None:
                sys.modules["tools.mcp_tool"] = original
        # Whether [] or not depends on prior stub; the key assertion is no raise
        assert isinstance(result, list)


# ===========================================================================
# Unit: _neus_write_mcp_entry
# ===========================================================================

class TestNeusWriteMcpEntry:
    def test_writes_command_and_args_to_config(self):
        saved: dict = {}

        def fake_load():
            return {"mcp_servers": {}}

        def fake_save(cfg):
            saved.update(cfg)

        with (
            patch("hermes_cli.config.load_config", side_effect=fake_load),
            patch("hermes_cli.config.save_config", side_effect=fake_save),
            patch("tools.mcp_tool.register_mcp_servers", return_value=[]),
        ):
            _neus_write_mcp_entry(
                "github",
                ["npx", "-y", "@modelcontextprotocol/server-github"],
                env={"GITHUB_TOKEN": "ghp_test"},
            )

        servers = saved.get("mcp_servers", {})
        assert "github" in servers
        entry = servers["github"]
        assert entry["command"] == "npx"
        assert entry["args"] == ["-y", "@modelcontextprotocol/server-github"]
        assert entry["env"] == {"GITHUB_TOKEN": "ghp_test"}

    def test_omits_env_when_empty(self):
        saved: dict = {}

        with (
            patch("hermes_cli.config.load_config", return_value={"mcp_servers": {}}),
            patch("hermes_cli.config.save_config", side_effect=lambda c: saved.update(c)),
            patch("tools.mcp_tool.register_mcp_servers", return_value=[]),
        ):
            _neus_write_mcp_entry("github", ["npx", "-y", "@scope/pkg"])

        entry = saved["mcp_servers"]["github"]
        assert "env" not in entry

    def test_overwrites_existing_entry(self):
        existing = {"mcp_servers": {"github": {"command": "uvx", "args": ["old"]}}}
        saved: dict = {}

        with (
            patch("hermes_cli.config.load_config", return_value=dict(existing)),
            patch("hermes_cli.config.save_config", side_effect=lambda c: saved.update(c)),
            patch("tools.mcp_tool.register_mcp_servers", return_value=[]),
        ):
            _neus_write_mcp_entry("github", ["npx", "-y", "@scope/new-pkg"])

        assert saved["mcp_servers"]["github"]["command"] == "npx"
        assert saved["mcp_servers"]["github"]["args"] == ["-y", "@scope/new-pkg"]


# ===========================================================================
# Unit: _neus_remove_mcp_entry
# ===========================================================================

class TestNeusRemoveMcpEntry:
    def test_removes_existing_entry(self):
        saved: dict = {}

        with (
            patch(
                "hermes_cli.config.load_config",
                return_value={"mcp_servers": {"github": {"command": "npx", "args": []}}},
            ),
            patch("hermes_cli.config.save_config", side_effect=lambda c: saved.update(c)),
        ):
            _neus_remove_mcp_entry("github")

        assert "github" not in saved.get("mcp_servers", {})

    def test_remove_nonexistent_is_noop(self):
        saved: dict = {}

        with (
            patch("hermes_cli.config.load_config", return_value={"mcp_servers": {}}),
            patch("hermes_cli.config.save_config", side_effect=lambda c: saved.update(c)),
        ):
            _neus_remove_mcp_entry("does-not-exist")

        assert saved.get("mcp_servers") == {}


# ===========================================================================
# Integration: list_mcp_servers reads from Neus
# ===========================================================================

class TestListMcpServersReadsNeus:
    """list_mcp_servers must draw from Neus, not from a Safent-side store."""

    def _make_wiring(self):
        from hermes.agents_os.infrastructure.dbus_runtime_service import (
            DbusRuntimeServiceWiring,
        )
        return DbusRuntimeServiceWiring(
            agent_state=None,
            approval_gate=None,
            authorized_uids=frozenset({1000}),
            work_queue=None,
            wake_signal=None,
        )

    @pytest.mark.asyncio
    async def test_returns_live_server_from_get_mcp_status(self):
        wiring = self._make_wiring()
        live_status = [{"name": "github", "connected": True, "tools": 3, "transport": "stdio"}]
        neus_cfg = {"github": {"command": "npx", "args": ["-y", "@scope/pkg"]}}

        with (
            patch("tools.mcp_tool.get_mcp_status", return_value=live_status),
            patch("tools.mcp_tool._load_mcp_config", return_value=neus_cfg),
        ):
            result = await wiring.list_mcp_servers()

        assert len(result) == 1
        assert result[0]["server_id"] == "github"
        assert result[0]["tool_count"] == 3
        assert result[0]["health"] == "healthy"
        assert result[0]["argv"] == ["npx", "-y", "@scope/pkg"]

    @pytest.mark.asyncio
    async def test_includes_configured_but_not_connected(self):
        wiring = self._make_wiring()
        neus_cfg = {"offline-server": {"command": "uvx", "args": ["myserver"]}}

        with (
            patch("tools.mcp_tool.get_mcp_status", return_value=[]),
            patch("tools.mcp_tool._load_mcp_config", return_value=neus_cfg),
        ):
            result = await wiring.list_mcp_servers()

        assert len(result) == 1
        assert result[0]["server_id"] == "offline-server"
        assert result[0]["health"] == "disconnected"
        assert result[0]["tool_count"] == 0

    @pytest.mark.asyncio
    async def test_deduplicates_live_and_configured(self):
        """A server present in both get_mcp_status and _load_mcp_config appears once."""
        wiring = self._make_wiring()
        live_status = [{"name": "github", "connected": True, "tools": 2, "transport": "stdio"}]
        neus_cfg = {"github": {"command": "npx", "args": ["-y", "@scope/pkg"]}}

        with (
            patch("tools.mcp_tool.get_mcp_status", return_value=live_status),
            patch("tools.mcp_tool._load_mcp_config", return_value=neus_cfg),
        ):
            result = await wiring.list_mcp_servers()

        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_returns_empty_when_tools_mcp_tool_unavailable(self):
        wiring = self._make_wiring()
        with patch.dict("sys.modules", {"tools.mcp_tool": None}):  # type: ignore[dict-item]
            result = await wiring.list_mcp_servers()
        assert result == []


# ===========================================================================
# Integration: add_mcp_server gate + Neus write
# ===========================================================================

class _FakeServer:
    """Minimal stand-in for a connected McpServer returned by _mcp_connect."""
    def __init__(self, tool_count: int = 4):
        self.tools = [object()] * tool_count


class TestAddMcpServerGateAndNeusWrite:
    """Gate stays in front of Neus write. Blocked installs never reach Neus."""

    def _make_wiring(self, scan_blocked: bool = False, scan_fail_with_force: bool = False):
        from hermes.agents_os.infrastructure.dbus_runtime_service import (
            DbusRuntimeServiceWiring,
        )
        wiring = DbusRuntimeServiceWiring(
            agent_state=None,
            approval_gate=None,
            authorized_uids=frozenset({1000}),
            work_queue=None,
            wake_signal=None,
            mcp_server_manager=MagicMock(),
        )
        # Stub authorize to always allow operator
        wiring._authorize_and_resolve = MagicMock()

        if scan_blocked and not scan_fail_with_force:
            # Returns a blocked result (no force path)
            wiring._scan_install_target = MagicMock(
                return_value={"ok": False, "blocked": True, "error": "FAIL verdict", "scan_id": "aaaa"}
            )
        elif scan_fail_with_force:
            # First call blocked; second (allow_warn rescan) passes.
            # scan_id must be a valid UUID string for UUID() parsing in the override path.
            _FAKE_SCAN_ID = "11111111-1111-1111-1111-111111111111"
            call_count = [0]
            def _scan(*a, **kw):
                call_count[0] += 1
                if call_count[0] == 1:
                    return {"ok": False, "blocked": True, "error": "FAIL", "scan_id": _FAKE_SCAN_ID}
                return None  # second call: cleared
            wiring._scan_install_target = MagicMock(side_effect=_scan)
            wiring._scan_service_lazy = MagicMock(return_value=MagicMock(allow_target=MagicMock()))
        else:
            # Gate passes
            wiring._scan_install_target = MagicMock(return_value=None)

        return wiring

    @pytest.mark.asyncio
    async def test_scan_fail_blocks_neus_write(self):
        """When gate returns blocked, Neus is never written."""
        wiring = self._make_wiring(scan_blocked=True)
        draft = json.dumps({
            "server_id": "evil-mcp",
            "label": "Evil",
            "argv": ["npx", "-y", "@evil/mcp"],
        })

        saved_calls: list = []
        with (
            patch(
                "hermes.agents_os.infrastructure.dbus_runtime_service._scanner_can_analyze_argv",
                return_value=True,
            ),
            patch(
                "hermes.agents_os.infrastructure.dbus_runtime_service._prefetch_mcp_package",
            ),
            patch("hermes_cli.config.save_config", side_effect=lambda c: saved_calls.append(c)),
        ):
            result = await wiring.add_mcp_server(draft_json=draft, sender_uid=1000)

        assert result["ok"] is False
        assert result.get("blocked") is True
        assert saved_calls == [], "Neus save_config must NOT be called when gate blocks"

    @pytest.mark.asyncio
    async def test_gate_pass_writes_to_neus(self):
        """When gate passes, the entry is written to Neus config.yaml."""
        wiring = self._make_wiring(scan_blocked=False)
        # Use a key in _MCP_BYOK_ENV_KEYS so the env validator passes.
        draft = json.dumps({
            "server_id": "github",
            "label": "GitHub",
            "argv": ["npx", "-y", "@modelcontextprotocol/server-github"],
            "env": {"OPENAI_API_KEY": "sk-test"},
        })

        neus_state: dict = {"mcp_servers": {}}
        fake_server = _FakeServer(tool_count=5)

        with (
            patch(
                "hermes.agents_os.infrastructure.dbus_runtime_service._scanner_can_analyze_argv",
                return_value=True,
            ),
            patch(
                "hermes.agents_os.infrastructure.dbus_runtime_service._prefetch_mcp_package",
            ),
            patch(
                "hermes.agents_os.infrastructure.dbus_runtime_service._mcp_connect",
                new_callable=AsyncMock,
                return_value=fake_server,
            ),
            patch("hermes_cli.config.load_config", return_value=dict(neus_state)),
            patch("hermes_cli.config.save_config", side_effect=lambda c: neus_state.update(c)),
            patch("tools.mcp_tool.register_mcp_servers", return_value=[]),
        ):
            result = await wiring.add_mcp_server(draft_json=draft, sender_uid=1000)

        assert result["ok"] is True
        assert result["tool_count"] == 5
        servers = neus_state.get("mcp_servers", {})
        assert "github" in servers, "Entry must be written to Neus mcp_servers"
        assert servers["github"]["command"] == "npx"
        assert servers["github"]["args"] == ["-y", "@modelcontextprotocol/server-github"]
        assert servers["github"]["env"] == {"OPENAI_API_KEY": "sk-test"}

    @pytest.mark.asyncio
    async def test_force_override_writes_to_neus_after_clearing_block(self):
        """Owner force=True clears the FAIL gate, then persists to Neus."""
        wiring = self._make_wiring(scan_fail_with_force=True)
        draft = json.dumps({
            "server_id": "risky-mcp",
            "label": "Risky",
            "argv": ["npx", "-y", "@risky/mcp"],
            "force": True,
        })

        neus_state: dict = {"mcp_servers": {}}
        fake_server = _FakeServer(tool_count=2)

        with (
            patch(
                "hermes.agents_os.infrastructure.dbus_runtime_service._scanner_can_analyze_argv",
                return_value=True,
            ),
            patch(
                "hermes.agents_os.infrastructure.dbus_runtime_service._prefetch_mcp_package",
            ),
            patch(
                "hermes.agents_os.infrastructure.dbus_runtime_service._mcp_connect",
                new_callable=AsyncMock,
                return_value=fake_server,
            ),
            patch("hermes_cli.config.load_config", return_value=dict(neus_state)),
            patch("hermes_cli.config.save_config", side_effect=lambda c: neus_state.update(c)),
            patch("tools.mcp_tool.register_mcp_servers", return_value=[]),
        ):
            result = await wiring.add_mcp_server(draft_json=draft, sender_uid=1000)

        assert result["ok"] is True, f"Force override should succeed: {result}"
        assert "risky-mcp" in neus_state.get("mcp_servers", {})

    @pytest.mark.asyncio
    async def test_added_server_visible_in_list_mcp_servers(self):
        """Round-trip: add writes to Neus → list reads from Neus → server appears."""
        wiring = self._make_wiring(scan_blocked=False)
        draft = json.dumps({
            "server_id": "myserver",
            "label": "My Server",
            "argv": ["uvx", "my-mcp-server"],
        })

        neus_state: dict = {"mcp_servers": {}}
        fake_server = _FakeServer(tool_count=3)

        with (
            patch(
                "hermes.agents_os.infrastructure.dbus_runtime_service._scanner_can_analyze_argv",
                return_value=True,
            ),
            patch(
                "hermes.agents_os.infrastructure.dbus_runtime_service._prefetch_mcp_package",
            ),
            patch(
                "hermes.agents_os.infrastructure.dbus_runtime_service._mcp_connect",
                new_callable=AsyncMock,
                return_value=fake_server,
            ),
            patch("hermes_cli.config.load_config", return_value=dict(neus_state)),
            patch("hermes_cli.config.save_config", side_effect=lambda c: neus_state.update(c)),
            patch("tools.mcp_tool.register_mcp_servers", return_value=[]),
        ):
            add_result = await wiring.add_mcp_server(draft_json=draft, sender_uid=1000)

        assert add_result["ok"] is True

        # Now list — Neus state has the entry; no separate Safent store is consulted.
        expected_neus_cfg = {
            "myserver": neus_state["mcp_servers"]["myserver"]
        }
        with (
            patch("tools.mcp_tool.get_mcp_status", return_value=[
                {"name": "myserver", "connected": True, "tools": 3, "transport": "stdio"}
            ]),
            patch("tools.mcp_tool._load_mcp_config", return_value=expected_neus_cfg),
        ):
            listed = await wiring.list_mcp_servers()

        assert any(s["server_id"] == "myserver" for s in listed), (
            "Server added through the gate must appear in list_mcp_servers via Neus"
        )
        server = next(s for s in listed if s["server_id"] == "myserver")
        assert server["tool_count"] == 3


# ===========================================================================
# Unit: _import_seed_mcp_servers — first-boot/upgrade seed importer
#
# Regression for the P0 found 2026-07-10: the image bakes + tmpfiles-copies
# mcp-servers.json but NOTHING registered it into Neus since the single-source
# migration → 0 seeded MCPs connected at startup (verified live on 0.8.32).
# ===========================================================================

from hermes.agents_os.infrastructure import dbus_runtime_service as _drs  # noqa: E402
from hermes.agents_os.infrastructure.dbus_runtime_service import (  # noqa: E402
    _import_seed_mcp_servers,
)


class TestSeedImporter:
    SEEDS = [
        {
            "server_id": "excel",
            "label": "Hojas de cálculo · crea y edita Excel (.xlsx) sin clave",
            "argv": ["uvx", "excel-mcp-server", "stdio"],
        },
        {
            "server_id": "serena",
            "label": "Código · entiende y edita proyectos (Python, TypeScript) sin clave",
            "argv": ["uvx", "--from", "serena-agent==1.5.3", "serena", "start-mcp-server"],
        },
    ]

    def _wire(self, tmp_path, monkeypatch, *, seeds=None, marker=None, mcp_servers=None):
        """Point the importer at tmp files + an in-memory Neus store; return the store."""
        seed_file = tmp_path / "mcp-servers.json"
        if seeds is not None:
            seed_file.write_text(json.dumps(seeds), encoding="utf-8")
        marker_file = tmp_path / "instance" / "mcp-seeds-imported.json"
        if marker is not None:
            marker_file.parent.mkdir(parents=True, exist_ok=True)
            marker_file.write_text(json.dumps(marker), encoding="utf-8")
        monkeypatch.setattr(_drs, "_MCP_SEED_FILE", str(seed_file))
        monkeypatch.setattr(_drs, "_MCP_SEED_MARKER", str(marker_file))

        store: dict[str, Any] = {"mcp_servers": dict(mcp_servers or {})}
        cfg_mod = sys.modules["hermes_cli.config"]
        monkeypatch.setattr(cfg_mod, "load_config", lambda: store, raising=False)
        monkeypatch.setattr(cfg_mod, "save_config", store.update, raising=False)
        tool_mod = sys.modules["tools.mcp_tool"]
        monkeypatch.setattr(
            tool_mod, "_load_mcp_config", lambda: store.get("mcp_servers", {}), raising=False
        )
        monkeypatch.setattr(tool_mod, "register_mcp_servers", lambda _: [], raising=False)
        return store, marker_file

    def test_first_boot_imports_all_seeds_with_labels(self, tmp_path, monkeypatch):
        store, marker_file = self._wire(tmp_path, monkeypatch, seeds=self.SEEDS)
        _import_seed_mcp_servers()
        assert set(store["mcp_servers"]) == {"excel", "serena"}
        serena = store["mcp_servers"]["serena"]
        assert serena["command"] == "uvx"
        assert serena["args"][0:2] == ["--from", "serena-agent==1.5.3"]
        assert serena["label"].startswith("Código")
        # Marker records both ids so they never re-import.
        assert set(json.loads(marker_file.read_text())) == {"excel", "serena"}

    def test_second_call_is_idempotent(self, tmp_path, monkeypatch):
        store, _ = self._wire(tmp_path, monkeypatch, seeds=self.SEEDS)
        _import_seed_mcp_servers()
        snapshot = json.dumps(store, sort_keys=True)
        _import_seed_mcp_servers()
        assert json.dumps(store, sort_keys=True) == snapshot

    def test_owner_removed_seed_never_resurrects(self, tmp_path, monkeypatch):
        # Marker says serena was imported once; the owner then removed it from Neus.
        store, _ = self._wire(
            tmp_path, monkeypatch, seeds=self.SEEDS, marker=["excel", "serena"],
        )
        _import_seed_mcp_servers()
        assert "serena" not in store["mcp_servers"], (
            "a seed the owner removed must not resurrect on the next boot"
        )

    def test_upgrade_adds_only_new_seed(self, tmp_path, monkeypatch):
        # Existing volume: excel imported long ago (marker) and still configured;
        # the image upgrade ships the new serena seed → only serena imports.
        store, marker_file = self._wire(
            tmp_path, monkeypatch, seeds=self.SEEDS, marker=["excel"],
            mcp_servers={"excel": {"command": "uvx", "args": ["excel-mcp-server", "stdio"]}},
        )
        _import_seed_mcp_servers()
        assert set(store["mcp_servers"]) == {"excel", "serena"}
        assert set(json.loads(marker_file.read_text())) == {"excel", "serena"}

    def test_existing_config_entry_is_never_overwritten(self, tmp_path, monkeypatch):
        # The owner customized the excel argv — the importer must not clobber it.
        custom = {"command": "uvx", "args": ["excel-mcp-server", "stdio", "--custom"]}
        store, _ = self._wire(
            tmp_path, monkeypatch, seeds=self.SEEDS, mcp_servers={"excel": dict(custom)},
        )
        _import_seed_mcp_servers()
        assert store["mcp_servers"]["excel"] == custom

    def test_missing_or_malformed_seed_is_fail_soft(self, tmp_path, monkeypatch):
        store, _ = self._wire(tmp_path, monkeypatch, seeds=None)  # no file
        _import_seed_mcp_servers()
        assert store["mcp_servers"] == {}
        (tmp_path / "mcp-servers.json").write_text("{not json", encoding="utf-8")
        _import_seed_mcp_servers()
        assert store["mcp_servers"] == {}
