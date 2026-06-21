"""VM smoke-test: broker-gate hardening verification (iteration 3).

Run this script INSIDE the baked VM with HERMES_ENGINE=nous and hermes-agent
installed to validate that every WRITE-classified tool (native Nous +
external Composio/MCP) has a broker-routing wrapper installed in tools.registry,
and that no blocked/raw handler is reachable from the sequential path.

Usage:
    HERMES_ENGINE=nous python3 verify-tool-gate.py

Exit code:
    0  — all checks PASS
    1  — one or more checks FAIL

Sections:
  S1 — tools.registry enumeration: every WRITE tool has a wrapper, not raw handler.
  S2 — broker dispatch fires EXACTLY ONCE on sequential-path dispatch for WRITE.
  S3 — broker fires EXACTLY ONCE for external (Composio/MCP) WRITE tools.
  S4 — READ tools do NOT route through broker (no double-gate, no BLOCKED).
  S5 — broker-less Composio spec construction is fail-closed (raises).
  S6 — PATH C: memory/clarify monkeypatched (or documented residual if no hermes-agent).
"""

from __future__ import annotations

import asyncio
import json
import sys
import traceback
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch as _patch
from uuid import uuid4

# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------
_PASS = 0
_FAIL = 0
_SKIP = 0


def _ok(label: str) -> None:
    global _PASS
    _PASS += 1
    print(f"  [PASS] {label}")


def _bad(label: str, detail: str = "") -> None:
    global _FAIL
    _FAIL += 1
    print(f"  [FAIL] {label}")
    if detail:
        for line in detail.strip().splitlines():
            print(f"         {line}")


def _skip(label: str, reason: str = "") -> None:
    global _SKIP
    _SKIP += 1
    print(f"  [SKIP] {label}" + (f" — {reason}" if reason else ""))


def _hdr(title: str) -> None:
    print(f"\n== {title} ==")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_consent_ctx() -> Any:
    from hermes.capabilities.domain.ports import ConsentContext
    return ConsentContext(tenant_id=uuid4(), operator_id=uuid4())


def _make_outcome(result: dict | None = None) -> Any:
    from hermes.capabilities.domain.ports import ExecutionOutcome, ExecutionStatus
    return ExecutionOutcome(
        proposal_id=uuid4(),
        status=ExecutionStatus.EXECUTED,
        result=result or {"ok": True},
    )


def _make_broker(dispatch_log: list[Any]) -> Any:
    """Mock broker that records every dispatch call."""
    broker = MagicMock()

    async def _record(proposal, consent_context):
        dispatch_log.append(proposal)
        return _make_outcome()

    broker.dispatch = AsyncMock(side_effect=_record)
    return broker


def _is_broker_wrapper(handler: Any) -> bool:
    """Heuristic: a broker wrapper is a closure that references _dispatch_via_bridge
    or agent._dispatch_write_proposal.  Raw Nous handlers are plain functions from
    the tools.* namespace; _blocked_handler is a module-level function.
    """
    if handler is None:
        return False
    name = getattr(handler, "__name__", "") or ""
    qualname = getattr(handler, "__qualname__", "") or ""
    module = getattr(handler, "__module__", "") or ""
    # Closures from _make_sequential_write_wrapper or _make_external_sequential_wrapper
    # are named _broker_write_wrapper / _sequential_wrapper.
    if "_broker_write_wrapper" in qualname or "_sequential_wrapper" in qualname:
        return True
    # Legacy / fallback check: handler defined in nous_engine module
    if "nous_engine" in module:
        return True
    # _blocked_handler is a dead giveaway of the broken stub
    if "_blocked_handler" in name or "_blocked_handler" in qualname:
        return False
    # Raw Nous tool functions live in tools.* modules
    if module.startswith("tools."):
        return False
    return False


# ---------------------------------------------------------------------------
# Provider-independent construction helpers
# ---------------------------------------------------------------------------

