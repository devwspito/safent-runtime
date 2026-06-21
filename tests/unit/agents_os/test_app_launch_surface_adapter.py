"""Tests for AppLaunchSurfaceAdapter — URL support (TAREA 1).

Covers:
  - Happy path: browser + valid HTTPS URL → cmd includes URL.
  - Happy path: browser + valid HTTP URL → cmd includes URL.
  - No URL: existing behavior unchanged (bare binary).
  - Non-browser app with URL: URL silently ignored, bare binary launched.
  - URL validation: rejects file://, javascript:, data:, shell metacharacters,
    oversized URLs, empty scheme.
  - resolve_binary: alias map (navegador, browser, chromium, etc.).
  - surface_kind mismatch → REJECTED_BY_POLICY.
  - Missing app_name → REJECTED_BY_POLICY.
  - Unknown app_name → REJECTED_BY_POLICY.
  - Missing emitter → EXECUTED_FAILED.
  - serialize_for_signing includes url field.
"""
from __future__ import annotations

import pytest
from uuid import uuid4

from hermes.agents_os.domain.ports.surface_adapter_port import (
    CapturedAction,
    ReplayStatus,
)
from hermes.agents_os.domain.surface_kind import SurfaceKind
from hermes.agents_os.infrastructure.app_launch_surface_adapter import (
    AppLaunchSurfaceAdapter,
    _build_cmd,
    _validate_url,
    _resolve_binary,
)

pytestmark = pytest.mark.unit

_TENANT = uuid4()
_OPERATOR = uuid4()


def _make_action(
    surface_kind: SurfaceKind = SurfaceKind.APP_LAUNCH,
    app_name: str | None = "navegador",
    url: str | None = None,
) -> CapturedAction:
    payload: dict = {}
    if app_name is not None:
        payload["app_name"] = app_name
    if url is not None:
        payload["url"] = url
    return CapturedAction(
        surface_kind=surface_kind,
        intent_desc="test",
        payload=payload,
        tenant_id=_TENANT,
        human_operator_id=_OPERATOR,
    )


# ---------------------------------------------------------------------------
# _validate_url
# ---------------------------------------------------------------------------
class TestValidateUrl:
    def test_https_valid(self) -> None:
        assert _validate_url("https://www.youtube.com") is None

    def test_http_valid(self) -> None:
        assert _validate_url("http://example.com/path?q=1") is None

    def test_empty_url(self) -> None:
        assert _validate_url("") is not None

    def test_file_scheme_blocked(self) -> None:
        assert _validate_url("file:///etc/passwd") is not None

    def test_javascript_scheme_blocked(self) -> None:
        assert _validate_url("javascript:alert(1)") is not None

    def test_data_scheme_blocked(self) -> None:
        assert _validate_url("data:text/html,<h1>x</h1>") is not None

    def test_semicolon_injection(self) -> None:
        assert _validate_url("https://x.com; rm -rf /") is not None

    def test_pipe_injection(self) -> None:
        assert _validate_url("https://x.com|cat /etc/shadow") is not None

    def test_backtick_injection(self) -> None:
        assert _validate_url("https://x.com`whoami`") is not None

    def test_dollar_injection(self) -> None:
        assert _validate_url("https://x.com?q=$(cat /etc/passwd)") is not None

    def test_newline_injection(self) -> None:
        assert _validate_url("https://x.com\nX-Injected: evil") is not None

    def test_oversized_url(self) -> None:
        long_url = "https://x.com/" + "a" * 2048
        assert _validate_url(long_url) is not None

    def test_max_length_ok(self) -> None:
        # exactly 2048 chars: "https://x.com/" is 15 chars
        prefix = "https://x.com/"
        url = prefix + "a" * (2048 - len(prefix))
        assert len(url) == 2048
        assert _validate_url(url) is None


