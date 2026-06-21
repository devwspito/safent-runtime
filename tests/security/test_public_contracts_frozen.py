"""T008 — Architectural contract: public signatures of core ports MUST NOT change.

Constitution I / FR-028: BrowserPort, SelectorRegistry, StepRecorder,
BrowserSession, StorageStatePort and ReasoningEngine.run_cycle are the
frozen public API of the browser + reasoning bounded contexts. Any change
to these signatures is a breaking change that requires an explicit review
and a corresponding update to this test.

If this test goes RED it means a port signature was altered without consent.
Fix the port — not the test — unless the change is intentional and reviewed.

Scope: method names + parameter names (including keyword-only markers).
Return annotations and type literals are NOT checked here because they use
``from __future__ import annotations`` string forms that differ by Python
version; the shapes that matter are the call contracts (what callers pass).
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from hermes.browser.application.session import BrowserSession
from hermes.browser.application.step_recorder import StepRecorder
from hermes.browser.domain.port import BrowserPort
from hermes.browser.domain.ports.storage_state_port import StorageStatePort
from hermes.browser.domain.selector import SelectorRegistry
from hermes.runtime.engine import ReasoningEngine

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _public_methods(cls: type) -> dict[str, inspect.Signature]:
    """Return {method_name: Signature} for all public methods of *cls*."""
    result: dict[str, inspect.Signature] = {}
    for name, member in inspect.getmembers(cls):
        if name.startswith("_") and name != "__init__":
            continue
        if callable(member) or isinstance(inspect.getattr_static(cls, name, None), (classmethod, staticmethod)):
            try:
                sig = inspect.signature(member)
                result[name] = sig
            except (ValueError, TypeError):
                pass
    return result


def _param_names(sig: inspect.Signature) -> list[str]:
    """Ordered list of parameter names (excluding ``self``)."""
    return [
        name
        for name, param in sig.parameters.items()
        if name != "self"
    ]


def _kwonly_params(sig: inspect.Signature) -> set[str]:
    """Set of keyword-only parameter names."""
    return {
        name
        for name, param in sig.parameters.items()
        if param.kind == inspect.Parameter.KEYWORD_ONLY
    }


def _assert_method_exists(methods: dict[str, Any], name: str, cls_name: str) -> None:
    assert name in methods, (
        f"{cls_name}.{name} no longer exists — "
        f"Constitution I / FR-028 violation. Update port + callers, then update this test."
    )


def _assert_params(
    sig: inspect.Signature,
    expected_params: list[str],
    method_label: str,
) -> None:
    actual = _param_names(sig)
    assert actual == expected_params, (
        f"{method_label}: parameter list changed.\n"
        f"  Expected : {expected_params}\n"
        f"  Actual   : {actual}\n"
        f"Constitution I / FR-028 — fix the port, not this test."
    )


def _assert_kwonly(
    sig: inspect.Signature,
    expected_kwonly: set[str],
    method_label: str,
) -> None:
    actual = _kwonly_params(sig)
    assert actual == expected_kwonly, (
        f"{method_label}: keyword-only params changed.\n"
        f"  Expected : {sorted(expected_kwonly)}\n"
        f"  Actual   : {sorted(actual)}\n"
        f"Constitution I / FR-028 — fix the port, not this test."
    )


# ---------------------------------------------------------------------------
# BrowserPort
# ---------------------------------------------------------------------------


class TestBrowserPortFrozen:
    EXPECTED_METHODS = frozenset({"execute", "take_screenshot", "take_dom_snapshot", "current_url", "close"})

    def test_expected_methods_present(self) -> None:
        methods = _public_methods(BrowserPort)
        for name in self.EXPECTED_METHODS:
            _assert_method_exists(methods, name, "BrowserPort")

    def test_no_unexpected_public_methods_added(self) -> None:
        methods = _public_methods(BrowserPort)
        extra = {
            name for name in methods
            if name not in self.EXPECTED_METHODS and not name.startswith("_")
            and name not in {"capabilities", "driver_name"}  # declared @property, not callable via getmembers
        }
        # Properties (capabilities, driver_name) are not included — only methods.
        assert extra == set(), (
            f"BrowserPort gained new public methods: {extra}. "
            "Intentional? Update this test after review."
        )

    def test_execute_params(self) -> None:
        sig = inspect.signature(BrowserPort.execute)
        _assert_params(sig, ["step", "hitl_approval_token"], "BrowserPort.execute")
        _assert_kwonly(sig, {"hitl_approval_token"}, "BrowserPort.execute")

    def test_close_params(self) -> None:
        sig = inspect.signature(BrowserPort.close)
        _assert_params(sig, [], "BrowserPort.close")

    def test_take_screenshot_params(self) -> None:
        sig = inspect.signature(BrowserPort.take_screenshot)
        _assert_params(sig, [], "BrowserPort.take_screenshot")

    def test_take_dom_snapshot_params(self) -> None:
        sig = inspect.signature(BrowserPort.take_dom_snapshot)
        _assert_params(sig, [], "BrowserPort.take_dom_snapshot")

    def test_current_url_params(self) -> None:
        sig = inspect.signature(BrowserPort.current_url)
        _assert_params(sig, [], "BrowserPort.current_url")


# ---------------------------------------------------------------------------
# SelectorRegistry
# ---------------------------------------------------------------------------


class TestSelectorRegistryFrozen:
    EXPECTED_METHODS = frozenset({"fetch_latest", "history", "persist", "mark_deprecated", "touch_ok"})

    def test_expected_methods_present(self) -> None:
        methods = _public_methods(SelectorRegistry)
        for name in self.EXPECTED_METHODS:
            _assert_method_exists(methods, name, "SelectorRegistry")

    def test_fetch_latest_params(self) -> None:
        sig = inspect.signature(SelectorRegistry.fetch_latest)
        _assert_params(sig, ["site_id", "flow_id", "step_id", "tenant_scope"], "SelectorRegistry.fetch_latest")
        _assert_kwonly(sig, {"site_id", "flow_id", "step_id", "tenant_scope"}, "SelectorRegistry.fetch_latest")

    def test_history_params(self) -> None:
        sig = inspect.signature(SelectorRegistry.history)
        _assert_params(sig, ["site_id", "flow_id", "step_id", "tenant_scope"], "SelectorRegistry.history")
        _assert_kwonly(sig, {"site_id", "flow_id", "step_id", "tenant_scope"}, "SelectorRegistry.history")

    def test_persist_params(self) -> None:
        sig = inspect.signature(SelectorRegistry.persist)
        _assert_params(sig, ["selector"], "SelectorRegistry.persist")

    def test_mark_deprecated_params(self) -> None:
        sig = inspect.signature(SelectorRegistry.mark_deprecated)
        _assert_params(sig, ["selector_id", "reason"], "SelectorRegistry.mark_deprecated")
        _assert_kwonly(sig, {"reason"}, "SelectorRegistry.mark_deprecated")

    def test_touch_ok_params(self) -> None:
        sig = inspect.signature(SelectorRegistry.touch_ok)
        _assert_params(sig, ["selector_id"], "SelectorRegistry.touch_ok")


# ---------------------------------------------------------------------------
# StepRecorder
# ---------------------------------------------------------------------------


class TestStepRecorderFrozen:
    EXPECTED_METHODS = frozenset({"record_pre", "record_post"})

    def test_expected_methods_present(self) -> None:
        methods = _public_methods(StepRecorder)
        for name in self.EXPECTED_METHODS:
            _assert_method_exists(methods, name, "StepRecorder")

    def test_init_params(self) -> None:
        sig = inspect.signature(StepRecorder.__init__)
        _assert_params(sig, ["artifact_store", "sink"], "StepRecorder.__init__")
        _assert_kwonly(sig, {"artifact_store", "sink"}, "StepRecorder.__init__")

    def test_record_pre_params(self) -> None:
        sig = inspect.signature(StepRecorder.record_pre)
        _assert_params(sig, ["step", "screenshot", "dom_text"], "StepRecorder.record_pre")
        _assert_kwonly(sig, {"screenshot", "dom_text"}, "StepRecorder.record_pre")

    def test_record_post_params(self) -> None:
        sig = inspect.signature(StepRecorder.record_post)
        _assert_params(sig, ["step", "outcome", "screenshot", "dom_text"], "StepRecorder.record_post")
        _assert_kwonly(sig, {"screenshot", "dom_text"}, "StepRecorder.record_post")


# ---------------------------------------------------------------------------
# BrowserSession
# ---------------------------------------------------------------------------


class TestBrowserSessionFrozen:
    EXPECTED_METHODS = frozenset({"open", "navigate", "act", "observe", "extract", "close"})

    def test_expected_methods_present(self) -> None:
        methods = _public_methods(BrowserSession)
        for name in self.EXPECTED_METHODS:
            _assert_method_exists(methods, name, "BrowserSession")

    def test_init_params(self) -> None:
        sig = inspect.signature(BrowserSession.__init__)
        _assert_params(
            sig,
            ["config", "driver", "recorder", "storage_state_port", "storage_state_key"],
            "BrowserSession.__init__",
        )
        _assert_kwonly(
            sig,
            {"config", "driver", "recorder", "storage_state_port", "storage_state_key"},
            "BrowserSession.__init__",
        )

    def test_navigate_params(self) -> None:
        sig = inspect.signature(BrowserSession.navigate)
        _assert_params(sig, ["url", "intent_desc"], "BrowserSession.navigate")

    def test_act_params(self) -> None:
        sig = inspect.signature(BrowserSession.act)
        _assert_params(
            sig,
            ["instruction", "risk", "fill_value", "hitl_approval_token"],
            "BrowserSession.act",
        )

    def test_observe_params(self) -> None:
        sig = inspect.signature(BrowserSession.observe)
        _assert_params(sig, ["instruction"], "BrowserSession.observe")

    def test_extract_params(self) -> None:
        sig = inspect.signature(BrowserSession.extract)
        _assert_params(sig, ["instruction", "schema"], "BrowserSession.extract")
        _assert_kwonly(sig, {"instruction", "schema"}, "BrowserSession.extract")

    def test_close_params(self) -> None:
        sig = inspect.signature(BrowserSession.close)
        _assert_params(sig, [], "BrowserSession.close")


# ---------------------------------------------------------------------------
# StorageStatePort
# ---------------------------------------------------------------------------


class TestStorageStatePortFrozen:
    EXPECTED_METHODS = frozenset({"load", "save", "invalidate", "lock"})

    def test_expected_methods_present(self) -> None:
        methods = _public_methods(StorageStatePort)
        for name in self.EXPECTED_METHODS:
            _assert_method_exists(methods, name, "StorageStatePort")

    def test_load_params(self) -> None:
        sig = inspect.signature(StorageStatePort.load)
        _assert_params(sig, ["tenant_id", "site_id"], "StorageStatePort.load")
        _assert_kwonly(sig, {"tenant_id", "site_id"}, "StorageStatePort.load")

    def test_save_params(self) -> None:
        sig = inspect.signature(StorageStatePort.save)
        _assert_params(sig, ["state"], "StorageStatePort.save")

    def test_invalidate_params(self) -> None:
        sig = inspect.signature(StorageStatePort.invalidate)
        _assert_params(sig, ["tenant_id", "site_id", "reason"], "StorageStatePort.invalidate")
        _assert_kwonly(sig, {"tenant_id", "site_id", "reason"}, "StorageStatePort.invalidate")

    def test_lock_params(self) -> None:
        sig = inspect.signature(StorageStatePort.lock)
        _assert_params(sig, ["tenant_id", "site_id", "timeout_s"], "StorageStatePort.lock")
        _assert_kwonly(sig, {"tenant_id", "site_id", "timeout_s"}, "StorageStatePort.lock")


# ---------------------------------------------------------------------------
# ReasoningEngine.run_cycle — the most critical frozen signature
# ---------------------------------------------------------------------------


class TestReasoningEngineRunCycleFrozen:
    def test_run_cycle_exists(self) -> None:
        assert hasattr(ReasoningEngine, "run_cycle"), (
            "ReasoningEngine.run_cycle no longer exists — "
            "Constitution I / FR-028 violation."
        )

    def test_run_cycle_params(self) -> None:
        sig = inspect.signature(ReasoningEngine.run_cycle)
        _assert_params(sig, ["context"], "ReasoningEngine.run_cycle")

    def test_run_cycle_has_no_extra_params(self) -> None:
        sig = inspect.signature(ReasoningEngine.run_cycle)
        param_names = _param_names(sig)
        assert param_names == ["context"], (
            f"ReasoningEngine.run_cycle signature changed: params={param_names}. "
            "This is a Constitution I violation — chunk_sink and task_id are "
            "injected via DecisionContext.metadata, NOT as additional parameters."
        )
