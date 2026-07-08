"""T039 — CapabilityBroker (CTRL-1..6, 9, 14 / FR-013..018).

ÚNICO choke-point del agente con el SO real (FR-014). Todo dispatch de
ToolCallProposal pasa por aquí. Fail-closed en cada rama de duda.

Secuencia de 8 pasos (research.md §El broker/effector como dispatcher):
  1. resolver(tool_name) → binding | None ⇒ REJECTED_BY_POLICY + audit.
  2. Clasificar riesgo efectivo: binding.risk, elevado si taint o ApiCall+PII.
  3. Consent: operator_id None ⇒ REJECTED_BY_CONSENT. Si required_capability,
     consent.assert_active() fail-closed. Inmediatamente antes del replay (CTRL-2).
  4. HITL: si riesgo HIGH o requires_forced_hitl → exige token válido.
     Sin token: register_pending + PENDING_APPROVAL. Sin replay.
  5. Idempotencia: idempotency_key = compute_idempotency_key(proposal).
     Si was_executed ⇒ no re-ejecutar. record_intent ANTES del efecto.
  6. Sintetizar CapturedAction(surface_kind, payload=proposal.parameters).
     Rehidratación de PII SOLO aquí, lo más tarde posible (CTRL-14);
     nunca a logs.
  7. dispatcher.replay(action, hitl_approval_token, consent_token).
  8. consent.use() tras éxito. record_outcome. Firma + persiste audit
     PROPOSAL_EXECUTED con la ACCIÓN REAL (CTRL-9). Mapea
     ReplayOutcome.status → ExecutionStatus. Devuelve ExecutionOutcome
     con audit_entry_id real.

Controles implementados:
  CTRL-1  — token HITL criptográfico (ApprovalGatePort.verify_token).
  CTRL-2  — consent.assert_active ANTES del replay.
  CTRL-3  — BROKER-7 heredado del registro (ExtendedCapabilityBinding).
  CTRL-4  — riesgo LOW + auto_executable ⇒ sin HITL.
  CTRL-5  — taint untrusted ⇒ HITL forzado (requires_forced_hitl).
  CTRL-9  — audit del ReplayOutcome real, no del narrative.
  CTRL-11 — intent_log idempotente antes del efecto.
  CTRL-13 — operator_id None ⇒ fail-closed total.
  CTRL-14 — ApiCall + PII ⇒ HITL elevado.

NO toca: BrowserPort, SurfaceAdapterPort (los consume), ConsentManager
  (lo orquesta), AuditHashChainSigner (lo usa). Constitución I/II/IV.

Capa: application (orquesta domain + infraestructura vía puertos).
Sin framework. Sin I/O directa (delega al dispatcher + repo).
"""

from __future__ import annotations

import contextlib
import logging
import re
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from hermes.agents.domain.agent import AutonomyLevel
from hermes.agents_os.application.audit_hash_chain import AuditHashChainSigner, AuditKind
from hermes.agents_os.application.consent_manager import Capability, ConsentDenied, ConsentScope
from hermes.agents_os.domain.ports.surface_adapter_port import CapturedAction, ReplayStatus
from hermes.agents_os.domain.surface_kind import SurfaceKind
from hermes.capabilities.application.capability_registry import (
    is_terminal_command_allowlisted,
)
from hermes.capabilities.application.intent_log import IntentLog, compute_idempotency_key
from hermes.capabilities.domain.ports import (
    ApprovalGatePort,
    CapabilityRegistryPort,
    ConsentContext,
    ExecutionOutcome,
    ExecutionStatus,
    RiskLevel,
    SignedAuditRepositoryPort,
)
from hermes.capabilities.domain.provenance_taint import (
    ProvenanceTaint,
    is_sensitive_path_read_under_taint,
    requires_forced_hitl,
)
from hermes.capabilities.infrastructure.surface_adapter_dispatcher import (
    SurfaceAdapterDispatcher,
    SurfaceAdapterNotFound,
)
from hermes.domain.proposal import ToolCallProposal

if TYPE_CHECKING:
    from hermes.capabilities.application.external_anchor import ExternalAnchorPort
    from hermes.capabilities.application.install_executor import InstallExecutorPort
    from hermes.capabilities.infrastructure.composio_surface_adapter import ComposioSurfaceAdapter
    from hermes.capabilities.infrastructure.os_native_dispatcher import OsNativeDispatcher
    from hermes.mcp.infrastructure.mcp_surface_adapter import McpSurfaceAdapter
    from hermes.tasks.domain.ports import AgentStatePort

logger = logging.getLogger("hermes.capabilities.broker")

# Regex para detectar placeholders PII en parámetros (CTRL-14).
_PII_PATTERN: re.Pattern[str] = re.compile(r"<PII:[^>]+>")


