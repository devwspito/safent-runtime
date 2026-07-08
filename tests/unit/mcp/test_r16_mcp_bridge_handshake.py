"""R16 (2026-07-07) regression tests — finish the Cerebro's MCP bridge handshake.

Root causes fixed (see dbus_runtime_service.py / hermes-mcp-launcher /
stdio_mcp_client.py module comments for the full story):

  1. `_MCP_BYOK_ENV_KEYS` rejected the WHOLE add_mcp_server draft when the
     cloud's McpSpec.env carried HOME/MCP_REMOTE_CONFIG_DIR/XDG_CONFIG_HOME —
     add_mcp_server never reached the scan/prefetch/connect steps at all, and
     the resulting {"ok": False, "error": "env inválido: ..."} was classified
     as a TRANSITORY failure (no "blocked" key) → last_applied_version never
     advanced, silently, forever.
  2. Even past that gate, the daemon always forwarded ITS OWN HOME
     (/var/lib/hermes/hermes-home — an InaccessiblePath for the MCP-jailed
     child) as a launcher override, clobbering the launcher's own correct,
     writable default (/var/lib/hermes/mcp-home). HOME is now launcher-owned
     (mirrors PATH) and never daemon/BYOK-overridable.
  3. A MANAGED_REMOTE server (mcp-remote bridging to OUR OWN cloud endpoint)
     had no path onto the MCP netns's default-deny egress allow-list —
     _grant_mcp_egress_for_managed_remote grants exactly ONE, locally-derived,
     SSRF-validated host (the paired instance_association.cloud_endpoint),
     never a bundle/argv-supplied one.

Covers:
  (a) _MCP_BYOK_ENV_KEYS accepts HOME/MCP_REMOTE_CONFIG_DIR/XDG_CONFIG_HOME.
  (b) add_mcp_server no longer hard-rejects a draft carrying those keys.
  (c) hermes-mcp-launcher: HOME is NOT in _ALLOWED_ENV_KEYS (not caller-
      overridable) but IS in _FORWARDED_ENV_KEYS (always forwarded from the
      launcher's own env) — mirrors the PATH precedent exactly.
  (d) hermes-mcp-launcher.service: NODE_OPTIONS carries --use-env-proxy.
  (e) stdio_mcp_client._build_mcp_env no longer forwards the daemon's own
      HOME into the launcher spawn request.
  (f) _grant_mcp_egress_for_managed_remote: no-op for a non-MANAGED_REMOTE
      server_id; grants exactly the paired cloud_endpoint host for
      safent-control; rejects an unsafe (SSRF) cloud_endpoint; no-op when
      unpaired; idempotent (does not re-push when the host is already
      granted).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_LAUNCHER_SCRIPT = (
    _REPO_ROOT / "ops" / "agents-os-edition" / "scripts" / "hermes-mcp-launcher"
)
_LAUNCHER_UNIT = (
    _REPO_ROOT / "ops" / "agents-os-edition" / "systemd" / "hermes-mcp-launcher.service"
)


# ---------------------------------------------------------------------------
# (a)(b) _MCP_BYOK_ENV_KEYS / _validate_mcp_env
# ---------------------------------------------------------------------------


class TestByokEnvKeysAcceptOAuthBridgeVars:
    def test_home_accepted(self) -> None:
        from hermes.agents_os.infrastructure.dbus_runtime_service import (
            _validate_mcp_env,
        )

        result = _validate_mcp_env({"HOME": "/var/lib/hermes"})
        assert result == {"HOME": "/var/lib/hermes"}

    def test_mcp_remote_config_dir_accepted(self) -> None:
        from hermes.agents_os.infrastructure.dbus_runtime_service import (
            _validate_mcp_env,
        )

        result = _validate_mcp_env(
            {"MCP_REMOTE_CONFIG_DIR": "/var/lib/hermes/.mcp-auth"}
        )
        assert result["MCP_REMOTE_CONFIG_DIR"] == "/var/lib/hermes/.mcp-auth"

    def test_xdg_config_home_accepted(self) -> None:
        from hermes.agents_os.infrastructure.dbus_runtime_service import (
            _validate_mcp_env,
        )

        result = _validate_mcp_env({"XDG_CONFIG_HOME": "/var/lib/hermes/.config"})
        assert result["XDG_CONFIG_HOME"] == "/var/lib/hermes/.config"

    def test_full_cerebro_env_dict_validates(self) -> None:
        """The exact shape the cloud's McpSpec.env carried in bundle v4 — this
        MUST validate (draft acceptance), even though the launcher will not
        honour HOME as an override (see TestLauncherHomeOwnership)."""
        from hermes.agents_os.infrastructure.dbus_runtime_service import (
            _validate_mcp_env,
        )

        result = _validate_mcp_env(
            {
                "HOME": "/var/lib/hermes",
                "MCP_REMOTE_CONFIG_DIR": "/var/lib/hermes/.mcp-auth",
                "XDG_CONFIG_HOME": "/var/lib/hermes/.config",
            }
        )
        assert set(result) == {"HOME", "MCP_REMOTE_CONFIG_DIR", "XDG_CONFIG_HOME"}

    def test_still_rejects_arbitrary_keys(self) -> None:
        """R16 only widens the allow-list by three named, bounded keys — it
        must not become a silent-accept-anything gate."""
        from hermes.agents_os.infrastructure.dbus_runtime_service import (
            _validate_mcp_env,
        )

        with pytest.raises(ValueError, match="clave de env no permitida"):
            _validate_mcp_env({"LD_PRELOAD": "/tmp/evil.so"})


def _make_wiring_with_mcp():
    from hermes.agents_os.infrastructure.dbus_runtime_service import (
        DbusRuntimeServiceWiring,
    )
    from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState

    class _FakeApprovalGate:
        async def register_pending(self, **_): ...
        async def approve(self, **_): return "tok"
        async def reject(self, **_): ...
        async def verify_token(self, **_): return False
        async def approved_token_for(self, _): return None

    class _FakeMcpServer:
        tools: list = []

    class _FakeMcpManager:
        async def connect(self, **_): return _FakeMcpServer()
        def health(self, _): return "unknown"
        async def list_tools(self, _): return []

    return DbusRuntimeServiceWiring(
        agent_state=InMemoryAgentState(),
        approval_gate=_FakeApprovalGate(),
        authorized_uids=frozenset({1000}),
        mcp_server_manager=_FakeMcpManager(),
    )


class TestAddMcpServerAcceptsOAuthBridgeEnv:
    """Regression: the draft that previously hard-failed with "env inválido"
    before ever reaching the scan/prefetch/connect pipeline now proceeds."""

    def test_cerebro_shaped_draft_passes_env_validation(self) -> None:
        import asyncio

        wiring = _make_wiring_with_mcp()
        draft = {
            "server_id": "safent-control",
            "label": "Safent Control",
            "argv": ["npx", "mcp-remote", "https://tenant.example.com/mcp"],
            "env": {
                "HOME": "/var/lib/hermes",
                "MCP_REMOTE_CONFIG_DIR": "/var/lib/hermes/.mcp-auth",
                "XDG_CONFIG_HOME": "/var/lib/hermes/.config",
            },
        }
        with (
            patch(
                "hermes.agents_os.infrastructure.dbus_runtime_service."
                "DbusRuntimeServiceWiring._scan_install_target",
                return_value=None,
            ),
            patch(
                "hermes.agents_os.infrastructure.dbus_runtime_service."
                "_prefetch_mcp_package",
                return_value=None,
            ),
            patch(
                "hermes.agents_os.infrastructure.dbus_runtime_service."
                "_grant_mcp_egress_for_managed_remote",
                return_value=None,
            ),
            patch(
                "hermes.agents_os.infrastructure.dbus_runtime_service."
                "_neus_write_mcp_entry",
                return_value=None,
            ),
        ):
            result = asyncio.run(
                wiring.add_mcp_server(draft_json=json.dumps(draft), sender_uid=1000)
            )
        # No longer "env inválido" — the draft proceeds to the connect step,
        # which fails soft in this unit test (no real MCP subprocess/manager
        # wiring) but MUST NOT be an env-validation error.
        if not result["ok"]:
            assert "env inválido" not in result.get("error", "")


# ---------------------------------------------------------------------------
# (c) hermes-mcp-launcher — HOME is launcher-owned, not caller-overridable
# ---------------------------------------------------------------------------


class TestLauncherHomeOwnership:
    def _launcher_src(self) -> str:
        return _LAUNCHER_SCRIPT.read_text(encoding="utf-8")

    def test_home_not_in_allowed_env_keys(self) -> None:
        """HOME must NOT be caller-overridable — mirrors the PATH precedent.
        A regression here reopens R16's root cause #2 (the daemon's own HOME,
        an InaccessiblePath for the MCP-jailed child, silently clobbering the
        launcher's correct default)."""
        namespace: dict = {}
        # Import-free static check: locate the frozenset literal boundaries.
        src = self._launcher_src()
        start = src.index("_ALLOWED_ENV_KEYS: frozenset[str] = frozenset({")
        end = src.index("})", start)
        block = src[start:end]
        assert '"HOME"' not in block, (
            "HOME must not be a caller-overridable launcher env key (R16)"
        )

    def test_home_in_forwarded_env_keys(self) -> None:
        """HOME must still be forwarded to the netns-jailed transient unit —
        just always from the launcher's OWN env, never the caller's."""
        src = self._launcher_src()
        start = src.index("_FORWARDED_ENV_KEYS: frozenset[str] = _ALLOWED_ENV_KEYS |")
        end = src.index("})", start)
        block = src[start:end]
        assert '"HOME"' in block

    def test_mcp_remote_config_dir_not_forwarded(self) -> None:
        """Deliberately asymmetric (R16): validated at the D-Bus gate but NOT
        forwarded by the launcher — it falls back to $HOME/.mcp-auth, which
        is writable once HOME is launcher-pinned. Forwarding the CLOUD's
        literal (currently unwritable) value would just move the EACCES."""
        src = self._launcher_src()
        start = src.index("_ALLOWED_ENV_KEYS: frozenset[str] = frozenset({")
        end = src.index("})", start)
        block = src[start:end]
        assert "MCP_REMOTE_CONFIG_DIR" not in block


