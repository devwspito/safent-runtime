"""hermes-tailscaled.{service,path} + hermes-tailscale-control.{service,path} (022).

Covers the plan.md invariants that must hold on every commit, independent of
whether the owner ever opts in to a tailnet:

  1. The four units parse (systemd-analyze verify when available, else
     structural asserts — same opt-in pattern as tests/integration/test_boot_graph.py).
  2. Both tailscaled proxies (HTTP-CONNECT :1055, SOCKS5 :1056) bind
     127.0.0.1 ONLY — never a wildcard address.
  3. RED-TEAM INVARIANT: browser-host.nft / mcp-host.nft still drop
     127.0.0.0/8 (tailscaled's loopback) and 100.64.0.0/10 (Tailscale CGNAT)
     from every agent netns, in the SAME anti-pivot daddr set as the rest of
     RFC1918 — so an agent can never reach the proxies directly, only the
     host-netns egress-proxy can.
  4. The control script is actually baked into the image and both .path
     units are actually enabled (the pattern test_host_firewall_enabled.py
     pins: a unit that ships but is never `systemctl enable`d never starts).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[3]
_SYSTEMD = _ROOT / "ops/agents-os-edition/systemd"
_NETNS = _ROOT / "ops/agents-os-edition/netns"

_TAILSCALED_SERVICE = _SYSTEMD / "hermes-tailscaled.service"
_TAILSCALED_PATH = _SYSTEMD / "hermes-tailscaled.path"
_CONTROL_SERVICE = _SYSTEMD / "hermes-tailscale-control.service"
_CONTROL_PATH = _SYSTEMD / "hermes-tailscale-control.path"
_WATCHER_SERVICE = _SYSTEMD / "hermes-tailscale-status-watcher.service"
_ALL_UNITS = [_TAILSCALED_SERVICE, _TAILSCALED_PATH, _CONTROL_SERVICE, _CONTROL_PATH, _WATCHER_SERVICE]
_SERVICE_UNITS = [_TAILSCALED_SERVICE, _CONTROL_SERVICE, _WATCHER_SERVICE]
_PATH_UNITS = [_TAILSCALED_PATH, _CONTROL_PATH]

_TAILSCALED_TEXT = _TAILSCALED_SERVICE.read_text(encoding="utf-8")
_CONTROL_SERVICE_TEXT = _CONTROL_SERVICE.read_text(encoding="utf-8")
_WATCHER_SERVICE_TEXT = _WATCHER_SERVICE.read_text(encoding="utf-8")
_BROWSER_HOST_NFT = (_NETNS / "browser-host.nft").read_text(encoding="utf-8")
_MCP_HOST_NFT = (_NETNS / "mcp-host.nft").read_text(encoding="utf-8")
_CONTAINERFILE_TEXT = (_ROOT / "ops/container/Containerfile").read_text(encoding="utf-8")
_TMPFILES_TEXT = (_ROOT / "ops/agents-os-edition/tmpfiles/hermes.conf").read_text(encoding="utf-8")

# Golden-text (mirrors test_companion_nft_generation.py style): the EXACT
# anti-pivot daddr set both host-side nft tables must keep, verbatim.
_ANTIPIVOT_DADDR_SET = (
    "ip daddr { 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, "
    "127.0.0.0/8, 169.254.0.0/16, 100.64.0.0/10 }"
)


def _exact_line(text: str, line: str) -> bool:
    return any(raw.strip() == line for raw in text.splitlines())


# ---------------------------------------------------------------------------
# 1. Units exist + parse
# ---------------------------------------------------------------------------


class TestUnitsExist:
    @pytest.mark.parametrize("unit", _ALL_UNITS, ids=lambda p: p.name)
    def test_file_exists(self, unit: Path) -> None:
        assert unit.exists(), f"{unit.name} is missing"


class TestSystemdAnalyzeVerify:
    """Opt-in structural check — same pattern as test_boot_graph.py's cycle check.

    A fresh --root sandbox has no sysinit.target and no real /usr/sbin/tailscaled
    binary, so "Unit ... not found" / "is not executable" are EXPECTED noise —
    only genuine parse problems ("Unknown key name", "Failed to parse ...") fail
    the test.
    """

    @pytest.mark.skipif(
        shutil.which("systemd-analyze") is None,
        reason="systemd-analyze not available — skipping structural verify",
    )
    def test_no_structural_parse_errors(self, tmp_path: Path) -> None:
        unit_dir = tmp_path / "etc" / "systemd" / "system"
        unit_dir.mkdir(parents=True)
        for u in _ALL_UNITS:
            (unit_dir / u.name).write_bytes(u.read_bytes())

        result = subprocess.run(
            ["systemd-analyze", "verify", "--root", str(tmp_path)]
            + [str(unit_dir / u.name) for u in _ALL_UNITS],
            capture_output=True,
            text=True,
        )

        bad = [
            line
            for line in (result.stdout + result.stderr).splitlines()
            if "unknown key name" in line.lower() or "failed to parse" in line.lower()
        ]
        assert not bad, "systemd-analyze verify found structural errors:\n" + "\n".join(bad)


class TestStructuralFallback:
    """Runs unconditionally (CI base without systemd-analyze) — belt and suspenders."""

    @pytest.mark.parametrize("unit", _ALL_UNITS, ids=lambda p: p.name)
    def test_section_headers_are_well_formed(self, unit: Path) -> None:
        for line in unit.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if s.startswith("["):
                assert s.endswith("]"), f"{unit.name}: malformed section header {s!r}"

    @pytest.mark.parametrize("unit", _SERVICE_UNITS, ids=lambda p: p.name)
    def test_service_units_declare_exec_start(self, unit: Path) -> None:
        assert "ExecStart=" in unit.read_text(encoding="utf-8")

    @pytest.mark.parametrize("unit", _PATH_UNITS, ids=lambda p: p.name)
    def test_path_units_declare_path_exists_and_unit(self, unit: Path) -> None:
        text = unit.read_text(encoding="utf-8")
        assert "PathExists=" in text
        assert "Unit=" in text


# ---------------------------------------------------------------------------
# 2. Loopback-only binds
# ---------------------------------------------------------------------------


class TestLoopbackOnlyBinds:
    def test_http_connect_proxy_is_loopback_only(self) -> None:
        assert "--outbound-http-proxy-listen=127.0.0.1:1055" in _TAILSCALED_TEXT

    def test_socks5_proxy_is_loopback_only(self) -> None:
        assert "--socks5-server=127.0.0.1:1056" in _TAILSCALED_TEXT

    def test_no_wildcard_bind_in_the_exec_start_directive(self) -> None:
        exec_start = next(
            line for line in _TAILSCALED_TEXT.splitlines() if line.strip().startswith("ExecStart=")
        )
        exec_block_start = _TAILSCALED_TEXT.index(exec_start)
        exec_block = _TAILSCALED_TEXT[exec_block_start:].split("Restart=", 1)[0]
        assert "0.0.0.0:" not in exec_block
        assert "[::]:" not in exec_block


class TestTailscaleddHardeningPresent:
    @pytest.mark.parametrize(
        "line",
        [
            "NoNewPrivileges=yes",
            "ProtectSystem=strict",
            "ProtectProc=invisible",
            "CapabilityBoundingSet=",
        ],
    )
    def test_directive_present_verbatim(self, line: str) -> None:
        assert _exact_line(_TAILSCALED_TEXT, line), (
            f"hermes-tailscaled.service lost hardening directive: {line!r}"
        )

    def test_read_write_paths_excludes_the_control_staging_dir(self) -> None:
        """tailscaled itself never touches the control script's staging dir —
        least privilege: only hermes-tailscale-control (root) writes there."""
        rw_line = next(
            line for line in _TAILSCALED_TEXT.splitlines() if line.startswith("ReadWritePaths=")
        )
        assert "/run/hermes/tailscale-control" not in rw_line

    def test_inaccessible_paths_hides_the_vault_master_key(self) -> None:
        assert "/var/lib/hermes/master.key" in _TAILSCALED_TEXT
        assert "/var/lib/hermes/keys" in _TAILSCALED_TEXT

    def test_restrict_address_families_includes_netlink(self) -> None:
        """Regression (fresh-image check, 2026-09-10): without AF_NETLINK the
        unit crash-loops on boot — netmon's NETLINK_ROUTE socket for the
        container's OWN interface state gets EAFNOSUPPORT ("netlinkrib:
        address family not supported by protocol"), so tailscaled never
        comes up. Verified live: adding AF_NETLINK fixed it, nothing else
        changed. A stricter list that breaks the daemon is not more secure."""
        af_line = next(
            line
            for line in _TAILSCALED_TEXT.splitlines()
            if line.startswith("RestrictAddressFamilies=")
        )
        assert "AF_NETLINK" in af_line


class TestControlServiceHardeningPresent:
    @pytest.mark.parametrize(
        "line",
        ["ProtectSystem=strict", "RestrictAddressFamilies=AF_UNIX", "PrivateTmp=yes"],
    )
    def test_directive_present_verbatim(self, line: str) -> None:
        assert _exact_line(_CONTROL_SERVICE_TEXT, line)

    def test_read_write_paths_cover_the_three_tailnet_dirs(self) -> None:
        rw_line = next(
            line
            for line in _CONTROL_SERVICE_TEXT.splitlines()
            if line.startswith("ReadWritePaths=")
        )
        for path in (
            "/run/hermes/tailscale-control",
            "/run/hermes/tailscale",
            "/var/lib/hermes/tailscale",
        ):
            assert path in rw_line, f"hermes-tailscale-control.service ReadWritePaths missing {path}"


class TestStatusWatcherWiring:
    """contracts.md §2/§7: a LOW-privilege sidecar owns status.json, bound to
    tailscaled's own lifecycle (never independently enabled)."""

    def test_tailscaled_wants_the_watcher(self) -> None:
        assert _exact_line(_TAILSCALED_TEXT, "Wants=hermes-tailscale-status-watcher.service")

    def test_watcher_binds_to_and_follows_tailscaled(self) -> None:
        assert _exact_line(_WATCHER_SERVICE_TEXT, "BindsTo=hermes-tailscaled.service")
        assert _exact_line(_WATCHER_SERVICE_TEXT, "After=hermes-tailscaled.service")

    def test_watcher_runs_as_the_low_privilege_tailscale_uid_not_root(self) -> None:
        assert _exact_line(_WATCHER_SERVICE_TEXT, "User=hermes-tailscale")
        assert _exact_line(_WATCHER_SERVICE_TEXT, "Group=hermes-tailscale")

    def test_watcher_has_zero_capabilities(self) -> None:
        assert _exact_line(_WATCHER_SERVICE_TEXT, "CapabilityBoundingSet=")

    def test_watcher_never_independently_enabled(self) -> None:
        assert not _exact_line(_WATCHER_SERVICE_TEXT, "[Install]")

    def test_watcher_cannot_read_the_control_staging_dir(self) -> None:
        """Least privilege: the watcher only ever reads tailscaled's own state —
        it has no business anywhere near the daemon's connect/disconnect staging."""
        inaccessible = next(
            line for line in _WATCHER_SERVICE_TEXT.splitlines() if line.startswith("InaccessiblePaths=")
        )
        assert "/run/hermes/tailscale-control" in inaccessible


