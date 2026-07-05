"""Human-facing summaries for HITL approval proposals.

Single source of truth for the owner-facing card text shown in the Safent Cowork
approval panel.  Every proposal — regardless of origin (security_hook native danger,
broker HITL, Nous engine write-pending, MCP/Composio read) — passes through
`human_summary` before being surfaced in the frontend.

Design:
  * `human_summary(tool_name, args)` → title sentence (≤120 chars, plain language).
  * `human_body(tool_name, args)`    → optional explanatory sentence (may be empty).
  * The raw `justification` / `parameters_redacted` fields are NOT replaced — they
    remain in the row for the "Ver detalles técnicos" panel in the frontend.

Layer: capabilities (application-adjacent, no I/O, no framework deps).
Imported by: security_hook (runtime) and approvals_api (shell_server/cowork).
"""

from __future__ import annotations

from typing import Any


def human_summary(tool_name: str, args: dict[str, Any] | None = None) -> str:
    """Return a plain-language title sentence for a HITL approval card.

    Covers all known tool categories.  Falls back to a safe generic phrase
    for unknown tools so the card is never empty or technical.
    """
    safe_args = args if isinstance(args, dict) else {}
    t = (tool_name or "").lower()
    name = str(safe_args.get("name") or safe_args.get("identifier") or "").strip()

    if _is_skill_tool(t):
        return _skill_summary(name)
    if _is_install_mcp(t):
        return _install_mcp_summary(name)
    if _is_install_app(t):
        return _install_app_summary(name)
    if _is_write_file(t):
        return _write_file_summary(safe_args)
    if _is_execute_code(t):
        return "El agente quiere ejecutar un comando."
    if _is_send_message(t):
        return _send_message_summary(safe_args)
    if _is_browser_navigate(t):
        return _browser_navigate_summary(safe_args)
    if _is_delegate_to_colleague(t):
        return _delegate_to_colleague_summary(safe_args)
    if _is_delegate(t):
        return "El agente quiere pedir ayuda a otro agente."
    if _is_cronjob(t):
        return "El agente quiere programar una tarea automática."
    if _is_policy_or_mfa(t):
        return "El agente quiere cambiar sus propios permisos de seguridad."
    return f"El agente quiere ejecutar «{tool_name}»."


def human_body(tool_name: str, args: dict[str, Any] | None = None) -> str:
    """Return an optional explanatory sentence (may be empty string).

    Shown below the title in the card when there is useful context beyond
    the action type.
    """
    safe_args = args if isinstance(args, dict) else {}
    t = (tool_name or "").lower()

    if _is_skill_tool(t):
        return ("Añadir una habilidad amplía lo que el agente puede hacer "
                "de forma permanente — revisa que confías en su origen.")
    if _is_install_mcp(t):
        return ("Un servidor MCP extiende al agente con herramientas externas. "
                "Asegúrate de que el origen es de confianza.")
    if _is_install_app(t):
        return "Instalar una aplicación amplía las capacidades del agente."
    if _is_policy_or_mfa(t):
        return "Cambiar permisos de seguridad afecta a todas las acciones futuras."
    if _is_write_file(t):
        path = str(safe_args.get("path") or safe_args.get("filename") or "").strip()
        if path:
            return f"Ruta: {path}"
    if _is_delegate_to_colleague(t):
        return (
            "Esto sale de la organización del agente — el asistente de tu "
            "colega recibirá la petición y SU dueño deberá aprobarla."
        )
    return ""


# ---------------------------------------------------------------------------
# Category predicates — one per action family, trivially testable
# ---------------------------------------------------------------------------

def _is_skill_tool(t: str) -> bool:
    return "skill" in t


def _is_install_mcp(t: str) -> bool:
    return "install_mcp" in t or t == "mcp_install"


def _is_install_app(t: str) -> bool:
    return "install" in t and "mcp" not in t and "skill" not in t


def _is_write_file(t: str) -> bool:
    return t in {"write_file", "patch"}


def _is_execute_code(t: str) -> bool:
    return t in {"execute_code", "run_code", "terminal", "run_command", "run_terminal", "process"}


def _is_send_message(t: str) -> bool:
    return (
        t in {"send_message", "discord", "discord_admin"}
        or t.startswith("ha_")
        or ("send" in t and "file" not in t)
    )


def _is_browser_navigate(t: str) -> bool:
    return t in {"browser_navigate", "browser_open", "open_url"}


def _is_delegate(t: str) -> bool:
    return "delegate" in t or "spawn" in t or t == "mixture_of_agents"


def _is_delegate_to_colleague(t: str) -> bool:
    """FASE 3 (A2A cross-human) — distinct from `_is_delegate` (in-process
    sub-agent): this one leaves the organization to ask ANOTHER human's
    assistant. Checked BEFORE `_is_delegate` in human_summary's if-chain
    since "delegate" is a substring of this tool's name too."""
    return t == "delegate_to_colleague"


def _is_cronjob(t: str) -> bool:
    return t == "cronjob"


def _is_policy_or_mfa(t: str) -> bool:
    return "policy" in t or "mfa" in t or "permission" in t or "security" in t


# ---------------------------------------------------------------------------
# Per-category formatters
# ---------------------------------------------------------------------------

def _skill_summary(name: str) -> str:
    extra = f" «{name}»" if name and name != "web" else ""
    return f"El agente quiere añadir una nueva habilidad{extra}."


def _install_mcp_summary(name: str) -> str:
    extra = f" «{name}»" if name else ""
    return f"El agente quiere conectar una herramienta externa{extra}."


def _install_app_summary(name: str) -> str:
    extra = f" «{name}»" if name else ""
    return f"El agente quiere instalar una aplicación{extra}."


def _write_file_summary(args: dict[str, Any]) -> str:
    path = str(args.get("path") or args.get("filename") or "").strip()
    if path:
        return f"El agente quiere guardar un archivo en «{path}»."
    return "El agente quiere guardar un archivo."


def _send_message_summary(args: dict[str, Any]) -> str:
    dest = str(
        args.get("channel") or args.get("to") or args.get("recipient") or ""
    ).strip()
    if dest:
        return f"El agente quiere enviar un mensaje a «{dest}»."
    return "El agente quiere enviar un mensaje."


def _delegate_to_colleague_summary(args: dict[str, Any]) -> str:
    employee_id = str(args.get("employee_id") or "").strip()
    if employee_id:
        return f"El agente quiere pedir ayuda al asistente de «{employee_id}»."
    return "El agente quiere pedir ayuda al asistente de un compañero."


def _browser_navigate_summary(args: dict[str, Any]) -> str:
    url = str(args.get("url") or args.get("href") or "").strip()
    if url:
        # Truncate long URLs for readability in the card title
        display = url if len(url) <= 60 else url[:57] + "…"
        return f"El agente quiere abrir «{display}»."
    return "El agente quiere abrir una página web."
