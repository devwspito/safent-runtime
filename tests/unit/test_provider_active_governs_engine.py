"""Regression tests — specs/025-safent-repaso hallazgo #1 ("el proveedor
'activo' no gobierna el motor").

Root causes fixed:
  1. ``configure_native_provider`` wrote model.provider/model.default to
     config.yaml UNCONDITIONALLY — even when the caller only wanted to save
     a key (set_active=False). Any "configure" silently hijacked the engine's
     active provider, defeating the UI's "Activar" toggle.
  2. The REST/D-Bus adapter (``ConfigureNativeProvider``) dropped the
     ``set_active`` field from the request body entirely before it ever
     reached the daemon method — so the flag had NO effect end-to-end.
  3. ``set_active_provider`` (``POST /providers/{id}/activate``) only
     accepted SQL-repo UUIDs — reactivating an already-configured NATIVE
     catalogue provider (e.g. "anthropic", "gemini") by id crashed with
     ValueError, so a user could never switch BACK to a previously
     configured native provider without re-pasting its API key.
  4. The engine's own resolved-runtime cache
     (``hermes.runtime.nous_engine._RUNTIME_PROVIDER_CACHE``) had a 30s TTL
     with NO invalidation hook on provider switch, so even a CORRECT
     config.yaml write could take up to 30s to reach the next chat.

Coverage:
  A. DbusRuntimeServiceWiring.configure_native_provider — set_active gating,
     fail-loud on set_active without model, key isolation (os.environ only
     gets the ACTIVE provider's key).
  B. DbusRuntimeServiceWiring.set_active_provider — native (non-UUID) ids:
     switch A -> B -> A restores A's own remembered model; unknown/
     unconfigured native ids fail soft instead of crashing.
  C. hermes.runtime.nous_engine.clear_runtime_provider_cache — actually
     empties the module-level cache.
  D. Integration-style: the REST router (create_providers_router) forwards
     `set_active` from the POST body all the way into the D-Bus mutator call
     (fake proxy), end to end.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

pytestmark = pytest.mark.unit

_DBUS_MODULE = "hermes.agents_os.infrastructure.dbus_runtime_service"


def _make_wiring(tmp_path: Path, *, active_provider_service: Any | None = None) -> Any:
    from hermes.agents_os.infrastructure.dbus_runtime_service import (
        DbusRuntimeServiceWiring,
    )
    from hermes.shell_server.providers.repo import SQLiteProviderRepository
    from hermes.shell_server.security.secrets import SecretsVault
    from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState

    class _NullGate:
        async def register_pending(self, *, proposal_id, **_) -> None: ...
        async def approve(self, *, proposal_id, approved_by) -> str:
            return ""
        async def reject(self, *, proposal_id, rejected_by, reason) -> None: ...
        async def verify_token(self, *, proposal_id, token) -> bool:
            return False
        async def approved_token_for(self, proposal_id) -> str | None:
            return None

    vault = SecretsVault(master_key=os.urandom(32))
    repo = SQLiteProviderRepository(db_path=tmp_path / "p.db", vault=vault)
    return DbusRuntimeServiceWiring(
        agent_state=InMemoryAgentState(),
        approval_gate=_NullGate(),
        authorized_uids=frozenset({1000}),
        provider_repo=repo,
        active_provider_service=active_provider_service,
    )


_FAKE_REGISTRY = {
    "openai-api": MagicMock(api_key_env_vars=("OPENAI_API_KEY",), base_url_env_var="OPENAI_BASE_URL",
                             auth_type="api_key"),
    "anthropic": MagicMock(api_key_env_vars=("ANTHROPIC_API_KEY",), base_url_env_var="",
                            auth_type="api_key"),
}


@pytest.fixture()
def _hermes_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point HERMES_HOME at an isolated dir — _write_hermes_env/_read_hermes_env/
    the native-provider-model memory are plain-file helpers with no hermes_cli
    dependency, so they run for REAL against this dir in the tests below."""
    home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def _isolate_provider_envs():
    """Tests inject OPENAI_API_KEY/ANTHROPIC_API_KEY into the live process env
    (that's the behavior under test) — never let that leak across tests."""
    names = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY")
    saved = {n: os.environ.pop(n, None) for n in names}
    yield
    for n, v in saved.items():
        if v is None:
            os.environ.pop(n, None)
        else:
            os.environ[n] = v


# ---------------------------------------------------------------------------
# A. configure_native_provider — set_active gating + key isolation
# ---------------------------------------------------------------------------


