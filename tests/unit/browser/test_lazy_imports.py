"""T104 — lazy-imports verificados.

CI base no instala `playwright`, `stagehand`, `playwright-extra`, `pytesseract`,
`azure-ai-documentintelligence`. Importar `hermes.browser` y sus submodulos NO
debe fallar; solo fallan los metodos que realmente intentan usar la lib.

Constitucion V: tests base sin Chromium ni red ni binarios externos.
"""
from __future__ import annotations

import importlib
import sys

import pytest


@pytest.mark.parametrize(
    "modname",
    [
        "hermes.browser",
        "hermes.browser.domain",
        "hermes.browser.application",
        "hermes.browser.infrastructure",
        "hermes.browser.testing",
    ],
)
def test_top_level_modules_import_without_extras(modname: str) -> None:
    """Top-level + subpaquetes se importan sin las extras `[browser]` instaladas."""
    mod = importlib.import_module(modname)
    assert mod.__name__ == modname


def test_stagehand_driver_imports_without_stagehand_installed() -> None:
    """`StagehandDriver` se puede importar; solo `.start()` levanta `StagehandNotInstalledError`."""
    from hermes.browser.infrastructure import StagehandDriver, StagehandNotInstalledError

    driver = StagehandDriver(model_name="anthropic/claude-3-5-haiku-20241022")
    assert driver.driver_name == "stagehand"
    # `.start()` solo es el que verifica que stagehand-py esta presente.
    assert StagehandNotInstalledError is not None


def test_playwright_driver_imports_without_playwright_installed() -> None:
    from hermes.browser.infrastructure import (
        PlaywrightDriver,
        PlaywrightNotInstalledError,
    )

    driver = PlaywrightDriver()
    assert driver.driver_name == "playwright"
    assert PlaywrightNotInstalledError is not None


def test_simulated_missing_dependency_does_not_break_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Aunque pongamos `sys.modules["playwright"] = None`, el subpaquete sigue importable."""
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "stagehand", None)
    # Forzamos re-import del subpaquete de infrastructure.
    for cached in list(sys.modules):
        if cached.startswith("hermes.browser.infrastructure"):
            sys.modules.pop(cached, None)
    importlib.import_module("hermes.browser.infrastructure.stagehand_driver")
    importlib.import_module("hermes.browser.infrastructure.playwright_driver")
    # No exception.
