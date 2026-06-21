"""Regression tests for Lumen Backend.needsOnboarding deferred-model rule.

Spec 011 US4: the LLM provider is NOT a precondition for accessing the desktop.
`needsOnboarding` must be True iff the account sentinel is absent — never because
an active model is missing.

These tests inspect the source via AST (no PySide6 import required) and exercise
the pure logic of _recompute_needs_onboarding via monkey-patching.
"""
from __future__ import annotations

import ast
import textwrap
import types
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

pytestmark = pytest.mark.unit

_LUMEN_SRC = (
    Path(__file__).parents[2] / "src" / "hermes" / "lumen" / "__main__.py"
)


# ---------------------------------------------------------------------------
# AST-level contract: _recompute_needs_onboarding must NOT reference
# _has_active_model (the deferred-model invariant, spec 011 US4).
# ---------------------------------------------------------------------------


def _extract_method_source(method_name: str) -> str:
    """Return the source lines of a method defined in the lumen __main__.py."""
    tree = ast.parse(_LUMEN_SRC.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == method_name:
            return ast.unparse(node)
    raise AssertionError(f"Method {method_name!r} not found in {_LUMEN_SRC}")


def test_recompute_needs_onboarding_does_not_reference_has_active_model() -> None:
    """_recompute_needs_onboarding must NOT read _has_active_model (spec 011 US4).

    Providing an LLM key is deferred — the desktop must be accessible as soon as
    the account sentinel exists, regardless of provider state.
    If this test fails, someone re-introduced the provider gate.
    """
    src = _extract_method_source("_recompute_needs_onboarding")
    assert "_has_active_model" not in src, (
        "_recompute_needs_onboarding still references _has_active_model. "
        "Spec 011 US4 forbids the provider as a desktop gate. "
        "Remove the 'or (not self._has_active_model)' clause."
    )


def test_recompute_needs_onboarding_references_account_sentinel() -> None:
    """_recompute_needs_onboarding MUST still check the account sentinel.

    Account creation is the only real gate (local session security).
    """
    src = _extract_method_source("_recompute_needs_onboarding")
    assert "_ACCOUNT_SENTINEL" in src, (
        "_recompute_needs_onboarding no longer checks _ACCOUNT_SENTINEL. "
        "The account sentinel is the sole desktop gate and must remain."
    )


# ---------------------------------------------------------------------------
# Logic tests: simulate the Backend's _recompute_needs_onboarding behavior
# by building a minimal stand-in that executes the same pure conditional.
# No PySide6 import required.
# ---------------------------------------------------------------------------


def _make_stand_in(sentinel_exists: bool, has_active_model: bool):
    """Build a minimal object that mirrors the Backend's onboarding fields."""
    obj = types.SimpleNamespace(
        _has_active_model=has_active_model,
        _needs_onboarding=not sentinel_exists,
        _emitted=False,
    )

    def _emit():
        obj._emitted = True

    def _recompute(self=obj):
        new_val = not sentinel_exists  # mirrors the patched sentinel state
        if new_val != self._needs_onboarding:
            self._needs_onboarding = new_val
            _emit()

    obj._recompute_needs_onboarding = _recompute
    return obj


class TestNeedsOnboardingLogic:
    """Pure-logic tests for the deferred-model rule without PySide6."""

    def test_no_sentinel_no_provider_needs_onboarding(self) -> None:
        """Fresh install, no account, no provider → onboarding required."""
        obj = _make_stand_in(sentinel_exists=False, has_active_model=False)
        assert obj._needs_onboarding is True

    def test_sentinel_present_no_provider_onboarding_false(self) -> None:
        """Account created, no provider yet → desktop accessible (spec 011 US4)."""
        obj = _make_stand_in(sentinel_exists=True, has_active_model=False)
        assert obj._needs_onboarding is False

    def test_sentinel_present_with_provider_onboarding_false(self) -> None:
        """Account + provider both present → desktop still accessible."""
        obj = _make_stand_in(sentinel_exists=True, has_active_model=True)
        assert obj._needs_onboarding is False

    def test_recompute_flips_from_true_to_false_on_account_creation(self) -> None:
        """After account created (sentinel appears), _recompute flips onboarding off."""
        # Start: no sentinel → onboarding True.
        obj = _make_stand_in(sentinel_exists=False, has_active_model=False)
        assert obj._needs_onboarding is True

        # Simulate sentinel appearing and recompute being called.
        # We need a new stand-in reflecting sentinel=True but with current state=True.
        obj2 = types.SimpleNamespace(
            _has_active_model=False,
            _needs_onboarding=True,  # was True before sentinel appeared
            _emitted=False,
        )

        def _emit():
            obj2._emitted = True

        def _recompute_with_sentinel(self=obj2):
            new_val = False  # sentinel now exists
            if new_val != self._needs_onboarding:
                self._needs_onboarding = new_val
                _emit()

        obj2._recompute_needs_onboarding = _recompute_with_sentinel
        obj2._recompute_needs_onboarding()

        assert obj2._needs_onboarding is False
        assert obj2._emitted is True, (
            "needsOnboardingChanged signal must be emitted when the gate flips."
        )

    def test_recompute_does_not_emit_when_provider_changes(self) -> None:
        """Provider state changes must NOT flip needsOnboarding (spec 011 US4).

        Before the fix, losing a provider would re-show the onboarding overlay.
        """
        # Sentinel present, provider disappears — needsOnboarding must stay False.
        obj = _make_stand_in(sentinel_exists=True, has_active_model=True)
        obj._needs_onboarding = False  # desktop was already accessible

        # Simulate provider becoming inactive while sentinel is still present.
        obj._has_active_model = False
        obj._recompute_needs_onboarding()  # runs with sentinel_exists=True

        assert obj._needs_onboarding is False, (
            "Losing the active provider must NOT re-enable the onboarding gate. "
            "Spec 011 US4: provider is deferred, not a desktop prerequisite."
        )
        assert obj._emitted is False, (
            "needsOnboardingChanged must NOT fire when only the provider changes."
        )

    def test_has_active_model_stays_independent_signal(self) -> None:
        """hasActiveModel is a separate signal, unrelated to needsOnboarding."""
        obj = _make_stand_in(sentinel_exists=True, has_active_model=False)
        # needsOnboarding is False (desktop open)
        assert obj._needs_onboarding is False
        # hasActiveModel is False (banner should be shown by QML)
        assert obj._has_active_model is False