class TestConfigureNativeProviderSetActiveGating:
    def test_set_active_false_does_not_touch_config_or_live_env(
        self, tmp_path: Path, _hermes_home: Path
    ) -> None:
        wiring = _make_wiring(tmp_path)
        with (
            patch(f"{_DBUS_MODULE}._write_hermes_model_config") as mock_write_model,
            patch(f"{_DBUS_MODULE}._clear_engine_runtime_cache") as mock_clear_cache,
            patch.dict("sys.modules", {"hermes_cli.auth": MagicMock(PROVIDER_REGISTRY=_FAKE_REGISTRY)}),
        ):
            result = wiring.configure_native_provider(
                provider_id="openai-api", api_key="sk-a", model="gpt-4.1-mini",
                base_url="", sender_uid=1000, set_active=False,
            )

        assert result == {"ok": True, "provider_id": "openai-api"}
        # Key persisted to .env (so a LATER activate doesn't need it re-pasted)...
        assert (_hermes_home / ".env").read_text(encoding="utf-8").strip() == "OPENAI_API_KEY=sk-a"
        # ...but the engine's active model must NOT flip, and the key must NOT
        # reach the live process env (least-privilege: unrelated providers
        # stay out of os.environ until actually activated).
        mock_write_model.assert_not_called()
        mock_clear_cache.assert_not_called()
        assert "OPENAI_API_KEY" not in os.environ

    def test_set_active_true_writes_config_and_injects_live_env(
        self, tmp_path: Path, _hermes_home: Path
    ) -> None:
        active_svc = MagicMock()
        wiring = _make_wiring(tmp_path, active_provider_service=active_svc)
        with (
            patch(f"{_DBUS_MODULE}._write_hermes_model_config") as mock_write_model,
            patch(f"{_DBUS_MODULE}._clear_engine_runtime_cache") as mock_clear_cache,
            patch.dict("sys.modules", {"hermes_cli.auth": MagicMock(PROVIDER_REGISTRY=_FAKE_REGISTRY)}),
        ):
            result = wiring.configure_native_provider(
                provider_id="openai-api", api_key="sk-a", model="gpt-4.1-mini",
                base_url="", sender_uid=1000, set_active=True,
            )

        assert result == {"ok": True, "provider_id": "openai-api"}
        mock_write_model.assert_called_once_with("openai-api", "gpt-4.1-mini", "")
        assert os.environ["OPENAI_API_KEY"] == "sk-a"
        active_svc.force_refresh.assert_called_once()
        mock_clear_cache.assert_called_once()

    def test_set_active_true_without_model_fails_loud_instead_of_inheriting(
        self, tmp_path: Path, _hermes_home: Path
    ) -> None:
        """Root cause of the matrix's 'heredaron default_model: claude-sonnet-4-5':
        activating with an empty model used to silently keep the PREVIOUS
        provider's model string. Must fail instead of writing a broken pair."""
        wiring = _make_wiring(tmp_path)
        with (
            patch(f"{_DBUS_MODULE}._write_hermes_model_config") as mock_write_model,
            patch(f"{_DBUS_MODULE}._write_hermes_env") as mock_write_env,
            patch.dict("sys.modules", {"hermes_cli.auth": MagicMock(PROVIDER_REGISTRY=_FAKE_REGISTRY)}),
        ):
            result = wiring.configure_native_provider(
                provider_id="openai-api", api_key="sk-a", model="",
                base_url="", sender_uid=1000, set_active=True,
            )

        assert result == {"ok": False, "error": "model requerido para activar"}
        mock_write_model.assert_not_called()
        mock_write_env.assert_not_called()


# ---------------------------------------------------------------------------
# A2. list_native_providers — curated default_model suggestion (PROV-02)
# ---------------------------------------------------------------------------


class TestListNativeProvidersDefaultModel:
    def test_curated_ids_carry_a_default_model(self, tmp_path: Path, _hermes_home: Path) -> None:
        wiring = _make_wiring(tmp_path)
        # NOTE: MagicMock(name=...) reserves `name` for the mock's own repr —
        # it does NOT set a `.name` attribute. Assign it after construction so
        # list_native_providers' `getattr(cfg, "name", pid)` sees a real string.
        anthropic_cfg = MagicMock()
        anthropic_cfg.name = "Anthropic"
        made_up_cfg = MagicMock()
        made_up_cfg.name = "Made Up"
        registry = {"anthropic": anthropic_cfg, "made-up-id": made_up_cfg}
        with patch.dict("sys.modules", {"hermes_cli.auth": MagicMock(PROVIDER_REGISTRY=registry)}):
            out = wiring.list_native_providers()

        by_id = {row["provider_id"]: row for row in out}
        # Curated id: the UI's "Add/Connect" form can pre-fill this.
        assert by_id["anthropic"]["default_model"] == "claude-sonnet-4-6"
        # Non-curated id: "" (not missing) — the field still starts empty and
        # editable rather than absent, so the frontend never has to special-case it.
        assert by_id["made-up-id"]["default_model"] == ""


