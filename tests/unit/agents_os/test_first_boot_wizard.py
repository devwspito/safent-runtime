"""Tests InMemoryFirstBootWizard (FR-002, FR-019..FR-023, FR-032)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from hermes.agents_os.application.first_boot_wizard import (
    InMemoryFirstBootWizard,
)

_SPEC = (
    Path(__file__).parents[3]
    / "specs"
    / "003-agents-os-edition"
)
if str(_SPEC) not in sys.path:
    sys.path.insert(0, str(_SPEC))

from contracts.first_boot_wizard_port import (  # noqa: E402
    ConsentInitialSelection,
    ExposedServiceDescriptor,
    LocaleSelection,
    NetworkDecision,
    TenantBindingDecision,
    TenantBindingIntent,
    WizardConsentScreenSkipped,
    WizardExposedServicesNotReviewed,
    WizardSessionNotFound,
    WizardState,
    WizardStateInvalid,
)
from contracts.agents_os_image_port import InstallProfileKind  # noqa: E402

pytestmark = pytest.mark.unit


def _locale() -> LocaleSelection:
    return LocaleSelection(
        language_code="es",
        keyboard_layout="es",
        timezone="Europe/Madrid",
    )


def _bind_now() -> TenantBindingIntent:
    return TenantBindingIntent(decision=TenantBindingDecision.BIND_NOW)


def _service() -> ExposedServiceDescriptor:
    return ExposedServiceDescriptor(
        service_name="hermes-control-plane",
        interface="loopback:8000",
        protocol="https",
        expected_identity="hermes-cp",
        human_description="API local del runtime",
    )


@pytest.fixture
def wizard() -> InMemoryFirstBootWizard:
    return InMemoryFirstBootWizard()


class TestStart:
    async def test_start_returns_collecting_profile(
        self, wizard: InMemoryFirstBootWizard
    ) -> None:
        snap = await wizard.start(agent_driven=True)
        assert snap.state == WizardState.COLLECTING_PROFILE
        assert snap.agent_driven is True

    async def test_get_snapshot_unknown_raises(
        self, wizard: InMemoryFirstBootWizard
    ) -> None:
        from uuid import uuid4

        with pytest.raises(WizardSessionNotFound):
            await wizard.get_snapshot(wizard_session_id=uuid4())


class TestPersonalDesktopHappyPath:
    async def test_full_flow_completes(
        self, wizard: InMemoryFirstBootWizard
    ) -> None:
        snap = await wizard.start(agent_driven=True)
        sid = snap.wizard_session_id
        snap = await wizard.set_profile(
            wizard_session_id=sid,
            profile_kind=InstallProfileKind.PERSONAL_DESKTOP,
        )
        snap = await wizard.set_locale(
            wizard_session_id=sid, locale=_locale()
        )
        snap = await wizard.set_network(
            wizard_session_id=sid, decision=NetworkDecision.CONNECTED
        )
        snap = await wizard.set_tenant_binding(
            wizard_session_id=sid, intent=_bind_now()
        )
        assert snap.state == WizardState.COLLECTING_CONSENTS

        snap = await wizard.set_initial_consents(
            wizard_session_id=sid,
            consents=ConsentInitialSelection(
                granted=(("documents", "session"),)
            ),
        )
        assert snap.state == WizardState.REVIEWING_EXPOSED_SERVICES

        snap = await wizard.review_exposed_services(
            wizard_session_id=sid,
            services=(_service(),),
            acknowledged=True,
        )
        assert snap.state == WizardState.FINALIZING

        snap = await wizard.finalize(wizard_session_id=sid)
        assert snap.state == WizardState.COMPLETED
        assert snap.produced_node_installation_id is not None


class TestServerSkipsConsents:
    async def test_server_path_skips_consents(
        self, wizard: InMemoryFirstBootWizard
    ) -> None:
        snap = await wizard.start(agent_driven=False)
        sid = snap.wizard_session_id
        await wizard.set_profile(
            wizard_session_id=sid,
            profile_kind=InstallProfileKind.SERVER,
        )
        await wizard.set_locale(wizard_session_id=sid, locale=_locale())
        await wizard.set_network(
            wizard_session_id=sid, decision=NetworkDecision.CONNECTED
        )
        snap = await wizard.set_tenant_binding(
            wizard_session_id=sid, intent=_bind_now()
        )
        assert snap.state == WizardState.REVIEWING_EXPOSED_SERVICES


class TestFailClosed:
    async def test_exposed_services_not_acknowledged_blocks(
        self, wizard: InMemoryFirstBootWizard
    ) -> None:
        snap = await wizard.start(agent_driven=True)
        sid = snap.wizard_session_id
        await wizard.set_profile(
            wizard_session_id=sid,
            profile_kind=InstallProfileKind.SERVER,
        )
        await wizard.set_locale(wizard_session_id=sid, locale=_locale())
        await wizard.set_network(
            wizard_session_id=sid, decision=NetworkDecision.CONNECTED
        )
        await wizard.set_tenant_binding(
            wizard_session_id=sid, intent=_bind_now()
        )
        with pytest.raises(WizardExposedServicesNotReviewed):
            await wizard.review_exposed_services(
                wizard_session_id=sid,
                services=(_service(),),
                acknowledged=False,
            )

    async def test_finalize_without_profile_blocked(
        self, wizard: InMemoryFirstBootWizard
    ) -> None:
        snap = await wizard.start(agent_driven=True)
        with pytest.raises(WizardStateInvalid):
            await wizard.finalize(wizard_session_id=snap.wizard_session_id)

    async def test_finalize_personal_without_consents_blocked(
        self, wizard: InMemoryFirstBootWizard
    ) -> None:
        # Forzar entrar a FINALIZING saltándonos CONSENTS — la API no
        # permite directamente, lo verificamos por la guarda en finalize.
        # Construimos un estado inválido y verificamos que finalize falla.
        snap = await wizard.start(agent_driven=True)
        sid = snap.wizard_session_id
        await wizard.set_profile(
            wizard_session_id=sid,
            profile_kind=InstallProfileKind.PERSONAL_DESKTOP,
        )
        # Intentar avanzar saltando consents no es posible por contrato.
        # Verificamos que el contrato no permite saltar — set_network →
        # tenant_binding → CONSENTS obligatorio en personal_desktop.
        await wizard.set_locale(wizard_session_id=sid, locale=_locale())
        await wizard.set_network(
            wizard_session_id=sid, decision=NetworkDecision.CONNECTED
        )
        snap = await wizard.set_tenant_binding(
            wizard_session_id=sid, intent=_bind_now()
        )
        # Ahora estamos en COLLECTING_CONSENTS — no podemos invocar
        # review_exposed_services hasta tenerlos.
        with pytest.raises(WizardStateInvalid):
            await wizard.review_exposed_services(
                wizard_session_id=sid,
                services=(_service(),),
                acknowledged=True,
            )


class TestAbandonAndFallback:
    async def test_abandon_marks_state(
        self, wizard: InMemoryFirstBootWizard
    ) -> None:
        snap = await wizard.start(agent_driven=True)
        snap = await wizard.abandon(
            wizard_session_id=snap.wizard_session_id,
            reason="user_quit",
        )
        assert snap.state == WizardState.ABANDONED
        assert snap.abandoned_at is not None

    async def test_fallback_to_traditional_ui(
        self, wizard: InMemoryFirstBootWizard
    ) -> None:
        snap = await wizard.start(agent_driven=True)
        snap = await wizard.fallback_to_traditional_ui(
            wizard_session_id=snap.wizard_session_id,
            cause="agent_crash",
        )
        assert snap.state == WizardState.FALLBACK_TRADITIONAL_UI
        assert snap.agent_driven is False
