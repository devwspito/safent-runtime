"""Build ToolSpec list from the CapabilityRegistry for the Nous engine (spec 014 inc. 3).

ROOT CAUSE FIXED HERE
---------------------
The Nous engine's `_resolve_external_specs` receives all specs from `_tools_source`
and keeps only those whose name does NOT appear in the Nous native tool catalog
(`classify_nous_tool(s.name) is None`).

Before this module, `native_tool_specs` in `__main__._tools_source` only contained
the 6 OS_NATIVE_SKILLS (screenshot, screen_record, mouse_move, …) from
`build_os_native_tool_specs`.  The 7 DESKTOP_APP tools (lo_open_document,
lo_write_text, lo_save_document, navigate_app, activate_app, click_app_element,
type_in_app), the FILESYSTEM write tools (write_file, delete_file, …), TERMINAL
(run_command, run_terminal), BROWSER (navigate, snapshot, …), etc. had NO ToolSpec
objects — they existed only as CapabilityBinding entries in the registry (so the
broker could enforce policy on them) but were NEVER injected into the LLM schema.

This module builds ToolSpec objects for all capability registry entries that:
  1. Are NOT already covered by `build_os_native_tool_specs` (OS_NATIVE_SKILLS).
  2. Are NOT Nous-internal tools (would collide with the native Nous catalog).

Security invariant (UNBREAKABLE):
  - Every tool call MUST go through CapabilityBroker.dispatch. ZERO direct execution.
  - READ_ONLY (LOW + auto_executable) tools get a broker-dispatching async handler.
  - WRITE (HIGH + not auto_executable) tools get handler=None; they route as
    proposals through GovernedAIAgent._dispatch_external_write → broker.dispatch.
  - Risk is fixed SERVER-SIDE from the CapabilityRegistry. The LLM NEVER dictates risk.
  - Unknown tools (not in registry) → broker fail-closes (REJECTED_BY_POLICY).

DESKTOP_APP specifics (LibreOfficeUnoSurfaceAdapter):
  The adapter reads `action.payload.get("op")` to dispatch to open_document /
  write_text / save_document.  Since each tool name already encodes the operation
  (lo_open_document → "open_document", lo_write_text → "write_text", etc.), we
  inject `op` into the proposal parameters at handler build time so the adapter
  receives it correctly.  The LLM never sees `op` as a free parameter — it is
  a fixed field injected by the handler/proposal shaping logic.

Capa: runtime (wires domain ports — no framework, no I/O directa).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from hermes.capabilities.application.capability_registry import _REGISTRY_TABLE
from hermes.capabilities.domain.ports import RiskLevel
from hermes.domain.tool_spec import ToolRisk, ToolSpec

if TYPE_CHECKING:
    from hermes.capabilities.domain.ports import CapabilityBrokerPort, ConsentContext

logger = logging.getLogger("hermes.runtime.capability_tool_specs")

# ---------------------------------------------------------------------------
# Tools already covered by build_os_native_tool_specs — exclude to avoid
# duplicate ToolSpec objects with conflicting handlers.
# ---------------------------------------------------------------------------
_OS_NATIVE_SKILL_NAMES: frozenset[str] = frozenset({
    "screenshot",
    "screen_record",
    "mouse_move",
    "mouse_click",
    "type_text",
    "begin_computer_use",
})

# ---------------------------------------------------------------------------
# Tools superseded by Nous-native equivalents.
#
# These names map 1:1 to tools that hermes-agent already provides natively.
# Registering them alongside the native tools causes the LLM to see duplicate
# function names which wastes token budget and creates routing ambiguity.
#
# IMPORTANT: removing from this registry does NOT remove the CapabilityBroker
# binding. The broker bindings remain; only the LLM-visible ToolSpec is
# suppressed. Native Nous tools still pass through the broker via GovernedAIAgent.
#
# Mapping (our name → Nous-native replacement):
#   run_command / run_terminal  → terminal, process
#   navigate                    → browser_navigate
#   snapshot                    → browser_snapshot
#   read_url                    → web_extract
#   click  (browser CSS)        → browser_click
#   type_  (browser CSS)        → browser_type
#
# activate_app, click_app_element, type_in_app, navigate_app — NO Nous-native
# equivalent for Wayland/D-Bus GUI control; kept in the LLM schema.
# mouse_click, type_text, begin_computer_use — already excluded via
# _OS_NATIVE_SKILL_NAMES (registered by build_os_native_tool_specs).
# ---------------------------------------------------------------------------
_NOUS_NATIVE_DUPLICATES: frozenset[str] = frozenset({
    # TERMINAL — Nous-native: terminal, process
    "run_command",
    "run_terminal",
    # BROWSER surface — Nous-native: browser_navigate, browser_click,
    # browser_type, browser_snapshot, web_extract
    "navigate",
    "snapshot",
    "read_url",
    "click",   # browser CSS-selector click (NOT mouse_click / Wayland input)
    "type_",   # browser CSS-selector type  (NOT type_text / SessionInputBridge)
    # FILESYSTEM mutación — Nous-native: terminal (rm/mv), patch, write_file.
    # El `terminal` nativo está confinado por Landlock al workspace; lo
    # catastrófico (rm -rf /) lo corta el suelo hardline del hook.
    "delete_file",
    "move_file",
    "rename_file",
    "list_dir",            # Nous-native: search_files / terminal (ls)
    # SCHEDULING — Nous-native: cronjob (create/list/update/pause/resume/remove)
    "schedule_task",
    "unschedule_task",
    "list_scheduled_tasks",
    # PAQUETES — Nous-native: terminal (dnf/pip).
    "install_package",
    "remove_package",
    # RED — Nous-native: web_extract / terminal (curl). (api_call se conserva:
    # puede ser dispatch de Composio.)
    "http_request",
    # INFO de sistema — Nous-native: terminal (uname/lscpu/pactl/lsusb)
    "get_system_info",
    "list_devices",
    "list_audio_devices",
    # CONTROL GUI de elementos — Hermes-native: toolset `computer_use` (backend
    # Wayland lumen-cua-driver: click/type por coordenadas v1, AT-SPI v2).
    "click_app_element",
    "type_in_app",
})

# CONSERVADAS a propósito (NO duplican nada nativo seguro en Linux):
#   - activate_app / navigate_app / click_app_element / type_in_app: control de
#     apps GUI; el `computer_use` nativo de Hermes es macOS-only → sin equivalente.
#   - start/stop/restart_service / get_service_status / list_services: os_native
#     con la DENYLIST anti-autopirateo (seguridad — protege al kernel de que el
#     agente se desactive a sí mismo; `terminal systemctl` no lleva esa protección).
#   - change_system_setting / update_system_setting: mutación sensible del SO.
#   - api_call: posible dispatch de Composio. lo_*: edición de documentos vía UNO.

# ---------------------------------------------------------------------------
# Tools that are part of the Nous native catalog (nous_tool_risk_map).
# Including these would collide with Nous's own dispatch logic.
# ---------------------------------------------------------------------------
_NOUS_NATIVE_NAMES: frozenset[str] = frozenset({
    "read_file",
    "search_files",
    "write_file",
    "patch",
    "web_search",
    "web_extract",
    "browser_snapshot",
    "browser_back",
    "browser_get_images",
    "browser_console",
    "browser_navigate",
    "browser_click",
    "browser_type",
    "browser_scroll",
    "browser_press",
    "browser_vision",
    "browser_cdp",
    "browser_dialog",
    "computer_use",
    "execute_code",
    "terminal",
    "process",
    "send_message",
    "discord",
    "discord_admin",
    "ha_call_service",
    "ha_get_state",
    "ha_list_entities",
    "ha_list_services",
    "feishu_doc_read",
    "feishu_drive_list_comments",
    "feishu_drive_list_comment_replies",
    "feishu_drive_add_comment",
    "feishu_drive_reply_comment",
    "yb_query_group_info",
    "yb_query_group_members",
    "yb_search_sticker",
    "yb_send_dm",
    "yb_send_sticker",
    "x_search",
    "kanban_list",
    "kanban_show",
    "kanban_heartbeat",
    "kanban_create",
    "kanban_complete",
    "kanban_block",
    "kanban_unblock",
    "kanban_comment",
    "kanban_link",
    "skill_manage",
    "skill_view",
    "skills_list",
    "memory",
    "session_search",
    "image_generate",
    "video_generate",
    "text_to_speech",
    "video_analyze",
    "vision_analyze",
    "delegate_task",
    "mixture_of_agents",
    "todo",
    "clarify",
    "cronjob",
})

# ---------------------------------------------------------------------------
# JSON schema definitions for each capability tool (what the LLM sees).
# Kept here to keep capability_registry.py free of presentation concerns.
# ---------------------------------------------------------------------------

_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    # ------------------------------------------------------------------
    # DESKTOP_APP — LibreOffice UNO (lo_*)
    # ------------------------------------------------------------------
    "lo_open_document": {
        "type": "object",
        "properties": {
            "document_path": {
                "type": "string",
                "description": (
                    "Absolute path to the LibreOffice document to open "
                    "(e.g. /home/user/Documents/report.odt). "
                    "Must be within the agent workspace."
                ),
            },
        },
        "required": ["document_path"],
    },
    "lo_write_text": {
        "type": "object",
        "properties": {
            "document_path": {
                "type": "string",
                "description": "Absolute path to the LibreOffice document to edit.",
            },
            "text": {
                "type": "string",
                "description": "Text content to write into the document.",
            },
            "target": {
                "type": "string",
                "enum": ["cursor", "cell"],
                "description": (
                    "Where to write: 'cursor' (text cursor in Writer) "
                    "or 'cell' (spreadsheet cell in Calc, requires cell_address)."
                ),
                "default": "cursor",
            },
            "cell_address": {
                "type": "string",
                "description": (
                    "Cell address when target='cell' (e.g. 'A1', 'B2'). "
                    "Ignored for target='cursor'."
                ),
            },
        },
        "required": ["document_path", "text"],
    },
    "lo_save_document": {
        "type": "object",
        "properties": {
            "document_path": {
                "type": "string",
                "description": "Absolute path to the LibreOffice document to save.",
            },
        },
        "required": ["document_path"],
    },
    # ------------------------------------------------------------------
    # DESKTOP_APP — AT-SPI generic navigation
    # ------------------------------------------------------------------
    "navigate_app": {
        "type": "object",
        "properties": {
            "app_name": {
                "type": "string",
                "description": "Name of the application to focus (e.g. 'LibreOffice Writer', 'Files').",
            },
            "action": {
                "type": "string",
                "description": "Navigation action: 'focus', 'read_tree', or 'open_menu'.",
                "enum": ["focus", "read_tree", "open_menu"],
            },
        },
        "required": ["app_name"],
    },
    "activate_app": {
        "type": "object",
        "properties": {
            "app_name": {
                "type": "string",
                "description": "App to launch in the graphical session (visible on screen). E.g. 'calculadora', 'editor', 'navegador'. Use this to OPEN any visible app — including the browser.",
            },
            "url": {
                "type": "string",
                "description": "Optional. For the browser only: an http(s) URL to open directly (e.g. 'https://www.youtube.com'). Opens a VISIBLE browser at that page. Ignored for non-browser apps.",
            },
        },
        "required": ["app_name"],
    },
    "click_app_element": {
        "type": "object",
        "properties": {
            "app_name": {
                "type": "string",
                "description": "Target application name.",
            },
            "element_role": {
                "type": "string",
                "description": "AT-SPI role of the element (e.g. 'button', 'menu item').",
            },
            "element_name": {
                "type": "string",
                "description": "Accessible name of the element to click.",
            },
        },
        "required": ["app_name", "element_name"],
    },
    "type_in_app": {
        "type": "object",
        "properties": {
            "app_name": {
                "type": "string",
                "description": "Target application name.",
            },
            "text": {
                "type": "string",
                "description": "Text to type into the focused field.",
                "maxLength": 4096,
            },
        },
        "required": ["app_name", "text"],
    },
    # ------------------------------------------------------------------
    # FILESYSTEM writes (read_file / list_dir are Nous-native; writes go here)
    # ------------------------------------------------------------------
    "delete_file": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute path of the file to delete.",
            },
        },
        "required": ["path"],
    },
    "move_file": {
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "Absolute source path."},
            "destination": {"type": "string", "description": "Absolute destination path."},
        },
        "required": ["source", "destination"],
    },
    "rename_file": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Absolute path of the file to rename."},
            "new_name": {"type": "string", "description": "New filename (not full path — just the name)."},
        },
        "required": ["path", "new_name"],
    },
    # ------------------------------------------------------------------
    # TERMINAL
    # ------------------------------------------------------------------
    "run_command": {
        "type": "object",
        "properties": {
            "argv": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Command and arguments as a list (e.g. ['ls', '-la', '/tmp']). "
                    "Shell expansion is NOT supported. Only allowlisted binaries "
                    "are permitted."
                ),
                "minItems": 1,
            },
        },
        "required": ["argv"],
    },
    "run_terminal": {
        "type": "object",
        "properties": {
            "argv": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Command and arguments as a list. No shell expansion.",
                "minItems": 1,
            },
        },
        "required": ["argv"],
    },
    # ------------------------------------------------------------------
    # BROWSER (our surface adapters — not Nous-native browser_* tools)
    # ------------------------------------------------------------------
    "navigate": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to navigate to."},
        },
        "required": ["url"],
    },
    "snapshot": {
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "Browser session ID (optional — uses active session if omitted).",
            },
        },
        "required": [],
    },
    "read_url": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL to fetch and read."},
        },
        "required": ["url"],
    },
    "click": {
        "type": "object",
        "properties": {
            "selector": {"type": "string", "description": "CSS selector or text of element to click."},
        },
        "required": ["selector"],
    },
    "type_": {
        "type": "object",
        "properties": {
            "selector": {"type": "string", "description": "CSS selector of the input field."},
            "text": {"type": "string", "description": "Text to type into the field."},
        },
        "required": ["selector", "text"],
    },
    # ------------------------------------------------------------------
    # API_CALL
    # ------------------------------------------------------------------
    "api_call": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "HTTP endpoint URL."},
            "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"], "default": "GET"},
            "body": {"type": "object", "description": "Request body (JSON)."},
            "headers": {"type": "object", "description": "Additional HTTP headers."},
        },
        "required": ["url"],
    },
    "http_request": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "HTTP endpoint URL."},
            "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"], "default": "GET"},
            "body": {"type": "object", "description": "Request body (JSON)."},
        },
        "required": ["url"],
    },
    # ------------------------------------------------------------------
    # FILESYSTEM READ (not Nous-native: list_dir differs from search_files)
    # ------------------------------------------------------------------
    "list_dir": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute path of the directory to list.",
            },
        },
        "required": ["path"],
    },
    # ------------------------------------------------------------------
    # OS-NATIVE READ — system info and devices (feature 007)
    # ------------------------------------------------------------------
    "get_service_status": {
        "type": "object",
        "properties": {
            "unit": {
                "type": "string",
                "description": "systemd unit name (e.g. 'hermes-runtime.service').",
            },
        },
        "required": ["unit"],
    },
    "get_system_info": {
        "type": "object",
        "properties": {
            "fields": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Subset of info fields to retrieve (e.g. ['cpu', 'memory']). Empty = all.",
            },
        },
        "required": [],
    },
    "list_audio_devices": {
        "type": "object",
        "properties": {},
        "required": [],
    },
    "list_devices": {
        "type": "object",
        "properties": {
            "subsystem": {
                "type": "string",
                "description": "Optional udev subsystem filter (e.g. 'usb', 'block').",
            },
        },
        "required": [],
    },
    "list_scheduled_tasks": {
        "type": "object",
        "properties": {},
        "required": [],
    },
    "list_services": {
        "type": "object",
        "properties": {
            "state": {
                "type": "string",
                "enum": ["active", "inactive", "failed", "all"],
                "description": "Filter by service state.",
                "default": "all",
            },
        },
        "required": [],
    },
    # ------------------------------------------------------------------
    # OS-NATIVE WRITE — service management and scheduling (feature 007)
    # ------------------------------------------------------------------
    "restart_service": {
        "type": "object",
        "properties": {
            "unit": {
                "type": "string",
                "description": "systemd unit name to restart.",
            },
        },
        "required": ["unit"],
    },
    "schedule_task": {
        "type": "object",
        "properties": {
            "task_name": {
                "type": "string",
                "description": "Identifier for the scheduled task.",
            },
            "cron_expression": {
                "type": "string",
                "description": "Cron expression (e.g. '0 9 * * 1-5' for 9am weekdays).",
            },
            "command": {
                "type": "string",
                "description": "Command to run (must be on the terminal allowlist).",
            },
        },
        "required": ["task_name", "cron_expression", "command"],
    },
    "start_service": {
        "type": "object",
        "properties": {
            "unit": {
                "type": "string",
                "description": "systemd unit name to start.",
            },
        },
        "required": ["unit"],
    },
    "stop_service": {
        "type": "object",
        "properties": {
            "unit": {
                "type": "string",
                "description": "systemd unit name to stop.",
            },
        },
        "required": ["unit"],
    },
    "unschedule_task": {
        "type": "object",
        "properties": {
            "task_name": {
                "type": "string",
                "description": "Identifier of the scheduled task to remove.",
            },
        },
        "required": ["task_name"],
    },
    # ------------------------------------------------------------------
    # SYSTEM_SETTINGS
    # ------------------------------------------------------------------
    "change_system_setting": {
        "type": "object",
        "properties": {
            "setting": {"type": "string", "description": "System setting key to change."},
            "value": {"description": "New value for the setting."},
        },
        "required": ["setting", "value"],
    },
    "update_system_setting": {
        "type": "object",
        "properties": {
            "setting": {"type": "string", "description": "System setting key to update."},
            "value": {"description": "New value for the setting."},
        },
        "required": ["setting", "value"],
    },
    # ------------------------------------------------------------------
    # PACKAGE_MANAGER
    # ------------------------------------------------------------------
    "install_package": {
        "type": "object",
        "properties": {
            "package": {"type": "string", "description": "Package name to install."},
        },
        "required": ["package"],
    },
    "remove_package": {
        "type": "object",
        "properties": {
            "package": {"type": "string", "description": "Package name to remove."},
        },
        "required": ["package"],
    },
    # ------------------------------------------------------------------
    # INSTALL — búsqueda e instalación de extensiones del SO agéntico
    # ------------------------------------------------------------------
    "search_mcp": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query for MCP servers (e.g. 'github', 'postgres').",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of results to return (default 20, max 50).",
                "default": 20,
            },
        },
        "required": ["query"],
    },
    "search_skills": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query for skills in the Hermes Skills Hub.",
            },
            "source": {
                "type": "string",
                "description": "Source filter: 'all' (default), 'github', or a specific hub source.",
                "default": "all",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of results (default 20, max 50).",
                "default": 20,
            },
        },
        "required": ["query"],
    },
    "search_apps": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query for Linux apps (Flatpak / RPM).",
            },
            "source": {
                "type": "string",
                "description": "Package source: 'all' (default), 'flatpak', or 'rpm'.",
                "default": "all",
            },
        },
        "required": ["query"],
    },
    "install_mcp": {
        "type": "object",
        "properties": {
            "server_id": {
                "type": "string",
                "description": (
                    "Unique ID for the MCP server (lowercase alphanumeric + hyphens, "
                    "e.g. 'github-mcp', 'postgres-mcp')."
                ),
            },
            "argv": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Command to start the MCP server "
                    "(e.g. ['npx', '-y', '@modelcontextprotocol/server-github']). "
                    "First element must be an allowed runner: npx, uvx, node, python3."
                ),
                "minItems": 1,
            },
            "env": {
                "type": "object",
                "description": (
                    "Optional BYOK environment variables for the MCP server "
                    "(e.g. {\"GITHUB_TOKEN\": \"ghp_...\"}). "
                    "Only whitelisted keys are accepted."
                ),
                "additionalProperties": {"type": "string"},
            },
        },
        "required": ["server_id", "argv"],
    },
    "install_skill": {
        "type": "object",
        "properties": {
            "identifier": {
                "type": "string",
                "description": (
                    "Skill identifier from the Hermes Skills Hub "
                    "(e.g. 'owner/repo' for GitHub-hosted skills)."
                ),
            },
        },
        "required": ["identifier"],
    },
    "install_app": {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": "Package source: 'flatpak' or 'rpm'.",
                "enum": ["flatpak", "rpm"],
            },
            "package_id": {
                "type": "string",
                "description": (
                    "Package identifier for the chosen source "
                    "(e.g. 'com.github.tchx84.Flatseal' for Flatpak, "
                    "'vim' for RPM)."
                ),
            },
        },
        "required": ["source", "package_id"],
    },
    "connect_integration": {
        "type": "object",
        "properties": {
            "slug": {
                "type": "string",
                "description": (
                    "Composio toolkit slug for the OAuth-simple integration to connect "
                    "(e.g. 'github', 'gmail', 'slack', 'notion'). "
                    "Only OAuth2/OAuth1 apps are supported; API-key apps use "
                    "configure_native_provider instead."
                ),
            },
        },
        "required": ["slug"],
    },
}

# ---------------------------------------------------------------------------
# DESKTOP_APP: map tool_name → UNO operation string
# (injected into proposal.parameters as "op" so the adapter dispatches correctly)
# ---------------------------------------------------------------------------
_DESKTOP_APP_OP_MAP: dict[str, str] = {
    "lo_open_document": "open_document",
    "lo_write_text": "write_text",
    "lo_save_document": "save_document",
}

# ---------------------------------------------------------------------------
# Human-readable descriptions for each tool (what the LLM reads)
# ---------------------------------------------------------------------------
_TOOL_DESCRIPTIONS: dict[str, str] = {
    "lo_open_document": (
        "Open a LibreOffice document (Writer, Calc, Impress) via UNO. "
        "Use to inspect or prepare a document before writing. "
        "LOW risk — read-only observation."
    ),
    "lo_write_text": (
        "Write text into an open LibreOffice document via UNO. "
        "For Writer: inserts at cursor. For Calc: writes to the specified cell. "
        "HIGH risk — HITL approval required before execution."
    ),
    "lo_save_document": (
        "Save the current LibreOffice document to disk via UNO. "
        "HIGH risk — persists irreversible changes. HITL approval required."
    ),
    "navigate_app": (
        "Navigate to or read the accessibility tree of a desktop application. "
        "LOW risk — observation only, no state mutation."
    ),
    "activate_app": (
        "USE THIS to OPEN or LAUNCH a desktop app the user must SEE on screen, OR to open "
        "the browser at a website the user wants to VIEW. By name (calculator, calendar, "
        "files, text editor, browser). For the BROWSER, pass `url` to open it directly at "
        "that page: activate_app(app_name='navegador', url='https://www.youtube.com') opens "
        "a VISIBLE Chromium window at YouTube. So 'open the browser', 'open YouTube', "
        "'show me <website>', 'open the calculator' → activate_app (with url if it's a web). "
        "Instant — no mouse/keyboard control needed. LOW risk — no data mutation. "
        "To READ or automate a website WITHOUT showing it to the user (scraping, background "
        "form-fill, checking a value), use browser_navigate instead (headless, invisible). "
        "Prefer activate_app over begin_computer_use for simply opening or switching to an app."
    ),
    "click_app_element": (
        "Click an accessible element in a desktop application (button, menu item, etc.). "
        "HIGH risk — can submit forms, trigger destructive actions. HITL required."
    ),
    "type_in_app": (
        "Type text into the focused field of a desktop application. "
        "HIGH risk — can enter credentials or commands. HITL required."
    ),
    "delete_file": (
        "Permanently delete a file. HIGH risk — irreversible. HITL required."
    ),
    "move_file": (
        "Move a file to a new location. HIGH risk — modifies filesystem. HITL required."
    ),
    "rename_file": (
        "Rename a file. HIGH risk — modifies filesystem. HITL required."
    ),
    "run_command": (
        "Run a terminal command. HIGH risk — executes arbitrary code. "
        "Only allowlisted read-only binaries may skip HITL; others always require approval."
    ),
    "run_terminal": (
        "Run a terminal command. HIGH risk — always requires HITL approval."
    ),
    "navigate": (
        "USE THIS to OPEN a website or navigate to a URL in the browser "
        "(e.g. 'open google.com', 'go to https://example.com'). "
        "LOW risk — passive navigation. "
        "Prefer this over begin_computer_use whenever the goal is visiting a web address."
    ),
    "snapshot": (
        "Capture the current state of the browser page (DOM snapshot). LOW risk."
    ),
    "read_url": (
        "Fetch and read the content of a URL. LOW risk — passive read."
    ),
    "click": (
        "Click an element in the browser. HIGH risk — can submit forms, authenticate."
    ),
    "type_": (
        "Type text into a browser input field. HIGH risk — can enter credentials."
    ),
    "api_call": (
        "Make an HTTP request to an external API. HIGH risk — external network call."
    ),
    "http_request": (
        "Make an HTTP request. HIGH risk — external network call."
    ),
    "list_dir": (
        "List the contents of a directory. LOW risk — read-only observation."
    ),
    "get_service_status": (
        "Get the status of a systemd service. LOW risk — read-only."
    ),
    "get_system_info": (
        "Read system information (CPU, memory, OS version). LOW risk — read-only."
    ),
    "list_audio_devices": (
        "List audio input/output devices. LOW risk — read-only."
    ),
    "list_devices": (
        "List hardware devices via udev. LOW risk — read-only."
    ),
    "list_scheduled_tasks": (
        "List currently scheduled tasks. LOW risk — read-only."
    ),
    "list_services": (
        "List systemd services and their states. LOW risk — read-only."
    ),
    "restart_service": (
        "Restart a systemd service. HIGH risk — disrupts running service. HITL required."
    ),
    "schedule_task": (
        "Schedule a recurring task. HIGH risk — modifies scheduler. HITL required."
    ),
    "start_service": (
        "Start a stopped systemd service. HIGH risk — HITL required."
    ),
    "stop_service": (
        "Stop a running systemd service. HIGH risk — disrupts service. HITL required."
    ),
    "unschedule_task": (
        "Remove a scheduled task. HIGH risk — HITL required."
    ),
    "change_system_setting": (
        "Change a system setting. HIGH risk — modifies OS configuration."
    ),
    "update_system_setting": (
        "Update a system setting. HIGH risk — modifies OS configuration."
    ),
    "install_package": (
        "Install a software package. HIGH risk — modifies the system. HITL required."
    ),
    "remove_package": (
        "Remove a software package. HIGH risk — modifies the system. HITL required."
    ),
    # ------------------------------------------------------------------
    # INSTALL — búsqueda e instalación de extensiones del SO agéntico
    # ------------------------------------------------------------------
    "search_mcp": (
        "Search the MCP server registry for available MCP servers. "
        "LOW risk — read-only network query, no installation. "
        "Returns a list of matching servers with their commands and descriptions. "
        "Use this before install_mcp to discover the correct argv."
    ),
    "search_skills": (
        "Search the Hermes Skills Hub for available agent skills. "
        "LOW risk — read-only query across GitHub and configured skill sources. "
        "Returns skill name, description, identifier, and trust level. "
        "Use this before install_skill to find the correct identifier."
    ),
    "search_apps": (
        "Search Flathub and/or RPM repositories for Linux desktop applications. "
        "LOW risk — read-only package catalog query. "
        "Returns application name, description, and package_id. "
        "Use this before install_app to find the correct package_id and source."
    ),
    "install_mcp": (
        "Install and connect an MCP server to the agent runtime. "
        "HIGH risk — modifies the agent extension surface; requires HITL approval. "
        "The server is scanned by the Security Center (scan → score → block if FAIL) "
        "before connecting. Only allowed runners (npx, uvx, node, python3) are accepted. "
        "Use search_mcp first to find the correct server_id and argv."
    ),
    "install_skill": (
        "Install an agent skill from the Hermes Skills Hub. "
        "HIGH risk — adds executable skill code to the agent; requires HITL approval. "
        "The skill is scanned by the Security Center before installation. "
        "Use search_skills first to find the correct identifier."
    ),
    "install_app": (
        "Install a Linux desktop application via Flatpak or RPM. "
        "HIGH risk — modifies the system; requires HITL approval. "
        "The app is scanned by the Security Center (CVE / provenance check) before install. "
        "Use search_apps first to find the correct source and package_id. "
        "Returns an op_id that can be polled for async install progress."
    ),
    "connect_integration": (
        "Connect a third-party integration via OAuth (e.g. GitHub, Gmail, Slack). "
        "HIGH risk — initiates an OAuth flow; requires HITL approval. "
        "Only OAuth2/OAuth1 (simple-link) apps are supported. "
        "Returns a redirect_url / connect_url that the user must open in a browser "
        "to authorise the connection. "
        "For API-key providers (OpenAI, Anthropic…) use configure_native_provider instead."
    ),
}


def _risk_level_to_tool_risk(risk: RiskLevel) -> ToolRisk:
    """Map CapabilityRegistry RiskLevel → ToolSpec ToolRisk.

    LOW → READ_ONLY (broker-dispatching handler, no HITL).
    HIGH → WRITE_PROPOSAL (handler=None, routes as proposal through HITL).
    """
    return ToolRisk.READ_ONLY if risk == RiskLevel.LOW else ToolRisk.WRITE_PROPOSAL


def _make_read_handler(
    tool_name: str,
    *,
    broker: "CapabilityBrokerPort",
    consent_ref: "list[ConsentContext]",
    op_override: str | None = None,
) -> Any:
    """Build an async broker-dispatching handler for a READ_ONLY capability tool.

    The handler constructs a ToolCallProposal and calls broker.dispatch, gaining:
      - Kill-switch (CTRL-12)
      - Consent gate (CTRL-2/13)
      - Audit (CTRL-9)
      - HITL bypass only for LOW + auto_executable bindings (CTRL-4)

    op_override: if set, injects {"op": op_override} into the parameters so that
    DESKTOP_APP adapters receive the correct operation discriminator without
    exposing it as a free LLM parameter.

    consent_ref: mutable single-element list holding the active ConsentContext.
    The engine updates consent_ref[0] per-cycle with the real per-task operator_id
    (spec 014 inc. 3 / CTRL-13 fix). All handlers built from the same
    build_capability_tool_specs call share this reference — updating it once
    propagates to all READ handlers without rebuilding them. The list is the
    mutable cell; ConsentContext itself remains immutable (frozen dataclass).

    Security invariant: the only writer of consent_ref[0] is
    NousReasoningEngine._update_capability_consent_ref, which derives the
    operator_id exclusively from DecisionContext.metadata["task_operator_id"]
    (set by the orchestrator from item.payload["enqueued_by"] — server-side
    channel.sender_uid — CTRL-P1-3 / CWE-862). The LLM never writes to it.
    """
    from hermes.domain.proposal import ToolCallProposal  # noqa: PLC0415
    from hermes.capabilities.domain.ports import ExecutionStatus  # noqa: PLC0415

    async def _handler(args: dict[str, Any]) -> dict[str, Any]:
        params = dict(args)
        if op_override is not None:
            params["op"] = op_override

        # Read the current consent context from the mutable ref. This is the
        # per-cycle value updated by the engine before calling the handler.
        effective_ctx: "ConsentContext" = consent_ref[0]

        proposal = ToolCallProposal(
            proposal_id=uuid4(),
            tool_name=tool_name,
            tenant_id=effective_ctx.tenant_id,
            entity_id=str(effective_ctx.tenant_id),
            entity_type="os_surface",
            parameters=params,
            justification=f"capability READ: {tool_name}",
        )
        outcome = await broker.dispatch(proposal, effective_ctx)

        if outcome.status is ExecutionStatus.EXECUTED:
            return outcome.result or {}

        logger.warning(
            "hermes.capability_tool_specs.read_rejected: tool=%s status=%s error=%s",
            tool_name,
            outcome.status,
            outcome.error,
        )
        return {
            "error": f"capability_read_blocked: {outcome.status}",
            "detail": outcome.error or "",
        }

    return _handler


def build_capability_tool_specs(
    *,
    broker: "CapabilityBrokerPort",
    consent_context: "ConsentContext",
    registered_surface_kinds: "frozenset | None" = None,
) -> "tuple[tuple[ToolSpec, ...], list[ConsentContext]]":
    """Build ToolSpec objects for all CapabilityRegistry entries not covered elsewhere.

    Excludes:
      - OS_NATIVE_SKILLS (covered by build_os_native_tool_specs).
      - Nous-native tool names (would collide with Nous's own dispatch).
      - skill_manage / memory / session_search (internal agent tools, handled
        via nous_tool_risk_map WRITE path or Nous natively).

    READ_ONLY tools get a broker-dispatching async handler.
    WRITE tools get handler=None — they route as proposals through
    GovernedAIAgent._dispatch_external_write → broker.dispatch.

    Security invariant: EVERY execution path reaches broker.dispatch.
    No direct adapter calls. No bypass.

    Returns:
        (specs, consent_ref) where consent_ref is a mutable single-element list
        holding the active ConsentContext for all READ handlers. The engine
        updates consent_ref[0] per-cycle with the real per-task operator_id
        (spec 014 inc. 3 / CTRL-13 fix). Callers MUST NOT update consent_ref
        from any path reachable by the LLM (CWE-862).
    """
    # Shared mutable cell for per-cycle operator_id propagation (spec 014 inc. 3).
    # All READ handlers close over this reference; updating [0] propagates
    # instantly to every handler without rebuilding specs.
    consent_ref: list[ConsentContext] = [consent_context]

    specs: list[ToolSpec] = []
    skipped_no_schema: list[str] = []
    skipped_unregistered: list[str] = []

    for tool_name, binding in _REGISTRY_TABLE.items():
        if tool_name in _OS_NATIVE_SKILL_NAMES:
            continue
        if tool_name in _NOUS_NATIVE_NAMES:
            continue
        if tool_name in _NOUS_NATIVE_DUPLICATES:
            continue

        # No anunciamos al LLM una tool de surface_adapter cuyo adapter NO está
        # registrado en este proceso: dispararía SurfaceAdapterNotFound al
        # despachar (p.ej. api_call sin allowlist de hosts, o tools visibles
        # APP_LAUNCH en la variante terminal sin compositor). "Anunciado ⟺
        # ejecutable" — sin tools rotas. registered_surface_kinds es la verdad
        # de terreno del SurfaceAdapterDispatcher (fail-open si None: comportamiento
        # previo). Solo filtra executor="surface_adapter"; install/os_native/
        # composio/mcp tienen su propio executor y no pasan por el dispatcher.
        if (
            registered_surface_kinds is not None
            and getattr(binding, "executor", "") == "surface_adapter"
            and binding.surface_kind is not None
            and binding.surface_kind not in registered_surface_kinds
        ):
            skipped_unregistered.append(tool_name)
            continue

        schema = _TOOL_SCHEMAS.get(tool_name)
        if schema is None:
            skipped_no_schema.append(tool_name)
            continue

        description = _TOOL_DESCRIPTIONS.get(tool_name, f"Capability tool: {tool_name}")
        tool_risk = _risk_level_to_tool_risk(binding.risk)
        op_override = _DESKTOP_APP_OP_MAP.get(tool_name)
        # BROWSER: el adapter despacha por op == tool_name (navigate/click/read_url/
        # snapshot/type_). Sin esto el op llega vacío → "unknown browser op=''".
        if op_override is None and getattr(binding.surface_kind, "value", "") == "browser":
            op_override = tool_name

        if tool_risk == ToolRisk.READ_ONLY:
            handler = _make_read_handler(
                tool_name,
                broker=broker,
                consent_ref=consent_ref,
                op_override=op_override,
            )
        else:
            handler = None

        try:
            spec = ToolSpec(
                name=tool_name,
                description=description,
                parameters_schema=schema,
                risk=tool_risk,
                entity_type="os_surface",
                handler=handler,
            )
            specs.append(spec)
        except ValueError as exc:
            logger.error(
                "hermes.capability_tool_specs.spec_build_failed: tool=%s error=%s",
                tool_name,
                exc,
            )

    if skipped_no_schema:
        logger.warning(
            "hermes.capability_tool_specs.skipped_no_schema: tools=%s — "
            "add to _TOOL_SCHEMAS to expose to the LLM",
            sorted(skipped_no_schema),
        )
    if skipped_unregistered:
        logger.info(
            "hermes.capability_tool_specs.skipped_unregistered_surface: tools=%s — "
            "surface adapter not registered in this process (advertise⟺executable)",
            sorted(skipped_unregistered),
        )

    count = len(specs)
    logger.info(
        "hermes.capability_tool_specs.built: count=%d tools=%s",
        count,
        sorted(s.name for s in specs),
    )
    return tuple(specs), consent_ref
