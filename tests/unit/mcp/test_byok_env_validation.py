"""Regression tests for BYOK env plumbing — _validate_mcp_env + Transport.stdio.

Covers:
  (a) Transport.stdio accepts and stores env; immutable after construction.
  (b) _validate_mcp_env: happy-path with valid OD_* keys.
  (c) _validate_mcp_env: rejects unknown keys (not silently dropped).
  (d) _validate_mcp_env: rejects non-string keys.
  (e) _validate_mcp_env: rejects empty string values.
  (f) _validate_mcp_env: OD_DAEMON_URL must be http(s).
  (g) _validate_mcp_env: OD_DAEMON_URL with file:// rejected.
  (h) _validate_mcp_env: OD_DAEMON_URL without netloc rejected.
  (i) _validate_mcp_env: empty dict is valid (no env = no BYOK).
  (j) _validate_mcp_env: non-dict input raises.
  (k) add_mcp_server: valid env accepted and persisted in config entry.
  (l) add_mcp_server: unknown env key rejected with ok:False.
  (m) add_mcp_server: invalid OD_DAEMON_URL scheme rejected.
  (n) add_mcp_server: token NOT logged in clear (log sanitisation).
  (o) Transport.env is a MappingProxyType (immutable, not a plain dict).
  (p) reconnect_persisted_mcp_servers passes stored env to _mcp_connect.

NOTE: TestAddMcpServerEnv patches _scan_install_target to return None (fail-open)
so that the security-center infrastructure (SQLite repos, /var/lib/hermes) is
never touched in unit tests. The security gate itself is tested separately in
tests/security_center/.
"""

from __future__ import annotations

import asyncio
import json
import logging
import textwrap
from types import MappingProxyType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from hermes.mcp.domain.value_objects import Transport

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# (a)(o) Transport.stdio with env
# ---------------------------------------------------------------------------


class TestTransportEnv:
    def test_stdio_with_env_stores_values(self) -> None:
        t = Transport.stdio(["npx", "-y", "open-design-mcp"],
                            env={"OD_DAEMON_URL": "http://localhost:3000"})
        assert t.env["OD_DAEMON_URL"] == "http://localhost:3000"

    def test_stdio_without_env_defaults_to_empty(self) -> None:
        t = Transport.stdio(["npx", "-y", "open-design-mcp"])
        assert dict(t.env) == {}

    def test_env_is_immutable_proxy(self) -> None:
        t = Transport.stdio(["npx", "-y", "open-design-mcp"],
                            env={"OD_DAEMON_URL": "http://localhost:3000"})
        assert isinstance(t.env, MappingProxyType)
        with pytest.raises(TypeError):
            t.env["OD_DAEMON_URL"] = "http://evil.example.com"  # type: ignore[index]

    def test_plain_dict_coerced_to_proxy_on_direct_construction(self) -> None:
        # When constructed directly (not via .stdio()), coercion still applies.
        t = Transport(argv=("npx",), env={"OD_DAEMON_URL": "http://host:1"})
        assert isinstance(t.env, MappingProxyType)


# ---------------------------------------------------------------------------
# _validate_mcp_env helper — import from infrastructure module
# ---------------------------------------------------------------------------


@pytest.fixture
def _validate():
    """Return the validate function from the infrastructure module."""
    from hermes.agents_os.infrastructure.dbus_runtime_service import (
        _validate_mcp_env,
    )
    return _validate_mcp_env


