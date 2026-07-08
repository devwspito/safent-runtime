"""Unit tests for hermes.security.browser_launcher_client + launcher server.

Covers (per implementation brief):
  - SO_PEERCRED check: gid != hermes → reject.
  - session_name regex: ^exec-[a-z0-9]+$ — rejects bad input.
  - Property template is fixed/not caller-influenced.
  - Fail-closed: BrowserLauncherClient raises when no launcher.
  - No bare-argv fallback: AgentBrowserCli raises when jail=1 + launcher down.
  - hermes-runtime.service has NO NetworkNamespacePath (daemon not in browser netns).
"""

from __future__ import annotations

import asyncio
import json
import re
import struct
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]  # this repo (lumen-runtime), portable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hermes.security.browser_launcher_client import (
    BrowserLauncherClient,
    BrowserLauncherError,
    BrowserLauncherUnavailable,
)

pytestmark = pytest.mark.unit

# ── Regex validation (server-side logic mirrored in tests) ────────────────────

_SESSION_RE = re.compile(r"^exec-[a-z0-9]{1,64}$")

_VALID_SESSION_NAMES = [
    "exec-abc",
    "exec-abc123",
    "exec-0",
    "exec-z9",
]
_INVALID_SESSION_NAMES = [
    "",
    "exec-",
    "exec-ABC",           # uppercase not allowed
    "exec-abc!",          # special char
    "hermes-browser-abc", # wrong prefix
    "../etc/passwd",      # path traversal
    "exec-abc/../evil",   # path traversal
    "exec abc",           # space
    "exec-" + "a" * 300, # excessively long
]


class TestSessionNameRegex:
    """The server validates session_name format — callers cannot bypass."""

    @pytest.mark.parametrize("name", _VALID_SESSION_NAMES)
    def test_valid_session_names_match(self, name: str) -> None:
        assert _SESSION_RE.match(name), f"expected {name!r} to match"

    @pytest.mark.parametrize("name", _INVALID_SESSION_NAMES)
    def test_invalid_session_names_rejected(self, name: str) -> None:
        assert not _SESSION_RE.match(name), f"expected {name!r} to NOT match"


# ── BrowserLauncherClient unit tests ─────────────────────────────────────────

class TestBrowserLauncherClientSocketMissing:
    """Fail-closed: socket absent → BrowserLauncherUnavailable (no bare-argv fallback)."""

    @pytest.mark.asyncio
    async def test_raises_unavailable_when_socket_absent(self) -> None:
        client = BrowserLauncherClient(socket_path=Path("/nonexistent/socket.sock"))
        with pytest.raises(BrowserLauncherUnavailable):
            await client.launch(session_name="exec-abc", domains_whitelist=())

    @pytest.mark.asyncio
    async def test_does_not_swallow_error(self) -> None:
        client = BrowserLauncherClient(socket_path=Path("/nonexistent/socket.sock"))
        with pytest.raises(BrowserLauncherUnavailable, match="socket"):
            await client.launch(session_name="exec-abc")


class TestBrowserLauncherClientOkResponse:
    """Happy path: launcher returns ok=true → no exception."""

    @pytest.mark.asyncio
    async def test_launch_succeeds_on_ok_response(self) -> None:
        response = {"ok": True, "session_name": "exec-abc"}
        client = _client_with_mock_response(response)
        # Should not raise.
        await client.launch(session_name="exec-abc", domains_whitelist=("example.com",))

    @pytest.mark.asyncio
    async def test_launch_sends_only_session_and_domains(self) -> None:
        """Invariant: only session_name + domains_whitelist travel over the wire."""
        captured: dict = {}
        response = {"ok": True, "session_name": "exec-abc"}
        client = _client_capturing_request(response, captured)
        await client.launch(
            session_name="exec-abc",
            domains_whitelist=("example.com", "cdn.example.com"),
        )
        req = captured.get("request", {})
        # ONLY these two fields — no systemd-run -p flags must be present.
        assert set(req.keys()) == {"session_name", "domains_whitelist"}
        assert req["session_name"] == "exec-abc"
        assert set(req["domains_whitelist"]) == {"example.com", "cdn.example.com"}


class TestBrowserLauncherClientErrorResponse:
    """Launcher returns ok=False → BrowserLauncherError (fail-closed)."""

    @pytest.mark.asyncio
    async def test_raises_error_on_ok_false(self) -> None:
        response = {"ok": False, "error": "invalid session_name"}
        client = _client_with_mock_response(response)
        with pytest.raises(BrowserLauncherError, match="invalid session_name"):
            await client.launch(session_name="exec-abc")

    @pytest.mark.asyncio
    async def test_raises_error_on_unauthorized(self) -> None:
        response = {"ok": False, "error": "unauthorized"}
        client = _client_with_mock_response(response)
        with pytest.raises(BrowserLauncherError, match="unauthorized"):
            await client.launch(session_name="exec-abc")


