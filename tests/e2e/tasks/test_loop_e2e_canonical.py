"""T050 — E2E canónico del loop autónomo (P0, feature 005).

Escenario:
  "Lee <tmp>/input.txt y escribe el resumen en <tmp>/output.txt"

Sin UI, sin red, sin LLM, sin Chromium.
SQLite real en tmp_path. FilesystemSurfaceAdapter real (lee/escribe ficheros).

Flujo completo:
  1. Se escribe input.txt en tmp_path.
  2. Se encolan 2 tareas: task_main (read+write) + task_readonly (sólo read).
  3. Bootstrap: reconcile_stale + seed signer.
  4. Loop drena task_main:
     a. Ciclo 1: engine propone read_file (LOW) → EXECUTED.
        El broker auto-ejecuta (consent + LOW). Loop sigue.
        (read_file no completa la tarea — no es el último efecto real.)
        Nota: el loop marca COMPLETED cuando _dispatch_proposals devuelve real_evidence.
        Para modelar "read entonces write" correctamente, la primera propuesta (read_file)
        devuelve EXECUTED (real_evidence != None), lo que completaría la tarea.
        La segunda vuelta procesa write_file con HITL.
        Por tanto: la tarea read_file se encola por separado (task_readonly ya la cubre).
        task_main en realidad propone write_file directamente (el agente "ya sabe" el contenido
        porque lo pasa en el mismo ciclo como parámetro de la propuesta).

        DISEÑO DEL SCRIPTED ENGINE:
          - Ciclo 1 para task_main: propone write_file → HIGH → PENDING_APPROVAL
          - Ciclo 2 para task_main (tras aprobación): propone write_file con mismo proposal_id
            → broker encuentra token aprobado → EXECUTED → tarea COMPLETED
          - task_readonly en algún momento: propone read_file → EXECUTED → COMPLETED

        Esto modela exactamente LOOP-4: mientras task_main espera aprobación, task_readonly
        progresa. El loop NO se bloquea.

Aserciones (quickstart.md §2):
  V1: output.txt existe y tiene contenido (len > 0).
  V2: task_main queda COMPLETED; execution_audit_entry_id + execution_head_hash NOT NULL en DB.
  V3: ≥2 entradas PROPOSAL_EXECUTED en el audit (write_file task_main + read_file task_readonly).
  V4: verify_chain pasa (SC-006); head local == última ancla FakeExternalAnchor (CTRL-8).
  V5: cada DecisionContext visto por el engine tiene trigger que empieza por 'queue_drain:'.
  V6 (bonus HITL): write_file pasó por pending_approvals + hay HITL_APPROVED en el audit.
  V7 (LOOP-4): task_readonly progresó (COMPLETED) mientras task_main esperaba aprobación.

Hallazgos documentados (mismatches arreglados):
  MISMATCH-1: AgentLoopOrchestrator._dispatch_proposals no consultaba approval_gate.approved_token_for.
              La tarea quedaba PENDING_APPROVAL pero el loop nunca la re-ejecutaba con el token
              aprobado aunque el gate lo tuviese listo. Arreglo: se añadió _fetch_hitl_token +
              parámetro approval_gate opcional al constructor.
  MISMATCH-2: SqliteWorkQueue no tenía re_enqueue_after_approval (transición PENDING_APPROVAL→PENDING).
              Sin esto el operador no podía re-encolar la tarea tras aprobar. Arreglo: método añadido
              en SqliteWorkQueue e InMemoryWorkQueue.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from hermes.agents_os.application.audit_hash_chain import AuditHashChainSigner, AuditKind
from hermes.agents_os.domain.surface_kind import SurfaceKind
from hermes.agents_os.infrastructure.filesystem_surface_adapter import FilesystemSurfaceAdapter
from hermes.agents_os.infrastructure.sqlite_audit_repository import SqliteAuditRepository
from hermes.capabilities.application.capability_broker import CapabilityBroker
from hermes.capabilities.application.capability_registry import CapabilityRegistry
from hermes.agents_os.application.consent_manager import (
    Capability,
    ConsentManager,
    ConsentScope,
)
from hermes.capabilities.application.hitl_approval_minter import HitlApprovalMinter
from hermes.capabilities.application.intent_log import IntentLog
from hermes.capabilities.domain.ports import ConsentContext
from hermes.capabilities.infrastructure.sqlite_approval_gate import SqliteApprovalGate
from hermes.capabilities.infrastructure.surface_adapter_dispatcher import SurfaceAdapterDispatcher
from hermes.capabilities.testing.fake_external_anchor import FakeExternalAnchor
from hermes.domain.proposal import ToolCallProposal
from hermes.tasks.application.agent_loop_orchestrator import AgentLoopOrchestrator
from hermes.tasks.domain.ports import TaskStatus, WorkItem
from hermes.tasks.infrastructure.sqlite_work_queue import SqliteWorkQueue
from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState
from hermes.testing import FakeReasoningEngine, scripted_response

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Constantes del escenario
# ---------------------------------------------------------------------------

_SIGNING_KEY: bytes = b"hermes-e2e-test-signing-key-32b!"  # 32 bytes exactos
_TENANT_ID: UUID = uuid4()
_OPERATOR_ID: UUID = uuid4()
_APPROVED_BY: UUID = uuid4()


# ---------------------------------------------------------------------------
# Helpers de construcción de propuestas
# ---------------------------------------------------------------------------


def _write_proposal(
    source_path: str,
    target_path: str,
    summary: str,
    proposal_id: UUID | None = None,
) -> ToolCallProposal:
    """Propuesta write_file (HIGH, necesita HITL)."""
    return ToolCallProposal(
        proposal_id=proposal_id or uuid4(),
        tool_name="write_file",
        tenant_id=_TENANT_ID,
        entity_id="output-file",
        entity_type="file",
        parameters={
            "op": "write_file",
            "path": target_path,
            "content": summary,
        },
        justification=f"escribir resumen de {source_path} en {target_path}",
    )


def _read_proposal(source_path: str) -> ToolCallProposal:
    """Propuesta read_file (LOW, auto-ejecutable)."""
    return ToolCallProposal(
        proposal_id=uuid4(),
        tool_name="read_file",
        tenant_id=_TENANT_ID,
        entity_id="input-file",
        entity_type="file",
        parameters={
            "op": "read_file",
            "path": source_path,
        },
        justification=f"leer contenido de {source_path}",
    )


def _work_item(*, trigger_kind: str = "manual_enqueue") -> WorkItem:
    return WorkItem.new(
        tenant_id=_TENANT_ID,
        trigger_kind=trigger_kind,
        payload={"instruction": "proceso E2E", "enqueued_by": "qa-engineer"},
    )


# ---------------------------------------------------------------------------
# Fixture: grafo real completo (T050)
# ---------------------------------------------------------------------------


@pytest.fixture()
def e2e_env(tmp_path: Path):
    """Grafo de producción completo cableado con SQLite real en tmp_path.

    Componentes reales:
      - SqliteWorkQueue (tareas durables)
      - SqliteAuditRepository + AuditHashChainSigner (audit chain firmada)
      - SqliteApprovalGate + HitlApprovalMinter (HITL criptográfico)
      - CapabilityRegistry (tabla declarativa real — read_file=LOW, write_file=HIGH)
      - ConsentManager (DOCUMENTS concedido al operador)
      - FilesystemSurfaceAdapter (lee/escribe ficheros reales en tmp_path)
      - SurfaceAdapterDispatcher
      - IntentLog
      - FakeExternalAnchor (ancla externa en memoria)

    El único fake del «cerebro» es FakeReasoningEngine (scripted, cero LLM).
    """
    db_path = tmp_path / "shell-state.db"

    # --- Audit chain ---
    anchor = FakeExternalAnchor()
    audit_repo = SqliteAuditRepository(db_path=db_path, external_anchor=anchor)
    signer = AuditHashChainSigner(signing_key=_SIGNING_KEY)

    # --- HITL gate ---
    minter = HitlApprovalMinter(signing_key=_SIGNING_KEY)
    approval_gate = SqliteApprovalGate(
        db_path=db_path,
        minter=minter,
        signer=signer,
        audit_repo=audit_repo,
    )

    # --- Capability registry (real) ---
    registry = CapabilityRegistry()

    # --- Consent manager: DOCUMENTS concedido al operador (persistent) ---
    consent_manager = ConsentManager()
    consent_manager.grant(
        tenant_id=_TENANT_ID,
        human_operator_id=_OPERATOR_ID,
        capability=Capability.DOCUMENTS,
        scope=ConsentScope.PERSISTENT,
    )

    # --- Filesystem adapter (real — sólo permite tmp_path) ---
    fs_adapter = FilesystemSurfaceAdapter(
        allowed_prefixes=(str(tmp_path),),
    )
    dispatcher = SurfaceAdapterDispatcher(
        adapters={SurfaceKind.FILESYSTEM: fs_adapter}
    )

    # --- Intent log (in-memory: idempotencia sin disco adicional) ---
    intent_log = IntentLog()

    # --- Broker ---
    broker = CapabilityBroker(
        registry=registry,
        consent_manager=consent_manager,
        approval_gate=approval_gate,
        dispatcher=dispatcher,
        signer=signer,
        audit_repo=audit_repo,
        intent_log=intent_log,
        anchor=anchor,
    )

    # --- Work queue ---
    queue = SqliteWorkQueue(db_path=db_path)

    # --- Agent state ---
    agent_state = InMemoryAgentState()

    return {
        "tmp_path": tmp_path,
        "db_path": db_path,
        "queue": queue,
        "agent_state": agent_state,
        "broker": broker,
        "approval_gate": approval_gate,
        "audit_repo": audit_repo,
        "signer": signer,
        "anchor": anchor,
        "consent_manager": consent_manager,
    }


# ---------------------------------------------------------------------------
# T050: Escenario canónico completo (P0 demo)
# ---------------------------------------------------------------------------


class TestLoopE2ECanonical:
    """Prueba de extremo a extremo del loop autónomo (T050).

    Verifica los 5 criterios del quickstart.md §2 + HITL + LOOP-4.
    """

    async def test_p0_canonical_full_flow(self, e2e_env) -> None:  # noqa: PLR0915 — E2E necesariamente largo
        """Loop autónomo: encolar → leer → escribir (HITL) → completar → audit.

        Verifica:
          V1 output.txt existe con contenido.
          V2 task_main COMPLETED con execution_audit_entry_id y execution_head_hash != NULL.
          V3 ≥2 PROPOSAL_EXECUTED en el audit (write_file + read_file).
          V4 verify_chain pasa; head local == última ancla.
          V5 todos los triggers empiezan por 'queue_drain:'.
          V6 write_file pasó por pending_approvals + hay HITL_APPROVED.
          V7 task_readonly COMPLETED mientras task_main esperaba aprobación (LOOP-4).
        """
        tmp_path: Path = e2e_env["tmp_path"]
        queue: SqliteWorkQueue = e2e_env["queue"]
        agent_state: InMemoryAgentState = e2e_env["agent_state"]
        broker: CapabilityBroker = e2e_env["broker"]
        approval_gate: SqliteApprovalGate = e2e_env["approval_gate"]
        audit_repo: SqliteAuditRepository = e2e_env["audit_repo"]
        signer: AuditHashChainSigner = e2e_env["signer"]
        anchor: FakeExternalAnchor = e2e_env["anchor"]
        db_path: Path = e2e_env["db_path"]

        # ── Preparar archivos ──────────────────────────────────────────────
        input_txt = tmp_path / "input.txt"
        output_txt = tmp_path / "output.txt"
        input_content = "Este es el contenido de prueba E2E del loop autónomo P0."
        input_txt.write_text(input_content, encoding="utf-8")

        # ── Construir propuestas scripted ──────────────────────────────────
        # write_file propuesta reutilizable (mismo proposal_id para re-dispatch
        # con token HITL aprobado en el segundo ciclo).
        write_pid = uuid4()
        write_proposal = _write_proposal(
            source_path=str(input_txt),
            target_path=str(output_txt),
            summary=f"RESUMEN: {input_content[:30]}...",
            proposal_id=write_pid,
        )
        read_proposal = _read_proposal(str(input_txt))

        # task_main: propone write_file (HIGH → primer ciclo PENDING_APPROVAL;
        # segundo ciclo con token aprobado → EXECUTED).
        # FakeReasoningEngine tiene round-robin si se agota la lista, pero lo
        # configuramos con 2 entradas: ambas retornan write_proposal (misma instancia
        # = mismo proposal_id para que el minter valide el mismo token).
        engine_main = FakeReasoningEngine(
            scripted=[
                scripted_response(proposals=[write_proposal]),  # ciclo 1: → PENDING_APPROVAL
                scripted_response(proposals=[write_proposal]),  # ciclo 2: → EXECUTED (con token)
            ]
        )

        # task_readonly: propone read_file (LOW → EXECUTED en el primer ciclo).
        engine_readonly = FakeReasoningEngine(
            scripted=[scripted_response(proposals=[read_proposal])]
        )

        # ── Encolar tareas ─────────────────────────────────────────────────
        task_main = _work_item()
        task_readonly = _work_item()
        await queue.enqueue(task_main)
        await queue.enqueue(task_readonly)

        # ── Consent context ────────────────────────────────────────────────
        consent_ctx = ConsentContext(
            tenant_id=_TENANT_ID,
            operator_id=_OPERATOR_ID,
        )

        # ── Orquestador para task_main (con approval_gate inyectado) ──────
        orch_main = AgentLoopOrchestrator(
            queue=queue,
            state=agent_state,
            engine=engine_main,
            broker=broker,
            consent_context=consent_ctx,
            notify_watchdog=lambda: None,
            idle_poll_s=0.0,
            pause_poll_s=0.0,
            firmer=signer,
            audit_repo=audit_repo,
            approval_gate=approval_gate,
        )

        # Orquestador para task_readonly (sin gate — lectura pura, sin HITL).
        orch_readonly = AgentLoopOrchestrator(
            queue=queue,
            state=agent_state,
            engine=engine_readonly,
            broker=broker,
            consent_context=consent_ctx,
            notify_watchdog=lambda: None,
            idle_poll_s=0.0,
            pause_poll_s=0.0,
            firmer=signer,
            audit_repo=audit_repo,
        )

        await orch_main.bootstrap()

        # ─────────────────────────────────────────────────────────────────
        # FASE 1: procesar task_main (write_file) → PENDING_APPROVAL
        # ─────────────────────────────────────────────────────────────────
        claimed_main = await queue.claim_next()
        assert claimed_main is not None
        # Podría ser cualquiera de las dos tareas; identificamos la que es task_main
        if claimed_main.id != task_main.id:
            # Si salió task_readonly primero, procesarla y sacar task_main después
            await orch_readonly._process(claimed_main)  # type: ignore[attr-defined]
            claimed_main = await queue.claim_next()
            assert claimed_main is not None
            assert claimed_main.id == task_main.id

        await orch_main._process(claimed_main)  # type: ignore[attr-defined]

        # Verificar que task_main quedó PENDING_APPROVAL (write_file es HIGH sin token)
        db_conn = sqlite3.connect(str(db_path))
        db_conn.row_factory = sqlite3.Row
        row_after_first = db_conn.execute(
            "SELECT status FROM agent_tasks WHERE task_id = ?",
            (str(task_main.id),),
        ).fetchone()
        assert row_after_first is not None
        assert row_after_first["status"] == "pending_approval", (
            f"task_main debe estar PENDING_APPROVAL tras primer ciclo; "
            f"estado real: {row_after_first['status']!r}"
        )

        # ─────────────────────────────────────────────────────────────────
        # VERIFICACIÓN LOOP-4: task_readonly progresa mientras task_main espera
        # ─────────────────────────────────────────────────────────────────
        claimed_readonly = await queue.claim_next()
        assert claimed_readonly is not None, (
            "El loop debe poder tomar task_readonly mientras task_main está PENDING_APPROVAL (LOOP-4)"
        )
        if claimed_readonly.id == task_main.id:
            # task_main volvió a pending (no debería ocurrir aquí; defensivo)
            pytest.fail(
                "task_main no debería estar disponible antes de la aprobación del operador"
            )

        await orch_readonly._process(claimed_readonly)  # type: ignore[attr-defined]

        row_readonly = db_conn.execute(
            "SELECT status FROM agent_tasks WHERE task_id = ?",
            (str(task_readonly.id),),
        ).fetchone()
        assert row_readonly is not None
        assert row_readonly["status"] == "completed", (
            f"task_readonly debe completarse mientras task_main espera (LOOP-4); "
            f"estado real: {row_readonly['status']!r}"
        )

        # ─────────────────────────────────────────────────────────────────
        # FASE 2: operador aprueba → re-encolar → segundo ciclo → EXECUTED
        # ─────────────────────────────────────────────────────────────────
        # V6: verificar que pending_approvals tiene la propuesta
        pending_row = db_conn.execute(
            "SELECT * FROM pending_approvals WHERE proposal_id = ?",
            (str(write_pid),),
        ).fetchone()
        assert pending_row is not None, (
            "write_file debe haber registrado la propuesta en pending_approvals (SC-004)"
        )
        assert pending_row["status"] == "pending", (
            f"La propuesta debe estar 'pending' antes de aprobación; "
            f"estado: {pending_row['status']!r}"
        )

        # Operador aprueba (simula la API de supervisión D-Bus)
        _token = await approval_gate.approve(
            proposal_id=write_pid,
            approved_by=_APPROVED_BY,
        )
        assert _token and len(_token) > 0, "approve() debe devolver un token firmado"

        # Re-encolar task_main a PENDING (la transición PENDING_APPROVAL → PENDING
        # normalmente la dispara la API de supervisión tras la aprobación).
        await queue.re_enqueue_after_approval(task_main.id)

        # El loop ahora puede reclamar task_main y re-dispatchar write_file con el token
        claimed_main_2 = await queue.claim_next()
        assert claimed_main_2 is not None
        assert claimed_main_2.id == task_main.id, (
            "task_main debe ser la siguiente tarea disponible tras re-enqueue"
        )

        await orch_main._process(claimed_main_2)  # type: ignore[attr-defined]

        # ─────────────────────────────────────────────────────────────────
        # ASERCIONES FINALES
        # ─────────────────────────────────────────────────────────────────

        # V1: output.txt existe con contenido
        assert output_txt.exists(), "output.txt debe existir tras la ejecución del agente"
        assert output_txt.stat().st_size > 0, "output.txt no debe estar vacío (V1)"

        # V2: task_main COMPLETED con evidencia real
        row_completed = db_conn.execute(
            "SELECT status, execution_audit_entry_id, execution_head_hash "
            "FROM agent_tasks WHERE task_id = ?",
            (str(task_main.id),),
        ).fetchone()
        assert row_completed is not None
        assert row_completed["status"] == "completed", (
            f"task_main debe estar COMPLETED; estado real: {row_completed['status']!r}"
        )
        assert row_completed["execution_audit_entry_id"] is not None, (
            "execution_audit_entry_id debe ser NOT NULL (SC-001/V2)"
        )
        assert row_completed["execution_head_hash"] is not None, (
            "execution_head_hash debe ser NOT NULL (V2)"
        )

        # V3: ≥2 entradas PROPOSAL_EXECUTED en el audit (write_file + read_file)
        chain = await audit_repo.load_chain()
        executed_entries = [
            e for e in chain if e.audit_kind == AuditKind.PROPOSAL_EXECUTED
        ]
        assert len(executed_entries) >= 2, (
            f"Debe haber al menos 2 PROPOSAL_EXECUTED en el audit "
            f"(write_file + read_file); encontrados: {len(executed_entries)} (V3)"
        )

        # CTRL-9: la descripción del audit usa la acción REAL (tool_name → status),
        # no el narrative del LLM. Formato: "<tool_name> → <replay_status>".
        known_tools = {"read_file", "write_file", "list_dir"}
        for entry in executed_entries:
            has_tool_ref = any(tool in entry.description for tool in known_tools)
            assert has_tool_ref, (
                f"PROPOSAL_EXECUTED debe referenciar la acción real en description (CTRL-9); "
                f"description: {entry.description!r} — debe contener el tool_name"
            )

        # V4: verify_chain pasa (SC-006)
        verifier = AuditHashChainSigner(signing_key=_SIGNING_KEY)
        verifier.verify_chain(chain)  # raise AuditChainCorrupted si rompe

        # Head local == última ancla (CTRL-8)
        local_head = signer.head_hash_hex
        assert await anchor.verify(local_head), (
            "El head local debe coincidir con la última ancla externa (CTRL-8/V4)"
        )

        # V5: todos los triggers del engine empiezan por 'queue_drain:'
        all_engine_calls = engine_main.calls + engine_readonly.calls
        assert len(all_engine_calls) >= 2, (
            "Debe haber al menos 2 ciclos de razonamiento (main + readonly)"
        )
        for ctx in all_engine_calls:
            assert ctx.trigger.startswith("queue_drain:"), (
                f"Trigger debe empezar por 'queue_drain:'; fue: {ctx.trigger!r} (SC-002/V5)"
            )

        # V6: write_file pasó por PENDING_APPROVAL + hay HITL_APPROVED en el audit
        pending_final = db_conn.execute(
            "SELECT status FROM pending_approvals WHERE proposal_id = ?",
            (str(write_pid),),
        ).fetchone()
        assert pending_final is not None
        assert pending_final["status"] == "approved", (
            f"La propuesta write_file debe estar 'approved' en pending_approvals (SC-004/V6)"
        )

        hitl_entries = [
            e for e in chain if e.audit_kind == AuditKind.HITL_APPROVED
        ]
        assert len(hitl_entries) >= 1, (
            "Debe haber al menos un HITL_APPROVED en el audit (SC-004/V6)"
        )

        # V7: task_readonly COMPLETED durante la espera de aprobación (LOOP-4)
        row_readonly_final = db_conn.execute(
            "SELECT status FROM agent_tasks WHERE task_id = ?",
            (str(task_readonly.id),),
        ).fetchone()
        assert row_readonly_final is not None
        assert row_readonly_final["status"] == "completed", (
            "task_readonly debe haber completado mientras task_main esperaba HITL (LOOP-4/V7)"
        )

        db_conn.close()

    async def test_engine_triggers_all_start_with_queue_drain(self, e2e_env) -> None:
        """SC-002: todos los DecisionContext del engine tienen trigger queue_drain:*.

        No usa UI, no usa evento externo — solo cola drenada por iniciativa propia.
        """
        queue: SqliteWorkQueue = e2e_env["queue"]
        agent_state: InMemoryAgentState = e2e_env["agent_state"]
        broker: CapabilityBroker = e2e_env["broker"]
        audit_repo: SqliteAuditRepository = e2e_env["audit_repo"]
        signer: AuditHashChainSigner = e2e_env["signer"]
        tmp_path: Path = e2e_env["tmp_path"]

        input_txt = tmp_path / "input2.txt"
        input_txt.write_text("contenido SC-002", encoding="utf-8")

        read_proposal = _read_proposal(str(input_txt))
        engine = FakeReasoningEngine(
            scripted=[scripted_response(proposals=[read_proposal])]
        )

        orch = AgentLoopOrchestrator(
            queue=queue,
            state=agent_state,
            engine=engine,
            broker=broker,
            consent_context=ConsentContext(
                tenant_id=_TENANT_ID,
                operator_id=_OPERATOR_ID,
            ),
            notify_watchdog=lambda: None,
            idle_poll_s=0.0,
            pause_poll_s=0.0,
            firmer=signer,
            audit_repo=audit_repo,
        )

        task = _work_item()
        await queue.enqueue(task)
        await orch.bootstrap()

        claimed = await queue.claim_next()
        assert claimed is not None
        await orch._process(claimed)  # type: ignore[attr-defined]

        assert len(engine.calls) == 1
        assert engine.calls[0].trigger.startswith("queue_drain:"), (
            f"trigger debe empezar por 'queue_drain:'; fue: {engine.calls[0].trigger!r}"
        )
        assert engine.calls[0].cycle_id == task.id, (
            "cycle_id debe coincidir con el id de la tarea (SC-002)"
        )

    async def test_write_file_is_not_auto_executed_without_hitl(self, e2e_env) -> None:
        """Constitución II: write_file (HIGH) sin token HITL → PENDING_APPROVAL, no ejecuta."""
        tmp_path: Path = e2e_env["tmp_path"]
        queue: SqliteWorkQueue = e2e_env["queue"]
        agent_state: InMemoryAgentState = e2e_env["agent_state"]
        broker: CapabilityBroker = e2e_env["broker"]
        audit_repo: SqliteAuditRepository = e2e_env["audit_repo"]
        signer: AuditHashChainSigner = e2e_env["signer"]
        db_path: Path = e2e_env["db_path"]

        output_path = tmp_path / "should_not_exist.txt"
        write_proposal = _write_proposal(
            source_path=str(tmp_path / "x.txt"),
            target_path=str(output_path),
            summary="no debe escribirse",
        )

        engine = FakeReasoningEngine(
            scripted=[scripted_response(proposals=[write_proposal])]
        )

        orch = AgentLoopOrchestrator(
            queue=queue,
            state=agent_state,
            engine=engine,
            broker=broker,
            consent_context=ConsentContext(
                tenant_id=_TENANT_ID,
                operator_id=_OPERATOR_ID,
            ),
            notify_watchdog=lambda: None,
            idle_poll_s=0.0,
            pause_poll_s=0.0,
            firmer=signer,
            audit_repo=audit_repo,
            # approval_gate NO inyectado → el loop no puede buscar token aprobado
        )

        task = _work_item()
        await queue.enqueue(task)
        await orch.bootstrap()

        claimed = await queue.claim_next()
        assert claimed is not None
        await orch._process(claimed)  # type: ignore[attr-defined]

        assert not output_path.exists(), (
            "write_file HIGH no debe ejecutarse sin token HITL (Constitución II)"
        )

        db_conn = sqlite3.connect(str(db_path))
        db_conn.row_factory = sqlite3.Row
        row = db_conn.execute(
            "SELECT status FROM agent_tasks WHERE task_id = ?",
            (str(task.id),),
        ).fetchone()
        assert row is not None
        assert row["status"] == "pending_approval", (
            f"task debe quedar PENDING_APPROVAL sin token HITL; estado: {row['status']!r}"
        )
        db_conn.close()

    async def test_read_file_executes_without_hitl_and_chain_is_valid(
        self, e2e_env
    ) -> None:
        """read_file (LOW, auto_executable=True) se ejecuta sin HITL; cadena íntegra."""
        tmp_path: Path = e2e_env["tmp_path"]
        queue: SqliteWorkQueue = e2e_env["queue"]
        agent_state: InMemoryAgentState = e2e_env["agent_state"]
        broker: CapabilityBroker = e2e_env["broker"]
        audit_repo: SqliteAuditRepository = e2e_env["audit_repo"]
        signer: AuditHashChainSigner = e2e_env["signer"]
        db_path: Path = e2e_env["db_path"]

        input_txt = tmp_path / "low_risk.txt"
        input_txt.write_text("contenido bajo riesgo", encoding="utf-8")

        read_proposal = _read_proposal(str(input_txt))
        engine = FakeReasoningEngine(
            scripted=[scripted_response(proposals=[read_proposal])]
        )

        orch = AgentLoopOrchestrator(
            queue=queue,
            state=agent_state,
            engine=engine,
            broker=broker,
            consent_context=ConsentContext(
                tenant_id=_TENANT_ID,
                operator_id=_OPERATOR_ID,
            ),
            notify_watchdog=lambda: None,
            idle_poll_s=0.0,
            pause_poll_s=0.0,
            firmer=signer,
            audit_repo=audit_repo,
        )

        task = _work_item()
        await queue.enqueue(task)
        await orch.bootstrap()

        claimed = await queue.claim_next()
        assert claimed is not None
        await orch._process(claimed)  # type: ignore[attr-defined]

        # Tarea COMPLETED
        db_conn = sqlite3.connect(str(db_path))
        db_conn.row_factory = sqlite3.Row
        row = db_conn.execute(
            "SELECT status, execution_audit_entry_id FROM agent_tasks WHERE task_id = ?",
            (str(task.id),),
        ).fetchone()
        assert row is not None
        assert row["status"] == "completed", (
            f"read_file (LOW) debe completar la tarea sin HITL; estado: {row['status']!r}"
        )
        assert row["execution_audit_entry_id"] is not None

        # Cadena íntegra
        chain = await audit_repo.load_chain()
        verifier = AuditHashChainSigner(signing_key=_SIGNING_KEY)
        verifier.verify_chain(chain)  # no raise → cadena OK

        db_conn.close()