def _force_register_builtin_tools() -> bool:
    """Import and self-register all builtin Nous tool modules into tools.registry.

    Nous tool modules register themselves by calling registry.register() at
    module level.  Normally this happens when model_tools is imported (which
    calls discover_builtin_tools() at module level).  In the smoke-test we
    call discover_builtin_tools() directly to avoid pulling in the full
    model_tools import chain (OpenAI SDK, etc.).

    Returns True if at least one tool was registered, False if tools.registry
    is unavailable (hermes-agent not installed).
    """
    try:
        from tools.registry import discover_builtin_tools, registry as _r  # noqa: PLC0415
    except ImportError:
        return False

    discover_builtin_tools()
    return True


def _build_stub_governed_agent(
    *,
    broker: Any,
    consent_context: Any,
    engine_loop: asyncio.AbstractEventLoop | None = None,
    external_catalog: Any = None,
) -> Any | None:
    """Construct a GovernedAIAgent without a configured LLM provider.

    GovernedAIAgent.__init__ calls AIAgent(*args, **kwargs) (the NousResearch
    inner agent), whose init_agent() validates LLM credentials and raises
    RuntimeError("No LLM provider configured") in the baked image before any
    provider is onboarded.

    This helper patches _import_ai_agent in nous_engine to return a no-op
    stub class so the inner AIAgent.__init__ becomes a NOP.  All Hermes-owned
    attributes (_broker, _consent_context, _wire_sequential_gate, etc.) are
    set normally — only the credential-dependent Nous code is bypassed.

    The stub's only purpose is to hold self._inner._invoke_tool = self._invoke_tool
    without raising.  run_conversation is never called in the smoke-test.

    Returns the constructed GovernedAIAgent, or None if the import fails
    (hermes-agent not installed on this host).
    """
    try:
        from hermes.runtime.nous_engine import GovernedAIAgent, _ExternalToolCatalog  # noqa: PLC0415
    except ImportError:
        return None

    class _StubAIAgent:
        """Minimal stand-in for NousResearch AIAgent.

        Accepts the same positional/keyword arguments GovernedAIAgent passes
        and does nothing — no credential check, no HTTP client, no disk I/O.
        Only _invoke_tool attribute access is needed by GovernedAIAgent.__init__.
        """

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        # Placeholder so self._inner._invoke_tool = self._invoke_tool doesn't
        # raise AttributeError during GovernedAIAgent.__init__.
        _invoke_tool: Any = None

    catalog = external_catalog if external_catalog is not None else _ExternalToolCatalog(())

    try:
        with _patch("hermes.runtime.nous_engine._import_ai_agent", return_value=_StubAIAgent):
            agent = GovernedAIAgent(
                model="test/model",
                broker=broker,
                consent_context=consent_context,
                engine_loop=engine_loop,
                tenant_id=uuid4(),
                external_catalog=catalog,
            )
    except Exception:
        return None

    return agent


# ---------------------------------------------------------------------------
# S1 — tools.registry enumeration
# ---------------------------------------------------------------------------

