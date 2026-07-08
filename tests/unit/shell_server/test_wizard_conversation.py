"""Unit tests for WizardConversation using FakeLLM — no real provider in CI.

Scenarios:
  - Happy path: personal_desktop full flow start→finalize.
  - Ambiguous answer triggers re-ask (no state advance).
  - wizard/status flips to complete after finalize endpoint.
"""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import UUID

import pytest

# spec-003 first-boot wizard (the `contracts` port package AND the
# `hermes.shell_server.wizard` package it drives) is not vendored in this
# checkout/CI and is absent from the baked image. Skip the whole module when
# unavailable instead of failing collection. Mirrors the guard in
# tests/unit/agents_os/test_first_boot_wizard.py.
_SPEC = Path(__file__).parents[3] / "specs" / "003-agents-os-edition"
if str(_SPEC) not in sys.path:
    sys.path.insert(0, str(_SPEC))

try:
    from contracts.first_boot_wizard_port import WizardState  # noqa: E402
    import hermes.shell_server.wizard.conversation  # noqa: E402,F401
except (ImportError, RuntimeError):  # spec-003 wizard not present in this checkout/CI/image
    pytest.skip(
        "spec 003 first-boot wizard not present in this checkout/CI",
        allow_module_level=True,
    )

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# FakeLLM — deterministic canned responses per step
# ---------------------------------------------------------------------------

class FakeLLM:
    """Deterministic LLM for unit tests.

    Accepts a queue of responses; each call pops the next one.
    """

    def __init__(self, responses: list[str]) -> None:
        self._queue = list(responses)

    async def complete(
        self,
        *,
        system_prompt: str,
        messages: list[dict],
        temperature: float = 0.3,
        max_tokens: int = 512,
    ) -> str:
        if not self._queue:
            raise AssertionError("FakeLLM queue exhausted — unexpected LLM call")
        return self._queue.pop(0)


# ---------------------------------------------------------------------------
# Canned LLM responses for the personal_desktop happy path
# ---------------------------------------------------------------------------

_PROFILE_RESPONSE = (
    '{"resolved": true, "value": "personal_desktop"}\n'
    "Perfecto, has elegido el perfil de escritorio personal. Ahora configuremos el idioma."
)

_LOCALE_RESPONSE = (
    '{"resolved": true, "language_code": "es", "keyboard_layout": "es", "timezone": "Europe/Madrid"}\n'
    "Idioma español, teclado es, zona horaria Europe/Madrid. Pasamos a la red."
)

_NETWORK_RESPONSE = (
    '{"resolved": true, "value": "connected"}\n'
    "Conectado a red. Ahora la vinculación con tenant."
)

_TENANT_RESPONSE = (
    '{"resolved": true, "decision": "defer"}\n'
    "Vinculación diferida para más adelante. Revisemos los permisos."
)

_CONSENTS_RESPONSE = (
    '{"resolved": true, "granted": ["documents", "microphone"]}\n'
    "Permisos de documentos y micrófono concedidos. Revisemos los servicios."
)

_SERVICES_RESPONSE = (
    '{"resolved": true, "acknowledged": true}\n'
    "Servicios revisados y confirmados. Listo para finalizar."
)

# Ambiguous response — no resolved key
_AMBIGUOUS_RESPONSE = '{"resolved": false, "reason": "No entendí la opción elegida."}'

