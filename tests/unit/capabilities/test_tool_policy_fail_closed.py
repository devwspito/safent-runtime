"""Unreadable owner restrictions never silently become permissive defaults."""
import json
from pathlib import Path

import pytest

from hermes.capabilities.tool_policy import PolicyUnavailableError, Preset, ToolPolicyStore

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("raw", [
    "{", "[]", "null", '"permisivo"',
    '{"preset":"unknown"}', '{"preset":[]}', '{"preset":null}',
    '{"overrides":[]}', '{"overrides":{"read_file":"false"}}',
    '{"overrides":{"read_file":0}}', '{"overrides":{"":true}}',
    '{"approval_on_dangers":0}', '{"approval_on_dangers":null}',
])
def test_invalid_document_denies_without_rewriting(tmp_path: Path, raw: str) -> None:
    path = tmp_path / "policy.json"
    path.write_text(raw)
    store = ToolPolicyStore(path)
    for tool in ("read_file", "install_mcp", "unknown_external_tool"):
        assert not store.is_enabled(tool)
        assert store.is_owner_disabled(tool)
        assert not store.for_agent("worker", {tool: {"enabled": True}}).is_enabled(tool)
    assert store.approval_on_dangers()
    with pytest.raises(PolicyUnavailableError):
        store.snapshot()
    for operation in (
        lambda: store.set_tool("read_file", True),
        lambda: store.apply_preset(Preset.PERMISIVO),
        lambda: store.set_approval_on_dangers(False),
    ):
        with pytest.raises(PolicyUnavailableError):
            operation()
        assert path.read_text() == raw


def test_unreadable_policy_denies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ToolPolicyStore(tmp_path / "policy.json")

    def denied(*_args: object, **_kwargs: object) -> str:
        raise PermissionError("private policy")

    monkeypatch.setattr(Path, "read_text", denied)
    assert not store.is_enabled("read_file")
    assert store.is_owner_disabled("read_file")
    assert store.approval_on_dangers()


def test_missing_file_is_fresh_but_broken_symlink_is_not(tmp_path: Path) -> None:
    path = tmp_path / "policy.json"
    store = ToolPolicyStore(path)
    assert store.is_enabled("read_file")
    path.symlink_to(tmp_path / "missing-target")
    assert not store.is_enabled("read_file")
    assert store.is_owner_disabled("read_file")


def test_snapshot_and_decision_read_one_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ToolPolicyStore(tmp_path / "policy.json")
    documents = iter([
        {"preset": "bloqueado", "approval_on_dangers": True},
        {"preset": "permisivo", "approval_on_dangers": False},
    ])
    monkeypatch.setattr(store, "_load", lambda: next(documents))
    snapshot = store.snapshot()
    assert snapshot["preset"] == "bloqueado"
    assert not any(snapshot["tools"].values())
    assert snapshot["approval_on_dangers"] is True
    assert store.is_enabled("read_file")


def test_valid_owner_restrictions_remain_effective(tmp_path: Path) -> None:
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"preset": "permisivo", "overrides": {"read_file": False}}))
    store = ToolPolicyStore(path)
    assert store.is_owner_disabled("read_file")
    assert not store.is_enabled("read_file")
    assert store.is_enabled("web_search")
