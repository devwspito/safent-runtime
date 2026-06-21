"""AlwaysOnPolicy — invariante 24/7 (FR-040..FR-046).

El SO nunca duerme. El agente nunca pausa. Esta clase es la política de
dominio que el supervisor en infrastructure aplica al systemd del nodo.

Reglas dominio (sin acoplamiento a sistema):

- ``suspend_targets_masked``: lista de targets de systemd que DEBEN estar
  enmascarados (`systemctl mask <target>`) en boot.
- ``logind_overrides``: configuración de ``logind.conf`` que asegura
  ``HandleLidSwitch=ignore`` y similares.
- ``critical_services``: procesos del agente que requieren
  ``Restart=always`` con backoff explícito.
- ``screen_lock_does_not_pause_agent``: invariante; en `personal-desktop`
  el screensaver/lock NO suspende el agente. Verificable por inspección
  de cgroups + estado del runtime.

Esta clase NO toca el sistema operativo — describe la política. El
adapter en ``infrastructure/`` la traduce a llamadas a systemctl + busctl.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class InstallProfile(StrEnum):
    """Heredado del spec 003 FR-003."""

    WORKSPACE_ONLY = "workspace-only"
    PERSONAL_DESKTOP = "personal-desktop"
    SERVER = "server"


# Targets de systemd que deben enmascararse para que el SO NUNCA suspenda
# por sí mismo. La única ruta hacia suspend pasa por `hermes suspend --yes`
# (cli/main.py), que requiere intervención humana explícita.
_DEFAULT_MASK_TARGETS: tuple[str, ...] = (
    "sleep.target",
    "suspend.target",
    "hibernate.target",
    "hybrid-sleep.target",
    "suspend-then-hibernate.target",
)

# Overrides en /etc/systemd/logind.conf.d/agents-os-always-on.conf
_DEFAULT_LOGIND_OVERRIDES: dict[str, str] = {
    "HandleLidSwitch": "ignore",
    "HandleLidSwitchDocked": "ignore",
    "HandleLidSwitchExternalPower": "ignore",
    "HandleSuspendKey": "ignore",
    "HandleHibernateKey": "ignore",
    "IdleAction": "ignore",
}


@dataclass(frozen=True, slots=True)
class CriticalService:
    """Proceso supervisado por systemd con política Restart=always."""

    name: str
    restart_policy: str = "always"
    restart_sec: int = 5
    burst_window_s: int = 600
    burst_max_restarts: int = 12
    profile_scope: tuple[InstallProfile, ...] = ()


_DEFAULT_CRITICAL_SERVICES: tuple[CriticalService, ...] = (
    CriticalService(
        name="hermes-runtime.service",
        profile_scope=(
            InstallProfile.WORKSPACE_ONLY,
            InstallProfile.PERSONAL_DESKTOP,
            InstallProfile.SERVER,
        ),
    ),
    CriticalService(
        name="hermes-control-plane.service",
        profile_scope=(InstallProfile.WORKSPACE_ONLY, InstallProfile.SERVER),
    ),
    CriticalService(
        name="hermes-remote-control.service",
        profile_scope=(
            InstallProfile.WORKSPACE_ONLY,
            InstallProfile.PERSONAL_DESKTOP,
            InstallProfile.SERVER,
        ),
    ),
    CriticalService(
        name="hermes-whisper.service",
        profile_scope=(
            InstallProfile.WORKSPACE_ONLY,
            InstallProfile.PERSONAL_DESKTOP,
            InstallProfile.SERVER,
        ),
    ),
    CriticalService(
        name="hermes-audit-tail.service",
        profile_scope=(
            InstallProfile.WORKSPACE_ONLY,
            InstallProfile.PERSONAL_DESKTOP,
            InstallProfile.SERVER,
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class AlwaysOnPolicy:
    """Política dominio del invariante 24/7.

    Atributos:
        profile: perfil de instalación al que aplica la política.
        suspend_targets_masked: targets systemd a enmascarar (FR-041).
        logind_overrides: pares clave-valor para logind.conf (FR-041, FR-042).
        critical_services: lista de servicios con Restart=always (FR-043).
        drain_ota_before_promote: si True, OTA hace graceful drain antes
            de promover slot B (FR-044). En personal-desktop puede ser
            opcional si la sesión humana lo solicita; en server siempre.
        screen_lock_pauses_agent: SIEMPRE False — el lock NO pausa el agente
            (FR-042). Expuesto como atributo para que los tests verifiquen
            el invariante con assertEqual.
    """

    profile: InstallProfile
    suspend_targets_masked: tuple[str, ...] = _DEFAULT_MASK_TARGETS
    logind_overrides: dict[str, str] = field(
        default_factory=lambda: dict(_DEFAULT_LOGIND_OVERRIDES)
    )
    critical_services: tuple[CriticalService, ...] = _DEFAULT_CRITICAL_SERVICES
    drain_ota_before_promote: bool = True
    screen_lock_pauses_agent: bool = False  # invariante FR-042

    def __post_init__(self) -> None:
        if self.screen_lock_pauses_agent is not False:
            raise ValueError(
                "screen_lock_pauses_agent DEBE ser False (FR-042 invariante)."
            )
        for target in self.suspend_targets_masked:
            if not target.endswith(".target"):
                raise ValueError(
                    f"suspend_targets_masked elemento {target!r} no es "
                    "un systemd target válido"
                )

    def services_for_profile(self) -> tuple[CriticalService, ...]:
        """Filtra los servicios críticos aplicables al perfil del nodo."""
        return tuple(
            svc
            for svc in self.critical_services
            if not svc.profile_scope or self.profile in svc.profile_scope
        )


def default_policy_for(profile: InstallProfile) -> AlwaysOnPolicy:
    """Devuelve la política por defecto del perfil dado.

    Pensada para uso en boot y en tests de invariante.
    """
    return AlwaysOnPolicy(profile=profile)
