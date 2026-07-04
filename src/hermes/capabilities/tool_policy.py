"""Per-command tool policy — the backing of the Security/Policies UI (P4.B).

The owner is sovereign: a Policies screen lists EVERY command and lets them toggle it
on/off and pick a preset. Their decision, their responsibility. Our default ("Equilibrado")
balances usability + autonomy without compromising security. A disabled command is denied
at the universal tool gate (the agent's LLM may propose it, but the deterministic gate
refuses) — to use it, the owner enables it in the UI.

The policy file lives owner-only under /var/lib/hermes (agent-inaccessible: the agent runs
in a separate sandbox without this path). Changing the policy is itself a most-delicate
action → MFA + riddle (enforced at the API layer).
"""

from __future__ import annotations

import json
import os
from enum import StrEnum
from pathlib import Path

from hermes.capabilities.tool_delicacy import CAGED_NATIVE_TOOLS, default_enabled_equilibrado
from hermes.runtime.nous_tool_risk_map import NOUS_TOOL_CATALOG

_DEFAULT_PATH = Path(os.environ.get("HERMES_POLICY_DIR", "/var/lib/hermes/policies")) / "tool_policy.json"

# Capability (os_surface) tools the broker can execute — kept in sync with
# capability_tool_specs; listed here so the Policies UI shows them too.
_CAPABILITY_TOOLS: frozenset[str] = frozenset({
    "activate_app", "navigate_app", "search_apps", "install_app",
    "connect_integration", "install_mcp", "search_mcp", "install_skill", "search_skills",
    "get_service_status", "list_services", "start_service", "stop_service", "restart_service",
    "lo_open_document", "lo_save_document", "lo_write_text",
})

# Full catalog the Policies UI lists. CAGED_NATIVE_TOOLS comes from the single source
# (tool_delicacy) — no hand-listed duplicate of the cage set (de-dup audit 2026-06-19).
TOOL_CATALOG: frozenset[str] = NOUS_TOOL_CATALOG | _CAPABILITY_TOOLS | CAGED_NATIVE_TOOLS

# ---------------------------------------------------------------------------
# Category mapping: tool name → human-readable capability group.
#
# Category labels are UI-facing (Spanish, domain language for Lumen).
# Origin axes:
#   - native (NOUS_TOOL_CATALOG): categorised by functional group below.
#   - capability (_CAPABILITY_TOOLS + CAGED_NATIVE_TOOLS): derives from
#     SurfaceKind via _CAPABILITY_CATEGORY_MAP.
#   - mcp:      "Herramientas externas (MCP)"
#   - composio: "Apps conectadas (Composio)"
# ---------------------------------------------------------------------------

_NATIVE_TOOL_CATEGORY: dict[str, str] = {
    # Files and documents
    "read_file": "Ficheros y documentos",
    "search_files": "Ficheros y documentos",
    "write_file": "Ficheros y documentos",
    "patch": "Ficheros y documentos",
    # Terminal and execution
    "execute_code": "Terminal y ejecución",
    "terminal": "Terminal y ejecución",
    "process": "Terminal y ejecución",
    # Web and browser
    "web_search": "Web y navegador",
    "web_extract": "Web y navegador",
    "browser_snapshot": "Web y navegador",
    "browser_back": "Web y navegador",
    "browser_get_images": "Web y navegador",
    "browser_console": "Web y navegador",
    "browser_navigate": "Web y navegador",
    "browser_click": "Web y navegador",
    "browser_type": "Web y navegador",
    "browser_scroll": "Web y navegador",
    "browser_press": "Web y navegador",
    "browser_vision": "Web y navegador",
    "browser_cdp": "Web y navegador",
    "browser_dialog": "Web y navegador",
    # Screen and input control
    "computer_use": "Pantalla y control",
    # Messaging / communication
    "send_message": "Comunicación",
    "discord": "Comunicación",
    "discord_admin": "Comunicación",
    "feishu_doc_read": "Comunicación",
    "feishu_drive_list_comments": "Comunicación",
    "feishu_drive_list_comment_replies": "Comunicación",
    "feishu_drive_add_comment": "Comunicación",
    "feishu_drive_reply_comment": "Comunicación",
    "yb_query_group_info": "Comunicación",
    "yb_query_group_members": "Comunicación",
    "yb_search_sticker": "Comunicación",
    "yb_send_dm": "Comunicación",
    "yb_send_sticker": "Comunicación",
    "x_search": "Comunicación",
    # Home automation (IoT)
    "ha_call_service": "Sistema",
    "ha_get_state": "Sistema",
    "ha_list_entities": "Sistema",
    "ha_list_services": "Sistema",
    # Kanban / task management
    "kanban_list": "Orquestación",
    "kanban_show": "Orquestación",
    "kanban_heartbeat": "Orquestación",
    "kanban_create": "Orquestación",
    "kanban_complete": "Orquestación",
    "kanban_block": "Orquestación",
    "kanban_unblock": "Orquestación",
    "kanban_comment": "Orquestación",
    "kanban_link": "Orquestación",
    # Skills
    "skill_manage": "Programación",
    "skill_view": "Programación",
    "skills_list": "Programación",
    # Memory
    "memory": "Memoria",
    "session_search": "Memoria",
    # Media
    "image_generate": "Medios",
    "video_generate": "Medios",
    "text_to_speech": "Medios",
    "video_analyze": "Medios",
    "vision_analyze": "Medios",
    # Orchestration / delegation
    "delegate_task": "Orquestación",
    "mixture_of_agents": "Orquestación",
    "todo": "Orquestación",
    # Clarify (user dialogue)
    "clarify": "Comunicación",
    # Scheduler
    "cronjob": "Sistema",
}

