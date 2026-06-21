"""Unit tests for workspace artifact auto-attach helpers.

Coverage:
  - _snapshot_workspace: captures existing files with mtime; returns {} on missing dir.
  - _workspace_delta: detects new files, modified files; ignores unchanged files;
    ignores dotfiles; ignores zero-byte files.
  - _attach_artifacts: appends MEDIA tokens for un-referenced paths;
    skips paths already present in narrative; respects cap; fail-soft on error.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes.runtime.nous_engine import (
    _WORKSPACE_ATTACH_CAP,
    _attach_artifacts,
    _snapshot_workspace,
    _workspace_delta,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# _snapshot_workspace
# ---------------------------------------------------------------------------


class TestSnapshotWorkspace:
    def test_returns_dict_with_mtime_for_regular_files(self, tmp_path: Path) -> None:
        (tmp_path / "report.pdf").write_bytes(b"data")
        (tmp_path / "image.png").write_bytes(b"data")

        snap = _snapshot_workspace(str(tmp_path))

        assert str(tmp_path / "report.pdf") in snap
        assert str(tmp_path / "image.png") in snap
        for mtime in snap.values():
            assert isinstance(mtime, float)
            assert mtime > 0

    def test_returns_empty_dict_for_missing_dir(self, tmp_path: Path) -> None:
        missing = str(tmp_path / "does_not_exist")
        assert _snapshot_workspace(missing) == {}

    def test_does_not_recurse_into_subdirectories(self, tmp_path: Path) -> None:
        sub = tmp_path / "subdir"
        sub.mkdir()
        (sub / "nested.txt").write_bytes(b"x")
        (tmp_path / "top.txt").write_bytes(b"x")

        snap = _snapshot_workspace(str(tmp_path))

        assert str(tmp_path / "top.txt") in snap
        assert str(sub / "nested.txt") not in snap

    def test_returns_empty_dict_on_oserror(self, tmp_path: Path) -> None:
        with patch("hermes.runtime.nous_engine.Path") as mock_path_cls:
            mock_p = mock_path_cls.return_value
            mock_p.is_dir.side_effect = OSError("permission denied")
            result = _snapshot_workspace(str(tmp_path))
        assert result == {}


# ---------------------------------------------------------------------------
# _workspace_delta
# ---------------------------------------------------------------------------


class TestWorkspaceDelta:
    def test_detects_new_file(self, tmp_path: Path) -> None:
        snapshot: dict[str, float] = {}
        (tmp_path / "new.docx").write_bytes(b"content")

        delta = _workspace_delta(snapshot, str(tmp_path))

        assert any(p.name == "new.docx" for p in delta)

    def test_detects_modified_file(self, tmp_path: Path) -> None:
        f = tmp_path / "chart.png"
        f.write_bytes(b"original")
        old_mtime = f.stat().st_mtime
        snapshot = {str(f): old_mtime}

        # Advance mtime past snapshot value.
        new_mtime = old_mtime + 2.0
        import os
        os.utime(str(f), (new_mtime, new_mtime))

        delta = _workspace_delta(snapshot, str(tmp_path))

        assert any(p.name == "chart.png" for p in delta)

    def test_ignores_unchanged_file(self, tmp_path: Path) -> None:
        f = tmp_path / "existing.xlsx"
        f.write_bytes(b"data")
        mtime = f.stat().st_mtime
        snapshot = {str(f): mtime}

        delta = _workspace_delta(snapshot, str(tmp_path))

        assert not any(p.name == "existing.xlsx" for p in delta)

    def test_ignores_dotfiles(self, tmp_path: Path) -> None:
        (tmp_path / ".hidden").write_bytes(b"secret")
        snapshot: dict[str, float] = {}

        delta = _workspace_delta(snapshot, str(tmp_path))

        assert not any(p.name == ".hidden" for p in delta)

    def test_ignores_zero_byte_files(self, tmp_path: Path) -> None:
        (tmp_path / "empty.txt").write_bytes(b"")
        snapshot: dict[str, float] = {}

        delta = _workspace_delta(snapshot, str(tmp_path))

        assert not any(p.name == "empty.txt" for p in delta)

    def test_returns_newest_first(self, tmp_path: Path) -> None:
        import os

        f_old = tmp_path / "old.txt"
        f_new = tmp_path / "new.txt"
        f_old.write_bytes(b"x")
        f_new.write_bytes(b"x")
        base = time.time()
        os.utime(str(f_old), (base, base))
        os.utime(str(f_new), (base + 10, base + 10))

        snapshot: dict[str, float] = {}
        delta = _workspace_delta(snapshot, str(tmp_path))

        names = [p.name for p in delta]
        assert names.index("new.txt") < names.index("old.txt")

    def test_returns_empty_on_missing_dir(self, tmp_path: Path) -> None:
        missing = str(tmp_path / "no_such_dir")
        assert _workspace_delta({}, missing) == []

    def test_returns_empty_on_oserror(self, tmp_path: Path) -> None:
        with patch("hermes.runtime.nous_engine.Path") as mock_path_cls:
            mock_p = mock_path_cls.return_value
            mock_p.is_dir.side_effect = OSError("permission denied")
            result = _workspace_delta({}, str(tmp_path))
        assert result == []


# ---------------------------------------------------------------------------
# _attach_artifacts
# ---------------------------------------------------------------------------


class TestAttachArtifacts:
    def test_appends_media_token_for_unreferenced_path(self, tmp_path: Path) -> None:
        f = tmp_path / "report.pdf"
        narrative = "Aquí está tu informe."

        result = _attach_artifacts(narrative, [f])

        assert f"\nMEDIA:{f}" in result

    def test_skips_already_referenced_path(self, tmp_path: Path) -> None:
        f = tmp_path / "chart.png"
        narrative = f"He guardado el fichero en {f}."

        result = _attach_artifacts(narrative, [f])

        # Token must not be duplicated — path already present in narrative.
        assert result.count(str(f)) == 1

    def test_respects_cap(self, tmp_path: Path) -> None:
        paths = [tmp_path / f"file_{i}.txt" for i in range(_WORKSPACE_ATTACH_CAP + 5)]
        narrative = ""

        result = _attach_artifacts(narrative, paths)

        token_count = result.count("\nMEDIA:")
        assert token_count == _WORKSPACE_ATTACH_CAP

    def test_returns_unchanged_narrative_when_no_new_paths(self) -> None:
        narrative = "No hay ficheros nuevos."
        result = _attach_artifacts(narrative, [])
        assert result == narrative

    def test_returns_unchanged_narrative_on_exception(self, tmp_path: Path) -> None:
        narrative = "Original."
        broken_path = Path("/")  # will not match but won't error

        # Simulate an exception inside the function.
        with patch(
            "hermes.runtime.nous_engine._WORKSPACE_ATTACH_CAP",
            new_callable=lambda: property(lambda self: (_ for _ in ()).throw(RuntimeError("boom"))),
        ):
            # The cap constant is module-level; patch the list slicing by making paths
            # raise inside the function via a mock.
            class _BadList(list):
                def __getitem__(self, item: object) -> object:
                    raise RuntimeError("boom")

            result = _attach_artifacts(narrative, _BadList([broken_path]))  # type: ignore[arg-type]

        assert result == narrative

    def test_multiple_new_paths_each_get_own_token(self, tmp_path: Path) -> None:
        paths = [tmp_path / f"out_{i}.png" for i in range(3)]
        narrative = "Done."

        result = _attach_artifacts(narrative, paths)

        for p in paths:
            assert f"\nMEDIA:{p}" in result
        assert result.count("\nMEDIA:") == 3
