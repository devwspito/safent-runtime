"""AgentLoopOrchestrator — P0 loop autónomo del agente (T024/CTRL-9).

Drena la WorkQueue por iniciativa propia:
  bootstrap() → reconcile_stale + siembra head del firmer
  run_forever() → watchdog → pausa? → claim → idle → _process
  _process(item) → audit TASK_CLAIMED → run_cycle → map outcomes → mark

T051: acepta kind=chat_message; construye DecisionContext con chunk_sink en metadata;
  SIEMPRE emite status/done al socket aunque el engine no streamee.
T052 (🔒 G4): derived_from_untrusted_content=True para chat_message — ConsentContext
  tainted → broker fuerza HITL sobre propuestas derivadas (CTRL-P1-24).

NO modifica litellm_engine (NFR-002). NO toca BrowserPort/SelectorRegistry.
El motor se inyecta como ReasoningEnginePort (inversión de dependencia).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from hermes.capabilities.domain.ports import (
    CapabilityBrokerPort,
    ConsentContext,
    ExecutionStatus,
)
from hermes.tasks.application.decision_context_builder import build_decision_context
from hermes.tasks.application.worker_wake_signal import MonoWorkerWakeSignal
from hermes.tasks.domain.ports import AgentStatePort, WorkItem, WorkItemKind, WorkQueuePort
from hermes.tasks.domain.task_cancel_registry import (
    OperationCancelled,
    get_cancel_registry,
)

logger = logging.getLogger("hermes.tasks.loop")


class AgentLoopOrchestrator:
    """Servicio de orquestación del loop autónomo. Application layer.

    Inyección de dependencias (DIP):
        queue:           WorkQueuePort
        state:           AgentStatePort
        engine:          ReasoningEnginePort (el motor existente, sin tocar)
        broker:          CapabilityBrokerPort (único choke-point al SO)
        consent_context: ConsentContext (operador bajo cuyo consent opera)
        notify_watchdog: callable[[], None] (sd_notify WATCHDOG=1)
        idle_poll_s:     segundos de sleep cuando no hay trabajo
        pause_poll_s:    segundos de sleep cuando el loop está pausado
        firmer:          AuditHashChainSigner | None (si None, no audita)
        audit_repo:      SignedAuditRepositoryPort | None

    El firmer y el audit_repo son opcionales en US1 — cuando se proporcionan,
    se emiten TASK_CLAIMED/COMPLETED/FAILED (T026/FR-019).
    """

    def __init__(
        self,
        *,
        queue: WorkQueuePort,
        state: AgentStatePort,
        engine: Any,  # ReasoningEnginePort — Protocol, Any para evitar import circular
        broker: CapabilityBrokerPort,
        consent_context: ConsentContext,
        notify_watchdog: Callable[[], None],
        idle_poll_s: float = 1.0,
        pause_poll_s: float = 5.0,
        firmer: Any | None = None,        # AuditHashChainSigner | None
        audit_repo: Any | None = None,    # SignedAuditRepositoryPort | None
        approval_gate: Any | None = None, # ApprovalGatePort | None (HITL token lookup)
        intent_log: Any | None = None,    # IntentLog | None (reconciliación I2/RECON-1)
        chunk_sink: Any | None = None,    # ChunkSinkAdapter | None (T050/T051 stream)
        browser_adapter: Any | None = None,  # BrowserSurfaceAdapter | None
        agent_registry: Any | None = None,   # AgentRegistryPort | None (autonomy_level)
        conversation_repo: Any | None = None,  # SQLiteConversationRepository | None (Bug #2)
        notification_store: Any | None = None,  # SqliteNotificationStore | None (bell)
        memory_extraction_enabled: bool = True,  # Feature B: post-chat memory extraction
        usage_repo: Any | None = None,  # SQLiteUsageRepository | None (metering)
    ) -> None:
        self._queue = queue
        self._state = state
        self._engine = engine
        self._broker = broker
        self._consent = consent_context
        self._notify_watchdog = notify_watchdog
        self._idle_poll_s = idle_poll_s
        self._pause_poll_s = pause_poll_s
        self._firmer = firmer
        self._audit_repo = audit_repo
        self._approval_gate = approval_gate
        self._intent_log = intent_log
        self._chunk_sink = chunk_sink  # ChunkSinkAdapter | None
        self._browser_adapter = browser_adapter  # BrowserSurfaceAdapter | None
        self._agent_registry = agent_registry  # AgentRegistryPort | None
        self._conversation_repo = conversation_repo  # SQLiteConversationRepository | None
        self._notification_store = notification_store  # SqliteNotificationStore | None
        self._memory_extraction_enabled = memory_extraction_enabled
        self._usage_repo = usage_repo  # SQLiteUsageRepository | None
        self._shutdown = asyncio.Event()
        self._wake: MonoWorkerWakeSignal = MonoWorkerWakeSignal()
        self._pool: Any | None = None  # set by run_forever(); read by active_worker_count()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def bootstrap(self) -> None:
        """Inicializa antes de run_forever.

        1. reconcile_stale: re-encola huérfanos con lease vencido (SC-003/FR-007).
        2. Siembra _last_hash del firmer desde la DB (continuidad de la cadena).
        3. reconcile_pending_intents: intents sin outcome = crash entre record_intent
           y record_outcome → marcar needs_human_review (I2/RECON-1). NO re-ejecutar.
        """
        n = await self._queue.reconcile_stale()
        if n > 0:
            logger.info(
                "hermes.tasks.loop.reconciled_stale",
                extra={"count": n},
            )

        await self._seed_firmer()
        await self._reconcile_pending_intents()

    async def run_forever(self) -> None:
        """Bucle principal. Termina cuando request_shutdown() es llamado.

        Delega en WorkerPool con size=1 (mono-worker) para mantener el
        mismo comportamiento de P0 mientras se habilita la ruta pool.
        El watchdog SIEMPRE se emite antes de verificar shutdown (NFR-007).
        """
        # Emitir watchdog al menos una vez antes de delegarr (NFR-007 garantía).
        self._notify_watchdog()

        if self._shutdown.is_set():
            return

        # Delegar en WorkerPool(size=1) — ciclo claim->_process->mark intacto.
        from hermes.tasks.application.worker_pool import WorkerPool  # noqa: PLC0415

        self._pool = pool = WorkerPool(
            queue=self._queue,
            state=self._state,
            engine=self._engine,
            broker=self._broker,
            consent_context=self._consent,
            notify_watchdog=self._notify_watchdog,
            idle_poll_s=self._idle_poll_s,
            pause_poll_s=self._pause_poll_s,
            firmer=self._firmer,
            audit_repo=self._audit_repo,
            approval_gate=self._approval_gate,
            intent_log=self._intent_log,
            chunk_sink=self._chunk_sink,
            browser_adapter=self._browser_adapter,
            agent_registry=self._agent_registry,
            conversation_repo=self._conversation_repo,
            notification_store=self._notification_store,
            memory_extraction_enabled=self._memory_extraction_enabled,
            usage_repo=self._usage_repo,
        )
        # Propagar el shutdown event del orchestrator al pool.
        pool._shutdown = self._shutdown  # type: ignore[attr-defined]
        # Propagar la wake signal del orchestrator (misma instancia — CTRL-P1-12).
        self._wake = pool._wake_signal  # type: ignore[attr-defined]

        from hermes.tasks.application.worker_pool import _resolve_worker_pool_size  # noqa: PLC0415
        await pool.run_forever(size=_resolve_worker_pool_size())

    def request_shutdown(self) -> None:
        """Señaliza parada limpia (SIGTERM del daemon)."""
        self._shutdown.set()

    def active_worker_count(self) -> int:
        """Return the number of workers currently processing a task.

        Returns 0 before run_forever() is called (pool not yet created).
        Safe to call from any context — reads a plain int attribute.
        """
        if self._pool is None:
            return 0
        return self._pool.active_worker_count()

    @property
    def wake_signal(self) -> MonoWorkerWakeSignal:
        """Señal de wake-on-enqueue. El ControlPlanePort.enqueue llama
        wake_signal.wake_one() TRAS el commit del item en la cola (T040/T048).

        Orden estricto: commit ANTES de wake (CTRL-P1-12).
        """
        return self._wake

    # ------------------------------------------------------------------
    # Private: item processing
    # ------------------------------------------------------------------

    async def _process(self, item: WorkItem) -> None:
        """Procesa un item reclamado.

        1. Audit TASK_CLAIMED.
        2. build_decision_context → trigger=queue_drain:<kind>, cycle_id=item.id.
           Para chat_message: inyecta chunk_sink en metadata (T050/T051).
        3. engine.run_cycle.
        4. Sin proposals → mark_failed. Para chat_message emite done(error) (T051).
        5. Por cada proposal → broker.dispatch con consent tainted si chat (T052).
        6. mark_completed SOLO con evidencia real (SC-001).
        """
        assert item.claim_token is not None, "claim_token debe existir en IN_PROGRESS"

        await self._emit_claimed(item)

        is_chat = item.kind is WorkItemKind.CHAT_MESSAGE
        chunk_sink = self._chunk_sink

        # conversation_id (server-side, CWE-862 safe) — needed both for the stream
        # injection AND for the counting sink's incremental persistence.
        _conv_id_for_inject = (item.payload.get("conversation_id") or "").strip()
        _conv_uuid_for_persist = None
        if is_chat and _conv_id_for_inject and self._conversation_repo is not None:
            try:
                from uuid import UUID as _UUID  # noqa: PLC0415
                _conv_uuid_for_persist = _UUID(_conv_id_for_inject)
            except (ValueError, AttributeError):
                _conv_uuid_for_persist = None

        # FIX B.2 — wrap the real sink in a counting adapter so we know post-cycle
        # whether the engine emitted incremental deltas (streaming) or nothing.
        # It ALSO persists the answer incrementally to the mirror (resume mirror-first
        # on refresh). The original sink is used for emit_status + the fallback close().
        counting_sink: _CountingChunkSink | None = (
            _CountingChunkSink(
                chunk_sink,
                repo=self._conversation_repo,
                conversation_id=_conv_uuid_for_persist,
                task_id=item.id,
            )
            if (is_chat and chunk_sink is not None) else None
        )
        effective_sink = counting_sink if counting_sink is not None else chunk_sink

        ctx = build_decision_context(item)
        # BUG FIX (P0 — HITL approval "caducó antes de aprobarla" loop): the
        # engine's in-cycle WRITE dispatch previously called broker.dispatch()
        # WITHOUT a work_item_id (nous_engine only carried conversation_id, which
        # is injected below ONLY for chat). Delegated/autonomous/scheduled items
        # (is_chat=False) have no conversation_id, so register_pending always
        # persisted UUID(int=0) and approve_action could never find a real queue
        # task to re-enqueue after the owner approved. Inject the REAL work_item_id
        # (server-side, item.id) into metadata for EVERY cycle — chat or not — so
        # the engine can thread it through to the broker (mirrors conversation_id).
        ctx = _inject_work_item_id(ctx, item.id)
        if is_chat and chunk_sink is not None:
            # Inyecta el sink + task_id + conversation_id en metadata
            # (Constitución I: NO toca run_cycle). task_id_for_stream permite
            # al engine emitir deltas al socket correcto. conversation_id permite
            # al engine emitir ChatDelta/ChatStreamEnd D-Bus signals
            # (spec streaming-dbus). Ambos vienen del WorkItem server-side
            # (CWE-862 safe — nunca del payload directo del cliente).
            ctx = _inject_chunk_sink(
                ctx, counting_sink, task_id=item.id,
                conversation_id=_conv_id_for_inject,
            )
            await chunk_sink.emit_status(task_id=item.id, status="in_progress")
            # Wire the chat task into live_activity so the Office floor animates
            # the agent character while the response is in-flight — even during
            # pure narrative replies that invoke no tool calls. The entry is
            # cleared by worker_pool._worker_loop's finally block
            # (live_activity.clear(item.id)) on task completion or failure.
            _chat_agent_id = _resolve_chat_agent_id(item.payload)
            _record_chat_activity(str(item.id), _chat_agent_id)

        # spec 014 inc. 3 (CTRL-13 fix): propaga el operator_id verificado del
        # operador que encoló la tarea (enqueued_by, resuelto server-side desde
        # channel.sender_uid en ControlPlaneService — CTRL-P1-3, CWE-862).
        # Se inyecta en metadata para que el engine lo use como consent_context
        # per-ciclo sin cambiar la firma de run_cycle (Constitución I).
        # Si no hay enqueued_by (tarea autónoma sin operador) queda None → broker
        # sigue fail-closed (CTRL-13). NUNCA se acepta del payload de la herramienta.
        task_operator_id = _extract_enqueued_by_uuid(item)
        ctx = _inject_task_operator_id(ctx, task_operator_id)

        # Historial de la conversación (FIX "Hermes se presenta cada mensaje"):
        # el daemon ya persistió el mensaje del usuario en el enqueue, así que
        # get_detail().messages = [..., user:actual]; el historial es todo menos
        # el último. Sin esto el LLM recibe history=0 y trata cada turno como
        # nuevo. Best-effort: un fallo aquí NO rompe el chat (sigue sin historial).
        if is_chat and self._conversation_repo is not None:
            conv_id = (item.payload.get("conversation_id") or "").strip()
            if conv_id:
                try:
                    from uuid import UUID as _UUID  # noqa: PLC0415
                    _detail = self._conversation_repo.get_detail(
                        conversation_id=_UUID(conv_id)
                    )
                    _msgs = list(getattr(_detail, "messages", None) or [])
                    _hist = [
                        {"role": m.role, "content": m.content} for m in _msgs[:-1]
                    ]
                    if _hist:
                        ctx = _inject_conversation_history(ctx, _hist)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "hermes.tasks.loop.history_load_failed: %s", exc
                    )

        # Consent base con el operator_id del task (sobrescribe el daemon-level si
        # el daemon arrancó sin HERMES_OPERATOR_ID). Si task_operator_id es None,
        # se mantiene el consent daemon-level (que también puede ser None → fail-closed).
        base_consent = _override_operator_id(self._consent, task_operator_id)

        # T052 (🔒 G4): consent pre-ciclo solo taintado si es chat.
        # El taint por lectura externa se aplica POST-ciclo (ver abajo).
        pre_cycle_consent = _taint_consent_if_chat(base_consent, is_chat)

        # Cancel requested while queued / just before dispatch: don't start the
        # cycle at all — mark terminal and close the stream cleanly.
        if get_cancel_registry().is_cancelled(item.id):
            await self._handle_cancelled(
                item, get_cancel_registry().reason(item.id), effective_sink, is_chat
            )
            return

        _cycle_start = time.monotonic()
        try:
            output = await self._engine.run_cycle(ctx)
        except OperationCancelled as exc:
            # Operator stopped the task mid-cycle (stream callback raised). Terminal,
            # NO retry (unlike a normal failure).
            reason = str(exc).strip() or "Detenida por el operador"
            logger.info(
                "hermes.tasks.loop.cancelled task=%s reason=%s", str(item.id), reason
            )
            await self._handle_cancelled(item, reason, effective_sink, is_chat)
            return
        except Exception as exc:
            _latency_ms = int((time.monotonic() - _cycle_start) * 1000)
            # exc_info + traceback explícito en el MENSAJE: el handler stderr→journald
            # NO serializa `extra=` (se perdía el detalle: "engine_error" a secas, sin
            # causa). El chat fallaba en silencio y era indebugable. Metemos la traza
            # completa en el texto para que journalctl la muestre siempre.
            import traceback as _tb  # noqa: PLC0415
            logger.error(
                "hermes.tasks.loop.engine_error task=%s error=%s\n%s",
                str(item.id), str(exc), _tb.format_exc(),
                extra={"task_id": str(item.id), "error": str(exc)},
            )
            # Surface the real cause to the operator (chat UI shows this). A bare
            # exception class name is undebuggable; include the message so a
            # provider error (model/param/quota) is actionable, not opaque.
            _detail = str(exc).strip().replace("\n", " ")
            error_reason = f"{type(exc).__name__}: {_detail}" if _detail else type(exc).__name__
            error_reason = error_reason[:400]
            if is_chat:
                await self._safe_close_stream(
                    effective_sink, item, "failed", error=error_reason
                )
            # El fallo del motor debe SER VISIBLE en la UI: ChatBar sondea
            # get_conversation, así que sin un mensaje persistido el usuario ve
            # "Thinking…" eterno y luego nada (fallo silencioso, indebugable
            # desde el escritorio). Persistimos el error como turno del
            # asistente — mismo canal que una respuesta normal. Best-effort.
            if is_chat and self._conversation_repo is not None:
                conv_id_str = item.payload.get("conversation_id") or ""
                if conv_id_str:
                    try:
                        from uuid import UUID as _UUID  # noqa: PLC0415
                        self._conversation_repo.append_message(
                            conversation_id=_UUID(conv_id_str),
                            role="assistant",
                            content=(
                                "⚠ No he podido completar la respuesta: "
                                f"{error_reason}"
                            ),
                            task_id=item.id,
                        )
                    except Exception as _pexc:  # noqa: BLE001
                        logger.warning(
                            "hermes.tasks.loop.chat.persist_error_failed: %s", _pexc
                        )
            await self._do_mark_failed(item, error_reason)
            return

        _latency_ms = int((time.monotonic() - _cycle_start) * 1000)
        if self._usage_repo is not None:
            try:
                _usage_agent_id = _resolve_chat_agent_id(item.payload) or None
                self._usage_repo.record_cycle(
                    agent_id=(_usage_agent_id if _usage_agent_id else None),
                    conversation_id=(item.payload.get("conversation_id") or None),
                    task_id=str(item.id),
                    usage=output.usage,
                    tool_calls=len(output.tool_steps),
                    latency_ms=_latency_ms,
                    outcome=("pending_approval" if output.tool_call_proposals else "completed"),
                )
            except Exception as _uexc:  # noqa: BLE001
                logger.warning("hermes.tasks.loop.usage_record_failed: %s", _uexc)

        if not output.tool_call_proposals:
            if is_chat and output.narrative.strip():
                # Persiste los pasos de tool (auto-EXECUTED in-loop) ANTES de la
                # fila del asistente, para reconstruir las tarjetas al recargar.
                if is_chat:
                    self._persist_tool_steps(item, output.tool_steps)
                # FIX B.2 — use counting_sink.delta_count (set by the engine's
                # stream_callback via the counting wrapper) to detect streaming.
                _prior_emit_count = counting_sink.delta_count if counting_sink is not None else 0
                await self._handle_chat_narrative_reply(
                    item, output.narrative, effective_sink,
                    prior_emit_count=_prior_emit_count,
                )
                return
            # A tool was EXECUTED inline this cycle (e.g. a block-and-resume HITL
            # approval ran the exact call) but the model emitted no closing narrative.
            # The turn DID act — persist the steps and close cleanly instead of the
            # spurious no_actions FAILURE ("La respuesta falló: no_actions" — the red
            # chat-error notification the owner saw after approving in 2s). tool_steps
            # are call descriptors {tool,label,target} with no per-step outcome; a
            # denied tool almost always yields an explanatory narrative and so never
            # reaches this no-narrative branch, making presence the right signal here.
            if is_chat and output.tool_steps:
                self._persist_tool_steps(item, output.tool_steps)
                await self._safe_close_stream(effective_sink, item, "completed")
                await self._do_mark_completed(item, None)
                return
            logger.info(
                "hermes.tasks.loop.no_actions",
                extra={"task_id": str(item.id)},
            )
            # T051: chat sin modelo/narrativa → done legible; nunca silencioso.
            no_actions_error = (
                "inference_not_configured"
                if is_chat
                else "no_actions"
            )
            if is_chat:
                await self._safe_close_stream(
                    effective_sink, item, "failed", error=no_actions_error
                )
            await self._do_mark_failed(item, "no_actions")
            return

        # CTRL-5 / TOP-1: consent post-ciclo — taintado si:
        #   (a) el item es chat_message (contenido del usuario = untrusted), O
        #   (b) el motor leyó contenido externo no confiable (web/Composio/fichero).
        # Esto cierra el vector del loop AUTÓNOMO: scheduler+Composio READ → taint.
        consent = _taint_consent_if_external(pre_cycle_consent, output.read_external_content)

        # Persiste, ANTES del dispatch, los pasos de tool y la respuesta del
        # asistente en la ruta de tools/HITL. Sin esto, un reload de una
        # conversación que propuso/usó tools muestra solo el mensaje del usuario
        # (la rama narrativa de arriba es la única que persistía). Persistir antes
        # del dispatch garantiza la fila pase lo que pase con la propuesta
        # (PENDING_APPROVAL / REJECTED / FAILED / success). Mutuamente excluyente
        # con la rama narrativa (branch on `if not tool_call_proposals`) → sin
        # doble escritura.
        if is_chat:
            self._persist_tool_steps(item, output.tool_steps)
            if output.narrative.strip():
                self._persist_assistant_turn(item, output.narrative)

        dispatch_result = await self._dispatch_proposals(
            item, output.tool_call_proposals, consent=consent
        )
        if dispatch_result is None:
            # Dispatch ya llamó mark_failed/mark_pending_approval/mark_rejected.
            if is_chat:
                await self._safe_close_stream(effective_sink, item, "failed")
            return

        audit_entry_id, head_hash = dispatch_result
        if is_chat:
            await self._safe_close_stream(effective_sink, item, "completed")
        await self._do_mark_completed(item, audit_entry_id, head_hash)

    async def _dispatch_proposals(
        self,
        item: WorkItem,
        proposals: tuple,
        *,
        consent: ConsentContext | None = None,
    ) -> tuple[Any, str | None] | None:
        """Despacha proposals en orden. Returns (audit_entry_id, head_hash) o None.

        None significa que la tarea ya fue resuelta (pending_approval/rejected/failed).
        `consent` permite propagar taint por chat_message (T052/CTRL-P1-24).
        El autonomy_level del agente activo se resuelve aquí y se pasa al broker
        como parámetro explícito (sin estado global).

        FR-015 — Re-dispatch tras aprobación HITL:
        Si el work item tiene `_pending_proposal_id` en su payload (fijado por
        mark_pending_approval), se intenta recuperar el token de aprobación para
        ese proposal_id original. Si existe, la propuesta nueva del motor se
        re-despacha con el proposal_id original + el token aprobado, de modo que
        `verify_token` pase (single-use) y el broker ejecute. El token se consume
        en ese único despacho; un segundo re-dispatch sin aprobación vuelve a
        PENDING_APPROVAL (fail-closed intacto).
        """
        real_evidence_id = None
        real_evidence_hash: str | None = None
        effective_consent = consent if consent is not None else self._consent
        # Prioritize the target agent's autonomy (the one the task was dispatched to),
        # not the UI-active agent. Falls back to active_agent_id() when item has none.
        _task_agent_id: str | None = None
        try:
            _task_agent_id = item.payload.get("agent_id") or None  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
        autonomy_level = _resolve_active_autonomy_level(
            self._agent_registry, agent_id=_task_agent_id
        )

        # FR-015: recuperar proposal_id + token pre-aprobados para este work_item.
        # Presente solo cuando el item viene de re-enqueue_after_approval.
        pre_approved_proposal_id, pre_approved_token = (
            await self._fetch_pre_approved_token(item)
        )

        for proposal in proposals:
            hitl_token = await self._fetch_hitl_token(proposal.proposal_id)

            # FR-015: si no hay token para el nuevo proposal_id pero existe uno
            # pre-aprobado para este work_item, usarlo sustituyendo el proposal_id
            # para que verify_token (ligado al id original) pase correctamente.
            # Esto no debilita el gate: el token sigue siendo HMAC criptográfico
            # single-use y la autorización fue emitida por un operador autenticado.
            dispatch_proposal = proposal
            if hitl_token is None and pre_approved_token is not None:
                import dataclasses as _dc  # noqa: PLC0415
                dispatch_proposal = _dc.replace(
                    proposal, proposal_id=pre_approved_proposal_id
                )
                hitl_token = pre_approved_token

            outcome = await self._broker.dispatch(
                dispatch_proposal,
                effective_consent,
                hitl_approval_token=hitl_token,
                work_item_id=item.id,
                autonomy_level=autonomy_level,
                conversation_id=(item.payload.get("conversation_id") or ""),
            )

            if outcome.status is ExecutionStatus.PENDING_APPROVAL:
                await self._queue.mark_pending_approval(
                    item.id,
                    claim_token=item.claim_token,
                    proposal_id=proposal.proposal_id,
                )
                return None

            if outcome.status in (
                ExecutionStatus.REJECTED_BY_CONSENT,
                ExecutionStatus.REJECTED_BY_POLICY,
            ):
                await self._queue.mark_rejected(
                    item.id,
                    claim_token=item.claim_token,
                    reason=outcome.error or str(outcome.status),
                )
                return None

            if outcome.status is ExecutionStatus.FAILED:
                await self._do_mark_failed(
                    item, outcome.error or "dispatch_failed"
                )
                return None

            if outcome.is_real_execution:
                real_evidence_id = outcome.audit_entry_id
                real_evidence_hash = outcome.execution_head_hash

        if real_evidence_id is None:
            return None
        return real_evidence_id, real_evidence_hash

    async def _handle_chat_narrative_reply(
        self,
        item: WorkItem,
        narrative: str,
        chunk_sink: Any | None,
        *,
        prior_emit_count: int = 0,
    ) -> None:
        """Emite la narrativa del agente al stream y completa la tarea.

        Ruta: chat_message + narrative non-empty + zero tool_call_proposals.
        Evidencia de ejecución: audit entry CHAT_REPLIED firmado en la cadena.
        El texto NO se loguea (CTRL-P1-9 / PII). Solo longitud.

        Bug #2 fix: persiste la respuesta del asistente en conversation_repo para
        que GetConversation la devuelva y la UI la muestre. Best-effort: un fallo
        de persistencia NO rompe el chat (el stream ya fue emitido).

        FIX B.2: si prior_emit_count > 0, el engine ya emitió deltas incrementales
        durante run_conversation. No se re-emite la narrativa completa (evita
        duplicar el texto en el cliente). Solo se cierra el stream con done().
        La persistencia en conversation_repo se mantiene SIEMPRE (fallback QML).
        """
        from hermes.tasks.control_plane.domain.ports import (  # noqa: PLC0415
            StreamChunkKind,
            TaskStreamChunk,
        )

        # GATE 0 / M2 — persiste la respuesta del asistente ANTES de cerrar el stream.
        # Si cerramos primero, el cliente recibe DONE y llama a GetConversation antes de
        # que la BD tenga la fila → la UI no muestra la respuesta hasta refrescar varias
        # veces (write-before-notify). Best-effort: fallo de escritura no rompe el ciclo.
        self._persist_assistant_turn(item, narrative)

        if chunk_sink is not None:
            already_streamed = prior_emit_count > 0
            if not already_streamed:
                # Fallback monolítico: el engine no hizo streaming token-a-token,
                # emitimos la narrativa completa de una vez (comportamiento previo).
                delta_chunk = TaskStreamChunk(kind=StreamChunkKind.DELTA, delta=narrative)
                await chunk_sink.emit(task_id=item.id, chunk=delta_chunk)
            # Siempre cerramos el stream: el cliente espera el frame DONE. Wrapped:
            # a broker close() failure must not skip _emit_chat_replied/mark_completed
            # (the narrative is already persisted above → the mirror poll adopts it).
            await self._safe_close_stream(chunk_sink, item, "completed")

        audit_entry_id, head_hash = await self._emit_chat_replied(item, narrative)
        await self._do_mark_completed(item, audit_entry_id, head_hash)
        # Feature B: post-cycle memory extraction (best-effort, fail-soft).
        await self._maybe_extract_memory(item, narrative)

    def _persist_assistant_turn(self, item: WorkItem, narrative: str) -> None:
        """Persiste la respuesta del asistente en el store de conversaciones para
        que GetConversation la devuelva y la UI la muestre tras un reload.

        conversation_id viene del payload del item (mismo valor que el cliente
        pasó al Enqueue). Best-effort: un fallo de persistencia NO rompe el ciclo.
        Se llama EXACTAMENTE una vez por outcome (la rama narrativa y la rama de
        tools son mutuamente excluyentes — branch on `if not tool_call_proposals`).
        """
        if self._conversation_repo is None:
            return
        conv_id_str = item.payload.get("conversation_id") or ""
        if not conv_id_str:
            return
        try:
            from uuid import UUID as _UUID  # noqa: PLC0415
            # UPSERT (not append): if the counting sink already wrote a 'streaming'
            # partial row for this task, this UPDATES it to the final answer +
            # status='complete' (no duplicate). If streaming was skipped, it inserts.
            self._conversation_repo.upsert_assistant_message(
                conversation_id=_UUID(conv_id_str),
                task_id=item.id,
                content=narrative,
                status="complete",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "hermes.tasks.loop.chat.persist_assistant_failed: %s", exc
            )

    def _persist_tool_steps(
        self, item: WorkItem, tool_steps: "tuple[dict, ...]"
    ) -> None:
        """Persiste cada paso de tool (role='tool', content = JSON del descriptor
        {tool,label,target}) en orden de ejecución, ANTES de la fila del asistente,
        para que un reload reconstruya las tarjetas de pasos. Best-effort.

        SECURITY: el descriptor viene de nous_engine._describe_tool_call, que YA
        trunca a 200 chars y NO almacena credenciales/PII — esta es la única fuente.
        """
        if self._conversation_repo is None or not tool_steps:
            return
        conv_id_str = item.payload.get("conversation_id") or ""
        if not conv_id_str:
            return
        try:
            import json as _json  # noqa: PLC0415
            from uuid import UUID as _UUID  # noqa: PLC0415
            conv_id = _UUID(conv_id_str)
            for step in tool_steps:
                self._conversation_repo.append_message(
                    conversation_id=conv_id,
                    role="tool",
                    content=_json.dumps(step, ensure_ascii=False),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "hermes.tasks.loop.chat.persist_tool_steps_failed: %s", exc
            )

    # ------------------------------------------------------------------
    # Private: state transitions with observability
    # ------------------------------------------------------------------

    async def _safe_close_stream(
        self, sink: Any, item: WorkItem, outcome: str, error: str | None = None
    ) -> None:
        """Emit the terminal stream frame, never letting a broker/close() error
        propagate. A close() failure must NOT skip mark_* nor crash the worker; the
        client's 2s mirror poll adopts the persisted answer regardless."""
        if sink is None:
            return
        try:
            await sink.close(task_id=item.id, outcome=outcome, error=error)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "hermes.tasks.loop.stream.close_failed task=%s outcome=%s: %s",
                str(item.id), outcome, exc,
            )

    async def _do_mark_failed(self, item: WorkItem, reason: str) -> None:
        await self._queue.mark_failed(
            item.id,
            claim_token=item.claim_token,  # type: ignore[arg-type]
            reason=reason,
        )
        await self._emit_failed(item, reason)
        self._emit_notification_failed(item, reason)

    async def _handle_cancelled(
        self, item: WorkItem, reason: str, effective_sink: Any, is_chat: bool
    ) -> None:
        """Operator stopped this task: close the stream, persist a note, mark the
        task CANCELLED (terminal, no retry), and clear the cancel flag."""
        if is_chat and effective_sink is not None:
            try:
                await effective_sink.close(
                    task_id=item.id, outcome="cancelled", error=reason
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("hermes.tasks.loop.cancel.stream_close_failed: %s", exc)
        if is_chat and self._conversation_repo is not None:
            conv_id_str = item.payload.get("conversation_id") or ""
            if conv_id_str:
                try:
                    from uuid import UUID as _UUID  # noqa: PLC0415
                    self._conversation_repo.append_message(
                        conversation_id=_UUID(conv_id_str),
                        role="assistant",
                        content=f"⏹ Tarea detenida por el operador. {reason}".strip(),
                        task_id=item.id,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("hermes.tasks.loop.cancel.persist_failed: %s", exc)
        try:
            await self._queue.mark_cancelled(
                item.id,
                claim_token=item.claim_token,  # type: ignore[arg-type]
                reason=reason,
            )
        except Exception as exc:  # noqa: BLE001 — task may have already terminated
            logger.warning("hermes.tasks.loop.cancel.mark_failed: %s", exc)
        finally:
            get_cancel_registry().clear(item.id)

    async def _do_mark_completed(
        self, item: WorkItem, audit_entry_id: Any, head_hash: str | None = None
    ) -> None:
        await self._queue.mark_completed(
            item.id,
            claim_token=item.claim_token,  # type: ignore[arg-type]
            audit_entry_id=audit_entry_id,
            execution_head_hash=head_hash,
        )
        await self._emit_completed(item, audit_entry_id)
        self._emit_notification_completed(item)

    # ------------------------------------------------------------------
    # Private: audit emission (T026)
    # ------------------------------------------------------------------

    async def _reconcile_pending_intents(self) -> None:
        """Marca tareas con intents pendientes sin outcome como FAILED (RECON-1/I2).

        Un intent pendiente indica que el proceso crasheó entre record_intent y
        record_outcome. El efecto puede o no haberse aplicado en el SO — NO
        re-despachar. La tarea queda FAILED para que el operador la revise.
        """
        if self._intent_log is None:
            return
        task_ids = self._intent_log.pending_task_ids()
        if not task_ids:
            return
        logger.warning(
            "hermes.tasks.loop.pending_intents_detected",
            extra={"count": len(task_ids)},
        )
        for task_id_str in task_ids:
            try:
                from uuid import UUID  # noqa: PLC0415
                task_id = UUID(task_id_str)
                # Intentar marcar como failed si está en un estado reclaimable.
                # Si ya está en PENDING (reconcile_stale la relanzó), marcarla rejected
                # requiere un claim_token. El enfoque más seguro: encolar una tarea
                # de revisión humana en su lugar, o simplemente loguear y marcar manualmente.
                # En P0, logueamos con nivel ERROR para que el operador actúe.
                logger.error(
                    "hermes.tasks.loop.needs_human_review: "
                    "task_id=%s tiene intent sin outcome — posible efecto parcial. "
                    "Requiere revisión humana antes de reintentar.",
                    task_id,
                )
            except (ValueError, AttributeError):
                logger.error(
                    "hermes.tasks.loop.invalid_task_id_in_intent_log: %s",
                    task_id_str,
                )

    async def _seed_firmer(self) -> None:
        """Siembra _last_hash del firmer desde la DB al arrancar (AUD-1)."""
        if self._firmer is None or self._audit_repo is None:
            return
        head = await self._audit_repo.head_hash_hex()
        if head is not None:
            object.__setattr__(self._firmer, "_last_hash", bytes.fromhex(head))

    async def _emit_claimed(self, item: WorkItem) -> None:
        if self._firmer is None or self._audit_repo is None:
            return
        from hermes.tasks.application.loop_observability import emit_task_claimed  # noqa: PLC0415
        await emit_task_claimed(
            firmer=self._firmer,
            audit_repo=self._audit_repo,
            task_id=item.id,
            tenant_id=item.tenant_id,
            trigger_kind=item.trigger_kind,
        )

    async def _emit_completed(self, item: WorkItem, execution_audit_entry_id: Any) -> None:
        if self._firmer is None or self._audit_repo is None:
            return
        from hermes.tasks.application.loop_observability import emit_task_completed  # noqa: PLC0415
        await emit_task_completed(
            firmer=self._firmer,
            audit_repo=self._audit_repo,
            task_id=item.id,
            tenant_id=item.tenant_id,
            execution_audit_entry_id=execution_audit_entry_id,
        )

    async def _emit_chat_replied(
        self, item: WorkItem, narrative: str
    ) -> tuple[Any, str | None]:
        """Firma y persiste CHAT_REPLIED. Returns (audit_entry_id, head_hash_hex).

        Si el firmer no está configurado devuelve un UUID sintético y None para
        que _do_mark_completed siga funcionando (los tests sin firmer usan este path).
        """
        if self._firmer is None or self._audit_repo is None:
            from uuid import uuid4  # noqa: PLC0415
            return uuid4(), None
        from hermes.tasks.application.loop_observability import emit_chat_replied  # noqa: PLC0415
        return await emit_chat_replied(
            firmer=self._firmer,
            audit_repo=self._audit_repo,
            task_id=item.id,
            tenant_id=item.tenant_id,
            narrative_len=len(narrative),
        )

    async def _emit_failed(self, item: WorkItem, reason: str) -> None:
        if self._firmer is None or self._audit_repo is None:
            return
        from hermes.tasks.application.loop_observability import emit_task_failed  # noqa: PLC0415
        await emit_task_failed(
            firmer=self._firmer,
            audit_repo=self._audit_repo,
            task_id=item.id,
            tenant_id=item.tenant_id,
            reason=reason,
        )

    # ------------------------------------------------------------------
    # Private: notification emission (bell feature)
    # Fail-soft: a notification failure NEVER breaks the task/chat path.
    # ------------------------------------------------------------------

    def _emit_notification_completed(self, item: WorkItem) -> None:
        """Emit a task-completed notification. Fail-soft."""
        if self._notification_store is None:
            return
        is_chat = item.kind is WorkItemKind.CHAT_MESSAGE
        kind = "chat" if is_chat else "task"
        if is_chat:
            title = "Chat respondido"
            body = "El asistente ha completado la respuesta."
        else:
            label = _item_label(item)
            title = f"Tarea '{label}' completada"
            body = "La tarea ha terminado con éxito."
        try:
            self._notification_store.add(
                kind=kind,
                title=title,
                body=body,
                status="ok",
                conversation_id=item.payload.get("conversation_id") or None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("hermes.notifications.emit_completed_failed: %s", exc)

    def _emit_notification_failed(self, item: WorkItem, reason: str) -> None:
        """Emit a task-failed notification. Fail-soft."""
        if self._notification_store is None:
            return
        is_chat = item.kind is WorkItemKind.CHAT_MESSAGE
        kind = "chat" if is_chat else "task"
        label = _item_label(item)
        if is_chat:
            title = "Error en el chat"
            body = f"La respuesta falló: {reason[:120]}"
        else:
            title = f"Tarea '{label}' falló"
            body = f"Error: {reason[:120]}"
        try:
            self._notification_store.add(
                kind=kind,
                title=title,
                body=body,
                status="error",
                conversation_id=item.payload.get("conversation_id") or None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("hermes.notifications.emit_failed_failed: %s", exc)

    async def _maybe_extract_memory(
        self, item: WorkItem, narrative: str
    ) -> None:
        """Post-cycle memory extraction hook (Feature B). Fail-soft."""
        if not self._memory_extraction_enabled:
            return
        user_message = (item.payload.get("text") or "").strip()
        if not user_message and not narrative.strip():
            return
        try:
            from hermes.memory.application.post_cycle_extractor import (  # noqa: PLC0415
                maybe_extract_and_store,
            )
            await maybe_extract_and_store(
                user_message=user_message,
                assistant_reply=narrative,
                tenant_id=item.tenant_id,
                engine=self._engine,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("hermes.memory.post_cycle.failed: %s", exc)

    async def _fetch_hitl_token(self, proposal_id: Any) -> str | None:
        """Busca el token HITL aprobado para este proposal, si el gate está inyectado.

        Permite que el loop re-dispatche automáticamente proposals que el operador
        ya aprobó en el buzón de aprobaciones (approved_token_for). Fail-closed:
        None si no hay gate o si la propuesta aún no fue aprobada.
        """
        if self._approval_gate is None:
            return None
        try:
            return await self._approval_gate.approved_token_for(proposal_id)
        except Exception:  # noqa: BLE001
            return None

    async def _fetch_pre_approved_token(
        self, item: WorkItem
    ) -> tuple[Any, str | None]:
        """FR-015: recupera (original_proposal_id, token) pre-aprobado para el work_item.

        Solo aplica si el item fue previamente bloqueado en PENDING_APPROVAL y
        re-encolado tras aprobación humana. En ese caso, mark_pending_approval
        almacena `_pending_proposal_id` en el payload.

        Returns:
            (original_proposal_id, token) si existe aprobación válida.
            (None, None) en cualquier otro caso (fail-closed).
        """
        if self._approval_gate is None:
            return None, None
        raw_pending_id = item.payload.get("_pending_proposal_id")
        if not raw_pending_id:
            return None, None
        try:
            from uuid import UUID as _UUID  # noqa: PLC0415
            original_id = _UUID(str(raw_pending_id))
            token = await self._approval_gate.approved_token_for(original_id)
            if token is None:
                return None, None
            return original_id, token
        except Exception:  # noqa: BLE001
            return None, None

    async def _idle(self, seconds: float) -> None:
        """Espera interruptible: wake_one() sale antes del timeout (SC-006).

        Sustituye asyncio.sleep ciego por wait_for_work(timeout) del
        MonoWorkerWakeSignal. Si seconds == 0 no bloquea (tests unitarios
        con idle_poll_s=0.0 mantienen el comportamiento original).
        """
        if seconds <= 0:
            return
        await self._wake.wait_for_work(timeout=seconds)


# ---------------------------------------------------------------------------
# Module-level pure helpers (T051/T052 — domain logic sin I/O)
# ---------------------------------------------------------------------------


def _resolve_active_autonomy_level(
    agent_registry: "Any", *, agent_id: "str | None" = None
) -> "Any":
    """Lee el AutonomyLevel del agente DESTINO de la tarea (fail-safe).

    Prioridad: agent_id explícito (el agente al que va la tarea)
    → DEFAULT_AGENT_ID (CEO) → AutonomyLevel.BALANCED.

    El broker interpreta None como BALANCED (invariante).
    No lanza — la resolución del nivel de autonomía nunca debe tumbar el loop.
    """
    from hermes.agents.domain.agent import DEFAULT_AGENT_ID, AutonomyLevel  # noqa: PLC0415

    if agent_registry is None:
        return AutonomyLevel.BALANCED
    try:
        resolved_id = agent_id or DEFAULT_AGENT_ID
        agent = agent_registry.get_agent(resolved_id)
        return agent.autonomy_level
    except Exception:  # noqa: BLE001 — fail-safe: el loop no debe caerse
        return AutonomyLevel.BALANCED


def _inject_chunk_sink(
    ctx: "Any",  # DecisionContext
    chunk_sink: Any,
    task_id: "Any | None" = None,
    conversation_id: str = "",
) -> "Any":
    """Devuelve un nuevo DecisionContext con chunk_sink (y task_id_for_stream) en metadata.

    NO modifica la firma de run_cycle (Constitución I). El campo `metadata` es
    opaco; el engine lo lee si implementa streaming, si no, lo ignora.

    task_id_for_stream: UUID del WorkItem — el engine lo usa para emitir deltas
    al socket correcto. Inyectado aquí (server-side desde item.id) y nunca del
    payload del cliente (CWE-862 safe).

    conversation_id: UUID string del WorkItem.payload["conversation_id"] —
    inyectado aquí para que el engine lo use en las señales ChatDelta/ChatStreamEnd
    (spec streaming-dbus). Nunca del payload directo del cliente (CWE-862 safe).

    IMPORTANTE: preserva operator_instruction y agent_id para que el texto del
    usuario llegue al engine. Omitirlos los resetea a "" / None (Bug #1).
    """
    from hermes.domain.decision_context import DecisionContext  # noqa: PLC0415

    new_meta = {**ctx.metadata, "chunk_sink": chunk_sink}
    if task_id is not None:
        new_meta["task_id_for_stream"] = task_id
    if conversation_id:
        new_meta["conversation_id"] = conversation_id
    return DecisionContext(
        tenant_id=ctx.tenant_id,
        cycle_id=ctx.cycle_id,
        trigger=ctx.trigger,
        subjects=ctx.subjects,
        constraints=ctx.constraints,
        operator_instruction=ctx.operator_instruction,
        agent_id=ctx.agent_id,
        domain_payload=ctx.domain_payload,
        metadata=new_meta,
    )


def _inject_work_item_id(
    ctx: "Any",  # DecisionContext
    work_item_id: "Any",  # UUID
) -> "Any":
    """Devuelve un DecisionContext con el work_item_id REAL en metadata.

    Bug fix (2026-07 / HITL approval loop): sin esto, el engine no tiene forma
    de saber qué WorkItem originó el ciclo salvo cuando hay conversation_id
    (solo chat) — un WRITE propuesto in-cycle en una tarea delegada/autónoma
    pasaba work_item_id=None al broker, que persistía UUID(int=0) en
    pending_approvals. approve_action leía ese cero, lo trataba como
    "no es una tarea de cola" y NUNCA re-encolaba tras la aprobación del dueño:
    la tarjeta expiraba silenciosamente y la tarea quedaba atascada en
    pending_approval para siempre. Se inyecta SIEMPRE (chat y no-chat) — campo
    opaco en metadata, NO toca la firma de run_cycle (Constitución I).
    """
    from hermes.domain.decision_context import DecisionContext  # noqa: PLC0415

    new_meta = {**ctx.metadata, "work_item_id": work_item_id}
    return DecisionContext(
        tenant_id=ctx.tenant_id,
        cycle_id=ctx.cycle_id,
        trigger=ctx.trigger,
        subjects=ctx.subjects,
        constraints=ctx.constraints,
        operator_instruction=ctx.operator_instruction,
        agent_id=ctx.agent_id,
        domain_payload=ctx.domain_payload,
        metadata=new_meta,
    )


def _inject_conversation_history(
    ctx: "Any",  # DecisionContext
    history: list,
) -> "Any":
    """Devuelve un DecisionContext con el historial de la conversación en metadata.

    Bug "Hermes se presenta cada mensaje": run_conversation se llamaba con SOLO
    el mensaje actual (history=0) → el LLM trataba cada turno como el primero.
    Inyectamos el historial (mensajes previos) para que run_cycle lo pase a
    run_conversation y el agente responda EN CONTEXTO. Campo opaco metadata,
    NO toca la firma de run_cycle (Constitución I).
    """
    from hermes.domain.decision_context import DecisionContext  # noqa: PLC0415

    new_meta = {**ctx.metadata, "conversation_history": history}
    return DecisionContext(
        tenant_id=ctx.tenant_id,
        cycle_id=ctx.cycle_id,
        trigger=ctx.trigger,
        subjects=ctx.subjects,
        constraints=ctx.constraints,
        operator_instruction=ctx.operator_instruction,
        agent_id=ctx.agent_id,
        domain_payload=ctx.domain_payload,
        metadata=new_meta,
    )


def _taint_consent_if_chat(
    base: ConsentContext, is_chat: bool
) -> ConsentContext:
    """Devuelve un ConsentContext con derived_from_untrusted_content=True si chat.

    T052 (🔒 G4 / CTRL-P1-24): el contenido del usuario es untrusted. El broker
    lee este flag en dispatch() y fuerza HITL sobre proposals derivadas.
    """
    if not is_chat:
        return base
    return ConsentContext(
        tenant_id=base.tenant_id,
        operator_id=base.operator_id,
        derived_from_untrusted_content=True,
    )


def _extract_enqueued_by_uuid(item: "WorkItem") -> "UUID | None":
    """Extrae el operator_id del enqueued_by del payload del item.

    enqueued_by fue fijado server-side en ControlPlaneService.enqueue desde
    channel.sender_uid (CTRL-P1-3 / CWE-862) — nunca del payload del cliente.
    Retorna None si ausente o inválido: el broker seguirá fail-closed (CTRL-13).
    """
    from uuid import UUID  # noqa: PLC0415

    raw = item.payload.get("enqueued_by", "").strip()
    if not raw:
        return None
    try:
        return UUID(raw)
    except ValueError:
        logger.warning(
            "hermes.tasks.loop.enqueued_by_invalid_uuid: item=%s value=%r",
            item.id,
            raw[:64],
        )
        return None


def _inject_task_operator_id(
    ctx: "Any",  # DecisionContext
    operator_id: "UUID | None",
) -> "Any":
    """Inyecta task_operator_id en metadata del DecisionContext (spec 014 inc. 3).

    Permite que el engine resuelva el operator_id real por ciclo desde el
    WorkItem, sin cambiar la firma de run_cycle (Constitución I).
    Si operator_id es None no se inyecta nada (el engine usa su valor previo).
    """
    if operator_id is None:
        return ctx
    from hermes.domain.decision_context import DecisionContext  # noqa: PLC0415

    new_meta = {**ctx.metadata, "task_operator_id": operator_id}
    return DecisionContext(
        tenant_id=ctx.tenant_id,
        cycle_id=ctx.cycle_id,
        trigger=ctx.trigger,
        subjects=ctx.subjects,
        constraints=ctx.constraints,
        operator_instruction=ctx.operator_instruction,
        agent_id=ctx.agent_id,
        domain_payload=ctx.domain_payload,
        metadata=new_meta,
    )


def _override_operator_id(
    base: ConsentContext, operator_id: "UUID | None"
) -> ConsentContext:
    """Retorna un ConsentContext con operator_id rellenado desde enqueued_by.

    Rellena SOLO si base.operator_id es None (daemon arrancó sin
    HERMES_OPERATOR_ID). Si el daemon ya tiene un operator_id válido, lo
    preserva — ambas fuentes son legítimas y el daemon tiene precedencia.

    Seguridad: operator_id SOLO se toma de enqueued_by (server-side).
    Si operator_id es None, devuelve base sin cambios → fail-closed (CTRL-13).
    """
    if operator_id is None or base.operator_id is not None:
        return base
    return ConsentContext(
        tenant_id=base.tenant_id,
        operator_id=operator_id,
        derived_from_untrusted_content=base.derived_from_untrusted_content,
    )


def _taint_consent_if_external(
    base: ConsentContext, read_external_content: bool
) -> ConsentContext:
    """Eleva derived_from_untrusted_content si el ciclo leyó contenido externo.

    CTRL-5 / TOP-1: cierra el vector del loop autónomo. Si el motor ejecutó
    una tool READ que ingirió contenido no confiable (web, Composio, fichero
    fuera del allowlist de confianza), TODAS las proposals del ciclo quedan
    tainteadas → el broker fuerza HITL independientemente de su kind.

    Si base ya tiene derived_from_untrusted_content=True (chat_message), se
    mantiene — el taint es monotónico.
    """
    if base.derived_from_untrusted_content or not read_external_content:
        return base
    return ConsentContext(
        tenant_id=base.tenant_id,
        operator_id=base.operator_id,
        derived_from_untrusted_content=True,
    )


def _resolve_chat_agent_id(item_payload: "dict | None") -> str:
    """Return the contract agent id for live_activity tagging during a chat task.

    Reads agent_id from the WorkItem payload (set by chat_start via the
    per-conversation binding). Falls back to DEFAULT_AGENT_ID (CEO) when
    absent — never reads the global active_agent_id.
    """
    from hermes.agents.domain.agent import DEFAULT_AGENT_ID  # noqa: PLC0415

    if item_payload is None:
        return DEFAULT_AGENT_ID
    return str(item_payload.get("agent_id") or DEFAULT_AGENT_ID)


def _record_chat_activity(task_id: str, agent_id: str) -> None:
    """Register a synthetic chat-responding entry in live_activity. Fail-soft."""
    try:
        from hermes.runtime import live_activity  # noqa: PLC0415
        live_activity.record(task_id, agent_id, "chat_responding")
    except Exception:  # noqa: BLE001 — never interrupt the chat path
        pass


def _item_label(item: "WorkItem") -> str:
    """Return a short human label for a WorkItem (for notifications).

    Priority: payload 'label' > payload 'title' > instruction[:60] > task id.
    Never raises — used in notification emit (fail-soft context).
    """
    try:
        payload = item.payload or {}
        label = (
            payload.get("label")
            or payload.get("title")
            or payload.get("text", "")[:60]
            or str(item.id)[:8]
        )
        return str(label).strip()[:80] or str(item.id)[:8]
    except Exception:  # noqa: BLE001
        return str(item.id)[:8]


# Throttle for incremental assistant persistence: upsert the mirror every N answer
# deltas (balances "mirror is never stale on refresh" vs DB write frequency).
_PERSIST_EVERY_N_DELTAS = 12


class _CountingChunkSink:
    """Thin wrapper around a ChunkSinkPort that counts DELTA emissions.

    FIX B.2: the orchestrator wraps the real chunk_sink in this counter before
    injecting it into DecisionContext.metadata.  After run_cycle completes,
    delta_count > 0 means the engine streamed tokens incrementally.
    _handle_chat_narrative_reply then skips the monolithic re-emit.

    All other methods delegate unchanged — no behaviour difference.
    """

    def __init__(
        self,
        inner: Any,
        *,
        repo: Any = None,
        conversation_id: Any = None,
        task_id: Any = None,
    ) -> None:
        self._inner = inner
        self.delta_count: int = 0
        # Persistencia incremental del asistente (resume mirror-first en refresh):
        # acumulamos el texto de respuesta (DELTA, no thinking) y lo escribimos al
        # espejo cada _PERSIST_EVERY_N_DELTAS, fail-soft. Así un refresh repinta el
        # parcial desde la BD al instante, sin depender del replay volátil.
        self._repo = repo
        self._conv_id = conversation_id
        self._task_id = task_id
        self._answer = ""
        self._since_persist = 0

    async def emit(self, *, task_id: Any, chunk: Any) -> None:
        from hermes.tasks.control_plane.domain.ports import StreamChunkKind  # noqa: PLC0415
        kind = getattr(chunk, "kind", None)
        if kind is StreamChunkKind.DELTA:
            self.delta_count += 1
            self._answer += getattr(chunk, "delta", "") or ""
            self._since_persist += 1
            if self._since_persist >= _PERSIST_EVERY_N_DELTAS:
                self._since_persist = 0
                self._persist_partial("streaming")
        elif kind is StreamChunkKind.THINKING_DELTA:
            self.delta_count += 1
        await self._inner.emit(task_id=task_id, chunk=chunk)

    def _persist_partial(self, status: str) -> None:
        """Upsert the accumulated answer to the mirror. FAIL-SOFT — a DB hiccup must
        NEVER break the live stream / agent loop."""
        if self._repo is None or self._conv_id is None or self._task_id is None:
            return
        if not self._answer:
            return
        try:
            self._repo.upsert_assistant_message(
                conversation_id=self._conv_id,
                task_id=self._task_id,
                content=self._answer,
                status=status,
            )
        except Exception:  # noqa: BLE001 — persistence is best-effort; the stream wins
            logger.debug("hermes.tasks.loop.incremental_persist_failed", exc_info=True)

    async def close(self, *, task_id: Any, outcome: str, error: Any = None) -> None:
        await self._inner.close(task_id=task_id, outcome=outcome, error=error)

    async def emit_status(self, *, task_id: Any, status: str) -> None:
        await self._inner.emit_status(task_id=task_id, status=status)
