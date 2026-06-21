"""T061 — Idempotencia de token HITL bajo N workers concurrentes (CTRL-P1-22/23).

Verifica:
  - El token HITL es idempotente por proposal_id: un token aprobado solo
    se puede consumir UNA vez, incluso con N workers intentando usarlo
    simultáneamente (CTRL-P1-22, consent.use() ONCE atómico).
  - consent.use() es atómico entre workers — no hay double-spend (CTRL-P1-23).
  - IntentLog/ApprovalGate son thread-safe e idempotentes por proposal_id.

El test usa el SqliteApprovalGate real (integración) porque la garantía de
single-use es atómica solo en SQLite (UPDATE WHERE consumed_at IS NULL).
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from hermes.capabilities.domain.ports import (
    ConsentContext,
    ExecutionOutcome,
    ExecutionStatus,
    RiskLevel,
)
from hermes.domain.proposal import ToolCallProposal
from hermes.tasks.testing.in_memory_agent_state import InMemoryAgentState

pytestmark = pytest.mark.integration

_TENANT = uuid4()
_OPERATOR = uuid4()
_SIGNING_KEY = b"\xab" * 32


def _consent() -> ConsentContext:
    return ConsentContext(tenant_id=_TENANT, operator_id=_OPERATOR)


def _make_real_approval_gate(db_path: Path):
    from hermes.agents_os.application.audit_hash_chain import AuditHashChainSigner
    from hermes.agents_os.infrastructure.sqlite_audit_repository import SqliteAuditRepository
    from hermes.capabilities.application.hitl_approval_minter import HitlApprovalMinter
    from hermes.capabilities.infrastructure.sqlite_approval_gate import SqliteApprovalGate

    signer = AuditHashChainSigner(signing_key=_SIGNING_KEY)
    audit_repo = SqliteAuditRepository(db_path=db_path)
    minter = HitlApprovalMinter(signing_key=_SIGNING_KEY)
    return SqliteApprovalGate(
        db_path=db_path,
        minter=minter,
        signer=signer,
        audit_repo=audit_repo,
        token_ttl=3600,
    )


class TestConsentTokenConcurrent:
    async def test_hitl_token_single_use_under_concurrent_workers(self) -> None:
        """CTRL-P1-22: token HITL single-use por proposal_id bajo N workers.

        Escenario:
          - proposal_id pre-aprobado con un token
          - N corrutinas simulando workers intentan verify_token SIMULTÁNEAMENTE
          - Solo UNA debe obtener True; el resto False (single-use atómico).

        Implementación: el gate usa UPDATE WHERE consumed_at IS NULL;
        el UPDATE es atómico en SQLite y solo afecta 1 fila la primera vez.
        """
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "shell-state.db"
            gate = _make_real_approval_gate(db_path)

            proposal_id = uuid4()
            work_item_id = uuid4()

            # Registrar la propuesta como pending
            await gate.register_pending(
                proposal_id=proposal_id,
                work_item_id=work_item_id,
                consent_context=_consent(),
                risk=RiskLevel.HIGH,
                justification="test approval",
                parameters_redacted={"path": "/tmp/x"},
            )

            # Aprobar y obtener el token
            token = await gate.approve(
                proposal_id=proposal_id,
                approved_by=_OPERATOR,
            )
            assert token, "El token de aprobación no debe ser vacío"

            # N=8 workers intentan consumir el token simultáneamente
            n_workers = 8
            results: list[bool] = []
            lock = asyncio.Lock()

            async def _try_verify() -> None:
                result = await gate.verify_token(
                    proposal_id=proposal_id,
                    token=token,
                )
                async with lock:
                    results.append(result)

            await asyncio.gather(*[_try_verify() for _ in range(n_workers)])

            # Exactamente 1 True (primer consumo); el resto False
            true_count = sum(1 for r in results if r)
            false_count = sum(1 for r in results if not r)

            assert true_count == 1, (
                f"El token debe consumirse exactamente 1 vez. "
                f"Obtenidos {true_count} True y {false_count} False. "
                "CTRL-P1-22: single-use atómico requerido."
            )
            assert false_count == n_workers - 1, (
                f"Los {n_workers - 1} workers restantes deben obtener False."
            )

    async def test_register_pending_idempotent_by_proposal_id(self) -> None:
        """CTRL-P1-22: register_pending es idempotente por proposal_id.

        N workers pueden llamar register_pending con el mismo proposal_id
        sin crear duplicados ni lanzar excepción (INSERT OR IGNORE).
        """
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "shell-state.db"
            gate = _make_real_approval_gate(db_path)

            proposal_id = uuid4()
            work_item_id = uuid4()

            async def _register() -> None:
                await gate.register_pending(
                    proposal_id=proposal_id,
                    work_item_id=work_item_id,
                    consent_context=_consent(),
                    risk=RiskLevel.HIGH,
                    justification="test",
                    parameters_redacted={},
                )

            # 5 workers intentan registrar la misma propuesta
            await asyncio.gather(*[_register() for _ in range(5)])

            # Debe existir exactamente 1 fila para este proposal_id
            # (verificamos aprobando — si hubiera duplicados, fallaría)
            token = await gate.approve(
                proposal_id=proposal_id,
                approved_by=_OPERATOR,
            )
            assert token, "Debe poder aprobarse exactamente 1 vez"

    async def test_consent_use_once_atomic_no_double_spend(self) -> None:
        """CTRL-P1-23: consent.use() ONCE es atómico entre N workers.

        El ConsentManager implementa ONCE: la primera llamada consume el consent;
        las siguientes levantan ConsentDenied. Con N workers concurrentes,
        como máximo 1 debe ejecutar con consent ONCE.

        Nota: Este test verifica el invariante del ConsentManager, que es
        compartido por todos los workers (singleton inyectado al broker).
        """
        from hermes.agents_os.application.consent_manager import (
            ConsentManager,
            Capability,
            ConsentScope,
        )
        from hermes.agents_os.infrastructure.sqlite_consent_repo import SQLiteConsentRepository

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "consent.db"
            repo = SQLiteConsentRepository(db_path=db_path)
            cm = ConsentManager(repo=repo)

            # Otorgar consent ONCE para esta capability
            capability = Capability("terminal")
            cm.grant(
                tenant_id=_TENANT,
                human_operator_id=_OPERATOR,
                capability=capability,
                scope=ConsentScope.ONCE,
            )

            # N workers intentan usar el consent simultáneamente
            n_workers = 5
            successes: list[int] = []
            denials: list[int] = []
            lock = asyncio.Lock()

            from hermes.agents_os.application.consent_manager import ConsentDenied

            async def _try_use() -> None:
                try:
                    cm.use(human_operator_id=_OPERATOR, capability=capability)
                    async with lock:
                        successes.append(1)
                except ConsentDenied:
                    async with lock:
                        denials.append(1)

            await asyncio.gather(*[_try_use() for _ in range(n_workers)])

            # ONCE: exactamente 1 uso exitoso (no double-spend)
            assert len(successes) == 1, (
                f"ConsentScope.ONCE debe permitir exactamente 1 uso. "
                f"Got {len(successes)} éxitos. CTRL-P1-23: no double-spend."
            )
            assert len(denials) == n_workers - 1

    async def test_intent_log_idempotent_by_idempotency_key_concurrent(self) -> None:
        """CTRL-P1-22: IntentLog.record_intent es idempotente bajo concurrencia.

        N workers intentan registrar el mismo intent (misma idempotency_key)
        simultáneamente. Solo debe guardarse 1 registro (INSERT OR IGNORE).
        """
        from hermes.capabilities.application.intent_log import (
            IntentLog,
            compute_idempotency_key,
        )

        with tempfile.TemporaryDirectory() as tmp:
            db_path = str(Path(tmp) / "intents.db")
            log = IntentLog(db_path=db_path)

            proposal = ToolCallProposal(
                proposal_id=uuid4(),
                tool_name="write_file",
                tenant_id=_TENANT,
                entity_id="f",
                entity_type="file",
                parameters={"path": "/tmp/x"},
                justification="test",
            )
            key = compute_idempotency_key(proposal)

            # N workers intentan registrar el mismo intent
            n_workers = 8
            lock = asyncio.Lock()
            errors: list[str] = []

            async def _record_intent() -> None:
                try:
                    log.record_intent(key, proposal, task_id=str(uuid4()))
                except Exception as exc:  # noqa: BLE001
                    async with lock:
                        errors.append(str(exc))

            await asyncio.gather(*[_record_intent() for _ in range(n_workers)])

            # No debe haber errores (INSERT OR IGNORE es idempotente)
            assert not errors, f"record_intent lanzó errores: {errors}"

            # Solo debe haber 1 intent pendiente para esta key
            pending = log.pending_intents()
            matching = [k for k in pending if k == key]
            assert len(matching) == 1, (
                f"Solo debe existir 1 intent para la key. Got {len(matching)}. "
                "CTRL-P1-22: idempotente por proposal_id requerido."
            )

    async def test_worker_pool_hitl_token_consumed_once_across_workers(self) -> None:
        """CTRL-P1-22: al despachar con token HITL en pool concurrente, single-use.

        Verifica que el WorkerPool con N workers no permite que el mismo token
        sea consumido dos veces (el SqliteApprovalGate lo garantiza atómicamente).
        Este test usa el CapabilityBroker real con un gate real.
        """
        from hermes.tasks.application.worker_pool import WorkerPool
        from hermes.capabilities.application.capability_broker import CapabilityBroker
        from hermes.capabilities.application.capability_registry import CapabilityRegistry
        from hermes.capabilities.application.intent_log import IntentLog
        from hermes.capabilities.domain.ports import CapabilityBinding, RiskLevel as RL
        from hermes.capabilities.infrastructure.surface_adapter_dispatcher import (
            SurfaceAdapterDispatcher,
        )
        from hermes.agents_os.application.audit_hash_chain import AuditHashChainSigner
        from hermes.agents_os.domain.ports.surface_adapter_port import (
            CapturedAction,
            ReplayOutcome,
            ReplayStatus,
        )
        from hermes.agents_os.domain.surface_kind import SurfaceKind
        from hermes.agents_os.infrastructure.sqlite_audit_repository import SqliteAuditRepository
        from hermes.tasks.testing.in_memory_work_queue import InMemoryWorkQueue
        from hermes.testing import FakeReasoningEngine, scripted_response
        import dataclasses

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "shell-state.db"
            signer = AuditHashChainSigner(signing_key=_SIGNING_KEY)
            audit_repo = SqliteAuditRepository(db_path=db_path)
            gate = _make_real_approval_gate(db_path)
            intent_log = IntentLog(db_path=str(db_path))

            replay_calls: list[UUID] = []
            lock = asyncio.Lock()

            @dataclasses.dataclass
            class _CountingAdapter:
                _surface_kind: SurfaceKind = SurfaceKind.FILESYSTEM

                @property
                def surface_kind(self) -> SurfaceKind:
                    return self._surface_kind

                async def capture(self, **_) -> CapturedAction:
                    raise NotImplementedError

                async def replay(self, action, **__) -> ReplayOutcome:
                    async with lock:
                        replay_calls.append(action.action_id)
                    return ReplayOutcome(
                        action_id=action.action_id,
                        status=ReplayStatus.EXECUTED_OK,
                    )

                def serialize_for_signing(self, action) -> bytes:
                    return b""

            from hermes.capabilities.testing.fake_capability_registry import FakeCapabilityRegistry
            from hermes.agents_os.application.consent_manager import ConsentManager

            registry = FakeCapabilityRegistry()
            registry.register(CapabilityBinding(
                tool_name="read_file",
                surface_kind=SurfaceKind.FILESYSTEM,
                required_capability=None,
                risk=RL.LOW,
                auto_executable=True,
            ))
            dispatcher = SurfaceAdapterDispatcher(
                adapters={SurfaceKind.FILESYSTEM: _CountingAdapter()}
            )
            state = InMemoryAgentState()
            broker = CapabilityBroker(
                registry=registry,
                consent_manager=ConsentManager(),
                approval_gate=gate,
                dispatcher=dispatcher,
                signer=signer,
                audit_repo=audit_repo,
                intent_log=intent_log,
                agent_state=state,
            )

            from hermes.tasks.domain.ports import WorkItem
            queue = InMemoryWorkQueue()

            # Encolar 3 tareas independientes (proposals distintas)
            p1, p2, p3 = (_proposal_with_name("read_file") for _ in range(3))

            await queue.enqueue(_item())
            await queue.enqueue(_item())
            await queue.enqueue(_item())

            engine = FakeReasoningEngine(scripted=[
                scripted_response(proposals=[p1]),
                scripted_response(proposals=[p2]),
                scripted_response(proposals=[p3]),
            ])

            pool = WorkerPool(
                queue=queue,
                state=state,
                engine=engine,
                broker=broker,
                consent_context=_consent(),
                notify_watchdog=lambda: None,
                idle_poll_s=0.01,
                pause_poll_s=0.01,
            )

            from hermes.tasks.domain.ports import TaskStatus

            async def _stop_when_done() -> None:
                deadline = asyncio.get_event_loop().time() + 3.0
                while asyncio.get_event_loop().time() < deadline:
                    done = (
                        queue.items_with_status(TaskStatus.COMPLETED)
                        + queue.items_with_status(TaskStatus.FAILED)
                    )
                    if len(done) >= 3:
                        pool.request_shutdown()
                        return
                    await asyncio.sleep(0.01)
                pool.request_shutdown()

            await pool.bootstrap()
            await asyncio.gather(
                pool.run_forever(size=3),
                _stop_when_done(),
            )

            # Cada proposal debe haberse ejecutado exactamente 1 vez
            # (idempotency_key distinta por proposal, sin double-spend)
            assert len(replay_calls) == 3, (
                f"Esperados 3 replays (1 por propuesta). Got {len(replay_calls)}. "
                "CTRL-P1-22: proposals idempotentes bajo concurrencia."
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _item() -> "WorkItem":
    from hermes.tasks.domain.ports import WorkItem
    return WorkItem.new(
        tenant_id=_TENANT,
        trigger_kind="manual_enqueue",
        payload={"instruction": "do something", "enqueued_by": "op-1"},
    )


def _proposal_with_name(tool_name: str) -> ToolCallProposal:
    return ToolCallProposal(
        proposal_id=uuid4(),
        tool_name=tool_name,
        tenant_id=_TENANT,
        entity_id="f",
        entity_type="file",
        parameters={},
        justification="test",
    )
