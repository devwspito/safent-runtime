"""Puertos del bounded context `capabilities` — el broker/effector.

ÚNICO choke-point del agente con el mundo (FR-014, spec §Riesgos). NO redefine
SurfaceAdapterPort (Constitución I) — lo CONSUME como dependencia. NO redefine
ConsentManager ni AuditHashChainSigner — los orquesta.

Capa: capabilities/domain define ExecutionOutcome + RiskLevel + los Protocols.
capabilities/application implementa CapabilityBroker. capabilities/infrastructure
implementa el ApprovalGate (SQLite pending_approvals) y el AuditRepository.

Constitución II: HIGH jamás se ejecuta sin hitl_approval_token VÁLIDO (verificable,
no presence-check — ver threat-model CTRL-1).
Constitución IV: fail-closed — tool desconocido, consent ausente, operador
ausente, token inválido -> NO ejecuta.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from uuid import UUID

if TYPE_CHECKING:
    # Dependencias EXISTENTES — se muestran como tipos, NO se redefinen aquí.
    from hermes.agents.domain.agent import AutonomyLevel
    from hermes.agents_os.domain.ports.surface_adapter_port import (
        SurfaceAdapterPort,  # noqa: F401
    )
    from hermes.agents_os.domain.surface_kind import SurfaceKind
    from hermes.domain.proposal import ToolCallProposal


# ---------------------------------------------------------------------------
# Domain value objects / enums
# ---------------------------------------------------------------------------


class RiskLevel(StrEnum):
    """Riesgo de una propuesta — lo fija el CapabilityRegistry server-side,
    NUNCA el LLM (anti prompt-injection). HIGH exige HITL (Constitución II).
    """

    LOW = "low"    # read / write reversible interno -> consent, sin HITL
    HIGH = "high"  # irreversible / externa / system -> consent + HITL obligatorio


class ExecutionStatus(StrEnum):
    """Resultado del paso al mundo. Mapea 1:1 desde ReplayStatus del adapter,
    más los rechazos previos al adapter (broker fail-closed).
    """

    EXECUTED = "executed"
    FAILED = "failed"
    PENDING_APPROVAL = "pending_approval"      # HIGH sin token válido
    REJECTED_BY_CONSENT = "rejected_by_consent"
    REJECTED_BY_POLICY = "rejected_by_policy"  # tool desconocido / política


@dataclass(frozen=True, slots=True)
class CapabilityBinding:
    """Resolución estática de un tool_name a su perfil de seguridad.
    Tabla declarativa revisada por security-engineer (fuente de verdad de
    "qué es HIGH"). `executor` distingue surface-adapter de os-native graphical.

    `reversible` es una decisión de seguridad deliberada (C2 / F-1):
      - True SOLO si la acción es estrictamente interna al proceso del agente,
        sin efecto externo (red, credenciales, filesystem observable), y
        completamente deshacible sin rastro en el SO.
      - Marcar reversible=True abre la relajación AUTONOMOUS para LOW+no-auto.
        Requiere revisión explícita de security-engineer; el test C3 lo hace
        fallar si no está en la allow-list aprobada.
      - Default False (conservador / fail-closed).
    """

    tool_name: str
    surface_kind: SurfaceKind | None          # None si executor == "os_native"
    required_capability: str | None             # Capability.value, o None si READ_ONLY puro
    risk: RiskLevel
    auto_executable: bool = False               # threat-model CTRL-4: allow-list
    executor: str = "surface_adapter"           # "surface_adapter" | "os_native"
    reversible: bool = False                    # C2: relajación AUTONOMOUS acotada; ver docstring


@dataclass(frozen=True, slots=True)
class ConsentContext:
    """Identidad bajo cuyo consent opera el agente (HERMES_OPERATOR_ID).
    operator_id None -> fail-closed: toda capability con consent se deniega.
    """

    tenant_id: UUID
    operator_id: UUID | None
    derived_from_untrusted_content: bool = False  # threat-model CTRL-5 (taint)


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    """Resultado de dispatch. `audit_entry_id` es la EVIDENCIA real exigida por
    SC-001: mark_completed lo requiere. Inmutable.
    """

    proposal_id: UUID
    status: ExecutionStatus
    audit_entry_id: UUID | None = None         # presente si hubo ejecución real auditada
    execution_head_hash: str | None = None     # signed_payload_hash_hex del audit de ejecución
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    duration_ms: int = 0

    @property
    def is_real_execution(self) -> bool:
        """SC-001: solo EXECUTED con audit_entry_id cuenta como acción real."""
        return self.status is ExecutionStatus.EXECUTED and self.audit_entry_id is not None


# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------


@runtime_checkable
class CapabilityBrokerPort(Protocol):
    """El effector. ÚNICO punto por el que el agente actúa sobre el SO (FR-014).

    dispatch() es la secuencia fail-closed completa:
      resolver(tool_name)->binding | clasificar riesgo + procedencia |
      consent.assert_active + use | HIGH o untrusted -> exige hitl token VÁLIDO |
      sintetiza CapturedAction(payload=proposal.parameters) | selecciona adapter
      por surface_kind | adapter.replay(...) | firma+persiste audit con la ACCIÓN
      REAL | mapea ReplayOutcome->ExecutionOutcome.
    """

    async def dispatch(
        self,
        proposal: ToolCallProposal,
        consent_context: ConsentContext,
        *,
        hitl_approval_token: str | None = None,
        autonomy_level: AutonomyLevel | None = None,
    ) -> ExecutionOutcome:
        ...


@runtime_checkable
class ApprovalGatePort(Protocol):
    """Buzón durable de aprobaciones HIGH (tabla pending_approvals).

    P0 sin UI nueva: el broker registra el HIGH pendiente; el operador resuelve
    por la API de supervisión EXISTENTE (transporte de supervisión, NO dispara
    run_cycle -> NFR-001). La aprobación emite un approval_token VERIFICABLE
    (firmado, ligado a proposal_id, single-use — threat-model CTRL-1).
    """

    async def register_pending(
        self,
        *,
        proposal_id: UUID,
        work_item_id: UUID,
        consent_context: ConsentContext,
        risk: RiskLevel,
        justification: str,
        parameters_redacted: dict[str, Any],
        tool_name: str = "",
        action_digest: str = "",
        conversation_id: str = "",
        route: str = "",
        sensitivity_categories: frozenset[str] = frozenset(),
        agent_id: str = "",
    ) -> str:
        """Crea/actualiza la fila pending (idempotente por proposal_id).

        `conversation_id` ancla la tarjeta al hilo de chat para que el widget
        in-chat la muestre; `tool_name`/`action_digest` dan contexto y dedup.

        `route` (Fase 2 Phase 4b): "enterprise" cuando
        `capabilities.approval_router.route()` enrutó ESTA acción a un
        aprobador remoto de Enterprise; "" (default) = LOCAL, sin cambio de
        comportamiento. `sensitivity_categories`/`agent_id` acompañan una fila
        "enterprise" (contexto para el aprobador remoto + el push loop de
        `hermes.config_sync.remote_approvals`); ignorados para LOCAL.

        Returns:
            'pending' en el caso normal. Un valor distinto (p.ej. 'rejected')
            señala que un breaker durable (demasiados re-registros de la MISMA
            propuesta sin resolución) la bloqueó terminalmente — el caller debe
            tratarlo como REJECTED_BY_POLICY, no como PENDING_APPROVAL.
        """
        ...

    async def verify_token(self, *, proposal_id: UUID, token: str) -> bool:
        """Valida criptográficamente el token contra ESA proposal (no presence-
        check): firma HMAC, ligado a proposal_id, no expirado, single-use.
        Fail-closed: False ante cualquier duda. threat-model CTRL-1.
        """
        ...

    async def approved_token_for(self, proposal_id: UUID) -> str | None:
        """Devuelve el approval_token si la propuesta fue APROBADA y no expiró;
        None en cualquier otro caso (fail-closed). El broker re-dispatcha con él.
        """
        ...

    async def approve(
        self, *, proposal_id: UUID, approved_by: UUID, mfa_factors: Any | None = None
    ) -> str:
        """Aprobación humana: genera approval_token firmado, marca approved,
        registra quién aprobó (SC-004). Lo invoca la API de supervisión.

        `mfa_factors` se verifica AQUÍ (el gate es el único punto de enforcement MFA
        en toda superficie — red-team 2026-06-19, finding 3). Fail-closed sin factores.
        """
        ...

    async def reject(self, *, proposal_id: UUID, rejected_by: UUID, reason: str) -> None:
        """Rechazo humano: la propuesta nunca se ejecuta; la tarea va a REJECTED."""
        ...

    async def work_item_id_for_proposal(self, proposal_id: UUID) -> UUID | None:
        """Devuelve el work_item_id asociado a una proposal, o None si no existe.

        Permite a approve_action recuperar el work_item_id sin que el llamador
        lo tenga que conocer (el gate es el registry proposal→work_item).
        """
        ...


@runtime_checkable
class CapabilityRegistryPort(Protocol):
    """Resuelve tool_name -> CapabilityBinding. Fuente de verdad server-side del
    riesgo y la capability. Desconocido -> None (broker fail-closed: rechaza).
    """

    def resolve(self, tool_name: str) -> CapabilityBinding | None:
        ...


@runtime_checkable
class SignedAuditRepositoryPort(Protocol):
    """Persiste las AuditEntry firmadas (el firmer NO persiste). Append-only,
    verificable (FR-019, SC-006). Provee el head_hash para sembrar el firmer
    tras reinicio (continuidad de la cadena).
    """

    async def append(self, entry: Any) -> None:
        """Persiste una AuditEntry (hermes.agents_os...AuditEntry) append-only."""
        ...

    async def head_hash_hex(self) -> str | None:
        """Hash de la última entrada persistida — siembra _last_hash del firmer
        al arrancar para no romper la cadena en el boundary de reinicio.
        """
        ...

    async def load_chain(self, *, tenant_id: UUID | None = None) -> list[Any]:
        """Carga la cadena ordenada para verify_chain (observabilidad, SC-006)."""
        ...