# ---------------------------------------------------------------------------
# _build_cmd
# ---------------------------------------------------------------------------
class TestBuildCmd:
    def test_browser_with_https_url(self) -> None:
        cmd = _build_cmd("chromium-browser", "https://youtube.com")
        assert cmd == "chromium-browser https://youtube.com"

    def test_browser_no_url(self) -> None:
        assert _build_cmd("chromium-browser", "") == "chromium-browser"

    def test_non_browser_with_url_ignored(self) -> None:
        # gnome-calculator doesn't accept URLs → bare binary
        cmd = _build_cmd("gnome-calculator", "https://youtube.com")
        assert cmd == "gnome-calculator"

    def test_invalid_url_returns_error_sentinel(self) -> None:
        result = _build_cmd("chromium-browser", "javascript:alert(1)")
        assert result.startswith("ERROR:")

    def test_file_url_returns_error_sentinel(self) -> None:
        result = _build_cmd("chromium-browser", "file:///etc/passwd")
        assert result.startswith("ERROR:")

    def test_shell_injection_returns_error_sentinel(self) -> None:
        result = _build_cmd("chromium-browser", "https://x.com; rm -rf /")
        assert result.startswith("ERROR:")


# ---------------------------------------------------------------------------
# _resolve_binary
# ---------------------------------------------------------------------------
class TestResolveBinary:
    @pytest.mark.parametrize("alias", [
        "navegador", "browser", "chromium", "chromium-browser",
        "chrome", "web", "internet",
        "Navegador",        # case-insensitive
        "BROWSER",
        "navegádor",  # accent: á → a after NFD strip
    ])
    def test_browser_aliases_resolve_to_chromium_browser(self, alias: str) -> None:
        assert _resolve_binary(alias) == "chromium-browser"

    @pytest.mark.parametrize("alias", [
        "calculadora", "calculator", "calc",
    ])
    def test_calculator_aliases(self, alias: str) -> None:
        assert _resolve_binary(alias) == "gnome-calculator"

    def test_app_with_spaces_not_in_alias_map_returns_none(self) -> None:
        # Names with spaces that are not in the alias map can't resolve — they
        # fail both the alias lookup and the _SAFE_BINARY_RE (which disallows spaces).
        assert _resolve_binary("unknown application xyz") is None

    def test_path_traversal_rejected(self) -> None:
        assert _resolve_binary("../../etc/passwd") is None

    def test_shell_chars_rejected(self) -> None:
        assert _resolve_binary("chromium; rm -rf /") is None