class CapabilityBroker:
    """Implementación de CapabilityBrokerPort. Único choke-point effector.

    Args:
        registry:        CapabilityRegistryPort — resuelve tool_name a binding.
        consent_manager: ConsentManager — assert_active/use fail-closed.
        approval_gate:   ApprovalGatePort — register_pending/verify_token.
        dispatcher:      SurfaceAdapterDispatcher — replay por surface_kind.
        signer:          AuditHashChainSigner — firma cada AuditEntry.
        audit_repo:      SignedAuditRepositoryPort — persiste append-only.
        intent_log:      IntentLog — idempotencia (CTRL-11).
        anchor:          ExternalAnchorPort opcional (CTRL-8).
        access_scope_repo: AgentAccessScope repo opcional (Fase 2 Phase 4e) —
                     habilita el enrutamiento Enterprise (LOCAL vs ENTERPRISE)
                     de las aprobaciones HITL mediadas por el broker
                     (install_*/set_policy/skill_manage/MCP write). None
                     (default) => TODO queda LOCAL, comportamiento de hoy sin
                     cambio — ver `wire_access_scope`/`_resolve_enterprise_route`.
        tenant_id:   tenant_id del daemon, usado junto con access_scope_repo.
    """

    def __init__(
        self,
        *,
        registry: CapabilityRegistryPort,
        consent_manager: Any,  # ConsentManager — no es un Protocol formal
        approval_gate: ApprovalGatePort,
        dispatcher: SurfaceAdapterDispatcher,
        signer: AuditHashChainSigner,
        audit_repo: SignedAuditRepositoryPort,
        intent_log: IntentLog,
        anchor: ExternalAnchorPort | None = None,
        agent_state: AgentStatePort | None = None,
        os_native_dispatcher: OsNativeDispatcher | None = None,
        composio_adapter: ComposioSurfaceAdapter | None = None,
        mcp_adapter: McpSurfaceAdapter | None = None,
        install_executor: InstallExecutorPort | None = None,
        autonomous_default: bool = False,
        access_scope_repo: Any | None = None,
        tenant_id: str = "",
    ) -> None:
        self._registry = registry
        # FULL AUTÓNOMO por defecto (decisión del dueño, 2026-06-12): el Cerebro
        # puede hacer TODO sin gates de consent ni HITL. El mecanismo de gates SIGUE
        # existiendo (para que el dueño los CIERRE manualmente luego), pero por
        # defecto está ABIERTO. Lo que NUNCA se abre: el kill-switch (Paso 0) y la
        # denylist anti-autopirateo (Paso 1b) — ese es el suelo inapelable. Además
        # el audit firma TODO igual. El dueño capa con permisos por-agente/denylist.
        self._autonomous_default = autonomous_default
        self._consent_manager = consent_manager
        self._approval_gate = approval_gate
        self._dispatcher = dispatcher
        self._signer = signer
        self._audit_repo = audit_repo
        self._intent_log = intent_log
        self._anchor = anchor
        self._agent_state = agent_state
        self._os_native_dispatcher = os_native_dispatcher
        # KC-4: ComposioSurfaceAdapter para despachar Composio READ via broker.
        self._composio_adapter = composio_adapter
        # 013-P1: McpSurfaceAdapter para despachar tool calls MCP via broker.
        self._mcp_adapter = mcp_adapter
        # Install executor: search/install/connect tools (fail-closed when None).
        self._install_executor = install_executor
        # Enterprise approval routing (Fase 2 Phase 4e): optional, None by
        # default (Community / composition root hasn't wired it yet) => every
        # broker HITL registration stays LOCAL, zero regression. Wired for
        # real via `wire_access_scope` (two-step wiring — the repo is built
        # AFTER the broker at the composition root, see __main__.py).
        self._access_scope_repo = access_scope_repo
        self._tenant_id = tenant_id

    def wire_access_scope(self, *, access_scope_repo: Any, tenant_id: str) -> None:
        """Post-construction wiring for the Enterprise access-scope repo
        (Fase 2 Phase 4e) — mirrors the two-step wiring pattern already used
        for `os_native_dispatcher.wire_computer_use_broker` in `__main__.py`
        (the repo is built AFTER the broker in the boot sequence). Optional;
        skipping this call leaves `dispatch()`'s broker MFA-tier routing at
        LOCAL (today's behaviour, unchanged)."""
        self._access_scope_repo = access_scope_repo
        self._tenant_id = tenant_id

    def registered_surface_kinds(self) -> frozenset:
        """SurfaceKinds con adapter realmente registrado en el dispatcher.

        Verdad de terreno para que la capa de tool-specs no anuncie al LLM tools
        de surface_adapter inejecutables (advertise ⟺ executable). Read-only;
        no toca la secuencia de dispatch.
        """
        return self._dispatcher.registered_kinds()

    async def dispatch(  # noqa: PLR0911
        self,
        proposal: ToolCallProposal,
        consent_context: ConsentContext,
        *,
        hitl_approval_token: str | None = None,
        work_item_id: UUID | None = None,
        autonomy_level: AutonomyLevel | None = None,
        conversation_id: str = "",
    ) -> ExecutionOutcome:
        """Paso único al mundo — secuencia fail-closed de 8+1 pasos.

        Paso 0 (CTRL-12 / KILL-2): chequeo atómico de pausa ANTES de cualquier
        otro efecto. No cacheado — lee el estado real en cada llamada. Si
        pausado, devuelve REJECTED_BY_POLICY sin tocar el adapter ni el
        intent_log (idempotente ante re-intentos).
        """

        # ----------------------------------------------------------------
        # Paso 0: Kill-switch atómico — fail-closed si pausado (CTRL-12)
        # ----------------------------------------------------------------
        if self._agent_state is not None and await self._agent_state.is_paused():
            return ExecutionOutcome(
                proposal_id=proposal.proposal_id,
                status=ExecutionStatus.REJECTED_BY_POLICY,
                error="agent paused — dispatch blocked by kill-switch (CTRL-12)",
            )

        # ----------------------------------------------------------------
        # Paso 1: Resolver binding
        # ----------------------------------------------------------------
        binding = self._registry.resolve(proposal.tool_name)
        if binding is None:
            return await self._reject_by_policy(
                proposal, reason=f"tool_name={proposal.tool_name!r} no registrado"
            )

        # ----------------------------------------------------------------
        # Paso 1b: Denylist anti-autopirateo (CTRL-P2-2/3) — TERMINAL,
        # inapelable por HITL (NFR-002). Se evalúa ANTES de systemd y
        # ANTES de cualquier otro gate. Solo aplica a os_native.
        # ----------------------------------------------------------------
        denylist_outcome = await self._check_denylist(proposal, binding, consent_context)
        if denylist_outcome is not None:
            return denylist_outcome

        # ----------------------------------------------------------------
        # Paso 2: Clasificar riesgo efectivo
        # ----------------------------------------------------------------
        taint = ProvenanceTaint(
            derived_from_untrusted_content=consent_context.derived_from_untrusted_content
        )
        effective_risk = _compute_effective_risk(binding, taint, proposal)

        # ----------------------------------------------------------------
        # Paso 3: Consent — operator_id None ⇒ fail-closed (CTRL-13)
        # FULL AUTÓNOMO: se omite el gate de consent (el dueño lo abre por defecto;
        # capa luego). Kill-switch + denylist + audit siguen activos.
        # ----------------------------------------------------------------
        if not self._autonomous_default:
            consent_outcome = await self._run_consent_gate(proposal, consent_context, binding)
            if consent_outcome is not None:
                return consent_outcome

        # ----------------------------------------------------------------
        # Paso 4: HITL — HIGH o taint forzado ⇒ exige token (CTRL-1)
        # El autonomy_level del agente activo modula si LOW+no-auto requiere
        # HITL, pero NUNCA exime acciones HIGH (invariante de seguridad F-1).
        # V-1 (fix): el modo AUTÓNOMO solo relaja el caso LOW/reversible. HIGH y el
        # taint-forzado (CTRL-5, anti-inyección) — que _compute_effective_risk ya
        # eleva a HIGH — exigen HITL SIEMPRE, incluso en autónomo. Una orden
        # inyectada de borrar/exfiltrar/instalar NUNCA se auto-ejecuta sin el dueño.
        # ----------------------------------------------------------------
        if self._autonomous_default:
            needs_hitl = effective_risk is RiskLevel.HIGH
        else:
            needs_hitl = _needs_hitl(effective_risk, binding, autonomy_level)
        if needs_hitl:
            token_ok = await self._verify_hitl_token(
                proposal_id=proposal.proposal_id,
                token=hitl_approval_token,
            )
            if not token_ok:
                # Registrar en el buzón durable y devolver PENDING_APPROVAL.
                # work_item_id propagado desde el orquestador para trazabilidad —
                # y para que approve_action pueda re-encolar la tarea REAL tras la
                # aprobación (bug fix 2026-07: antes se perdía y quedaba en 0).
                resolved_work_item_id = work_item_id if work_item_id is not None else UUID(int=0)
                # Enterprise approval routing (Fase 2 Phase 4e — corrected):
                # mirrors security_hook._compute_danger_route via the SAME
                # shared resolver, so the native gate and the broker can never
                # diverge on WHO resolves a given tool's approval. Uses the
                # EXPLICIT consent_context.agent_id (NEVER a threading.local —
                # see ConsentContext.agent_id's docstring for the prior bug
                # this replaces: the broker coroutine runs on the event-loop
                # thread, a different OS thread than the one that stamps
                # conversation_task_registry's thread-locals).
                enterprise_route, agent_id, sensitivity_categories = (
                    self._resolve_enterprise_route(
                        tool_name=proposal.tool_name, args=proposal.parameters,
                        agent_id=consent_context.agent_id,
                    )
                )
                # Enterprise round-trip requires a non-empty action_digest —
                # the cloud's signed decision envelope always carries one, and
                # remote_approvals._extract_envelope fail-closed rejects ANY
                # empty field ("invalid_envelope"). A broker row otherwise
                # never sets one (dedup here is by proposal_id — see
                # register_pending's own NULL-vs-'' comment); compute it ONLY
                # when this row is actually going to Enterprise — a LOCAL
                # broker row is unaffected (action_digest stays NULL, exactly
                # as before this fix).
                action_digest = (
                    _compute_action_digest(proposal.tool_name, proposal.parameters)
                    if enterprise_route == "enterprise" else ""
                )
                pending_status = await self._approval_gate.register_pending(
                    proposal_id=proposal.proposal_id,
                    work_item_id=resolved_work_item_id,
                    consent_context=consent_context,
                    risk=effective_risk,
                    justification=proposal.justification,
                    parameters_redacted=proposal.parameters,
                    tool_name=proposal.tool_name,
                    action_digest=action_digest,
                    conversation_id=conversation_id,
                    route=enterprise_route,
                    sensitivity_categories=sensitivity_categories,
                    agent_id=agent_id,
                )
                # Durable breaker (2026-07): register_pending devuelve un status
                # distinto de 'pending' cuando la MISMA propuesta se re-registró
                # demasiadas veces sin resolución (re-encolados/re-reclamos en
                # bucle) — terminal, fail-closed. No re-anunciar como pendiente.
                if pending_status != "pending":
                    return await self._reject_by_policy(
                        proposal,
                        reason=(
                            f"'{proposal.tool_name}' bloqueado tras demasiados "
                            "reintentos sin aprobación del dueño (breaker durable) "
                            "— no se re-propone."
                        ),
                    )
                return ExecutionOutcome(
                    proposal_id=proposal.proposal_id,
                    status=ExecutionStatus.PENDING_APPROVAL,
                )

        # ----------------------------------------------------------------
        # Paso 5: Idempotencia (CTRL-11) — record_intent ANTES del efecto
        # ----------------------------------------------------------------
        idempotency_key = compute_idempotency_key(proposal)
        if self._intent_log.was_executed(idempotency_key):
            return ExecutionOutcome(
                proposal_id=proposal.proposal_id,
                status=ExecutionStatus.EXECUTED,
                result={"idempotent": True},
            )
        # RECON-1/I2: intent registrado pero sin outcome = crash previo.
        # El efecto puede haberse aplicado parcialmente — NO re-ejecutar.
        # Devolver FAILED para que el orquestador eleve la tarea a revisión humana.
        if self._intent_log.has_pending_intent(idempotency_key):
            logger.warning(
                "hermes.broker.pending_intent_detected: idempotency_key=%s — "
                "intent sin outcome (crash previo). NO re-ejecutando. Requiere revisión humana.",
                idempotency_key[:16],
            )
            return ExecutionOutcome(
                proposal_id=proposal.proposal_id,
                status=ExecutionStatus.FAILED,
                error="pending_intent_without_outcome — needs_human_review (RECON-1)",
            )
        self._intent_log.record_intent(
            idempotency_key, proposal, task_id=str(work_item_id) if work_item_id else None
        )

        # ----------------------------------------------------------------
        # Paso 6: Sintetizar CapturedAction — rehidratación PII solo aquí
        # ----------------------------------------------------------------
        action = _build_captured_action(
            proposal=proposal,
            binding=binding,
            tenant_id=consent_context.tenant_id,
            operator_id=consent_context.operator_id,
            work_item_id=work_item_id,
        )

        # ----------------------------------------------------------------
        # Paso 7: Dispatch — surface_adapter, os_native, o composio (KC-4)
        # ----------------------------------------------------------------
        executor_kind = getattr(binding, "executor", "surface_adapter")
        if executor_kind == "os_native":
            replay_outcome = await self._dispatch_os_native(proposal, action)
        elif executor_kind == "composio":
            replay_outcome = await self._dispatch_composio(proposal, action)
        elif executor_kind == "mcp":
            replay_outcome = await self._dispatch_mcp(proposal, action)
        elif executor_kind == "install":
            replay_outcome = await self._dispatch_install(proposal, action)
        else:
            try:
                replay_outcome = await self._dispatcher.replay(
                    action,
                    hitl_approval_token=hitl_approval_token,
                    consent_token=None,
                )
            except SurfaceAdapterNotFound as exc:
                return await self._reject_by_policy(proposal, reason=str(exc))

        # ----------------------------------------------------------------
        # Paso 8: Post-efecto — consent.use, record_outcome, audit (CTRL-9)
        # ----------------------------------------------------------------
        execution_status = _map_replay_status(replay_outcome.status)
        self._intent_log.record_outcome(
            idempotency_key,
            ExecutionOutcome(proposal_id=proposal.proposal_id, status=execution_status),
        )

        if execution_status == ExecutionStatus.EXECUTED and binding.required_capability:
            # assert_active ya pasó antes del replay; use puede fallar si era ONCE y expiró.
            # Suprimimos ConsentDenied intencionalmente — no es un error de negocio aquí.
            with contextlib.suppress(ConsentDenied):
                self._consent_manager.use(
                    human_operator_id=consent_context.operator_id,
                    capability=Capability(binding.required_capability),
                )

        audit_entry = await self._signer.append_and_persist(
            audit_kind=(
                AuditKind.PROPOSAL_EXECUTED
                if execution_status == ExecutionStatus.EXECUTED
                else AuditKind.PROPOSAL_REJECTED
            ),
            actor=str(consent_context.operator_id),
            description=_audit_description(proposal, replay_outcome),
            payload=_audit_payload(proposal, replay_outcome),
            tenant_id=consent_context.tenant_id,
            audit_repo=self._audit_repo,
        )

        return ExecutionOutcome(
            proposal_id=proposal.proposal_id,
            status=execution_status,
            audit_entry_id=audit_entry.entry_id,
            execution_head_hash=audit_entry.signed_payload_hash_hex,
            result=replay_outcome.result,
            error=replay_outcome.error,
            duration_ms=replay_outcome.duration_ms,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _check_denylist(
        self,
        proposal: ToolCallProposal,
        binding: object,
        consent_context: ConsentContext,
    ) -> ExecutionOutcome | None:
        """Paso 1b: verifica denylist anti-autopirateo para skills os_native.

        Solo actúa sobre bindings con executor='os_native' y skills de mutación
        de servicio. Retorna ExecutionOutcome(REJECTED_BY_POLICY) si el servicio
        está protegido. Terminal e inapelable (NFR-002/CTRL-P2-2).
        """
        if self._os_native_dispatcher is None:
            return None
        executor_kind = getattr(binding, "executor", "surface_adapter")
        if executor_kind != "os_native":
            return None

        unit = proposal.parameters.get("unit")
        if unit is None:
            return None  # no unit param — denylist doesn't apply

        # Only service-mutation ops trigger the denylist
        _service_ops = {"start_service", "stop_service", "restart_service"}
        if proposal.tool_name not in _service_ops:
            return None

        # Import denylist from the dispatcher (single source of truth)
        denylist = getattr(self._os_native_dispatcher, "_denylist", None)
        if denylist is None:
            return None

        # Use canonical identity resolution so real systemd aliases cannot bypass
        # the denylist (CONDITION-2). Fallback to lexical when systemd is absent.
        if denylist.is_protected_canonical(unit):
            reason = (
                f"REJECTED_BY_POLICY: operación '{proposal.tool_name}' sobre servicio "
                f"protegido '{unit}' rechazada — frenos del agente son inviolables "
                "(CTRL-P2-2/NFR-002). Terminal e inapelable por HITL."
            )
            return await self._reject_by_policy(proposal, reason=reason)
        return None

    async def _dispatch_composio(
        self,
        proposal: ToolCallProposal,
        action: CapturedAction,
    ) -> Any:
        """Paso 7 alternativo (KC-4): invoca ComposioSurfaceAdapter.replay.

        Requiere que composio_adapter esté inyectado. Si no lo está,
        falla con REJECTED_BY_POLICY (fail-closed — Constitución IV).

        El payload de la CapturedAction ya contiene {slug, params, entity_id}
        tal como lo armó _build_captured_action desde proposal.parameters.
        """
        from hermes.agents_os.domain.ports.surface_adapter_port import (  # noqa: PLC0415
            ReplayOutcome,
            ReplayStatus,
        )

        if self._composio_adapter is None:
            return ReplayOutcome(
                action_id=action.action_id,
                status=ReplayStatus.REJECTED_BY_POLICY,
                error="composio_adapter no configurado — fail-closed (KC-4)",
            )

        return await self._composio_adapter.replay(action)

    async def _dispatch_mcp(
        self,
        proposal: ToolCallProposal,
        action: CapturedAction,
    ) -> Any:
        """Paso 7 alternativo (013-P1): invoca McpSurfaceAdapter.replay.

        Requiere que mcp_adapter esté inyectado. Si no lo está,
        falla con REJECTED_BY_POLICY (fail-closed — Constitución IV).

        La cadena de gates (kill-switch, resolve, taint, consent, HITL,
        idempotencia, captured-action, audit) se reutiliza VERBATIM — cero
        cambio de contrato del broker (plan.md §Arquitectura seam 4).
        """
        from hermes.agents_os.domain.ports.surface_adapter_port import (  # noqa: PLC0415
            ReplayOutcome,
            ReplayStatus,
        )

        if self._mcp_adapter is None:
            return ReplayOutcome(
                action_id=action.action_id,
                status=ReplayStatus.REJECTED_BY_POLICY,
                error="mcp_adapter no configurado — fail-closed (013-P1)",
            )

        return await self._mcp_adapter.replay(action)

    async def _dispatch_install(
        self,
        proposal: ToolCallProposal,
        action: CapturedAction,
    ) -> Any:
        """Paso 7 alternativo: invoca InstallExecutorPort.execute.

        Requiere que install_executor esté inyectado. Sin él, falla con
        REJECTED_BY_POLICY (fail-closed — Constitución IV).

        Cubre search_mcp/search_skills/search_apps (LOW/auto) e
        install_mcp/install_skill/install_app/connect_integration (HIGH/HITL).
        El scan de seguridad ocurre DENTRO de las funciones del wiring; este
        método NUNCA lo puentea.
        """
        from hermes.agents_os.domain.ports.surface_adapter_port import (  # noqa: PLC0415
            ReplayOutcome,
            ReplayStatus,
        )

        if self._install_executor is None:
            return ReplayOutcome(
                action_id=action.action_id,
                status=ReplayStatus.REJECTED_BY_POLICY,
                error="install_executor no configurado — fail-closed (install branch)",
            )

        return await self._install_executor.execute(proposal, action)

    async def _dispatch_os_native(
        self,
        proposal: ToolCallProposal,
        action: CapturedAction,
    ) -> Any:
        """Paso 7 alternativo: invoca el executor nativo (CTRL-P2-1).

        Requiere que os_native_dispatcher esté inyectado. Si no lo está,
        falla con REJECTED_BY_POLICY (fail-closed — Constitución IV).

        El OsNativeDispatcher aplica internamente la denylist anti-autopirateo
        ANTES de llamar a systemd (CTRL-P2-2/3).
        """
        from hermes.agents_os.domain.ports.surface_adapter_port import ReplayOutcome, ReplayStatus  # noqa: PLC0415

        if self._os_native_dispatcher is None:
            return ReplayOutcome(
                action_id=action.action_id,
                status=ReplayStatus.REJECTED_BY_POLICY,
                error="os_native_dispatcher no configurado — fail-closed (CTRL-P2-1)",
            )

        result = await self._os_native_dispatcher.execute(
            skill_name=proposal.tool_name,
            args=proposal.parameters,
        )

        # Denylist rejection surfaces as REJECTED_BY_POLICY (CTRL-P2-2)
        if not result.get("ok", False) and "REJECTED_BY_POLICY" in str(result.get("reason", "")):
            return ReplayOutcome(
                action_id=action.action_id,
                status=ReplayStatus.REJECTED_BY_POLICY,
                error=result.get("reason"),
            )

        status = ReplayStatus.EXECUTED_OK if result.get("ok", False) else ReplayStatus.EXECUTED_FAILED
        return ReplayOutcome(
            action_id=action.action_id,
            status=status,
            result=result,
            error=result.get("reason") if not result.get("ok", False) else None,
        )

    async def _run_consent_gate(
        self,
        proposal: ToolCallProposal,
        consent_context: ConsentContext,
        binding: object,
    ) -> ExecutionOutcome | None:
        """Paso 3: valida operator_id, consent activo y persistent_forbidden (CTRL-2/3/13).

        Returns ExecutionOutcome si rechazado, None si puede continuar.
        """
        if consent_context.operator_id is None:
            return _rejected_by_consent(
                proposal, reason="operator_id ausente — fail-closed (CTRL-13)"
            )
        if binding.required_capability is None:  # type: ignore[union-attr]
            return None
        persistent_forbidden = getattr(binding, "persistent_forbidden", False)
        try:
            capability = Capability(binding.required_capability)  # type: ignore[union-attr]
        except ValueError as exc:
            return _rejected_by_consent(proposal, reason=f"capability inválida: {exc}")

        try:
            active_consent = self._consent_manager.assert_active(
                human_operator_id=consent_context.operator_id,
                capability=capability,
            )
        except ConsentDenied as exc:
            # CTRL-3 fix: los tools persistent_forbidden (computer-use,
            # begin_computer_use) NO se pre-conceden por consent — el consent de
            # SESIÓN lo acuña la tarjeta HITL ámbar del gate de aprobación (que
            # corre DESPUÉS de este gate). Si rechazáramos aquí por "sin consent",
            # la tarjeta ámbar nunca se dispararía (chicken-and-egg) y el agente
            # jamás podría operar la pantalla. Dejamos pasar: la verja real sigue
            # siendo el token HITL (HIGH/no-auto exige aprobación humana abajo).
            if persistent_forbidden:
                return None
            await self._audit_rejected(
                proposal=proposal,
                consent_context=consent_context,
                reason=str(exc),
            )
            return _rejected_by_consent(proposal, reason=str(exc))

        if persistent_forbidden and active_consent.scope == ConsentScope.PERSISTENT:
            # Hay un consent PERSISTENT pero este tool lo prohíbe (CTRL-3): NO vale
            # como auto-grant. En vez de RECHAZAR (lo que bloqueaba abrir apps con
            # los permisos por defecto), dejamos pasar al gate HITL para exigir una
            # aprobación de SESIÓN fresca (tarjeta ámbar). CTRL-3 se preserva: el
            # gate HITL de abajo sigue exigiendo el token de aprobación humana.
            return None
        return None

    async def _reject_by_policy(
        self, proposal: ToolCallProposal, *, reason: str
    ) -> ExecutionOutcome:
        entry = await self._signer.append_and_persist(
            audit_kind=AuditKind.PROPOSAL_REJECTED,
            actor="broker",
            description=f"REJECTED_BY_POLICY: {reason}",
            payload={"proposal_id": str(proposal.proposal_id), "tool_name": proposal.tool_name},
            audit_repo=self._audit_repo,
        )
        return ExecutionOutcome(
            proposal_id=proposal.proposal_id,
            status=ExecutionStatus.REJECTED_BY_POLICY,
            error=reason,
        )

    async def _audit_rejected(
        self,
        *,
        proposal: ToolCallProposal,
        consent_context: ConsentContext,
        reason: str,
    ) -> None:
        await self._signer.append_and_persist(
            audit_kind=AuditKind.PROPOSAL_REJECTED,
            actor=str(consent_context.operator_id),
            description=f"REJECTED_BY_CONSENT: {reason}",
            payload={"proposal_id": str(proposal.proposal_id)},
            tenant_id=consent_context.tenant_id,
            audit_repo=self._audit_repo,
        )

    async def _verify_hitl_token(
        self, *, proposal_id: UUID, token: str | None
    ) -> bool:
        """Verifica el token HITL via approval_gate (criptográfico, single-use).

        Fail-closed: False si token es None o vacío.
        """
        if not token:
            return False
        return await self._approval_gate.verify_token(
            proposal_id=proposal_id, token=token
        )

    def _resolve_enterprise_route(
        self, *, tool_name: str, args: dict[str, Any], agent_id: str,
    ) -> tuple[str, str, frozenset]:
        """Resolve the Enterprise approval route for a broker HITL proposal
        (Fase 2 Phase 4e). Calls the SAME `enterprise_approval_routing.
        resolve_route_and_context` that `hermes.runtime.security_hook.
        _compute_danger_route` calls — the native-danger gate and the broker
        can never diverge on WHO resolves a given tool's approval.

        `agent_id` MUST be the EXPLICIT identity resolved on the cycle's
        event-loop thread (`ConsentContext.agent_id` — see its docstring) —
        NEVER `conversation_task_registry.get_current_cycle_agent()`, a
        threading.local stamped on a DIFFERENT OS thread (the executor thread)
        than the one this coroutine runs on (bridged via
        run_coroutine_threadsafe). Reading that thread-local here always
        resolved "" in the real daemon — the root cause of the prior, reverted
        attempt's dead routing (Fase 2 Phase 4d).

        `access_scope_repo=None` (Community / composition root hasn't wired
        `wire_access_scope` yet) => LOCAL, unchanged from today (zero
        regression). Fail-closed: ANY resolution error also degrades to LOCAL
        (never auto-executes, mirrors `_compute_danger_route`'s own
        fail-soft-to-LOCAL discipline).

        Returns (route_str, agent_id, sensitivity_categories) matching
        `ApprovalGatePort.register_pending`'s wire shape — "" (LOCAL) or
        "enterprise", masked to ""/frozenset() for LOCAL exactly like
        `security_hook._resolve_native_danger_approval` masks its own call.
        """
        if self._access_scope_repo is None:
            return "", "", frozenset()
        try:
            from hermes.capabilities.approval_router import ApprovalRoute  # noqa: PLC0415
            from hermes.capabilities.infrastructure.enterprise_approval_routing import (  # noqa: PLC0415
                resolve_route_and_context,
            )

            resolved_route, categories = resolve_route_and_context(
                tool_name=tool_name, args=args,
                access_scope_repo=self._access_scope_repo, tenant_id=self._tenant_id,
                agent_id=agent_id,
            )
            is_enterprise = resolved_route is ApprovalRoute.ENTERPRISE
            return (
                "enterprise" if is_enterprise else "",
                agent_id if is_enterprise else "",
                categories if is_enterprise else frozenset(),
            )
        except Exception as exc:  # noqa: BLE001 — fail-closed: never widen/skip the gate
            logger.warning(
                "hermes.capabilities.broker.enterprise_route_resolve_failed tool=%s "
                "error=%r — falling back to LOCAL",
                tool_name,
                exc,
            )
            return "", "", frozenset()


# Satisface CapabilityBrokerPort structural check.
assert isinstance(CapabilityBroker, type)


# ---------------------------------------------------------------------------
# Helpers puros (sin efectos laterales)
# ---------------------------------------------------------------------------


def _compute_action_digest(tool_name: str, parameters: dict[str, Any]) -> str:
    """Deterministic digest of (tool_name, parameters) — SAME formula as
    `security_hook._resolve_native_danger_approval` / `nous_engine.
    _deterministic_proposal_id` (sha256 of tool_name + canonical JSON params),
    kept byte-identical so a native and a broker occurrence of the SAME
    action collide on the SAME digest, matching the existing native-path
    semantics. Deliberately inlined here rather than extracted into a shared
    module (out of scope for this change) — three short, stable call sites of
    one formula is not a drift risk worth a premature cross-module
    abstraction; the value is only ever a dedup/cross-check key for the
    Enterprise round-trip, never security-decision-bearing on its own.
    """
    import hashlib
    import json as _json

    digest_input = tool_name + "\x00" + _json.dumps(parameters, sort_keys=True, default=str)
    return hashlib.sha256(digest_input.encode("utf-8", "replace")).hexdigest()


def _compute_effective_risk(
    binding: object,
    taint: ProvenanceTaint,
    proposal: ToolCallProposal,
) -> RiskLevel:
    """Determina el riesgo efectivo de la propuesta.

    Eleva a HIGH si:
    - requires_forced_hitl(taint, binding) (CTRL-5).
    - Es ApiCall con campos PII en parámetros (CTRL-14).
    - Es TERMINAL y el comando NO está en la allow-list (CTRL-6/BROKER-8).
      En P0 TERMINAL es siempre HIGH/HITL, pero la allow-list es el gate
      correcto — si en el futuro un binding TERMINAL llega con auto_executable,
      pasa por aquí y queda clasificado correctamente.
    """
    base_risk: RiskLevel = binding.risk  # type: ignore[attr-defined]
    if requires_forced_hitl(taint, binding):  # type: ignore[arg-type]
        return RiskLevel.HIGH
    # Fix-3 (CTRL-5 / TOP-1): bajo taint, read_file de rutas sensibles → HITL.
    if is_sensitive_path_read_under_taint(taint, proposal.tool_name, proposal.parameters):
        return RiskLevel.HIGH
    if _is_api_call_with_pii(binding, proposal):
        return RiskLevel.HIGH
    if _is_terminal_not_allowlisted(binding, proposal):
        return RiskLevel.HIGH
    return base_risk


def _is_terminal_not_allowlisted(binding: object, proposal: ToolCallProposal) -> bool:
    """True si es TERMINAL y el comando NO pasa la allow-list (CTRL-6/BROKER-8).

    Consulta is_terminal_command_allowlisted con el argv del parámetro `command`.
    Si el parámetro no existe o no es lista, fail-closed (HIGH).
    """
    surface_kind = getattr(binding, "surface_kind", None)
    if surface_kind != SurfaceKind.TERMINAL:
        return False
    argv = proposal.parameters.get("command") or proposal.parameters.get("argv")
    if not isinstance(argv, list):
        return True  # No hay argv válido — fail-closed
    return not is_terminal_command_allowlisted(argv)


def _is_api_call_with_pii(binding: object, proposal: ToolCallProposal) -> bool:
    """True si es ApiCall y los parámetros contienen placeholders PII (CTRL-14)."""
    surface_kind = getattr(binding, "surface_kind", None)
    if surface_kind != SurfaceKind.API_CALL:
        return False
    return _parameters_contain_pii(proposal.parameters)


def _parameters_contain_pii(params: dict[str, Any]) -> bool:
    """True si algún valor del dict (recursivo) contiene un placeholder PII."""
    for value in params.values():
        if isinstance(value, str) and _PII_PATTERN.search(value):
            return True
        if isinstance(value, dict) and _parameters_contain_pii(value):
            return True
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str) and _PII_PATTERN.search(item):
                    return True
    return False