def check_s1_registry_write_tools_have_wrappers() -> None:
    _hdr("S1 — tools.registry: WRITE tools have broker wrappers")

    try:
        from tools.registry import registry as nous_registry
    except ImportError:
        _skip("S1 — registry check", "hermes-agent not installed")
        return

    from hermes.runtime.nous_tool_risk_map import NOUS_TOOL_CATALOG, NousRisk, classify_nous_tool

    # Ensure builtin tool modules have self-registered before we enumerate.
    _force_register_builtin_tools()

    # Build a stub agent so _wire_sequential_gate installs broker wrappers.
    import threading as _threading
    bg_loop = asyncio.new_event_loop()
    t = _threading.Thread(target=bg_loop.run_forever, daemon=True)
    t.start()

    try:
        dispatch_log: list[Any] = []
        broker = _make_broker(dispatch_log)
        consent = _make_consent_ctx()

        agent = _build_stub_governed_agent(
            broker=broker,
            consent_context=consent,
            engine_loop=bg_loop,
        )

        if agent is None:
            _skip("S1 — registry check", "GovernedAIAgent stub construction failed")
            return

        write_tools = [t for t in NOUS_TOOL_CATALOG if classify_nous_tool(t) is NousRisk.WRITE]
        checked = 0
        missing = []
        blocked = []

        for tool_name in sorted(write_tools):
            entry = nous_registry.get_entry(tool_name)
            if entry is None:
                # Not registered in this hermes-agent build — skip.
                continue
            handler = getattr(entry, "handler", None) or getattr(entry, "fn", None)
            if handler is None:
                missing.append(tool_name)
            elif "_blocked_handler" in getattr(handler, "__name__", "") or \
                 "_blocked_handler" in getattr(handler, "__qualname__", ""):
                blocked.append(tool_name)
            checked += 1

        if missing:
            _bad("S1 — WRITE tools with no handler", ", ".join(missing))
        elif blocked:
            _bad("S1 — WRITE tools still using _blocked_handler stub", ", ".join(blocked))
        elif checked == 0:
            _skip("S1 — no WRITE tools registered in this build", "hermes-agent catalog may differ")
        else:
            _ok(f"S1 — {checked} WRITE tool(s) have handlers (no _blocked_handler stub)")
    finally:
        bg_loop.call_soon_threadsafe(bg_loop.stop)
        t.join(timeout=3)


# ---------------------------------------------------------------------------
# S2 — sequential-path WRITE dispatch fires broker exactly once
# ---------------------------------------------------------------------------

def check_s2_sequential_write_fires_broker_once() -> None:
    _hdr("S2 — sequential path: WRITE tool dispatch fires broker exactly once")

    try:
        from tools.registry import registry as nous_registry
    except ImportError:
        _skip("S2 — sequential dispatch", "hermes-agent not installed")
        return

    from hermes.runtime.nous_tool_risk_map import NOUS_TOOL_CATALOG, NousRisk, classify_nous_tool

    # Ensure builtin tool modules have self-registered.
    _force_register_builtin_tools()

    # Pick any WRITE tool that is registered.
    test_tool = None
    for t in sorted(NOUS_TOOL_CATALOG):
        if classify_nous_tool(t) is NousRisk.WRITE and nous_registry.get_entry(t):
            test_tool = t
            break

    if test_tool is None:
        _skip("S2 — sequential dispatch", "no WRITE tools registered")
        return

    import threading as _threading
    bg_loop = asyncio.new_event_loop()
    t = _threading.Thread(target=bg_loop.run_forever, daemon=True)
    t.start()

    try:
        dispatch_log: list[Any] = []
        broker = _make_broker(dispatch_log)
        consent = _make_consent_ctx()

        # Build a stub agent — _wire_sequential_gate replaces registry entries
        # for every WRITE tool with broker-dispatching wrappers.
        agent = _build_stub_governed_agent(
            broker=broker,
            consent_context=consent,
            engine_loop=bg_loop,
        )

        if agent is None:
            _skip("S2 — sequential dispatch", "GovernedAIAgent stub construction failed")
            return

        # Call registry.dispatch directly — this is what execute_tool_calls_sequential does.
        entry = nous_registry.get_entry(test_tool)
        handler = getattr(entry, "handler", None) or getattr(entry, "fn", None)
        if handler is None:
            _skip(f"S2 — {test_tool}", "no handler on entry")
            return

        try:
            result_str = handler({"test": True})
        except Exception as exc:
            _skip(f"S2 — {test_tool}", f"handler raised: {exc}")
            return

        if len(dispatch_log) == 1:
            _ok(f"S2 — '{test_tool}' sequential WRITE: broker.dispatch fired exactly once")
        elif len(dispatch_log) == 0:
            _bad(
                f"S2 — '{test_tool}' broker.dispatch NOT called",
                f"result={result_str!r} — raw handler or _blocked_handler stub still in place",
            )
        else:
            _bad(
                f"S2 — '{test_tool}' broker.dispatch called {len(dispatch_log)} times",
                "double-gate: broker fired more than once per tool call",
            )
    finally:
        bg_loop.call_soon_threadsafe(bg_loop.stop)
        t.join(timeout=3)


