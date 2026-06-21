"""Security regression tests — kernel confinement for terminal + filesystem adapters.

Spec 014 blockers B-1 and B-2:
  B-1: TerminalSurfaceAdapter wraps commands in systemd-run scope.
       Denylist remains as defense-in-depth, not as the gate.
  B-2: FilesystemSurfaceAdapter opens files via openat2/O_NOFOLLOW,
       eliminating TOCTOU window and symlink escape.

These tests verify behavior WITHOUT requiring a live systemd or root
privileges. Kernel enforcement is verified via unit-level mocking plus
a real symlink-escape test (B-2) that uses the actual openat2 path.

Tests that require a real VM (confinement against master.key or WAN)
are marked ``requires_vm`` and excluded in CI.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

pytestmark = pytest.mark.security


# ===========================================================================
# B-1 — TerminalSurfaceAdapter: systemd-run scope wrapping
# ===========================================================================


class TestTerminalScopeWrap:
    """Verify that _run delegates to systemd-run when scope is enabled."""

    def test_build_scoped_argv_contains_hardening_properties(self) -> None:
        """_build_scoped_argv produces a systemd-run argv with hardening props."""
        from hermes.agents_os.infrastructure.terminal_surface_adapter import (
            _build_scoped_argv,
        )

        argv = _build_scoped_argv(["echo", "hello"], timeout_s=30.0, workspace=None)

        assert argv[0] == "systemd-run"
        joined = " ".join(argv)
        assert "NoNewPrivileges=yes" in joined
        assert "CapabilityBoundingSet=" in joined
        assert "IPAddressDeny=any" in joined
        assert "ProtectSystem=strict" in joined
        assert "ProtectHome=yes" in joined
        assert "PrivateTmp=yes" in joined
        assert "RestrictNamespaces=yes" in joined
        assert "MemoryMax=512M" in joined
        assert "CPUQuota=50%" in joined
        # Command must appear after '--'
        sep_idx = argv.index("--")
        assert argv[sep_idx + 1 :] == ["echo", "hello"]

    def test_build_scoped_argv_with_workspace_adds_readwritepaths(self) -> None:
        from hermes.agents_os.infrastructure.terminal_surface_adapter import (
            _build_scoped_argv,
        )

        argv = _build_scoped_argv(
            ["touch", "x.txt"],
            timeout_s=10.0,
            workspace="/var/lib/hermes/terminal-workspace",
        )
        joined = " ".join(argv)
        assert "ReadWritePaths=/var/lib/hermes/terminal-workspace" in joined

    def test_scope_disabled_does_not_call_systemd_run(self) -> None:
        """HERMES_TERMINAL_SCOPE=0 skips scope wrapping (CI mode)."""
        from hermes.agents_os.infrastructure.terminal_surface_adapter import (
            _scope_enabled,
        )

        with patch.dict("os.environ", {"HERMES_TERMINAL_SCOPE": "0"}):
            assert _scope_enabled() is False

    def test_scope_enabled_by_default(self) -> None:
        from hermes.agents_os.infrastructure.terminal_surface_adapter import (
            _scope_enabled,
        )

        env = {k: v for k, v in os.environ.items() if k != "HERMES_TERMINAL_SCOPE"}
        with patch.dict("os.environ", env, clear=True):
            assert _scope_enabled() is True

    @pytest.mark.asyncio
    async def test_scope_enabled_but_systemd_run_missing_raises_fail_closed(
        self,
    ) -> None:
        """When scope is enabled but systemd-run is absent, deny the execution."""
        from hermes.agents_os.infrastructure.terminal_surface_adapter import (
            TerminalConfinementUnavailableError,
            TerminalSurfaceAdapter,
        )

        adapter = TerminalSurfaceAdapter()
        with (
            patch.dict("os.environ", {"HERMES_TERMINAL_SCOPE": "1"}),
            patch(
                "hermes.agents_os.infrastructure.terminal_surface_adapter._systemd_run_path",
                return_value=None,
            ),
            pytest.raises(TerminalConfinementUnavailableError),
        ):
            await adapter._run(["echo", "hi"], "/tmp")

    @pytest.mark.asyncio
    async def test_replay_returns_rejected_when_scope_unavailable(self) -> None:
        """replay() returns REJECTED_BY_POLICY when confinement unavailable."""
        from hermes.agents_os.domain.ports.surface_adapter_port import ReplayStatus
        from hermes.agents_os.infrastructure.terminal_surface_adapter import (
            TerminalConfinementUnavailableError,
            TerminalSurfaceAdapter,
        )

        adapter = TerminalSurfaceAdapter()
        with (
            patch.dict("os.environ", {"HERMES_TERMINAL_SCOPE": "0"}),
        ):
            captured = await adapter.capture(
                intent_desc="test",
                params={"argv": ["echo", "hi"], "cwd": "/tmp"},
                tenant_id=uuid4(),
                human_operator_id=uuid4(),
            )

        with (
            patch.dict("os.environ", {"HERMES_TERMINAL_SCOPE": "1"}),
            patch(
                "hermes.agents_os.infrastructure.terminal_surface_adapter._systemd_run_path",
                return_value=None,
            ),
        ):
            outcome = await adapter.replay(captured)

        assert outcome.status is ReplayStatus.REJECTED_BY_POLICY
        assert "confinement" in (outcome.error or "").lower()

    @pytest.mark.asyncio
    async def test_scope_disabled_executes_command_directly(self) -> None:
        """HERMES_TERMINAL_SCOPE=0 executes the raw command (CI path)."""
        from hermes.agents_os.infrastructure.terminal_surface_adapter import (
            TerminalSurfaceAdapter,
        )

        adapter = TerminalSurfaceAdapter()
        with patch.dict("os.environ", {"HERMES_TERMINAL_SCOPE": "0"}):
            exit_code, stdout, _, _ = await adapter._run(["echo", "ok-ci"], "/tmp")

        assert exit_code == 0
        assert "ok-ci" in stdout

    def test_denylist_still_rejects_before_scope(self) -> None:
        """Denylist check fires before the scope — defense in depth."""
        from hermes.agents_os.infrastructure.terminal_surface_adapter import (
            TerminalSurfaceAdapter,
        )

        adapter = TerminalSurfaceAdapter()
        with pytest.raises(ValueError, match="denylist"):
            adapter._reject_if_denylisted(["rm", "-rf", "/"])

    @pytest.mark.asyncio
    async def test_capture_uses_scope_and_records_exit_code(self) -> None:
        """End-to-end capture with mocked systemd-run records the real exit code."""
        from hermes.agents_os.domain.surface_kind import SurfaceKind
        from hermes.agents_os.infrastructure.terminal_surface_adapter import (
            TerminalSurfaceAdapter,
        )

        adapter = TerminalSurfaceAdapter()

        async def _fake_run_raw(argv: list, cwd: str):
            # Verify systemd-run is the first element
            assert argv[0].endswith("systemd-run")
            assert "NoNewPrivileges=yes" in " ".join(argv)
            # Simulate successful echo
            return (0, "hello\n", "", 5)

        with (
            patch.dict("os.environ", {"HERMES_TERMINAL_SCOPE": "1"}),
            patch(
                "hermes.agents_os.infrastructure.terminal_surface_adapter._systemd_run_path",
                return_value="/usr/bin/systemd-run",
            ),
            patch.object(adapter, "_run_raw", _fake_run_raw),
        ):
            action = await adapter.capture(
                intent_desc="greet",
                params={"argv": ["echo", "hello"], "cwd": "/tmp"},
                tenant_id=uuid4(),
                human_operator_id=uuid4(),
            )

        assert action.surface_kind == SurfaceKind.TERMINAL
        assert action.payload["exit_code"] == 0
        assert "hello" in action.payload["stdout_redacted"]


# ===========================================================================
# B-2 — FilesystemSurfaceAdapter: openat2 / O_NOFOLLOW symlink prevention
# ===========================================================================


class TestFilesystemSymlinkEscape:
    """Verify symlink escape is rejected at the kernel level (TOCTOU-safe)."""

    def test_symlink_escape_read_is_rejected(self, tmp_path: Path) -> None:
        """A symlink pointing outside the workspace is rejected on open, not path check."""
        from hermes.agents_os.infrastructure.filesystem_surface_adapter import (
            FilesystemSurfaceAdapter,
        )

        workspace = tmp_path / "workspace"
        workspace.mkdir()
        sensitive = tmp_path / "secret.txt"
        sensitive.write_text("VERY SECRET")

        # Create a symlink inside the workspace pointing outside.
        link = workspace / "escape_link"
        link.symlink_to(sensitive)

        adapter = FilesystemSurfaceAdapter(allowed_prefixes=(str(workspace),))

        import asyncio  # noqa: PLC0415

        with pytest.raises((PermissionError, OSError)):
            asyncio.run(
                adapter._execute(
                    "read_file",
                    str(link),
                    {"op": "read_file", "path": str(link)},
                    workspace=str(workspace),
                )
            )

    def test_symlink_escape_write_is_rejected(self, tmp_path: Path) -> None:
        """A symlink write attempt (e.g. writing /etc/shadow via symlink) is rejected."""
        from hermes.agents_os.infrastructure.filesystem_surface_adapter import (
            FilesystemSurfaceAdapter,
        )

        workspace = tmp_path / "ws"
        workspace.mkdir()
        target_outside = tmp_path / "outside.txt"

        link = workspace / "tricky"
        link.symlink_to(target_outside)

        adapter = FilesystemSurfaceAdapter(allowed_prefixes=(str(workspace),))

        import asyncio  # noqa: PLC0415

        with pytest.raises((PermissionError, OSError)):
            asyncio.run(
                adapter._execute(
                    "write_file",
                    str(link),
                    {"op": "write_file", "path": str(link), "content": "pwned"},
                    workspace=str(workspace),
                )
            )

    def test_symlink_escape_delete_is_rejected(self, tmp_path: Path) -> None:
        """delete_file via symlink outside workspace is rejected."""
        from hermes.agents_os.infrastructure.filesystem_surface_adapter import (
            FilesystemSurfaceAdapter,
        )

        workspace = tmp_path / "ws"
        workspace.mkdir()
        victim = tmp_path / "victim.txt"
        victim.write_text("important")

        link = workspace / "del_link"
        link.symlink_to(victim)

        adapter = FilesystemSurfaceAdapter(allowed_prefixes=(str(workspace),))

        import asyncio  # noqa: PLC0415

        with pytest.raises((PermissionError, OSError)):
            asyncio.run(
                adapter._execute(
                    "delete_file",
                    str(link),
                    {"op": "delete_file", "path": str(link)},
                    workspace=str(workspace),
                )
            )
        # Victim must not have been deleted.
        assert victim.exists(), "victim file must not be deleted via symlink"

    def test_path_escape_via_dotdot_rejected(self, tmp_path: Path) -> None:
        """resolve_to_workspace_relative rejects ../escape patterns."""
        from hermes.agents_os.infrastructure.filesystem_surface_adapter import (
            WorkspaceEscapeError,
            _resolve_to_workspace_relative,
        )

        workspace = str(tmp_path / "ws")
        Path(workspace).mkdir()

        # ../../etc/shadow-style escape
        escape_path = str(tmp_path / "ws" / ".." / ".." / "etc" / "shadow")

        with pytest.raises((WorkspaceEscapeError, PermissionError)):
            _resolve_to_workspace_relative(escape_path, workspace)

    def test_legitimate_path_within_workspace_accepted(self, tmp_path: Path) -> None:
        """A real file inside the workspace opens correctly."""
        from hermes.agents_os.infrastructure.filesystem_surface_adapter import (
            FilesystemSurfaceAdapter,
        )

        workspace = tmp_path / "ws"
        workspace.mkdir()
        target = workspace / "data.txt"
        target.write_text("hello")

        adapter = FilesystemSurfaceAdapter(allowed_prefixes=(str(workspace),))

        import asyncio  # noqa: PLC0415

        result = asyncio.run(
            adapter._execute(
                "read_file",
                str(target),
                {"op": "read_file", "path": str(target)},
                workspace=str(workspace),
            )
        )
        assert result["text"] == "hello"

    def test_replay_rejects_symlink_via_outcome(self, tmp_path: Path) -> None:
        """replay() translates a symlink PermissionError to REJECTED_BY_POLICY."""
        from hermes.agents_os.domain.ports.surface_adapter_port import (
            CapturedAction,
            ReplayStatus,
        )
        from hermes.agents_os.domain.surface_kind import SurfaceKind
        from hermes.agents_os.infrastructure.filesystem_surface_adapter import (
            FilesystemSurfaceAdapter,
        )

        workspace = tmp_path / "ws"
        workspace.mkdir()
        victim = tmp_path / "victim.txt"
        victim.write_text("secret")

        link = workspace / "link_escape"
        link.symlink_to(victim)

        adapter = FilesystemSurfaceAdapter(allowed_prefixes=(str(workspace),))

        action = CapturedAction(
            action_id=uuid4(),
            surface_kind=SurfaceKind.FILESYSTEM,
            intent_desc="read secret via symlink",
            payload={"op": "read_file", "path": str(link)},
            tenant_id=uuid4(),
            human_operator_id=uuid4(),
        )

        import asyncio  # noqa: PLC0415

        outcome = asyncio.run(adapter.replay(action))
        assert outcome.status in (
            ReplayStatus.REJECTED_BY_POLICY,
            ReplayStatus.EXECUTED_FAILED,
        ), f"Expected policy rejection or failure, got {outcome.status}: {outcome.error}"


class TestFilesystemPathAllowlistStillEnforced:
    """Path allowlist (outer gate) continues working alongside openat2 (inner gate)."""

    def test_path_outside_allowlist_rejected_on_capture(
        self, tmp_path: Path
    ) -> None:
        from hermes.agents_os.infrastructure.filesystem_surface_adapter import (
            FilesystemSurfaceAdapter,
        )

        outside = tmp_path / "denied.txt"
        outside.write_text("nope")
        adapter = FilesystemSurfaceAdapter(allowed_prefixes=("/usr/share/",))

        import asyncio  # noqa: PLC0415

        with pytest.raises(PermissionError, match="allowlist|escapes"):
            asyncio.run(
                adapter.capture(
                    intent_desc="bad read",
                    params={"op": "read_file", "path": str(outside)},
                    tenant_id=uuid4(),
                    human_operator_id=uuid4(),
                )
            )

    def test_empty_allowlist_rejected_at_construction(self) -> None:
        from hermes.agents_os.infrastructure.filesystem_surface_adapter import (
            FilesystemSurfaceAdapter,
        )

        with pytest.raises(ValueError, match="fail-closed"):
            FilesystemSurfaceAdapter(allowed_prefixes=())


class TestOpenat2Availability:
    """openat2 probe and fallback path."""

    def test_openat2_available_is_bool(self) -> None:
        from hermes.agents_os.infrastructure.filesystem_surface_adapter import (
            _OPENAT2_OK,
        )

        assert isinstance(_OPENAT2_OK, bool)

    def test_open_beneath_works_on_regular_file(self, tmp_path: Path) -> None:
        """_open_beneath opens a regular file without raising."""
        from hermes.agents_os.infrastructure.filesystem_surface_adapter import (
            _O_RDONLY,
            _open_base,
            _open_beneath,
        )

        target = tmp_path / "test.txt"
        target.write_text("data")

        base_fd = _open_base(str(tmp_path))
        try:
            fd = _open_beneath(base_fd, b"test.txt", _O_RDONLY)
            assert fd >= 0
            os.close(fd)
        finally:
            os.close(base_fd)

    def test_open_beneath_rejects_symlink(self, tmp_path: Path) -> None:
        """_open_beneath raises OSError (ELOOP or ENOENT) for symlinks."""
        from hermes.agents_os.infrastructure.filesystem_surface_adapter import (
            _O_RDONLY,
            _open_base,
            _open_beneath,
        )

        target = tmp_path / "real.txt"
        target.write_text("real")
        link = tmp_path / "link.txt"
        link.symlink_to(target)

        base_fd = _open_base(str(tmp_path))
        try:
            with pytest.raises(OSError):
                _open_beneath(base_fd, b"link.txt", _O_RDONLY)
        finally:
            os.close(base_fd)
