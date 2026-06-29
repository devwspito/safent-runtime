"""GET /api/v1/runtime/agent-stats — live agent state + today's usage.

Merges three read-only sources:
  1. D-Bus get_runtime_status  → which agents are currently working (activity[]).
  2. SqliteAgentRegistry       → name / department / color for every known agent.
  3. SQLiteUsageRepository     → tokens / cost_usd / tasks for today (UTC).

Contract (fail-soft, arrays always present — never 500):
  {
    "available": true,
    "agents": [
      {
        "agent_id": "...",
        "name":     "...",
        "department": "...",
        "color":    "...",
        "state":    "idle" | "working",
        "active_task_count": 0,
        "today": {"tokens": 0, "cost_usd": 0.0, "tasks": 0},
        "health": "ok" | "degraded" | "unknown"
      }
    ]
  }

On any error: {"available": false, "agents": []}.

Design notes:
- `state` = "working" when the agent appears in activity[] or equals active_agent_id.
- `health` = "degraded" when today's tasks > 0 but cycles include failures (not
  exposed here — kept as "ok" / "unknown" for simplicity; "degraded" is reserved
  for future signal).
- The registry list_agents() respects the roster-on/off toggle (same as the roster
  endpoint) so hidden specialists do not clutter the stats view.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from hermes.shell_server.metering.usage_repo import SQLiteUsageRepository

logger = logging.getLogger("hermes.shell_server.metering.agent_stats")

_DB_PATH = Path(
    os.environ.get("HERMES_SHELL_DB", "/var/lib/hermes/shell-state.db")
)

_EMPTY_TODAY: dict[str, Any] = {"tokens": 0, "cost_usd": 0.0, "tasks": 0}

# Office floor live stream (SSE). One connection per client; the server pushes a
# new snapshot only when it changed, with a periodic keepalive so proxies don't
# drop the idle connection. Replaces the old 4 s client poll.
_STREAM_TICK_S = 2.0
_STREAM_KEEPALIVE_S = 20.0
_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",  # disable proxy buffering so frames flush immediately
}


def _active_agent_ids(runtime_status: dict[str, Any]) -> frozenset[str]:
    """Extract the set of agent_ids currently active from a runtime status dict."""
    active: set[str] = set()

    active_agent_id = runtime_status.get("active_agent_id")
    if active_agent_id:
        active.add(str(active_agent_id))

    for entry in runtime_status.get("activity", []) or []:
        aid = (entry or {}).get("agent_id")
        if aid:
            active.add(str(aid))

    return frozenset(active)


def _agent_stat(
    agent: dict[str, Any],
    *,
    active_ids: frozenset[str],
    today_map: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    agent_id = agent.get("agent_id", "")
    is_working = agent_id in active_ids
    today = dict(today_map.get(agent_id, _EMPTY_TODAY))

    return {
        "agent_id": agent_id,
        "name": agent.get("name", ""),
        "department": agent.get("department") or "",
        "color": agent.get("color") or "",
        "state": "working" if is_working else "idle",
        "active_task_count": 1 if is_working else 0,
        "today": today,
        "health": "ok" if today["tasks"] > 0 else "unknown",
    }


def create_agent_stats_router() -> APIRouter:
    """Return the APIRouter for GET /api/v1/runtime/agent-stats."""
    router = APIRouter(tags=["runtime"])

    @router.get("/api/v1/runtime/agent-stats")
    async def agent_stats(request: Request) -> dict[str, Any]:
        """Live agent floor status + today's token/cost usage per agent.

        Fail-soft: any error returns {available: false, agents: []} so the
        frontend never receives undefined where it expects an array.
        """
        try:
            return await _build_agent_stats(request)
        except Exception:  # noqa: BLE001
            logger.exception("hermes.agent_stats.build_failed")
            return {"available": False, "agents": []}

    @router.get("/api/v1/runtime/agent-stream")
    async def runtime_stream(request: Request) -> StreamingResponse:
        """SSE push of the live Office floor: runtime status + per-agent stats.

        Replaces the 4 s client poll with a single server-pushed stream. Emits a
        frame only when the snapshot changes (one D-Bus round-trip per tick), plus
        a periodic keepalive so idle connections survive. Fail-soft per tick.
        """

        async def _gen() -> Any:
            last_key: str | None = None
            idle_s = 0.0
            while True:
                if await request.is_disconnected():
                    break
                snapshot = await _build_runtime_snapshot(request)
                # Dedup on the SEMANTIC snapshot: runtime.captured_at is a fresh
                # per-call timestamp with no frontend consumer, so it must NOT count
                # as a change — otherwise every tick reads as "changed", a full frame
                # is re-pushed every _STREAM_TICK_S and the keepalive branch never
                # fires (idle proxies would then drop the connection).
                dedup_key = _dedup_key(snapshot)
                if dedup_key != last_key:
                    last_key = dedup_key
                    idle_s = 0.0
                    yield f"data: {json.dumps(snapshot, default=str)}\n\n"
                else:
                    idle_s += _STREAM_TICK_S
                    if idle_s >= _STREAM_KEEPALIVE_S:
                        idle_s = 0.0
                        yield ": keepalive\n\n"
                await asyncio.sleep(_STREAM_TICK_S)

        return StreamingResponse(
            _gen(), media_type="text/event-stream", headers=_SSE_HEADERS
        )

    return router


def _dedup_key(snapshot: dict[str, Any]) -> str:
    """Stable change-detection key for the Office snapshot.

    Excludes runtime.captured_at (a fresh per-call timestamp with no frontend
    consumer) so an unchanged floor does not read as a change every tick — which
    would re-push a full frame every tick and starve the keepalive branch.
    """
    runtime = {
        k: v for k, v in snapshot.get("runtime", {}).items() if k != "captured_at"
    }
    return json.dumps(
        {"runtime": runtime, "stats": snapshot.get("stats")}, default=str, sort_keys=True
    )


async def _build_runtime_snapshot(request: Request) -> dict[str, Any]:
    """Combined Office-floor snapshot: one get_runtime_status feeds both views."""
    proxy = request.app.state.dbus_proxy
    try:
        runtime_status = await proxy.call_dict("get_runtime_status")
        runtime_status.setdefault("available", True)
    except Exception:  # noqa: BLE001 — daemon transient/unavailable
        runtime_status = {
            "state": "idle",
            "active_task_count": 0,
            "available": False,
            "captured_at": datetime.now(tz=UTC).isoformat(),
        }
    stats = await _build_agent_stats(request, runtime_status=runtime_status)
    return {"runtime": runtime_status, "stats": stats}


async def _build_agent_stats(
    request: Request, *, runtime_status: dict[str, Any] | None = None
) -> dict[str, Any]:
    proxy = request.app.state.dbus_proxy

    # The stream caller fetches runtime status once and passes it in to avoid a
    # second D-Bus round-trip per tick; the plain endpoint fetches it here.
    if runtime_status is None:
        runtime_status = {}
        try:
            runtime_status = await proxy.call_dict("get_runtime_status")
        except Exception:  # noqa: BLE001
            logger.warning("hermes.agent_stats.dbus_unavailable — proceeding without live state")

    active_ids = _active_agent_ids(runtime_status)

    raw_agents = await _list_agents_safe(proxy)

    try:
        today_map = _fetch_today_map()
    except Exception:  # noqa: BLE001
        logger.warning("hermes.agent_stats.fetch_today_failed — today usage unavailable")
        today_map = {}

    agents = [
        _agent_stat(a, active_ids=active_ids, today_map=today_map)
        for a in raw_agents
    ]

    available = runtime_status.get("available", bool(runtime_status))
    return {"available": available, "agents": agents}


async def _list_agents_safe(proxy: Any) -> list[dict[str, Any]]:
    try:
        return await proxy.call_list("list_agents")
    except Exception:  # noqa: BLE001
        logger.warning("hermes.agent_stats.list_agents_failed — returning empty roster")
        return []


def _fetch_today_map() -> dict[str, dict[str, Any]]:
    try:
        repo = SQLiteUsageRepository(db_path=_DB_PATH)
        return repo.today_by_agent()
    except Exception:  # noqa: BLE001
        logger.warning("hermes.agent_stats.usage_repo_failed — today usage unavailable")
        return {}