# Valid profile after an ambiguous one
_PROFILE_AFTER_AMBIGUOUS = (
    '{"resolved": true, "value": "server"}\n'
    "Perfil servidor seleccionado."
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_conversation(llm: FakeLLM):
    from hermes.agents_os.application.first_boot_wizard import InMemoryFirstBootWizard
    from hermes.shell_server.wizard.conversation import WizardConversation

    wizard = InMemoryFirstBootWizard()
    return WizardConversation(wizard=wizard, llm=llm)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestPersonalDesktopHappyPath:
    """Full personal_desktop wizard from start to finalize via WizardConversation."""

    async def test_full_flow_reaches_completed(self) -> None:
        llm = FakeLLM([
            _PROFILE_RESPONSE,
            _LOCALE_RESPONSE,
            _NETWORK_RESPONSE,
            _TENANT_RESPONSE,
            _CONSENTS_RESPONSE,
            _SERVICES_RESPONSE,
        ])
        conv = _make_conversation(llm)

        snap, opening = await conv.start()
        assert snap.state == WizardState.COLLECTING_PROFILE
        assert opening  # non-empty opening message

        sid = snap.wizard_session_id

        snap, msg, done = await conv.handle_message(
            session_id=sid, user_message="quiero escritorio personal"
        )
        assert snap.state == WizardState.COLLECTING_LOCALE
        assert not done

        snap, msg, done = await conv.handle_message(
            session_id=sid, user_message="español, España"
        )
        assert snap.state == WizardState.COLLECTING_NETWORK
        assert not done

        snap, msg, done = await conv.handle_message(
            session_id=sid, user_message="sí, conectado"
        )
        assert snap.state == WizardState.COLLECTING_TENANT_BINDING
        assert not done

        snap, msg, done = await conv.handle_message(
            session_id=sid, user_message="más tarde, no quiero vincular ahora"
        )
        assert snap.state == WizardState.COLLECTING_CONSENTS
        assert not done

        snap, msg, done = await conv.handle_message(
            session_id=sid, user_message="acepto documentos y micrófono"
        )
        assert snap.state == WizardState.REVIEWING_EXPOSED_SERVICES
        assert not done

        snap, msg, done = await conv.handle_message(
            session_id=sid, user_message="sí, lo he revisado, de acuerdo"
        )
        assert snap.state == WizardState.FINALIZING
        assert not done

        # Finalize via the wizard directly (API endpoint handles this in prod).
        from hermes.agents_os.application.first_boot_wizard import InMemoryFirstBootWizard
        # conv._wizard is the instance; access it directly for the test.
        final_snap = await conv._wizard.finalize(wizard_session_id=sid)
        assert final_snap.state == WizardState.COMPLETED
        assert final_snap.produced_node_installation_id is not None

    async def test_collected_values_are_correct(self) -> None:
        llm = FakeLLM([
            _PROFILE_RESPONSE,
            _LOCALE_RESPONSE,
            _NETWORK_RESPONSE,
            _TENANT_RESPONSE,
            _CONSENTS_RESPONSE,
            _SERVICES_RESPONSE,
        ])
        conv = _make_conversation(llm)
        snap, _ = await conv.start()
        sid = snap.wizard_session_id

        await conv.handle_message(session_id=sid, user_message="personal_desktop")
        await conv.handle_message(session_id=sid, user_message="español")
        await conv.handle_message(session_id=sid, user_message="conectado")
        await conv.handle_message(session_id=sid, user_message="más tarde")
        await conv.handle_message(session_id=sid, user_message="todo")
        await conv.handle_message(session_id=sid, user_message="sí")

        final = await conv._wizard.get_snapshot(wizard_session_id=sid)
        assert final.collected_profile_kind is not None
        assert final.collected_locale is not None
        assert final.collected_locale.language_code == "es"
        assert final.collected_network_decision is not None
        assert final.collected_tenant_binding is not None
        assert final.reviewed_exposed_services is True


class TestAmbiguousAnswerTriggerReAsk:
    """Ambiguous LLM response must not advance state; re-ask is returned."""

    async def test_ambiguous_does_not_advance_state(self) -> None:
        llm = FakeLLM([
            _AMBIGUOUS_RESPONSE,    # first call: ambiguous
            _PROFILE_AFTER_AMBIGUOUS,  # second call: resolved
        ])
        conv = _make_conversation(llm)
        snap, _ = await conv.start()
        sid = snap.wizard_session_id

        # First message → ambiguous → state stays COLLECTING_PROFILE.
        snap_after, msg, done = await conv.handle_message(
            session_id=sid, user_message="hmm no sé"
        )
        assert snap_after.state == WizardState.COLLECTING_PROFILE
        assert not done
        assert "No entendí" in msg or "interpretar" in msg.lower()

        # Second message → resolved → advances to next state.
        snap_after, msg, done = await conv.handle_message(
            session_id=sid, user_message="servidor"
        )
        assert snap_after.state == WizardState.COLLECTING_LOCALE
        assert not done

    async def test_ambiguous_does_not_consume_extra_state_steps(self) -> None:
        """State machine step count does not increase on ambiguous answer."""
        llm = FakeLLM([_AMBIGUOUS_RESPONSE])
        conv = _make_conversation(llm)
        snap, _ = await conv.start()
        sid = snap.wizard_session_id
        state_before = snap.state

        snap_after, _, _ = await conv.handle_message(
            session_id=sid, user_message="algo ambiguo"
        )
        assert snap_after.state == state_before


class TestWizardApiEndpoints:
    """REST API layer — status, start, message, finalize via TestClient."""

    @pytest.fixture
    def client(self, tmp_path):
        import os
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from hermes.shell_server.wizard.api import create_wizard_router
        from hermes.shell_server.security.secrets import SecretsVault

        # Inyectar vault con clave efímera: master.key no existe en CI/tests
        # (SecretsVault fail-closed en prod requiere el fichero real).
        test_vault = SecretsVault(master_key=os.urandom(32))
        app = FastAPI()
        app.include_router(create_wizard_router(tmp_path / "wizard.db", vault=test_vault))
        return TestClient(app)

    def test_status_initially_false(self, client) -> None:
        r = client.get("/api/v1/wizard/status")
        assert r.status_code == 200
        assert r.json()["first_boot_complete"] is False
        assert r.json()["completed_at"] is None

    def test_start_requires_active_provider(self, client) -> None:
        # No provider seeded → 503.
        r = client.post("/api/v1/wizard/start")
        assert r.status_code == 503

    def test_get_unknown_session_returns_404(self, client) -> None:
        import uuid
        r = client.get(f"/api/v1/wizard/{uuid.uuid4()}")
        assert r.status_code == 404

    def test_finalize_unknown_session_returns_404(self, client) -> None:
        import uuid
        r = client.post(f"/api/v1/wizard/{uuid.uuid4()}/finalize")
        assert r.status_code == 404

    def test_message_unknown_session_returns_404(self, client) -> None:
        import uuid
        r = client.post(
            f"/api/v1/wizard/{uuid.uuid4()}/message",
            json={"user_message": "hola"},
        )
        assert r.status_code == 404


class TestWizardApiWithFakeLLM:
    """Integration of API layer + WizardConversation with FakeLLM injected."""

    @pytest.fixture
    def client_and_llm(self, tmp_path, monkeypatch):
        """Build app with FakeLLM injected by monkeypatching _get_llm in api module."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import hermes.shell_server.wizard.api as wizard_api_module
        from hermes.shell_server.wizard import api as wizard_api

        # We monkeypatch create_wizard_router to inject a FakeLLM factory
        # instead of resolving the provider from DB.
        db_path = tmp_path / "wizard.db"

        # Directly override _get_llm at module level for the test scope.
        fake_llm_holder = {"llm": None}

        import os
        from hermes.shell_server.security.secrets import SecretsVault
        test_vault = SecretsVault(master_key=os.urandom(32))

        app = FastAPI()
        app.include_router(wizard_api.create_wizard_router(db_path, vault=test_vault))

        client = TestClient(app)
        return client, db_path, fake_llm_holder

    async def test_status_flips_after_finalize(self, tmp_path) -> None:
        """Finalize endpoint writes bootstrap marker → status becomes true."""
        from hermes.shell_server.wizard.api import (
            _mark_bootstrap_complete,
            _is_bootstrap_complete,
            init_schema,
        )

        db_path = tmp_path / "bootstrap_test.db"
        init_schema(db_path)

        complete, ts = _is_bootstrap_complete(db_path)
        assert complete is False

        _mark_bootstrap_complete(db_path)

        complete, ts = _is_bootstrap_complete(db_path)
        assert complete is True
        assert ts is not None

    async def test_mark_bootstrap_idempotent(self, tmp_path) -> None:
        from hermes.shell_server.wizard.api import (
            _mark_bootstrap_complete,
            _is_bootstrap_complete,
            init_schema,
        )

        db_path = tmp_path / "idempotent.db"
        init_schema(db_path)
        _mark_bootstrap_complete(db_path)
        ts_first = _is_bootstrap_complete(db_path)[1]
        _mark_bootstrap_complete(db_path)  # second call must not raise
        ts_second = _is_bootstrap_complete(db_path)[1]
        # Idempotent: timestamp must not change (ON CONFLICT DO NOTHING).
        assert ts_first == ts_second


class TestWizardFullFlowWithInjectedLLM:
    """Full personal_desktop flow using direct conversation + wizard objects
    (bypasses HTTP to avoid provider dependency), then verifies bootstrap marker."""

    async def test_full_flow_sets_bootstrap_marker(self, tmp_path) -> None:
        from hermes.agents_os.application.first_boot_wizard import InMemoryFirstBootWizard
        from hermes.shell_server.wizard.conversation import WizardConversation
        from hermes.shell_server.wizard.api import (
            _snap_to_dict,
            _save_session,
            _mark_bootstrap_complete,
            _is_bootstrap_complete,
            init_schema,
        )

        db_path = tmp_path / "full.db"
        init_schema(db_path)

        llm = FakeLLM([
            _PROFILE_RESPONSE,
            _LOCALE_RESPONSE,
            _NETWORK_RESPONSE,
            _TENANT_RESPONSE,
            _CONSENTS_RESPONSE,
            _SERVICES_RESPONSE,
        ])
        wizard = InMemoryFirstBootWizard()
        conv = WizardConversation(wizard=wizard, llm=llm)

        snap, _ = await conv.start()
        sid = snap.wizard_session_id

        for msg in [
            "escritorio personal",
            "español España",
            "sí red",
            "más tarde",
            "acepto todo",
            "confirmado",
        ]:
            snap, _, done = await conv.handle_message(session_id=sid, user_message=msg)

        # Should be in FINALIZING now.
        assert snap.state == WizardState.FINALIZING

        # Finalize wizard + mark bootstrap.
        final_snap = await wizard.finalize(wizard_session_id=sid)
        assert final_snap.state == WizardState.COMPLETED
        _mark_bootstrap_complete(db_path)

        complete, ts = _is_bootstrap_complete(db_path)
        assert complete is True
        assert ts is not None


# ---------------------------------------------------------------------------
# LLM-key gate: proveedor sin API key → el wizard la pide ANTES de arrancar
# la conversación (que necesita el LLM). Determinista, sin tocar el enum.
# ---------------------------------------------------------------------------

class TestWizardLlmKeyGate:
    """El gate recoge la API key cifrada y luego arranca el wizard real."""

    def _seed_keyless_active_provider(self, db_path, vault):
        from uuid import uuid4

        from hermes.shell_server.providers.domain import Provider, ProviderKind
        from hermes.shell_server.providers.repo import SQLiteProviderRepository

        repo = SQLiteProviderRepository(db_path=db_path, vault=vault)
        provider = Provider(
            provider_id=uuid4(),
            alias="vLLM Spark Qwen 3.6",
            kind=ProviderKind.VLLM,
            base_url="http://127.0.0.1:8888/vllm",
            has_api_key=False,
            default_model="qwen3.6-35b-a3b",
            is_active=True,
        )
        repo.add(provider=provider, api_key=None)  # activo pero SIN key
        return repo, provider

    def _client(self, db_path, vault, validator):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from hermes.shell_server.wizard.api import create_wizard_router

        app = FastAPI()
        app.include_router(
            create_wizard_router(db_path, vault=vault, llm_validator=validator)
        )
        return TestClient(app)

    @pytest.fixture
    def vault(self):
        import os

        from hermes.shell_server.security.secrets import SecretsVault

        return SecretsVault(master_key=os.urandom(32))

    def test_start_without_key_asks_for_key(self, tmp_path, vault) -> None:
        db_path = tmp_path / "gate.db"
        self._seed_keyless_active_provider(db_path, vault)

        async def _ok(_p, _k):
            return None

        client = self._client(db_path, vault, _ok)
        r = client.post("/api/v1/wizard/start")
        assert r.status_code == 201
        body = r.json()
        assert body["state"] == "awaiting_llm_key"
        assert "clave de API" in body["assistant_message"]
        assert "vLLM Spark Qwen 3.6" in body["assistant_message"]

    def test_bad_key_reasks_and_does_not_persist(self, tmp_path, vault) -> None:
        db_path = tmp_path / "gate.db"
        repo, provider = self._seed_keyless_active_provider(db_path, vault)

        async def _reject(_p, _k):
            return "AuthenticationError: 401"

        client = self._client(db_path, vault, _reject)
        sid = client.post("/api/v1/wizard/start").json()["session_id"]
        r = client.post(
            f"/api/v1/wizard/{sid}/message", json={"user_message": "clave-mala"}
        )
        assert r.status_code == 200
        body = r.json()
        assert body["state"] == "awaiting_llm_key"
        assert "401" in body["assistant_message"]
        # La key inválida NO se persiste.
        assert repo.get(provider_id=provider.provider_id).has_api_key is False

    def test_good_key_persists_and_starts_wizard(self, tmp_path, vault) -> None:
        db_path = tmp_path / "gate.db"
        repo, provider = self._seed_keyless_active_provider(db_path, vault)
        seen = {}

        async def _accept(_p, k):
            seen["key"] = k
            return None

        client = self._client(db_path, vault, _accept)
        sid = client.post("/api/v1/wizard/start").json()["session_id"]
        r = client.post(
            f"/api/v1/wizard/{sid}/message", json={"user_message": "sk-secreta-123"}
        )
        assert r.status_code == 200
        body = r.json()
        # Mismo session_id (la UI no cambia de sesión).
        assert body["session_id"] == sid
        # Arrancó el wizard real en el primer paso.
        assert body["state"] == "collecting_profile"
        assert body["assistant_message"].startswith("✓ **Conectado.**")
        assert body["done"] is False
        # La key se validó y se guardó cifrada.
        assert seen["key"] == "sk-secreta-123"
        stored = repo.get(provider_id=provider.provider_id)
        assert stored.has_api_key is True
        assert repo.reveal_api_key(provider_id=provider.provider_id) == "sk-secreta-123"


# ---------------------------------------------------------------------------
# Wizard DETERMINISTA por formulario (sin LLM): cada endpoint avanza un paso
# de la state machine con valores estructurados.
# ---------------------------------------------------------------------------

class TestWizardFormDeterministic:
    """El onboarding de formularios no depende del LLM en absoluto."""

    @pytest.fixture
    def client(self, tmp_path):
        import os

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from hermes.shell_server.security.secrets import SecretsVault
        from hermes.shell_server.wizard.api import create_wizard_router

        vault = SecretsVault(master_key=os.urandom(32))
        app = FastAPI()
        app.include_router(create_wizard_router(tmp_path / "form.db", vault=vault))
        return TestClient(app)

    def _B(self, sid):
        return f"/api/v1/wizard/form/{sid}"

    def test_full_personal_desktop_flow(self, client) -> None:
        # start (sin LLM, sin gate)
        r = client.post("/api/v1/wizard/form/start")
        assert r.status_code == 201
        sid = r.json()["session_id"]
        assert r.json()["state"] == "collecting_profile"

        # profile
        r = client.post(self._B(sid) + "/profile", json={"profile_kind": "personal_desktop"})
        assert r.status_code == 200 and r.json()["state"] == "collecting_locale"

        # locale
        r = client.post(
            self._B(sid) + "/locale",
            json={"language_code": "es", "keyboard_layout": "es", "timezone": "Europe/Madrid"},
        )
        assert r.json()["state"] == "collecting_network"

        # network
        r = client.post(self._B(sid) + "/network", json={"decision": "connected"})
        assert r.json()["state"] == "collecting_tenant_binding"

        # tenant (defer)
        r = client.post(self._B(sid) + "/tenant", json={"decision": "defer"})
        # personal_desktop → siguiente paso es consents
        assert r.json()["state"] == "collecting_consents"

        # consents
        r = client.post(
            self._B(sid) + "/consents",
            json={"granted": [["documents", "session"], ["screen", "session"]]},
        )
        assert r.json()["state"] == "reviewing_exposed_services"

        # services
        r = client.post(self._B(sid) + "/services", json={"acknowledged": True})
        assert r.json()["state"] == "finalizing"

        # finalize → completed + bootstrap marcado
        r = client.post(self._B(sid) + "/finalize")
        assert r.status_code == 200
        assert r.json()["node_installation_id"]
        assert client.get("/api/v1/wizard/status").json()["first_boot_complete"] is True

    def test_server_profile_skips_consents(self, client) -> None:
        sid = client.post("/api/v1/wizard/form/start").json()["session_id"]
        client.post(self._B(sid) + "/profile", json={"profile_kind": "server"})
        client.post(
            self._B(sid) + "/locale",
            json={"language_code": "en", "keyboard_layout": "us", "timezone": "UTC"},
        )
        client.post(self._B(sid) + "/network", json={"decision": "offline_continue"})
        # server NO es personal → tenant salta directo a reviewing_exposed_services
        r = client.post(self._B(sid) + "/tenant", json={"decision": "defer"})
        assert r.json()["state"] == "reviewing_exposed_services"

    def test_invalid_profile_is_422(self, client) -> None:
        sid = client.post("/api/v1/wizard/form/start").json()["session_id"]
        r = client.post(self._B(sid) + "/profile", json={"profile_kind": "nope"})
        assert r.status_code == 422

    def test_services_not_acknowledged_blocks(self, client) -> None:
        sid = client.post("/api/v1/wizard/form/start").json()["session_id"]
        client.post(self._B(sid) + "/profile", json={"profile_kind": "personal_desktop"})
        client.post(
            self._B(sid) + "/locale",
            json={"language_code": "es", "keyboard_layout": "es", "timezone": "UTC"},
        )
        client.post(self._B(sid) + "/network", json={"decision": "connected"})
        client.post(self._B(sid) + "/tenant", json={"decision": "defer"})
        client.post(self._B(sid) + "/consents", json={"granted": []})
        # acknowledged=False → fail-closed (FR-023)
        r = client.post(self._B(sid) + "/services", json={"acknowledged": False})
        assert r.status_code == 422

    def test_exposed_services_list(self, client) -> None:
        r = client.get("/api/v1/wizard/form/exposed-services")
        assert r.status_code == 200
        services = r.json()["services"]
        assert len(services) >= 1
        assert {"service_name", "interface", "protocol", "human_description"} <= set(
            services[0].keys()
        )