class TestValidateMcpEnv:
    def test_empty_dict_is_valid(self, _validate) -> None:
        assert _validate({}) == {}

    def test_valid_od_daemon_url_only(self, _validate) -> None:
        result = _validate({"OD_DAEMON_URL": "http://localhost:3000"})
        assert result["OD_DAEMON_URL"] == "http://localhost:3000"

    def test_all_valid_od_keys_accepted(self, _validate) -> None:
        env = {
            "OD_DAEMON_URL": "https://od.example.com",
            "OD_API_TOKEN": "secret-token-xyz",
            "OD_AUTH_MODE": "bearer",
            "OD_BASIC_USER": "admin",
            "OD_BASIC_PASS": "pass123",
        }
        result = _validate(env)
        assert result == env

    def test_unknown_key_raises_not_drops(self, _validate) -> None:
        with pytest.raises(ValueError, match="clave de env no permitida"):
            _validate({"ARBITRARY_KEY": "value"})

    def test_unknown_key_with_valid_key_raises(self, _validate) -> None:
        with pytest.raises(ValueError, match="clave de env no permitida"):
            _validate({
                "OD_DAEMON_URL": "http://localhost:3000",
                "MALICIOUS_KEY": "injected",
            })

    def test_non_string_key_raises(self, _validate) -> None:
        with pytest.raises(ValueError, match="clave de env no es string"):
            _validate({123: "value"})  # type: ignore[dict-item]

    def test_empty_string_value_raises(self, _validate) -> None:
        with pytest.raises(ValueError, match="debe ser string no vacío"):
            _validate({"OD_DAEMON_URL": ""})

    def test_non_string_value_raises(self, _validate) -> None:
        with pytest.raises(ValueError, match="debe ser string no vacío"):
            _validate({"OD_API_TOKEN": 12345})  # type: ignore[dict-item]

    def test_od_daemon_url_https_valid(self, _validate) -> None:
        result = _validate({"OD_DAEMON_URL": "https://design.corp.example.com"})
        assert result["OD_DAEMON_URL"] == "https://design.corp.example.com"

    def test_od_daemon_url_file_scheme_rejected(self, _validate) -> None:
        with pytest.raises(ValueError, match="http o https"):
            _validate({"OD_DAEMON_URL": "file:///etc/passwd"})

    def test_od_daemon_url_data_scheme_rejected(self, _validate) -> None:
        with pytest.raises(ValueError, match="http o https"):
            _validate({"OD_DAEMON_URL": "data:text/plain,hello"})

    def test_od_daemon_url_bare_hostname_rejected(self, _validate) -> None:
        # No scheme → urlparse puts everything in path, netloc is empty.
        with pytest.raises(ValueError):
            _validate({"OD_DAEMON_URL": "localhost:3000"})

    def test_non_dict_input_raises(self, _validate) -> None:
        with pytest.raises(ValueError, match="diccionario"):
            _validate("OD_DAEMON_URL=http://localhost")  # type: ignore[arg-type]

    def test_od_api_token_passthrough_without_url_validation(self, _validate) -> None:
        # Token is opaque; any non-empty string is accepted.
        result = _validate({"OD_API_TOKEN": "tok_any_chars_!@#$"})
        assert result["OD_API_TOKEN"] == "tok_any_chars_!@#$"


# ---------------------------------------------------------------------------
# add_mcp_server env handling — functional tests against DbusRuntimeServiceWiring
# ---------------------------------------------------------------------------


def _make_wiring_with_mcp():
    """Return a DbusRuntimeServiceWiring with a minimal MCP manager fake."""
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


# Context manager that patches _scan_install_target to return None (fail-open).
# This avoids touching the security-center SQLite infrastructure in unit tests.
_patch_scan = patch(
    "hermes.agents_os.infrastructure.dbus_runtime_service."
    "DbusRuntimeServiceWiring._scan_install_target",
    return_value=None,
)

# C1 PASS-3: add_mcp_server now PRE-FETCHES the scanned package into the shared runner
# cache (npm cache add / uv tool install) so the runtime can spawn OFFLINE. That step
# hits the real npm/uv + /var/lib/hermes, which unit tests must not do — patch it to a
# no-op (the prefetch is exercised separately; here we test env validation + persistence).
_patch_prefetch = patch(
    "hermes.agents_os.infrastructure.dbus_runtime_service._prefetch_mcp_package",
    return_value=None,
)