# ---------------------------------------------------------------------------
# AppLaunchSurfaceAdapter.replay
# ---------------------------------------------------------------------------
class TestReplay:
    @pytest.mark.asyncio
    async def test_browser_with_url_emits_correct_cmd(self) -> None:
        emitted: list[str] = []
        adapter = AppLaunchSurfaceAdapter(launch_emitter=emitted.append)
        action = _make_action(app_name="navegador", url="https://youtube.com")

        outcome = await adapter.replay(action)

        assert outcome.status == ReplayStatus.EXECUTED_OK
        assert len(emitted) == 1
        assert emitted[0] == "chromium-browser https://youtube.com"
        assert outcome.result["url"] == "https://youtube.com"

    @pytest.mark.asyncio
    async def test_browser_without_url_emits_bare_binary(self) -> None:
        emitted: list[str] = []
        adapter = AppLaunchSurfaceAdapter(launch_emitter=emitted.append)
        action = _make_action(app_name="navegador")

        outcome = await adapter.replay(action)

        assert outcome.status == ReplayStatus.EXECUTED_OK
        assert emitted[0] == "chromium-browser"
        assert outcome.result["url"] is None

    @pytest.mark.asyncio
    async def test_calculator_with_url_emits_bare_binary(self) -> None:
        emitted: list[str] = []
        adapter = AppLaunchSurfaceAdapter(launch_emitter=emitted.append)
        action = _make_action(app_name="calculadora", url="https://youtube.com")

        outcome = await adapter.replay(action)

        assert outcome.status == ReplayStatus.EXECUTED_OK
        assert emitted[0] == "gnome-calculator"

    @pytest.mark.asyncio
    async def test_invalid_url_rejected(self) -> None:
        emitted: list[str] = []
        adapter = AppLaunchSurfaceAdapter(launch_emitter=emitted.append)
        action = _make_action(app_name="navegador", url="javascript:evil()")

        outcome = await adapter.replay(action)

        assert outcome.status == ReplayStatus.REJECTED_BY_POLICY
        assert len(emitted) == 0

    @pytest.mark.asyncio
    async def test_file_url_rejected(self) -> None:
        emitted: list[str] = []
        adapter = AppLaunchSurfaceAdapter(launch_emitter=emitted.append)
        action = _make_action(app_name="navegador", url="file:///etc/shadow")

        outcome = await adapter.replay(action)

        assert outcome.status == ReplayStatus.REJECTED_BY_POLICY
        assert len(emitted) == 0

    @pytest.mark.asyncio
    async def test_shell_injection_in_url_rejected(self) -> None:
        emitted: list[str] = []
        adapter = AppLaunchSurfaceAdapter(launch_emitter=emitted.append)
        action = _make_action(
            app_name="navegador", url="https://x.com; rm -rf /"
        )

        outcome = await adapter.replay(action)

        assert outcome.status == ReplayStatus.REJECTED_BY_POLICY
        assert len(emitted) == 0

    @pytest.mark.asyncio
    async def test_surface_kind_mismatch_rejected(self) -> None:
        adapter = AppLaunchSurfaceAdapter(launch_emitter=lambda _: None)
        action = _make_action(surface_kind=SurfaceKind.API_CALL)

        outcome = await adapter.replay(action)

        assert outcome.status == ReplayStatus.REJECTED_BY_POLICY

    @pytest.mark.asyncio
    async def test_missing_app_name_rejected(self) -> None:
        adapter = AppLaunchSurfaceAdapter(launch_emitter=lambda _: None)
        action = _make_action(app_name=None)

        outcome = await adapter.replay(action)

        assert outcome.status == ReplayStatus.REJECTED_BY_POLICY

    @pytest.mark.asyncio
    async def test_unknown_app_name_with_spaces_rejected(self) -> None:
        # Names with spaces don't match _SAFE_BINARY_RE and aren't in the alias
        # map, so they can't resolve → REJECTED_BY_POLICY.
        adapter = AppLaunchSurfaceAdapter(launch_emitter=lambda _: None)
        action = _make_action(app_name="not a real application")

        outcome = await adapter.replay(action)

        assert outcome.status == ReplayStatus.REJECTED_BY_POLICY

    @pytest.mark.asyncio
    async def test_no_emitter_returns_failed(self) -> None:
        adapter = AppLaunchSurfaceAdapter()  # no emitter
        action = _make_action(app_name="navegador")

        outcome = await adapter.replay(action)

        assert outcome.status == ReplayStatus.EXECUTED_FAILED

    @pytest.mark.asyncio
    async def test_set_launch_emitter_after_construction(self) -> None:
        emitted: list[str] = []
        adapter = AppLaunchSurfaceAdapter()
        adapter.set_launch_emitter(emitted.append)
        action = _make_action(app_name="navegador")

        outcome = await adapter.replay(action)

        assert outcome.status == ReplayStatus.EXECUTED_OK
        assert emitted == ["chromium-browser"]


# ---------------------------------------------------------------------------
# serialize_for_signing
# ---------------------------------------------------------------------------
class TestSerializeForSigning:
    def test_includes_url_field(self) -> None:
        adapter = AppLaunchSurfaceAdapter()
        action = _make_action(app_name="navegador", url="https://youtube.com")
        data = adapter.serialize_for_signing(action)
        import json
        parsed = json.loads(data)
        assert parsed["url"] == "https://youtube.com"
        assert parsed["app_name"] == "navegador"

    def test_url_defaults_to_empty_string_when_absent(self) -> None:
        adapter = AppLaunchSurfaceAdapter()
        action = _make_action(app_name="navegador")
        data = adapter.serialize_for_signing(action)
        import json
        parsed = json.loads(data)
        assert parsed["url"] == ""
