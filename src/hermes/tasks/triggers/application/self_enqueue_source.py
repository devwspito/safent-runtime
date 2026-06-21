"""SelfEnqueueSource — auto-encolado de seguimiento (US3/FR-022/CTRL-P2-10).

Invocada por el orchestrator tras mark_completed cuando CycleOutput trae
un follow_up. NO es una corrutina de fondo.

Invariantes críticos:
  - cascade_depth ≤ 1 (cap de cascada — la comprobación real está en TriggerGate).
  - dedup_key obligatoria (None → rechazo).
  - enqueued_by HEREDADO de la tarea madre (no del follow_up).
  - Presupuesto/hora por origen gestionado por TriggerGate → repo.consume_budget.
  - derived_from_untrusted_content HEREDADO de la tarea madre (Fix-6/CTRL-5):
    si el ciclo madre leyó contenido externo no confiable, la hija hereda el
    taint → su instrucción va a domain_payload (untrusted), nunca a
    operator_instruction.

Esta clase es una capa delgada: valida la petición y delega en TriggerGate.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID

from hermes.tasks.triggers.domain.authorized_trigger_ports import AuthorizedTriggerType

if TYPE_CHECKING:
    from hermes.tasks.domain.ports import WorkQueuePort
    from hermes.tasks.triggers.application.trigger_gate import TriggerGate

logger = logging.getLogger("hermes.tasks.triggers.self_enqueue")


class SelfEnqueueSource:
    """Procesa el follow_up de una tarea completada.

    Diseño: delega TODA la lógica de gate + dedup + cascade + budget en
    TriggerGate.enqueue_from_trigger — no duplica.
    """

    def __init__(
        self,
        *,
        gate: TriggerGate,
        queue: WorkQueuePort,
    ) -> None:
        self._gate = gate
        self._queue = queue

    async def process_follow_up(
        self,
        *,
        parent_work_item_id: UUID,
        instruction: str,
        dedup_key: str | None,
        priority: int = 0,
        parent_read_external_content: bool = False,
    ) -> UUID | None:
        """Encola COMO MUCHO una tarea hija (FR-022). Devuelve task_id o None.

        El gate verifica:
          1. El origen self_enqueue está autorizado (allow-list).
          2. cascade_depth ≤ 1 (la madre no es ya hija de otra).
          3. dedup_key obligatoria.
          4. Presupuesto/hora no agotado.
          5. enqueued_by heredado de la madre.

        Fix-6 / CTRL-5:
          6. derived_from_untrusted_content heredado de la madre: si el ciclo
             madre leyó contenido externo no confiable, la tarea hija hereda el
             taint. La instrucción generada por el LLM desde contenido untrusted
             NUNCA se trata como operator_instruction (trusted) — queda en
             domain_payload (untrusted), gateada por HITL normal del broker.
        """
        if not dedup_key:
            logger.warning(
                "hermes.triggers.self_enqueue.missing_dedup_key",
                extra={"parent": str(parent_work_item_id)},
            )
            return None

        # El scope "autonomous" es el valor convenido para self_enqueue
        # (scope_validation='parent_task_kind' en el catálogo de tipos).
        return await self._gate.enqueue_from_trigger(
            trigger_type=AuthorizedTriggerType.SELF_ENQUEUE,
            scope_value="autonomous",
            instruction=instruction,
            dedup_key=dedup_key,
            priority=priority,
            derived_from_untrusted_content=parent_read_external_content,
            parent_work_item_id=parent_work_item_id,
        )
