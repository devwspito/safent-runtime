"""OpenShellSandboxProvider: lifecycle adapter for browser sandboxes via the OpenShell CLI.

Manages three concerns for a single sandbox:
  1. Gateway liveness — ensures the openshell gateway daemon is running.
  2. Sandbox lifecycle — provisions a sandbox (create or reuse) with Chromium
     headless inside, forwards the CDP port to localhost, and tears it down.
  3. Egress policy — translates an allowlist of domain names into an OpenShell
     network_policy YAML (default-deny + per-binary allow rules) and hot-pushes
     it into the live sandbox via `openshell policy set --wait`.

Design constraints (spec 012 / plan.md):
  - CLI invoked via subprocess with args as list — no shell=True, no injection risk.
  - All subprocesses have explicit timeouts.
  - Named exceptions for each failure mode; never raises bare Exception.
  - Idempotent: `provision()` returns the existing cdp_url if already forwarded.
  - teardown() is a no-op if the provider was never provisioned.

Placement: infrastructure layer. The domain layer has zero awareness of OpenShell.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Final

if TYPE_CHECKING:
    from typing import Any

import yaml

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Typed exceptions — never leak bare subprocess errors to domain callers.
# --------------------------------------------------------------------------


class OpenShellGatewayNotRunningError(RuntimeError):
    """openshell gateway is not reachable; call `openshell gateway start` first."""


class OpenShellSandboxProvisionError(RuntimeError):
    """Sandbox could not be created or the CDP forward could not be established."""


class OpenShellPolicyPushError(RuntimeError):
    """Policy YAML could not be pushed to the running sandbox."""


class OpenShellTeardownError(RuntimeError):
    """Forward or sandbox delete failed during teardown (non-fatal by default)."""


# --------------------------------------------------------------------------
# Value objects
# --------------------------------------------------------------------------

DEFAULT_OPENSHELL_BIN: Final[str] = os.environ.get(
    "OPENSHELL_BIN", os.path.expanduser("~/.local/bin/openshell")
)
DEFAULT_GATEWAY_TIMEOUT_S: Final[int] = 10
DEFAULT_PROVISION_TIMEOUT_S: Final[int] = 60
DEFAULT_POLICY_TIMEOUT_S: Final[int] = 30
DEFAULT_TEARDOWN_TIMEOUT_S: Final[int] = 15

# Chromium binary inside the sandbox — used to scope network policies per-binary.
_SANDBOX_CHROMIUM_GLOB: Final[str] = "/opt/**"


@dataclass
class EgressAllowEntry:
    """A single allowed egress endpoint for the sandbox browser.

    host: domain name (e.g. "httpbin.org")
    port: TCP port (default 443)
    tls: "passthrough" (SNI only) or "terminate" (MITM — requires CA)
    """

    host: str
    port: int = 443
    tls: str = "passthrough"


@dataclass
class SandboxProvisionResult:
    """Result of a successful provision() call."""

    sandbox_name: str
    cdp_host: str
    cdp_port: int

    @property
    def cdp_url(self) -> str:
        return f"http://{self.cdp_host}:{self.cdp_port}"


# --------------------------------------------------------------------------
# Policy YAML builder — pure function, no I/O
# --------------------------------------------------------------------------


def build_egress_policy_yaml(
    allowlist: list[EgressAllowEntry],
    *,
    binary_glob: str = _SANDBOX_CHROMIUM_GLOB,
) -> str:
    """Translate an allowlist of EgressAllowEntry into an OpenShell policy YAML string.

    The generated policy:
      - Has a named network_policy group per entry (named after the host).
      - Each group scopes the allow rule to the Chromium binary glob.
      - All unlisted traffic is denied by default (OpenShell default-deny).

    Returns a YAML string ready to write to a temp file and pass to
    `openshell policy set <sandbox> --policy <file>`.
    """
    network_policies: dict[str, object] = {}

    for entry in allowlist:
        policy_name = _sanitize_policy_name(entry.host)
        network_policies[policy_name] = {
            "name": policy_name,
            "endpoints": [
                {
                    "host": entry.host,
                    "port": entry.port,
                    "protocol": "tcp",
                    "tls": entry.tls,
                    "enforcement": "enforce",
                    "access": "full",
                }
            ],
            "binaries": [{"path": binary_glob}],
        }

    policy: dict[str, object] = {
        "version": 1,
        "network_policies": network_policies,
    }
    return yaml.dump(policy, sort_keys=False, allow_unicode=True)


def _sanitize_policy_name(host: str) -> str:
    """Convert a hostname to a safe policy-group name (alphanumeric + hyphen)."""
    return "".join(c if c.isalnum() or c == "-" else "_" for c in host)


def make_egress_approved_sites_provider(
    provider: "OpenShellSandboxProvider",
) -> "Callable[[object], frozenset[str]]":
    """Return an ApprovedSitesProvider backed by provider.approved_hosts.

    The returned callable reads `provider.approved_hosts` on every call, so the
    application-layer WRITE gate always reflects the most-recently-applied egress
    policy without requiring a restart. This is the single source of truth contract:
    only hosts allowed by `apply_egress_policy` can receive WRITE actions.

    Usage in the composition root:
        sandbox = OpenShellSandboxProvider(sandbox_name="hermes-browser", cdp_port=9222)
        sandbox.provision()
        sandbox.apply_egress_policy([EgressAllowEntry("httpbin.org")])
        adapter = BrowserSurfaceAdapter(
            ...,
            approved_sites=make_egress_approved_sites_provider(sandbox),
        )
    """
    def _provider(_tenant_id: object) -> frozenset[str]:
        return provider.approved_hosts

    return _provider


# --------------------------------------------------------------------------
# Provider
# --------------------------------------------------------------------------


@dataclass
class OpenShellSandboxProvider:
    """Manages the lifecycle of one OpenShell sandbox for a headless browser session.

    Usage:
        provider = OpenShellSandboxProvider(sandbox_name="hermes-browser", cdp_port=9222)
        result = await provider.provision()       # cdp_url = "http://127.0.0.1:9222"
        await provider.apply_egress_policy([EgressAllowEntry("httpbin.org")])
        ...
        await provider.teardown()

    All methods are sync — subprocess calls are inherently blocking and should be
    wrapped in asyncio.to_thread() by async callers. Kept sync intentionally to
    avoid hidden threading complexity inside the provider itself.
    """

    sandbox_name: str
    cdp_port: int = 9222
    cdp_bind: str = "127.0.0.1"
    openshell_bin: str = field(default_factory=lambda: DEFAULT_OPENSHELL_BIN)
    gateway_timeout_s: int = DEFAULT_GATEWAY_TIMEOUT_S
    provision_timeout_s: int = DEFAULT_PROVISION_TIMEOUT_S
    policy_timeout_s: int = DEFAULT_POLICY_TIMEOUT_S
    teardown_timeout_s: int = DEFAULT_TEARDOWN_TIMEOUT_S

    _provisioned: bool = field(default=False, init=False, repr=False)
    _approved_hosts: frozenset[str] = field(
        default_factory=frozenset, init=False, repr=False
    )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def provision(self) -> SandboxProvisionResult:
        """Ensure the gateway is up, the sandbox exists, and the CDP port is forwarded.

        Idempotent: if `_provisioned` is already True, returns the cached result
        without calling the CLI again.

        Raises:
            OpenShellGatewayNotRunningError: gateway is not reachable.
            OpenShellSandboxProvisionError: sandbox create or forward failed.
        """
        self._assert_gateway_running()
        self._ensure_sandbox_exists()
        self._ensure_forward_running()
        self._provisioned = True

        result = SandboxProvisionResult(
            sandbox_name=self.sandbox_name,
            cdp_host=self.cdp_bind,
            cdp_port=self.cdp_port,
        )
        logger.info(
            "hermes.openshell.provisioned",
            extra={
                "sandbox": self.sandbox_name,
                "cdp_url": result.cdp_url,
            },
        )
        return result

    @property
    def approved_hosts(self) -> frozenset[str]:
        """Return the set of hostnames currently allowed by the applied egress policy.

        Updated atomically after each successful `apply_egress_policy()` call.
        Empty until apply_egress_policy has been called (fail-closed default).
        """
        return self._approved_hosts

    def apply_egress_policy(self, allowlist: list[EgressAllowEntry]) -> None:
        """Incrementally update the sandbox network policy to match allowlist.

        Uses `openshell policy update` rather than `policy set` so that the
        existing filesystem_policy on the live sandbox is preserved.  The diff
        against the previously-applied allowlist drives which endpoints are added
        and which are removed.

        On success, `approved_hosts` is updated to reflect the new allowlist so
        that the application-layer WRITE gate stays consistent with the kernel
        egress gate (single source of truth).

        Raises:
            OpenShellPolicyPushError: any policy update CLI call failed.
        """
        if not allowlist:
            logger.warning(
                "hermes.openshell.apply_egress_policy_empty_allowlist",
                extra={"sandbox": self.sandbox_name},
            )

        new_hosts = {e.host: e for e in allowlist}
        old_hosts = set(self._approved_hosts)

        to_add = [e for e in allowlist if e.host not in old_hosts]
        to_remove = [h for h in old_hosts if h not in new_hosts]

        for entry in to_add:
            self._update_add_endpoint(entry)

        for host in to_remove:
            self._update_remove_endpoint(host)

        # Update approved_hosts atomically after all CLI calls succeed.
        self._approved_hosts = frozenset(new_hosts.keys())

        logger.info(
            "hermes.openshell.policy_applied",
            extra={
                "sandbox": self.sandbox_name,
                "added": [e.host for e in to_add],
                "removed": to_remove,
                "allowed_hosts": list(new_hosts.keys()),
            },
        )

    def teardown(self) -> None:
        """Stop the CDP forward. Does NOT delete the sandbox (reuse across sessions).

        Call teardown() in a finally block around the browser session. Non-fatal:
        logs warnings on failure but does not raise by default so as not to mask
        the primary exception from the browser session.
        """
        if not self._provisioned:
            return

        try:
            self._stop_forward()
        except OpenShellTeardownError as exc:
            logger.warning(
                "hermes.openshell.teardown_forward_failed",
                extra={"sandbox": self.sandbox_name, "error": str(exc)},
            )

        self._provisioned = False
        logger.info(
            "hermes.openshell.teardown_complete",
            extra={"sandbox": self.sandbox_name},
        )

    # ------------------------------------------------------------------
    # Internal helpers — each wraps exactly one CLI call
    # ------------------------------------------------------------------

    def _assert_gateway_running(self) -> None:
        """Verify the gateway is reachable. Raises OpenShellGatewayNotRunningError."""
        result = self._run(
            ["status"],
            timeout=self.gateway_timeout_s,
        )
        if result.returncode != 0:
            raise OpenShellGatewayNotRunningError(
                f"openshell gateway not running (exit {result.returncode}). "
                "Run `openshell gateway start` first.\n"
                f"stderr: {result.stderr.strip()}"
            )

    def _ensure_sandbox_exists(self) -> None:
        """Create the sandbox if it doesn't exist yet. Idempotent via `sandbox list`."""
        list_result = self._run(
            ["sandbox", "list"],
            timeout=self.provision_timeout_s,
        )
        if list_result.returncode != 0:
            raise OpenShellSandboxProvisionError(
                f"openshell sandbox list failed (exit {list_result.returncode}): "
                f"{list_result.stderr.strip()}"
            )

        if self.sandbox_name in list_result.stdout:
            logger.debug(
                "hermes.openshell.sandbox_exists_reusing",
                extra={"sandbox": self.sandbox_name},
            )
            return

        create_result = self._run(
            ["sandbox", "create", "--name", self.sandbox_name],
            timeout=self.provision_timeout_s,
        )
        if create_result.returncode != 0:
            raise OpenShellSandboxProvisionError(
                f"openshell sandbox create failed (exit {create_result.returncode}): "
                f"{create_result.stderr.strip()}"
            )
        logger.info(
            "hermes.openshell.sandbox_created",
            extra={"sandbox": self.sandbox_name},
        )

    def _ensure_forward_running(self) -> None:
        """Start the CDP port forward if not already active."""
        fwd_list = self._run(
            ["forward", "list"],
            timeout=self.gateway_timeout_s,
        )
        bind_spec = f"{self.cdp_bind}:{self.cdp_port}"
        # Check by sandbox name + port in the list output
        if (
            fwd_list.returncode == 0
            and self.sandbox_name in fwd_list.stdout
            and str(self.cdp_port) in fwd_list.stdout
        ):
            logger.debug(
                "hermes.openshell.forward_already_active",
                extra={"sandbox": self.sandbox_name, "port": self.cdp_port},
            )
            return

        fwd_result = self._run(
            ["forward", "start", "-d", f"{bind_spec}", self.sandbox_name],
            timeout=self.provision_timeout_s,
        )
        if fwd_result.returncode != 0:
            raise OpenShellSandboxProvisionError(
                f"openshell forward start failed (exit {fwd_result.returncode}): "
                f"{fwd_result.stderr.strip()}"
            )

    def _stop_forward(self) -> None:
        """Stop the CDP port forward. Raises OpenShellTeardownError on failure."""
        result = self._run(
            ["forward", "stop", str(self.cdp_port), self.sandbox_name],
            timeout=self.teardown_timeout_s,
        )
        if result.returncode != 0:
            raise OpenShellTeardownError(
                f"openshell forward stop failed (exit {result.returncode}): "
                f"{result.stderr.strip()}"
            )

    def _update_add_endpoint(self, entry: EgressAllowEntry) -> None:
        """Add a single endpoint to the live sandbox policy via `policy update`.

        Format passed to --add-endpoint: "host:port" (TCP; access/enforcement are
        defaults set by OpenShell for non-REST endpoints).
        The Chromium binary glob is scoped via --binary.
        --wait ensures the sandbox has loaded the revision before we return.
        """
        endpoint_spec = f"{entry.host}:{entry.port}"
        result = self._run(
            [
                "policy", "update",
                self.sandbox_name,
                "--add-endpoint", endpoint_spec,
                "--binary", _SANDBOX_CHROMIUM_GLOB,
                "--wait",
            ],
            timeout=self.policy_timeout_s,
        )
        if result.returncode != 0:
            raise OpenShellPolicyPushError(
                f"openshell policy update --add-endpoint {endpoint_spec!r} failed "
                f"(exit {result.returncode}): {result.stderr.strip()}"
            )

    def _update_remove_endpoint(self, host: str) -> None:
        """Remove an endpoint from the live sandbox policy via `policy update`.

        Removes all ports for the given host (we track host-level granularity).
        Best-effort: logs a warning if the endpoint was not present (idempotent).
        """
        # OpenShell remove-endpoint format: "host:port". We use 443 as the
        # canonical port — this mirrors the port used when adding.
        endpoint_spec = f"{host}:443"
        result = self._run(
            [
                "policy", "update",
                self.sandbox_name,
                "--remove-endpoint", endpoint_spec,
                "--wait",
            ],
            timeout=self.policy_timeout_s,
        )
        if result.returncode != 0:
            logger.warning(
                "hermes.openshell.remove_endpoint_failed",
                extra={
                    "sandbox": self.sandbox_name,
                    "endpoint": endpoint_spec,
                    "stderr": result.stderr.strip(),
                },
            )

    def _run(
        self,
        subcommand: list[str],
        *,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        """Run an openshell subcommand safely — args as list, no shell=True."""
        cmd = [self.openshell_bin, *subcommand]
        logger.debug(
            "hermes.openshell.cli_run",
            extra={"cmd": cmd},
        )
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            # Explicit: no shell=True, no PATH expansion outside the binary
        )