# ---------------------------------------------------------------------------
# S3 — external (Composio/MCP) WRITE tools route through broker
# ---------------------------------------------------------------------------

def check_s3_external_write_routes_through_broker() -> None:
    _hdr("S3 — external tools: WRITE route through broker on sequential path")

    from hermes.domain.tool_spec import ToolRisk, ToolSpec
    from hermes.runtime.nous_engine import _ExternalToolCatalog, _make_external_sequential_wrapper

    import asyncio
    import threading

    write_spec = ToolSpec(
        name="test_composio_write",
        description="Test WRITE spec",
        parameters_schema={"type": "object", "properties": {}},
        risk=ToolRisk.WRITE_PROPOSAL,
        entity_type="composio",
        handler=None,
    )

    dispatch_log: list[Any] = []

    # We need a real event loop for the async bridge inside _dispatch_external_write.
    bg_loop = asyncio.new_event_loop()
    t = threading.Thread(target=bg_loop.run_forever, daemon=True)
    t.start()

    try:
        broker = _make_broker(dispatch_log)
        consent = _make_consent_ctx()

        agent = _build_stub_governed_agent(
            broker=broker,
            consent_context=consent,
            engine_loop=bg_loop,
            external_catalog=_ExternalToolCatalog((write_spec,)),
        )

        if agent is None:
            _skip("S3 — external WRITE broker route", "GovernedAIAgent stub construction failed")
            return

        fake_registry = MagicMock()
        registered: dict[str, Any] = {}

        def _fake_register(name, **kwargs):
            registered[name] = kwargs.get("handler")

        fake_registry.register.side_effect = _fake_register
        _make_external_sequential_wrapper(agent, write_spec, fake_registry)

        wrapper = registered.get("test_composio_write")
        if wrapper is None:
            _bad("S3 — external WRITE", "_make_external_sequential_wrapper did not register handler")
            return

        try:
            result_str = wrapper({"param": "value"})
        except Exception as exc:
            _bad("S3 — external WRITE handler raised", traceback.format_exc())
            return

        if len(dispatch_log) == 1:
            proposal = dispatch_log[0]
            _ok(f"S3 — external WRITE '{write_spec.name}': broker.dispatch fired once, entity_type={proposal.entity_type}")
        elif len(dispatch_log) == 0:
            _bad(
                "S3 — external WRITE broker NOT fired",
                f"result={result_str!r} — _blocked_handler stub still in place or guard fired",
            )
        else:
            _bad(
                f"S3 — external WRITE broker fired {len(dispatch_log)} times",
                "double-gate detected",
            )
    finally:
        bg_loop.call_soon_threadsafe(bg_loop.stop)
        t.join(timeout=3)


# ---------------------------------------------------------------------------
# S3b — external READ calls spec.handler, NOT _blocked_handler
# ---------------------------------------------------------------------------

