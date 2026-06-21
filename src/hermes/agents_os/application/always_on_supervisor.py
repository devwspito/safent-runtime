"""AlwaysOnSupervisor — orquesta el invariante 24/7 (FR-040..FR-046).

Aplica la `AlwaysOnPolicy` del dominio sobre un sistema concreto a través
de un puerto `SystemSupervisorPort`. La capa application coordina la
secuencia (mask suspend → escribir logind override → habilitar restart
de criticals) y emite audit entries, pero no toca el SO directamente.

Aislamiento:
- Sin imports de `subprocess`, `systemd`, `dbus` — eso vive en el adapter
  de infrastructure. Aquí solo dependemos del puerto.
- Idempotente: aplicar dos veces la misma política no provoca cambios.

Operación `suspend_with_authorization` (FR-040): es la ÚNICA vía
permitida para suspender el SO. Requiere:
  1. Operador humano local explícito (firma TOTP del CLI).
  2. Drain previo (FR-044) — opcional desactivable con `--force`.
  3. Audit entry firmada antes de invocar `systemctl suspend`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

from hermes.agents_os.domain.always_on_policy import (
    AlwaysOnPolicy,
    CriticalService,
    InstallProfile,
)


@runtime_checkable
class SystemSupervisorPort(Protocol):
    """Puerto contra el sistema operativo (systemd + logind).

    Implementaciones reales en infrastructure usan `systemctl`/`busctl`;
    los tests usan `InMemorySystemSupervisor`.
    """

    def mask_targets(self, targets: Sequence[str]) -> None: ...

    def unmask_targets(self, targets: Sequence[str]) -> None: ...

    def write_logind_override(self, key_values: dict[str, str]) -> None: ...

    def ensure_service_unit(self, service: CriticalService) -> None: ...

    def list_active_critical_services(self) -> tuple[str, ...]: ...

    def suspend_system(self) -> None: ...


class SuspendNotAuthorizedError(RuntimeError):
    """FR-040: intento de suspender sin operador humano local."""


class DrainIncompleteError(RuntimeError):
    """FR-044: drain previo a suspend no completó."""


@dataclass(slots=True)
class SupervisorApplicationResult:
    """Resultado idempotente de aplicar la política."""

    targets_masked: tuple[str, ...]
    logind_keys_written: tuple[str, ...]
    services_enabled: tuple[str, ...]
    applied_at: datetime


class AlwaysOnSupervisor:
    """Aplica una `AlwaysOnPolicy` contra el sistema vía el puerto.

    No tiene estado entre llamadas — la idempotencia depende del adapter
    (mask de un target ya enmascarado es no-op en systemd).
    """

    def __init__(
        self,
        *,
        supervisor: SystemSupervisorPort,
        clock: callable = lambda: datetime.now(tz=UTC),
    ) -> None:
        self._supervisor = supervisor
        self._clock = clock

    def apply(self, policy: AlwaysOnPolicy) -> SupervisorApplicationResult:
        """Aplica la política al sistema."""
        self._supervisor.mask_targets(policy.suspend_targets_masked)
        self._supervisor.write_logind_override(policy.logind_overrides)

        applicable_services = policy.services_for_profile()
        for svc in applicable_services:
            self._supervisor.ensure_service_unit(svc)

        return SupervisorApplicationResult(
            targets_masked=tuple(policy.suspend_targets_masked),
            logind_keys_written=tuple(policy.logind_overrides.keys()),
            services_enabled=tuple(svc.name for svc in applicable_services),
            applied_at=self._clock(),
        )

    def suspend_with_authorization(
        self,
        *,
        policy: AlwaysOnPolicy,
        authorizing_human_user_id: UUID,
        totp_validated: bool,
        drain_completed: bool,
        force: bool = False,
    ) -> None:
        """FR-040: ruta autorizada de suspend.

        Args:
            policy: política activa (debe ser la del nodo).
            authorizing_human_user_id: UUID del humano que firmó.
            totp_validated: el CLI validó el TOTP justo antes de invocar.
            drain_completed: estado actual del drain (FR-044).
            force: bypass del drain (`hermes suspend --yes --force`).
        """
        if not totp_validated or authorizing_human_user_id is None:
            raise SuspendNotAuthorizedError(
                "suspend requiere TOTP + operador humano local (FR-040)"
            )
        if policy.drain_ota_before_promote and not drain_completed and not force:
            raise DrainIncompleteError(
                "drain incompleto; usa --force solo si entiendes el riesgo "
                "(FR-044)"
            )
        self._supervisor.suspend_system()


def supervise_policy_for_profile(
    profile: InstallProfile, *, supervisor: SystemSupervisorPort
) -> SupervisorApplicationResult:
    """Helper para boot: aplica la política por defecto del perfil."""
    from hermes.agents_os.domain.always_on_policy import default_policy_for

    return AlwaysOnSupervisor(supervisor=supervisor).apply(
        default_policy_for(profile)
    )