class TestTmpfilesStatusJsonDirIsTraversable:
    """contracts.md §2 REQUIRED CHANGE: 0700 blocks traversal for every uid but
    the owner — status.json would be unreachable by the shell-server/egress-proxy
    no matter its own file mode. 0711 = owner rwx, execute-only for everyone else
    (traversal of a KNOWN path only — no readdir, no write)."""

    def test_run_hermes_tailscale_dir_is_0711(self) -> None:
        line = next(
            raw
            for raw in _TMPFILES_TEXT.splitlines()
            if raw.split()[:2] == ["d", "/run/hermes/tailscale"]
        )
        fields = line.split()
        assert fields[2] == "0711", f"expected mode 0711, got {fields[2]!r} in: {line!r}"

    def test_control_staging_dir_stays_0700(self) -> None:
        """Only the tailscale dir relaxed — the control staging dir (which holds
        the raw auth_key/password briefly) must stay owner-only."""
        line = next(
            raw
            for raw in _TMPFILES_TEXT.splitlines()
            if raw.split()[:2] == ["d", "/run/hermes/tailscale-control"]
        )
        assert line.split()[2] == "0700"


# ---------------------------------------------------------------------------
# 3. RED-TEAM INVARIANT — agent netns cannot reach the tailscaled proxies
# ---------------------------------------------------------------------------