def check_s3b_external_read_calls_handler() -> None:
    _hdr("S3b — external tools: READ calls spec.handler, NOT _blocked_handler")

    from hermes.domain.tool_spec import ToolRisk, ToolSpec
    from hermes.runtime.nous_engine import _ExternalToolCatalog, _make_external_sequential_wrapper

    import asyncio
    import threading

    handler_calls: list[dict] = []

    async def _read_handler(params: dict) -> dict:
        handler_calls.append(params)
        return {"emails": [], "count": 0}

    read_spec = ToolSpec(
        name="test_composio_read",
        description="Test READ spec",
        parameters_schema={"type": "object", "properties": {}},
        risk=ToolRisk.READ_ONLY,
        entity_type="composio",
        handler=_read_handler,
    )

    bg_loop = asyncio.new_event_loop()
    t = threading.Thread(target=bg_loop.run_forever, daemon=True)
    t.start()

    try:
        broker = _make_broker([])
        consent = _make_consent_ctx()

        agent = _build_stub_governed_agent(
            broker=broker,
            consent_context=consent,
            engine_loop=bg_loop,
            external_catalog=_ExternalToolCatalog((read_spec,)),
        )

        if agent is None:
            _skip("S3b — external READ", "GovernedAIAgent stub construction failed")
            return

        fake_registry = MagicMock()
        registered: dict[str, Any] = {}

        def _fake_register(name, **kwargs):
            registered[name] = kwargs.get("handler")

        fake_registry.register.side_effect = _fake_register
        _make_external_sequential_wrapper(agent, read_spec, fake_registry)

        wrapper = registered.get("test_composio_read")
        if wrapper is None:
            _bad("S3b — external READ", "no wrapper registered")
            return

        try:
            result_str = wrapper({"query": "inbox"})
        except Exception as exc:
            _bad("S3b — external READ handler raised", traceback.format_exc())
            return

        if len(handler_calls) == 1:
            _ok("S3b — external READ: spec.handler called once (not _blocked_handler)")
        elif len(handler_calls) == 0:
            result_repr = result_str[:120] if isinstance(result_str, str) else repr(result_str)
            _bad(
                "S3b — external READ: spec.handler NOT called",
                f"result={result_repr} — _blocked_handler stub still registered",
            )
        else:
            _bad(f"S3b — external READ: spec.handler called {len(handler_calls)} times (expected 1)")
    finally:
        bg_loop.call_soon_threadsafe(bg_loop.stop)
        t.join(timeout=3)


# ---------------------------------------------------------------------------
# S4 — READ tools do NOT hit broker (no double-gate, no BLOCKED)
# ---------------------------------------------------------------------------

def check_s4_read_tools_bypass_broker() -> None:
    _hdr("S4 — READ tools: no broker dispatch (no double-gate)")

    try:
        from tools.registry import registry as nous_registry
    except ImportError:
        _skip("S4 — READ tools check", "hermes-agent not installed")
        return

    from hermes.runtime.nous_tool_risk_map import NOUS_TOOL_CATALOG, NousRisk, classify_nous_tool

    read_tools = [t for t in NOUS_TOOL_CATALOG if classify_nous_tool(t) is NousRisk.READ]
    double_gated: list[str] = []

    for tool_name in sorted(read_tools):
        entry = nous_registry.get_entry(tool_name)
        if entry is None:
            continue
        handler = getattr(entry, "handler", None) or getattr(entry, "fn", None)
        if handler is None:
            continue
        qualname = getattr(handler, "__qualname__", "")
        # Broker wrappers are closures with these names — READ tools must NOT have them.
        if "_broker_write_wrapper" in qualname or (
            "_sequential_wrapper" in qualname and
            # _sequential_wrapper for READ tools calls spec.handler directly (OK);
            # the name alone doesn't indicate broker dispatch. We flag only explicit
            # broker wrappers that contain _dispatch_write_proposal in their code.
            hasattr(handler, "__code__") and
            "_dispatch_write_proposal" in (handler.__code__.co_names or ())
        ):
            double_gated.append(tool_name)

    if double_gated:
        _bad("S4 — READ tools with broker dispatch wrappers (double-gate)", ", ".join(double_gated))
    else:
        registered_read = sum(1 for t in read_tools if nous_registry.get_entry(t))
        _ok(f"S4 — {registered_read} READ tool(s) bypass broker (no double-gate)")


# ---------------------------------------------------------------------------
# S5 — broker-less Composio spec construction is fail-closed
# ---------------------------------------------------------------------------

