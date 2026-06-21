"""Bridge: OS-native skills → ToolSpec del runtime Hermes.

Permite que el runtime registre las capacidades nativas del SO como tools que
el LLM puede invocar. El mapeo de riesgo respeta la garantía del CapturingToolHost:
READ_ONLY (screenshot) se ejecuta vía OsNativeDispatcher (que pasa por el broker
para consent+audit+kill-switch). WRITE_PROPOSAL (screen_record) se captura
como propuesta para HITL/consent antes de ejecutar.

CTRL-P2-1 (feature 007) — migración al broker:
    Los handlers READ_ONLY ya NO llaman directamente a EXECUTORS con
    asyncio.to_thread. Toda ejecución pasa por OsNativeDispatcher, el effector
    terminal de la rama os_native del CapabilityBroker (consent+HITL+kill-switch
    garantizados). El consent pre-flight se mantiene en _check_consent para el
    camino de fallback sin broker.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from .catalog import OS_NATIVE_SKILLS, OsNativeSkill, SkillRisk

# Skills de control GUI que el toolset NATIVO `computer_use` de Hermes (backend
# Wayland lumen-cua-driver) reemplaza → NO se exponen al LLM (que use la nativa).
_COMPUTER_USE_NATIVE_REPLACED: frozenset[str] = frozenset(
    {"mouse_click", "type_text", "begin_computer_use"}
)

if TYPE_CHECKING:
    from hermes.agents_os.application.consent_manager import ConsentManager
    from hermes.capabilities.infrastructure.os_native_dispatcher import OsNativeDispatcher


# Mapping catalog capability strings → Capability enum values.
# The catalog uses short names ('screen', 'microphone') that must match
# the Capability StrEnum values defined in consent_manager.py.
# feature 007 additions (CTRL-P2-21): nuevas capabilities OS-native.
_CATALOG_CAP_TO_ENUM_VALUE: dict[str, str] = {
    "screen": "screen",
    "microphone": "microphone",
    "system_services": "system_services",
    "system_info": "system_info",
    "udev_devices": "udev_devices",
    "audio_devices": "audio_devices",
    "scheduler": "scheduler",
    # host-operation MVP: pointer + keyboard injection
    "input_control": "input_control",
}


def _risk_to_tool_risk(risk: SkillRisk):
    from hermes.domain.tool_spec import ToolRisk

    return {
        SkillRisk.READ_ONLY: ToolRisk.READ_ONLY,
        SkillRisk.WRITE_PROPOSAL: ToolRisk.WRITE_PROPOSAL,
    }[risk]


def to_tool_spec(skill: OsNativeSkill, *, handler=None):
    """Convierte una OsNativeSkill en ToolSpec del runtime.

    handler: callable async para READ_ONLY (lo ejecuta Hermes). Para
    WRITE_PROPOSAL debe ser None (lo ejecuta el consumer tras HITL).
    """
    from hermes.domain.tool_spec import ToolSpec

    risk = _risk_to_tool_risk(skill.risk)
    return ToolSpec(
        name=skill.name,
        description=skill.description,
        parameters_schema=skill.parameters_schema,
        risk=risk,
        entity_type="os_surface",
        handler=handler,
    )


def _default_read_handler(
    skill_name: str,
    *,
    required_capabilities: tuple[str, ...],
    consent_manager: ConsentManager | None,
    human_operator_id: UUID | None,
    os_native_dispatcher: OsNativeDispatcher | None = None,
):
    """Handler async que delega al OsNativeDispatcher (CTRL-P2-1).

    El executor es bloqueante (D-Bus + GStreamer). El OsNativeDispatcher
    lo ejecuta en un thread interno (asyncio.to_thread), pero la llamada
    pasa por el effector del broker (consent+HITL+kill-switch garantizados).

    En producción SIEMPRE se inyecta os_native_dispatcher. En entornos
    headless sin dispatcher disponible, el fallback se gestiona por
    _execute_via_legacy_executor (función separada, no reemplaza este handler).
    """

    async def handler(args: dict) -> dict:
        _check_consent(
            required_capabilities=required_capabilities,
            consent_manager=consent_manager,
            human_operator_id=human_operator_id,
            skill_name=skill_name,
        )

        # Primary path: route through OsNativeDispatcher (broker's effector).
        # Consent+HITL+kill-switch gates are guaranteed to be in the chain (G1).
        if os_native_dispatcher is not None:
            return await os_native_dispatcher.execute(
                skill_name=skill_name, args=args
            )

        # Dispatcher not wired: delegate to the legacy executor bridge.
        # In personal-desktop profile this path is unreachable because
        # _check_consent fail-closes first if consent_manager is None.
        return await _execute_via_legacy_executor(skill_name, args)

    return handler


async def _execute_via_legacy_executor(skill_name: str, args: dict) -> dict:
    """FAIL-CLOSED stub — the raw executor path is no longer valid (CONDITION-1/G1).

    The only valid execution path is through OsNativeDispatcher (the broker's
    effector). Calling this function without a dispatcher means the security
    chain (consent + HITL + kill-switch + audit) is NOT in the call stack.
    Returning REJECTED instead of executing the raw executor ensures fail-closed
    behavior on non-desktop profiles (CTRL-P2-1/NFR-002).

    In production the dispatcher is always injected (_default_read_handler
    takes the `os_native_dispatcher is not None` branch and never reaches here).
    """
    return {
        "ok": False,
        "reason": (
            f"REJECTED: no OsNativeDispatcher injected for skill '{skill_name}'. "
            "Execution requires the broker chain (consent+HITL+audit). "
            "Inject an OsNativeDispatcher to enable this skill (CONDITION-1/CTRL-P2-1)."
        ),
    }


def _is_personal_desktop_profile() -> bool:
    """True when running inside a personal-desktop appliance image."""
    try:
        with open("/etc/agents-os-profile", encoding="utf-8") as fh:
            return fh.read().strip() == "personal-desktop"
    except OSError:
        return False


def _check_consent(
    *,
    required_capabilities: tuple[str, ...],
    consent_manager: ConsentManager | None,
    human_operator_id: UUID | None,
    skill_name: str,
) -> None:
    """Fail-closed consent gate for OS-native READ_ONLY skills.

    In personal-desktop profile both ``consent_manager`` and
    ``human_operator_id`` MUST be supplied — missing either raises
    ``ConsentDenied`` (fail-closed per FR-013 / constitución IV).

    In headless / test contexts both may be omitted to bypass the gate;
    the caller is responsible for only doing so in trusted environments.
    """
    if consent_manager is None or human_operator_id is None:
        if _is_personal_desktop_profile():
            from hermes.agents_os.application.consent_manager import (  # noqa: PLC0415
                ConsentDenied,
            )

            raise ConsentDenied(
                f"Consent gate not wired for skill '{skill_name}' on "
                "personal-desktop profile. ConsentManager and "
                "human_operator_id are required (FR-013 fail-closed)."
            )
        return

    from hermes.agents_os.application.consent_manager import Capability  # noqa: PLC0415

    for cap_str in required_capabilities:
        try:
            capability = Capability(cap_str)
        except ValueError:
            from hermes.agents_os.application.consent_manager import (  # noqa: PLC0415
                ConsentDenied,
            )
            raise ConsentDenied(
                f"Capability desconocida '{cap_str}' declarada por skill "
                f"'{skill_name}'. No se puede verificar el consent."
            )
        consent_manager.assert_active(
            human_operator_id=human_operator_id,
            capability=capability,
        )


def build_os_native_tool_specs(
    *,
    read_handlers: dict | None = None,
    consent_manager: ConsentManager | None = None,
    human_operator_id: UUID | None = None,
    os_native_dispatcher: OsNativeDispatcher | None = None,
) -> tuple:
    """Devuelve los ToolSpec de todas las OS-native skills.

    read_handlers: opcional {skill_name: async_handler} para las READ_ONLY.
        Si no se pasa una READ_ONLY, se usa el executor nativo por defecto.

    consent_manager + human_operator_id: si se pasan, cada handler READ_ONLY
        verificará consent activo para las capabilities declaradas antes de
        ejecutar (FR-013, constitución IV fail-closed). En tests/headless se
        pueden omitir para deshabilitar el gate.

    os_native_dispatcher: effector del broker (CTRL-P2-1). En producción
        SIEMPRE debe pasarse para que los handlers ruten por el broker.
        En headless/CI puede omitirse (usa el camino de fallback).
    """
    read_handlers = read_handlers or {}
    specs = []
    for skill in OS_NATIVE_SKILLS:
        # GUI-control reinventado → reemplazado por el toolset NATIVO computer_use
        # de Hermes (backend Wayland lumen-cua-driver). No registrar al LLM para
        # que use la tool nativa, no la custom (Hermes nativo sin más).
        if skill.name in _COMPUTER_USE_NATIVE_REPLACED:
            continue
        if skill.risk == SkillRisk.READ_ONLY:
            handler = read_handlers.get(skill.name) or _default_read_handler(
                skill.name,
                required_capabilities=skill.capabilities,
                consent_manager=consent_manager,
                human_operator_id=human_operator_id,
                os_native_dispatcher=os_native_dispatcher,
            )
        else:
            handler = None
        specs.append(to_tool_spec(skill, handler=handler))
    return tuple(specs)
