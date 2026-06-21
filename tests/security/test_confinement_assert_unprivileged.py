"""Regression tests — _assert_confinement_active unprivileged rewrite.

Verifies that EVERY check inside _assert_confinement_active works as
User=hermes with CapabilityBoundingSet= (no capabilities).

Concretely:
  - No `nft`, `ip netns exec`, or any other privileged binary is invoked.
  - All checks pass/fail based only on: systemctl is-active output, file-stat,
    file-read (/etc /run /sys /proc), and environment variables.

Tests are grouped per check function so regressions are easy to pinpoint.
No live systemd, no live netns, no root required — pure unit tests using
monkeypatching of subprocess.run and filesystem paths.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

pytestmark = pytest.mark.security


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_completed_process(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    cp = MagicMock(spec=subprocess.CompletedProcess)
    cp.stdout = stdout
    cp.stderr = ""
    cp.returncode = returncode
    return cp


# ---------------------------------------------------------------------------
# _check_systemctl_unit_active
# ---------------------------------------------------------------------------

class TestCheckSystemctlUnitActive:
    """_check_systemctl_unit_active only calls `/usr/bin/systemctl is-active <unit>`."""

    def test_active_unit_appends_no_failure(self) -> None:
        from hermes.runtime.__main__ import _check_systemctl_unit_active

        failures: list[str] = []
        with patch("subprocess.run", return_value=_make_completed_process("active\n")) as mock_run:
            _check_systemctl_unit_active("hermes-browser-netns.service", "netns", failures)

        assert failures == []
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "/usr/bin/systemctl"
        assert cmd[1] == "is-active"
        assert "hermes-browser-netns.service" in cmd
        # Confirm no nft / ip netns exec / ip link anywhere
        assert not any(x in ("nft", "ip") for x in cmd)

    def test_inactive_unit_appends_failure(self) -> None:
        from hermes.runtime.__main__ import _check_systemctl_unit_active

        failures: list[str] = []
        with patch("subprocess.run", return_value=_make_completed_process("inactive\n")):
            _check_systemctl_unit_active("hermes-browser-netns.service", "netns", failures)

        assert len(failures) == 1
        assert "inactive" in failures[0]
        assert "hermes-browser-netns.service" in failures[0]

    def test_failed_unit_appends_failure(self) -> None:
        from hermes.runtime.__main__ import _check_systemctl_unit_active

        failures: list[str] = []
        with patch("subprocess.run", return_value=_make_completed_process("failed\n", returncode=3)):
            _check_systemctl_unit_active("hermes-browser-netns.service", "netns", failures)

        assert len(failures) == 1
        assert "failed" in failures[0]

    def test_timeout_appends_failure(self) -> None:
        from hermes.runtime.__main__ import _check_systemctl_unit_active

        failures: list[str] = []
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="systemctl", timeout=5)):
            _check_systemctl_unit_active("hermes-browser-netns.service", "netns", failures)

        assert len(failures) == 1
        assert "check_error" in failures[0]

    def test_file_not_found_appends_failure(self) -> None:
        from hermes.runtime.__main__ import _check_systemctl_unit_active

        failures: list[str] = []
        with patch("subprocess.run", side_effect=FileNotFoundError):
            _check_systemctl_unit_active("hermes-browser-netns.service", "netns", failures)

        assert len(failures) == 1
        assert "check_error" in failures[0]

    def test_timeout_is_5_seconds(self) -> None:
        from hermes.runtime.__main__ import _check_systemctl_unit_active

        failures: list[str] = []
        with patch("subprocess.run", return_value=_make_completed_process("active\n")) as mock_run:
            _check_systemctl_unit_active("hermes-egress-proxy.service", "egress_proxy", failures)

        _, kwargs = mock_run.call_args
        assert kwargs.get("timeout") == 5.0


# ---------------------------------------------------------------------------
# _check_netns_path_exists
# ---------------------------------------------------------------------------

class TestCheckNetnsPathExists:
    """Verifies /run/netns/hermes-browser presence via Path.exists — no caps."""

    def test_path_present_appends_no_failure(self, tmp_path: Path) -> None:
        from hermes.runtime.__main__ import _check_netns_path_exists

        netns = tmp_path / "hermes-browser"
        netns.touch()
        failures: list[str] = []
        with patch("hermes.runtime.__main__.Path") as _mock_path:
            # Only intercept the /run/netns path, let others through.
            real_path = Path
            def _path_side_effect(p):
                if str(p) == "/run/netns/hermes-browser":
                    return netns
                return real_path(p)
            _mock_path.side_effect = _path_side_effect
            # Use real implementation directly with monkeypatched constant
            import hermes.runtime.__main__ as m
            orig = m.Path
            # Patch at the module level via the constant usage inside the function
        # Simpler: call with direct path monkeypatching
        with patch("hermes.runtime.__main__.Path", side_effect=lambda p: Path(str(p).replace("/run/netns/hermes-browser", str(netns)))):
            _check_netns_path_exists(failures)
        assert failures == []

    def test_path_absent_appends_failure(self, tmp_path: Path) -> None:
        from hermes.runtime.__main__ import _check_netns_path_exists

        absent = tmp_path / "no-such-netns"
        failures: list[str] = []
        with patch("hermes.runtime.__main__.Path", side_effect=lambda p: Path(str(p).replace("/run/netns/hermes-browser", str(absent)))):
            _check_netns_path_exists(failures)

        assert len(failures) == 1
        assert "netns_path_absent" in failures[0]


# ---------------------------------------------------------------------------
# _check_nft_rule_files_baked
# ---------------------------------------------------------------------------

class TestCheckNftRuleFilesBaked:
    """Verifies /etc/nftables baked-in rule files via read — no caps, no subprocess."""

    def test_both_files_present_and_non_empty_appends_no_failure(self, tmp_path: Path) -> None:
        from hermes.runtime.__main__ import _check_nft_rule_files_baked

        host_nft = tmp_path / "browser-host.nft"
        ns_nft = tmp_path / "browser-ns.nft"
        host_nft.write_text("table inet hermes_browser_egress { chain forward { policy drop; } }")
        ns_nft.write_text("table inet hermes_browser_local { chain output { policy drop; } }")

        failures: list[str] = []
        with (
            patch("hermes.runtime.__main__._NFT_HOST_RULE_FILE", host_nft),
            patch("hermes.runtime.__main__._NFT_NS_RULE_FILE", ns_nft),
        ):
            _check_nft_rule_files_baked(failures)

        assert failures == []

    def test_host_file_absent_appends_failure(self, tmp_path: Path) -> None:
        from hermes.runtime.__main__ import _check_nft_rule_files_baked

        absent = tmp_path / "nonexistent.nft"
        present = tmp_path / "browser-ns.nft"
        present.write_text("table inet hermes_browser_local { chain output { policy drop; } }")

        failures: list[str] = []
        with (
            patch("hermes.runtime.__main__._NFT_HOST_RULE_FILE", absent),
            patch("hermes.runtime.__main__._NFT_NS_RULE_FILE", present),
        ):
            _check_nft_rule_files_baked(failures)

        assert len(failures) == 1
        assert "nft_rule_file_absent" in failures[0]

    def test_empty_file_appends_failure(self, tmp_path: Path) -> None:
        from hermes.runtime.__main__ import _check_nft_rule_files_baked

        host_nft = tmp_path / "browser-host.nft"
        ns_nft = tmp_path / "browser-ns.nft"
        host_nft.write_text("")
        ns_nft.write_text("table inet hermes_browser_local {}")

        failures: list[str] = []
        with (
            patch("hermes.runtime.__main__._NFT_HOST_RULE_FILE", host_nft),
            patch("hermes.runtime.__main__._NFT_NS_RULE_FILE", ns_nft),
        ):
            _check_nft_rule_files_baked(failures)

        assert len(failures) == 1
        assert "nft_rule_file_empty" in failures[0]

    def test_no_nft_subprocess_is_ever_called(self, tmp_path: Path) -> None:
        """Critical regression: confirm no subprocess call to nft or ip is made."""
        from hermes.runtime.__main__ import _check_nft_rule_files_baked

        host_nft = tmp_path / "browser-host.nft"
        ns_nft = tmp_path / "browser-ns.nft"
        host_nft.write_text("table inet hermes_browser_egress {}")
        ns_nft.write_text("table inet hermes_browser_local {}")

        with (
            patch("hermes.runtime.__main__._NFT_HOST_RULE_FILE", host_nft),
            patch("hermes.runtime.__main__._NFT_NS_RULE_FILE", ns_nft),
            patch("subprocess.run") as mock_run,
        ):
            _check_nft_rule_files_baked([])

        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# _check_netns_enforcement (integration of the three sub-checks)
# ---------------------------------------------------------------------------

class TestCheckNetnsEnforcement:
    """Integration check: _check_netns_enforcement must never call nft or ip netns exec."""

    def test_all_passing_calls_only_systemctl(self, tmp_path: Path) -> None:
        from hermes.runtime.__main__ import _check_netns_enforcement

        host_nft = tmp_path / "browser-host.nft"
        ns_nft = tmp_path / "browser-ns.nft"
        host_nft.write_text("table inet hermes_browser_egress { chain forward { policy drop; } }")
        ns_nft.write_text("table inet hermes_browser_local { chain output { policy drop; } }")
        netns_path = tmp_path / "hermes-browser"
        netns_path.touch()

        failures: list[str] = []
        with (
            patch("hermes.runtime.__main__._NFT_HOST_RULE_FILE", host_nft),
            patch("hermes.runtime.__main__._NFT_NS_RULE_FILE", ns_nft),
            patch("subprocess.run", return_value=_make_completed_process("active\n")) as mock_run,
            patch("hermes.runtime.__main__.Path", side_effect=lambda p: Path(str(p).replace("/run/netns/hermes-browser", str(netns_path)))),
        ):
            _check_netns_enforcement(failures)

        assert failures == []
        # Only systemctl should be called — never nft or ip
        for call in mock_run.call_args_list:
            cmd = call[0][0]
            assert "nft" not in cmd, f"nft must not be called: {cmd}"
            assert "ip" not in cmd, f"ip must not be called: {cmd}"

    def test_service_inactive_propagates_failure(self, tmp_path: Path) -> None:
        from hermes.runtime.__main__ import _check_netns_enforcement

        host_nft = tmp_path / "host.nft"
        ns_nft = tmp_path / "ns.nft"
        host_nft.write_text("rules")
        ns_nft.write_text("rules")
        netns_path = tmp_path / "hermes-browser"
        netns_path.touch()

        failures: list[str] = []
        with (
            patch("hermes.runtime.__main__._NFT_HOST_RULE_FILE", host_nft),
            patch("hermes.runtime.__main__._NFT_NS_RULE_FILE", ns_nft),
            patch("subprocess.run", return_value=_make_completed_process("failed\n", returncode=3)),
            patch("hermes.runtime.__main__.Path", side_effect=lambda p: Path(str(p).replace("/run/netns/hermes-browser", str(netns_path)))),
        ):
            _check_netns_enforcement(failures)

        assert any("inactive" in f or "failed" in f for f in failures)


# ---------------------------------------------------------------------------
# _check_egress_proxy_enforcement
# ---------------------------------------------------------------------------

class TestCheckEgressProxyEnforcement:
    """_check_egress_proxy_enforcement uses systemctl + socket stat + /proc scan."""

    def test_all_passing_appends_no_failure(self, tmp_path: Path) -> None:
        from hermes.runtime.__main__ import _check_egress_proxy_enforcement

        sock = tmp_path / "egress-proxy.sock"
        sock.touch()

        failures: list[str] = []
        with (
            patch("subprocess.run", return_value=_make_completed_process("active\n")),
            patch("hermes.runtime.__main__.Path", side_effect=lambda p: Path(str(p).replace("/run/hermes/egress-proxy.sock", str(sock)))),
            patch("hermes.runtime.__main__._egress_proxy_process_alive", return_value=True),
        ):
            _check_egress_proxy_enforcement(failures)

        assert failures == []

    def test_service_inactive_appends_failure(self, tmp_path: Path) -> None:
        from hermes.runtime.__main__ import _check_egress_proxy_enforcement

        sock = tmp_path / "egress-proxy.sock"
        sock.touch()

        failures: list[str] = []
        with (
            patch("subprocess.run", return_value=_make_completed_process("inactive\n")),
            patch("hermes.runtime.__main__.Path", side_effect=lambda p: Path(str(p).replace("/run/hermes/egress-proxy.sock", str(sock)))),
            patch("hermes.runtime.__main__._egress_proxy_process_alive", return_value=True),
        ):
            _check_egress_proxy_enforcement(failures)

        assert any("inactive" in f or "failed" in f for f in failures)

    def test_socket_absent_is_non_fatal_when_service_active(self, tmp_path: Path) -> None:
        # El socket es un REFUERZO; la prueba autoritativa de enforcement es
        # `systemctl is-active hermes-egress-proxy` (mockeado "active"). Un
        # socket ausente NO debe brickear el boot → no se añade a failures.
        from hermes.runtime.__main__ import _check_egress_proxy_enforcement

        absent_sock = tmp_path / "no-sock"

        failures: list[str] = []
        with (
            patch("subprocess.run", return_value=_make_completed_process("active\n")),
            patch("hermes.runtime.__main__.Path", side_effect=lambda p: Path(str(p).replace("/run/hermes/egress-proxy.sock", str(absent_sock)))),
            patch("hermes.runtime.__main__._egress_proxy_process_alive", return_value=True),
        ):
            _check_egress_proxy_enforcement(failures)

        assert failures == []  # non-fatal (solo warning de diagnóstico)

    def test_process_dead_is_non_fatal_when_service_active(self, tmp_path: Path) -> None:
        from hermes.runtime.__main__ import _check_egress_proxy_enforcement

        sock = tmp_path / "egress-proxy.sock"
        sock.touch()

        failures: list[str] = []
        with (
            patch("subprocess.run", return_value=_make_completed_process("active\n")),
            patch("hermes.runtime.__main__.Path", side_effect=lambda p: Path(str(p).replace("/run/hermes/egress-proxy.sock", str(sock)))),
            patch("hermes.runtime.__main__._egress_proxy_process_alive", return_value=False),
        ):
            _check_egress_proxy_enforcement(failures)

        assert failures == []  # non-fatal (systemctl is-active es el gate fatal)

    def test_no_nft_or_ip_called(self, tmp_path: Path) -> None:
        """Critical regression: confirm no privileged binary is invoked."""
        from hermes.runtime.__main__ import _check_egress_proxy_enforcement

        sock = tmp_path / "egress-proxy.sock"
        sock.touch()

        with (
            patch("subprocess.run", return_value=_make_completed_process("active\n")) as mock_run,
            patch("hermes.runtime.__main__.Path", side_effect=lambda p: Path(str(p).replace("/run/hermes/egress-proxy.sock", str(sock)))),
            patch("hermes.runtime.__main__._egress_proxy_process_alive", return_value=True),
        ):
            _check_egress_proxy_enforcement([])

        for call in mock_run.call_args_list:
            cmd = call[0][0]
            assert "nft" not in cmd, f"nft must not be called: {cmd}"
            assert cmd[0] == "/usr/bin/systemctl"


# ---------------------------------------------------------------------------
# _read_memory_max_sysfs (cgroup fallback path)
# ---------------------------------------------------------------------------

class TestReadMemoryMaxSysfs:
    """_read_memory_max_sysfs: sysfs read + systemctl show fallback — zero caps."""

    def test_reads_sysfs_when_available(self, tmp_path: Path) -> None:
        from hermes.runtime.__main__ import _read_memory_max_sysfs

        mem_max = tmp_path / "memory.max"
        mem_max.write_text("4294967296\n")

        failures: list[str] = []
        result = _read_memory_max_sysfs(mem_max, failures)

        assert result == "4294967296"
        assert failures == []

    def test_returns_max_string_for_unlimited(self, tmp_path: Path) -> None:
        from hermes.runtime.__main__ import _read_memory_max_sysfs

        mem_max = tmp_path / "memory.max"
        mem_max.write_text("max\n")

        failures: list[str] = []
        result = _read_memory_max_sysfs(mem_max, failures)

        assert result == "max"
        assert failures == []

    def test_fallback_to_systemctl_on_permission_error(self, tmp_path: Path) -> None:
        from hermes.runtime.__main__ import _read_memory_max_sysfs

        mem_max = tmp_path / "memory.max"
        mem_max.write_text("4G")

        failures: list[str] = []
        with (
            patch.object(Path, "read_text", side_effect=PermissionError("EACCES")),
            patch("subprocess.run", return_value=_make_completed_process("MemoryMax=4294967296\n")) as mock_run,
        ):
            result = _read_memory_max_sysfs(mem_max, failures)

        assert result == "4294967296"
        assert failures == []
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "/usr/bin/systemctl"
        assert "nft" not in cmd
        assert "ip" not in cmd

    def test_fallback_translates_uint64_sentinel_to_max(self, tmp_path: Path) -> None:
        from hermes.runtime.__main__ import _read_memory_max_sysfs

        mem_max = tmp_path / "memory.max"

        with (
            patch.object(Path, "read_text", side_effect=PermissionError("EACCES")),
            patch("subprocess.run", return_value=_make_completed_process("MemoryMax=18446744073709551615\n")),
        ):
            result = _read_memory_max_sysfs(mem_max, [])

        assert result == "max"

    def test_fallback_timeout_appends_failure(self, tmp_path: Path) -> None:
        from hermes.runtime.__main__ import _read_memory_max_sysfs

        mem_max = tmp_path / "memory.max"

        failures: list[str] = []
        with (
            patch.object(Path, "read_text", side_effect=PermissionError("EACCES")),
            patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="systemctl", timeout=5)),
        ):
            result = _read_memory_max_sysfs(mem_max, failures)

        assert result is None
        assert len(failures) == 1
        assert "fallback_error" in failures[0]

    def test_fallback_uses_5s_timeout(self, tmp_path: Path) -> None:
        from hermes.runtime.__main__ import _read_memory_max_sysfs

        mem_max = tmp_path / "memory.max"

        with (
            patch.object(Path, "read_text", side_effect=PermissionError("EACCES")),
            patch("subprocess.run", return_value=_make_completed_process("MemoryMax=1073741824\n")) as mock_run,
        ):
            _read_memory_max_sysfs(mem_max, [])

        _, kwargs = mock_run.call_args
        assert kwargs.get("timeout") == 5.0


# ---------------------------------------------------------------------------
# Skip policy: dev-mode + env var
# ---------------------------------------------------------------------------

class TestConfinementSkipPolicy:
    """_is_confinement_check_required: skip only when both conditions hold."""

    def test_check_runs_when_env_not_set(self) -> None:
        from hermes.runtime.__main__ import _is_confinement_check_required

        env = {k: v for k, v in os.environ.items() if k != "HERMES_CONFINEMENT_CHECK"}
        with patch.dict("os.environ", env, clear=True):
            assert _is_confinement_check_required() is True

    def test_check_skipped_when_env_0_and_marker_exists(self, tmp_path: Path) -> None:
        from hermes.runtime.__main__ import _is_confinement_check_required

        marker = tmp_path / "dev-mode"
        marker.touch()
        with (
            patch.dict("os.environ", {"HERMES_CONFINEMENT_CHECK": "0"}),
            patch("hermes.runtime.__main__._DEV_MODE_MARKER", marker),
        ):
            assert _is_confinement_check_required() is False

    def test_check_runs_when_env_0_but_marker_absent(self, tmp_path: Path) -> None:
        from hermes.runtime.__main__ import _is_confinement_check_required

        absent_marker = tmp_path / "no-dev-mode"
        with (
            patch.dict("os.environ", {"HERMES_CONFINEMENT_CHECK": "0"}),
            patch("hermes.runtime.__main__._DEV_MODE_MARKER", absent_marker),
        ):
            assert _is_confinement_check_required() is True

    def test_check_runs_when_env_1_regardless_of_marker(self, tmp_path: Path) -> None:
        from hermes.runtime.__main__ import _is_confinement_check_required

        marker = tmp_path / "dev-mode"
        marker.touch()
        with (
            patch.dict("os.environ", {"HERMES_CONFINEMENT_CHECK": "1"}),
            patch("hermes.runtime.__main__._DEV_MODE_MARKER", marker),
        ):
            assert _is_confinement_check_required() is True
