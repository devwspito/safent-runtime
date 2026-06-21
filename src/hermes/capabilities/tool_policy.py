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
        """Full state for the UI: preset + every catalog tool's enabled flag."""
        preset = self._preset()
        overrides = self._load().get("overrides", {})
        tools = {
            t: (bool(overrides[t]) if t in overrides else _preset_default(preset, t))
            for t in sorted(TOOL_CATALOG)
        }
        return {"preset": preset.value, "tools": tools, "overridden": sorted(overrides),
                "mfa_on_dangers": self.mfa_on_dangers()}

    def set_tool(self, tool: str, enabled: bool) -> None:
        d = self._load()
        overrides = dict(d.get("overrides", {}))
        overrides[tool] = bool(enabled)
        d["overrides"] = overrides
        self._save(d)

    def apply_preset(self, preset: Preset) -> None:
        # A preset is a clean slate: drop per-tool overrides.
        self._save({"preset": preset.value, "overrides": {}})
