"""Puertos del BC `native_capabilities` — OS-native ToolSpec (feature 007, PIEZA 6).

Extiende el patrón de `shell_server/os_native_skills/catalog.py`
(catalog → executors → ToolSpec via `executor='os_native'`). El broker
(capabilities/) las invoca por su rama os_native sin tocarse
(Constitución I: NO BrowserPort/SurfaceAdapterPort).

Reglas DURAS de riesgo:
  - READ_ONLY  -> ejecuta directo bajo consent + audit (sin HITL).
  - HIGH       -> consent + HITL obligatorio + audit (muta el SO).
  - start/stop/restart_service: DENYLIST DURA terminal y pre-systemd
    (FR-008/009).
  - schedule_task/unschedule_task: SOLO crean/borran entradas en la allow-list
    de orígenes de timer; NUNCA units systemd arbitrarias (FR-010).

Nuevas skills nativas añadidas por feature 007:

  READ_ONLY (US1 / FR-001..004) — ejecutan directo, consent default-deny:
    list_services       capabilities=("system_services",)
    get_service_status  capabilities=("system_services",)
    get_system_info     capabilities=("system_info",)
    list_devices        capabilities=("udev_devices",)
    list_audio_devices  capabilities=("audio_devices",)

  HIGH (US4 / FR-006..010) — HITL obligatorio + denylist dura:
    start_service       capabilities=("system_services",) risk=HIGH
    stop_service        capabilities=("system_services",) risk=HIGH
    restart_service     capabilities=("system_services",) risk=HIGH
    schedule_task       capabilities=("scheduler",)       risk=HIGH
    unschedule_task     capabilities=("scheduler",)       risk=HIGH
    list_scheduled_tasks capabilities=("scheduler",)      risk=READ_ONLY
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol, runtime_checkable


class SkillRisk(StrEnum):
    """Riesgo de una OS-native skill.

    Mapea 1:1 con hermes.domain.tool_spec.ToolRisk + el riesgo efectivo del
    broker. READ_ONLY ejecuta directo; WRITE_PROPOSAL/HIGH pasan por HITL.

    P2 añade HIGH (write/operación del SO).
    """

    READ_ONLY = "read_only"
    WRITE_PROPOSAL = "write_proposal"
    HIGH = "high"   # feature 007: operación del SO (start/stop/restart, schedule)


@runtime_checkable
class ProtectedServiceDenylistPort(Protocol):
    """Denylist DURA anti-autopirateo (FR-008/FR-009, NFR-002).

    Conjunto COMPILADO e inmutable (no editable por el agente). Resuelve por
    IDENTIDAD CANÓNICA de unit systemd (no por cadena literal): aliases, rutas
    absolutas, `.service` implícito y `Names=` que apunten a un servicio
    protegido se rechazan igual (anti-aliasing).

    Decisión terminal e inapelable por HITL. Fail-closed: si la resolución
    canónica falla, trata como protegido.
    """

    def is_protected(self, unit: str) -> bool:
        """True si `unit` resuelve a un servicio protegido (frenos del agente).

        Mínimo inviolable: hermes-runtime, hermes-shell-server, hermes-consent,
        hermes-audit. Resolución via identidad canónica (systemctl show Id,Names).
        Fail-closed: True ante cualquier duda de resolución.
        """
        ...

    def protected_canonical_names(self) -> frozenset[str]:
        """Conjunto canónico de units protegidas (observabilidad / tests)."""
        ...


@runtime_checkable
class OsNativeDispatcherPort(Protocol):
    """Effector terminal de la rama `executor='os_native'` del broker.

    El broker (capability_broker.py), tras pasar consent+HITL+idempotencia,
    bifurca: si binding.executor == 'os_native' llama AQUÍ en vez de al
    SurfaceAdapterDispatcher. NO redefine ni esquiva ningún gate del broker
    (Constitución I/II/IV) — solo ejecuta el handler nativo del catálogo.

    Para start/stop/restart_service consulta ProtectedServiceDenylistPort
    ANTES de invocar systemd (rechazo terminal pre-SO).
    """

    async def execute(self, *, skill_name: str, args: dict) -> dict:
        """Ejecuta la OS-native skill por nombre. Devuelve el dict del executor.

        Raises / devuelve un dict con ok=False + reason de política si el
        skill opera sobre un servicio protegido (REJECTED_BY_POLICY). El broker
        mapea esto a ExecutionStatus.REJECTED_BY_POLICY + audit.
        """
        ...

    def supports(self, skill_name: str) -> bool:
        """True si este dispatcher conoce `skill_name` (catálogo os_native)."""
        ...
