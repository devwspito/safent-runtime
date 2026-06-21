"""InMemoryFirstBootWizard — application service del wizard de first-boot.

Cumple `FirstBootWizardPort` con un state machine inmutable. La UI
(GTK4 panel agéntico o CLI) consume estos snapshots y enseña la
pantalla correspondiente.

Reglas dominio:
  - START → COLLECTING_PROFILE → COLLECTING_LOCALE → COLLECTING_NETWORK
    → COLLECTING_TENANT_BINDING → COLLECTING_CONSENTS [solo personal]
    → REVIEWING_EXPOSED_SERVICES → FINALIZING → COMPLETED
  - ABANDONED y FALLBACK_TRADITIONAL_UI son terminales y se pueden
    alcanzar desde cualquier estado no-completed.
  - finalize() es fail-closed: si falta info obligatoria → raise.
  - Solo `personal_desktop` requiere screen de CONSENTS.

Implementación in-memory para tests y para single-tenant boot. El
adapter Postgres (server) lo escribe en la migration 022.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

# Importamos los enums + value objects desde el contract canónico.
import sys
from pathlib import Path

def _resolve_spec_dir() -> Path:
    """Devuelve el directorio del spec 003 que contiene contracts/.

    Busca primero en el repo (dev), luego en la imagen OCI baked
    (/opt/hermes/specs/003-agents-os-edition).
    """
    candidates = [
        Path(__file__).parents[4] / "specs" / "003-agents-os-edition",
        Path("/opt/hermes/specs/003-agents-os-edition"),
    ]
    for candidate in candidates:
        if (candidate / "contracts" / "__init__.py").exists():
            return candidate
    raise RuntimeError(
        "spec 003 contracts not found in repo nor /opt/hermes/specs/"
    )


_SPEC_DIR = _resolve_spec_dir()
if str(_SPEC_DIR) not in sys.path:
    sys.path.insert(0, str(_SPEC_DIR))

from contracts.first_boot_wizard_port import (  # noqa: E402
    ConsentInitialSelection,
    DiskEncryptionDecision,
    ExposedServiceDescriptor,
    LocaleSelection,
    NetworkDecision,
    TenantBindingIntent,
    WizardConsentScreenSkipped,
    WizardExposedServicesNotReviewed,
    WizardSessionNotFound,
    WizardSnapshot,
    WizardState,
    WizardStateInvalid,
)
from contracts.agents_os_image_port import (  # noqa: E402
    InstallProfileKind,
    OtaUpdateChannelKind,
)


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _empty_snapshot(*, agent_driven: bool) -> WizardSnapshot:
    now = _now()
    return WizardSnapshot(
        wizard_session_id=uuid4(),
        state=WizardState.NOT_STARTED,
        agent_driven=agent_driven,
        started_at=now,
        updated_at=now,
        completed_at=None,
        abandoned_at=None,
        collected_profile_kind=None,
        collected_locale=None,
        collected_network_decision=None,
        collected_disk_encryption=None,
        collected_tenant_binding=None,
        collected_initial_consents=None,
        reviewed_exposed_services=False,
        collected_channel=None,
        produced_node_installation_id=None,
    )


_PERSONAL_PROFILES = {
    InstallProfileKind.PERSONAL_DESKTOP,
}


class InMemoryFirstBootWizard:
    """Backend in-memory del wizard."""

    def __init__(self) -> None:
        self._sessions: dict[UUID, WizardSnapshot] = {}

    async def start(self, *, agent_driven: bool) -> WizardSnapshot:
        snap = replace(
            _empty_snapshot(agent_driven=agent_driven),
            state=WizardState.COLLECTING_PROFILE,
            updated_at=_now(),
        )
        self._sessions[snap.wizard_session_id] = snap
        return snap

    async def set_profile(
        self,
        *,
        wizard_session_id: UUID,
        profile_kind: InstallProfileKind,
    ) -> WizardSnapshot:
        return self._advance(
            wizard_session_id,
            require_state=WizardState.COLLECTING_PROFILE,
            next_state=WizardState.COLLECTING_LOCALE,
            updates={"collected_profile_kind": profile_kind},
        )

    async def set_locale(
        self,
        *,
        wizard_session_id: UUID,
        locale: LocaleSelection,
    ) -> WizardSnapshot:
        return self._advance(
            wizard_session_id,
            require_state=WizardState.COLLECTING_LOCALE,
            next_state=WizardState.COLLECTING_NETWORK,
            updates={"collected_locale": locale},
        )

    async def set_network(
        self,
        *,
        wizard_session_id: UUID,
        decision: NetworkDecision,
    ) -> WizardSnapshot:
        return self._advance(
            wizard_session_id,
            require_state=WizardState.COLLECTING_NETWORK,
            next_state=WizardState.COLLECTING_TENANT_BINDING,
            updates={"collected_network_decision": decision},
        )

    async def set_tenant_binding(
        self,
        *,
        wizard_session_id: UUID,
        intent: TenantBindingIntent,
    ) -> WizardSnapshot:
        current = self._fetch(wizard_session_id)
        next_state = (
            WizardState.COLLECTING_CONSENTS
            if current.collected_profile_kind in _PERSONAL_PROFILES
            else WizardState.REVIEWING_EXPOSED_SERVICES
        )
        return self._advance(
            wizard_session_id,
            require_state=WizardState.COLLECTING_TENANT_BINDING,
            next_state=next_state,
            updates={"collected_tenant_binding": intent},
        )

    async def set_initial_consents(
        self,
        *,
        wizard_session_id: UUID,
        consents: ConsentInitialSelection,
    ) -> WizardSnapshot:
        return self._advance(
            wizard_session_id,
            require_state=WizardState.COLLECTING_CONSENTS,
            next_state=WizardState.REVIEWING_EXPOSED_SERVICES,
            updates={"collected_initial_consents": consents},
        )

    async def review_exposed_services(
        self,
        *,
        wizard_session_id: UUID,
        services: tuple[ExposedServiceDescriptor, ...],
        acknowledged: bool,
    ) -> WizardSnapshot:
        if not acknowledged:
            raise WizardExposedServicesNotReviewed(
                "FR-023: el usuario debe reconocer la lista de servicios"
            )
        return self._advance(
            wizard_session_id,
            require_state=WizardState.REVIEWING_EXPOSED_SERVICES,
            next_state=WizardState.FINALIZING,
            updates={"reviewed_exposed_services": True},
        )

    async def finalize(self, *, wizard_session_id: UUID) -> WizardSnapshot:
        current = self._fetch(wizard_session_id)
        self._assert_finalizable(current)
        node_id = uuid4()
        snap = replace(
            current,
            state=WizardState.COMPLETED,
            updated_at=_now(),
            completed_at=_now(),
            produced_node_installation_id=node_id,
            collected_channel=OtaUpdateChannelKind.STABLE,
            collected_disk_encryption=(
                current.collected_disk_encryption
                or DiskEncryptionDecision.INHERITED_FROM_PHASE1
            ),
        )
        self._sessions[wizard_session_id] = snap
        return snap

    async def abandon(
        self, *, wizard_session_id: UUID, reason: str
    ) -> WizardSnapshot:
        current = self._fetch(wizard_session_id)
        if current.state == WizardState.COMPLETED:
            raise WizardStateInvalid(
                "no se puede abandonar una sesión COMPLETED"
            )
        snap = replace(
            current,
            state=WizardState.ABANDONED,
            updated_at=_now(),
            abandoned_at=_now(),
        )
        self._sessions[wizard_session_id] = snap
        return snap

    async def fallback_to_traditional_ui(
        self, *, wizard_session_id: UUID, cause: str
    ) -> WizardSnapshot:
        current = self._fetch(wizard_session_id)
        if current.state == WizardState.COMPLETED:
            return current
        snap = replace(
            current,
            state=WizardState.FALLBACK_TRADITIONAL_UI,
            updated_at=_now(),
            agent_driven=False,
        )
        self._sessions[wizard_session_id] = snap
        return snap

    async def get_snapshot(
        self, *, wizard_session_id: UUID
    ) -> WizardSnapshot:
        return self._fetch(wizard_session_id)

    # Helpers --------------------------------------------------------------

    def _fetch(self, sid: UUID) -> WizardSnapshot:
        if sid not in self._sessions:
            raise WizardSessionNotFound(str(sid))
        return self._sessions[sid]

    def _advance(
        self,
        sid: UUID,
        *,
        require_state: WizardState,
        next_state: WizardState,
        updates: dict[str, Any],
    ) -> WizardSnapshot:
        current = self._fetch(sid)
        if current.state != require_state:
            raise WizardStateInvalid(
                f"esperaba estado {require_state} pero está en {current.state}"
            )
        snap = replace(
            current,
            state=next_state,
            updated_at=_now(),
            **updates,
        )
        self._sessions[sid] = snap
        return snap

    def _assert_finalizable(self, snap: WizardSnapshot) -> None:
        if snap.state != WizardState.FINALIZING:
            raise WizardStateInvalid(
                f"finalize requiere estado FINALIZING, está {snap.state}"
            )
        if snap.collected_profile_kind is None:
            raise WizardStateInvalid("falta profile_kind")
        if snap.collected_locale is None:
            raise WizardStateInvalid("falta locale")
        if snap.collected_network_decision is None:
            raise WizardStateInvalid("falta network_decision")
        if snap.collected_tenant_binding is None:
            raise WizardStateInvalid("falta tenant_binding")
        if not snap.reviewed_exposed_services:
            raise WizardExposedServicesNotReviewed(
                "FR-023: servicios expuestos no revisados"
            )
        if (
            snap.collected_profile_kind in _PERSONAL_PROFILES
            and snap.collected_initial_consents is None
        ):
            raise WizardConsentScreenSkipped(
                "personal_desktop requiere set_initial_consents (FR-013)"
            )