# ---------------------------------------------------------------------------
# B. set_active_provider — native (non-UUID) ids
# ---------------------------------------------------------------------------


class TestSetActiveProviderNativeIds:
    def test_switch_a_then_b_then_reactivate_a_restores_a_own_model(
        self, tmp_path: Path, _hermes_home: Path
    ) -> None:
        active_svc = MagicMock()
        wiring = _make_wiring(tmp_path, active_provider_service=active_svc)
        write_model_calls: list[tuple] = []

        def _record_write_model(provider_id, model, base_url=""):
            write_model_calls.append((provider_id, model, base_url))

        with (
            patch(f"{_DBUS_MODULE}._write_hermes_model_config", side_effect=_record_write_model),
            patch(f"{_DBUS_MODULE}._clear_engine_runtime_cache"),
            patch.dict("sys.modules", {"hermes_cli.auth": MagicMock(PROVIDER_REGISTRY=_FAKE_REGISTRY)}),
        ):
            # Configure + activate A (anthropic), then B (openai-api) — B wins.
            wiring.configure_native_provider(
                provider_id="anthropic", api_key="sk-ant", model="claude-x",
                base_url="", sender_uid=1000, set_active=True,
            )
            wiring.configure_native_provider(
                provider_id="openai-api", api_key="sk-oai", model="gpt-4.1-mini",
                base_url="", sender_uid=1000, set_active=True,
            )
            assert write_model_calls[-1] == ("openai-api", "gpt-4.1-mini", "")

            # Switch BACK to A by id alone (no api_key re-supplied) — this is
            # exactly what the UI's "Activar" button does for a native row
            # (POST /providers/{id}/activate -> set_active_provider).
            result = wiring.set_active_provider(provider_id="anthropic", sender_uid=1000)

        # A's OWN model (claude-x) must come back, not B's leftover gpt-4.1-mini
        # and not an empty/inherited string.
        assert write_model_calls[-1] == ("anthropic", "claude-x", "")
        assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant"
        assert result.get("provider_id") == "anthropic" or result.get("ok") is True

    def test_unknown_native_id_fails_soft_not_valueerror(
        self, tmp_path: Path, _hermes_home: Path
    ) -> None:
        wiring = _make_wiring(tmp_path)
        with patch.dict("sys.modules", {"hermes_cli.auth": MagicMock(PROVIDER_REGISTRY=_FAKE_REGISTRY)}):
            result = wiring.set_active_provider(provider_id="not-a-real-provider", sender_uid=1000)
        assert result == {"ok": False, "error": "provider desconocido: not-a-real-provider"}

    def test_native_id_without_saved_key_refuses_to_activate(
        self, tmp_path: Path, _hermes_home: Path
    ) -> None:
        """A native provider that was never configured (no key in .env) must
        not silently activate with no credentials — that just moves the
        failure to the next chat (401) instead of surfacing it here."""
        wiring = _make_wiring(tmp_path)
        with patch.dict("sys.modules", {"hermes_cli.auth": MagicMock(PROVIDER_REGISTRY=_FAKE_REGISTRY)}):
            result = wiring.set_active_provider(provider_id="anthropic", sender_uid=1000)
        assert result["ok"] is False
        assert "anthropic" in result["error"]

    def test_native_id_with_key_but_no_model_refuses_to_activate(
        self, tmp_path: Path, _hermes_home: Path
    ) -> None:
        """specs/025-safent-repaso PROV-02 — the UI's Add/Connect body is
        configureNativeProvider({provider_id, api_key}), with NO `model`.
        Activating that provider must raise loudly (-> 422 in REST, see
        SetActiveProvider in the adapter) instead of writing config.yaml
        with model.provider set and no model.default, which used to crash
        the FIRST chat turn with HermesModelNotConfiguredError instead of
        failing here, clearly."""
        wiring = _make_wiring(tmp_path)
        with (
            patch(f"{_DBUS_MODULE}._write_hermes_model_config") as mock_write_model,
            patch.dict("sys.modules", {"hermes_cli.auth": MagicMock(PROVIDER_REGISTRY=_FAKE_REGISTRY)}),
        ):
            # Exactly the UI's current body: no `model` at all.
            wiring.configure_native_provider(
                provider_id="anthropic", api_key="sk-ant", model="",
                base_url="", sender_uid=1000, set_active=False,
            )

            with pytest.raises(ValueError, match="anthropic.*modelo"):
                wiring.set_active_provider(provider_id="anthropic", sender_uid=1000)

        # The broken config (provider set, no default) must NEVER be written.
        mock_write_model.assert_not_called()

    def test_sql_uuid_path_is_unaffected(self, tmp_path: Path, _hermes_home: Path) -> None:
        """Regression guard: the pre-existing SQL-repo UUID path (custom
        providers added via POST /providers) must keep working exactly as
        before — only NON-uuid ids take the new native branch."""
        wiring = _make_wiring(tmp_path)
        with (
            patch(f"{_DBUS_MODULE}._write_hermes_env"),
            patch(f"{_DBUS_MODULE}._write_hermes_model_config") as mock_write_model,
            patch(f"{_DBUS_MODULE}._clear_engine_runtime_cache"),
            patch("hermes.shell_server.providers.native_sync.kind_to_native_target") as mock_map,
            patch.dict("sys.modules", {"hermes_cli.auth": MagicMock(PROVIDER_REGISTRY=_FAKE_REGISTRY)}),
        ):
            from hermes.shell_server.providers.native_sync import NativeProviderTarget

            mock_map.return_value = NativeProviderTarget(
                provider_id="openai-api", env_var="OPENAI_API_KEY",
                base_url_env_var="OPENAI_BASE_URL", needs_base_url=False,
            )
            draft = json.dumps({
                "kind": "openai", "alias": "t", "default_model": "gpt-5.4-nano",
                "api_key": "sk-test", "set_active": False,
            })
            saved = wiring.add_provider(draft_json=draft, sender_uid=1000)
            # sanity: the SQL repo really did assign a UUID.
            UUID(saved["provider_id"])

            wiring.set_active_provider(provider_id=saved["provider_id"], sender_uid=1000)

        mock_write_model.assert_called_once_with("openai-api", "gpt-5.4-nano", "")


