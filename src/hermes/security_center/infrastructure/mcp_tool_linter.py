"""McpToolLinter — detect destructive tool names in MCP server manifests."""

from __future__ import annotations

import json
import logging
import re

from hermes.security_center.domain.install_target import InstallTarget
from hermes.security_center.domain.scan_score import Risk, Severity

logger = logging.getLogger("hermes.security_center.mcp_tool_linter")

# Patterns that indicate destructive intent in a tool name.
# Use (?<![a-zA-Z])...(?![a-zA-Z]) instead of \b so that underscore-joined
# names like "delete_user" or "drop_table" are matched correctly — \b treats
# underscore as a word character and misses those cases.
_DESTRUCTIVE_PATTERN = re.compile(
    r"(?<![a-zA-Z])(delete|remove|drop|rm|unlink|kill|format|wipe|purge|truncate|destroy)(?![a-zA-Z])",
    re.IGNORECASE,
)

# argv runners carrying elevated risk deserve an extra note.
_RISKY_RUNNERS = frozenset({"bash", "sh", "zsh", "fish", "pwsh", "powershell"})


class McpToolLinter:
    """Parses the manifest_json field and flags destructive tool names.

    Also checks argv[0] for shell runners (higher risk).
    """

    name = "mcp_lint"

    async def scan(self, target: InstallTarget) -> list[Risk]:
        risks: list[Risk] = []
        risks.extend(self._lint_argv(target))
        risks.extend(self._lint_manifest(target))
        return risks

    def _lint_argv(self, target: InstallTarget) -> list[Risk]:
        if not target.argv:
            return []
        runner = target.argv[0].rsplit("/", 1)[-1]
        if runner in _RISKY_RUNNERS:
            return [Risk(
                category="mcp_lint",
                severity=Severity.HIGH,
                message=f"MCP server uses risky shell runner: {runner}",
                evidence_ref=f"mcp_lint:risky_runner:{runner}",
            )]
        return []

    def _lint_manifest(self, target: InstallTarget) -> list[Risk]:
        if not target.manifest_json:
            return []
        try:
            manifest = json.loads(target.manifest_json)
        except json.JSONDecodeError as exc:
            logger.warning("hermes.security.mcp_linter_parse_error: %s", exc)
            return [Risk(
                category="mcp_lint",
                severity=Severity.LOW,
                message="manifest_json could not be parsed",
                evidence_ref="mcp_lint:parse_error",
            )]

        tools = manifest.get("tools") or []
        if not isinstance(tools, list):
            return []

        risks = []
        for tool in tools:
            if isinstance(tool, str):
                tool_name = tool
            else:
                tool_name = str(tool.get("name") or "")
            if _DESTRUCTIVE_PATTERN.search(tool_name):
                risks.append(Risk(
                    category="mcp_lint",
                    severity=Severity.HIGH,
                    message=f"Tool '{tool_name}' has a destructive name",
                    evidence_ref=f"mcp_lint:destructive_tool:{tool_name}",
                ))
        return risks