# ── No bare-argv fallback invariant ──────────────────────────────────────────

class TestNoBareArgvFallback:
    """When jail=1 and launcher is unavailable, browser must NOT run unconfined."""

    @pytest.mark.asyncio
    async def test_agent_browser_cli_raises_not_runs_unconfined(self) -> None:
        """AgentBrowserCli must raise when launcher unavailable (jail=1)."""
        from hermes.browser.infrastructure.agent_browser_cli import (
            AgentBrowserCli,
            AgentBrowserCommandError,
        )

        cli = AgentBrowserCli(session_name="exec-abc", domains_whitelist=())
        cli._started = True
        # Inject a launcher client that always fails.
        failing_client = MagicMock()
        failing_client.launch = AsyncMock(
            side_effect=BrowserLauncherUnavailable("socket missing")
        )
        cli._launcher_client = failing_client

        with patch.dict("os.environ", {"HERMES_BROWSER_JAIL": "1"}), \
             patch("hermes.browser.infrastructure.agent_browser_cli._jail_enabled", return_value=True):
            # The fail-closed guard (2026-07-05 audit) refuses under jail BEFORE
            # even consulting the launcher — this adapter has no --cdp attach, so
            # any spawn would be UNCONFINED. Stronger than the old
            # launcher-unavailable check.
            with pytest.raises(AgentBrowserCommandError, match="UNCONFINED"):
                # _run fail-closes on the jail guard before _spawn_daemon_jailed.
                await cli._run(["open", "https://example.com"])

    @pytest.mark.asyncio
    async def test_agent_browser_cli_refuses_under_jail_even_with_working_launcher(
        self,
    ) -> None:
        """The fail-closed guard fires under jail EVEN when the launcher works.

        This is the actual hole closed (2026-07-05 security audit): the adapter
        runs `agent-browser --session <name>` with NO --cdp, so agent-browser
        spawns its OWN browser (ignoring the launcher's jailed Chromium) —
        UNCONFINED in the daemon's host netns. It must never reach the spawn.
        """
        from hermes.browser.infrastructure.agent_browser_cli import (
            AgentBrowserCli,
            AgentBrowserCommandError,
        )

        cli = AgentBrowserCli(session_name="exec-abc")
        cli._started = True
        working_client = MagicMock()
        working_client.launch = AsyncMock(return_value=None)  # launcher SUCCEEDS
        cli._launcher_client = working_client

        with patch.dict("os.environ", {"HERMES_BROWSER_JAIL": "1"}), \
             patch("hermes.browser.infrastructure.agent_browser_cli._jail_enabled", return_value=True), \
             patch("asyncio.create_subprocess_exec") as mock_exec:
            with pytest.raises(AgentBrowserCommandError, match="UNCONFINED"):
                await cli._run(["open", "https://example.com"])
            mock_exec.assert_not_called()  # never spawned
            working_client.launch.assert_not_awaited()  # guard fires first

    @pytest.mark.asyncio
    async def test_no_subprocess_called_when_launcher_fails(self) -> None:
        """subprocess.Popen / create_subprocess_exec must NOT be called as fallback."""
        from hermes.browser.infrastructure.agent_browser_cli import (
            AgentBrowserCli,
            AgentBrowserCommandError,
        )

        cli = AgentBrowserCli(session_name="exec-abc")
        cli._started = True
        failing_client = MagicMock()
        failing_client.launch = AsyncMock(
            side_effect=BrowserLauncherUnavailable("socket missing")
        )
        cli._launcher_client = failing_client

        with patch.dict("os.environ", {"HERMES_BROWSER_JAIL": "1"}), \
             patch("hermes.browser.infrastructure.agent_browser_cli._jail_enabled", return_value=True), \
             patch("asyncio.create_subprocess_exec") as mock_exec:
            with pytest.raises(AgentBrowserCommandError):
                await cli._run(["open", "https://example.com"])
            mock_exec.assert_not_called()


# ── Exec-session cap frozen at 1 (shared-netns bleed guard) ───────────────────

class TestExecSessionCapFrozen:
    """HERMES_MAX_EXEC_SESSIONS default MUST stay 1 until per-session netns.

    Above 1, exec browsers share one netns → cross-session CDP/RFB bleed at
    10.200.0.2 with no app auth (latent HIGH). cap=1 + the nft policy-drop are the
    sole gate; a silent bump to the default would re-open the gap.
    """

    def _launcher_src(self) -> str:
        launcher = (
            Path(__file__).resolve().parents[3]
            / "ops" / "agents-os-edition" / "scripts" / "hermes-browser-launcher"
        )
        return launcher.read_text(encoding="utf-8")

    def test_default_cap_is_one(self) -> None:
        src = self._launcher_src()
        assert "_MAX_EXEC_SESSIONS_DEFAULT = 1" in src, (
            "The concurrent exec-session cap default MUST be 1 until per-session "
            "netns lands — a shared netns above 1 opens cross-session CDP/RFB bleed."
        )

    def test_raising_cap_above_1_warns_loudly(self) -> None:
        src = self._launcher_src()
        assert "max_exec_sessions_above_1" in src, (
            "Raising HERMES_MAX_EXEC_SESSIONS above 1 must emit a loud, auditable "
            "SECURITY warning (never a silent gap re-opening)."
        )


