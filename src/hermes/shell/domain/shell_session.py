"""ShellSession — sesión activa de la Hermes Shell en el SO.

DDD: el dominio del shell sabe de:
  - usuario humano local autenticado (hermes-user)
  - tenant binding activo (puede ser None en personal-desktop)
  - vista activa (chat | workspace | skills | audit | settings)
  - estado de la conexión con el runtime (connected | reconnecting | offline)

NO sabe de:
  - GTK4 widgets
  - DBus protocol
  - sistema de archivos

La capa presentation (GTK4) consume estos VOs; el binding ocurre en
infrastructure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4


class ShellView(StrEnum):
    """Vistas principales de la shell — corresponden a items del sidebar."""

    HOME = "home"
    CHAT = "chat"
    WORKSPACE = "workspace"
    SKILLS = "skills"
    AUDIT = "audit"
    INTEGRATIONS = "integrations"
    TASKS = "tasks"
    REMOTE = "remote"
    SETTINGS = "settings"


class RuntimeLinkState(StrEnum):
    """Estado del puente con hermes-runtime.service via DBus."""

    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    OFFLINE = "offline"
    DEGRADED = "degraded"


class ScreenLockPolicy(StrEnum):
    """FR-042: la shell NUNCA permite que el lock pause al agente.

    Solo dos modos:
      DISABLED — no hay lock automático (default Agents OS Edition)
      MANUAL_VISUAL_PRIVACY — usuario fuerza lock para privacidad visual,
        pero el agente sigue trabajando 24/7 por debajo (FR-042 invariante)
    """

    DISABLED = "disabled"
    MANUAL_VISUAL_PRIVACY = "manual_visual_privacy"


@dataclass(slots=True)
class ShellSession:
    """Sesión activa de un humano en la Hermes Shell."""

    session_id: UUID
    human_user_id: str
    tenant_id: UUID | None
    active_view: ShellView
    runtime_link_state: RuntimeLinkState
    screen_lock_policy: ScreenLockPolicy
    started_at: datetime
    last_interaction_at: datetime
    pending_consents: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.screen_lock_policy == ScreenLockPolicy.DISABLED:
            pass  # default sano para Agents OS Edition
        # Invariante FR-042: el screen_lock NUNCA pausa el agente.
        # Aquí solo registramos la política de presentación; el agente
        # vive en otro proceso systemd-system y no depende de la sesión.

    def switch_view(self, view: ShellView) -> None:
        self.active_view = view
        self.last_interaction_at = datetime.now(tz=UTC)

    def mark_runtime_link(self, state: RuntimeLinkState) -> None:
        self.runtime_link_state = state


def start_session(
    *,
    human_user_id: str,
    tenant_id: UUID | None = None,
) -> ShellSession:
    """Factory: arranca una nueva sesión Hermes Shell."""
    now = datetime.now(tz=UTC)
    return ShellSession(
        session_id=uuid4(),
        human_user_id=human_user_id,
        tenant_id=tenant_id,
        active_view=ShellView.HOME,
        runtime_link_state=RuntimeLinkState.OFFLINE,
        screen_lock_policy=ScreenLockPolicy.DISABLED,
        started_at=now,
        last_interaction_at=now,
    )
