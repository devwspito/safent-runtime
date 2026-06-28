"""LLM synthesis of a taught demonstration into a real, reusable SKILL.md.

The desktop teaching pipeline captured low-level input *steps* and replayed them
deterministically. In the web Lumen there is no low-level capture, and a brittle
coordinate-replay is the wrong abstraction anyway. Instead we let the model do
what a human teacher would: turn the demonstration (name + the operator's written
description / narration of the steps) into a generalizable SKILL.md the agent can
later execute by *reasoning*, not by replaying clicks.

Persistence is delegated to the daemon via a D-Bus verb (create_skill_from_text),
which uses SkillStoreAdapter — the SINGLE authorized writer of signed SKILL.md
files. This guarantees that governance frontmatter and the v2 HMAC signature
are identical to those produced by skill_manage (HITL path), making web-minted
skills promotable and verifiable at execution time.
"""

from __future__ import annotations

import logging
import re
from uuid import uuid4

import httpx

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "Eres el cerebro de un sistema operativo agéntico. Conviertes la demostración "
    "de un usuario en una SKILL reutilizable que TÚ podrás ejecutar después razonando "
    "(no repitiendo clicks).\n\n"
    "Puedes razonar primero si lo necesitas, pero el documento SKILL.md FINAL va "
    "SIEMPRE entre estas dos líneas exactas (cada una en su propia línea):\n"
    "===SKILL_START===\n"
    "===SKILL_END===\n"
    "No uses ``` dentro. Dentro del bloque, formato exacto:\n"
    "---\n"
    "name: <slug-en-minúsculas-con-guiones>\n"
    "description: <una línea, qué hace y cuándo usarla>\n"
    "---\n"
    "# <Nombre de la skill>\n\n"
    "## Objetivo\n<qué consigue, en una o dos frases>\n\n"
    "## Cuándo usarla\n<situaciones/triggers>\n\n"
    "## Pasos\n1. <paso en lenguaje natural, generalizable — nada de coordenadas>\n2. ...\n\n"
    "## Herramientas\n<navegador / terminal / apps / web_search / MCP que conviene usar>\n\n"
    "## Límites y seguridad\n<qué NO hacer, cuándo pedir confirmación>\n\n"
    "Reglas: español; pasos generalizables (parametriza lo variable con {placeholders}); "
    "nada de datos sensibles inventados; conciso y accionable."
)


class NoActiveProvider(RuntimeError):
    """Raised when there is no active LLM provider to synthesize with."""


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s or f"skill-{uuid4().hex[:8]}"


def _ensure_frontmatter_fields(content: str, name: str, description: str) -> str:
    """Garantiza name/description/version en el frontmatter — los exige el
    SkillMdDocument nativo (parse_skill_md). El LLM suele emitir solo
    `description`; sin `name`/`version` el SkillStoreAdapter rechaza el documento.
    Inyecta los que falten (slug, descripción, version=1) sin fiarnos del modelo.
    """
    slug = slugify(name)
    desc_default = (description or name).splitlines()[0][:200] if (description or name) else slug
    if not content.lstrip().startswith("---"):
        return f"---\nname: {slug}\ndescription: {desc_default}\nversion: 1\n---\n\n{content}"
    parts = content.split("---", 2)
    if len(parts) < 3:
        return content  # frontmatter malformado — no lo tocamos
    fm = parts[1]
    inject = ""
    if not re.search(r"(?m)^\s*name\s*:", fm):
        inject += f"name: {slug}\n"
    if not re.search(r"(?m)^\s*description\s*:", fm):
        inject += f"description: {desc_default}\n"
    if not re.search(r"(?m)^\s*version\s*:", fm):
        inject += "version: 1\n"
    if not inject:
        return content
    return f"---\n{inject}{fm.lstrip(chr(10))}---{parts[2]}"


