"""LLM synthesis of a taught demonstration into a real, reusable SKILL.md.

The desktop teaching pipeline captured low-level input *steps* and replayed them
deterministically. In the web Lumen there is no low-level capture, and a brittle
coordinate-replay is the wrong abstraction anyway. Instead we let the model do
what a human teacher would: turn the demonstration (name + the operator's written
description / narration of the steps) into a generalizable SKILL.md the agent can
later execute by *reasoning*, not by replaying clicks.

Two artifacts are produced so the skill is both usable and visible:
  1. $HERMES_HOME/skills/<slug>/SKILL.md  → the agent auto-discovers + uses it.
  2. a row in skill_packages_view         → the Skills UI lists it (state=validated).

Audio (Whisper) is intentionally out of scope here — synthesis is text-driven.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx

logger = logging.getLogger(__name__)

# Where the agent (daemon) discovers on-disk skills. Kept in sync with the
# daemon's HERMES_HOME (see dbus_runtime_service._list_native_hermes_agent_skills).
_DEFAULT_HERMES_HOME = "/var/lib/hermes/hermes-home"

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


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or _DEFAULT_HERMES_HOME)


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
    return content


def write_skill_file(*, name: str, skill_md: str, hermes_home: Path | None = None) -> Path:
    """Write SKILL.md under $HERMES_HOME/skills/<slug>/ so the agent loads it."""
    root = (hermes_home or _hermes_home()) / "skills" / slugify(name)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "SKILL.md"
    path.write_text(skill_md, encoding="utf-8")
    return path


def register_skill_row(*, db_path: Path, name: str, skill_md: str, signed_at: str) -> dict:
    """Insert a validated row into skill_packages_view so the Skills UI lists it.

    Signs the SKILL.md with the native keystore key (v2) for integrity. Best-effort:
    if signing is unavailable the row is still written without a signature.
    """
    skill_id = slugify(name)
    version = _next_version(db_path, skill_id)
    signature_hex = None
    signing_method = "v2"
    try:
        from hermes.shell_server.training.persist import resolve_signing_key  # noqa: PLC0415

        key, signing_method = resolve_signing_key(db_path)
        canonical = f"{skill_id}\n{version}\n{skill_md}".encode()
        signature_hex = hmac.new(key, canonical, hashlib.sha256).hexdigest()
    except Exception:  # noqa: BLE001 — keystore may be absent in dev
        logger.warning("skill_synthesis: signing unavailable; writing unsigned row")
        signing_method = "v2"

    package_id = str(uuid4())
    with _conn(db_path) as c:
        c.execute(
            """
            INSERT OR REPLACE INTO skill_packages_view (
              package_id, skill_id, skill_name, version, state,
              surface_kinds, signed_at, signature_short, signing_method, signature_hex
            ) VALUES (?, ?, ?, ?, 'validated', 'native', ?, ?, ?, ?)
            """,
            (
                package_id,
                skill_id,
                name,
                version,
                signed_at,
                signature_hex[:12] if signature_hex else None,
                signing_method,
                signature_hex,
            ),
        )
    return {
        "package_id": package_id,
        "skill_id": skill_id,
        "skill_name": name,
        "version": version,
    }


def _next_version(db_path: Path, skill_id: str) -> int:
    with _conn(db_path) as c:
        row = c.execute(
            "SELECT MAX(version) AS v FROM skill_packages_view WHERE skill_id = ?",
            (skill_id,),
        ).fetchone()
    return (row["v"] if row and row["v"] is not None else 0) + 1


def _conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


async def synthesize_and_persist(
    *,
    db_path: Path,
    repo,
    name: str,
    description: str,
) -> dict:
    """Full path: LLM → SKILL.md → write file + register row. Returns metadata."""
    skill_md = await synthesize_skill_md(name=name, description=description, repo=repo)
    path = write_skill_file(name=name, skill_md=skill_md)
    now = datetime.now(tz=UTC).isoformat()
    meta = register_skill_row(db_path=db_path, name=name, skill_md=skill_md, signed_at=now)
    meta["path"] = str(path)
    logger.info("skill_synthesis: created skill=%s package=%s", meta["skill_id"], meta["package_id"])
    return meta
