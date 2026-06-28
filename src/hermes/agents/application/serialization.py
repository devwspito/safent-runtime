"""Serialización Agent ↔ dict (JSON sobre D-Bus).

El control-plane D-Bus transporta agentes como JSON (firma "s"), igual que
ListConfiguredTasks/ListRecentTasks. Daemon y shell comparten estos mappers para
no divergir.
"""

from __future__ import annotations

from typing import Any

from hermes.agents.domain.agent import Agent, AgentDraft, AutonomyLevel, autonomy_level_from_str


def agent_to_dict(agent: Agent) -> dict[str, Any]:
    d: dict[str, Any] = {
        "agent_id": agent.agent_id,
        "name": agent.name,
        "color": agent.color,
        "role": agent.role,
        "register": agent.register,
        "primary_mission": agent.primary_mission,
        "instructions": agent.instructions,
        "language": agent.language,
        "golden_rules": list(agent.golden_rules),
        "forbidden_phrases": list(agent.forbidden_phrases),
        "autonomy_level": agent.autonomy_level.value,
        "is_default": agent.is_default,
        "department": agent.department,
        "provider_alias": agent.provider_alias,
        "managed_by": agent.managed_by,
        "created_at": agent.created_at.isoformat(),
        "updated_at": agent.updated_at.isoformat(),
    }
    # Expose "id" as an alias for "agent_id" so all consumers (REST
    # routers, roster builder, React frontend) can rely on a single key.
    d["id"] = d["agent_id"]
    return d


def draft_from_dict(data: dict[str, Any]) -> AgentDraft:
    """Construye un AgentDraft desde el dict del cliente. Tolera campos ausentes;
    solo `name` es obligatorio (lo valida el dominio Agent al persistir).

    Validación de autonomy_level en la frontera: valor desconocido → ValueError.
    """
    raw_autonomy = str(data.get("autonomy_level", AutonomyLevel.BALANCED.value))
    try:
        autonomy = autonomy_level_from_str(raw_autonomy)
    except ValueError:
        # Fail-closed: valor desconocido → default conservador
        autonomy = AutonomyLevel.BALANCED

    raw_dept = data.get("department")
    department: str | None = str(raw_dept).strip() or None if raw_dept is not None else None

    raw_alias = data.get("provider_alias")
    provider_alias: str | None = str(raw_alias).strip() or None if raw_alias is not None else None

    raw_managed_by = data.get("managed_by")
    managed_by: str | None = str(raw_managed_by).strip() or None if raw_managed_by is not None else None

    raw_agent_id = data.get("agent_id")
    agent_id: str | None = str(raw_agent_id).strip() or None if raw_agent_id is not None else None

    return AgentDraft(
        agent_id=agent_id,
        name=str(data.get("name", "")).strip(),
        role=str(data.get("role", "")),
        register=str(data.get("register", "")),
        primary_mission=str(data.get("primary_mission", "")),
        instructions=str(data.get("instructions", "")),
        color=str(data.get("color", "")) or "#6366f1",
        language=str(data.get("language", "")) or "es-ES",
        golden_rules=tuple(str(r) for r in data.get("golden_rules", []) if str(r).strip()),
        forbidden_phrases=tuple(
            str(p) for p in data.get("forbidden_phrases", []) if str(p).strip()
        ),
        autonomy_level=autonomy,
        department=department,
        provider_alias=provider_alias,
        managed_by=managed_by,
    )
