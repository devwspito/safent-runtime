"""Agent roster endpoint — GET /api/v1/agents/roster.

Returns the unified agent team grouped into departments:

  • "cerebro"      — the default (is_default=True) custom agent.
  • Factory depts  — Ruflo MCP tools grouped by category (kind="factory").
  • "mis-agentes"  — non-default custom agents without an explicit department.
  • Custom depts   — non-default custom agents with an explicit department field.

Ruflo is the "Powered by Ruflo" backend; it is never mentioned by name to the
user. The tool catalog is sourced in this priority order:
  1. Live MCP list via D-Bus (list_mcp_servers → list_tools for ruflo). ← primary
  2. Catalog JSON fallback at:
     /var/lib/hermes/npm-cache/_npx/*/node_modules/ruflo/src/ruvocal/static/
       huggingchat/routes.chat.json  (globbed at request time).
  3. Baked-in static catalog (subset of known tools) when neither is available.

Fail-soft: ruflo unavailability never 500s — only the custom departments appear.

Security: read-only, no auth required (same posture as GET /api/v1/agents).
"""

from __future__ import annotations

import glob
import json
import logging
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request

from hermes.tasks.control_plane.domain.ports import AgentUnavailable

logger = logging.getLogger("hermes.shell_server.cowork.roster_api")

# ---------------------------------------------------------------------------
# Ruflo department map: tool_name → department_id.
# Any name not in this map falls into "otros".
# ---------------------------------------------------------------------------

_RUFLO_DEPT_MAP: dict[str, str] = {
    # writing
    "email_writing": "writing",
    "essay_writing": "writing",
    "editing_rewrite": "writing",
    "summarization": "writing",
    "translation": "writing",
    "social_media_copy": "writing",
    "spell_checker": "writing",
    "job_app_docs": "writing",
    # coding
    "code_generation": "coding",
    "code_review_docs": "coding",
    "code_maintenance": "coding",
    "software_architecture_design": "coding",
    "frontend_ui": "coding",
    "terminal_cli": "coding",
    "agentic_orchestration": "coding",
    "formal_proof": "coding",
    # support
    "emotional_support": "support",
    "health_wellness_info": "support",
    "career_coaching": "support",
    "language_tutoring": "support",
    "learning_tutor": "support",
    # personal
    "meal_planning": "personal",
    "travel_planning": "personal",
    "personal_finance": "personal",
    "shopping_recommendations": "personal",
    "decision_support": "personal",
    # data
    "structured_data": "data",
    "qa_explanations": "data",
    "technical_explanation": "data",
    # creative
    "creative_writing": "creative",
    "interactive_roleplay": "creative",
    "character_impersonation": "creative",
    "casual_conversation": "creative",
    "brainstorming_ideas": "creative",
}

_DEPT_META: dict[str, dict[str, str]] = {
    "writing":  {"name": "Escritura",    "kind": "factory"},
    "coding":   {"name": "Código",       "kind": "factory"},
    "support":  {"name": "Apoyo",        "kind": "factory"},
    "personal": {"name": "Personal",     "kind": "factory"},
    "data":     {"name": "Datos",        "kind": "factory"},
    "creative": {"name": "Creatividad",  "kind": "factory"},
    "otros":    {"name": "Otros",        "kind": "factory"},
}

# Static baked-in subset (last-resort fallback when ruflo can't be reached and
# the npm cache glob also fails — avoids a completely empty factory section).
_STATIC_RUFLO_CATALOG: list[dict[str, str]] = [
    {"name": "email_writing",           "description": "Draft or revise emails with clear tone and a specific CTA."},
    {"name": "code_generation",         "description": "Generate new code, tests, and scaffolds from specs."},
    {"name": "summarization",           "description": "Condense documents into an abstract, key points, and action items."},
    {"name": "translation",             "description": "Translate between languages with register and terminology control."},
    {"name": "creative_writing",        "description": "Write fiction, poems, jokes, or scripts with style control."},
    {"name": "career_coaching",         "description": "Guide job search, skill gaps, interviews, and negotiation."},
    {"name": "technical_explanation",   "description": "Explain complex technical topics step-by-step with worked examples."},
    {"name": "structured_data",         "description": "Extract structured JSON from text."},
    {"name": "travel_planning",         "description": "Research trips and craft day-by-day itineraries with logistics."},
    {"name": "emotional_support",       "description": "Provide compassionate listening and gentle guidance for emotional well-being."},
]


