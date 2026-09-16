"""Structural regression: teach-by-browser must stay retired (10-sep-2026).

See specs/025-safent-repaso/retirada-ensenar.md for the removal boundary.
Two invariants, enforced so a future change can't silently resurrect the
feature or leave a dangling module behind "for one import":

  1. No module/package under src/hermes is named *teach*/*training*, except
     the explicit, documented exceptions below (all unrelated to
     teach-by-browser — see their own docstrings).
  2. No HTTP or WebSocket route under /api/v1/training* is registered on the
     real app built by shell_server.main.create_app().
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_SRC_HERMES = Path(__file__).parents[3] / "src" / "hermes"

# Documented exceptions — matched against the full relative module path
# (dot-separated, no .py), case-insensitive substring match on "teach"/
# "training".
_ALLOWED_NAMES: frozenset[str] = frozenset(
    {
        # GEPA offline self-evolution (hermes-evolution CLI entrypoint):
        # reads audit PROPOSAL_REJECTED entries for ANY skill regardless of
        # origin. Already marked KEEP by the prior L1c lane (oleada-1.md) —
        # unrelated to teach-by-browser, not retired here.
        "training",
        "training.application",
        "training.application.skill_evolution",
        "training.evolution",
        "training.evolution.__main__",
        "training.infrastructure",
        "training.infrastructure.gepa_evolution_engine",
    }
)


def _module_path(py_file: Path) -> str:
    rel = py_file.relative_to(_SRC_HERMES).with_suffix("")
    parts = [p for p in rel.parts if p != "__init__"]
    return ".".join(parts)


def _iter_hermes_modules() -> list[str]:
    modules = set()
    for py_file in _SRC_HERMES.rglob("*.py"):
        if "__pycache__" in py_file.parts:
            continue
        dotted = _module_path(py_file)
        if not dotted:
            continue
        modules.add(dotted)
        # Also register every ancestor package (so a package DIR named
        # *teach*/*training* is caught even if none of its files are).
        pieces = dotted.split(".")
        for i in range(1, len(pieces)):
            modules.add(".".join(pieces[:i]))
    return sorted(modules)


class TestNoTeachOrTrainingModulesRemain:
    def test_no_disallowed_module_names(self) -> None:
        offenders = [
            name
            for name in _iter_hermes_modules()
            if ("teach" in name.lower() or "training" in name.lower())
            and name not in _ALLOWED_NAMES
        ]
        assert offenders == [], (
            "teach-by-browser was retired 10-sep-2026 (specs/025-safent-repaso/"
            f"retirada-ensenar.md) but these modules remain: {offenders}. "
            "Either delete them or add them to _ALLOWED_NAMES with a reason."
        )


class TestNoTrainingRoutesRegistered:
    """Rebuilds the real app (same pattern as test_api_v1_authorization.py)."""

    @pytest.fixture()
    def app(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
        spool = tmp_path / "audit-spool"
        spool.mkdir()
        monkeypatch.setenv("HERMES_SHELL_DB", str(tmp_path / "shell-state.db"))
        monkeypatch.setenv("HERMES_AUDIT_SPOOL_DIR", str(spool))

        master_key = os.urandom(32)
        from hermes.shell_server import main as shell_main

        monkeypatch.setenv("HERMES_SHELL_DB", str(tmp_path / "shell-state.db"))

        original_vault = shell_main.SecretsVault

        class _TestVault(original_vault):  # type: ignore[valid-type]
            def __init__(self, **_: Any) -> None:
                super().__init__(master_key=master_key)

        monkeypatch.setattr(shell_main, "SecretsVault", _TestVault)

        from hermes.shell_server.main import create_app

        return create_app()

    def test_no_route_under_api_v1_training(self, app: Any) -> None:
        offenders = [
            route.path for route in app.routes if "/training" in getattr(route, "path", "")
        ]
        assert offenders == [], (
            f"routes still exist under /api/v1/training*: {offenders}"
        )
