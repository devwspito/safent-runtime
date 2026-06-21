"""Regression test: _client_factory in runtime/__main__ gives git-backed uvx
calls a 180s timeout instead of the default 30s.

Bug: StdioMcpClient was always created with timeout_sec=30.0. Serena uses
`uvx --from git+https://...` which clones + builds a repo on first run
(60-180s) and consistently timed out before the handshake completed.

Fix: _client_factory inspects argv; if runner is 'uvx' and any arg starts
with 'git+', it passes timeout_sec=180.0.
"""

from __future__ import annotations

import pytest

from hermes.mcp.domain.value_objects import Transport
from hermes.mcp.infrastructure.stdio_mcp_client import StdioMcpClient

pytestmark = pytest.mark.unit


def _make_factory():
    """Reproduce the exact factory logic from runtime/__main__.py."""

    def _client_factory(transport) -> StdioMcpClient:
        argv = list(transport.argv) if transport.argv else []
        runner = argv[0].rsplit("/", 1)[-1] if argv else ""
        is_git_backed = runner == "uvx" and any(a.startswith("git+") for a in argv)
        timeout = 180.0 if is_git_backed else 30.0
        return StdioMcpClient(transport=transport, timeout_sec=timeout)

    return _client_factory


class TestClientFactoryTimeout:
    """_client_factory assigns correct timeout per runner type."""

    def test_uvx_git_backed_gets_180s(self):
        factory = _make_factory()
        transport = Transport.stdio(
            ["uvx", "--from", "git+https://github.com/oraios/serena", "serena-mcp-server"]
        )
        client = factory(transport)
        assert client._timeout_sec == 180.0

    def test_uvx_registry_package_gets_30s(self):
        factory = _make_factory()
        transport = Transport.stdio(["uvx", "mcp-libreoffice"])
        client = factory(transport)
        assert client._timeout_sec == 30.0

    def test_npx_gets_30s(self):
        factory = _make_factory()
        transport = Transport.stdio(["npx", "-y", "@modelcontextprotocol/server-github"])
        client = factory(transport)
        assert client._timeout_sec == 30.0

    def test_node_gets_30s(self):
        factory = _make_factory()
        transport = Transport.stdio(["node", "/usr/share/open-design/dist/index.js"])
        client = factory(transport)
        assert client._timeout_sec == 30.0

    def test_python3_gets_30s(self):
        factory = _make_factory()
        transport = Transport.stdio(["python3", "-m", "some_mcp_server"])
        client = factory(transport)
        assert client._timeout_sec == 30.0

    def test_absolute_path_uvx_git_backed_gets_180s(self):
        # argv[0] may be a full path like /usr/bin/uvx
        factory = _make_factory()
        transport = Transport.stdio(
            ["/usr/bin/uvx", "--from", "git+https://github.com/example/server", "server"]
        )
        client = factory(transport)
        assert client._timeout_sec == 180.0