# ── Daemon NOT in browser netns (architecture invariant) ──────────────────────

class TestDaemonNotInBrowserNetns:
    """hermes-runtime.service must have NO NetworkNamespacePath= directive.

    This is the predecessor 'sin modelo' bug: putting the daemon in the browser
    netns cuts off its route to vLLM (loopback + LAN). The daemon belongs in the
    host netns; only the browser scope goes into hermes-browser.
    """

    def test_runtime_service_has_no_network_namespace_path(self) -> None:
        service_path = (_REPO_ROOT / "ops/agents-os-edition/systemd/hermes-runtime.service")
        content = service_path.read_text()
        # Check that no [Service] directive line (non-comment) sets NetworkNamespacePath=.
        # Comments may mention the directive name for documentation purposes.
        directive_lines = [
            line for line in content.splitlines()
            if not line.strip().startswith("#") and "NetworkNamespacePath=" in line
        ]
        assert not directive_lines, (
            "hermes-runtime.service MUST NOT have a NetworkNamespacePath= directive — "
            "the daemon belongs in the host netns, not the browser netns. "
            "This was the predecessor 'sin modelo' bug. "
            f"Found directive lines: {directive_lines}"
        )

    def test_launcher_service_has_correct_cap_bounding_set(self) -> None:
        """Launcher has minimal caps: NET_ADMIN + SYS_ADMIN + SYS_RESOURCE only."""
        service_path = (_REPO_ROOT / "ops/agents-os-edition/systemd/hermes-browser-launcher.service")
        content = service_path.read_text()
        assert "CAP_NET_ADMIN" in content
        assert "CAP_SYS_ADMIN" in content
        assert "CAP_SYS_RESOURCE" in content

    def test_runtime_service_has_no_ambient_caps(self) -> None:
        """Daemon must have empty CapabilityBoundingSet."""
        service_path = (_REPO_ROOT / "ops/agents-os-edition/systemd/hermes-runtime.service")
        content = service_path.read_text()
        assert "CapabilityBoundingSet=" in content

    def test_runtime_service_has_browser_jail_env(self) -> None:
        """HERMES_BROWSER_JAIL=1 must be explicit in the unit (auditable)."""
        service_path = (_REPO_ROOT / "ops/agents-os-edition/systemd/hermes-runtime.service")
        content = service_path.read_text()
        assert "HERMES_BROWSER_JAIL=1" in content

    def test_shell_server_service_has_browser_jail_env(self) -> None:
        """HERMES_BROWSER_JAIL=1 must be explicit in the shell-server unit too."""
        service_path = (_REPO_ROOT / "ops/agents-os-edition/systemd/hermes-shell-server.service")
        content = service_path.read_text()
        assert "HERMES_BROWSER_JAIL=1" in content


# ── Property template is hardcoded (server-side, not caller-influenced) ───────

