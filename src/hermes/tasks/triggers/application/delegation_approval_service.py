"""DelegationApprovalService — FASE 3 A2A cross-human (RUNTIME/associate side).

Aplica el modelo HITL obligatorio a las peticiones de delegación ENTRANTES (un
mensaje que el asistente de OTRO humano de la organización dirige al humano
LOCAL). Vive en `tasks/triggers/application` (junto a `TriggerGate`, del que es
el ÚNICO caller para `trigger_type=EXTERNAL_DELEGATION`) porque orquesta el
MISMO pre-gate default-deny que timer/system_event/self_enqueue — nunca lo
bypassa ni lo sustituye.

Flujo (item 3/4 del diseño):
  1. `submit`: verifica de nuevo el sobre y guarda su prueba firmada junto al
     vínculo actual. La aprobación revalida ambos y la frescura; nunca atribuye
     una tarjeta antigua sin prueba a la conexión actual.
  2. `approve`: SOLO tras la decisión del humano LOCAL (`approved_by` viene
     SIEMPRE del canal D-Bus autenticado — GetConnectionUnixUser / operator
     token verificado — NUNCA de la envelope ni del payload):
       a. crea una conversación NUEVA (kind=CHAT_MESSAGE) para que el asistente
          de B responda por el carril normal de chat.
       b. mintea una autorización FRESCA y de un solo uso
          (AuthorizedTriggerType.EXTERNAL_DELEGATION, scope=from_employee_id)
          FIRMADA por `approved_by` — así `TriggerGate._resolve_enqueued_by`
          (que para timer/system_event/self_enqueue usa
          `trigger.created_by_admin_uuid`) produce SIEMPRE
          `enqueued_by == approved_by`, la garantía dura de "provenance" del
          diseño, con independencia de si YA existía una autorización previa
          para este par (una autorización pre-existente NUNCA se reutiliza
          para derivar enqueued_by — cada aprobación es una decisión NUEVA).
       c. llama a `TriggerGate.enqueue_from_trigger` con
          `derived_from_untrusted_content=True` SIEMPRE (la instrucción del par
          es contenido no confiable — fuerza HITL en cada efecto derivado que
          el broker vea después).
       d. revoca la autorización recién usada (de un solo uso — nunca queda
          una fila habilitada más tiempo del necesario para ESTE encolado).
  3. `reject`: marca la tarjeta 'rejected'. NO encola nada. La outbox existente
     de estados publica la decisión, separada de entrega y de texto resultado.

Default-deny: `approve`/`reject` son NO-OP (False/None) si la tarjeta no existe
o ya fue resuelta — nunca se re-resuelve ni se re-encola una fila ya decidida.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from hermes.tasks.domain.ports import WorkItemKind
from hermes.tasks.triggers.application.delegation_authority import DelegationAuthorityError
from hermes.tasks.triggers.domain.authorized_trigger_ports import (
    AuthorizedTriggerType,
    RiskCeiling,
)

if TYPE_CHECKING:
    from hermes.tasks.infrastructure.sqlite_pending_delegations import (
        SqlitePendingDelegationRepository,
    )
    from hermes.tasks.triggers.application.trigger_gate import TriggerGate
    from hermes.tasks.triggers.infrastructure.sqlite_authorized_trigger_repository import (
        SqliteAuthorizedTriggerRepository,
    )

logger = logging.getLogger("hermes.tasks.triggers.delegation_approval")


class DelegationApprovalService:
    """Orquesta submit/approve/reject de delegaciones entrantes (A2A)."""

    def __init__(
        self,
        *,
        pending_repo: SqlitePendingDelegationRepository,
        trigger_repo: SqliteAuthorizedTriggerRepository,
        gate: TriggerGate,
        conversation_repo: object,
        authority=None,
    ) -> None:
        self._pending = pending_repo
        self._trigger_repo = trigger_repo
        self._gate = gate
        self._conversations = conversation_repo
        self._authority = authority

    async def submit(self, *, envelope: dict) -> str:
        """Registra una DelegationEnvelope kind=request YA VERIFICADA.

        Idempotente por message_id (ver SqlitePendingDelegationRepository.submit).
        Devuelve el status de la fila ('pending' | 'approved' | 'rejected').
        """
        if self._authority is None:
            raise DelegationAuthorityError("request_unverified")
        proof = self._authority.capture(envelope)
        return self._pending.submit(envelope=envelope, proof=proof)

    def _admission_state(self, row):
        if row.admission_state:
            return row.admission_state
        try:
            self._verify(row.message_id)
        except (PermissionError, ValueError, KeyError, AttributeError):
            return "unverified"
        return None

    def _verify(self, message_id):
        if self._authority is None:
            raise DelegationAuthorityError("request_unverified")
        self._authority.validate(message_id)

    def list_pending(self) -> list[dict]:
        """Metadatos de las tarjetas pendientes (CTRL-P1-5 style: sin secretos)."""
        return [
            {
                "message_id": d.message_id,
                "from_employee_id": d.from_employee_id,
                "body": d.body,
                "issued_at": d.issued_at,
                "created_at": d.created_at,
                **({"admission_state": state} if (state := self._admission_state(d)) else {}),
            }
            for d in self._pending.list_pending()
        ]

    async def approve(
        self, *, message_id: str, approved_by: UUID
    ) -> UUID | None:
        """Aprueba UNA tarjeta pendiente. Devuelve el task_id encolado, o None
        si la tarjeta no existe / ya está resuelta / el gate rechazó el encolado
        (fail-closed — nunca re-intenta con menos autoridad)."""
        row = self._pending.fetch(message_id=message_id)
        if row is None or row.status != "pending":
            logger.warning(
                "hermes.triggers.delegation.approve_no_pending_row",
                extra={"message_id": message_id},
            )
            return None

        try:
            self._verify(message_id)
        except (PermissionError, ValueError, KeyError, AttributeError):
            return None
        conversation_id = uuid4()
        claim_id = self._pending.claim_approval(
            message_id=message_id, approved_by=str(approved_by),
            conversation_id=str(conversation_id),
        )
        if claim_id is None:
            return None  # Another decision won, or earlier admission is uncertain.
        self._touch_conversation(
            conversation_id=conversation_id,
            body=row.body,
            to_agent_id=row.to_agent_id or None,
        )

        trigger_instance_id = await self._mint_one_shot_authorization(
            from_employee_id=row.from_employee_id,
            approved_by=approved_by,
            message_id=message_id,
        )

        # LOW fix (one-shot trigger provenance): the revoke MUST run even if
        # enqueue_from_trigger raises — otherwise a raising call leaks the
        # freshly-minted enabled row forever (a later delegation from the SAME
        # from_employee_id could then be misattributed to it).
        try:
            task_id = await self._gate.enqueue_from_trigger(
                trigger_type=AuthorizedTriggerType.EXTERNAL_DELEGATION,
                scope_value=row.from_employee_id,
                instruction=row.body,
                dedup_key=f"external-delegation-{message_id}",
                derived_from_untrusted_content=True,
                kind=WorkItemKind.CHAT_MESSAGE,
                conversation_id=str(conversation_id),
                target_agent_id=row.to_agent_id or None,
                delegation_correlation_id=row.correlation_id,
                authorization_instance_id=trigger_instance_id,
                admission_guard=lambda: self._authority.guard(message_id),
            )
        except DelegationAuthorityError:
            # The guard fails before the synchronous queue insertion. Unlike a
            # lost enqueue receipt, this is known not to have started work.
            self._pending.release_unenqueued_claim(message_id=message_id, claim_id=claim_id)
            return None
        finally:
            await self._trigger_repo.revoke(
                trigger_instance_id=trigger_instance_id, admin_uuid=approved_by
            )

        if task_id is None:
            self._pending.release_unenqueued_claim(message_id=message_id, claim_id=claim_id)
            # El gate rechazó pese a la autorización recién minteada — no debería
            # ocurrir en el camino feliz, pero fail-closed: no se marca 'approved'
            # sin task_id real (I1-style: nunca alucinar un éxito).
            logger.error(
                "hermes.triggers.delegation.gate_rejected_after_mint",
                extra={"message_id": message_id},
            )
            return None

        resolved = self._pending.resolve(
            message_id=message_id,
            status="approved",
            resolved_by=str(approved_by),
            task_id=str(task_id),
            conversation_id=str(conversation_id),
            claim_id=claim_id,
        )
        if not resolved:
            raise RuntimeError("Delegation admission receipt could not be persisted")
        logger.info(
            "hermes.triggers.delegation.approved",
            extra={
                "message_id": message_id,
                "task_id": str(task_id),
                "approved_by": str(approved_by),
            },
        )
        return task_id

    async def reject(self, *, message_id: str, rejected_by: UUID) -> bool:
        """Rechaza UNA tarjeta pendiente. No encola nada. True si esta llamada
        resolvió la fila (False si no existía o ya estaba resuelta)."""
        row = self._pending.fetch(message_id=message_id)
        if row is None or row.status != "pending":
            return False
        resolved = self._pending.resolve(
            message_id=message_id, status="rejected", resolved_by=str(rejected_by)
        )
        if resolved:
            logger.info(
                "hermes.triggers.delegation.rejected",
                extra={"message_id": message_id, "rejected_by": str(rejected_by)},
            )
        return resolved

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _touch_conversation(
        self, *, conversation_id: UUID, body: str, to_agent_id: str | None
    ) -> None:
        self._conversations.create_or_touch(
            conversation_id=conversation_id,
            first_user_message=body,
            agent_id=to_agent_id,
        )
        self._conversations.append_message(
            conversation_id=conversation_id, role="user", content=body
        )

    async def _mint_one_shot_authorization(
        self, *, from_employee_id: str, approved_by: UUID, message_id: str
    ) -> UUID:
        """Mintea una AuthorizedTrigger FRESCA, firmada por `approved_by` — NUNCA
        reutiliza una fila pre-existente para derivar `enqueued_by` (garantía
        dura: "enqueued_by = el humano que aprobó, jamás de la envelope").
        """
        signature = _sign_delegation_authorization(
            admin_uuid=approved_by,
            from_employee_id=from_employee_id,
            message_id=message_id,
        )
        trigger = await self._trigger_repo.authorize(
            trigger_type=AuthorizedTriggerType.EXTERNAL_DELEGATION,
            scope_value=from_employee_id,
            allowed_capabilities=(),
            risk_ceiling=RiskCeiling.LOW,
            admin_uuid=approved_by,
            approval_signature=signature,
        )
        return trigger.trigger_instance_id


def _sign_delegation_authorization(
    *, admin_uuid: UUID, from_employee_id: str, message_id: str
) -> str:
    """Recibo HMAC-SHA256 del contenido — mismo formato histórico que
    `dbus_runtime_service._sign_scheduled_task_draft`.

    La identidad autorizada procede del canal D-Bus autenticado y la decisión
    queda limitada por la fila local de un solo uso. Como el material HMAC no
    es secreto, este campo no es una firma PKI ni prueba de no repudio; sirve
    únicamente como huella del recibo persistido.
    """
    now = datetime.now(tz=UTC).isoformat()
    payload = f"{admin_uuid}|{from_employee_id}|{message_id}|{now}".encode()
    key_material = b"hermes:external-delegation:v1:" + str(admin_uuid).encode()
    sig = hmac.new(key_material, payload, hashlib.sha256).hexdigest()
    return f"hmac-sha256:{sig}"