class TestRedTeamInvariantAgentNetnsDropsTailnetLoopback:
    """plan.md §"Why it's safe": the agent netns MUST drop 127.0.0.0/8 +
    100.64.0.0/10 or it could dial tailscaled's loopback proxies directly,
    bypassing the audited egress allowlist entirely."""

    @pytest.mark.parametrize(
        "nft_text,name",
        [(_BROWSER_HOST_NFT, "browser-host.nft"), (_MCP_HOST_NFT, "mcp-host.nft")],
    )
    def test_loopback_and_cgnat_in_the_same_antipivot_set(self, nft_text: str, name: str) -> None:
        assert _ANTIPIVOT_DADDR_SET in nft_text, (
            f"{name}: lost the anti-pivot daddr set (127.0.0.0/8 + 100.64.0.0/10 "
            "must be dropped alongside RFC1918) — the tailnet loopback proxies "
            "would become reachable from the agent"
        )

    @pytest.mark.parametrize(
        "nft_text,name",
        [(_BROWSER_HOST_NFT, "browser-host.nft"), (_MCP_HOST_NFT, "mcp-host.nft")],
    )
    def test_forward_chain_policy_is_drop(self, nft_text: str, name: str) -> None:
        assert "type filter hook forward priority 0; policy drop;" in nft_text, (
            f"{name}: forward chain must default-deny (policy drop)"
        )