class TestAddMcpServerEnv:
    """Integration-style tests for add_mcp_server env validation path.

    The security scanner is patched to return None (fail-open) so these tests
    run without D-Bus, network, or /var/lib/hermes. Env validation and
    persistence are exercised in isolation.
    """

    def test_valid_env_accepted_and_persisted(self, tmp_path, monkeypatch) -> None:
        config_file = tmp_path / "mcp-servers.json"
        monkeypatch.setenv("HERMES_MCP_CONFIG", str(config_file))

        wiring = _make_wiring_with_mcp()
        draft = {
            "server_id": "open-design",
            "label": "Open Design",
            "argv": ["npx", "-y", "open-design-mcp"],
            "env": {
                "OD_DAEMON_URL": "http://localhost:3000",
                "OD_API_TOKEN": "secret",
            },
        }
        with _patch_scan, _patch_prefetch:
            result = asyncio.run(
                wiring.add_mcp_server(draft_json=json.dumps(draft), sender_uid=1000)
            )
        assert result["ok"] is True

        # Persisted entry must include env.
        entries = json.loads(config_file.read_text())
        assert len(entries) == 1
        saved = entries[0]
        assert saved["server_id"] == "open-design"
        assert saved["env"]["OD_DAEMON_URL"] == "http://localhost:3000"
        assert saved["env"]["OD_API_TOKEN"] == "secret"

    def test_unknown_env_key_returns_error(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("HERMES_MCP_CONFIG", str(tmp_path / "mcp-servers.json"))

        wiring = _make_wiring_with_mcp()
        draft = {
            "server_id": "open-design",
            "label": "Open Design",
            "argv": ["npx", "-y", "open-design-mcp"],
            "env": {"ARBITRARY_INJECT": "evil"},
        }
        # Env validation happens BEFORE the security scan — no patch needed here.
        result = asyncio.run(
            wiring.add_mcp_server(draft_json=json.dumps(draft), sender_uid=1000)
        )
        assert result["ok"] is False
        assert "env inválido" in result["error"]
        assert "ARBITRARY_INJECT" in result["error"]

    def test_invalid_od_daemon_url_scheme_returns_error(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("HERMES_MCP_CONFIG", str(tmp_path / "mcp-servers.json"))

        wiring = _make_wiring_with_mcp()
        draft = {
            "server_id": "open-design",
            "label": "Open Design",
            "argv": ["npx", "-y", "open-design-mcp"],
            "env": {"OD_DAEMON_URL": "file:///etc/passwd"},
        }
        result = asyncio.run(
            wiring.add_mcp_server(draft_json=json.dumps(draft), sender_uid=1000)
        )
        assert result["ok"] is False
        assert "env inválido" in result["error"]

    def test_no_env_field_works_as_before(self, tmp_path, monkeypatch) -> None:
        """Serena and other servers without env still install normally."""
        monkeypatch.setenv("HERMES_MCP_CONFIG", str(tmp_path / "mcp-servers.json"))

        wiring = _make_wiring_with_mcp()
        draft = {
            "server_id": "serena",
            "label": "Serena",
            "argv": ["uvx", "--from",
                     "git+https://github.com/oraios/serena",
                     "serena", "start-mcp-server"],
        }
        with _patch_scan, _patch_prefetch:
            result = asyncio.run(
                wiring.add_mcp_server(draft_json=json.dumps(draft), sender_uid=1000)
            )
        assert result["ok"] is True

        entries = json.loads((tmp_path / "mcp-servers.json").read_text())
        assert entries[0].get("env") is None  # no env key saved when absent

    def test_token_not_logged_in_clear(
        self, tmp_path, monkeypatch, caplog
    ) -> None:
        """OD_API_TOKEN value must never appear in log output."""
        monkeypatch.setenv("HERMES_MCP_CONFIG", str(tmp_path / "mcp-servers.json"))

        wiring = _make_wiring_with_mcp()
        secret_token = "super-secret-bearer-token-DO-NOT-LOG"
        draft = {
            "server_id": "open-design",
            "label": "Open Design",
            "argv": ["npx", "-y", "open-design-mcp"],
            "env": {
                "OD_DAEMON_URL": "http://localhost:3000",
                "OD_API_TOKEN": secret_token,
            },
        }
        with _patch_scan, _patch_prefetch, caplog.at_level(logging.DEBUG, logger="hermes"):
            asyncio.run(
                wiring.add_mcp_server(draft_json=json.dumps(draft), sender_uid=1000)
            )

        # The raw token value must not appear in any log record.
        for record in caplog.records:
            assert secret_token not in record.getMessage(), (
                f"Token leaked in log at level {record.levelname}: {record.getMessage()}"
            )


# ---------------------------------------------------------------------------
# (p) reconnect_persisted_mcp_servers passes stored env
# ---------------------------------------------------------------------------


class TestReconnectPassesEnv:
    def test_reconnect_forwards_stored_env(self, tmp_path, monkeypatch) -> None:
        """Entries with persisted env must be forwarded to _mcp_connect on boot."""
        from hermes.agents_os.infrastructure import dbus_runtime_service as svc_mod

        config = [
            {
                "server_id": "open-design",
                "label": "Open Design",
                "argv": ["npx", "-y", "open-design-mcp"],
                "env": {
                    "OD_DAEMON_URL": "http://localhost:3000",
                    "OD_API_TOKEN": "stored-secret",
                },
            },
            {
                "server_id": "serena",
                "label": "Serena",
                "argv": ["uvx", "--from", "git+...", "serena", "start-mcp-server"],
                # no env key
            },
        ]
        config_file = tmp_path / "mcp-servers.json"
        config_file.write_text(json.dumps(config))
        monkeypatch.setenv("HERMES_MCP_CONFIG", str(config_file))

        captured_calls: list[dict] = []

        async def _fake_mcp_connect(manager, server_id, argv, *, env=None):
            captured_calls.append({"server_id": server_id, "env": dict(env or {})})

            class _FakeServer:
                tools: list = []
            return _FakeServer()

        with patch.object(svc_mod, "_mcp_connect", side_effect=_fake_mcp_connect):
            asyncio.run(svc_mod.reconnect_persisted_mcp_servers(object()))

        assert len(captured_calls) == 2
        od_call = next(c for c in captured_calls if c["server_id"] == "open-design")
        assert od_call["env"]["OD_DAEMON_URL"] == "http://localhost:3000"
        assert od_call["env"]["OD_API_TOKEN"] == "stored-secret"

        serena_call = next(c for c in captured_calls if c["server_id"] == "serena")
        assert serena_call["env"] == {}  # no env stored, empty dict forwarded