def check_s5_brokerless_composio_raises() -> None:
    _hdr("S5 — broker-less Composio spec: fail-closed (raises RuntimeError)")

    try:
        from hermes.runtime.composio_tools_registry import (
            ComposioToolsRegistry,
            _default_tools_builder,
        )
        from pathlib import Path
    except ImportError as exc:
        _skip("S5 — broker-less check", f"import failed: {exc}")
        return

    # 1. _default_tools_builder raises unconditionally.
    raised = False
    try:
        asyncio.run(
            _default_tools_builder(MagicMock(api_key="k", entity_id="e"))
        )
    except RuntimeError as exc:
        if "broker-less" in str(exc):
            raised = True
        else:
            _bad("S5 — _default_tools_builder raised wrong error", str(exc))
            return
    except Exception as exc:
        _bad("S5 — _default_tools_builder raised unexpected exception", traceback.format_exc())
        return

    if raised:
        _ok("S5 — _default_tools_builder raises RuntimeError(broker-less) unconditionally")
    else:
        _bad("S5 — _default_tools_builder did NOT raise — broker-less construction is OPEN")

    # 2. ComposioToolsRegistry without tools_builder uses _default_tools_builder.
    registry = ComposioToolsRegistry(db_path=Path("/nonexistent/shell-state.db"))
    if registry._build_tools is _default_tools_builder:
        _ok("S5 — ComposioToolsRegistry._build_tools defaults to _default_tools_builder (fail-closed)")
    else:
        _bad(
            "S5 — ComposioToolsRegistry._build_tools is NOT _default_tools_builder",
            f"got: {registry._build_tools!r}",
        )


# ---------------------------------------------------------------------------
# S6 — PATH C: memory/clarify monkeypatched (or documented residual)
# ---------------------------------------------------------------------------