# ---------------------------------------------------------------------------
# B2. test_provider — native (non-UUID) ids (specs/025-safent-repaso PROV-03)
# ---------------------------------------------------------------------------
#
# Before the fix, test_provider did `pid = _UUID(provider_id)` unconditionally.
# A native catalogue id ("anthropic", "gemini"...) is not a UUID, so this
# raised ValueError — uncaught, it crosses the D-Bus boundary as a generic
# error that dbus_proxy._translate_dbus_error can't match to any
# org.hermes.Error.* name, so it falls through to AgentUnavailable. The REST
# route then reports 200 {"ok": false, "error": "daemon_unavailable"} for
# EVERY native "Test" click, valid key or not, and the card never activates.


class TestTestProviderNativeIds:
    async def test_native_id_reaches_the_real_validator_instead_of_crashing(
        self, tmp_path: Path, _hermes_home: Path
    ) -> None:
        wiring = _make_wiring(tmp_path)
        with patch.dict("sys.modules", {"hermes_cli.auth": MagicMock(PROVIDER_REGISTRY=_FAKE_REGISTRY)}):
            # Save a key + model WITHOUT activating (mirrors the UI's
            # "Add/Connect" step before the "Test" click).
            wiring.configure_native_provider(
                provider_id="anthropic", api_key="sk-ant-real", model="claude-x",
                base_url="", sender_uid=1000, set_active=False,
            )

            with patch(
                f"{_DBUS_MODULE}._nous_validate_model_string",
                new=AsyncMock(return_value=(True, None, None)),
            ) as mock_validate:
                result = await wiring.test_provider(provider_id="anthropic", sender_uid=1000)

        assert result == {"ok": True, "error": None, "code": None}
        mock_validate.assert_awaited_once_with("anthropic/claude-x", "sk-ant-real", "")

    async def test_unknown_native_id_fails_soft_not_valueerror(
        self, tmp_path: Path, _hermes_home: Path
    ) -> None:
        wiring = _make_wiring(tmp_path)
        with patch.dict("sys.modules", {"hermes_cli.auth": MagicMock(PROVIDER_REGISTRY=_FAKE_REGISTRY)}):
            result = await wiring.test_provider(provider_id="not-a-real-provider", sender_uid=1000)
        assert result == {"ok": False, "error": "provider desconocido: not-a-real-provider"}

    async def test_native_id_without_saved_key_fails_soft(
        self, tmp_path: Path, _hermes_home: Path
    ) -> None:
        """A native provider never configured (no key in .env) must report a
        clear reason, not crash and not silently probe with an empty key."""
        wiring = _make_wiring(tmp_path)
        with patch.dict("sys.modules", {"hermes_cli.auth": MagicMock(PROVIDER_REGISTRY=_FAKE_REGISTRY)}):
            result = await wiring.test_provider(provider_id="anthropic", sender_uid=1000)
        assert result["ok"] is False
        assert "anthropic" in result["error"]

    async def test_native_id_without_model_fails_soft(
        self, tmp_path: Path, _hermes_home: Path
    ) -> None:
        """specs/025-safent-repaso PROV-02 companion case: a key saved with NO
        model (the UI's configureNativeProvider({provider_id, api_key}) body,
        no `model`) must not crash test_provider either."""
        wiring = _make_wiring(tmp_path)
        with patch.dict("sys.modules", {"hermes_cli.auth": MagicMock(PROVIDER_REGISTRY=_FAKE_REGISTRY)}):
            wiring.configure_native_provider(
                provider_id="anthropic", api_key="sk-ant-real", model="",
                base_url="", sender_uid=1000, set_active=False,
            )
            result = await wiring.test_provider(provider_id="anthropic", sender_uid=1000)
        assert result == {"ok": False, "error": "anthropic no tiene modelo configurado"}

    async def test_sql_uuid_path_is_unaffected(self, tmp_path: Path, _hermes_home: Path) -> None:
        """Regression guard: a real SQL-repo provider (custom, UUID id) must
        keep going through _nous_validate_provider — only non-UUID ids take
        the new native branch."""
        wiring = _make_wiring(tmp_path)
        draft = json.dumps({
            "kind": "openai", "alias": "t", "default_model": "gpt-5.4-nano",
            "api_key": "sk-test", "set_active": False,
        })
        saved = wiring.add_provider(draft_json=draft, sender_uid=1000)
        UUID(saved["provider_id"])  # sanity: really a UUID

        with (
            patch(f"{_DBUS_MODULE}._nous_validate_provider", new=AsyncMock(return_value=(True, None, None))) as mock_sql,
            patch.object(wiring, "_test_native_provider", new=AsyncMock()) as mock_native,
        ):
            result = await wiring.test_provider(provider_id=saved["provider_id"], sender_uid=1000)

        assert result == {"ok": True, "error": None, "code": None}
        mock_sql.assert_awaited_once()
        mock_native.assert_not_awaited()