class TestPropertyTemplateIsHardcoded:
    """Verify the launcher script contains hardcoded -p= flags and no caller input."""

    def test_launcher_script_has_hardcoded_no_new_privileges(self) -> None:
        launcher_path = (_REPO_ROOT / "ops/agents-os-edition/scripts/hermes-browser-launcher")
        content = launcher_path.read_text()
        assert "NoNewPrivileges=yes" in content

    def test_launcher_script_has_hardcoded_netns(self) -> None:
        launcher_path = (_REPO_ROOT / "ops/agents-os-edition/scripts/hermes-browser-launcher")
        content = launcher_path.read_text()
        # The netns path is embedded in the _SCOPE_PROPERTIES constant or _NETNS_PATH.
        assert "/run/netns/hermes-browser" in content, (
            "Launcher must embed the hardcoded netns path — callers cannot change it."
        )

    def test_launcher_script_has_hardcoded_protect_system(self) -> None:
        launcher_path = (_REPO_ROOT / "ops/agents-os-edition/scripts/hermes-browser-launcher")
        content = launcher_path.read_text()
        assert "ProtectSystem=strict" in content

    def test_launcher_script_has_hardcoded_empty_cap_bounding_set(self) -> None:
        launcher_path = (_REPO_ROOT / "ops/agents-os-edition/scripts/hermes-browser-launcher")
        content = launcher_path.read_text()
        assert "CapabilityBoundingSet=" in content

    def test_launcher_script_validates_session_regex(self) -> None:
        """The launcher validates session_name server-side (caller cannot bypass)."""
        launcher_path = (_REPO_ROOT / "ops/agents-os-edition/scripts/hermes-browser-launcher")
        content = launcher_path.read_text()
        assert "exec-[a-z0-9]" in content

    def test_launcher_script_checks_peercred(self) -> None:
        """SO_PEERCRED is checked in the launcher (gid validation)."""
        launcher_path = (_REPO_ROOT / "ops/agents-os-edition/scripts/hermes-browser-launcher")
        content = launcher_path.read_text()
        assert "SO_PEERCRED" in content or "getsockopt" in content

    def test_jail_script_path_matches_code_constant(self) -> None:
        """Finding A: COPY target must be /usr/libexec/hermes/browser-jail (no hermes- prefix)."""
        # This repo's delivery Containerfile (ops/container/Containerfile) — the
        # sibling ops/agents-os-edition/containerfiles/Containerfile.base layout
        # belongs to the old hermes-runtime tree, not lumen-runtime.
        containerfile_path = (_REPO_ROOT / "ops/container/Containerfile")
        content = containerfile_path.read_text()
        # Must install as browser-jail (matches _JAIL_SCRIPT constant in code).
        assert "/usr/libexec/hermes/browser-jail" in content
        # Must NOT install under the old wrong name.
        assert "/usr/libexec/hermes/hermes-browser-jail" not in content


# ── Finding E: dead unit removed ──────────────────────────────────────────────

class TestDeadUnitRemoved:
    """hermes-netns-setup.service must not be in the active systemd directory."""

    def test_netns_setup_service_not_in_systemd_dir(self) -> None:
        active_path = (_REPO_ROOT / "ops/agents-os-edition/systemd/hermes-netns-setup.service")
        assert not active_path.exists(), (
            "hermes-netns-setup.service must be removed from the active systemd/ "
            "directory (it confines the wrong PID and references a deleted nft file). "
            "Move it to systemd.disabled/."
        )

    def test_netns_setup_in_disabled_dir(self) -> None:
        disabled_path = (_REPO_ROOT / "ops/agents-os-edition/systemd.disabled/hermes-netns-setup.service")
        assert disabled_path.exists(), (
            "hermes-netns-setup.service should be preserved in systemd.disabled/ "
            "for reference (not deleted, just disabled)."
        )


# ── Finding D: DNS via proxy ──────────────────────────────────────────────────

class TestDNSViaProxy:
    """Browser resolves DNS through proxy CONNECT, not direct :53."""

    def test_browser_ns_nft_has_no_direct_dns_rule(self) -> None:
        nft_path = (_REPO_ROOT / "ops/agents-os-edition/netns/browser-ns.nft")
        content = nft_path.read_text()
        # Direct DNS rules on port 53 must be removed (DNS goes via proxy CONNECT).
        # Presence of 'dport 53' in an accept rule would be wrong.
        lines_with_53 = [
            line for line in content.splitlines()
            if "dport 53" in line and "accept" in line
        ]
        assert not lines_with_53, (
            "browser-ns.nft must NOT allow direct DNS (:53) — "
            "DNS is resolved via proxy CONNECT (Finding D). "
            f"Found: {lines_with_53}"
        )

    def test_browser_host_nft_has_no_direct_dns_forward_rule(self) -> None:
        nft_path = (_REPO_ROOT / "ops/agents-os-edition/netns/browser-host.nft")
        content = nft_path.read_text()
        lines_with_53 = [
            line for line in content.splitlines()
            if "dport 53" in line and "accept" in line
        ]
        assert not lines_with_53, (
            "browser-host.nft must NOT forward direct DNS (:53) from the browser — "
            "DNS is resolved via proxy CONNECT (Finding D). "
            f"Found: {lines_with_53}"
        )


# ── Helpers ────────────────────────────────────────────────────────────────────

def _make_framed_response(payload: dict) -> bytes:
    body = json.dumps(payload).encode("utf-8")
    return struct.pack(">I", len(body)) + body


def _client_with_mock_response(response: dict) -> BrowserLauncherClient:
    """Return a BrowserLauncherClient whose _roundtrip returns response."""
    client = BrowserLauncherClient(socket_path=Path("/fake/socket.sock"))
    client._roundtrip = AsyncMock(return_value=response)  # type: ignore[method-assign]
    return client


def _client_capturing_request(response: dict, captured: dict) -> BrowserLauncherClient:
    """Return a client that captures the request payload before returning response."""

    async def _fake_roundtrip(req: dict) -> dict:
        captured["request"] = req
        return response

    client = BrowserLauncherClient(socket_path=Path("/fake/socket.sock"))
    client._roundtrip = _fake_roundtrip  # type: ignore[method-assign]
    return client