# ---------------------------------------------------------------------------
# 4. The units actually ship and actually get enabled
# ---------------------------------------------------------------------------


class TestContainerfileBakesAndEnablesTailnetControl:
    def test_control_script_is_copied_into_the_image(self) -> None:
        assert (
            "scripts/hermes-tailscale-control /usr/libexec/hermes/hermes-tailscale-control"
            in _CONTAINERFILE_TEXT
        ), "the script ships in ops/ but is never baked into the image"

    def test_status_watcher_script_is_copied_into_the_image(self) -> None:
        assert (
            "scripts/hermes-tailscale-status-watcher /usr/libexec/hermes/hermes-tailscale-status-watcher"
            in _CONTAINERFILE_TEXT
        ), "the status watcher ships in ops/ but is never baked into the image"

    def test_both_path_units_are_enabled_in_the_same_run_line(self) -> None:
        enable_line = next(
            line
            for line in _CONTAINERFILE_TEXT.splitlines()
            if line.strip().startswith("RUN systemctl enable")
            and "hermes-tailscaled.path" in line
        )
        assert "hermes-tailscale-control.path" in enable_line, (
            "hermes-tailscaled.path is enabled but hermes-tailscale-control.path "
            "is not — the daemon's connect/disconnect requests would never trigger"
        )