# Capability tools: derive category from SurfaceKind name pattern or explicit map.
_CAPABILITY_CATEGORY_MAP: dict[str, str] = {
    # Apps / desktop
    "activate_app": "Apps",
    "navigate_app": "Apps",
    "search_apps": "Apps",
    "install_app": "Apps",
    # Integrations / MCP
    "connect_integration": "Apps conectadas (Composio)",
    "install_mcp": "Herramientas externas (MCP)",
    "search_mcp": "Herramientas externas (MCP)",
    # Skills
    "install_skill": "Programación",
    "search_skills": "Programación",
    # System services
    "get_service_status": "Sistema",
    "list_services": "Sistema",
    "start_service": "Sistema",
    "stop_service": "Sistema",
    "restart_service": "Sistema",
    # LibreOffice / documents
    "lo_open_document": "Ficheros y documentos",
    "lo_save_document": "Ficheros y documentos",
    "lo_write_text": "Ficheros y documentos",
}

# Caged native exec/file tools category
_CAGED_CATEGORY_MAP: dict[str, str] = {
    "terminal": "Terminal y ejecución",
    "execute_code": "Terminal y ejecución",
    "process": "Terminal y ejecución",
    "read_file": "Ficheros y documentos",
    "search_files": "Ficheros y documentos",
    "write_file": "Ficheros y documentos",
    "patch": "Ficheros y documentos",
}

# Tools that the LLM does NOT see because they are superseded by Nous-native equivalents.
# Imported lazily to avoid a circular import at module load time (capability_tool_specs
# imports from this module indirectly via tool_delicacy).
def _get_nous_native_duplicates() -> frozenset[str]:
    try:
        from hermes.runtime.capability_tool_specs import _NOUS_NATIVE_DUPLICATES  # noqa: PLC0415
        return _NOUS_NATIVE_DUPLICATES
    except Exception:  # noqa: BLE001
        return frozenset()


def _get_os_native_skill_names() -> frozenset[str]:
    try:
        from hermes.runtime.capability_tool_specs import _OS_NATIVE_SKILL_NAMES  # noqa: PLC0415
        return _OS_NATIVE_SKILL_NAMES
    except Exception:  # noqa: BLE001
        return frozenset()


def _tool_label(name: str) -> str:
    """Humanise a snake_case tool name to Title Case for the UI."""
    return name.replace("_", " ").replace("__", " / ").title()


def _tool_category(name: str, origin: str) -> str:
    """Return the human-readable capability group for a tool."""
    if origin == "mcp":
        return "Herramientas externas (MCP)"
    if origin == "composio":
        return "Apps conectadas (Composio)"
    if name in _NATIVE_TOOL_CATEGORY:
        return _NATIVE_TOOL_CATEGORY[name]
    if name in _CAPABILITY_CATEGORY_MAP:
        return _CAPABILITY_CATEGORY_MAP[name]
    if name in _CAGED_CATEGORY_MAP:
        return _CAGED_CATEGORY_MAP[name]
    return "Sistema"


def _tool_origin(name: str) -> str:
    """Derive the static origin of a tool from the TOOL_CATALOG membership."""
    if name in NOUS_TOOL_CATALOG:
        return "native"
    if name in _CAPABILITY_TOOLS:
        return "capability"
    if name in CAGED_NATIVE_TOOLS:
        return "native"
    return "native"


class Preset(StrEnum):
    EQUILIBRADO = "equilibrado"  # default: all on except high-risk-default-off
    PERMISIVO = "permisivo"      # everything on (owner's choice/responsibility)
    BLOQUEADO = "bloqueado"      # everything off (max lockdown)


def _preset_default(preset: Preset, tool: str) -> bool:
    if preset is Preset.PERMISIVO:
        return True
    if preset is Preset.BLOQUEADO:
        return False
    return default_enabled_equilibrado(tool)  # Equilibrado ← single delicacy source