def _needs_hitl(
    effective_risk: RiskLevel,
    binding: object,
    autonomy_level: AutonomyLevel | None = None,
) -> bool:
    """Determina si la propuesta requiere token HITL según riesgo y nivel de autonomía.

    Invariante de seguridad (F-1, inapelable por cualquier nivel):
      HIGH siempre requiere HITL. El autonomy_level NUNCA puede eximir HIGH.

    Semántica por nivel (C2 — relajación acotada a lo explícitamente reversible):

      | effective_risk | auto_executable | reversible | ASK_ALWAYS | BALANCED | AUTONOMOUS |
      |----------------|-----------------|------------|------------|----------|------------|
      | HIGH           | any             | any        | HITL       | HITL     | HITL       |
      | LOW            | True            | any        | no HITL    | no HITL  | no HITL    |
      | LOW            | False           | False      | HITL       | HITL     | HITL       |
      | LOW            | False           | True       | HITL       | HITL     | no HITL    |

    La relajación AUTONOMOUS se acota SOLO a LOW+reversible=True. Ninguna binding
    tiene reversible=True hoy (el test C3 lo impone), así que AUTONOMOUS == BALANCED
    en el catálogo actual — de forma explícita y segura, no por casualidad.
    """
    # Invariante de seguridad: HIGH siempre exige HITL, sin excepción (F-1).
    if effective_risk is RiskLevel.HIGH:
        return True

    level = autonomy_level if autonomy_level is not None else AutonomyLevel.BALANCED
    auto_executable = getattr(binding, "auto_executable", False)

    # LOW + auto_executable: lectura pura — sin HITL en todos los niveles.
    if auto_executable:
        return False

    # LOW + no-auto: ASK_ALWAYS y BALANCED siempre exigen HITL.
    if level is not AutonomyLevel.AUTONOMOUS:
        return True

    # AUTONOMOUS + LOW + no-auto: relaja SOLO si la binding es explícitamente reversible.
    # reversible=True significa sin efecto externo, sin red, sin credenciales, deshacible.
    # El test C3 obliga a revisión de security-engineer antes de marcar cualquier binding
    # como reversible, de modo que esta relajación nunca ocurre silenciosamente.
    reversible = getattr(binding, "reversible", False)
    return not reversible


