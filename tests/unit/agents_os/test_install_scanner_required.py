"""An unavailable scanner is never a successful review or an install permit."""

import json
from unittest.mock import MagicMock

import pytest

from hermes.agents_os.infrastructure import dbus_runtime_service as module
from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState

pytestmark = pytest.mark.unit


@pytest.fixture
def wiring(monkeypatch):
    service = module.DbusRuntimeServiceWiring(
        agent_state=InMemoryAgentState(paused=False),
        approval_gate=MagicMock(),
        authorized_uids=frozenset({1000}),
    )
    monkeypatch.setattr(service, "_scan_service_lazy", lambda: None)
    return service


@pytest.mark.parametrize("kind", ["skill", "mcp_server", "package"])
def test_absent_scanner_blocks_review(wiring, kind):
    result = wiring._scan_install_target(kind, "example")
    assert result is not None
    assert result["blocked"] is True
    assert result["code"] == "scan_unavailable"
    assert "record" not in result and "scan_id" not in result


def test_draft_does_not_fabricate_pass_or_scan_id(wiring):
    result = json.loads(
        wiring.scan_install_draft(draft_json=json.dumps({"kind": "skill", "identifier": "example"}))
    )
    assert result.get("error")
    assert result.get("code") == "scan_unavailable"
    assert "verdict" not in result and "score" not in result and "scan_id" not in result


def test_broken_scanner_initialization_returns_safe_unavailable(wiring, monkeypatch):
    monkeypatch.setattr(
        wiring, "_scan_service_lazy", MagicMock(side_effect=OSError("sensitive-path"))
    )
    result = json.loads(
        wiring.scan_install_draft(draft_json='{"kind":"skill","identifier":"example"}')
    )
    assert result.get("code") == "scan_unavailable"
    assert "sensitive-path" not in json.dumps(result)


@pytest.mark.parametrize("force", [False, True])
def test_skill_install_never_starts_without_scanner(wiring, monkeypatch, force):
    start = MagicMock(return_value={"op_id": "should-not-start"})
    monkeypatch.setattr(module, "_start_hub_op", start)
    result = wiring.install_hub_skill(identifier="example", sender_uid=1000, force=force)
    assert result.get("blocked") is True
    start.assert_not_called()


def test_package_install_never_starts_without_scanner(wiring, monkeypatch):
    store = MagicMock()
    monkeypatch.setattr(wiring, "_package_store_service", lambda: store)
    result = wiring.install_package(source="flatpak", package_id="org.example.App", sender_uid=1000)
    assert result.get("blocked") is True
    store.start_install.assert_not_called()


@pytest.mark.parametrize("force", [False, True])
async def test_mcp_never_prefetches_or_connects_without_scanner(wiring, monkeypatch, force):
    manager = MagicMock()
    wiring._mcp_manager = manager
    prefetch = MagicMock()
    monkeypatch.setattr(module, "_prefetch_mcp_package", prefetch)
    result = await wiring.add_mcp_server(
        draft_json=json.dumps(
            {"server_id": "example", "argv": ["npx", "-y", "@example/server"], "force": force}
        ),
        sender_uid=1000,
    )
    assert result.get("blocked") is True
    prefetch.assert_not_called()
    assert manager.mock_calls == []