class TestLauncherUnitUsesEnvProxy:
    def test_node_options_has_use_env_proxy(self) -> None:
        """Node's native fetch()/undici does not honour http(s)_proxy env vars
        without --use-env-proxy — a fetch()-based MCP (mcp-remote) got
        `getaddrinfo EAI_AGAIN` under the MCP netns's proxy-only DNS model."""
        content = _LAUNCHER_UNIT.read_text(encoding="utf-8")
        assert "--use-env-proxy" in content
        assert "--dns-result-order=ipv4first" in content  # pre-existing flag kept


# ---------------------------------------------------------------------------
# (e) stdio_mcp_client._build_mcp_env no longer forwards the daemon's HOME
# ---------------------------------------------------------------------------


class TestStdioMcpClientDoesNotForwardDaemonHome:
    def test_home_absent_even_when_set_in_os_environ(self, monkeypatch) -> None:
        from hermes.mcp.infrastructure.stdio_mcp_client import _build_mcp_env

        monkeypatch.setenv("HOME", "/var/lib/hermes/hermes-home")
        monkeypatch.setenv("npm_config_cache", "/var/lib/hermes/npm-cache")
        env = _build_mcp_env()
        assert "HOME" not in env
        assert env.get("npm_config_cache") == "/var/lib/hermes/npm-cache"