async def synthesize_skill_md(
    *,
    name: str,
    description: str,
    repo,        # SQLiteProviderRepository (app.state.repo)
    timeout: float = 90.0,
) -> str:
    """Call the active provider's LLM to produce a SKILL.md document.

    Raises NoActiveProvider if no provider is active, or httpx/ValueError on
    transport/response errors (caller maps these to a friendly message).
    """
    provider = repo.get_active()
    if provider is None:
        raise NoActiveProvider("no hay un proveedor de modelo activo")

    api_key = None
    try:
        api_key = repo.reveal_api_key(provider_id=provider.provider_id)
    except Exception:  # noqa: BLE001 — key optional for keyless local endpoints
        api_key = None

    base_url = (provider.base_url or "").rstrip("/")
    if not base_url:
        # Cloud kinds (openai/anthropic) without a base_url aren't reachable from
        # the shell-server directly; require an OpenAI-compatible base_url.
        raise NoActiveProvider("el proveedor activo no expone un base_url compatible")

    user_msg = (
        f"Nombre de la skill: {name}\n\n"
        f"Descripción y pasos que demostró/escribió el usuario:\n{description}\n\n"
        "Genera el SKILL.md."
    )
    payload = {
        "model": provider.default_model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.3,
        "max_tokens": 2000,
        # Qwen3 reasons inline (no reasoning parser here) and pollutes the output.
        # Ask vLLM to disable thinking so we get the document directly. Harmless on
        # providers that ignore the field.
        "chat_template_kwargs": {"enable_thinking": False},
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    async with httpx.AsyncClient(timeout=timeout) as http:
        resp = await http.post(f"{base_url}/chat/completions", json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    content = content.strip()
    # The model may reason inline (Qwen has no reasoning parser). The real document
    # lives between the sentinels; pick the LARGEST block (the doc, not a prose
    # mention of the sentinel). Fall back to a frontmatter block if absent.
    # The real document is the LAST sentinel block (earlier mentions appear inside
    # any leftover reasoning). rfind targets the final pair reliably.
    s_tag, e_tag = "===SKILL_START===", "===SKILL_END==="
    si, ei = content.rfind(s_tag), content.rfind(e_tag)
    if si != -1 and ei != -1 and ei > si:
        content = content[si + len(s_tag):ei].strip()
    else:
        fm = re.search(r"(---\s*\ndescription:.*)", content, re.DOTALL)
        if fm:
            content = fm.group(1).strip()
    # Strip any leftover sentinels / code fences.
    content = content.replace("===SKILL_START===", "").replace("===SKILL_END===", "").strip()
    if content.startswith("```"):
        content = re.sub(r"^```[a-zA-Z]*\n?", "", content)
        content = re.sub(r"\n?```$", "", content).strip()
    if not content.startswith("---"):
        # Ensure a minimal frontmatter so discovery picks up a description.
        first_line = (description or name).splitlines()[0][:120] if (description or name) else name
        content = f"---\ndescription: {first_line}\n---\n{content}"
    # SkillMdDocument nativo exige name/description/version — garantízalos.
    content = _ensure_frontmatter_fields(content, name, description)
    return content


async def synthesize_and_persist(
    *,
    repo,
    name: str,
    description: str,
    dbus_proxy,
) -> dict:
    """Full path: LLM → SKILL.md → daemon (SkillStoreAdapter) → signed artefact.

    Delegates persistence to the daemon via D-Bus verb create_skill_from_text,
    which uses SkillStoreAdapter as the single authorized writer. Returns a dict
    with {package_id, skill_id, skill_name, version, path, state, signing_method}.

    Raises:
        NoActiveProvider: no active LLM provider.
        fastapi.HTTPException(503): daemon unavailable (dbus_proxy is None).
        hermes.tasks.control_plane.domain.ports.AgentUnavailable: D-Bus call failed.
    """
    from fastapi import HTTPException  # noqa: PLC0415

    if dbus_proxy is None:
        raise HTTPException(
            status_code=503,
            detail="skill synthesis requires the daemon (hermes-runtime is not running)",
        )

    skill_md = await synthesize_skill_md(name=name, description=description, repo=repo)
    meta = await dbus_proxy.call_dict("create_skill_from_text", name, skill_md)

    logger.info(
        "skill_synthesis: created skill=%s package=%s",
        meta.get("skill_id") or meta.get("skill_name"),
        meta.get("package_id"),
    )
    return meta
