"""Mapa canónico de tools de Nous → perfil de riesgo Hermes (F2).

Fuente de verdad server-side del riesgo de cada tool del catálogo de
NousResearch hermes-agent. NUNCA es decidida por el LLM ni por el
contenido de la llamada.

Clasificación (conservadora / fail-closed):
  READ  = LOW + auto_executable → el gate ejecuta nativo y audita.
  WRITE = HIGH → el gate captura como ToolCallProposal y delega en el broker.
  UNKNOWN → fail-closed: BLOCKED, la tool no se ejecuta.

Invariante de seguridad:
  - Cualquier tool ausente del mapa se bloquea (default-deny).
  - skill_manage es HIGH (F3 añadirá firma obligatoria).
  - execute_code / terminal / computer_use / process = HIGH (ejecución arbitraria).
  - Toda tool de escritura de red (send_message, discord, ha_call_service,
    feishu_drive_*, yb_send_*) = HIGH.
  - browser_* de snapshot/extracción = READ; de navegación/click/type = HIGH.

Para añadir una tool nueva: agregar al dict con la clasificación correcta
y ejecutar test_nous_tool_risk_map.py::test_no_unclassified_nous_tools
(falla si alguna tool del catálogo no está en el mapa).
"""

from __future__ import annotations

from enum import StrEnum


class NousRisk(StrEnum):
    """Nivel de riesgo de una tool de Nous desde la perspectiva de Hermes."""

    READ = "read"    # LOW+auto: el gate ejecuta nativo sin HITL.
    WRITE = "write"  # HIGH: el gate captura como proposal, broker decide.


# ---------------------------------------------------------------------------
# Catálogo completo — una entrada por tool registrada en el catálogo de Nous.
# Si cambia el catálogo de Nous, actualizar aquí Y el test de cobertura.
# ---------------------------------------------------------------------------

