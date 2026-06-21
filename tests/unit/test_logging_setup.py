"""Regression tests: configure_structured_logging (finding #27).

Verifies that the function is importable, idempotent, and does not crash
in headless CI (even if structlog is absent).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


class TestConfigureStructuredLogging:
    def test_importable(self) -> None:
        from hermes.logging_setup import configure_structured_logging

        assert callable(configure_structured_logging)

    def test_idempotent(self) -> None:
        """Calling twice must not raise."""
        from hermes.logging_setup import configure_structured_logging

        configure_structured_logging(service="test-svc", version="0.0.1")
        configure_structured_logging(service="test-svc", version="0.0.1")

    def test_fallback_when_structlog_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If structlog import fails, fallback plain logging is used (no crash)."""
        import importlib
        import sys

        # Temporarily hide structlog from import machinery.
        original = sys.modules.get("structlog")
        sys.modules["structlog"] = None  # type: ignore[assignment]
        try:
            import hermes.logging_setup as mod
            importlib.reload(mod)
            mod.configure_structured_logging(service="test-fallback", version="0.0")
        finally:
            if original is not None:
                sys.modules["structlog"] = original
            else:
                del sys.modules["structlog"]
            importlib.reload(mod)