# ---------------------------------------------------------------------------
# B3. _nous_validate_model_string — honest protocol classification (PROV-03,
# matriz-final-39eeb8e: "cambia la causa, no el síntoma"). The UUID crash is
# fixed (B2 above), but the anthropic probe hit
# `POST https://api.anthropic.com/chat/completions` (OpenAI shape) -> 404
# ALWAYS, valid key or not, because Anthropic has never served that route —
# only `/v1/messages`. Since ProvidersView.tsx only auto-activates on
# `ok === true`, the Anthropic card never activated. These tests fake the
# HTTP layer so no real network call happens.
# ---------------------------------------------------------------------------


class _FakeAnthropicResponse:
    def __init__(self, *, status: int, body: str) -> None:
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body

    async def __aenter__(self) -> "_FakeAnthropicResponse":
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _FakeAnthropicSession:
    """Drop-in for aiohttp.ClientSession — records the exact request
    _probe_anthropic_messages_api sends and replays a scripted response.
    Never touches the network."""

    def __init__(self, response: _FakeAnthropicResponse, captured: dict) -> None:
        self._response = response
        self._captured = captured

    async def __aenter__(self) -> "_FakeAnthropicSession":
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    def post(self, url: str, *, headers: dict, json: dict, timeout: object) -> _FakeAnthropicResponse:  # noqa: A002
        self._captured["url"] = url
        self._captured["headers"] = headers
        self._captured["json"] = json
        return self._response


def _install_fake_anthropic_http(
    monkeypatch: pytest.MonkeyPatch, *, status: int, body: str
) -> dict:
    import aiohttp

    captured: dict = {}
    response = _FakeAnthropicResponse(status=status, body=body)
    monkeypatch.setattr(
        aiohttp, "ClientSession", lambda *_a, **_k: _FakeAnthropicSession(response, captured)
    )
    return captured