_NOUS_TOOL_RISK: dict[str, NousRisk] = {
    # ----------------------------------------------------------------
    # File tools
    # ----------------------------------------------------------------
    "read_file": NousRisk.READ,
    "search_files": NousRisk.READ,
    "list_dir": NousRisk.READ,          # lista directorio (no muta)
    "write_file": NousRisk.WRITE,       # escribe en filesystem
    "patch": NousRisk.WRITE,            # escribe en filesystem
    "create_dir": NousRisk.WRITE,       # crea directorio
    "delete_file": NousRisk.WRITE,      # borra (destructivo) — antes None → default-deny ciego

    # ----------------------------------------------------------------
    # Web tools — búsqueda/extracción = READ; taint de procedencia
    # se propaga por read_external_content en CycleOutput (CTRL-5).
    # ----------------------------------------------------------------
    "web_search": NousRisk.READ,
    "web_extract": NousRisk.READ,

    # ----------------------------------------------------------------
    # Browser tools
    # Snapshot/back = READ (no modifica estado externo).
    # Navigate/click/type/scroll/press = WRITE (modifica sesión web).
    # ----------------------------------------------------------------
    "browser_snapshot": NousRisk.READ,
    "browser_back": NousRisk.READ,
    "browser_get_images": NousRisk.READ,
    "browser_console": NousRisk.READ,
    "browser_navigate": NousRisk.WRITE,
    "browser_click": NousRisk.WRITE,
    "browser_type": NousRisk.WRITE,
    "browser_scroll": NousRisk.WRITE,
    "browser_press": NousRisk.WRITE,
    "browser_vision": NousRisk.READ,    # captura pantalla, sin efecto
    "browser_cdp": NousRisk.WRITE,      # CDP arbitrario = efectos desconocidos
    "browser_dialog": NousRisk.WRITE,   # interactúa con diálogos (click OK/Cancel)

    # ----------------------------------------------------------------
    # Computer use — acceso a pantalla y teclado del SO
    # ----------------------------------------------------------------
    "computer_use": NousRisk.WRITE,

    # ----------------------------------------------------------------
    # Code execution / terminal / process — ejecución arbitraria
    # ----------------------------------------------------------------
    "execute_code": NousRisk.WRITE,
    "terminal": NousRisk.WRITE,
    "process": NousRisk.WRITE,

    # ----------------------------------------------------------------
    # Messaging — envío de mensajes a terceros
    # ----------------------------------------------------------------
    "send_message": NousRisk.WRITE,
    "discord": NousRisk.WRITE,
    "discord_admin": NousRisk.WRITE,

    # ----------------------------------------------------------------
    # Home Assistant — control de dispositivos IoT
    # ----------------------------------------------------------------
    "ha_call_service": NousRisk.WRITE,  # actúa sobre dispositivos físicos
    "ha_get_state": NousRisk.READ,
    "ha_list_entities": NousRisk.READ,
    "ha_list_services": NousRisk.READ,

    # ----------------------------------------------------------------
    # Feishu / Lark — escritura en docs y comentarios
    # ----------------------------------------------------------------
    "feishu_doc_read": NousRisk.READ,
    "feishu_drive_list_comments": NousRisk.READ,
    "feishu_drive_list_comment_replies": NousRisk.READ,
    "feishu_drive_add_comment": NousRisk.WRITE,
    "feishu_drive_reply_comment": NousRisk.WRITE,

    # ----------------------------------------------------------------
    # Yuanbao messaging
    # ----------------------------------------------------------------
    "yb_query_group_info": NousRisk.READ,
    "yb_query_group_members": NousRisk.READ,
    "yb_search_sticker": NousRisk.READ,
    "yb_send_dm": NousRisk.WRITE,
    "yb_send_sticker": NousRisk.WRITE,

    # ----------------------------------------------------------------
    # X (Twitter) search — lectura pura
    # ----------------------------------------------------------------
    "x_search": NousRisk.READ,

    # ----------------------------------------------------------------
    # Kanban — lectura y escritura de tareas internas
    # ----------------------------------------------------------------
    "kanban_list": NousRisk.READ,
    "kanban_show": NousRisk.READ,
    "kanban_heartbeat": NousRisk.READ,
    "kanban_create": NousRisk.WRITE,
    "kanban_complete": NousRisk.WRITE,
    "kanban_block": NousRisk.WRITE,
    "kanban_unblock": NousRisk.WRITE,
    "kanban_comment": NousRisk.WRITE,
    "kanban_link": NousRisk.WRITE,

    # ----------------------------------------------------------------
    # Skill management — HIGH (F3 añade firma obligatoria)
    # Modificar skills del agente es una operación de alta consecuencia.
    # ----------------------------------------------------------------
    "skill_manage": NousRisk.WRITE,
    "skill_view": NousRisk.READ,
    "skills_list": NousRisk.READ,

    # ----------------------------------------------------------------
    # Memory / session — escritura en memoria del agente
    # ----------------------------------------------------------------
    "memory": NousRisk.WRITE,           # modifica MEMORY.md del agente
    "session_search": NousRisk.READ,

    # ----------------------------------------------------------------
    # Media generation — I/O externo (APIs de imagen/vídeo/TTS)
    # ----------------------------------------------------------------
    "image_generate": NousRisk.WRITE,
    "video_generate": NousRisk.WRITE,
    "text_to_speech": NousRisk.WRITE,
    "video_analyze": NousRisk.READ,
    "vision_analyze": NousRisk.READ,

    # ----------------------------------------------------------------
    # Delegation / orchestration
    # ----------------------------------------------------------------
    "delegate_task": NousRisk.WRITE,    # spawna subagente = efecto externo
    "mixture_of_agents": NousRisk.WRITE,

    # ----------------------------------------------------------------
    # Todo — escritura interna al agente (tarea local)
    # ----------------------------------------------------------------
    "todo": NousRisk.WRITE,

    # ----------------------------------------------------------------
    # Clarify — pregunta al usuario (efecto de canal de comunicación)
    # ----------------------------------------------------------------
    "clarify": NousRisk.WRITE,

    # ----------------------------------------------------------------
    # Cronjob — modifica el programador de tareas del SO
    # ----------------------------------------------------------------
    "cronjob": NousRisk.WRITE,
}


# Conjunto completo de tools del catálogo Nous — sirve para el test de
# cobertura que falla si una tool del catálogo queda sin clasificar.
NOUS_TOOL_CATALOG: frozenset[str] = frozenset(_NOUS_TOOL_RISK.keys())


def classify_nous_tool(tool_name: str) -> NousRisk | None:
    """Devuelve el perfil de riesgo de una tool de Nous.

    Returns:
        NousRisk si la tool está en el catálogo.
        None si la tool es desconocida (el gate aplica fail-closed).
    """
    return _NOUS_TOOL_RISK.get(tool_name)