def _build_captured_action(
    *,
    proposal: ToolCallProposal,
    binding: object,
    tenant_id: UUID,
    operator_id: UUID | None,
    work_item_id: UUID | None = None,
) -> CapturedAction:
    """Sintetiza CapturedAction desde proposal.parameters.

    La rehidratación de PII ocurre aquí (lo más tarde posible — CTRL-14).
    El payload NUNCA se envía a logs.
    """
    surface_kind: SurfaceKind = getattr(binding, "surface_kind", SurfaceKind.FILESYSTEM)
    return CapturedAction(
        action_id=uuid4(),
        surface_kind=surface_kind,
        intent_desc=proposal.justification,
        payload=proposal.parameters,  # rehidratación PII: el adapter la gestiona
        tenant_id=tenant_id,
        human_operator_id=operator_id,
        work_item_id=work_item_id,
    )


def _map_replay_status(status: ReplayStatus) -> ExecutionStatus:
    """Mapea ReplayStatus del adapter → ExecutionStatus del broker."""
    _MAPPING: dict[ReplayStatus, ExecutionStatus] = {
        ReplayStatus.EXECUTED_OK: ExecutionStatus.EXECUTED,
        ReplayStatus.EXECUTED_FAILED: ExecutionStatus.FAILED,
        ReplayStatus.HITL_REQUIRED: ExecutionStatus.PENDING_APPROVAL,
        ReplayStatus.REJECTED_BY_CONSENT: ExecutionStatus.REJECTED_BY_CONSENT,
        ReplayStatus.REJECTED_BY_POLICY: ExecutionStatus.REJECTED_BY_POLICY,
    }
    return _MAPPING.get(status, ExecutionStatus.FAILED)


def _audit_description(proposal: ToolCallProposal, replay_outcome: Any) -> str:
    """Descripción del audit desde el ReplayOutcome real, no del narrative (CTRL-9)."""
    status = getattr(replay_outcome, "status", "unknown")
    return f"{proposal.tool_name} → {status}"


def _audit_payload(proposal: ToolCallProposal, replay_outcome: Any) -> dict[str, Any]:
    """Payload del audit desde el ReplayOutcome real (CTRL-9). Sin PII."""
    return {
        "proposal_id": str(proposal.proposal_id),
        "tool_name": proposal.tool_name,
        "replay_status": str(getattr(replay_outcome, "status", "unknown")),
        "duration_ms": getattr(replay_outcome, "duration_ms", 0),
    }


def _rejected_by_consent(
    proposal: ToolCallProposal, *, reason: str
) -> ExecutionOutcome:
    return ExecutionOutcome(
        proposal_id=proposal.proposal_id,
        status=ExecutionStatus.REJECTED_BY_CONSENT,
        error=reason,
    )