class TestAnthropicMessagesApiProbe:
    """Anthropic must be probed via its REAL wire format (POST /v1/messages,
    x-api-key + anthropic-version) — never the OpenAI Chat Completions shape
    every other provider uses."""

    async def test_hits_v1_messages_not_chat_completions(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hermes.agents_os.infrastructure import dbus_runtime_service as m

        captured = _install_fake_anthropic_http(monkeypatch, status=200, body='{"id":"msg_1"}')
        await m._nous_validate_model_string("anthropic/claude-sonnet-4-6", "sk-ant-real", None)

        assert captured["url"] == "https://api.anthropic.com/v1/messages"
        assert not captured["url"].endswith("/chat/completions")

    async def test_valid_key_returns_ok_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from hermes.agents_os.infrastructure import dbus_runtime_service as m

        captured = _install_fake_anthropic_http(monkeypatch, status=200, body='{"id":"msg_1"}')
        ok, err, code = await m._nous_validate_model_string(
            "anthropic/claude-sonnet-4-6", "sk-ant-real", None
        )

        assert (ok, err, code) == (True, None, None)
        assert captured["headers"]["x-api-key"] == "sk-ant-real"
        assert captured["headers"]["anthropic-version"]

    async def test_rejected_key_returns_invalid_key_code(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hermes.agents_os.infrastructure import dbus_runtime_service as m

        _install_fake_anthropic_http(
            monkeypatch,
            status=401,
            body='{"type":"error","error":{"type":"authentication_error","message":"invalid x-api-key"}}',
        )
        ok, err, code = await m._nous_validate_model_string(
            "anthropic/claude-sonnet-4-6", "sk-ant-bad", None
        )

        assert ok is False
        assert code == "invalid_key"
        assert "x-api-key" in err

    async def test_wrong_endpoint_returns_endpoint_error_code(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The exact regression being pinned: before this fix, EVERY
        anthropic probe hit /chat/completions and got an unclassified,
        unconditional 404. Now a real 404 (e.g. a misconfigured self-hosted
        base_url) is classified as endpoint_error, not a bare ok:false."""
        from hermes.agents_os.infrastructure import dbus_runtime_service as m

        _install_fake_anthropic_http(monkeypatch, status=404, body="404 page not found")
        ok, err, code = await m._nous_validate_model_string(
            "anthropic/claude-sonnet-4-6", "sk-ant-real", "https://self-hosted.example.com"
        )

        assert ok is False
        assert code == "endpoint_error"

    async def test_custom_base_url_appends_the_real_messages_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hermes.agents_os.infrastructure import dbus_runtime_service as m

        captured = _install_fake_anthropic_http(monkeypatch, status=200, body="{}")
        await m._nous_validate_model_string(
            "anthropic/claude-sonnet-4-6", "sk-ant-real", "https://proxy.example.com/anthropic/"
        )

        assert captured["url"] == "https://proxy.example.com/anthropic/v1/messages"

    async def test_logs_one_structured_journal_line_per_probe(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """specs/025-safent-repaso matriz-final-39eeb8e re-verificación
        d2eb8c6 (menor nuevo): this probe used aiohttp directly and left ZERO
        trace in the journal, unlike gemini/openai-api (httpx/openai SDK, both
        log their own request line) — the one provider whose bug WAS the
        endpoint it hit had no observable evidence of what it actually
        called. One structured line per probe, never the key or the body."""
        from hermes.agents_os.infrastructure import dbus_runtime_service as m

        _install_fake_anthropic_http(
            monkeypatch,
            status=401,
            body=(
                '{"type":"error","error":{"type":"authentication_error",'
                '"message":"invalid x-api-key sk-ant-super-secret-leak"}}'
            ),
        )
        with caplog.at_level("INFO", logger="hermes.agents_os.dbus_runtime_service"):
            await m._probe_anthropic_messages_api(
                bare_model="claude-sonnet-4-6",
                api_key="sk-ant-super-secret-leak",
                base_url=None,
            )

        probe_records = [
            r for r in caplog.records if r.getMessage() == "hermes.providers.probe_completed"
        ]
        assert len(probe_records) == 1, "exactly one structured line per probe"
        record = probe_records[0]
        assert record.provider_id == "anthropic"
        assert record.endpoint == "api.anthropic.com/v1/messages"
        assert record.status == 401
        assert record.code == "invalid_key"

        for r in caplog.records:
            assert "sk-ant-super-secret-leak" not in r.getMessage()
            assert "authentication_error" not in r.getMessage()

    async def test_logs_one_line_even_on_network_exception(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A probe that never got an HTTP response (DNS/timeout/connection
        error) must still leave exactly one journal line, with status/code
        both null rather than silently skipping the log."""
        import aiohttp

        from hermes.agents_os.infrastructure import dbus_runtime_service as m

        class _RaisingSession:
            async def __aenter__(self) -> "_RaisingSession":
                return self

            async def __aexit__(self, *_exc: object) -> bool:
                return False

            def post(self, *_a: object, **_k: object) -> None:
                raise ConnectionError("Cannot connect to host api.anthropic.com:443")

        monkeypatch.setattr(aiohttp, "ClientSession", lambda *_a, **_k: _RaisingSession())

        with caplog.at_level("INFO", logger="hermes.agents_os.dbus_runtime_service"):
            ok, err, code = await m._probe_anthropic_messages_api(
                bare_model="claude-sonnet-4-6", api_key="sk-ant-x", base_url=None
            )

        assert ok is False
        assert code is None
        probe_records = [
            r for r in caplog.records if r.getMessage() == "hermes.providers.probe_completed"
        ]
        assert len(probe_records) == 1
        assert probe_records[0].status is None
        assert probe_records[0].code is None
        assert probe_records[0].endpoint == "api.anthropic.com/v1/messages"


class TestOpenAiCompatibleProbeClassification:
    """gemini (OpenAI-compatible) keeps the EXISTING client path untouched —
    the same classification rule (401/403 -> invalid_key, 404 ->
    endpoint_error) now also applies to whatever the openai SDK raises,
    using the REAL openai exception types (`.status_code`), not a hand-
    rolled duck type. hermes_cli is not installed in this environment (it
    ships inside the container image only — same constraint documented in
    test_dbus_provider_verbs.py), so resolve_runtime_provider is faked via
    sys.modules, mirroring this file's own PROVIDER_REGISTRY fake above."""

    async def test_401_from_the_endpoint_is_classified_invalid_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import httpx
        import openai

        from hermes.agents_os.infrastructure import dbus_runtime_service as m

        request = httpx.Request(
            "POST", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
        )
        response = httpx.Response(401, request=request, json={"error": {"message": "API key not valid"}})
        auth_error = openai.AuthenticationError("API key not valid", response=response, body=None)

        class _FakeCompletions:
            def create(self, **_kw: object) -> None:
                raise auth_error

        class _FakeChat:
            completions = _FakeCompletions()

        class _FakeOpenAI:
            def __init__(self, **_kw: object) -> None:
                self.chat = _FakeChat()

        fake_openai_module = MagicMock(OpenAI=_FakeOpenAI)
        fake_runtime_provider_module = MagicMock()
        fake_runtime_provider_module.resolve_runtime_provider.return_value = {
            "api_key": "bad-key",
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        }

        with patch.dict(
            "sys.modules",
            {"openai": fake_openai_module, "hermes_cli.runtime_provider": fake_runtime_provider_module},
        ):
            ok, err, code = await m._nous_validate_model_string(
                "gemini/gemini-2.5-flash", "bad-key", None
            )

        assert ok is False
        assert code == "invalid_key"
        assert "API key not valid" in err

    async def test_404_from_the_endpoint_is_classified_endpoint_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import httpx
        import openai

        from hermes.agents_os.infrastructure import dbus_runtime_service as m

        request = httpx.Request("POST", "https://bogus.example.com/v1/chat/completions")
        response = httpx.Response(404, request=request, text="404 page not found")
        not_found_error = openai.NotFoundError("404 page not found", response=response, body=None)

        class _FakeCompletions:
            def create(self, **_kw: object) -> None:
                raise not_found_error

        class _FakeChat:
            completions = _FakeCompletions()

        class _FakeOpenAI:
            def __init__(self, **_kw: object) -> None:
                self.chat = _FakeChat()

        fake_openai_module = MagicMock(OpenAI=_FakeOpenAI)
        fake_runtime_provider_module = MagicMock()
        fake_runtime_provider_module.resolve_runtime_provider.return_value = {
            "api_key": "sk-whatever",
            "base_url": "https://bogus.example.com",
        }

        with patch.dict(
            "sys.modules",
            {"openai": fake_openai_module, "hermes_cli.runtime_provider": fake_runtime_provider_module},
        ):
            ok, err, code = await m._nous_validate_model_string(
                "gemini/gemini-2.5-flash", "sk-whatever", None
            )

        assert ok is False
        assert code == "endpoint_error"


# ---------------------------------------------------------------------------
# C. Engine runtime cache invalidation
# ---------------------------------------------------------------------------


class TestClearRuntimeProviderCache:
    def test_clear_empties_the_cache(self) -> None:
        from hermes.runtime import nous_engine

        nous_engine._RUNTIME_PROVIDER_CACHE[12345] = (99999999999.0, ({"provider": "gemini"}, "gemini-2.5"))
        assert nous_engine._RUNTIME_PROVIDER_CACHE  # sanity: populated

        nous_engine.clear_runtime_provider_cache()

        assert nous_engine._RUNTIME_PROVIDER_CACHE == {}

    def test_cleared_cache_forces_fresh_resolve_on_next_call(self) -> None:
        """Without the fix, a switch inside the 30s TTL window kept serving the
        stale (pre-switch) runtime — this is the concrete 'keeps going to
        Gemini even after activating Anthropic' symptom from the matrix."""
        from hermes.runtime import nous_engine

        calls: list[str] = []

        def _fake_resolve(model_config):
            calls.append(model_config.model)
            return ({"provider": model_config.model}, "bare-model")

        with patch.object(nous_engine, "_resolve_hermes_runtime", side_effect=_fake_resolve):
            engine_id = 777
            from hermes.runtime.model_config import ModelConfig
            first = nous_engine._cached_resolve_hermes_runtime(engine_id, ModelConfig(model="gemini"))
            # Still within TTL: same engine_id, DIFFERENT model_config (switch
            # happened) — the stale cache would incorrectly win without clear().
            nous_engine.clear_runtime_provider_cache()
            second = nous_engine._cached_resolve_hermes_runtime(engine_id, ModelConfig(model="anthropic"))

        assert first[0]["provider"] == "gemini"
        assert second[0]["provider"] == "anthropic"
        assert calls == ["gemini", "anthropic"]

    def test_slow_resolve_does_not_poison_cache_after_concurrent_clear(self) -> None:
        """PROV-05 — the ~30-70s "switch takes a while" symptom is a write-
        after-clear race, not a missing invalidation call: _resolve_hermes_
        runtime() runs OUTSIDE the lock (it's a blocking disk/SDK read), so a
        resolve that started BEFORE a switch can still be mid-flight when
        clear_runtime_provider_cache() runs, and then write its STALE result
        back into the cache AFTER the clear — re-poisoning it with the OLD
        provider for a full new 30s TTL window even though the switch (and
        config.yaml) already moved on. Reproduces the exact matrix pattern:
        turn #1 (in flight before the switch) correctly returns the OLD
        provider; turn #2 (issued AFTER the switch) must NOT inherit turn
        #1's stale write."""
        from hermes.runtime import nous_engine

        engine_id = 888
        calls: list[str] = []

        def _fake_resolve(model_config):
            calls.append(model_config.model)
            if len(calls) == 1:
                # The owner switches providers (and the daemon clears the
                # cache) WHILE this first resolve is still running.
                nous_engine.clear_runtime_provider_cache()
            return ({"provider": model_config.model}, "bare-model")

        with patch.object(nous_engine, "_resolve_hermes_runtime", side_effect=_fake_resolve):
            from hermes.runtime.model_config import ModelConfig
            first = nous_engine._cached_resolve_hermes_runtime(engine_id, ModelConfig(model="gemini"))
            second = nous_engine._cached_resolve_hermes_runtime(engine_id, ModelConfig(model="anthropic"))

        assert first[0]["provider"] == "gemini"
        assert second[0]["provider"] == "anthropic"
        # The critical assertion: turn #2 must have MISSED the cache and
        # recomputed. Without the epoch guard, turn #1's write-back (which
        # runs AFTER the clear but is unconditional) wins the race and turn
        # #2 reads it straight from cache — same failure mode as before the
        # fix, just moved one layer down: "invalidated but immediately
        # re-poisoned" instead of "never invalidated".
        assert calls == ["gemini", "anthropic"]


# ---------------------------------------------------------------------------
# D. Integration-style: REST -> D-Bus mutator carries set_active end to end
# ---------------------------------------------------------------------------


class _FakeMutatorProxy:
    """Stands in for DbusRuntimeProxy — records the exact (member, args) the
    REST router forwards, the way the real daemon call would receive them."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []

    async def call_mutator(self, member: str, *args) -> dict:
        self.calls.append((member, args))
        return {"ok": True, "provider_id": "gemini"}

    async def call_list(self, member: str, *args):
        return []

    async def call_dict(self, member: str, *args):
        return {}


class TestConfigureNativeProviderRestForwardsSetActive:
    def _client(self, proxy: _FakeMutatorProxy):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from hermes.shell_server.cowork.providers_api import create_providers_router

        app = FastAPI()
        app.state.dbus_proxy = proxy
        app.include_router(create_providers_router())
        return TestClient(app)

    def test_set_active_true_reaches_the_mutator_call(self) -> None:
        proxy = _FakeMutatorProxy()
        client = self._client(proxy)

        r = client.post(
            "/api/v1/providers/native",
            json={"provider_id": "gemini", "api_key": "k", "model": "gemini-2.5-flash", "set_active": True},
        )

        assert r.status_code == 201
        assert len(proxy.calls) == 1
        member, args = proxy.calls[0]
        assert member == "configure_native_provider"
        draft = json.loads(args[0])
        assert draft["set_active"] is True
        assert draft["provider_id"] == "gemini"

    def test_set_active_omitted_defaults_false_in_the_forwarded_draft(self) -> None:
        proxy = _FakeMutatorProxy()
        client = self._client(proxy)

        r = client.post(
            "/api/v1/providers/native",
            json={"provider_id": "gemini", "api_key": "k"},
        )

        assert r.status_code == 201
        draft = json.loads(proxy.calls[0][1][0])
        assert draft["set_active"] is False
