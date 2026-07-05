"""T036 — CapabilityRegistry (CTRL-3/4/6/TOP-1/3).

Tabla declarativa `tool_name → CapabilityBinding`. Fuente de verdad server-side
del riesgo y la capability requerida. El LLM NUNCA dicta el riesgo.

REGLAS DURAS (firmadas por security-engineer / threat-model §3):

  AUTO-EJECUTABLES (risk=LOW, auto_executable=True):
    `read_file`, `list_dir` (filesystem reads).
    `memory`, `session_search` (F4: agent-internal, tenant-confined, PII-gated).
    OS-native READ_ONLY tools (screenshot, list_services, etc.).
    Ningún filesystem write, terminal, API externa, ni skill_manage.

  TODO write/delete/move/rename/terminal/package/system_settings/api_call
  externa => risk=HIGH, auto_executable=False (HITL obligatorio en P0,
  incluso con consent activo).

  TERMINAL/FILESYSTEM_FULL/PACKAGE_MANAGER/SYSTEM_SETTINGS:
    `persistent_forbidden=True` en el binding (la política se aplica en el
    broker en Wave B). El broker de Wave A lo expone para que el gate rechace
    el consent PERSISTENT antes de intentar el replay.

  TERMINAL con allow-list:
    `is_terminal_command_allowlisted(argv)` → bool. Sin match => HITL.
    Allow-list mínima y conservadora: solo herramientas de lectura pura que
    no aceptan argumentos de escritura ni shell-expansion.
    Binarios: `ls`, `cat`, `echo`, `pwd`, `whoami`, `id`, `date`, `env`.
    Ningún shell wrapper (`bash -c`, `sh -c`, `python -c`, etc.).

  DESCONOCIDO: `resolve` devuelve None → broker fail-closed.

Capa: application (orquesta domain). Sin framework. Sin I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from hermes.agents_os.application.consent_manager import Capability
from hermes.agents_os.domain.surface_kind import SurfaceKind
from hermes.capabilities.domain.ports import CapabilityBinding, CapabilityRegistryPort, RiskLevel

# ---------------------------------------------------------------------------
# ExtendedCapabilityBinding — añade metadato de política sobre CapabilityBinding
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExtendedCapabilityBinding(CapabilityBinding):
    """CapabilityBinding con metadato adicional de política.

    `persistent_forbidden`: si True, el consent PERSISTENT no está permitido
    para esta capability (CTRL-3/BROKER-7). El broker lo consulta antes de
    aceptar el ConsentScope de la operación.
    """

    persistent_forbidden: bool = False


# ---------------------------------------------------------------------------
# Allow-list de comandos de terminal (CTRL-6/BROKER-8)
# ---------------------------------------------------------------------------

# Binarios permitidos sin HITL. Todos son de LECTURA PURA y no aceptan
# argumentos que produzcan efectos de escritura en el SO.
# Justificación conservadora:
#   - `ls` / `pwd` / `whoami` / `id` / `date`: enumeración del entorno.
#   - `cat`: lectura de fichero (el path allowlist del FS adapter lo acota).
#   - `echo`: solo imprime argumentos, no escribe a ficheros (sin redirección).
#   - `env`: lista variables de entorno del proceso.
# NO incluidos (y razón):
#   - `bash`, `sh`, `python`, `perl`, `ruby`: shell wrappers que permiten
#     `bash -c 'rm -rf /'` (CWE-77/78).
#   - `find`, `grep`: aceptan `-exec` que puede derivar en escritura.
#   - `sudo`, `su`: escalada de privilegios.
#   - `curl`, `wget`, `nc`: red (ApiCall surface distinta).
#   - `cp`, `mv`, `rm`, `mkdir`, `chmod`, `chown`: escritura/destructivo.
_TERMINAL_ALLOWLIST_BINARIES: Final[frozenset[str]] = frozenset(
    {"ls", "cat", "echo", "pwd", "whoami", "id", "date", "env"}
)

# Prefijos de argumentos que denegaríamos incluso en binarios permitidos.
# `echo` con `>` o `>>` podría escribir, pero eso lo provee el shell, no
# el binario — sin shell wrapper, `echo` no puede redirigir.
# Esta lista es un extra de defensa en profundidad.
_TERMINAL_FORBIDDEN_ARG_PREFIXES: Final[tuple[str, ...]] = (
    ">",
    ">>",
    "|",
    ";",
    "&&",
    "||",
    "`",
    "$(",
)


def is_terminal_command_allowlisted(argv: list[str]) -> bool:
    """True si el comando terminal puede auto-ejecutarse sin HITL.

    Requisito CTRL-6/BROKER-8: allow-list de binario+args; sin match => HITL.

    Args:
        argv: lista de argumentos (argv[0] = binario, argv[1:] = args).
              No acepta strings con shell-expansion (el caller debe pasar
              argv ya parseado, NO pasar a `shell=True`).

    Returns:
        True SOLO si:
          - argv no está vacío.
          - El binario (basename de argv[0]) está en la allow-list.
          - Ningún argumento contiene prefijos de shell-injection.
        False en cualquier otro caso (fail-closed).
    """
    if not argv:
        return False

    binary = _extract_binary_name(argv[0])
    if binary not in _TERMINAL_ALLOWLIST_BINARIES:
        return False

    return not _has_forbidden_args(argv[1:])


def _extract_binary_name(path: str) -> str:
    """Devuelve el basename del binario (resiste `/usr/bin/ls` o `./ls`)."""
    return path.rsplit("/", 1)[-1]


def _has_forbidden_args(args: list[str]) -> bool:
    """True si algún argumento contiene un prefijo de shell-injection."""
    return any(
        arg.startswith(prefix)
        for arg in args
        for prefix in _TERMINAL_FORBIDDEN_ARG_PREFIXES
    )


# ---------------------------------------------------------------------------
# Tabla declarativa tool_name → ExtendedCapabilityBinding
# ---------------------------------------------------------------------------

# Nota: `surface_kind` es el tipo de surface adapter que maneja el tool.
# `required_capability` es el Capability.value que el ConsentManager debe
# tener activo (None = solo para READ_ONLY puro sin consent obligatorio;
# en P0 todos los auto-ejecutables son READ_ONLY del filesystem).

_REGISTRY_TABLE: Final[dict[str, ExtendedCapabilityBinding]] = {
    # ------------------------------------------------------------------
    # OS-NATIVE READ_ONLY — captura nativa vía broker (CTRL-P2-1/G1)
    # Migradas de tool_specs.py (era broker-bypass). Ahora executor='os_native'
    # garantiza consent+HITL+kill-switch en el CapabilityBroker.dispatch.
    # ------------------------------------------------------------------
    "screenshot": ExtendedCapabilityBinding(
        tool_name="screenshot",
        surface_kind=None,
        required_capability=Capability.SCREEN_CAPTURE.value,
        risk=RiskLevel.LOW,
        auto_executable=True,
        executor="os_native",
        persistent_forbidden=False,
    ),
    "screen_record": ExtendedCapabilityBinding(
        tool_name="screen_record",
        surface_kind=None,
        required_capability=Capability.SCREEN_CAPTURE.value,
        risk=RiskLevel.HIGH,
        auto_executable=False,
        executor="os_native",
        persistent_forbidden=False,
    ),
    # ------------------------------------------------------------------
    # OS-NATIVE READ_ONLY — feature 007 US1 (FR-001..004, SC-005/006)
    # Default-deny: requieren consent explícito antes de ejecutar.
    # Sin HITL (solo lectura, no mutan el SO).
    # ------------------------------------------------------------------
    "list_services": ExtendedCapabilityBinding(
        tool_name="list_services",
        surface_kind=None,
        required_capability=Capability.SYSTEM_SERVICES.value,
        risk=RiskLevel.LOW,
        auto_executable=True,
        executor="os_native",
        persistent_forbidden=False,
    ),
    "get_service_status": ExtendedCapabilityBinding(
        tool_name="get_service_status",
        surface_kind=None,
        required_capability=Capability.SYSTEM_SERVICES.value,
        risk=RiskLevel.LOW,
        auto_executable=True,
        executor="os_native",
        persistent_forbidden=False,
    ),
    "get_system_info": ExtendedCapabilityBinding(
        tool_name="get_system_info",
        surface_kind=None,
        required_capability=Capability.SYSTEM_INFO.value,
        risk=RiskLevel.LOW,
        auto_executable=True,
        executor="os_native",
        persistent_forbidden=False,
    ),
    "list_devices": ExtendedCapabilityBinding(
        tool_name="list_devices",
        surface_kind=None,
        required_capability=Capability.UDEV_DEVICES.value,
        risk=RiskLevel.LOW,
        auto_executable=True,
        executor="os_native",
        persistent_forbidden=False,
    ),
    "list_audio_devices": ExtendedCapabilityBinding(
        tool_name="list_audio_devices",
        surface_kind=None,
        required_capability=Capability.AUDIO_DEVICES.value,
        risk=RiskLevel.LOW,
        auto_executable=True,
        executor="os_native",
        persistent_forbidden=False,
    ),
    # ------------------------------------------------------------------
    # OS-NATIVE WRITE/HIGH — feature 007 US4 (FR-006..010)
    # HITL obligatorio. Denylist dura anti-autopirateo (FR-008/009).
    # ------------------------------------------------------------------
    "start_service": ExtendedCapabilityBinding(
        tool_name="start_service",
        surface_kind=None,
        required_capability=Capability.SYSTEM_SERVICES.value,
        risk=RiskLevel.HIGH,
        auto_executable=False,
        executor="os_native",
        persistent_forbidden=False,
    ),
    "stop_service": ExtendedCapabilityBinding(
        tool_name="stop_service",
        surface_kind=None,
        required_capability=Capability.SYSTEM_SERVICES.value,
        risk=RiskLevel.HIGH,
        auto_executable=False,
        executor="os_native",
        persistent_forbidden=False,
    ),
    "restart_service": ExtendedCapabilityBinding(
        tool_name="restart_service",
        surface_kind=None,
        required_capability=Capability.SYSTEM_SERVICES.value,
        risk=RiskLevel.HIGH,
        auto_executable=False,
        executor="os_native",
        persistent_forbidden=False,
    ),
    # ------------------------------------------------------------------
    # SCHEDULER — feature 007 US4 (FR-010)
    # schedule/unschedule = HIGH (HITL, muta allow-list).
    # list_scheduled_tasks = LOW READ_ONLY.
    # NUNCA crea units systemd arbitrarias (solo entradas allow-list timer).
    # ------------------------------------------------------------------
    "schedule_task": ExtendedCapabilityBinding(
        tool_name="schedule_task",
        surface_kind=None,
        required_capability=Capability.SCHEDULER.value,
        risk=RiskLevel.HIGH,
        auto_executable=False,
        executor="os_native",
        persistent_forbidden=False,
    ),
    "unschedule_task": ExtendedCapabilityBinding(
        tool_name="unschedule_task",
        surface_kind=None,
        required_capability=Capability.SCHEDULER.value,
        risk=RiskLevel.HIGH,
        auto_executable=False,
        executor="os_native",
        persistent_forbidden=False,
    ),
    "list_scheduled_tasks": ExtendedCapabilityBinding(
        tool_name="list_scheduled_tasks",
        surface_kind=None,
        required_capability=Capability.SCHEDULER.value,
        risk=RiskLevel.LOW,
        auto_executable=True,
        executor="os_native",
        persistent_forbidden=False,
    ),
    # ------------------------------------------------------------------
    # HOST-OPERATION MVP — input injection via SessionInputBridge
    # Security rationale:
    #   mouse_move: READ_ONLY/LOW/auto — pointer position only, no click.
    #     Still requires INPUT_CONTROL consent (default-deny).
    #   mouse_click: WRITE/HIGH/no-auto — can submit forms, auth dialogs,
    #     trigger destructive UI actions. HITL obligatorio.
    #   type_text: WRITE/HIGH/no-auto — can type passwords, shell commands,
    #     or arbitrary text in any focused widget. HITL obligatorio.
    # All three route through CapabilityBroker.dispatch (executor='os_native').
    # ------------------------------------------------------------------
    "mouse_move": ExtendedCapabilityBinding(
        tool_name="mouse_move",
        surface_kind=None,
        required_capability=Capability.INPUT_CONTROL.value,
        risk=RiskLevel.LOW,
        auto_executable=True,
        executor="os_native",
        persistent_forbidden=False,
    ),
    "mouse_click": ExtendedCapabilityBinding(
        tool_name="mouse_click",
        surface_kind=None,
        required_capability=Capability.INPUT_CONTROL.value,
        risk=RiskLevel.HIGH,
        auto_executable=False,
        executor="os_native",
        persistent_forbidden=False,
    ),
    "type_text": ExtendedCapabilityBinding(
        tool_name="type_text",
        surface_kind=None,
        required_capability=Capability.INPUT_CONTROL.value,
        risk=RiskLevel.HIGH,
        auto_executable=False,
        executor="os_native",
        persistent_forbidden=False,
    ),
    # ------------------------------------------------------------------
    # COMPUTER USE — autonomous GUI loop (begin_computer_use)
    #
    # HIGH / no-auto: the HITL amber card "¿Dejas que Safent controle
    # ratón/teclado para: <goal>?" fires before ANY screen interaction.
    # After approval the executor mints a SESSION-scoped INPUT_CONTROL
    # consent grant and runs the screenshot→vision→dispatch loop.
    #
    # persistent_forbidden=True: computer-use consent is inherently
    # bounded to a single task session. Persistent consent would allow
    # the agent to take over the screen without re-confirmation, which
    # is an unacceptable security posture.
    # ------------------------------------------------------------------
    "begin_computer_use": ExtendedCapabilityBinding(
        tool_name="begin_computer_use",
        surface_kind=None,
        required_capability=Capability.INPUT_CONTROL.value,
        risk=RiskLevel.HIGH,
        auto_executable=False,
        executor="os_native",
        persistent_forbidden=True,
    ),
    # ------------------------------------------------------------------
    # AUTO-EJECUTABLES — SOLO lecturas puras (CTRL-4/TOP-1)
    # ------------------------------------------------------------------
    "read_file": ExtendedCapabilityBinding(
        tool_name="read_file",
        surface_kind=SurfaceKind.FILESYSTEM,
        required_capability=Capability.DOCUMENTS.value,
        risk=RiskLevel.LOW,
        auto_executable=True,
        executor="surface_adapter",
        persistent_forbidden=False,
    ),
    "list_dir": ExtendedCapabilityBinding(
        tool_name="list_dir",
        surface_kind=SurfaceKind.FILESYSTEM,
        required_capability=Capability.DOCUMENTS.value,
        risk=RiskLevel.LOW,
        auto_executable=True,
        executor="surface_adapter",
        persistent_forbidden=False,
    ),
    # ------------------------------------------------------------------
    # FILESYSTEM — operaciones de escritura (HIGH, HITL obligatorio)
    # ------------------------------------------------------------------
    "write_file": ExtendedCapabilityBinding(
        tool_name="write_file",
        surface_kind=SurfaceKind.FILESYSTEM,
        required_capability=Capability.DOCUMENTS.value,
        risk=RiskLevel.HIGH,
        auto_executable=False,
        executor="surface_adapter",
        persistent_forbidden=False,
    ),
    "delete_file": ExtendedCapabilityBinding(
        tool_name="delete_file",
        surface_kind=SurfaceKind.FILESYSTEM,
        required_capability=Capability.FILESYSTEM_FULL.value,
        risk=RiskLevel.HIGH,
        auto_executable=False,
        executor="surface_adapter",
        persistent_forbidden=True,  # FILESYSTEM_FULL prohibido PERSISTENT
    ),
    "move_file": ExtendedCapabilityBinding(
        tool_name="move_file",
        surface_kind=SurfaceKind.FILESYSTEM,
        required_capability=Capability.FILESYSTEM_FULL.value,
        risk=RiskLevel.HIGH,
        auto_executable=False,
        executor="surface_adapter",
        persistent_forbidden=True,
    ),
    "rename_file": ExtendedCapabilityBinding(
        tool_name="rename_file",
        surface_kind=SurfaceKind.FILESYSTEM,
        required_capability=Capability.FILESYSTEM_FULL.value,
        risk=RiskLevel.HIGH,
        auto_executable=False,
        executor="surface_adapter",
        persistent_forbidden=True,
    ),
    # ------------------------------------------------------------------
    # TERMINAL — siempre HIGH; allow-list controla si HITL se puede omitir
    # En P0 TERMINAL es siempre HITL aunque esté en allow-list.
    # (El broker consulta is_terminal_command_allowlisted como gate adicional.)
    # ------------------------------------------------------------------
    "run_command": ExtendedCapabilityBinding(
        tool_name="run_command",
        surface_kind=SurfaceKind.TERMINAL,
        required_capability=Capability.TERMINAL.value,
        risk=RiskLevel.HIGH,
        auto_executable=False,
        executor="surface_adapter",
        persistent_forbidden=True,  # TERMINAL prohibido PERSISTENT (CTRL-3)
    ),
    "run_terminal": ExtendedCapabilityBinding(
        tool_name="run_terminal",
        surface_kind=SurfaceKind.TERMINAL,
        required_capability=Capability.TERMINAL.value,
        risk=RiskLevel.HIGH,
        auto_executable=False,
        executor="surface_adapter",
        persistent_forbidden=True,
    ),
    # ------------------------------------------------------------------
    # API_CALL — siempre HIGH (puede ser externa, rehidratación PII)
    # ------------------------------------------------------------------
    "api_call": ExtendedCapabilityBinding(
        tool_name="api_call",
        surface_kind=SurfaceKind.API_CALL,
        required_capability=Capability.NETWORK_LOCAL.value,
        risk=RiskLevel.HIGH,
        auto_executable=False,
        executor="surface_adapter",
        persistent_forbidden=False,
    ),
    "http_request": ExtendedCapabilityBinding(
        tool_name="http_request",
        surface_kind=SurfaceKind.API_CALL,
        required_capability=Capability.NETWORK_LOCAL.value,
        risk=RiskLevel.HIGH,
        auto_executable=False,
        executor="surface_adapter",
        persistent_forbidden=False,
    ),
    # ------------------------------------------------------------------
    # PEER_DELEGATION — FASE 3 (A2A cross-human). Siempre HIGH + no
    # auto_executable: gate como send_message (owner decision — "orquestación
    # INTERNA" delegate_task es NORMAL/fluida porque el sub-agente corre en la
    # MISMA jaula/broker; esto sale a OTRO ser humano fuera de la organización
    # del agente, así que exige el MISMO Aprobar/Rechazar que cualquier
    # comunicación saliente). persistent_forbidden=True: cada delegación es
    # una decisión nueva, nunca "recordar para siempre".
    # ------------------------------------------------------------------
    "delegate_to_colleague": ExtendedCapabilityBinding(
        tool_name="delegate_to_colleague",
        surface_kind=SurfaceKind.PEER_DELEGATION,
        required_capability=Capability.NETWORK_LOCAL.value,
        risk=RiskLevel.HIGH,
        auto_executable=False,
        executor="surface_adapter",
        persistent_forbidden=True,
    ),
    # ------------------------------------------------------------------
    # PACKAGE_MANAGER — siempre HIGH, prohibido PERSISTENT (CTRL-3)
    # ------------------------------------------------------------------
    "install_package": ExtendedCapabilityBinding(
        tool_name="install_package",
        surface_kind=SurfaceKind.PACKAGE_MANAGER,
        required_capability=Capability.PACKAGE_MANAGER.value,
        risk=RiskLevel.HIGH,
        auto_executable=False,
        executor="surface_adapter",
        persistent_forbidden=True,
    ),
    "remove_package": ExtendedCapabilityBinding(
        tool_name="remove_package",
        surface_kind=SurfaceKind.PACKAGE_MANAGER,
        required_capability=Capability.PACKAGE_MANAGER.value,
        risk=RiskLevel.HIGH,
        auto_executable=False,
        executor="surface_adapter",
        persistent_forbidden=True,
    ),
    # ------------------------------------------------------------------
    # SYSTEM_SETTINGS — siempre HIGH, prohibido PERSISTENT (CTRL-3)
    # ------------------------------------------------------------------
    "change_system_setting": ExtendedCapabilityBinding(
        tool_name="change_system_setting",
        surface_kind=SurfaceKind.SYSTEM_SETTINGS,
        required_capability=Capability.SYSTEM_SETTINGS.value,
        risk=RiskLevel.HIGH,
        auto_executable=False,
        executor="surface_adapter",
        persistent_forbidden=True,
    ),
    "update_system_setting": ExtendedCapabilityBinding(
        tool_name="update_system_setting",
        surface_kind=SurfaceKind.SYSTEM_SETTINGS,
        required_capability=Capability.SYSTEM_SETTINGS.value,
        risk=RiskLevel.HIGH,
        auto_executable=False,
        executor="surface_adapter",
        persistent_forbidden=True,
    ),
    # ------------------------------------------------------------------
    # SKILL_STORE — F3 skill_manage (HIGH, HITL obligatorio, firma v2)
    #
    # skill_manage WRITE → broker → SkillStoreAdapter (firma+persiste).
    # No se auto-ejecuta: toda mutación de skills exige HITL (constitución II).
    # skill_view / skills_list son READ (NousRisk.READ en nous_tool_risk_map) y
    # pasan por el gate nativo de Nous sin pasar por el surface adapter.
    # required_capability=None: el store de skills del agente es propiedad del
    # agente, no del usuario — no requiere consent per-capability.
    # persistent_forbidden=False: skills son durable por diseño.
    # ------------------------------------------------------------------
    "skill_manage": ExtendedCapabilityBinding(
        tool_name="skill_manage",
        surface_kind=SurfaceKind.SKILL_STORE,
        required_capability=None,
        risk=RiskLevel.HIGH,
        auto_executable=False,
        executor="surface_adapter",
        persistent_forbidden=False,
    ),
    # ------------------------------------------------------------------
    # MEMORY — F4 agent memory writes (LOW, auto-executable, tenant-confined)
    #
    # Classification rationale:
    #   - LOW + auto_executable: memory is reversible internal agent state.
    #     No external system effect. Confinement + PII gate replace HITL.
    #   - Two security conditions met (both required to keep LOW):
    #     (a) Tenant-confined: MemorySurfaceAdapter scopes to
    #         /var/lib/hermes/memory/<tenant_id>/. Path traversal prevented.
    #     (b) PII gated: TenantMemoryStore rejects content matching the
    #         strict threat-pattern scanner before writing.
    #   - Still goes through broker.dispatch (WRITE in nous_tool_risk_map)
    #     for audit (AuditKind.PROPOSAL_EXECUTED on every write).
    #   - required_capability=None: agent's own memory, not user-consented data.
    #   - persistent_forbidden=False: memory is durable by design.
    #
    # session_search is READ in nous_tool_risk_map → executed natively by Nous,
    # never reaches the broker. Registered here as LOW+auto for completeness
    # (in case a litellm tool calls it via the broker).
    # ------------------------------------------------------------------
    "memory": ExtendedCapabilityBinding(
        tool_name="memory",
        surface_kind=SurfaceKind.MEMORY,
        required_capability=None,
        risk=RiskLevel.LOW,
        auto_executable=True,
        executor="surface_adapter",
        persistent_forbidden=False,
    ),
    "session_search": ExtendedCapabilityBinding(
        tool_name="session_search",
        surface_kind=SurfaceKind.MEMORY,
        required_capability=None,
        risk=RiskLevel.LOW,
        auto_executable=True,
        executor="surface_adapter",
        persistent_forbidden=False,
    ),
    # ------------------------------------------------------------------
    # BROWSER — Fix-5 (CTRL-5 / TOP-1 preventivo)
    #
    # READ verbs (navigate / snapshot / read_url): observación pasiva de la
    # web; LOW + auto_executable. El contenido que ingieren activa el taint
    # del ciclo via CapturingToolHost._is_untrusted_read (tag "browser").
    #
    # WRITE verbs (click / type_): mutan el estado de la página (formularios,
    # sesiones, datos). HIGH + auto_executable=False → HITL obligatorio.
    # Justificación: click/type_ pueden autenticar, enviar formularios, o
    # exfiltrar datos a través de acciones del usuario. El HITL es el gate
    # correcto, no un allowlist app-layer (que el LLM podría bypassar).
    # ------------------------------------------------------------------
    "navigate": ExtendedCapabilityBinding(
        tool_name="navigate",
        surface_kind=SurfaceKind.BROWSER,
        required_capability=None,
        risk=RiskLevel.LOW,
        auto_executable=True,
        executor="surface_adapter",
        persistent_forbidden=False,
    ),
    "snapshot": ExtendedCapabilityBinding(
        tool_name="snapshot",
        surface_kind=SurfaceKind.BROWSER,
        required_capability=None,
        risk=RiskLevel.LOW,
        auto_executable=True,
        executor="surface_adapter",
        persistent_forbidden=False,
    ),
    "read_url": ExtendedCapabilityBinding(
        tool_name="read_url",
        surface_kind=SurfaceKind.BROWSER,
        required_capability=None,
        risk=RiskLevel.LOW,
        auto_executable=True,
        executor="surface_adapter",
        persistent_forbidden=False,
    ),
    "click": ExtendedCapabilityBinding(
        tool_name="click",
        surface_kind=SurfaceKind.BROWSER,
        required_capability=None,
        risk=RiskLevel.HIGH,
        auto_executable=False,
        executor="surface_adapter",
        persistent_forbidden=False,
    ),
    "type_": ExtendedCapabilityBinding(
        tool_name="type_",
        surface_kind=SurfaceKind.BROWSER,
        required_capability=None,
        risk=RiskLevel.HIGH,
        auto_executable=False,
        executor="surface_adapter",
        persistent_forbidden=False,
    ),
    # ------------------------------------------------------------------
    # DESKTOP_APP — operar apps reales via adapter (AT-SPI o UNO).
    # T061: riesgo fijado SERVER-SIDE, NUNCA por el LLM (anti prompt-injection).
    #
    # Regla: READ sobre app = LOW (observar sin mutar).
    #        WRITE sobre app = HIGH (muta estado externo, irreversible) → HITL.
    #
    # Mutación de ficheros del agente SIEMPRE via FILESYSTEM (determinista),
    # NUNCA via clicks AT-SPI sobre Nautilus (DESIGN.md Decisión 3).
    #
    # LibreOffice UNO (T060) es el adapter preferido para LO por ser determinista.
    # AT-SPI es fallback para operaciones no cubiertas por UNO.
    # ------------------------------------------------------------------

    # -- LibreOffice UNO (determinista) --
    # open_document: leer/abrir = LOW (no muta fichero externo, solo lo inspecciona).
    "lo_open_document": ExtendedCapabilityBinding(
        tool_name="lo_open_document",
        surface_kind=SurfaceKind.DESKTOP_APP,
        required_capability=Capability.DOCUMENTS.value,
        risk=RiskLevel.LOW,
        auto_executable=True,
        executor="surface_adapter",
        persistent_forbidden=False,
    ),
    # write_text: muta contenido del documento = HIGH (irreversible sin guardar explícito).
    # HITL obligatorio. persistent_forbidden=True: consent PERSISTENT implica que el
    # agente puede modificar documentos del humano sin re-confirmación → inaceptable.
    "lo_write_text": ExtendedCapabilityBinding(
        tool_name="lo_write_text",
        surface_kind=SurfaceKind.DESKTOP_APP,
        required_capability=Capability.DOCUMENTS.value,
        risk=RiskLevel.HIGH,
        auto_executable=False,
        executor="surface_adapter",
        persistent_forbidden=True,
    ),
    # save_document: persiste el documento en disco = HIGH (efecto irreversible externo).
    # HITL obligatorio. persistent_forbidden=True (misma razón que write_text).
    "lo_save_document": ExtendedCapabilityBinding(
        tool_name="lo_save_document",
        surface_kind=SurfaceKind.DESKTOP_APP,
        required_capability=Capability.DOCUMENTS.value,
        risk=RiskLevel.HIGH,
        auto_executable=False,
        executor="surface_adapter",
        persistent_forbidden=True,
    ),

    # -- Navegación de app (AT-SPI generic) --
    # navigate_app: foco, abrir menú, leer árbol accesibilidad = LOW (observar).
    "navigate_app": ExtendedCapabilityBinding(
        tool_name="navigate_app",
        surface_kind=SurfaceKind.DESKTOP_APP,
        required_capability=Capability.DESKTOP_FILES.value,
        risk=RiskLevel.LOW,
        auto_executable=True,
        executor="surface_adapter",
        persistent_forbidden=False,
    ),
    # activate_app: lanza o trae al frente una app instalada = LOW (no muta datos).
    # Rutas al surface_kind APP_LAUNCH (no DESKTOP_APP) porque el daemon (hermes,
    # sin display) no puede lanzar apps directamente — emite AppLaunchRequested(cmd)
    # al compositor (safentso-shell) via D-Bus. El adapter AppLaunchSurfaceAdapter
    # resuelve app_name → binario y llama al launch_emitter.
    "activate_app": ExtendedCapabilityBinding(
        tool_name="activate_app",
        surface_kind=SurfaceKind.APP_LAUNCH,
        required_capability=Capability.DESKTOP_FILES.value,
        risk=RiskLevel.LOW,
        auto_executable=True,
        executor="surface_adapter",
        persistent_forbidden=False,
    ),
    # click_app_element: clicar un elemento accesible (puede enviar formularios) = HIGH.
    # HITL obligatorio: el clic puede autenticar, borrar, o enviar datos.
    "click_app_element": ExtendedCapabilityBinding(
        tool_name="click_app_element",
        surface_kind=SurfaceKind.DESKTOP_APP,
        required_capability=Capability.DESKTOP_FILES.value,
        risk=RiskLevel.HIGH,
        auto_executable=False,
        executor="surface_adapter",
        persistent_forbidden=False,
    ),
    # type_in_app: teclear en un campo de una app = HIGH (muta estado, puede ser credencial).
    "type_in_app": ExtendedCapabilityBinding(
        tool_name="type_in_app",
        surface_kind=SurfaceKind.DESKTOP_APP,
        required_capability=Capability.DESKTOP_FILES.value,
        risk=RiskLevel.HIGH,
        auto_executable=False,
        executor="surface_adapter",
        persistent_forbidden=False,
    ),
    # ------------------------------------------------------------------
    # INSTALL — búsqueda e instalación de extensiones del SO agéntico.
    #
    # Search verbs (READ, LOW):
    #   search_mcp / search_skills / search_apps son solo de red (lectura);
    #   no mutan el SO ni instalan nada.  auto_executable=True porque son
    #   puras consultas informativas, equivalentes a list_services.
    #   required_capability=NETWORK_LOCAL: consultan registros remotos.
    #
    # Install / connect verbs (WRITE, HIGH):
    #   install_mcp / install_skill / install_app / connect_integration
    #   mutan el SO (añaden MCP, skills, apps, conectan OAuth).  HITL
    #   obligatorio — persistent_forbidden=True porque no debe poder
    #   instalarse repetidamente sin re-confirmación.  El scan de
    #   seguridad corre DENTRO del executor (no se puentea aquí).
    # ------------------------------------------------------------------
    "search_mcp": ExtendedCapabilityBinding(
        tool_name="search_mcp",
        surface_kind=None,
        required_capability=Capability.NETWORK_LOCAL.value,
        risk=RiskLevel.LOW,
        auto_executable=True,
        executor="install",
        persistent_forbidden=False,
    ),
    "search_skills": ExtendedCapabilityBinding(
        tool_name="search_skills",
        surface_kind=None,
        required_capability=Capability.NETWORK_LOCAL.value,
        risk=RiskLevel.LOW,
        auto_executable=True,
        executor="install",
        persistent_forbidden=False,
    ),
    "search_apps": ExtendedCapabilityBinding(
        tool_name="search_apps",
        surface_kind=None,
        required_capability=Capability.NETWORK_LOCAL.value,
        risk=RiskLevel.LOW,
        auto_executable=True,
        executor="install",
        persistent_forbidden=False,
    ),
    "install_mcp": ExtendedCapabilityBinding(
        tool_name="install_mcp",
        surface_kind=None,
        required_capability=Capability.PACKAGE_MANAGER.value,
        risk=RiskLevel.HIGH,
        auto_executable=False,
        executor="install",
        persistent_forbidden=True,
    ),
    "install_skill": ExtendedCapabilityBinding(
        tool_name="install_skill",
        surface_kind=None,
        required_capability=Capability.PACKAGE_MANAGER.value,
        risk=RiskLevel.HIGH,
        auto_executable=False,
        executor="install",
        persistent_forbidden=True,
    ),
    "install_app": ExtendedCapabilityBinding(
        tool_name="install_app",
        surface_kind=None,
        required_capability=Capability.PACKAGE_MANAGER.value,
        risk=RiskLevel.HIGH,
        auto_executable=False,
        executor="install",
        persistent_forbidden=True,
    ),
    "connect_integration": ExtendedCapabilityBinding(
        tool_name="connect_integration",
        surface_kind=None,
        required_capability=Capability.NETWORK_LOCAL.value,
        risk=RiskLevel.HIGH,
        auto_executable=False,
        executor="install",
        persistent_forbidden=True,
    ),
}


# ---------------------------------------------------------------------------
# CapabilityRegistry — implementa CapabilityRegistryPort
# ---------------------------------------------------------------------------


class CapabilityRegistry:
    """Implementación de CapabilityRegistryPort sobre la tabla declarativa.

    `resolve` devuelve None para tool_name desconocido → el broker hace
    fail-closed (REJECTED_BY_POLICY).

    La tabla es inmutable en tiempo de ejecución: el LLM nunca puede
    modificar el riesgo de un tool (anti prompt-injection, CTRL-4).
    """

    def resolve(self, tool_name: str) -> ExtendedCapabilityBinding | None:
        """Resuelve tool_name a su binding de seguridad.

        Returns:
            ExtendedCapabilityBinding si el tool está registrado.
            None si es desconocido (broker fail-closed).
        """
        return _REGISTRY_TABLE.get(tool_name)


# Satisface CapabilityRegistryPort structural check.
assert isinstance(CapabilityRegistry(), CapabilityRegistryPort)