# ---------------------------------------------------------------------------
# (f) _grant_mcp_egress_for_managed_remote
# ---------------------------------------------------------------------------


_ASSOC_SCHEMA = """
CREATE TABLE instance_association (
  id INTEGER PRIMARY KEY CHECK(id = 1),
  instance_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL,
  paired_at TEXT NOT NULL,
  cloud_endpoint TEXT NOT NULL,
  signing_pubkey_hex TEXT NOT NULL DEFAULT '',
  license_json TEXT NOT NULL DEFAULT '{}',
  last_applied_version INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'active',
  instance_secret_ciphertext BLOB,
  directory_json TEXT NOT NULL DEFAULT ''
);
"""


def _seed_association_db(db_path: Path, *, cloud_endpoint: str, state: str = "active") -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(_ASSOC_SCHEMA)
        conn.execute(
            "INSERT INTO instance_association "
            "(id, instance_id, tenant_id, paired_at, cloud_endpoint, state) "
            "VALUES (1, 'inst-1', 'tenant-1', '2026-07-07T00:00:00Z', ?, ?)",
            (cloud_endpoint, state),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def egress_env(tmp_path, monkeypatch):
    """Isolate HERMES_SHELL_DB + the MCP grants file for each test."""
    db_path = tmp_path / "shell-state.db"
    grants_path = tmp_path / "mcp-egress-grants.json"
    monkeypatch.setenv("HERMES_SHELL_DB", str(db_path))
    with patch("hermes.shell_server.egress_api._MCP_GRANTS_PATH", grants_path):
        yield db_path, grants_path


class TestGrantMcpEgressForManagedRemote:
    def test_noop_for_non_managed_remote_server(self, egress_env) -> None:
        db_path, grants_path = egress_env
        _seed_association_db(db_path, cloud_endpoint="https://tenant.example.com")
        from hermes.agents_os.infrastructure.dbus_runtime_service import (
            _grant_mcp_egress_for_managed_remote,
        )

        _grant_mcp_egress_for_managed_remote("excel")
        assert not grants_path.exists()

    def test_grants_the_paired_cloud_endpoint_host(self, egress_env) -> None:
        db_path, grants_path = egress_env
        _seed_association_db(db_path, cloud_endpoint="https://tenant.example.com:8443")
        from hermes.agents_os.infrastructure.dbus_runtime_service import (
            _grant_mcp_egress_for_managed_remote,
        )

        with patch(
            "hermes.shell_server.egress_api._push_session", return_value=True
        ) as push:
            _grant_mcp_egress_for_managed_remote("safent-control")
        assert grants_path.exists()
        assert json.loads(grants_path.read_text()) == {
            "domains": ["tenant.example.com"]
        }
        push.assert_called_once()

    def test_idempotent_no_repush_when_already_granted(self, egress_env) -> None:
        db_path, grants_path = egress_env
        _seed_association_db(db_path, cloud_endpoint="https://tenant.example.com")
        grants_path.write_text(json.dumps({"domains": ["tenant.example.com"]}))
        from hermes.agents_os.infrastructure.dbus_runtime_service import (
            _grant_mcp_egress_for_managed_remote,
        )

        with patch(
            "hermes.shell_server.egress_api._push_session", return_value=True
        ) as push:
            _grant_mcp_egress_for_managed_remote("safent-control")
        push.assert_not_called()

    def test_noop_when_unpaired_no_association_db(self, egress_env) -> None:
        _db_path, grants_path = egress_env  # DB never created — unpaired instance
        from hermes.agents_os.infrastructure.dbus_runtime_service import (
            _grant_mcp_egress_for_managed_remote,
        )

        _grant_mcp_egress_for_managed_remote("safent-control")
        assert not grants_path.exists()

    def test_rejects_unsafe_cloud_endpoint_ssrf(self, egress_env) -> None:
        db_path, grants_path = egress_env
        _seed_association_db(db_path, cloud_endpoint="https://169.254.169.254/mcp")
        from hermes.agents_os.infrastructure.dbus_runtime_service import (
            _grant_mcp_egress_for_managed_remote,
        )

        with patch(
            "hermes.shell_server.egress_api._push_session", return_value=True
        ) as push:
            _grant_mcp_egress_for_managed_remote("safent-control")
        assert not grants_path.exists()
        push.assert_not_called()

    def test_never_trusts_argv_supplied_host(self, egress_env) -> None:
        """The grant is derived ONLY from the locally-paired cloud_endpoint —
        server_id/argv carry no host, by construction (defense against a
        hypothetically-malformed bundle trying to widen egress via argv)."""
        db_path, grants_path = egress_env
        _seed_association_db(db_path, cloud_endpoint="https://tenant.example.com")
        from hermes.agents_os.infrastructure.dbus_runtime_service import (
            _grant_mcp_egress_for_managed_remote,
        )

        with patch("hermes.shell_server.egress_api._push_session", return_value=True):
            _grant_mcp_egress_for_managed_remote("safent-control")
        assert json.loads(grants_path.read_text()) == {
            "domains": ["tenant.example.com"]
        }
