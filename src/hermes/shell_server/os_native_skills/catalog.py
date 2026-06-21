"""Catálogo de capacidades NATIVAS del SO (declarativo, sin framework)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class SkillRisk(StrEnum):
    """Mapea 1:1 con hermes.domain.tool_spec.ToolRisk (sin importarlo aquí)."""

    READ_ONLY = "read_only"
    WRITE_PROPOSAL = "write_proposal"


@dataclass(frozen=True, slots=True)
class OsNativeSkill:
    """Una capacidad nativa del SO expuesta como tool del agente.

    Attributes:
        name:         nombre snake_case de la tool (lo que el LLM invoca).
        description:  descripción para el LLM (qué hace, cuándo usarla).
        parameters_schema: JSON schema OpenAI de los argumentos.
        risk:         clasificación de riesgo (READ_ONLY ejecuta directo;
                      WRITE_PROPOSAL pasa por HITL/consent).
        capabilities: capabilities de consentimiento requeridas antes de ejecutar
                      (p.ej. 'screen', 'microphone'). Default-deny.
    """

    name: str
    description: str
    parameters_schema: dict
    risk: SkillRisk
    capabilities: tuple[str, ...] = field(default_factory=tuple)


SCREENSHOT = OsNativeSkill(
    name="screenshot",
    description=(
        "Captura una imagen del estado actual de la pantalla del sistema "
        "(el compositor completo: el navegador y cualquier aplicación de "
        "escritorio visible). Útil para inspeccionar qué hay en pantalla "
        "antes de decidir una acción. Devuelve la ruta del PNG guardado."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Por qué necesitas ver la pantalla ahora.",
            }
        },
        "required": [],
    },
    risk=SkillRisk.READ_ONLY,
    capabilities=("screen",),
)

SCREEN_RECORD = OsNativeSkill(
    name="screen_record",
    description=(
        "Graba un vídeo de la pantalla del sistema durante una duración "
        "dada, con audio del micrófono si está disponible. Captura el "
        "compositor entero (navegador y apps de escritorio). Devuelve la "
        "ruta del archivo .webm. Solicita esta capacidad como bundle al "
        "inicio de la tarea para evitar interrupciones."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "duration_seconds": {
                "type": "integer",
                "minimum": 1,
                "maximum": 600,
                "description": "Segundos a grabar (1–600).",
            },
            "with_audio": {
                "type": "boolean",
                "description": "Incluir audio del micrófono si hay fuente.",
            },
        },
        "required": ["duration_seconds"],
    },
    risk=SkillRisk.WRITE_PROPOSAL,
    capabilities=("screen", "microphone"),
)


MOUSE_MOVE = OsNativeSkill(
    name="mouse_move",
    description=(
        "Mueve el cursor del ratón a una posición absoluta (x, y) en la pantalla "
        "del sistema. Coordenadas en píxeles desde la esquina superior izquierda. "
        "Requiere consent INPUT_CONTROL activo. Operación de bajo riesgo (solo posición)."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "x": {"type": "number", "description": "Coordenada X en píxeles."},
            "y": {"type": "number", "description": "Coordenada Y en píxeles."},
        },
        "required": ["x", "y"],
    },
    risk=SkillRisk.READ_ONLY,
    capabilities=("input_control",),
)

MOUSE_CLICK = OsNativeSkill(
    name="mouse_click",
    description=(
        "Pulsa y suelta un botón del ratón en la posición actual. "
        "btn: 0=izquierdo, 1=derecho, 2=central. "
        "Requiere consent INPUT_CONTROL activo. "
        "Puede interactuar con formularios, menús y controles de la pantalla — HITL obligatorio."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "btn": {
                "type": "integer",
                "enum": [0, 1, 2],
                "description": "Botón: 0=izquierdo, 1=derecho, 2=central.",
            },
        },
        "required": ["btn"],
    },
    risk=SkillRisk.WRITE_PROPOSAL,
    capabilities=("input_control",),
)

TYPE_TEXT = OsNativeSkill(
    name="type_text",
    description=(
        "Escribe una cadena de texto sintetizando pulsaciones de tecla en el "
        "sistema operativo (como si un humano tecleara). Texto máximo 4096 chars. "
        "Requiere consent INPUT_CONTROL activo. Puede escribir en formularios, "
        "terminales, editores — HITL obligatorio."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "maxLength": 4096,
                "description": "Texto a teclear en el campo o aplicación activa.",
            },
        },
        "required": ["text"],
    },
    risk=SkillRisk.WRITE_PROPOSAL,
    capabilities=("input_control",),
)


BEGIN_COMPUTER_USE = OsNativeSkill(
    name="begin_computer_use",
    description=(
        "USE THIS ONLY for complex multi-step GUI automation that requires seeing the screen "
        "and iteratively clicking/typing (screenshot → vision → mouse/keyboard loop). "
        "DO NOT use this to simply open or launch an app — use activate_app for that. "
        "DO NOT use this to open a website or navigate to a URL — use navigate for that. "
        "Only invoke begin_computer_use when the task genuinely needs iterative visual "
        "interaction with a running application (e.g. filling a form, reading a dialog, "
        "operating a complex native UI). "
        "Requires explicit human approval (HITL) before any screen interaction begins. "
        "Provide a clear, specific goal describing what needs to be accomplished."
    ),
    parameters_schema={
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": (
                    "Clear description of what to accomplish via GUI interaction. "
                    "Be specific: name the application and the exact action needed."
                ),
            },
            "target_window": {
                "type": "string",
                "description": (
                    "Optional: name or title hint of the window to focus "
                    "(e.g. 'Firefox', 'LibreOffice Writer'). "
                    "Leave empty to let the agent find the right window."
                ),
            },
        },
        "required": ["goal"],
    },
    risk=SkillRisk.WRITE_PROPOSAL,
    capabilities=("input_control",),
)


OS_NATIVE_SKILLS: tuple[OsNativeSkill, ...] = (
    SCREENSHOT, SCREEN_RECORD, MOUSE_MOVE, MOUSE_CLICK, TYPE_TEXT, BEGIN_COMPUTER_USE
)


def skill_by_name(name: str) -> OsNativeSkill | None:
    for s in OS_NATIVE_SKILLS:
        if s.name == name:
            return s
    return None