# ---------------------------------------------------------------------------
# Ruflo catalog loading (fallback chain)
# ---------------------------------------------------------------------------

def _humanize(name: str) -> str:
    """Convert snake_case tool name to Title Case display name."""
    return name.replace("_", " ").title()


def _load_ruflo_catalog_from_json() -> list[dict[str, str]] | None:
    """Glob for the ruflo npm-cache catalog JSON. Returns None on any error."""
    patterns = [
        "/var/lib/hermes/npm-cache/_npx/*/node_modules/ruflo/src/ruvocal/static/huggingchat/routes.chat.json",
        str(Path.home() / ".npm/_npx/*/node_modules/ruflo/src/ruvocal/static/huggingchat/routes.chat.json"),
    ]
    for pattern in patterns:
        matches = glob.glob(pattern)
        if not matches:
            continue
        path = matches[0]
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
            if isinstance(raw, list):
                catalog = [
                    {"name": str(e["name"]), "description": str(e.get("description", ""))}
                    for e in raw
                    if isinstance(e, dict) and e.get("name")
                ]
                logger.info(
                    "hermes.roster.ruflo_catalog_from_json path=%s tools=%d",
                    path,
                    len(catalog),
                )
                return catalog
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.roster.ruflo_catalog_json_read_failed path=%s error=%s", path, exc)
    return None


def _ruflo_catalog_from_mcp_tools(tools: list[dict]) -> list[dict[str, str]]:
    """Convert live MCP list_tools output to the catalog format."""
    result = []
    for t in tools:
        name = str(t.get("name") or t.get("qualified_name") or "")
        # Strip the mcp__ruflo__ prefix when present.
        bare = re.sub(r"^mcp__ruflo__", "", name)
        if not bare:
            continue
        result.append({"name": bare, "description": str(t.get("description") or "")})
    return result


async def _load_ruflo_catalog_live(proxy: Any) -> list[dict[str, str]] | None:
    """Try to get live ruflo tools from the daemon's MCP manager via D-Bus."""
    try:
        servers: list[dict] = await proxy.call_list("list_mcp_servers")
        ruflo_entry = next((s for s in servers if s.get("server_id") == "ruflo"), None)
        if ruflo_entry and ruflo_entry.get("health") == "healthy":
            tools: list[dict] = await proxy.call_list("list_mcp_tools", "ruflo")
            if tools:
                catalog = _ruflo_catalog_from_mcp_tools(tools)
                logger.info("hermes.roster.ruflo_catalog_live tools=%d", len(catalog))
                return catalog
    except Exception as exc:  # noqa: BLE001
        logger.warning("hermes.roster.ruflo_catalog_live_failed error=%s", exc)
    return None


async def _get_ruflo_catalog(proxy: Any) -> tuple[list[dict[str, str]], str]:
    """Return (catalog, source) with fail-soft fallback chain.

    source values: "live" | "json" | "static"
    """
    live = await _load_ruflo_catalog_live(proxy)
    if live:
        return live, "live"

    from_json = _load_ruflo_catalog_from_json()
    if from_json:
        return from_json, "json"

    logger.warning("hermes.roster.ruflo_catalog_fallback_to_static")
    return _STATIC_RUFLO_CATALOG, "static"


# ---------------------------------------------------------------------------
# Roster assembly
# ---------------------------------------------------------------------------

