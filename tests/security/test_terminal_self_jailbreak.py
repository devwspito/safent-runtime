"""Security regression tests — terminal self-jailbreak guard.

Verifies that _check_terminal_self_jailbreak blocks any terminal command that
would stop/disable/mask/kill/rm/mv a protected Hermes service or its filesystem
artefacts, while allowing read-only and non-protected mutations to pass.

Gate position in hook: Step 3 (after hardline, before command-guards).
Audit event: hermes.security_hook.pre.self_jailbreak_blocked
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def _check(command_str: str):
    """Invoke the function under test directly."""
    from hermes.runtime.security_hook import _check_terminal_self_jailbreak
    return _check_terminal_self_jailbreak(command_str)


# ---------------------------------------------------------------------------
# Commands that MUST be blocked
# ---------------------------------------------------------------------------


class TestBlockedCommands:
    """Every command in this class must return a non-None block message."""

    # -- systemctl mutations on protected services --

    def test_systemctl_stop_hermes_runtime(self) -> None:
        assert _check("systemctl stop hermes-runtime") is not None

    def test_systemctl_stop_with_service_suffix(self) -> None:
        assert _check("systemctl stop hermes-runtime.service") is not None

    def test_systemctl_disable_hermes_runtime(self) -> None:
        assert _check("systemctl disable hermes-runtime") is not None

    def test_systemctl_mask_hermes_runtime(self) -> None:
        assert _check("systemctl mask hermes-runtime") is not None

    def test_systemctl_kill_hermes_runtime(self) -> None:
        assert _check("systemctl kill hermes-runtime") is not None

    def test_systemctl_restart_hermes_runtime(self) -> None:
        assert _check("systemctl restart hermes-runtime") is not None

    def test_systemctl_stop_hermes_audit(self) -> None:
        assert _check("systemctl stop hermes-audit") is not None

    def test_systemctl_stop_hermes_consent(self) -> None:
        assert _check("systemctl stop hermes-consent") is not None

    def test_systemctl_stop_hermes_keygen(self) -> None:
        assert _check("systemctl stop hermes-keygen") is not None

    def test_systemctl_stop_hermes_shell_server(self) -> None:
        assert _check("systemctl stop hermes-shell-server") is not None

    def test_systemctl_disable_now_hermes_runtime(self) -> None:
        # --now is a flag; should still be detected
        assert _check("systemctl disable --now hermes-runtime") is not None

    def test_systemctl_stop_uppercase_alias(self) -> None:
        assert _check("systemctl stop Hermes-Runtime") is not None

    def test_systemctl_stop_full_path_binary(self) -> None:
        assert _check("/usr/bin/systemctl stop hermes-runtime") is not None

    # -- shell-wrapped systemctl --

    def test_bash_c_systemctl_disable_egress(self) -> None:
        assert _check('bash -c "systemctl disable hermes-egress-proxy"') is not None

    def test_bash_c_systemctl_stop_hermes_runtime(self) -> None:
        assert _check("bash -c 'systemctl stop hermes-runtime'") is not None

    def test_sh_c_systemctl_mask_audit(self) -> None:
        assert _check("sh -c 'systemctl mask hermes-audit'") is not None

    def test_full_path_bash_c_stop(self) -> None:
        assert _check('/usr/bin/bash -c "systemctl stop hermes-runtime"') is not None

    # -- pkill / kill / killall on hermes processes --

    def test_pkill_f_hermes_runtime(self) -> None:
        assert _check("pkill -f hermes-runtime") is not None

    def test_killall_hermes_runtime(self) -> None:
        assert _check("killall hermes-runtime") is not None

    def test_pkill_hermes_audit(self) -> None:
        assert _check("pkill hermes-audit") is not None

    def test_kill_by_process_name_hermes(self) -> None:
        assert _check("kill hermes") is not None

    # -- rm / mv of protected unit files --

    def test_rm_unit_file(self) -> None:
        assert _check("rm /usr/lib/systemd/system/hermes-runtime.service") is not None

    def test_rm_rf_unit_dir(self) -> None:
        assert _check("rm -rf /usr/lib/systemd/system/hermes-runtime.service") is not None

    def test_mv_unit_file(self) -> None:
        assert _check(
            "mv /usr/lib/systemd/system/hermes-runtime.service /tmp/hidden.service"
        ) is not None

    def test_rm_kernel_binary(self) -> None:
        assert _check("rm /usr/libexec/hermes/runtime-daemon") is not None

    def test_chattr_immutable_remove_on_unit(self) -> None:
        assert _check("chattr -i /usr/lib/systemd/system/hermes-runtime.service") is not None

    def test_rm_hermes_var_lib(self) -> None:
        assert _check("rm -rf /var/lib/hermes/") is not None

    # -- service stop shorthand --

    def test_service_hermes_runtime_stop(self) -> None:
        assert _check("service hermes-runtime stop") is not None


# ---------------------------------------------------------------------------
# Commands that MUST be allowed (read-only or non-protected)
# ---------------------------------------------------------------------------


class TestAllowedCommands:
    """Every command in this class must return None (no block)."""

    # -- read-only systemctl verbs on protected services --

    def test_systemctl_status_hermes_runtime(self) -> None:
        assert _check("systemctl status hermes-runtime") is None

    def test_systemctl_is_active_hermes_runtime(self) -> None:
        assert _check("systemctl is-active hermes-runtime") is None

    def test_systemctl_show_hermes_runtime(self) -> None:
        assert _check("systemctl show hermes-runtime") is None

    def test_systemctl_list_units(self) -> None:
        assert _check("systemctl list-units") is None

    def test_systemctl_cat_hermes_runtime(self) -> None:
        assert _check("systemctl cat hermes-runtime") is None

    # -- mutations on NON-protected services --

    def test_systemctl_restart_cups(self) -> None:
        assert _check("systemctl restart cups") is None

    def test_systemctl_stop_nginx(self) -> None:
        assert _check("systemctl stop nginx") is None

    def test_systemctl_disable_sshd(self) -> None:
        assert _check("systemctl disable sshd") is None

    def test_pkill_nginx(self) -> None:
        assert _check("pkill nginx") is None

    def test_kill_numeric_pid(self) -> None:
        # Numeric PID — no service name to match
        assert _check("kill -9 1234") is None

    # -- filesystem operations on non-protected paths --

    def test_ls_systemd_dir(self) -> None:
        assert _check("ls /usr/lib/systemd/system/") is None

    def test_rm_non_hermes_service(self) -> None:
        assert _check("rm /usr/lib/systemd/system/nginx.service") is None

    def test_rm_tmp_file(self) -> None:
        assert _check("rm /tmp/somefile.txt") is None

    # -- benign commands --

    def test_cat_file(self) -> None:
        assert _check("cat /etc/hosts") is None

    def test_echo_hello(self) -> None:
        assert _check("echo hello") is None

    def test_empty_string(self) -> None:
        assert _check("") is None


# ---------------------------------------------------------------------------
# Integration: hook factory wires the new step correctly
# ---------------------------------------------------------------------------


class TestHookIntegration:
    """The pre_tool_call hook returned by make_pre_tool_call_hook blocks
    self-jailbreak commands at Step 3 before reaching command-guards or
    denylist."""

    def _make_hook(self):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        agent_state = MagicMock()
        agent_state.is_paused = AsyncMock(return_value=False)

        loop = asyncio.new_event_loop()

        broker = MagicMock()
        broker._os_native_dispatcher = None  # skip denylist check

        from hermes.runtime.security_hook import make_pre_tool_call_hook

        hook = make_pre_tool_call_hook(
            agent_state=agent_state,
            engine_loop=loop,
            broker=broker,
        )
        return hook, loop

    def _run_hook(self, hook, loop, tool_name: str, command: str):
        from unittest.mock import patch

        # Patch kill-switch/hardline/command-guards to no-op so the test isolates
        # the self-jailbreak step (Step 3 of the hook).
        with patch(
            "hermes.runtime.security_hook._check_kill_switch", return_value=False
        ), patch(
            "hermes.runtime.security_hook._check_hardline_native", return_value=None
        ), patch(
            "hermes.runtime.security_hook._check_command_guards_native", return_value=None
        ):
            return hook(tool_name=tool_name, args={"command": command})

    def test_hook_blocks_systemctl_stop_hermes_runtime(self) -> None:
        from hermes.runtime.security_hook import _SELF_JAILBREAK_MSG

        hook, loop = self._make_hook()
        result = self._run_hook(hook, loop, "terminal", "systemctl stop hermes-runtime")
        assert result is not None
        assert result.get("action") == "block"
        # The block must be the self-jailbreak guard's message (Step 3), not any
        # other block path — this asserts the command was routed to and rejected by
        # the self-jailbreak guard specifically. The message evolved (commit
        # fd665d9) from a terse "anti-autopirateo" note to an explicit, non-appealable
        # hard-block that instructs the model to stop and stay honest; assert against
        # the source constant so the routing invariant holds without pinning wording.
        message = result.get("message", "")
        assert message == _SELF_JAILBREAK_MSG
        # Security intent preserved: an inapelable (non-appealable) hard security block.
        assert "inapelable" in message
        assert "kernel de seguridad" in message

    def test_hook_blocks_pkill_hermes_runtime(self) -> None:
        hook, loop = self._make_hook()
        result = self._run_hook(hook, loop, "run_command", "pkill -f hermes-runtime")
        assert result is not None
        assert result.get("action") == "block"

    def test_hook_allows_systemctl_status_hermes_runtime(self) -> None:
        hook, loop = self._make_hook()
        result = self._run_hook(hook, loop, "terminal", "systemctl status hermes-runtime")
        assert result is None

    def test_hook_allows_systemctl_stop_nginx(self) -> None:
        hook, loop = self._make_hook()
        result = self._run_hook(hook, loop, "terminal", "systemctl stop nginx")
        assert result is None
