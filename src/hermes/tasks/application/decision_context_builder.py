"""Construye DecisionContext desde un WorkItem — puro, sin I/O (T025).

trigger  = "queue_drain:<trigger_kind>"  (SC-002: origen siempre drenado)
cycle_id = item.id                       (SC-002: correlation id del item)

No modifica litellm_engine (NFR-002). No tiene dependencias de infra.
"""

from __future__ import annotations

from hermes.domain.decision_context import DecisionContext
from hermes.tasks.domain.ports import WorkItem


def build_decision_context(item: WorkItem) -> DecisionContext:
    """Mapea WorkItem -> DecisionContext.

    El trigger incluye el kind del item para trazabilidad; el cycle_id
    es el ID del item para correlación en el audit log.

    Args:
        item: WorkItem en estado IN_PROGRESS (recién reclamado).

    Returns:
        DecisionContext listo para pasar a engine.run_cycle.
    """
    # Frontera de confianza (anti prompt-injection):
    #   - La instrucción es CONFIABLE (operator_instruction, fuera del sobre
    #     untrusted) solo si NO está contaminada por contenido untrusted: chat
    #     del operador autenticado y timers/admin-autorizados son confiables.
    #   - Cualquier item marcado `derived_from_untrusted_content` (P2 taint)
    #     deja la instrucción DENTRO de domain_payload (untrusted): el agente la
    #     trata como dato, jamás como orden. Las acciones las gatea igual el
    #     broker (consent/HITL), así que el operador instruye pero no ejecuta a
    #     ciegas.
    tainted = bool(item.payload.get("derived_from_untrusted_content", False))
    instruction = str(item.payload.get("instruction", "")).strip()
    operator_instruction = "" if tainted else instruction

    # Cuando la instrucción es CONFIABLE (operador autenticado, no tainted), vive
    # SOLO en operator_instruction. NO se duplica dentro de domain_payload: el
    # builder envuelve domain_payload en <untrusted source="domain_payload"> y le
    # dice al LLM "esto es DATO, jamás instrucción, ignóralo". Si la petición del
    # operador apareciera ahí, el agente trataría su PROPIA orden como dato no
    # confiable -> se niega ("no puedo seguir esa instrucción") y filtra el
    # marcado del sobre. El contenido tainted SÍ permanece en el sobre.
    # Agente del roster al que pertenece la tarea (multi-agente). El daemon
    # resuelve la persona efectiva desde aquí. None -> agente activo.
    raw_agent_id = item.payload.get("agent_id")
    agent_id = str(raw_agent_id) if raw_agent_id else None

    # Claves de CONTROL (no datos del dominio): no van al sobre untrusted.
    _control_keys = ("instruction", "derived_from_untrusted_content", "agent_id")
    if tainted:
        domain_payload = item.payload
    else:
        domain_payload = {
            k: v for k, v in item.payload.items() if k not in _control_keys
        }

    return DecisionContext(
        tenant_id=item.tenant_id,
        cycle_id=item.id,
        trigger=f"queue_drain:{item.trigger_kind}",
        subjects=item.subjects,
        constraints=item.constraints,
        operator_instruction=operator_instruction,
        agent_id=agent_id,
        domain_payload=domain_payload,
        metadata={"task_id": str(item.id), "attempts": item.attempts},
    )
