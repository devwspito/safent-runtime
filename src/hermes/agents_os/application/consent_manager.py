"""ConsentManager — capability-based consent estilo macOS (FR-013).

Spec 003 invariante:
- En perfil ``personal-desktop`` el agente NO accede a una categoría de
  datos del usuario humano sin consentimiento explícito previo.
- El consentimiento es por capability + scope, revocable en tiempo real.
- Cada concesión y revocación queda en el audit log local con cadena
  hash (FR-016).

Capabilities (estilo macOS):
    DOCUMENTS, DOWNLOADS, DESKTOP_FILES,
    CAMERA, MICROPHONE,
    NETWORK_LOCAL,
    PACKAGE_MANAGER, SYSTEM_SETTINGS, TERMINAL,
    FILESYSTEM_FULL,

Scope: ``"once"`` (un solo replay) | ``"session"`` (hasta fin de sesión
del usuario humano) | ``"persistent"`` (hasta revocación explícita).

Constitución IV: fail-closed por defecto. Si una skill solicita ejecutar
una acción que requiere capability X y NO hay consent activo →
``ConsentDenied``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

if TYPE_CHECKING:
    from hermes.agents_os.infrastructure.sqlite_consent_repo import (
        SQLiteConsentRepository,
    )


class Capability(StrEnum):
    DOCUMENTS = "documents"
    DOWNLOADS = "downloads"
    DESKTOP_FILES = "desktop_files"
    CAMERA = "camera"
    MICROPHONE = "microphone"
    SCREEN_CAPTURE = "screen"
    NETWORK_LOCAL = "network_local"
    PACKAGE_MANAGER = "package_manager"
    SYSTEM_SETTINGS = "system_settings"
    TERMINAL = "terminal"
    FILESYSTEM_FULL = "filesystem_full"
    # --- feature 007: OS-native capabilities (append-only, default-deny) ---
    SYSTEM_SERVICES = "system_services"   # observar/operar servicios systemd
    SYSTEM_INFO = "system_info"           # /proc, /sys, uname (read-only)
    UDEV_DEVICES = "udev_devices"         # enumerar dispositivos (read-only)
    AUDIO_DEVICES = "audio_devices"       # enumerar fuentes/sinks PipeWire (RO)
    SCHEDULER = "scheduler"               # crear/borrar entradas allow-list timer
    # --- feature 009: browser confinement (append-only, default-deny) ---
    BROWSER = "browser"                   # proceso agent-browser confinado en netns + Landlock
    # --- 2026-07-05 audit: confinar el CONTROLLER CDP de agent-browser ---
    # El controller (agent-browser --cdp) es HIJO del daemon (uid 880) y hereda el
    # ruleset RUNTIME AMPLIO (que da READ a master.key vía `/var` RX). Este perfil
    # MÁS ESTRECHO se aplica al binario agent-browser vía shim antes de execv: da lo
    # que el controller necesita (usr/lib/proc/tmp/dev + browser-sessions) pero NIEGA
    # el keystore (`/var/lib/hermes/master.key`, shell-state.db, keys). NO grantable
    # al agente — es el perfil del propio controller. Ver project_safent_agent_browser_go_nogo_fase4.
    BROWSER_CONTROLLER = "browser_controller"
    # --- host-operation MVP: pointer + keyboard input injection ---
    INPUT_CONTROL = "input_control"       # mover/clicar ratón + teclear en el compositor
    # --- P0-2: confinamiento Landlock del PROPIO daemon (defense-in-depth) ---
    # NO es grantable al agente — es el perfil con el que el daemon se autoconfina.
    RUNTIME = "runtime"                   # daemon self-confinement (2ª capa LSM sobre systemd)


class ConsentScope(StrEnum):
    ONCE = "once"
    SESSION = "session"
    PERSISTENT = "persistent"


class ConsentDenied(RuntimeError):
    """No hay consent activo para la capability solicitada."""


@dataclass(frozen=True, slots=True)
class Consent:
    consent_id: UUID = field(default_factory=uuid4)
    tenant_id: UUID | None = None
    human_operator_id: UUID | None = None
    capability: Capability = Capability.DOCUMENTS
    scope: ConsentScope = ConsentScope.ONCE
    granted_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    usage_count: int = 0
    last_used_at: datetime | None = None

    def is_active(self, *, now: datetime) -> bool:
        if self.revoked_at is not None:
            return False
        if self.expires_at is not None and now >= self.expires_at:
            return False
        return True


class ConsentManager:
    """Capability-based consent manager (FR-013 / FR-054).

    Optional ``repo`` enables SQLite persistence so that consent state
    survives runtime restarts (FR-054 requirement: revocation must be
    atomic and pre-flight must be consistent after restart).

    When repo is supplied:
      - ``grant`` / ``revoke`` / ``use`` persist each state change.
      - ``load_from_repo`` hydrates in-memory state from DB on startup.
    """

    def __init__(
        self,
        *,
        repo: "SQLiteConsentRepository | None" = None,
    ) -> None:
        self._by_key: dict[tuple[UUID, Capability], Consent] = {}
        self._granted_log: list[Consent] = []
        self._revoked_log: list[Consent] = []
        self._repo = repo
        if repo is not None:
            self._hydrate_from_repo(repo)

    def _hydrate_from_repo(self, repo: "SQLiteConsentRepository") -> None:
        """Load active consents from persistent store on startup."""
        for consent in repo.load_active():
            key = (consent.human_operator_id, consent.capability)
            self._by_key[key] = consent
            self._granted_log.append(consent)

    def grant(
        self,
        *,
        tenant_id: UUID,
        human_operator_id: UUID,
        capability: Capability,
        scope: ConsentScope,
        session_ttl_s: int = 8 * 3600,
    ) -> Consent:
        """Otorga un consent. Reemplaza el activo previo si existía."""
        now = datetime.now(tz=UTC)
        expires_at = None
        if scope == ConsentScope.SESSION:
            expires_at = now + timedelta(seconds=session_ttl_s)
        consent = Consent(
            tenant_id=tenant_id,
            human_operator_id=human_operator_id,
            capability=capability,
            scope=scope,
            granted_at=now,
            expires_at=expires_at,
        )
        self._by_key[(human_operator_id, capability)] = consent
        self._granted_log.append(consent)
        if self._repo is not None:
            self._repo.save(consent)
        return consent

    def revoke(
        self, *, human_operator_id: UUID, capability: Capability
    ) -> Consent | None:
        """Revoca el consent activo de un (operator, capability)."""
        key = (human_operator_id, capability)
        existing = self._by_key.get(key)
        if existing is None:
            return None
        now = datetime.now(tz=UTC)
        revoked = Consent(
            consent_id=existing.consent_id,
            tenant_id=existing.tenant_id,
            human_operator_id=existing.human_operator_id,
            capability=existing.capability,
            scope=existing.scope,
            granted_at=existing.granted_at,
            expires_at=existing.expires_at,
            revoked_at=now,
            usage_count=existing.usage_count,
            last_used_at=existing.last_used_at,
        )
        self._by_key.pop(key)
        self._revoked_log.append(revoked)
        if self._repo is not None:
            self._repo.save(revoked)
        return revoked

    def assert_active(
        self,
        *,
        human_operator_id: UUID,
        capability: Capability,
    ) -> Consent:
        """Fail-closed: lanza ``ConsentDenied`` si no hay consent activo."""
        consent = self._by_key.get((human_operator_id, capability))
        now = datetime.now(tz=UTC)
        if consent is None or not consent.is_active(now=now):
            raise ConsentDenied(
                f"No hay consent activo para capability={capability.value} "
                f"operator={human_operator_id}. "
                "El cliente debe otorgarlo desde el panel agéntico (FR-013)."
            )
        return consent

    def use(
        self,
        *,
        human_operator_id: UUID,
        capability: Capability,
    ) -> Consent:
        """Marca un uso de un consent. Si era ``ONCE``, lo invalida tras este uso.

        Invariante de concurrencia:
            ``use()`` es check-then-act (assert_active → mutate → revoke-if-ONCE)
            atómico SOLO bajo el modelo de ejecución single-event-loop sin threads
            en el path de dispatch. El dict ``_by_key`` es in-process; Python GIL
            + asyncio garantizan que ninguna corutina interleave entre el check y
            la escritura dentro de un mismo frame de evento.

            Si en el futuro ``ConsentManager`` se mueve a un store concurrente
            real (multi-proceso, SQLite compartido entre workers, Redis, etc.),
            ``use()`` necesita una operación compare-and-swap (CAS) o un lock
            distribuido para evitar double-spend. La firma del método y la
            semántica external NO cambian; sólo la implementación de la capa de
            persistencia debe garantizar la atomicidad.
        """
        consent = self.assert_active(
            human_operator_id=human_operator_id, capability=capability
        )
        # Incrementa contador (creando nueva instancia ya que es frozen).
        used = Consent(
            consent_id=consent.consent_id,
            tenant_id=consent.tenant_id,
            human_operator_id=consent.human_operator_id,
            capability=consent.capability,
            scope=consent.scope,
            granted_at=consent.granted_at,
            expires_at=consent.expires_at,
            revoked_at=consent.revoked_at,
            usage_count=consent.usage_count + 1,
            last_used_at=datetime.now(tz=UTC),
        )
        self._by_key[(human_operator_id, capability)] = used
        if self._repo is not None:
            self._repo.save(used)
        # Si ONCE, revocar tras este uso.
        if used.scope == ConsentScope.ONCE:
            self.revoke(
                human_operator_id=human_operator_id, capability=capability
            )
        return used

    def list_active(
        self, *, human_operator_id: UUID
    ) -> tuple[Consent, ...]:
        now = datetime.now(tz=UTC)
        return tuple(
            c
            for (op_id, _), c in self._by_key.items()
            if op_id == human_operator_id and c.is_active(now=now)
        )

    # Capabilities seeded as PERSISTENT on first boot (autonomous defaults).
    # FULL AUTÓNOMO por defecto (decisión del dueño, 2026-06-12): TODAS las
    # capacidades se conceden de fábrica para que el Cerebro pueda hacer LITERAL
    # TODO con el hardware y el software del SO. Única excluida: RUNTIME (perfil de
    # auto-confinamiento del daemon, no concedible al agente). El dueño capa luego;
    # el kill-switch + la denylist + el audit son el suelo inapelable.
    _AUTONOMOUS_DEFAULTS: frozenset[Capability] = frozenset({
        Capability.DOCUMENTS,
        Capability.DOWNLOADS,
        Capability.DESKTOP_FILES,
        Capability.BROWSER,
        Capability.SCREEN_CAPTURE,
        Capability.NETWORK_LOCAL,
        Capability.SYSTEM_INFO,
        Capability.SYSTEM_SERVICES,
        Capability.UDEV_DEVICES,
        Capability.AUDIO_DEVICES,
        Capability.SCHEDULER,
        # NOTA (V-3): TERMINAL, INPUT_CONTROL, FILESYSTEM_FULL, PACKAGE_MANAGER,
        # SYSTEM_SETTINGS, CAMERA, MICROPHONE quedan FUERA a propósito — son
        # irreversibles / alto blast-radius y SIEMPRE pasan por la tarjeta HITL
        # ámbar (el dueño aprueba cada vez). El código ahora COINCIDE con el
        # comentario de abajo (antes se contradecían: V-3). Concederlas de fábrica
        # abría el moat.
    })
    # Capabilities intentionally NOT seeded — must go through the HITL amber card
    # each time they are needed. Excluded because they are irreversible or high
    # blast-radius (decision: "Autónomo" product mode, 2026-06-12):
    #   INPUT_CONTROL    — pointer/keyboard injection; begin_computer_use is
    #                      persistent_forbidden/CTRL-3 and must be minted per-session.
    #                      Opening apps does NOT require input_control (activate_app
    #                      uses desktop_files/documents), so excluding this does not
    #                      prevent app launching.
    #   TERMINAL         — arbitrary command execution; always HITL.
    #   FILESYSTEM_FULL  — unrestricted FS access; always HITL.
    #   PACKAGE_MANAGER  — installs/removes system packages; always HITL.
    #   SYSTEM_SETTINGS  — modifies OS configuration; always HITL.
    #   CAMERA           — privacy-sensitive sensor; always HITL.
    #   MICROPHONE       — privacy-sensitive sensor; always HITL.
    #   RUNTIME          — daemon self-confinement profile; not grantable to the agent.

    def seed_defaults(
        self,
        *,
        human_operator_id: UUID,
        tenant_id: UUID,
    ) -> tuple[Consent, ...]:
        """Grant autonomous-safe capabilities as PERSISTENT for the operator.

        Only capabilities in ``_AUTONOMOUS_DEFAULTS`` are seeded. High-risk or
        privacy-sensitive capabilities (INPUT_CONTROL, TERMINAL, FILESYSTEM_FULL,
        PACKAGE_MANAGER, SYSTEM_SETTINGS, CAMERA, MICROPHONE) are intentionally
        excluded so they must be approved via the HITL amber card on each use.

        Idempotent: skips any capability that already has an entry (active OR
        previously revoked — revoked entries live in ``_revoked_log``). Revocations
        made by the user are therefore never overwritten.

        Returns the newly created consents (empty tuple if all were already present).
        """
        seeded: list[Consent] = []
        already_seen = self._keys_ever_seen(human_operator_id)
        for cap in self._AUTONOMOUS_DEFAULTS:
            if cap in already_seen:
                continue
            consent = self.grant(
                tenant_id=tenant_id,
                human_operator_id=human_operator_id,
                capability=cap,
                scope=ConsentScope.PERSISTENT,
            )
            seeded.append(consent)
        return tuple(seeded)

    def _keys_ever_seen(self, human_operator_id: UUID) -> frozenset[Capability]:
        """Return capabilities that have any record (active or revoked) for operator."""
        active = frozenset(
            cap
            for (op_id, cap) in self._by_key
            if op_id == human_operator_id
        )
        revoked = frozenset(
            c.capability
            for c in self._revoked_log
            if c.human_operator_id == human_operator_id
        )
        return active | revoked

    @property
    def granted_log(self) -> tuple[Consent, ...]:
        return tuple(self._granted_log)

    @property
    def revoked_log(self) -> tuple[Consent, ...]:
        return tuple(self._revoked_log)