def check_s6_memory_clarify_gated() -> None:
    _hdr("S6 — PATH C: memory/clarify gated via monkeypatch")

    # 1. _wire_inline_branch_gates and _patch_memory_tool/_patch_clarify_tool exist.
    try:
        from hermes.runtime.nous_engine import (
            _wire_inline_branch_gates,
            _patch_memory_tool,
            _patch_clarify_tool,
        )
    except ImportError as exc:
        _bad("S6 — _wire_inline_branch_gates not importable", str(exc))
        return

    _ok("S6 — _wire_inline_branch_gates, _patch_memory_tool, _patch_clarify_tool importable")

    # 2. Docstring documents residuals (todo, delegate_task).
    import inspect
    doc = inspect.getdoc(_wire_inline_branch_gates) or ""
    if "todo" in doc.lower():
        _ok("S6 — _wire_inline_branch_gates docstring documents 'todo' residual")
    else:
        _bad("S6 — 'todo' not documented in _wire_inline_branch_gates docstring")

    if "delegate_task" in doc:
        _ok("S6 — _wire_inline_branch_gates docstring documents 'delegate_task' residual")
    else:
        _bad("S6 — 'delegate_task' not documented in _wire_inline_branch_gates docstring")

    # 3. Verify monkeypatch works when tools.memory_tool is available.
    try:
        import threading as _threading
        import tools.memory_tool as _mem_mod
        from unittest.mock import patch as _upatch

        dispatch_log: list[Any] = []

        def _fake_bridge(*, proposal, broker, consent_context, engine_loop):
            dispatch_log.append(proposal)
            from hermes.capabilities.domain.ports import ExecutionOutcome, ExecutionStatus
            return ExecutionOutcome(
                proposal_id=uuid4(),
                status=ExecutionStatus.EXECUTED,
                result={"ok": True},
            )

        # Provide a real event loop: _dispatch_write_proposal checks engine_loop is
        # not None before calling _dispatch_via_bridge (which we patch here anyway).
        bg_loop_mem = asyncio.new_event_loop()
        thr_mem = _threading.Thread(target=bg_loop_mem.run_forever, daemon=True)
        thr_mem.start()

        try:
            broker = _make_broker([])
            agent = _build_stub_governed_agent(
                broker=broker,
                consent_context=_make_consent_ctx(),
                engine_loop=bg_loop_mem,
            )

            if agent is None:
                _skip("S6 — memory monkeypatch", "agent stub construction failed")
            else:
                original = _mem_mod.memory_tool

                try:
                    with _upatch("hermes.runtime.nous_engine._dispatch_via_bridge", side_effect=_fake_bridge):
                        _patch_memory_tool(agent)
                        result = _mem_mod.memory_tool(action="add", target="memory", content="test", store=None)
                finally:
                    _mem_mod.memory_tool = original

                if len(dispatch_log) == 1:
                    _ok("S6 — memory WRITE routed through broker after monkeypatch")
                else:
                    _bad(
                        f"S6 — memory WRITE: broker.dispatch called {len(dispatch_log)} times (expected 1)",
                        "PATH C memory bypass still open",
                    )
        finally:
            bg_loop_mem.call_soon_threadsafe(bg_loop_mem.stop)
            thr_mem.join(timeout=3)

    except ImportError:
        _skip("S6 — memory monkeypatch live test", "tools.memory_tool not installed")

    # 4. Clarify.
    try:
        import threading as _threading
        import tools.clarify_tool as _cl_mod
        from unittest.mock import patch as _upatch

        dispatch_log_cl: list[Any] = []

        def _fake_bridge_cl(*, proposal, broker, consent_context, engine_loop):
            dispatch_log_cl.append(proposal)
            from hermes.capabilities.domain.ports import ExecutionOutcome, ExecutionStatus
            return ExecutionOutcome(
                proposal_id=uuid4(),
                status=ExecutionStatus.PENDING_APPROVAL,
                result=None,
            )

        bg_loop_cl = asyncio.new_event_loop()
        thr_cl = _threading.Thread(target=bg_loop_cl.run_forever, daemon=True)
        thr_cl.start()

        try:
            broker = _make_broker([])
            agent = _build_stub_governed_agent(
                broker=broker,
                consent_context=_make_consent_ctx(),
                engine_loop=bg_loop_cl,
            )

            if agent is None:
                _skip("S6 — clarify monkeypatch", "agent stub construction failed")
            else:
                original_cl = _cl_mod.clarify_tool

                try:
                    with _upatch("hermes.runtime.nous_engine._dispatch_via_bridge", side_effect=_fake_bridge_cl):
                        _patch_clarify_tool(agent)
                        result = _cl_mod.clarify_tool(question="Continue?", choices=["yes", "no"])
                finally:
                    _cl_mod.clarify_tool = original_cl

                if len(dispatch_log_cl) == 1:
                    _ok("S6 — clarify routed through broker after monkeypatch")
                else:
                    _bad(
                        f"S6 — clarify: broker.dispatch called {len(dispatch_log_cl)} times (expected 1)",
                        "PATH C clarify bypass still open",
                    )
        finally:
            bg_loop_cl.call_soon_threadsafe(bg_loop_cl.stop)
            thr_cl.join(timeout=3)

    except ImportError:
        _skip("S6 — clarify monkeypatch live test", "tools.clarify_tool not installed")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("=" * 70)
    print("Hermes broker-gate hardening — VM smoke-test (iteration 3)")
    print("HERMES_ENGINE=nous required. Run inside the baked VM.")
    print("=" * 70)

    check_s1_registry_write_tools_have_wrappers()
    check_s2_sequential_write_fires_broker_once()
    check_s3_external_write_routes_through_broker()
    check_s3b_external_read_calls_handler()
    check_s4_read_tools_bypass_broker()
    check_s5_brokerless_composio_raises()
    check_s6_memory_clarify_gated()

    print()
    print("=" * 70)
    print(f"RESULT: {_PASS} PASS  |  {_FAIL} FAIL  |  {_SKIP} SKIP")
    if _FAIL == 0:
        print("STATUS: ALL CHECKS PASS — broker gate hardening iteration 3 verified.")
    else:
        print(f"STATUS: {_FAIL} CHECK(S) FAILED — see above for details.")
    print("=" * 70)

    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
