"""The companion is reached DIRECTLY, so it must bypass the egress proxy (024).

The companion's whole network design is a direct path: ONE `ip daddr` + `tcp
dport` accept and a scoped masquerade in mcp-host.nft. The egress proxy's pinned
policy (default-deny, no registries) does not list it, so tunnelling there
answers 403.

That became load-bearing when the MCP launcher unit's NODE_OPTIONS gained
`--use-env-proxy` (Node >= 24): node's global fetch then honours http(s)_proxy
for EVERY request, so mcp-remote stopped taking its direct route and the
handshake died with

    Connection error: TypeError: fetch failed
      cause: RequestAbortedError [AbortError]: Proxy response (403) !== 200 when HTTP Tunneling

leaving the seeded server `disconnected` / `esperando_servicio` forever. node's
--use-env-proxy honours no_proxy; these tests pin that counterpart AND its
narrowness (a proxy-bypass list is an audit hole if it is ever widened).
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_LAUNCHER = (
    Path(__file__).resolve().parents[3] / "ops/agents-os-edition/scripts/hermes-mcp-launcher"
).read_text(encoding="utf-8")


class TestCompanionBypassesTheEgressProxy:
    def test_no_proxy_is_set_in_both_cases(self) -> None:
        assert "--setenv=no_proxy={_COMPANION_NO_PROXY}" in _LAUNCHER
        assert "--setenv=NO_PROXY={_COMPANION_NO_PROXY}" in _LAUNCHER

    def test_bypass_list_is_the_exact_companion_host_and_ip(self) -> None:
        assert '_COMPANION_NO_PROXY = "ads.safent.internal,10.201.0.10"' in _LAUNCHER

    def test_bypass_never_widens_to_a_suffix_or_a_subnet(self) -> None:
        line = next(ln for ln in _LAUNCHER.splitlines() if ln.startswith("_COMPANION_NO_PROXY"))
        assert ".safent.internal," not in line.replace("ads.safent.internal,", "")
        assert "/24" not in line
        assert "*" not in line
        assert "localhost" not in line

    def test_it_is_set_with_the_proxy_vars_so_the_caller_cannot_override_it(self) -> None:
        """Both must live in the same authoritative `cmd.extend` block appended
        LAST — a caller-settable bypass list would let any MCP escape the audit."""
        block = _LAUNCHER.split("# Egress proxy vars LAST", 1)[1].split("cmd.append(\"--\")", 1)[0]
        for key in ("http_proxy", "https_proxy", "no_proxy", "NO_PROXY"):
            assert f"--setenv={key}=" in block

    def test_no_proxy_is_not_a_caller_overridable_env_key(self) -> None:
        """MCP-05: the launcher's env gate is now a validated pattern
        (`_is_allowed_env_key`), not a fixed frozenset — NO_PROXY is
        explicitly deny-listed there, and no_proxy (lowercase) can never
        match the pattern (upper-case-first) in the first place."""
        module = _load_launcher_module()
        assert module._is_allowed_env_key("no_proxy") is False
        assert module._is_allowed_env_key("NO_PROXY") is False


def _load_launcher_module():
    import importlib.machinery
    import importlib.util

    launcher_path = Path(__file__).resolve().parents[3] / "ops/agents-os-edition/scripts/hermes-mcp-launcher"
    loader = importlib.machinery.SourceFileLoader("hermes_mcp_launcher_no_proxy_test", str(launcher_path))
    spec = importlib.util.spec_from_file_location(
        "hermes_mcp_launcher_no_proxy_test", launcher_path, loader=loader
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
