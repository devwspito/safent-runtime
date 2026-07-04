"""Unit tests for hermes.capabilities.proposal_summary.

Verifies that human_summary always returns a plain-language sentence (never
empty, never a raw technical string like "nous tool call: X") and that the
frontend _to_frontend helper surfaces it as the card's `summary` field.
"""

from __future__ import annotations

import pytest

from hermes.capabilities.proposal_summary import human_summary, human_body


# ---------------------------------------------------------------------------
# human_summary — happy paths per tool category
# ---------------------------------------------------------------------------

class TestHumanSummarySkill:
    def test_skill_manage_returns_human_text(self) -> None:
        result = human_summary("skill_manage", {"name": "python_runner"})
        assert "habilidad" in result.lower()
        assert "python_runner" in result

    def test_install_skill_no_name(self) -> None:
        result = human_summary("install_skill", {})
        assert "habilidad" in result.lower()
        assert "nous tool call" not in result

    def test_skill_summary_excludes_web_name(self) -> None:
        result = human_summary("skill_manage", {"name": "web"})
        assert "«web»" not in result


class TestHumanSummaryInstall:
    def test_install_mcp_with_name(self) -> None:
        result = human_summary("install_mcp", {"name": "brave-search"})
        assert "herramienta" in result.lower()
        assert "brave-search" in result

    def test_install_app_with_name(self) -> None:
        result = human_summary("install_app", {"name": "Calculator"})
        assert "aplicación" in result.lower()
        assert "Calculator" in result


class TestHumanSummaryWriteFile:
    def test_write_file_with_path(self) -> None:
        result = human_summary("write_file", {"path": "/tmp/out.txt"})
        assert "archivo" in result.lower()
        assert "/tmp/out.txt" in result

    def test_write_file_no_path(self) -> None:
        result = human_summary("write_file", {})
        assert "archivo" in result.lower()

    def test_patch_is_write_file(self) -> None:
        result = human_summary("patch", {"filename": "app.py"})
        assert "archivo" in result.lower()


class TestHumanSummaryExecuteCode:
    def test_execute_code(self) -> None:
        result = human_summary("execute_code", {})
        assert "comando" in result.lower()

    def test_terminal(self) -> None:
        result = human_summary("terminal", {"command": "ls -la"})
        assert "comando" in result.lower()


class TestHumanSummarySendMessage:
    def test_send_message_with_channel(self) -> None:
        result = human_summary("send_message", {"channel": "#general"})
        assert "mensaje" in result.lower()
        assert "#general" in result

    def test_discord_no_dest(self) -> None:
        result = human_summary("discord", {})
        assert "mensaje" in result.lower()


class TestHumanSummaryBrowser:
    def test_browser_navigate_with_url(self) -> None:
        result = human_summary("browser_navigate", {"url": "https://example.com"})
        assert "página" in result.lower() or "abrir" in result.lower()
        assert "example.com" in result

    def test_browser_navigate_url_truncated(self) -> None:
        long_url = "https://example.com/" + "a" * 80
        result = human_summary("browser_navigate", {"url": long_url})
        assert len(result) < 200  # sanity: no unbounded output
        assert "…" in result


class TestHumanSummaryDelegate:
    def test_delegate_task(self) -> None:
        result = human_summary("delegate_task", {})
        assert "agente" in result.lower()


class TestHumanSummaryDelegateToColleague:
    """FASE 3 (A2A cross-human) — distinct from in-process delegate_task."""

    def test_includes_employee_id(self) -> None:
        result = human_summary("delegate_to_colleague", {"employee_id": "bob@org.example"})
        assert "bob@org.example" in result

    def test_generic_fallback_without_employee_id(self) -> None:
        result = human_summary("delegate_to_colleague", {})
        assert "compañero" in result.lower() or "colega" in result.lower()

    def test_not_confused_with_in_process_delegate_task(self) -> None:
        """'delegate' is a substring of 'delegate_to_colleague' — must NOT
        fall through to the generic in-process delegate_task phrasing."""
        result = human_summary("delegate_to_colleague", {"employee_id": "bob@org.example"})
        assert result != "El agente quiere pedir ayuda a otro agente."

    def test_body_mentions_leaving_the_organization(self) -> None:
        body = human_body("delegate_to_colleague", {"employee_id": "bob@org.example"})
        assert "organización" in body.lower()


class TestHumanSummaryCronjob:
    def test_cronjob(self) -> None:
        result = human_summary("cronjob", {})
        assert "tarea" in result.lower() or "automática" in result.lower()


class TestHumanSummaryPolicy:
    def test_set_policy(self) -> None:
        result = human_summary("set_policy", {})
        assert "permisos" in result.lower() or "seguridad" in result.lower()

    def test_disable_mfa(self) -> None:
        result = human_summary("disable_mfa", {})
        assert "permisos" in result.lower() or "seguridad" in result.lower()


class TestHumanSummaryFallback:
    def test_unknown_tool_never_empty(self) -> None:
        result = human_summary("some_totally_unknown_tool_xyz", {})
        assert result
        assert "nous tool call" not in result
        assert "nous external tool call" not in result
        assert "MCP READ" not in result
        assert "Composio READ" not in result
        assert "capability READ" not in result

    def test_empty_tool_name_returns_string(self) -> None:
        result = human_summary("", {})
        assert isinstance(result, str)
        assert result  # non-empty fallback

    def test_none_args_safe(self) -> None:
        result = human_summary("write_file", None)
        assert "archivo" in result.lower()


# ---------------------------------------------------------------------------
# human_body — contextual explanations
# ---------------------------------------------------------------------------

class TestHumanBody:
    def test_skill_body_mentions_habilidad(self) -> None:
        body = human_body("skill_manage", {})
        assert body  # non-empty for skill
        assert "habilidad" in body.lower()

    def test_write_file_body_shows_path(self) -> None:
        body = human_body("write_file", {"path": "/etc/hosts"})
        assert "/etc/hosts" in body

    def test_write_file_body_empty_without_path(self) -> None:
        body = human_body("write_file", {})
        assert body == ""

    def test_unknown_tool_body_is_empty(self) -> None:
        body = human_body("some_unknown_tool", {})
        assert body == ""


# ---------------------------------------------------------------------------
# Contract: summary is always human (not raw technical strings)
# ---------------------------------------------------------------------------

TECHNICAL_STRINGS = [
    "nous tool call:",
    "nous external tool call:",
    "MCP READ:",
    "Composio READ:",
    "capability READ:",
    "actúa fuera de la jaula",
]

CANDIDATE_TOOLS = [
    ("skill_manage", {"name": "test"}),
    ("write_file", {"path": "/tmp/x"}),
    ("execute_code", {"code": "echo hi"}),
    ("install_mcp", {"name": "srv"}),
    ("cronjob", {}),
    ("send_message", {"channel": "slack"}),
    ("browser_navigate", {"url": "https://x.com"}),
    ("delegate_task", {}),
    ("set_policy", {}),
    ("disable_mfa", {}),
    ("some_unknown_tool", {}),
]


@pytest.mark.parametrize("tool_name, args", CANDIDATE_TOOLS)
def test_summary_never_contains_technical_jargon(tool_name: str, args: dict) -> None:
    result = human_summary(tool_name, args)
    for tech in TECHNICAL_STRINGS:
        assert tech not in result, (
            f"human_summary({tool_name!r}) contained technical string {tech!r}: {result!r}"
        )
