"""Enterprise approval-routing resolution — single source of truth for
"WHO resolves this action's HITL approval" (Fase 2 Phase 4d).

Extracted out of `hermes.runtime.security_hook` so BOTH approval seams that
register a pending approval consult the EXACT SAME resolution logic and can
never diverge on which tenant/agent/tool combination reaches ENTERPRISE:

  - `hermes.runtime.security_hook._compute_danger_route` — NATIVE tools that
    bypass the broker entirely (terminal/execute_code/send_message/...).
  - `hermes.capabilities.application.capability_broker.CapabilityBroker.
    dispatch` — capability/MCP/install tools that DO go through the broker
    (install_app/install_mcp/install_skill/skill_manage/set_policy/...).

Neither call site re-derives the eligibility calculus; both resolve
`agent_managed_by`/`tenant_remote_approval_enabled`/`approval_tier` via the
functions below and feed them into the SAME `hermes.capabilities.
approval_router.route()` (TOTP-keyed: ENTERPRISE iff the tenant gate holds
AND `tool_delicacy.is_mfa_required(tool)` — see that module's docstring for
the full model).

Fail-soft to (None / False / "standard") on any per-field resolution error —
mirrors `_check_agent_access_scope`'s own fail-open posture: this is an
observability consult that feeds a routing decision, never an enforcement
gate itself, so it must never raise into its caller.

Capa: infrastructure (reads the association store's SQLite-backed license
flag via lazy imports — mirrors the exact pattern this module was extracted
from). Consumed by BOTH the application layer (capability_broker) and the
runtime layer (security_hook) — never imported by domain layers.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from hermes.capabilities.approval_router import ApprovalRoute, route
from hermes.capabilities.tool_sensitivity import sensitivity

logger = logging.getLogger("hermes.capabilities.enterprise_approval_routing")


def _resolve_ambient_agent_id(explicit_agent_id: str | None) -> str:
    """Resolve the agent_id to key the access-scope lookup by.

    `explicit_agent_id` (Fase 2 Phase 4e): when the caller already resolved
    the identity on the CORRECT thread (e.g. CapabilityBroker.dispatch, via
    ConsentContext.agent_id — see that field's docstring for why this exists:
    the broker's coroutine runs on the event-loop thread via
    run_coroutine_threadsafe, a DIFFERENT OS thread than the one
    conversation_task_registry's thread-locals are stamped on), use it
    directly — NEVER fall through to the thread-local in that case.

    None (default) falls back to `get_current_cycle_agent()` — UNCHANGED
    behavior for the native-danger gate (`security_hook._compute_danger_
    route`), which runs on the SAME executor thread that stamps it.
    """
    if explicit_agent_id:
        return explicit_agent_id
    from hermes.runtime.conversation_task_registry import (  # noqa: PLC0415
        get_current_cycle_agent,
    )

    return get_current_cycle_agent()


def resolve_agent_managed_by(
    access_scope_repo: Any, tenant_id: str, agent_id: str | None = None,
) -> str | None:
    """Best-effort `managed_by` ("cloud" | None) for the calling agent.

    Enterprise governance, Fase 2 Phase 4a — feeds approval_router.route()'s
    tenant-gate check. Fails soft to None (=> never cloud-gated => LOCAL) on
    any error: no repo wired, no ambient agent, no scope row, or a lookup
    failure all degrade to "not cloud-managed". `agent_id` — see
    `_resolve_ambient_agent_id`'s docstring.
    """
    try:
        if access_scope_repo is None:
            return None

        resolved_agent_id = _resolve_ambient_agent_id(agent_id)
        if not resolved_agent_id:
            return None

        scope = access_scope_repo.get_scope(resolved_agent_id, tenant_id)
        return scope.managed_by if scope is not None else None
    except Exception:  # noqa: BLE001 — fail-soft: unknown managed_by => not cloud-gated
        return None


def resolve_agent_approval_tier(
    access_scope_repo: Any, tenant_id: str, agent_id: str | None = None,
) -> str:
    """Best-effort approval tier ("coordinator" | "standard") for the calling
    agent — threaded into approval_router.route() for OBSERVABILITY/back-compat
    only (Fase 2 Phase 4c: the TOTP-keyed model made this INERT for routing — a
    coordinator and a standard agent now route the SAME tool identically, since
    the deciding question is "does the worker hold a TOTP for this action", not
    agent trust tier). Fails CLOSED to "standard" on ANY error — harmless today
    (route() ignores the value), kept as the conservative default in case a
    future consumer resumes reading it. `agent_id` — see
    `_resolve_ambient_agent_id`'s docstring."""
    try:
        if access_scope_repo is None:
            return "standard"

        resolved_agent_id = _resolve_ambient_agent_id(agent_id)
        if not resolved_agent_id:
            return "standard"
        scope = access_scope_repo.get_scope(resolved_agent_id, tenant_id)
        return scope.approval_tier if scope is not None else "standard"
    except Exception:  # noqa: BLE001 — fail-closed: unknown tier => max gating (standard)
        return "standard"


_REMOTE_APPROVAL_FLAG_TTL_S: float = 10.0
# TTL cache for the tenant remote-approval flag. tenant_remote_approval_enabled
# sits on BOTH approval seams' hot path (evaluated for every HITL-required tool
# call) — caching avoids a fresh SQLite connection per call. A read that is
# stale by up to the TTL is safe: this flag only ever decides WHO resolves an
# approval that was ALREADY going to block (LOCAL vs ENTERPRISE); it can never
# skip the gate itself (I-3).
_remote_approval_flag_cache: dict[str, Any] = {"value": False, "expires_at": 0.0}


def tenant_remote_approval_enabled() -> bool:
    """Real accessor for the tenant's remote-approval flag (Fase 2 Phase 4b).

    True ONLY when this instance is paired/associated (Enterprise edition) AND
    the tenant's currently-applied license carries
    `remote_approval_enabled=True` — pushed by the cloud via
    `config_sync.policy_document.LicenseSpec` and persisted in
    `association_store.license_json` (the SAME store `feature_guard.py` reads
    for view entitlements; see `config_sync/applier.py`'s NOTE on
    `store.update_license()`). Fail-safe False on ANY error: unpaired instance,
    missing store, corrupt license, DB unavailable — a read failure NEVER
    widens who can approve, it only ever falls back to today's LOCAL path.
    """
    now = time.monotonic()
    if now < _remote_approval_flag_cache["expires_at"]:
        return bool(_remote_approval_flag_cache["value"])

    value = _read_tenant_remote_approval_flag()
    _remote_approval_flag_cache["value"] = value
    _remote_approval_flag_cache["expires_at"] = now + _REMOTE_APPROVAL_FLAG_TTL_S
    return value


def _read_tenant_remote_approval_flag() -> bool:
    """Uncached read of the association store's license.remote_approval_enabled.

    Never raises — any failure degrades to False (fail-safe: LOCAL stays the
    only reachable route for every tool call until this explicitly returns True).
    """
    try:
        import os  # noqa: PLC0415
        from pathlib import Path  # noqa: PLC0415

        from hermes.instance.association_store import SQLiteAssociationStore  # noqa: PLC0415
        from hermes.shell_server.security.secrets import SecretsVault  # noqa: PLC0415

        db_path = Path(
            os.environ.get(
                "HERMES_SHELL_DB",
                os.environ.get("HERMES_STATE_DB", "/var/lib/hermes/shell-state.db"),
            )
        )
        store = SQLiteAssociationStore(db_path=db_path, vault=SecretsVault())
        if not store.is_associated():
            return False
        assoc = store.get()
        if assoc is None:
            return False
        return bool(assoc.license.get("remote_approval_enabled", False))
    except Exception as exc:  # noqa: BLE001 — fail-safe: any error => False
        logger.warning(
            "hermes.capabilities.enterprise_approval_routing."
            "tenant_remote_approval_flag_read_failed error=%r — defaulting False (LOCAL)",
            exc,
        )
        return False


def resolve_route_and_context(
    *, tool_name: str, args: dict[str, Any], access_scope_repo: Any, tenant_id: str,
    agent_id: str | None = None,
) -> tuple[ApprovalRoute, frozenset]:
    """Resolve (route, sensitivity_categories) for ONE already-approvable tool
    call — the function BOTH approval seams call, so they can never diverge.

    `agent_id` (Fase 2 Phase 4e): pass the EXPLICIT identity here when the
    caller resolved it on its own thread (CapabilityBroker.dispatch — see
    `_resolve_ambient_agent_id`'s docstring). None (default) preserves the
    native-danger gate's existing thread-local fallback, UNCHANGED.

    Fail-soft to (LOCAL, frozenset()) on ANY error: a bug in this consult must
    never widen who can approve nor auto-execute an action — worst case, the
    LOCAL path runs exactly as if this consult never ran.

    `sensitivity_categories` no longer feeds the routing decision (Fase 2
    Phase 4c) — it is still classified and returned as CONTEXT for an
    ENTERPRISE-routed row's remote admin, without re-classifying the call twice.
    """
    try:
        categories = sensitivity(tool_name, args)
        resolved_route = route(
            tool=tool_name,
            agent_managed_by=resolve_agent_managed_by(access_scope_repo, tenant_id, agent_id),
            tenant_remote_approval_enabled=tenant_remote_approval_enabled(),
            approval_tier=resolve_agent_approval_tier(access_scope_repo, tenant_id, agent_id),
        )
        return resolved_route, categories
    except Exception as exc:  # noqa: BLE001 — fail-soft: never widen/skip the gate
        logger.warning(
            "hermes.capabilities.enterprise_approval_routing.resolve_failed "
            "tool=%s error=%r — falling back to LOCAL",
            tool_name,
            exc,
        )
        return ApprovalRoute.LOCAL, frozenset()
