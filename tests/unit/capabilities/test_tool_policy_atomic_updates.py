"""Policy decisions are atomic and concurrent owners do not lose unrelated edits."""
import json
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest

from hermes.capabilities.tool_policy import ToolPolicyStore

pytestmark = pytest.mark.unit


def _set_one(args: tuple[str, int]) -> None:
    path, index = args
    ToolPolicyStore(Path(path)).set_tool(f"tool-{index}", False)


def test_processes_do_not_lose_each_others_updates(tmp_path: Path) -> None:
    path = tmp_path / "policy.json"
    with ProcessPoolExecutor(max_workers=4) as pool:
        list(pool.map(_set_one, [(str(path), index) for index in range(24)]))
    assert json.loads(path.read_text())["overrides"] == {
        f"tool-{index}": False for index in range(24)
    }
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".policy-*"))


def test_failed_atomic_replace_keeps_the_previous_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ToolPolicyStore(tmp_path / "policy.json")
    store.set_tools({"read_file": False})
    before = (tmp_path / "policy.json").read_bytes()

    def fail_replace(*_args: object) -> None:
        raise OSError("disk error")

    monkeypatch.setattr("hermes.capabilities.tool_policy.os.replace", fail_replace)
    with pytest.raises(OSError, match="disk error"):
        store.set_tools({"read_file": True, "web_search": False})
    assert (tmp_path / "policy.json").read_bytes() == before
    assert not list(tmp_path.glob(".policy-*"))
