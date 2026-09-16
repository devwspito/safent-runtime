"""Regression — `create_app()` resolves HERMES_SHELL_DB at CALL time.

Before this fix `_DB_PATH` was a module-level constant, bound ONCE the first
time `hermes.shell_server.main` was imported anywhere in the process. Every
later `create_app()` call silently reused that first-bound path no matter
what `HERMES_SHELL_DB` was set to afterwards — a second `create_app()` call
in the SAME process (a second test, a differently-configured instance) got
the FIRST call's database, not its own. This made test order significant:
whichever test file imported `hermes.shell_server.main` first "won" the DB
path for every other test in the run.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit


def _patch_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    from hermes.shell_server import main as shell_main

    master_key = os.urandom(32)
    original_vault = shell_main.SecretsVault

    class _TestVault(original_vault):  # type: ignore[valid-type]
        def __init__(self, **_: Any) -> None:
            super().__init__(master_key=master_key)

    monkeypatch.setattr(shell_main, "SecretsVault", _TestVault)


class TestResolveDbPathIsCallTime:
    """`_resolve_db_path()` itself: pure, no import-time caching."""

    def test_reads_env_freshly_on_every_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hermes.shell_server.main import _resolve_db_path

        first = tmp_path / "first" / "shell-state.db"
        second = tmp_path / "second" / "shell-state.db"

        monkeypatch.setenv("HERMES_SHELL_DB", str(first))
        assert _resolve_db_path() == first

        monkeypatch.setenv("HERMES_SHELL_DB", str(second))
        assert _resolve_db_path() == second


class TestTwoCreateAppCallsUseIndependentDbPaths:
    """End-to-end: two `create_app()` calls, two different `HERMES_SHELL_DB`
    values, two ACTUAL sqlite databases on disk — not the same one."""

    def test_two_create_app_calls_with_different_env_produce_different_db_paths(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_vault(monkeypatch)
        spool = tmp_path / "audit-spool"
        spool.mkdir()
        monkeypatch.setenv("HERMES_AUDIT_SPOOL_DIR", str(spool))

        from hermes.shell_server.main import create_app

        db_path_a = tmp_path / "instance-a" / "shell-state.db"
        monkeypatch.setenv("HERMES_SHELL_DB", str(db_path_a))
        create_app()
        assert db_path_a.parent.stat().st_mode & 0o777 == 0o700
        assert db_path_a.exists(), (
            "create_app() #1 did not create its own sqlite db at HERMES_SHELL_DB "
            f"({db_path_a}) — did it reuse a stale module-level _DB_PATH?"
        )

        db_path_b = tmp_path / "instance-b" / "shell-state.db"
        monkeypatch.setenv("HERMES_SHELL_DB", str(db_path_b))
        create_app()
        assert db_path_b.parent.stat().st_mode & 0o777 == 0o700
        assert db_path_b.exists(), (
            "create_app() #2 did not create its own sqlite db at the NEW "
            f"HERMES_SHELL_DB ({db_path_b}) — it likely reused create_app() #1's "
            "DB path (the import-time-binding regression this test guards)."
        )
        assert db_path_a != db_path_b

    def test_existing_unsafe_directory_is_not_silently_repaired(self, tmp_path, monkeypatch):
        from hermes.security.configuration_lock import ConfigurationLockError
        from hermes.shell_server.main import create_app

        directory = tmp_path / "unsafe"
        directory.mkdir(mode=0o777)
        directory.chmod(0o777)
        monkeypatch.setenv("HERMES_SHELL_DB", str(directory / "state.db"))
        with pytest.raises(ConfigurationLockError, match="directory is unsafe"):
            create_app()
        assert directory.stat().st_mode & 0o777 == 0o777
        assert not (directory / "state.db").exists()
