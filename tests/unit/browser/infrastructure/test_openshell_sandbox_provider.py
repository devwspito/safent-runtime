"""Unit tests for OpenShellSandboxProvider and build_egress_policy_yaml.

No gateway, no sandbox, no network — CLI is mocked via monkeypatching subprocess.run.

Coverage:
  - build_egress_policy_yaml: allowlist → YAML (happy path, empty allowlist,
    multiple entries, hostname sanitisation).
  - OpenShellSandboxProvider.provision(): gateway-down error, sandbox-reuse,
    sandbox-create, forward-already-active, forward-start.
  - apply_egress_policy(): policy push success, CLI failure raises typed error.
  - teardown(): forward stop called, not-provisioned is a no-op.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest
import yaml

from hermes.browser.infrastructure.openshell_sandbox_provider import (
    EgressAllowEntry,
    OpenShellGatewayNotRunningError,
    OpenShellPolicyPushError,
    OpenShellSandboxProvisionError,
    OpenShellSandboxProvider,
    OpenShellTeardownError,
    SandboxProvisionResult,
    _sanitize_policy_name,
    build_egress_policy_yaml,
    make_egress_approved_sites_provider,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BIN = "/fake/openshell"


def _completed(
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    r = subprocess.CompletedProcess(args=[], returncode=returncode)
    r.stdout = stdout
    r.stderr = stderr
    return r


def _provider(**kwargs: Any) -> OpenShellSandboxProvider:
    defaults = {
        "sandbox_name": "test-jail",
        "cdp_port": 9222,
        "openshell_bin": _BIN,
    }
    defaults.update(kwargs)
    return OpenShellSandboxProvider(**defaults)


# ---------------------------------------------------------------------------
# build_egress_policy_yaml
# ---------------------------------------------------------------------------


class TestBuildEgressPolicyYaml:
    def test_single_entry_valid_yaml(self) -> None:
        result = build_egress_policy_yaml([EgressAllowEntry("httpbin.org")])
        parsed = yaml.safe_load(result)

        assert parsed["version"] == 1
        assert "network_policies" in parsed
        policies = parsed["network_policies"]
        assert len(policies) == 1
        key = next(iter(policies))
        entry = policies[key]["endpoints"][0]
        assert entry["host"] == "httpbin.org"
        assert entry["port"] == 443
        assert entry["tls"] == "passthrough"
        assert entry["enforcement"] == "enforce"

    def test_per_binary_scoped(self) -> None:
        result = build_egress_policy_yaml([EgressAllowEntry("httpbin.org")])
        parsed = yaml.safe_load(result)
        policies = parsed["network_policies"]
        key = next(iter(policies))
        binaries = policies[key]["binaries"]
        assert any("/opt/" in b["path"] for b in binaries)

    def test_multiple_entries_produce_separate_groups(self) -> None:
        allowlist = [
            EgressAllowEntry("api.example.com", port=443),
            EgressAllowEntry("cdn.example.com", port=80),
        ]
        result = build_egress_policy_yaml(allowlist)
        parsed = yaml.safe_load(result)
        assert len(parsed["network_policies"]) == 2

    def test_empty_allowlist_produces_no_policies(self) -> None:
        result = build_egress_policy_yaml([])
        parsed = yaml.safe_load(result)
        assert parsed["network_policies"] == {}

    def test_default_deny_implied_by_absence(self) -> None:
        """OpenShell default-deny: no wildcard-host endpoint allow rule must be present.

        The binary glob (/opt/**) is acceptable; the host field in each endpoint
        must be a concrete hostname, never a wildcard like '*' or '0.0.0.0'.
        """
        result = build_egress_policy_yaml([EgressAllowEntry("httpbin.org")])
        parsed = yaml.safe_load(result)
        for _group_name, group in parsed.get("network_policies", {}).items():
            for endpoint in group.get("endpoints", []):
                host = endpoint.get("host", "")
                assert host not in ("*", "0.0.0.0", ""), (
                    f"Wildcard or empty host in policy endpoint: {endpoint}"
                )

    def test_hostname_sanitised_in_policy_name(self) -> None:
        """Dots in hostnames are replaced so the YAML key is safe."""
        name = _sanitize_policy_name("api.test-host.com")
        assert "." not in name
        assert name.replace("_", "").replace("-", "").isalnum()


# ---------------------------------------------------------------------------
# OpenShellSandboxProvider.provision()
# ---------------------------------------------------------------------------


class TestProvision:
    def test_gateway_down_raises_typed_error(self) -> None:
        provider = _provider()
        with patch("subprocess.run", return_value=_completed(returncode=1, stderr="not running")):
            with pytest.raises(OpenShellGatewayNotRunningError):
                provider.provision()

    def test_provision_reuses_existing_sandbox(self) -> None:
        provider = _provider()
        responses = [
            _completed(stdout="Connected"),           # status
            _completed(stdout="test-jail  Ready"),    # sandbox list → exists
            _completed(stdout="test-jail  9222"),     # forward list → active
        ]
        with patch("subprocess.run", side_effect=responses) as mock_run:
            result = provider.provision()

        assert isinstance(result, SandboxProvisionResult)
        assert result.cdp_url == "http://127.0.0.1:9222"
        # sandbox create should NOT have been called
        calls_text = " ".join(str(c) for c in mock_run.call_args_list)
        assert "create" not in calls_text

    def test_provision_creates_sandbox_when_absent(self) -> None:
        provider = _provider()
        responses = [
            _completed(stdout="Connected"),        # status
            _completed(stdout="other-sandbox"),    # sandbox list → NOT present
            _completed(stdout="created"),          # sandbox create
            _completed(stdout=""),                 # forward list → NOT active
            _completed(stdout=""),                 # forward start
        ]
        with patch("subprocess.run", side_effect=responses) as mock_run:
            result = provider.provision()

        assert result.sandbox_name == "test-jail"
        calls_cmds = [c[0][0] for c in mock_run.call_args_list]
        assert any("create" in cmd for cmd in calls_cmds)

    def test_provision_starts_forward_when_absent(self) -> None:
        provider = _provider()
        responses = [
            _completed(stdout="Connected"),
            _completed(stdout="test-jail  Ready"),
            _completed(stdout="other-jail  9999"),   # forward list → not our port
            _completed(stdout=""),                   # forward start
        ]
        with patch("subprocess.run", side_effect=responses) as mock_run:
            provider.provision()

        calls_cmds = [c[0][0] for c in mock_run.call_args_list]
        assert any("start" in cmd for cmd in calls_cmds)

    def test_sandbox_create_failure_raises_typed_error(self) -> None:
        provider = _provider()
        responses = [
            _completed(stdout="Connected"),
            _completed(stdout=""),                          # list → absent
            _completed(returncode=1, stderr="quota exceeded"),  # create fails
        ]
        with patch("subprocess.run", side_effect=responses):
            with pytest.raises(OpenShellSandboxProvisionError, match="quota exceeded"):
                provider.provision()

    def test_forward_start_failure_raises_typed_error(self) -> None:
        provider = _provider()
        responses = [
            _completed(stdout="Connected"),
            _completed(stdout="test-jail  Ready"),
            _completed(stdout=""),                               # forward list empty
            _completed(returncode=1, stderr="port in use"),     # forward start fails
        ]
        with patch("subprocess.run", side_effect=responses):
            with pytest.raises(OpenShellSandboxProvisionError, match="port in use"):
                provider.provision()

    def test_provision_sets_provisioned_flag(self) -> None:
        provider = _provider()
        responses = [
            _completed(stdout="Connected"),
            _completed(stdout="test-jail  Ready"),
            _completed(stdout="test-jail  9222"),
        ]
        with patch("subprocess.run", side_effect=responses):
            provider.provision()
        assert provider._provisioned is True


# ---------------------------------------------------------------------------
# apply_egress_policy
# ---------------------------------------------------------------------------


class TestApplyEgressPolicy:
    def test_success_calls_policy_update(self) -> None:
        """apply_egress_policy uses `policy update --add-endpoint` for live sandboxes."""
        provider = _provider()
        with patch("subprocess.run", return_value=_completed()) as mock_run:
            provider.apply_egress_policy([EgressAllowEntry("httpbin.org")])

        args_list = mock_run.call_args_list
        # One call per new endpoint (httpbin.org is new)
        assert len(args_list) == 1
        cmd = args_list[0][0][0]
        assert "policy" in cmd
        assert "update" in cmd
        assert "--add-endpoint" in cmd

    def test_success_does_not_call_policy_set(self) -> None:
        """policy set would fail on live sandboxes with filesystem policy; use update."""
        provider = _provider()
        with patch("subprocess.run", return_value=_completed()) as mock_run:
            provider.apply_egress_policy([EgressAllowEntry("httpbin.org")])

        for call_obj in mock_run.call_args_list:
            cmd = call_obj[0][0]
            assert "set" not in cmd, (
                f"policy set must not be called on a live sandbox: {cmd}"
            )

    def test_policy_cli_failure_raises_typed_error(self) -> None:
        provider = _provider()
        with patch(
            "subprocess.run",
            return_value=_completed(returncode=2, stderr="sandbox not found"),
        ):
            with pytest.raises(OpenShellPolicyPushError, match="sandbox not found"):
                provider.apply_egress_policy([EgressAllowEntry("httpbin.org")])

    def test_multiple_endpoints_add_one_call_each(self) -> None:
        """Each entry in the allowlist triggers one --add-endpoint call."""
        provider = _provider()
        allowlist = [EgressAllowEntry("alpha.com"), EgressAllowEntry("beta.com")]
        with patch("subprocess.run", return_value=_completed()) as mock_run:
            provider.apply_egress_policy(allowlist)

        assert mock_run.call_count == 2, (
            f"Expected 2 CLI calls (one per endpoint), got {mock_run.call_count}"
        )

    def test_second_call_only_adds_new_host(self) -> None:
        """On the second call, only newly-added hosts trigger --add-endpoint."""
        provider = _provider()
        with patch("subprocess.run", return_value=_completed()) as mock_run:
            provider.apply_egress_policy([EgressAllowEntry("existing.com")])
            call_count_after_first = mock_run.call_count

            # Second call: same host + one new host.
            provider.apply_egress_policy([
                EgressAllowEntry("existing.com"),
                EgressAllowEntry("new.com"),
            ])
            additional_calls = mock_run.call_count - call_count_after_first

        # Only "new.com" is new → exactly 1 additional CLI call.
        assert additional_calls == 1, (
            f"Expected 1 additional call for the new host only; got {additional_calls}"
        )

    def test_removed_hosts_trigger_remove_endpoint(self) -> None:
        """Hosts no longer in the allowlist trigger --remove-endpoint."""
        provider = _provider()
        with patch("subprocess.run", return_value=_completed()) as mock_run:
            provider.apply_egress_policy([EgressAllowEntry("old.com")])
            mock_run.reset_mock()

            # Second call: old.com removed, new.com added.
            provider.apply_egress_policy([EgressAllowEntry("new.com")])

        cmds = [call_obj[0][0] for call_obj in mock_run.call_args_list]
        has_remove = any("--remove-endpoint" in cmd for cmd in cmds)
        has_add = any("--add-endpoint" in cmd for cmd in cmds)
        assert has_remove, "old.com must be removed via --remove-endpoint"
        assert has_add, "new.com must be added via --add-endpoint"


# ---------------------------------------------------------------------------
# teardown
# ---------------------------------------------------------------------------


class TestTeardown:
    def test_teardown_not_provisioned_is_noop(self) -> None:
        provider = _provider()
        with patch("subprocess.run") as mock_run:
            provider.teardown()
        mock_run.assert_not_called()

    def test_teardown_calls_forward_stop(self) -> None:
        provider = _provider()
        provider._provisioned = True  # simulate provisioned state
        with patch("subprocess.run", return_value=_completed()) as mock_run:
            provider.teardown()

        cmd = mock_run.call_args[0][0]
        assert "stop" in cmd

    def test_teardown_forward_failure_logs_warning_not_raises(self) -> None:
        provider = _provider()
        provider._provisioned = True
        with patch(
            "subprocess.run",
            return_value=_completed(returncode=1, stderr="nothing to stop"),
        ):
            # Must NOT raise — teardown is best-effort
            provider.teardown()

    def test_teardown_clears_provisioned_flag(self) -> None:
        provider = _provider()
        provider._provisioned = True
        with patch("subprocess.run", return_value=_completed()):
            provider.teardown()
        assert provider._provisioned is False


# ---------------------------------------------------------------------------
# approved_hosts — single source of truth
# ---------------------------------------------------------------------------


class TestApprovedHosts:
    def test_approved_hosts_empty_before_policy_applied(self) -> None:
        """Before apply_egress_policy(), approved_hosts is an empty frozenset."""
        provider = _provider()
        assert provider.approved_hosts == frozenset()

    def test_approved_hosts_updated_after_apply_egress_policy(self) -> None:
        """After a successful apply_egress_policy(), approved_hosts reflects the allowlist."""
        provider = _provider()
        allowlist = [EgressAllowEntry("httpbin.org"), EgressAllowEntry("api.example.com")]
        with patch("subprocess.run", return_value=_completed()):
            provider.apply_egress_policy(allowlist)

        assert provider.approved_hosts == frozenset({"httpbin.org", "api.example.com"})

    def test_approved_hosts_not_updated_on_policy_push_failure(self) -> None:
        """If the CLI call fails, approved_hosts must NOT be updated (atomic)."""
        provider = _provider()
        with patch(
            "subprocess.run",
            return_value=_completed(returncode=1, stderr="policy rejected"),
        ):
            with pytest.raises(OpenShellPolicyPushError):
                provider.apply_egress_policy([EgressAllowEntry("httpbin.org")])

        assert provider.approved_hosts == frozenset(), (
            "approved_hosts must remain empty when policy push fails"
        )

    def test_approved_hosts_reflects_latest_apply(self) -> None:
        """Successive calls to apply_egress_policy() overwrite approved_hosts."""
        provider = _provider()
        with patch("subprocess.run", return_value=_completed()):
            provider.apply_egress_policy([EgressAllowEntry("first.com")])
            assert provider.approved_hosts == frozenset({"first.com"})

            provider.apply_egress_policy([EgressAllowEntry("second.com")])
            assert provider.approved_hosts == frozenset({"second.com"})

    def test_make_egress_provider_returns_callable(self) -> None:
        """make_egress_approved_sites_provider returns a callable ApprovedSitesProvider."""
        provider = _provider()
        fn = make_egress_approved_sites_provider(provider)
        assert callable(fn)

    def test_make_egress_provider_reflects_live_approved_hosts(self) -> None:
        """The returned provider reads approved_hosts live — changes are visible immediately."""
        provider = _provider()
        fn = make_egress_approved_sites_provider(provider)

        # Before any policy: empty.
        assert fn(None) == frozenset()

        # After policy push: updated.
        with patch("subprocess.run", return_value=_completed()):
            provider.apply_egress_policy([EgressAllowEntry("httpbin.org")])

        assert fn(None) == frozenset({"httpbin.org"}), (
            "Provider must reflect the most-recently-applied egress policy"
        )

    def test_make_egress_provider_tenant_id_agnostic(self) -> None:
        """The provider ignores tenant_id (single-node, single-sandbox model)."""
        from uuid import uuid4  # noqa: PLC0415
        provider = _provider()
        fn = make_egress_approved_sites_provider(provider)
        with patch("subprocess.run", return_value=_completed()):
            provider.apply_egress_policy([EgressAllowEntry("httpbin.org")])

        tenant_a = uuid4()
        tenant_b = uuid4()
        assert fn(tenant_a) == fn(tenant_b) == frozenset({"httpbin.org"})


# ---------------------------------------------------------------------------
# CLI safety — no shell=True
# ---------------------------------------------------------------------------


class TestCliSafety:
    def test_run_uses_list_not_shell_string(self) -> None:
        """subprocess.run must receive a list, never a string (no shell injection)."""
        provider = _provider()
        captured: list[Any] = []

        def _capture(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            captured.append((cmd, kwargs))
            return _completed(returncode=1)  # short-circuit after first call

        with patch("subprocess.run", side_effect=_capture):
            try:
                provider.provision()
            except OpenShellGatewayNotRunningError:
                pass

        assert captured, "subprocess.run was never called"
        cmd_arg = captured[0][0]
        assert isinstance(cmd_arg, list), "cmd must be a list, not a shell string"
        kwargs = captured[0][1]
        assert not kwargs.get("shell", False), "shell=True is forbidden"