class ToolPolicyStore:
    """Owner-controlled per-tool enable map, backed by an owner-only JSON file."""

    def __init__(self, path: Path = _DEFAULT_PATH) -> None:
        self._path = path

    def _load(self) -> dict:
        try:
            d = json.loads(self._path.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except (FileNotFoundError, ValueError, OSError):
            return {}

    def _save(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._path)

    def _preset(self) -> Preset:
        raw = self._load().get("preset", Preset.EQUILIBRADO.value)
        try:
            return Preset(raw)
        except ValueError:
            return Preset.EQUILIBRADO

    def is_enabled(self, tool: str) -> bool:
        """Deterministic: explicit override wins, else the active preset's default.

        Unknown tools (not in the catalog) default to the preset default for safety
        (Equilibrado → enabled unless high-risk; Bloqueado → off). Fail-safe leans on
        the preset, never silently allows a high-risk tool.
        """
        d = self._load()
        overrides = d.get("overrides", {})
        if tool in overrides:
            return bool(overrides[tool])
        return _preset_default(self._preset(), tool)

    def is_owner_disabled(self, tool: str) -> bool:
        """True ONLY if the owner has CONSCIOUSLY disabled this tool.

        Distinguishes a deliberate owner decision from a preset default:
          - explicit override=False in the overrides map → True (owner toggled it off)
          - BLOQUEADO preset (owner chose full lockdown) → True
          - Equilibrado default-off (e.g. MOST_DELICATE tools) → False
            These tools have an approval path (HITL); Step 1.5 must not dead-end them
            before Step 1.6 can surface the approval card to the owner.
          - PERMISIVO preset → False (everything is on)

        Fail-safe: any read error → False (do not block; let the HITL gate decide).
        """
        try:
            d = self._load()
            overrides = d.get("overrides", {})
            if tool in overrides:
                return not bool(overrides[tool])
            preset = self._preset()
            return preset is Preset.BLOQUEADO
        except Exception:  # noqa: BLE001 — policy is usability layer; fail-open here
            return False

    def mfa_on_dangers(self) -> bool:
        """Whether DANGEROUS commands require owner MFA per execution (default ON).

        ON (default): a danger (delicacy DELICATE/MOST_DELICATE) pauses for owner MFA
        even in autonomous mode — the agent cannot self-provide the code. OFF (the
        owner's escape hatch, set behind MFA+riddle + a UI alert): dangers run free,
        owner takes responsibility. Fail-SAFE: any read error → True (gate stays up).
        """
        try:
            return bool(self._load().get("mfa_on_dangers", True))
        except Exception:  # noqa: BLE001 — never fail-open the danger gate
            return True

    def set_mfa_on_dangers(self, enabled: bool) -> None:
        d = self._load()
        d["mfa_on_dangers"] = bool(enabled)
        self._save(d)

    def snapshot(self) -> dict:
        """Full state for the UI: preset + every catalog tool's enabled flag.

        Returns:
            preset:         active preset name.
            tools:          {name: bool} — kept for backwards-compat with any
                            existing consumer (frontend, shell_server, tests).
            overridden:     sorted list of tool names with explicit overrides.
            mfa_on_dangers: whether danger-gate MFA is on.
            catalog:        list of enriched tool descriptors — one per tool
                            (static catalog + live dynamic tools from
                            DynamicToolRegistry).  Each entry:
                              name        : tool name (str)
                              label       : human-readable label (str)
                              category    : capability group (str)
                              delicacy    : "normal" | "delicate" | "most_delicate"
                              enabled     : bool (respects overrides + preset)
                              llm_visible : bool (False for NOUS_NATIVE_DUPLICATES
                                            and _OS_NATIVE_SKILL_NAMES suppressed tools)
                              origin      : "native" | "capability" | "mcp" | "composio"
        """
        from hermes.capabilities.tool_delicacy import delicacy  # noqa: PLC0415
        from hermes.capabilities.dynamic_tool_registry import get_dynamic_tool_registry  # noqa: PLC0415

        preset = self._preset()
        overrides = self._load().get("overrides", {})

        # Backwards-compat: the flat tools:{name:bool} map over the STATIC catalog.
        tools = {
            t: (bool(overrides[t]) if t in overrides else _preset_default(preset, t))
            for t in sorted(TOOL_CATALOG)
        }

        # Build the enriched catalog: static tools + live dynamic tools (deduped).
        nous_native_dupes = _get_nous_native_duplicates()
        os_native_skills = _get_os_native_skill_names()
        suppressed_from_llm = nous_native_dupes | os_native_skills

        # Seed with static catalog entries.
        catalog_names: dict[str, str] = {
            name: _tool_origin(name) for name in TOOL_CATALOG
        }

        # Overlay dynamic (live) tools — they take precedence over any same-named static entry.
        dynamic_registry = get_dynamic_tool_registry()
        for entry in dynamic_registry.all():
            catalog_names[entry.name] = entry.origin

        catalog = [
            {
                "name": name,
                "label": _tool_label(name),
                "category": _tool_category(name, origin),
                "delicacy": delicacy(name).value,
                "enabled": bool(overrides[name]) if name in overrides
                           else _preset_default(preset, name),
                "llm_visible": name not in suppressed_from_llm,
                "origin": origin,
            }
            for name, origin in sorted(catalog_names.items())
        ]

        return {
            "preset": preset.value,
            "tools": tools,
            "overridden": sorted(overrides),
            "mfa_on_dangers": self.mfa_on_dangers(),
            "catalog": catalog,
        }

    def set_tool(self, tool: str, enabled: bool) -> None:
        d = self._load()
        overrides = dict(d.get("overrides", {}))
        overrides[tool] = bool(enabled)
        d["overrides"] = overrides
        self._save(d)

    def apply_preset(self, preset: Preset) -> None:
        # A preset is a clean slate: drop per-tool overrides.
        self._save({"preset": preset.value, "overrides": {}})

    def for_agent(self, agent_id: str, overlay: dict) -> "AgentToolPolicyView":
        """Return a per-agent VIEW with a cloud-pushed policy_overlay on top.

        Precedence: agent overlay → global file (this store) → preset default.
        *overlay* is the AgentAccessScope.policy_overlay dict — only ever
        non-empty when a cloud scope set it (Enterprise Fase 2 Phase 2/3);
        an agent with no overlay behaves EXACTLY like calling this store
        directly (zero regression).
        """
        return AgentToolPolicyView(self, agent_id, overlay)


class AgentToolPolicyView:
    """Read-only per-agent overlay on top of a global ToolPolicyStore.

    Shape of *overlay*: {tool_name: {"enabled": bool}} (AgentAccessScope.
    policy_overlay).

    Sovereignty invariant (RESTRICT-ONLY): the cloud-pushed overlay may only
    NARROW the local owner's policy, never widen it. is_enabled is the
    INTERSECTION of the global store and the overlay — overlay `True` can
    NEVER re-enable a tool the owner disabled; overlay `False` CAN additionally
    disable a tool the owner left enabled. is_owner_disabled mirrors this: the
    owner's conscious disable is inviolable (an overlay can never clear it),
    while the overlay may itself register an additional disable. mfa_on_dangers
    has no per-agent axis in this overlay shape (always defers to the global
    decision).

    Malformed overlay entries (missing/wrong-typed "enabled") fail CLOSED:
    they are treated as an explicit DISABLE via the overlay — compatible with
    restrict-only, since a corrupt cloud-pushed entry must never be silently
    upgraded into a permissive default, and can never widen past the owner.
    """

    def __init__(self, base: ToolPolicyStore, agent_id: str, overlay: dict) -> None:
        self._base = base
        self._agent_id = agent_id
        self._overlay = overlay if isinstance(overlay, dict) else {}

    def _overlay_enabled(self, tool: str) -> bool | None:
        """Return the overlay's enabled bit for *tool*, or None if absent.

        A PRESENT but malformed entry (not a dict, or "enabled" not a bool)
        fails CLOSED to False (disabled) rather than falling through to the
        global store — an unenforceable/corrupt cloud-pushed entry must never
        be silently upgraded into a permissive default.
        """
        if tool not in self._overlay:
            return None
        entry = self._overlay[tool]
        if not isinstance(entry, dict) or not isinstance(entry.get("enabled"), bool):
            return False
        return entry["enabled"]

    def is_enabled(self, tool: str) -> bool:
        """INTERSECTION of the owner's policy and the cloud overlay.

        Owner disabled (base False) → always False, regardless of overlay.
        Owner enabled (base True) → overlay `False` narrows to disabled;
        overlay `True`/absent leaves it enabled. The overlay NEVER widens.
        """
        if not self._base.is_enabled(tool):
            return False
        overlay_bit = self._overlay_enabled(tool)
        return True if overlay_bit is None else overlay_bit

    def is_owner_disabled(self, tool: str) -> bool:
        """True if the owner disabled *tool*, OR the overlay additionally did.

        The owner's conscious disable is inviolable — the overlay can never
        clear it. An overlay `False` entry is itself a (narrowing) disable.
        """
        if self._base.is_owner_disabled(tool):
            return True
        overlay_bit = self._overlay_enabled(tool)
        return overlay_bit is False

    def mfa_on_dangers(self) -> bool:
        return self._base.mfa_on_dangers()