def _catalog_to_factory_departments(
    catalog: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """Group ruflo catalog entries into departments keyed by _RUFLO_DEPT_MAP."""
    # dept_id → list of agent-shaped dicts
    buckets: dict[str, list[dict[str, Any]]] = {}

    for entry in catalog:
        name = entry["name"]
        dept_id = _RUFLO_DEPT_MAP.get(name, "otros")
        if dept_id not in buckets:
            buckets[dept_id] = []
        buckets[dept_id].append({
            "id": f"ruflo:{name}",
            "name": _humanize(name),
            "description": entry["description"],
            "source": "ruflo",
            "department": dept_id,
            "is_default": False,
            "color": None,
        })

    # Stable ordering: follow _DEPT_META insertion order, then "otros" last.
    ordered_ids = [d for d in _DEPT_META if d in buckets]
    departments = []
    for dept_id in ordered_ids:
        meta = _DEPT_META[dept_id]
        departments.append({
            "id": dept_id,
            "name": meta["name"],
            "kind": meta["kind"],
            "agents": buckets[dept_id],
        })
    return departments


def _build_custom_departments(agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Build cerebro + custom departments from the Hermes agent registry list."""
    cerebro_agents: list[dict[str, Any]] = []
    dept_buckets: dict[str, list[dict[str, Any]]] = {}
    misc_agents: list[dict[str, Any]] = []

    for a in agents:
        agent_shape = {
            "id": a.get("agent_id", ""),
            "name": a.get("name", ""),
            "description": a.get("primary_mission", ""),
            "source": "custom",
            "department": a.get("department"),
            "is_default": bool(a.get("is_default", False)),
            "color": a.get("color") or None,
        }
        if a.get("is_default"):
            agent_shape["department"] = "cerebro"
            cerebro_agents.append(agent_shape)
        else:
            dept = (a.get("department") or "").strip()
            if dept:
                if dept not in dept_buckets:
                    dept_buckets[dept] = []
                dept_buckets[dept].append(agent_shape)
            else:
                misc_agents.append(agent_shape)

    departments: list[dict[str, Any]] = []

    # Cerebro always first.
    if cerebro_agents:
        departments.append({
            "id": "cerebro",
            "name": "Cerebro",
            "kind": "cerebro",
            "agents": cerebro_agents,
        })

    # Named custom departments (alpha by slug; namespaced id prevents collision
    # with factory dept ids such as "writing", "coding", "support", etc.).
    for slug in sorted(dept_buckets):
        departments.append({
            "id": f"custom:{slug}",
            "name": slug.replace("-", " ").title(),
            "kind": "custom",
            "agents": dept_buckets[slug],
        })

    # Mis agentes bucket (custom agents with no department).
    if misc_agents:
        departments.append({
            "id": "mis-agentes",
            "name": "Mis agentes",
            "kind": "custom",
            "agents": misc_agents,
        })

    return departments


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def create_roster_router() -> APIRouter:
    """Return the APIRouter for GET /api/v1/agents/roster."""
    router = APIRouter(prefix="/api/v1/agents", tags=["agents"])

    @router.get("/roster")
    async def get_agent_roster(request: Request) -> dict[str, Any]:
        """Unified agent team grouped into departments.

        Fail-soft: ruflo unavailability only omits the factory departments.
        Custom agents are always returned even when the daemon is unavailable.
        Never 500s.
        """
        proxy = request.app.state.dbus_proxy

        # Load custom agents (fail-soft: empty list).
        try:
            raw_agents: list[dict] = await proxy.call_list("list_agents")
        except Exception:  # noqa: BLE001
            raw_agents = []

        custom_departments = _build_custom_departments(raw_agents)

        # Load ruflo catalog (fail-soft: static fallback).
        try:
            catalog, source = await _get_ruflo_catalog(proxy)
        except Exception:  # noqa: BLE001
            catalog, source = _STATIC_RUFLO_CATALOG, "static"

        factory_departments = _catalog_to_factory_departments(catalog)

        # Final order: cerebro → factory depts → custom named depts → mis-agentes.
        cerebro_depts = [d for d in custom_departments if d["id"] == "cerebro"]
        named_custom = [d for d in custom_departments if d["id"] not in ("cerebro", "mis-agentes")]
        misc_dept = [d for d in custom_departments if d["id"] == "mis-agentes"]

        departments = cerebro_depts + factory_departments + named_custom + misc_dept

        logger.debug(
            "hermes.roster.built departments=%d ruflo_source=%s",
            len(departments),
            source,
        )
        return {"departments": departments}

    return router
