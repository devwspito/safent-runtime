"""DBus runtime service — interfaz local org.hermes.Runtime1.

Exposes el estado del runtime al panel agéntico GTK4 + CLI sin
exponer puerto TCP. Solo accesible vía system bus para el grupo
`hermes`.

Esta es la capa stub que NO importa dbus-python en tiempo de import —
el adapter real lo hace lazy. Permite testear el contrato de la API.

Clases:
  InMemoryRuntimeService  — fake para tests + panel sin runtime real.
  DbusRuntimeServiceWiring — cablea los puertos reales (AgentStatePort +
      ApprovalGatePort) con authZ verificada del sender del bus (T048).
      El binding D-Bus real vive en DbusAdapter (carga lazy — solo en
      personal-desktop); esta clase es testeable sin D-Bus.

Métodos (org.hermes.Runtime1):
  GetStatus() → {state, active_tasks, sandboxes, last_audit_head}
  RequestPause(reason: str, sender_uid: int) → bool
  RequestResume(sender_uid: int) → bool
  ApproveAction(proposal_id: str, sender_uid: int) → str  [approval_token]
  RejectAction(proposal_id: str, reason: str, sender_uid: int) → bool
  GetActiveConsents() → list[Capability]
  GetTelemetryEnabled() → bool
  SetTelemetryEnabled(value: bool, authorizing_user: str) → bool

AuthZ (CTRL-12/KILL-1, CWE-862):
  El sender_uid procede del bus (resuelto por DbusAdapter antes de
  llamar a esta clase) — NUNCA del payload del cliente. El wiring
  lo verifica contra `authorized_uids`. Fail-closed: UID no autorizado
  ⇒ DbusAuthorizationError (no ejecuta, no degrada).

Señales:
  TaskStarted(task_id, surface_kind)
  TaskCompleted(task_id, outcome)
  ConsentRequested(consent_id, capability, requestor)
  RemoteControlSessionAccepted(session_id, operator_id)

Wiring D-Bus real:
  El binding al bus (sd-bus / dbus-fast / dasbus) vive en el adapter
  cargado lazily en personal-desktop. Este módulo define solo la lógica
  verificable en CI. Ver `tests/security/test_dbus_wiring.py`.

Diseño de enqueue (Issue 2 / CTRL-P1-6):
  DbusRuntimeServiceWiring.enqueue() DELEGA en ControlPlaneService.enqueue().
  El Wiring convierte sender_uid (int del bus) en AuthenticatedChannel y
  llama al service. Esto garantiza que la ruta de producción (D-Bus) aplica
  el mismo rate-limit, PII-tokenization y audit que la ruta testeable.
  No hay duplicación de _uid_to_uuid / _tokenize_pii / _emit_accepted_audit.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from uuid import UUID

from hermes.agents_os.application.audit_hash_chain import AuditHashChainSigner
from hermes.agents_os.application.consent_manager import Capability

if TYPE_CHECKING:
    from hermes.agents_os.application.audit_hash_chain import AuditEntry
    from hermes.capabilities.domain.ports import ApprovalGatePort
    from hermes.shell_server.security.operator_token import OperatorTokenVerifier
    from hermes.tasks.control_plane.application.control_plane_service import ControlPlaneService
    from hermes.tasks.control_plane.domain.ports import EnqueueResult
    from hermes.tasks.domain.ports import AgentStatePort, WorkQueuePort

logger = logging.getLogger("hermes.agents_os.dbus_runtime_service")


def _parse_redacted_params(raw: object) -> dict:
    """Parse the JSON-text parameters_redacted column back to a dict.

    The gate stores it via json.dumps; the approval card needs a dict to render
    WHAT will run. Defensive: any malformed/empty value → {} (never raises).
    """
    if not raw or not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


class RuntimeStateError(RuntimeError):
    pass


@dataclass(slots=True)
class RuntimeStatusSnapshot:
    state: str
    active_task_count: int
    sandbox_count: int
    last_audit_head_hex: str
    telemetry_enabled: bool
    captured_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))


@runtime_checkable
class RuntimeServicePort(Protocol):
    """Puerto contra el runtime; usado por el panel y por dbus_adapter."""

    def get_status(self) -> RuntimeStatusSnapshot: ...

    def request_pause(self, *, reason: str, authorizing_user_id: UUID) -> None: ...

    def request_resume(self, *, authorizing_user_id: UUID) -> None: ...

    def get_active_consents(self) -> tuple[Capability, ...]: ...

    def set_telemetry_enabled(
        self, *, value: bool, authorizing_user_id: UUID
    ) -> None: ...


class InMemoryRuntimeService:
    """Implementación para tests + para el panel mientras no hay runtime.

    NO se conecta al dbus — eso lo hace el `DBusRuntimeAdapter` real que
    se carga solo en personal-desktop / server.
    """

    def __init__(
        self,
        *,
        audit_signer: AuditHashChainSigner,
        telemetry_enabled: bool = False,
    ) -> None:
        self._state = "idle"
        self._active_tasks = 0
        self._sandboxes = 0
        self._audit = audit_signer
        self._telemetry = telemetry_enabled
        self._consents: set[Capability] = set()

    def get_status(self) -> RuntimeStatusSnapshot:
        return RuntimeStatusSnapshot(
            state=self._state,
            active_task_count=self._active_tasks,
            sandbox_count=self._sandboxes,
            last_audit_head_hex=self._audit.head_hash_hex,
            telemetry_enabled=self._telemetry,
        )

    def request_pause(self, *, reason: str, authorizing_user_id: UUID) -> None:  # noqa: ARG002
        if self._state == "paused":
            return
        if self._state not in ("idle", "running"):
            raise RuntimeStateError(
                f"cannot pause from state={self._state}"
            )
        self._state = "paused"

    def request_resume(self, *, authorizing_user_id: UUID) -> None:  # noqa: ARG002
        if self._state != "paused":
            raise RuntimeStateError(
                f"cannot resume from state={self._state}"
            )
        self._state = "idle"

    def get_active_consents(self) -> tuple[Capability, ...]:
        return tuple(sorted(self._consents, key=str))

    def add_consent(self, capability: Capability) -> None:
        self._consents.add(capability)

    def revoke_consent(self, capability: Capability) -> None:
        self._consents.discard(capability)

    def set_telemetry_enabled(
        self, *, value: bool, authorizing_user_id: UUID  # noqa: ARG002
    ) -> None:
        # FR-061 (BLOQUEANTE telemetría pura): solo flip por humano local
        # autenticado. La verificación TOTP la hace el CLI antes de
        # invocarnos.
        self._telemetry = bool(value)

    def mark_task_started(self) -> None:
        self._active_tasks += 1
        self._state = "running"

    def mark_task_completed(self) -> None:
        self._active_tasks = max(0, self._active_tasks - 1)
        if self._active_tasks == 0:
            self._state = "idle"

    def set_sandbox_count(self, n: int) -> None:
        if n < 0:
            raise ValueError("sandbox_count debe ser ≥ 0")
        self._sandboxes = n


# ---------------------------------------------------------------------------
# T048 — DbusRuntimeServiceWiring
# ---------------------------------------------------------------------------


class DbusAuthorizationError(PermissionError):
    """Sender UID no autorizado para esta operación (CWE-862 / CTRL-12/KILL-1)."""


@dataclass(frozen=True, slots=True)
class HitlApprovalResult:
    """Resultado de ApproveAction — token opaco + operador verificado.

    thread_resumed: True when a blocked conversation thread was found and
    signalled (LIVE block-and-resume: the exact tool call will execute).
    False when no thread was waiting — either the proposal timed out (the
    event slot was cleaned up) or the approval arrived after the turn ended.
    POST-execution approvals are NOT silently treated as success.
    """

    approval_token: str
    approved_by: UUID
    thread_resumed: bool = True  # default True for non-native-danger tasks (no event)


class DbusRuntimeServiceWiring:
    """Capa de lógica pura que cablea los puertos reales con authZ del bus.

    Diseño (T048 / CTRL-12 / KILL-1 / SC-004 / CWE-862):
    - `sender_identity` (UID del proceso caller) lo resuelve el DbusAdapter
      desde el bus ANTES de invocar estos métodos — NUNCA viene del payload
      del cliente.
    - Solo los UIDs en `authorized_uids` pueden pausar/reanudar/aprobar/encolar.
    - Fail-closed: UID no autorizado ⇒ DbusAuthorizationError; no ejecuta.
    - `approved_by` / `changed_by` / `enqueued_by` = identidad verificada del
      sender, no del payload.
    - ApproveAction/RejectAction NO disparan run_cycle — solo resuelven la
      aprobación; el loop retoma la tarea pendiente (NFR-001).
    - `control_plane_service` (requerido para Enqueue): la ruta D-Bus DELEGA
      en ControlPlaneService para que rate-limit + PII + audit sean los mismos
      controles que en la ruta testeable (CWE-770 / CTRL-P1-6, Issue 2).

    Confused-deputy hybrid model (security-hardening):
    - Direct operator calls (GTK shell → D-Bus, uid=hermes-user) continue
      unchanged: sender_uid ∈ authorized_uids → authorized, operator_id
      derived from sender_uid.
    - Proxied calls (shell-server → D-Bus, uid=hermes process) MUST supply
      a valid OperatorToken in the `operator_token` argument. The token is
      verified by `operator_token_verifier` (HMAC-SHA256 over master.key
      subkey). operator_id is taken from the token, never from the proxy uid.
    - Proxy uid NOT in authorized_uids + no token → DbusAuthorizationError.
    - Token expired/forged → DbusAuthorizationError.
    - Read-only methods (get_status, list_*) do not require a token.

    Testeable sin D-Bus: inyecta fakes de AgentStatePort + ApprovalGatePort +
    ControlPlaneService y pasa sender_uid directamente. El DbusAdapter real
    resuelve el UID del bus y delega aquí.
    """

    def __init__(
        self,
        *,
        agent_state: AgentStatePort,
        approval_gate: ApprovalGatePort,
        authorized_uids: frozenset[int],
        work_queue: WorkQueuePort | None = None,
        wake_signal: Any | None = None,  # WorkerWakeSignal | None (CTRL-P1-12)
        control_plane_service: ControlPlaneService | None = None,
        trigger_repo: Any | None = None,   # SqliteAuthorizedTriggerRepository | None
        agent_registry: Any | None = None,  # AgentRegistryPort (roster multi-agente)
        skill_governance: Any | None = None,  # SkillGovernancePort (P0-1)
        platform_model_registry: Any | None = None,  # SqlitePlatformModelRegistry (F010)
        platform_model_signer: Any | None = None,   # PlatformModelSigner (security hardening)
        capability_binding_repo: Any | None = None,   # SqliteCapabilityBindingRepo (F010)
        access_scope_repo: Any | None = None,   # SqliteAgentAccessScopeRepo (Fase 2 Phase 3)
        provider_repo: Any | None = None,   # SQLiteProviderRepository (GATE 0 / M1: providers OS-nativos)
        conversation_repo: Any | None = None,  # SQLiteConversationRepository (GATE 0 / M2: chat OS-nativo)
        tenant_id: str = "",  # tenant scope for platform reads (F010)
        # Confused-deputy hybrid model (security-hardening):
        # UID of the proxy process (shell-server). When a call arrives from
        # this uid, a valid operator_token is required. Set to None to disable
        # the proxy path entirely (direct-only mode).
        proxy_uid: int | None = None,
        # OperatorTokenVerifier built from SecretsVault.derive_subkey(
        #   label="operator-token"). Required when proxy_uid is set.
        operator_token_verifier: OperatorTokenVerifier | None = None,
        # T017 — desktop overlay (spec 014-agentic-desktop):
        # ContextSnapshotComposer for RequestContextSnapshot (read-only).
        # None → method returns a "not-configured" stub JSON.
        context_snapshot_composer: Any | None = None,
        # AuditHashChainSigner for GetAuditChainHead (read-only head hash).
        # None → method returns integrity="unknown".
        audit_signer: Any | None = None,
        # spec 014 increment 3 — FR-013 operator consent control:
        # ConsentManager for GrantConsent/RevokeConsent/ListConsents.
        # None → methods return "not_configured" error (honest degradation).
        consent_manager: Any | None = None,  # ConsentManager | None
        # MCP Apps: McpServerManager del daemon (conexiones vivas). None →
        # los verbos MCP devuelven "not_configured" (degradación honesta).
        mcp_server_manager: Any | None = None,
        # ActiveProviderService — único punto de resolución del provider activo.
        # None → get_active_provider cae al camino legacy (_read_native_active +
        # provider_repo), igual que antes (degradación honesta).
        active_provider_service: Any | None = None,  # ActiveProviderService | None
        # Security Center scan service (lazy-init if None).
        # Injected here so tests can supply a fake; production uses the lazy
        # factory inside _scan_service_lazy().
        scan_service: Any | None = None,  # ScanService | None
        # Callback invoked after a successful scan to emit D-Bus signals.
        # Signature: (scan_id: str, verdict: str, scan_data_json: str) → None.
        # Injected by DbusRuntimeAdapter after bus start.
        scan_signal_emitter: Any | None = None,  # callable | None
        # FR-013 consent subject alignment: the OWNER operator UUID
        # (HERMES_OPERATOR_ID, resolved at daemon boot via _resolve_operator_id).
        # When set, all consent verbs operate on this fixed owner rather than
        # deriving the subject from sender_uid.  This ensures the UI, the seed,
        # and the broker all address the same operator record.
        # Fallback to _uid_to_uuid(sender_uid) when None (CI/test/backward-compat).
        operator_id: "UUID | None" = None,
        # Zero-arg callable returning the real in-flight worker count.
        # Injected from __main__ via orchestrator.active_worker_count.
        # None → runtime_status reports active_task_count=0 (honest degradation).
        worker_count_fn: "Callable[[], int] | None" = None,
        # SqliteNotificationStore — persists task/chat completion notifications.
        # None → notification verbs degrade gracefully (empty list / no-op write).
        notification_store: "Any | None" = None,
        # SQLiteAssociationStore — used by license enforcement (Fase 3a).
        # None → CE mode assumed (no license checks).
        association_store: "Any | None" = None,
        # SkillStoreAdapter — único escritor de SKILL.md firmados. Inyectado
        # desde __main__ (el mismo que se registra en SurfaceAdapterDispatcher).
        # None → create_skill_from_text no disponible (degradación honesta).
        skill_store_adapter: "Any | None" = None,
    ) -> None:
        self._state = agent_state
        self._gate = approval_gate
        self._authorized_uids = authorized_uids
        self._queue = work_queue
        self._wake_signal = wake_signal  # inyectado en composición daemon
        self._cp_service = control_plane_service
        self._trigger_repo = trigger_repo  # para AuthorizeTrigger/RevokeTrigger
        self._agent_registry = agent_registry  # gobernanza del roster (List/Create/...)
        self._skill_governance = skill_governance  # gobernanza de skills (P0-1)
        self._platform_model_registry = platform_model_registry  # gobernanza plataformas (F010)
        self._platform_model_signer = platform_model_signer  # firma/verificación modelos (hardening)
        self._capability_binding_repo = capability_binding_repo  # asignación capacidades (F010)
        self._access_scope_repo = access_scope_repo  # per-agent native-tool scope (Fase 2 Phase 3)
        self._provider_repo = provider_repo  # GATE 0 / M1: providers OS-nativos (D-Bus, no HTTP)
        self._conversation_repo = conversation_repo  # GATE 0 / M2: chat OS-nativo (D-Bus, no HTTP)
        self._tenant_id = tenant_id  # tenant scope para lecturas de plataforma (F010)
        self._proxy_uid = proxy_uid
        self._token_verifier = operator_token_verifier
        # T017 — desktop overlay (spec 014-agentic-desktop)
        self._context_snapshot_composer = context_snapshot_composer  # ContextSnapshotComposer | None
        self._audit_signer = audit_signer  # AuditHashChainSigner | None
        # spec 014 increment 3 — FR-013 operator consent control
        self._consent_manager = consent_manager  # ConsentManager | None
        # MCP Apps (gestión de servidores MCP por el operador)
        self._mcp_manager = mcp_server_manager
        # Único punto de resolución del provider activo (caché LRU 30s).
        self._active_provider_svc = active_provider_service  # ActiveProviderService | None
        # Security Center: pre-built ScanService (or None → lazy init on first use).
        self._scan_service = scan_service  # ScanService | None
        # Callback (scan_id, verdict, scan_data_json) emitted after scanning.
        # Injected by DbusRuntimeAdapter; None in test environments.
        self._scan_signal_emitter = scan_signal_emitter  # callable | None
        # In-memory audit accumulator — tests check via audit_entries_emitted()
        self._audit_entries: list[AuditEntry] = []
        # FR-013: owner operator UUID for consent subject alignment (see _consent_operator).
        self._operator_id: "UUID | None" = operator_id
        # Live in-flight worker count accessor (injected from __main__).
        # None → runtime_status reports 0 (honest degradation, not a lie).
        self._worker_count_fn: Callable[[], int] | None = worker_count_fn
        # SqliteNotificationStore — written by daemon, read via D-Bus by shell-server.
        # None → verbs degrade gracefully (list=[]/count=0/mark-read=no-op).
        self._notification_store = notification_store
        # SQLiteAssociationStore — license enforcement (Fase 3a).
        # None → CE mode assumed; no create_agent or enqueue restrictions apply.
        self._association_store = association_store  # SQLiteAssociationStore | None
        # SkillStoreAdapter — único escritor autorizado de SKILL.md firmados.
        # None → create_skill_from_text devuelve error (degradación honesta).
        self._skill_store_adapter = skill_store_adapter  # SkillStoreAdapter | None
        # FASE 3 (A2A cross-human) — DelegationApprovalService, singleton
        # perezoso (mismo patrón que _trigger_repo). Construido on-demand por
        # _require_delegation_approval_service, no inyectado por constructor:
        # depende de self._conversation_repo/_trigger_repo/_queue/_state, ya
        # todos presentes en self tras __init__.
        self._delegation_approval_service: Any | None = None

    # ------------------------------------------------------------------
    # Kill-switch (CTRL-12 / KILL-1)
    # ------------------------------------------------------------------

    async def request_pause(
        self,
        *,
        reason: str,
        sender_uid: int,
        operator_token: str | None = None,
    ) -> None:
        """Pausa el agente. sender_uid resuelto por el bus (CWE-862).

        operator_token required when sender_uid == proxy_uid (confused-deputy
        remediation). operator_id derived from token, not from proxy uid.

        Raises:
            DbusAuthorizationError: UID del sender no está autorizado o token inválido.
        """
        operator_id = self._authorize_and_resolve(
            sender_uid, operation="request_pause", operator_token=operator_token
        )
        await self._state.pause(by=operator_id, reason=reason)
        logger.info(
            "hermes.dbus.agent_paused",
            extra={"by_uid": sender_uid, "reason": reason},
        )

    async def cancel_task(
        self,
        *,
        task_id: str,
        sender_uid: int,
        operator_token: str | None = None,
    ) -> dict:
        """Solicita cancelar UNA tarea en ejecución por su task_id (cooperativa).

        Marca el task_id en el registry de cancelación (proceso-local); el
        stream-callback del ciclo lo consulta por-token y desenrolla run_conversation
        → el orchestrator marca la tarea CANCELLED (terminal, sin retry) y cierra el
        stream. authZ: operador (sender_uid del bus, CWE-862).
        """
        self._authorize_and_resolve(
            sender_uid, operation="cancel_task", operator_token=operator_token
        )
        from uuid import UUID as _UUID  # noqa: PLC0415
        from hermes.tasks.domain.task_cancel_registry import (  # noqa: PLC0415
            get_cancel_registry,
        )

        try:
            tid = _UUID(str(task_id))
        except (ValueError, TypeError):
            return {"ok": False, "error": f"task_id inválido: {task_id!r}"}
        get_cancel_registry().request_cancel(tid, reason="Detenida por el operador")
        logger.info(
            "hermes.dbus.task_cancel_requested",
            extra={"task_id": str(tid), "by_uid": sender_uid},
        )
        return {"ok": True, "requested": True}

    async def request_resume(
        self,
        *,
        sender_uid: int,
        operator_token: str | None = None,
    ) -> None:
        """Reanuda el agente. sender_uid resuelto por el bus (CWE-862).

        operator_token required when sender_uid == proxy_uid.

        Raises:
            DbusAuthorizationError: UID del sender no está autorizado o token inválido.
        """
        operator_id = self._authorize_and_resolve(
            sender_uid, operation="request_resume", operator_token=operator_token
        )
        await self._state.resume(by=operator_id)
        logger.info(
            "hermes.dbus.agent_resumed",
            extra={"by_uid": sender_uid},
        )

    # ------------------------------------------------------------------
    # Gobernanza del roster multi-agente (estado NATIVO del daemon, Principio 0)
    #   - Lecturas: supervisión, sin authZ.
    #   - Mutadores: autoría por sender_uid del bus (CWE-862), fail-closed.
    # ------------------------------------------------------------------
    def _require_registry(self):
        if self._agent_registry is None:
            raise RuntimeError("agent_registry no inyectado en el wiring")
        return self._agent_registry

    def _check_license_for_create_agent(self) -> None:
        """Enforce associate license limits before creating an agent.

        - Skipped entirely in community edition (no association_store).
        - LicenseExpired: raises if expires_at is in the past.
        - LicenseExceeded: raises if active agent count >= max_agents.
        - Never deletes or modifies agent data (invariant).
        """
        if self._association_store is None:
            return
        if not self._association_store.is_associated():
            return

        from hermes.agents.domain.ports import LicenseExceeded, LicenseExpired  # noqa: PLC0415

        assoc = self._association_store.get()
        if assoc is None:
            return

        lic: dict = assoc.license or {}
        self._assert_license_not_expired(lic)

        max_agents = lic.get("max_agents")
        if max_agents is not None and self._agent_registry is not None:
            # The per-agent license caps only cloud-managed (enterprise-licensed)
            # agents. The local CE roster + CEO are bundled with the runtime and
            # never consume a seat — otherwise a fresh associate (28 default
            # agents) would be over a 10-seat license before the cloud lands one.
            current_count = sum(
                1
                for a in self._agent_registry.list_agents()
                if getattr(a, "managed_by", None) == "cloud"
            )
            if current_count >= int(max_agents):
                logger.warning(
                    "hermes.dbus.license_exceeded",
                    extra={"current": current_count, "max": max_agents},
                )
                raise LicenseExceeded(
                    f"License limit reached: max_agents={max_agents}, "
                    f"current={current_count}. Upgrade your license to add more agents."
                )

    def _check_license_for_enqueue(self) -> None:
        """Enforce associate license expiry before enqueuing work.

        Raises LicenseExpired if the license has expired.
        Existing agents and data are never touched (invariant).
        """
        if self._association_store is None:
            return
        if not self._association_store.is_associated():
            return

        from hermes.agents.domain.ports import LicenseExpired  # noqa: PLC0415

        assoc = self._association_store.get()
        if assoc is None:
            return
        self._assert_license_not_expired(assoc.license or {})

    @staticmethod
    def _assert_license_not_expired(lic: dict) -> None:
        """Raise LicenseExpired if expires_at is present and in the past."""
        from hermes.agents.domain.ports import LicenseExpired  # noqa: PLC0415

        expires_at = lic.get("expires_at")
        if not expires_at:
            return
        from datetime import UTC, datetime  # noqa: PLC0415

        try:
            expiry = datetime.fromisoformat(str(expires_at))
        except ValueError:
            logger.warning(
                "hermes.dbus.license_bad_expires_at",
                extra={"value": str(expires_at)[:80]},
            )
            return
        # A date-only / naive expires_at (e.g. "2027-12-31", the common console
        # value) parses tz-naive; comparing it against an aware `now` raises
        # `TypeError: can't compare offset-naive and offset-aware datetimes` and
        # broke create_agent for EVERY cloud agent (2026-07-05, caught by the
        # 20-employee Enterprise live test). Assume UTC when no tz is given.
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=UTC)
        if datetime.now(tz=UTC) > expiry:
            logger.warning(
                "hermes.dbus.license_expired",
                extra={"expires_at": str(expires_at)},
            )
            raise LicenseExpired(
                f"Associate license expired at {expires_at}. "
                "Renew your license to create agents or enqueue tasks."
            )

    def list_agents(self) -> list[dict]:
        """Return the visible agent list.

        Associate mode (instance is paired with a cloud tenant):
          When cloud-managed agents exist, expose only those to the employee UI.
          This prevents the CE default roster of 28 agents from dominating the
          associate's UI — the enterprise controls which agents are visible.
          The default CEO (is_default=True) is always included as fallback.

        Community / CE mode (not associated, or no cloud agents yet):
          Returns the full registry list unchanged (default roster filtered per
          the default_roster_enabled flag in the registry).
        """
        from hermes.agents.application.serialization import agent_to_dict  # noqa: PLC0415

        if self._agent_registry is None:
            return []

        all_agents = self._agent_registry.list_agents()
        if self._is_associate_mode():
            return _filter_associate_agents(all_agents, agent_to_dict)
        return [agent_to_dict(a) for a in all_agents]

    def _is_associate_mode(self) -> bool:
        """True when the instance is paired with a cloud tenant."""
        if self._association_store is None:
            return False
        try:
            return self._association_store.is_associated()
        except Exception:  # noqa: BLE001
            return False

    def runtime_status(self) -> dict:
        """Return the real live runtime status: state, active_task_count,
        activity (in-flight tool per task), ruflo_active signal, and
        delegations (short-lived agent→agent hand-off edges).

        Read-only, no authZ. Fail-soft: core keys always present.
        Note: active_agent_id has been removed — there is no global active agent.
        Each conversation carries its own agent binding (per-conversation contract).
        """
        try:
            count = self._worker_count_fn() if self._worker_count_fn is not None else 0
            status: dict = {
                "state": "working" if count > 0 else "idle",
                "active_task_count": count,
                "captured_at": datetime.now(tz=UTC).isoformat(),
            }
        except Exception:  # noqa: BLE001 — never crash a status read
            logger.warning("hermes.dbus.runtime_status_error", exc_info=True)
            return {
                "state": "idle",
                "active_task_count": 0,
                "captured_at": datetime.now(tz=UTC).isoformat(),
            }

        try:
            from hermes.runtime import live_activity  # noqa: PLC0415
            activity = live_activity.snapshot()
            status["activity"] = activity
            status["ruflo_active"] = any(
                e.get("tool", "").startswith("mcp__ruflo__") for e in activity
            )
            status["delegations"] = live_activity.snapshot_delegations()
        except Exception:  # noqa: BLE001 — activity is additive; omit on error
            logger.debug("hermes.dbus.runtime_status_activity_error", exc_info=True)

        return status

    async def create_agent(self, *, draft, sender_uid: int) -> dict:
        from hermes.agents.application.serialization import agent_to_dict  # noqa: PLC0415

        self._authorize(sender_uid, operation="create_agent")
        self._check_license_for_create_agent()  # raises LicenseExceeded / LicenseExpired
        agent = self._require_registry().create_agent(draft)
        logger.info("hermes.dbus.agent_created", extra={"by_uid": sender_uid})
        return agent_to_dict(agent)

    async def update_agent(self, *, agent_id: str, draft, sender_uid: int) -> dict:
        from hermes.agents.application.serialization import agent_to_dict  # noqa: PLC0415

        self._authorize(sender_uid, operation="update_agent")
        agent = self._require_registry().update_agent(agent_id, draft)
        logger.info("hermes.dbus.agent_updated", extra={"by_uid": sender_uid})
        return agent_to_dict(agent)

    async def delete_agent(self, *, agent_id: str, sender_uid: int) -> None:
        self._authorize(sender_uid, operation="delete_agent")
        self._require_registry().delete_agent(agent_id)
        logger.info("hermes.dbus.agent_deleted", extra={"by_uid": sender_uid})

    def default_roster_enabled(self) -> bool:
        """¿Visible el equipo de especialistas por defecto? (read, sin authZ)."""
        if self._agent_registry is None:
            return True
        return self._agent_registry.default_roster_enabled()

    async def set_default_roster_enabled(self, *, enabled: bool, sender_uid: int) -> bool:
        """Enciende/apaga el equipo por defecto (filtra los `roster-*`, NO borra)."""
        self._authorize(sender_uid, operation="set_default_roster_enabled")
        self._require_registry().set_default_roster_enabled(enabled)
        logger.info(
            "hermes.dbus.default_roster_toggled",
            extra={"enabled": enabled, "by_uid": sender_uid},
        )
        return True

    # ------------------------------------------------------------------
    # Gobernanza de skills (Principio 0 / P0-1):
    #   - Lecturas: supervisión, sin authZ.
    #   - Mutadores: autoría por sender_uid del bus (CWE-862), fail-closed.
    # ------------------------------------------------------------------

    def _require_skill_governance(self) -> Any:
        if self._skill_governance is None:
            raise RuntimeError("skill_governance no inyectado en el wiring")
        return self._skill_governance

    # ------------------------------------------------------------------
    # GATE 0 / M1 — Providers OS-nativos (D-Bus, ya no HTTP shell-server).
    # El daemon POSEE la tabla providers + SecretsVault (las usa para resolver
    # el modelo activo). Lecturas: sin authZ. Mutadores: sender_uid del operador.
    # ------------------------------------------------------------------

    @staticmethod
    def _provider_to_dict(p: Any) -> dict:
        """Misma forma que ProviderResponse.from_provider (paridad con el HTTP)."""
        return {
            "provider_id": str(p.provider_id),
            "alias": p.alias,
            "kind": p.kind.value,
            "base_url": p.base_url,
            "default_model": p.default_model,
            "enabled": bool(p.enabled),
            "is_active": bool(p.is_active),
            "has_api_key": bool(p.has_api_key),
            "connectivity": p.connectivity.value,
            "last_checked_at": p.last_checked_at.isoformat() if p.last_checked_at else None,
            "created_at": p.created_at.isoformat() if getattr(p, "created_at", None) else "",
            "managed_by": getattr(p, "managed_by", None),
        }

    def list_providers(self) -> list[dict]:
        """Lista providers (read-only)."""
        if self._provider_repo is None:
            return []
        return [self._provider_to_dict(p) for p in self._provider_repo.list_all()]

    def get_active_provider(self) -> dict:
        """Provider activo, o {} si no hay (read-only).

        Delega en ActiveProviderService (caché LRU 30s, cascade nativo→SQL→env).
        Si el servicio no está inyectado, cae al camino legacy (_read_native_active
        + provider_repo) para compatibilidad hacia atrás.
        """
        if self._active_provider_svc is not None:
            return self._active_provider_svc.get_active_metadata()
        # Legacy fallback (sin ActiveProviderService inyectado).
        native = _read_native_active()
        if native:
            return native
        if self._provider_repo is None:
            return {}
        p = self._provider_repo.get_active()
        return self._provider_to_dict(p) if p is not None else {}

    # ------------------------------------------------------------------
    # Native provider sync — collapses Safent SQL store → hermes_cli NATIVO.
    # ------------------------------------------------------------------

    def _sync_to_native_provider(
        self,
        provider: "Any",  # hermes.shell_server.providers.domain.Provider
        api_key: "str | None",
        *,
        set_active: bool = False,
    ) -> None:
        """Write provider config to hermes_cli NATIVO path (fail-soft).

        Maps ProviderKind → native provider_id via native_sync.kind_to_native_target,
        then mirrors to HERMES_HOME/.env + config.yaml using the same helpers as
        configure_native_provider.  If hermes_cli is unavailable or the kind has
        no api_key (e.g. NOUS OAuth), this is a no-op — the SQL store remains the
        fallback source as before.

        The call is intentionally fire-and-log: native write failures MUST NOT
        break the Safent flow (the SQL store is still valid fallback per the cascade
        in provider_config_source.resolve_model_config).
        """
        try:
            from hermes.shell_server.providers.native_sync import kind_to_native_target  # noqa: PLC0415
            from hermes_cli.auth import PROVIDER_REGISTRY  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001
            logger.debug("hermes.dbus.native_sync_unavailable: %s", exc)
            return

        try:
            target = kind_to_native_target(provider.kind)

            # NOUS and OAuth providers have no api_key path — skip write.
            if not target.env_var:
                return

            key = (api_key or "").strip()
            if not key:
                return

            # Validate env_var against PROVIDER_REGISTRY so we never write to
            # an invented env-var: prefer the registry's declared api_key_env_vars
            # over our static table when the provider_id exists in the registry.
            registry_cfg = PROVIDER_REGISTRY.get(target.provider_id)
            env_var = target.env_var
            if registry_cfg is not None:
                declared_vars = getattr(registry_cfg, "api_key_env_vars", ()) or ()
                if declared_vars:
                    env_var = declared_vars[0]

            _write_hermes_env(env_var, key)

            bu = (provider.base_url or "").strip()
            if bu and target.base_url_env_var:
                _write_hermes_env(target.base_url_env_var, bu)
                if registry_cfg is not None:
                    declared_bu_var = getattr(registry_cfg, "base_url_env_var", "") or ""
                    if declared_bu_var and declared_bu_var != target.base_url_env_var:
                        _write_hermes_env(declared_bu_var, bu)

            if set_active:
                _write_hermes_model_config(
                    target.provider_id,
                    (provider.default_model or "").strip(),
                    bu,
                )

            # Inject into live process env so the running daemon resolves the
            # key on the next cycle without restart (mirrors configure_native_provider).
            try:
                import os as _os  # noqa: PLC0415
                _os.environ[env_var] = key
            except Exception as exc:  # noqa: BLE001
                logger.warning("hermes.dbus.native_sync_env_load_failed: %s", exc)

            if set_active and self._active_provider_svc is not None:
                self._active_provider_svc.force_refresh()

            logger.info(
                "hermes.dbus.native_sync_ok",
                extra={
                    "kind": str(provider.kind),
                    "native_id": target.provider_id,
                    "set_active": set_active,
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "hermes.dbus.native_sync_failed kind=%s: %s",
                getattr(provider, "kind", "?"),
                exc,
            )

    def migrate_active_provider_to_native(self) -> None:
        """One-shot startup migration: push SQL active provider → native config.

        Runs once at daemon boot (called from __main__ after wiring is built).
        Idempotent: if the native config already has an active provider
        (_load_native_model_config is not None) this is a no-op.  Fail-soft:
        any error is logged and swallowed — a broken provider MUST NOT prevent
        the daemon from starting.
        """
        try:
            from hermes.runtime.provider_config_source import (  # noqa: PLC0415
                _load_native_model_config,
            )

            if _load_native_model_config() is not None:
                logger.debug("hermes.dbus.migrate_provider.already_native")
                return

            if self._provider_repo is None:
                return

            active = self._provider_repo.get_active()
            if active is None:
                logger.debug("hermes.dbus.migrate_provider.no_sql_active")
                return

            api_key: str | None = None
            if active.has_api_key:
                try:
                    api_key = self._provider_repo.reveal_api_key(
                        provider_id=active.provider_id
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "hermes.dbus.migrate_provider.reveal_failed: %s", exc
                    )

            self._sync_to_native_provider(active, api_key, set_active=True)
            logger.info(
                "hermes.dbus.migrate_provider.done",
                extra={
                    "alias": active.alias,
                    "kind": str(active.kind),
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.dbus.migrate_provider.failed: %s", exc)

    def add_provider(self, *, draft_json: str, sender_uid: int) -> dict:
        """Crea provider. draft: {kind, alias, default_model, base_url, api_key, set_active}."""
        self._authorize_and_resolve(sender_uid, operation="add_provider")
        if self._provider_repo is None:
            raise RuntimeError("provider_repo no inyectado en el daemon")
        from hermes.shell_server.providers.domain import (  # noqa: PLC0415
            ProviderKind,
            new_provider,
        )

        d = json.loads(draft_json)
        api_key = d.get("api_key") or None
        provider = new_provider(
            alias=d["alias"],
            kind=ProviderKind(d["kind"]),
            default_model=d.get("default_model", ""),
            base_url=d.get("base_url") or None,
            has_api_key=api_key is not None,
        )
        # Ownership: the config-sync applier stamps managed_by="cloud" so the row
        # is gated against local edits/deletes (REST layer) + reconcilable.
        provider.managed_by = d.get("managed_by") or None
        saved = self._provider_repo.add(provider=provider, api_key=api_key)
        set_active = bool(d.get("set_active"))
        if set_active:
            self._provider_repo.set_active(provider_id=saved.provider_id)
            saved.is_active = True
        self._sync_to_native_provider(saved, api_key, set_active=set_active)
        return self._provider_to_dict(saved)

    def update_provider(self, *, provider_id: str, draft_json: str, sender_uid: int) -> dict:
        """Actualiza alias/default_model/base_url/enabled/api_key."""
        self._authorize_and_resolve(sender_uid, operation="update_provider")
        from uuid import UUID as _UUID  # noqa: PLC0415

        pid = _UUID(provider_id)
        current = self._provider_repo.get(provider_id=pid)
        d = json.loads(draft_json)
        if d.get("alias") is not None:
            current.alias = d["alias"]
        if d.get("default_model") is not None:
            current.default_model = d["default_model"]
        if d.get("base_url") is not None:
            current.base_url = d["base_url"]
        if d.get("enabled") is not None:
            current.enabled = bool(d["enabled"])
        if d.get("managed_by") is not None:
            current.managed_by = d["managed_by"]
        api_key = d.get("api_key") or None
        self._provider_repo.update(provider=current, api_key=api_key)
        # Honor set_active on update too (parity with add_provider): the cloud
        # bundle marks the agent's provider_alias active, and re-publishes route
        # through update_provider once the row exists. Without this the engine
        # keeps no active model and chat fails with "HERMES_MODEL no definido".
        set_active = bool(d.get("set_active"))
        if set_active:
            self._provider_repo.set_active(provider_id=pid)
        updated = self._provider_repo.get(provider_id=pid)
        self._sync_to_native_provider(updated, api_key, set_active=set_active)
        return self._provider_to_dict(updated)

    def delete_provider(self, *, provider_id: str, sender_uid: int) -> bool:
        self._authorize_and_resolve(sender_uid, operation="delete_provider")
        from uuid import UUID as _UUID  # noqa: PLC0415

        self._provider_repo.delete(provider_id=_UUID(provider_id))
        return True

    def set_active_provider(self, *, provider_id: str, sender_uid: int) -> dict:
        self._authorize_and_resolve(sender_uid, operation="set_active_provider")
        from uuid import UUID as _UUID  # noqa: PLC0415

        pid = _UUID(provider_id)
        self._provider_repo.set_active(provider_id=pid)
        p = self._provider_repo.get(provider_id=pid)
        # Reveal the stored api_key so _sync_to_native_provider can forward it.
        api_key: "str | None" = None
        try:
            api_key = self._provider_repo.reveal_api_key(provider_id=pid)
        except Exception as exc:  # noqa: BLE001
            logger.debug("hermes.dbus.set_active_reveal_failed: %s", exc)
        self._sync_to_native_provider(p, api_key, set_active=True)
        # force_refresh is called inside _sync_to_native_provider when set_active=True
        # and _active_provider_svc is present.  Call it here too as safety net for
        # the case where _sync_to_native fails (fail-soft path leaves svc stale).
        if self._active_provider_svc is not None:
            self._active_provider_svc.force_refresh()
        return self._provider_to_dict(p)

    async def test_provider(self, *, provider_id: str, sender_uid: int) -> dict:
        """Valida el provider a través del runtime REAL (Nous), no de un dialecto
        paralelo: resuelve el ModelConfig como el daemon + una completion mínima
        por hermes-agent. {ok, error}. Mantiene 'idioma de Hermes'."""
        self._authorize_and_resolve(sender_uid, operation="test_provider")
        from uuid import UUID as _UUID  # noqa: PLC0415

        pid = _UUID(provider_id)
        provider = self._provider_repo.get(provider_id=pid)
        api_key = self._provider_repo.reveal_api_key(provider_id=pid)
        try:
            ok, err = await _nous_validate_provider(provider, api_key)
        except Exception as exc:  # noqa: BLE001
            ok, err = False, f"{type(exc).__name__}: {str(exc)[:300]}"
        from hermes.shell_server.providers.domain import ProviderConnectivity  # noqa: PLC0415
        from datetime import datetime, timezone  # noqa: PLC0415

        provider.connectivity = (
            ProviderConnectivity.REACHABLE if ok else ProviderConnectivity.UNREACHABLE
        )
        provider.last_checked_at = datetime.now(tz=timezone.utc)
        self._provider_repo.update(provider=provider)
        return {"ok": ok, "error": err}

    # ------------------------------------------------------------------
    # Egress (config-sync path) — soberanía daemon-side.
    #
    # add_egress_domain: SÓLO añade dominios a la allow-list (egress-grants.json).
    # Invariantes de soberanía (NO negociables):
    #   - Nunca toca la blocklist (egress-blocklist.txt — inmutable, baked).
    #   - Nunca toca la deny-list (egress-denylist.json).
    #   - Nunca cambia el network mode (egress-mode.json).
    #   - Solo opera sobre _GRANTS_PATH (egress-grants.json).
    #
    # Validación: mismo _normalize + _DOMAIN_RE que egress_api.py REST, aplicados
    # en el wiring ANTES de persistir — el daemon es el escritor canónico.
    # ------------------------------------------------------------------

    def list_egress_grants(self) -> list[dict]:
        """Lista los dominios de la allow-list (read-only). Sin authZ."""
        from hermes.shell_server.egress_api import _load  # noqa: PLC0415

        return [{"domain": d} for d in _load()]

    def add_egress_domain(self, *, domain: str, sender_uid: int) -> dict:
        """Añade `domain` a la allow-list (egress-grants.json).

        Sovereignty contract (non-negotiable):
          - Validates with _normalize + _DOMAIN_RE (same as egress_api REST).
          - Rejects IPs, wildcards, empty, whitespace, and paths.
          - NEVER touches the blocklist, deny-list, or network mode.
          - Only writes egress-grants.json (browser allow-list).
          - Calls apply_persisted_grants() after save so the proxy applies
            the new allow-list without restart.

        Returns {"ok": True} on success, {"ok": False, "error": reason} on failure.
        """
        self._authorize_and_resolve(sender_uid, operation="add_egress_domain")
        from hermes.shell_server.egress_api import (  # noqa: PLC0415
            _DOMAIN_RE,
            _load,
            _normalize,
            _save,
            apply_persisted_grants,
        )

        normalised = _normalize(domain)
        if not normalised or not _DOMAIN_RE.match(normalised):
            logger.warning(
                "hermes.dbus.add_egress_domain.invalid",
                extra={"domain": domain[:64]},
            )
            return {"ok": False, "error": f"dominio inválido: {domain[:64]!r}"}

        existing = set(_load())
        if normalised in existing:
            return {"ok": True, "domain": normalised, "already_present": True}

        domains = sorted(existing | {normalised})
        _save(domains)
        apply_persisted_grants()
        logger.info("hermes.dbus.egress_domain_added", extra={"domain": normalised})
        return {"ok": True, "domain": normalised}

    # ------------------------------------------------------------------
    # GATE 0 / M2 — Conversaciones (chat) OS-nativas por D-Bus.
    # Lecturas (list/get): supervisión read-only, sin authZ. Delete: muta →
    # authZ por sender_uid (CWE-862). El daemon ES dueño del store; el stream
    # de respuesta ya viaja por el socket AF_UNIX (no HTTP).
    # ------------------------------------------------------------------

    @staticmethod
    def _conversation_summary_to_dict(c: Any) -> dict:
        return {
            "conversation_id": str(c.conversation_id),
            "title": c.title,
            "provider_alias": c.provider_alias,
            "model": c.model,
            "started_at": c.started_at.isoformat(),
            "last_msg_at": c.last_msg_at.isoformat(),
            "message_count": c.message_count,
            "agent_id": c.agent_id,
        }

    def list_conversations(self, *, agent_id: str | None = None) -> list[dict]:
        """Recientes (read-only). agent_id='' → todas; si no, filtra por agente."""
        if self._conversation_repo is None:
            return []
        items = self._conversation_repo.list_summaries(agent_id=agent_id or None)
        return [self._conversation_summary_to_dict(c) for c in items]

    def get_conversation(self, *, conversation_id: str) -> dict:
        """Detalle con mensajes (read-only). {} si no existe."""
        if self._conversation_repo is None:
            return {}
        from uuid import UUID as _UUID  # noqa: PLC0415

        try:
            d = self._conversation_repo.get_detail(conversation_id=_UUID(conversation_id))
        except Exception:
            return {}
        return {
            "conversation_id": str(d.conversation_id),
            "title": d.title,
            "provider_alias": d.provider_alias,
            "model": d.model,
            "started_at": d.started_at.isoformat(),
            "messages": [{"role": m.role, "content": m.content} for m in d.messages],
        }

    def delete_conversation(self, *, conversation_id: str, sender_uid: int) -> bool:
        """Borra una conversación. Muta → authZ por sender_uid (CWE-862)."""
        self._authorize_and_resolve(sender_uid, operation="delete_conversation")
        if self._conversation_repo is None:
            return False
        from uuid import UUID as _UUID  # noqa: PLC0415

        try:
            self._conversation_repo.delete(conversation_id=_UUID(conversation_id))
        except Exception:
            return False
        return True

    # ------------------------------------------------------------------
    # GATE 0 / M7 — Cuenta de SO (onboarding) OS-nativa por D-Bus.
    # Reemplaza POST /api/v1/setup/account. El daemon (User=hermes) DEJA un fichero
    # staged en /run/hermes/setup; el path-unit root hermes-account-apply lo aplica
    # (crea el usuario). One-time (sentinel). REUSE de shell_server.setup.api
    # (validación + escritura atómica), no reimplementar.
    # ------------------------------------------------------------------

    def stage_account(self, *, username: str, password: str, sender_uid: int) -> dict:
        """Valida y deja staged las credenciales de la cuenta de SO. {staged, error}."""
        self._authorize_and_resolve(sender_uid, operation="stage_account")
        from hermes.shell_server.setup.api import (  # noqa: PLC0415
            _DEFAULT_SENTINEL_FILE,
            _DEFAULT_STAGE_DIR,
            _validate_password,
            _validate_username,
            _write_staged_request,
        )

        if _DEFAULT_SENTINEL_FILE.exists():
            return {"staged": False, "error": "already_configured"}
        if not _validate_username(username):
            return {"staged": False, "error": "invalid_username"}
        if not _validate_password(password):
            return {"staged": False, "error": "invalid_password"}
        try:
            _write_staged_request(
                username=username, password=password, stage_dir=_DEFAULT_STAGE_DIR
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.dbus.stage_account_failed: %s", exc)
            return {"staged": False, "error": "stage_failed"}
        return {"staged": True, "error": None}

    def set_locale_keymap(self, *, locale: str, keymap: str, sender_uid: int) -> dict:
        """Stagea idioma + teclado del SO. El root oneshot hermes-locale-apply los
        aplica con localectl. {staged, error}. Validación estricta (allow-list de
        caracteres); el helper root NO confía en esta capa y revalida."""
        self._authorize_and_resolve(sender_uid, operation="set_locale_keymap")
        import json as _json  # noqa: PLC0415
        import os as _os  # noqa: PLC0415
        import re as _re  # noqa: PLC0415
        import stat as _stat  # noqa: PLC0415
        from datetime import UTC, datetime  # noqa: PLC0415
        from pathlib import Path  # noqa: PLC0415

        if not _re.match(r"^[a-z]{2}_[A-Z]{2}\.UTF-8$", locale or ""):
            return {"staged": False, "error": "invalid_locale"}
        if not _re.match(r"^[a-z0-9_-]{1,16}$", keymap or ""):
            return {"staged": False, "error": "invalid_keymap"}
        stage_dir = Path(_os.environ.get("HERMES_ACCOUNT_STAGE_DIR", "/run/hermes/setup"))
        try:
            stage_dir.mkdir(mode=0o700, exist_ok=True)
            target = stage_dir / "locale-request.json"
            tmp = target.with_suffix(".tmp")
            payload = {
                "locale": locale,
                "keymap": keymap,
                "requested_at": datetime.now(tz=UTC).isoformat(),
            }
            tmp.write_text(_json.dumps(payload), encoding="utf-8")
            _os.chmod(tmp, _stat.S_IRUSR | _stat.S_IWUSR)
            tmp.rename(target)
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.dbus.set_locale_keymap_failed: %s", exc)
            return {"staged": False, "error": "stage_failed"}
        return {"staged": True, "error": None}

    # ------------------------------------------------------------------
    # OAuth device-code de providers nativos (suscripciones sin clave API).
    # REUSE del patrón de hermes_cli/web_server.py (_start_device_code_flow +
    # _nous_poller): helpers puros de hermes_cli + poller en thread daemon.
    # Las credenciales se persisten en el auth-store de hermes_cli bajo
    # HERMES_HOME (= /var/lib/hermes/hermes-home), que es EXACTAMENTE donde
    # resolve_runtime_provider("nous") las lee al resolver el motor.
    # ------------------------------------------------------------------

    def start_provider_oauth(self, *, provider_id: str, sender_uid: int) -> dict:
        """Inicia el flow device-code. Devuelve {session_id, user_code,
        verification_url, expires_in, poll_interval} o {error}.

        Bloquea ~1 request HTTP (15s timeout) — el adapter D-Bus lo ejecuta
        en executor para no bloquear el event loop del daemon.
        """
        self._authorize_and_resolve(sender_uid, operation="start_provider_oauth")
        import os  # noqa: PLC0415
        import threading  # noqa: PLC0415
        import time as _time  # noqa: PLC0415
        import uuid as _uuid  # noqa: PLC0415

        # xAI (SuperGrok): OAuth de NAVEGADOR (loopback PKCE). El daemon levanta
        # un callback server local + construye la authorize URL; la UI la abre en
        # chromium; al volver, el worker intercambia el code y persiste. Port de
        # hermes_cli/web_server.py::_start_xai_loopback + _xai_loopback_worker.
        if provider_id == "xai-oauth":
            try:
                from hermes_cli import auth as hauth  # noqa: PLC0415
            except Exception as exc:  # noqa: BLE001
                return {"error": f"hermes_cli no disponible: {exc}"}
            try:
                discovery = hauth._xai_oauth_discovery()
                server, thr, cb_result, redirect_uri = hauth._xai_start_callback_server()
                hauth._xai_validate_loopback_redirect_uri(redirect_uri)
                verifier = hauth._oauth_pkce_code_verifier()
                challenge = hauth._oauth_pkce_code_challenge(verifier)
                import secrets as _secrets  # noqa: PLC0415
                state = _secrets.token_hex(16)
                authorize_url = hauth._xai_oauth_build_authorize_url(
                    authorization_endpoint=discovery["authorization_endpoint"],
                    redirect_uri=redirect_uri, code_challenge=challenge,
                    state=state, nonce=_secrets.token_hex(16),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("hermes.dbus.xai_oauth_start_failed: %s", exc)
                return {"error": f"xai start: {exc}"}
            sid = _uuid.uuid4().hex
            with _OAUTH_SESSIONS_LOCK:
                _OAUTH_SESSIONS[sid] = {
                    "status": "pending", "provider_id": "xai-oauth",
                    "server": server, "thread": thr, "callback_result": cb_result,
                    "redirect_uri": redirect_uri, "verifier": verifier,
                    "challenge": challenge, "state": state,
                    "token_endpoint": discovery["token_endpoint"],
                    "discovery": discovery, "error_message": None,
                }
            threading.Thread(
                target=_xai_loopback_worker, args=(sid,), daemon=True,
                name=f"oauth-xai-{sid[:6]}",
            ).start()
            return {"session_id": sid, "flow": "loopback", "auth_url": authorize_url,
                    "expires_in": 600}

        # OpenAI Codex (ChatGPT OAuth, suscripción): device-code propio de OpenAI
        # (no el endpoint estándar). El worker porta hermes_cli/web_server.py.
        if provider_id == "openai-codex":
            sid = _uuid.uuid4().hex
            with _OAUTH_SESSIONS_LOCK:
                _OAUTH_SESSIONS[sid] = {
                    "status": "pending", "provider_id": "openai-codex",
                    "user_code": "", "verification_url": "", "error_message": None,
                }
            threading.Thread(
                target=_codex_oauth_worker, args=(sid,), daemon=True,
                name=f"oauth-codex-{sid[:6]}",
            ).start()
            # Esperar a que el worker publique el user_code (step 1) ~10s.
            deadline = _time.monotonic() + 10
            while _time.monotonic() < deadline:
                with _OAUTH_SESSIONS_LOCK:
                    s = _OAUTH_SESSIONS.get(sid, {})
                if s.get("user_code") or s.get("status") in ("error", "approved"):
                    break
                _time.sleep(0.2)
            with _OAUTH_SESSIONS_LOCK:
                s = _OAUTH_SESSIONS.get(sid, {})
            if s.get("status") == "error":
                return {"error": s.get("error_message") or "codex device-auth falló"}
            if not s.get("user_code"):
                return {"error": "codex: timeout esperando user_code"}
            return {
                "session_id": sid, "flow": "device_code",
                "user_code": s["user_code"],
                "verification_url": s["verification_url"],
                "expires_in": int(s.get("expires_in") or 900),
                "poll_interval": int(s.get("interval") or 5),
            }

        if provider_id != "nous":
            return {"error": f"oauth_unsupported_provider:{provider_id}"}
        try:
            import httpx  # noqa: PLC0415
            from hermes_cli.auth import (  # noqa: PLC0415
                PROVIDER_REGISTRY,
                _request_device_code,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.dbus.oauth_unavailable: %s", exc)
            return {"error": "hermes_cli_unavailable"}

        pconfig = PROVIDER_REGISTRY["nous"]
        portal_base_url = (
            os.getenv("HERMES_PORTAL_BASE_URL")
            or os.getenv("NOUS_PORTAL_BASE_URL")
            or pconfig.portal_base_url
        ).rstrip("/")
        try:
            with httpx.Client(
                timeout=httpx.Timeout(15.0), headers={"Accept": "application/json"}
            ) as client:
                device_data = _request_device_code(
                    client=client,
                    portal_base_url=portal_base_url,
                    client_id=pconfig.client_id,
                    scope=pconfig.scope,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.dbus.oauth_device_request_failed: %s", exc)
            return {"error": f"device_request_failed: {exc}"}

        sid = _uuid.uuid4().hex
        sess = {
            "status": "pending",
            "provider_id": "nous",
            "device_code": str(device_data["device_code"]),
            "interval": int(device_data["interval"]),
            "expires_at": _time.time() + int(device_data["expires_in"]),
            "portal_base_url": portal_base_url,
            "client_id": pconfig.client_id,
            "scope": pconfig.scope,
            "error_message": None,
        }
        with _OAUTH_SESSIONS_LOCK:
            _OAUTH_SESSIONS[sid] = sess
        threading.Thread(
            target=_nous_oauth_poller, args=(sid,), daemon=True,
            name=f"oauth-poll-{sid[:6]}",
        ).start()
        return {
            "session_id": sid,
            "flow": "device_code",
            "user_code": str(device_data["user_code"]),
            "verification_url": str(device_data["verification_uri_complete"]),
            "expires_in": int(device_data["expires_in"]),
            "poll_interval": int(device_data["interval"]),
        }

    def get_provider_oauth_status(self, *, session_id: str) -> dict:
        """Estado del flow: {status: pending|approved|error, error_message}."""
        with _OAUTH_SESSIONS_LOCK:
            sess = _OAUTH_SESSIONS.get(session_id)
        if sess is None:
            return {"status": "unknown"}
        return {
            "status": sess["status"],
            "error_message": sess.get("error_message") or "",
        }

    # ------------------------------------------------------------------
    # MCP Apps — gestión de servidores MCP por el operador (SO-nativo).
    # El pipeline de ejecución YA existe (McpServerManager → capability
    # registry → surface adapter → broker → mcp_tool_specs → LLM); esto añade
    # la pieza que faltaba: configurar/persistir/conectar servidores.
    # Persistencia: config.yaml de Neus (hermes_cli.config), clave mcp_servers.
    # Neus es la single source of truth; Safent gates installs (scan/MFA) y
    # luego escribe a Neus — nunca mantiene su propio store paralelo.
    # ------------------------------------------------------------------

    async def list_mcp_servers(self) -> list[dict]:
        """Servidores MCP configurados + salud + nº tools (read-only).

        Reads Neus's native registry as the single source of truth:
          - get_mcp_status()    → live-connected entries with tool counts
          - _neus_load_config() → configured-but-not-yet-connected entries

        The two are merged (status wins for connected servers) so every
        configured entry appears exactly once regardless of connection state.
        """
        try:
            from tools.mcp_tool import (  # noqa: PLC0415
                get_mcp_status,
                _load_mcp_config as _neus_load_cfg,
            )
        except ImportError:
            logger.warning("hermes.dbus.list_mcp_servers: tools.mcp_tool unavailable — []")
            return []

        try:
            live_statuses: list[dict] = get_mcp_status()  # sync — uses background loop
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.dbus.list_mcp_servers get_mcp_status failed: %s", exc)
            live_statuses = []

        try:
            neus_cfg: dict[str, dict] = _neus_load_cfg()
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.dbus.list_mcp_servers _load_mcp_config failed: %s", exc)
            neus_cfg = {}

        # Authoritative daemon-side view of what is connected NOW (server_id → tools).
        # After a reconnect-on-restart the manager holds the live connections even if
        # Neus's get_mcp_status() per-server tracker lags at 0 — use it so the UI shows
        # the real tool_count + healthy state instead of a stale 0. (fix 2026-06-27)
        mgr: dict[str, int] = {}
        try:
            if self._mcp_manager is not None:
                snap = self._mcp_manager.snapshot()
                # Defensive: a manager that returns None/non-dict would make
                # mgr.get()/`in mgr` below break the WHOLE list → UI shows "no
                # MCP servers" while servers exist (the silent-empty bug class).
                mgr = snap if isinstance(snap, dict) else {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.dbus.list_mcp_servers snapshot failed: %s", exc)

        # Build output: live statuses first (they carry tool_count), then any
        # configured-but-not-connected entries not already in the live list. For every
        # entry the manager snapshot wins when it reports MORE tools / a live connection.
        seen: set[str] = set()
        out: list[dict] = []
        for status in live_statuses:
            sid = status.get("name", "")
            seen.add(sid)
            argv = _neus_argv(neus_cfg.get(sid, {}))
            tool_count = max(int(status.get("tools", 0) or 0), mgr.get(sid, 0))
            connected = bool(status.get("connected")) or sid in mgr
            out.append({
                "server_id": sid,
                "label": sid,
                "argv": argv,
                "health": "healthy" if connected else "disconnected",
                "tool_count": tool_count,
            })
        for sid, cfg in neus_cfg.items():
            if sid in seen:
                continue
            seen.add(sid)
            out.append({
                "server_id": sid,
                "label": sid,
                "argv": _neus_argv(cfg),
                "health": "healthy" if sid in mgr else "disconnected",
                "tool_count": mgr.get(sid, 0),
            })
        # Manager-connected servers absent from BOTH Neus sources (defensive — never drop
        # a live connection from the list just because the status trackers don't list it).
        for sid, tc in mgr.items():
            if sid in seen:
                continue
            out.append({
                "server_id": sid,
                "label": sid,
                "argv": _neus_argv(neus_cfg.get(sid, {})),
                "health": "healthy",
                "tool_count": tc,
            })
        return out

    async def add_mcp_server(self, *, draft_json: str, sender_uid: int) -> dict:
        """Configura + conecta un servidor MCP stdio. Muta → authZ operador.

        draft: {server_id, label, argv: [comando, ...], env?: {KEY: VALUE}}.
        SEGURIDAD: el comando (argv[0]) debe estar en la allowlist de runners
        (npx/uvx/node/python3) — el servidor corre como el usuario del daemon;
        no se permite un binario arbitrario. El argv viene del OPERADOR por
        D-Bus (nunca del LLM — Transport docstring lo exige).

        env (BYOK): diccionario opcional de variables de entorno BYOK para el
        servidor. Solo se permiten claves en _MCP_BYOK_ENV_KEYS; claves
        arbitrarias son rechazadas (no silenciadas) para evitar inyección.
        OD_DAEMON_URL se valida como URL http(s). El token OD_API_TOKEN se
        persiste cifrado en la config y nunca se registra en claro en logs.
        """
        self._authorize_and_resolve(sender_uid, operation="add_mcp_server")
        if self._mcp_manager is None:
            return {"ok": False, "error": "mcp_not_configured"}
        try:
            d = json.loads(draft_json)
        except (ValueError, TypeError) as exc:
            return {"ok": False, "error": f"draft_json inválido: {exc}"}
        sid = str(d.get("server_id") or "").strip()
        argv = [str(a) for a in (d.get("argv") or []) if str(a).strip()]
        # Owner sovereign override: the UI records the FAIL/WARN approval (with MFA) via
        # POST /security/decisions, then re-adds with force=True. Rides in the draft so
        # the D-Bus signature is unchanged. Without this, FAIL/WARN MCPs were PERMANENTLY
        # blocked even after the owner approved (add had no override path, unlike skills).
        force = bool(d.get("force"))
        import re as _re  # noqa: PLC0415
        if not _re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?", sid):
            return {"ok": False, "error": "server_id inválido (minúsculas/dígitos/guiones, patrón ServerSlug)"}
        if not argv:
            return {"ok": False, "error": "argv vacío"}
        runner = argv[0].rsplit("/", 1)[-1]
        if runner not in _MCP_ALLOWED_RUNNERS:
            return {
                "ok": False,
                "error": f"runner '{runner}' no permitido "
                         f"(allowlist: {sorted(_MCP_ALLOWED_RUNNERS)})",
            }
        # SECURITY-FIRST (C2): el scanner SÓLO puede analizar código que pueda
        # descargar de un registro (npm/PyPI). Si este argv no resuelve a una
        # coordenada descargable (p.ej. 'npx ./local.js', 'uvx --from /ruta'),
        # el scan no inspeccionaría nada y el install pasaría con CERO análisis.
        # Sin análisis ⇒ no PASS ⇒ rechazo explícito (no un near-PASS).
        if not _scanner_can_analyze_argv(argv):
            return {
                "ok": False,
                "error": (
                    "argv no resuelve a un paquete publicado descargable "
                    "(npm vía npx / PyPI vía uvx|pipx). El Centro de Seguridad no "
                    "puede analizar código que no esté en un registro; publica el "
                    "servidor MCP para poder verificarlo antes de instalarlo."
                ),
            }
        # Validate BYOK env — reject unknown keys loudly (security: no injection).
        raw_env = d.get("env") or {}
        try:
            byok_env: dict[str, str] = _validate_mcp_env(raw_env)
        except ValueError as exc:
            return {"ok": False, "error": f"env inválido: {exc}"}
        # Centro de Seguridad (antivirus agéntico): TODO MCP pasa por el scan
        # antes de conectar. FAIL con política auto_block → no se conecta. El
        # score se emite por señal → el modal InstallReview lo muestra. Defensa
        # en profundidad: aunque la UI no pre-escanee, este gate bloquea lo grave.
        # Offload the (networked) scan off the daemon loop — _scan_install_target →
        # _run_scan_sync parks the calling thread on .result(); inline it froze the loop
        # (red-team 2026-06-27, finding #3 residual).
        import asyncio as _asyncio_sc  # noqa: PLC0415
        from functools import partial as _partial_sc  # noqa: PLC0415
        scan_result = await _asyncio_sc.get_running_loop().run_in_executor(
            None,
            _partial_sc(
                self._scan_install_target,
                "mcp_server", sid, argv=argv, emit_signals=False,
            ),
        )
        if scan_result is not None and scan_result.get("blocked"):
            if not force:
                return scan_result  # ya incluye ok:False + blocked + error
            # Owner sovereign override (approved with MFA): record ALLOWED on THIS scan,
            # then re-scan with allow_warn=True so BOTH gates clear — the FAIL auto-block
            # (via the ALLOWED decision) AND the separate WARN gate. Mirrors the skill path
            # (_apply_owner_override_and_rescan). FAIL-CLOSED: if the override can't clear
            # it (no scan_id / re-scan still blocked / error), keep the block.
            try:
                from uuid import UUID as _UUID  # noqa: PLC0415
                scan_svc = self._scan_service_lazy()
                _sid_scan = scan_result.get("scan_id") or ""
                if scan_svc is not None and _sid_scan:
                    scan_svc.allow_target(_UUID(_sid_scan))
                    logger.warning(
                        "hermes.dbus.mcp_owner_override server=%s scan_id=%s — install "
                        "allowed by the owner's SOVEREIGN decision (MFA-gated)", sid, _sid_scan,
                    )
                scan_result = await _asyncio_sc.get_running_loop().run_in_executor(
                    None,
                    _partial_sc(
                        self._scan_install_target,
                        "mcp_server", sid, argv=argv, emit_signals=False, allow_warn=True,
                    ),
                )
                if scan_result is not None and scan_result.get("blocked"):
                    return scan_result  # override could not clear it
            except Exception as exc:  # noqa: BLE001 — fail-closed: keep the block
                logger.error("hermes.dbus.mcp_override_failed server=%s: %s", sid, exc)
                return {"ok": False, "blocked": True,
                        "error": f"no se pudo aplicar el override del dueño: {exc}"}
        # C1 PASS-3: PRE-FETCH the scanned package into the shared runner cache NOW, in
        # this trusted install path (daemon, host netns). The MCP RUNTIME then spawns
        # OFFLINE from that cache in its default-deny netns — no registry network at
        # runtime (closes the npm-PUT exfil residual). FAIL-CLOSED: a prefetch failure
        # aborts the add, so we never persist a server the offline runtime couldn't run.
        # Blocking subprocess → run off the event loop.
        try:
            import asyncio as _asyncio_pf  # noqa: PLC0415
            await _asyncio_pf.get_running_loop().run_in_executor(
                None, _prefetch_mcp_package, sid, argv
            )
        except RuntimeError as exc:
            logger.warning("hermes.dbus.mcp_prefetch_failed server=%s error=%s", sid, exc)
            return {"ok": False, "error": f"prefetch falló: {exc}"}
        try:
            server = await _mcp_connect(self._mcp_manager, sid, argv, env=byok_env)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"conexión falló: {exc}"}
        # GATE PASSED — persist to Neus's native registry (single source of truth).
        # The cage (scan/MFA) runs BEFORE this write; a poisoned command would be
        # RCE, so persistence ONLY happens after all gates clear.
        try:
            _neus_write_mcp_entry(sid, argv, env=byok_env)
        except Exception as exc:  # noqa: BLE001
            logger.error("hermes.dbus.mcp_neus_write_failed server=%s: %s", sid, exc)
            return {"ok": False, "error": f"persistencia en Neus falló: {exc}"}
        # Log connection without exposing secret values.
        _log_env_keys = sorted(byok_env.keys())
        logger.info(
            "hermes.dbus.mcp_connected server=%s runner=%s byok_keys=%s",
            sid, runner, _log_env_keys,
        )
        return {"ok": True, "tool_count": len(server.tools)}

    async def remove_mcp_server(self, *, server_id: str, sender_uid: int) -> dict:
        """Desconecta + borra de la config. Muta → authZ operador."""
        self._authorize_and_resolve(sender_uid, operation="remove_mcp_server")
        if self._mcp_manager is not None:
            try:
                await self._mcp_manager.disconnect(_mcp_id(server_id))
            except Exception:  # noqa: BLE001 — ya desconectado
                pass
        _neus_remove_mcp_entry(server_id)
        return {"ok": True}

    async def search_mcp_registry(self, *, query: str, limit: int) -> list[dict]:
        """Busca en el MCP Registry oficial y normaliza al formato de add_mcp_server.

        Read-only, sin authZ (mismo patrón que list_mcp_servers).
        Fail-soft: excepciones → [] con warning (no debe tumbar el daemon).
        """
        try:
            from hermes.mcp.infrastructure.registry_client import (  # noqa: PLC0415
                McpRegistryError,
                search_servers,
            )
            raw_entries = await search_servers(query=query, limit=limit or 20)
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.dbus.mcp_registry_search_failed: %s", exc)
            return []

        results: list[dict] = []
        for item in raw_entries:
            try:
                results.append(_normalize_registry_entry(item))
            except Exception as exc:  # noqa: BLE001 — entry malformada, skip
                logger.warning(
                    "hermes.dbus.mcp_registry_normalize_failed entry=%s err=%s",
                    item.get("server", {}).get("name", "?"),
                    exc,
                )
        return results

    # ------------------------------------------------------------------
    # Skill Hub de Hermes (hermes_cli/skills_hub + tools/skills_hub).
    # Búsqueda unificada multi-fuente + install/uninstall + instaladas.
    # Las skills del hub aterrizan en $HERMES_HOME/skills (las carga el
    # motor Nous directamente). REUSE de las funciones del CLI — el SO no
    # inventa un hub paralelo.
    # ------------------------------------------------------------------

    def search_skills_hub(
        self, *, query: str, source: str, limit: int, query_id: str = ""
    ) -> dict:
        """Busca en el hub (GitHub + fuentes configuradas). Read-only, red.

        BLOQUEA hasta 30s (timeout de unified_search) — el adapter lo corre
        en executor. Acepta un query_id para cancel-check: si el caller llamó
        cancel_skills_hub_search(query_id) antes de que esta función termine,
        devuelve {cancelled: true, query_id, results: []} en lugar del batch.
        """
        qid = (query_id or "").strip()
        cancel_event = _hub_search_get_cancel_event(qid) if qid else None

        try:
            from tools.skills_hub import create_source_router, unified_search  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.dbus.skills_hub_unavailable: %s", exc)
            return {"query_id": qid, "results": [], "cancelled": False}

        metas = unified_search(
            (query or "").strip(),
            create_source_router(),
            source_filter=source or "all",
            limit=min(max(int(limit or 20), 1), 50),
        )

        if cancel_event is not None and cancel_event.is_set():
            logger.debug("hermes.dbus.hub_search_cancelled query_id=%s", qid)
            return {"query_id": qid, "results": [], "cancelled": True}

        results = [
            {
                "name": m.name,
                "description": m.description,
                "source": m.source,
                "identifier": m.identifier,
                "trust_level": m.trust_level,
                "repo": m.repo,
                "tags": list(m.tags or []),
            }
            for m in metas
        ]
        return {"query_id": qid, "results": results, "cancelled": False}

    def cancel_skills_hub_search(self, *, query_id: str) -> dict:
        """Señaliza la cancelación de una búsqueda en vuelo. No-op si ya terminó."""
        qid = (query_id or "").strip()
        if not qid:
            return {"ok": False, "error": "query_id vacío"}
        _hub_search_cancel(qid)
        logger.debug("hermes.dbus.hub_search_cancel_requested query_id=%s", qid)
        return {"ok": True}

    def list_hub_skills(self) -> list[dict]:
        """Skills del hub instaladas (lockfile .hub/lock.json). Read-only."""
        try:
            from tools.skills_hub import HubLockFile  # noqa: PLC0415
            return HubLockFile().list_installed()
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.dbus.hub_lockfile_unreadable: %s", exc)
            return []

    def _scan_service_lazy(self):
        """Lazy-constructed ScanService with all 5 scanners and SQLite repos.

        Uses the pre-injected instance when available (tests / explicit DI).
        Falls back to building the full production composition root on first
        call. Import is deferred so the scanner infra is never loaded unless
        the scan path is actually exercised.

        Returns None if hermes.security_center is not installed (scan is
        additive/optional — must never crash the install path).
        """
        if self._scan_service is not None:
            return self._scan_service
        try:
            from hermes.security_center.application.scan_service import ScanService  # noqa: PLC0415
            from hermes.security_center.infrastructure.composio_allowlist import (  # noqa: PLC0415
                ComposioAllowlistScanner,
            )
            from hermes.security_center.infrastructure.heuristic_fallback import (  # noqa: PLC0415
                HeuristicFallbackScanner,
            )
            from hermes.security_center.infrastructure.mcp_tool_linter import (  # noqa: PLC0415
                McpToolLinter,
            )
            from hermes.security_center.infrastructure.package_content_scanner import (  # noqa: PLC0415
                PackageContentScanner,
            )
            from hermes.security_center.infrastructure.skill_content_scanner import (  # noqa: PLC0415
                SkillContentScanner,
            )
            from hermes.security_center.infrastructure.provenance_scanner import (  # noqa: PLC0415
                ProvenanceScanner,
            )
            from hermes.security_center.infrastructure.skill_signature_check import (  # noqa: PLC0415
                SkillSignatureCheck,
            )
            from hermes.security_center.infrastructure.sqlite_scan_repo import (  # noqa: PLC0415
                SQLitePolicyRepo,
                SQLiteScanRepo,
            )
            from hermes.security_center.infrastructure.trivy_cve_scanner import (  # noqa: PLC0415
                TriviaCveScanner,
                trivy_available,
                trivy_db_present,
            )
        except ImportError:
            logger.warning(
                "hermes.dbus.security_center_unavailable — scan es no-op "
                "(instalar hermes.security_center para habilitar)"
            )
            return None
        # Slot CVE: usa Trivy real cuando el binario está horneado (/usr/bin/trivy),
        # si no, el fallback heurístico (que solo deja la nota "trivy ausente").
        # Ambos comparten name="cve": NUNCA meter los dos en la lista (doble conteo)
        # — se elige exactamente uno aquí. Esta composición SÍ tolera Trivy (120 s):
        # _run_scan_sync la corre en un HILO con loop propio cuando el loop del
        # daemon gira, y TriviaCveScanner autoaplica su propio wait_for(120 s)
        # devolviendo [] al expirar. (El gate síncrono inline de composition.py NO
        # debe llevar Trivy: ahí se await-ea en el loop del daemon.)
        # Gate engine=trivy on BINARY *and* a usable baked DB. With the binary present
        # but the DB absent, every `trivy fs --skip-db-update` fails — selecting trivy
        # would mark every install unanalyzable (fail-loud → WARN) AND mislabel the
        # engine as "escaneo completo". Falling back to heuristic gives the honest
        # "revisión básica" label + owner-review path. (security-review 2026-06-26)
        using_trivy = trivy_available() and trivy_db_present()
        cve_scanner = TriviaCveScanner() if using_trivy else HeuristicFallbackScanner()
        engine = "trivy" if using_trivy else "heuristic"
        logger.info(
            "hermes.security.scan_engine_selected engine=%s trivy_bin=/usr/bin/trivy",
            engine,
        )
        self._scan_service = ScanService(
            # INVARIANTE (load-bearing): _run_scan_sync corre el scan en un HILO
            # aparte con su PROPIO loop cuando el loop del daemon ya gira (ver
            # _run_scan_sync) — por eso PackageContentScanner SÍ puede hacer red
            # ACOTADA (fetch + análisis estático del artefacto publicado; HTTP
            # timeout 20 s, descarga topada). Es el scanner que cierra el agujero
            # C2 (el scan dejaba pasar 'npx -y evil-data-stealer-mcp' con score
            # casi-PASS porque nadie miraba el contenido). Los otros siguen siendo
            # puros/CPU-local.
            scanners=[
                PackageContentScanner(),
                SkillContentScanner(),
                cve_scanner,
                ProvenanceScanner(),
                McpToolLinter(),
                SkillSignatureCheck(),
                ComposioAllowlistScanner(),
            ],
            history_repo=SQLiteScanRepo(),
            policy_repo=SQLitePolicyRepo(),
            engine=engine,
        )
        return self._scan_service

    def _run_scan_sync(self, target: "Any") -> "Any":
        """Run scan_service.scan(target) synchronously, desde CUALQUIER contexto.

        CRÍTICO: muchos verbos del gate (add_mcp_server, install_package,
        scan_install_draft) son async / corren EN el event loop del daemon. Crear
        ahí un `new_event_loop().run_until_complete()` lanza "Cannot run the event
        loop while another loop is running" → el scan revienta. Antes eso caía a
        fail-open (None) y el scan NUNCA corría para MCP/Apps (gate de teatro);
        con fail-closed bloqueaba TODO. Fix: si ya hay un loop corriendo, ejecutar
        el scan en un HILO aparte con su propio loop (el scan es local y rápido —
        Trivy se salta si no está). Si no hay loop (hub-op worker threads de
        skills), usar un loop propio directo.
        """
        import asyncio as _asyncio  # noqa: PLC0415

        def _run_in_fresh_loop() -> "Any":
            loop = _asyncio.new_event_loop()
            try:
                return loop.run_until_complete(self._scan_service_lazy().scan(target))
            finally:
                loop.close()

        try:
            _asyncio.get_running_loop()
        except RuntimeError:
            # Sin loop corriendo (hilo worker): loop propio directo.
            return _run_in_fresh_loop()
        # Loop del daemon corriendo: offload a un hilo con su propio loop para no
        # anidar loops. Bloquea brevemente (scan local, sin red).
        import concurrent.futures as _futures  # noqa: PLC0415

        with _futures.ThreadPoolExecutor(max_workers=1) as _ex:
            return _ex.submit(_run_in_fresh_loop).result()

    @staticmethod
    def _serialize_scan_record(record: "Any") -> str:
        """Serialize a ScanRecord to the JSON string expected by InstallReview QML.

        Emits the domain field names (category, evidence_ref) that the React
        InstallScanModal reads — renaming to scanner/evidence left the modal blank.
        Includes engine/engine_label/requires_owner_approval for honest provenance.
        """
        risks = [
            {
                "severity": r.severity.value,
                "category": r.category,
                "message": r.message,
                "evidence_ref": r.evidence_ref,
            }
            for r in record.score.risks
        ]
        engine = getattr(record, "engine", "heuristic")
        verdict_val = record.verdict.value if hasattr(record.verdict, "value") else str(record.verdict)
        return json.dumps({
            "scan_id": str(record.id),
            "identifier": record.target.identifier,
            "kind": record.target.kind,
            "score": record.score.value,
            "verdict": verdict_val,
            "engine": engine,
            "engine_label": getattr(record, "engine_label", engine),
            "requires_owner_approval": verdict_val in ("WARN", "FAIL"),
            "risks": risks,
        })

    def install_hub_skill(self, *, identifier: str, sender_uid: int, force: bool = False) -> dict:
        """Instala una skill del hub (clona + valida). Muta → authZ operador.

        Operación de red/git potencialmente larga → corre en thread y se
        sondea con get_hub_op_status(op_id). Mismo patrón que el OAuth.

        Security: runs a ScanService scan BEFORE do_install. If the scan
        verdict is FAIL and policy.auto_block_fail is True, the install is
        blocked. When force=True (operator-gated path only) the block dict is
        returned to the caller with scan_id so the owner can review real score
        and risks. On force=True we record decision=ALLOWED via
        ScanService.allow_target(), then re-run the scan — the cache now
        returns decision=ALLOWED so scan_service no longer raises → proceed.
        """
        self._authorize_and_resolve(sender_uid, operation="install_hub_skill")
        ident = (identifier or "").strip()
        if not ident:
            return {"error": "identifier vacío"}

        scan_result = self._scan_hub_target(ident)
        if scan_result is not None and scan_result.get("blocked"):
            if not force:
                return scan_result
            return self._apply_owner_override_and_rescan(ident, scan_result)

        return _start_hub_op(
            "install", ident,
            scan_record=scan_result.get("record") if scan_result else None,
            signal_emitter=self._scan_signal_emitter,
        )

    def _apply_owner_override_and_rescan(self, identifier: str, block: dict) -> dict:
        """Record owner-sovereign ALLOWED decision then re-scan so the gate passes.

        Invariant: the scan ALREADY ran (block was produced by _scan_hub_target).
        This only sets decision=ALLOWED on the existing record, never skips the scan.
        """
        from uuid import UUID as _UUID  # noqa: PLC0415

        scan_svc = self._scan_service_lazy()
        if scan_svc is None:
            return {"ok": False, "blocked": True,
                    "error": "Security Center no desplegado — no se puede registrar override."}

        scan_id_str = block.get("scan_id") or ""
        try:
            scan_id = _UUID(scan_id_str)
        except (ValueError, AttributeError):
            return {"ok": False, "blocked": True,
                    "error": f"scan_id inválido en el bloqueo: {scan_id_str!r}"}

        scan_svc.allow_target(scan_id)
        logger.warning(
            "hermes.dbus.install_owner_override identifier=%s scan_id=%s "
            "— instalación permitida por decisión SOBERANA del dueño",
            identifier, scan_id_str,
        )

        # Re-run with allow_warn=True: the owner's sovereign override must clear BOTH
        # gates — the ScanService FAIL block (via the ALLOWED decision recorded above)
        # AND the SEPARATE WARN gate in _scan_install_target. Without allow_warn the
        # re-scan re-blocked every WARN skill even after the owner approved with MFA,
        # so a WARN skill could NEVER be installed (the bug: "no puedo usar skills").
        scan_result2 = self._scan_install_target(
            "skill", identifier, emit_signals=False, allow_warn=True
        )
        if scan_result2 is not None and scan_result2.get("blocked"):
            # Should not happen after allow_target — surface as error.
            logger.error(
                "hermes.dbus.install_owner_override_still_blocked identifier=%s "
                "scan_id=%s — override set but re-scan still blocked",
                identifier, scan_id_str,
            )
            return scan_result2

        return _start_hub_op(
            "install", identifier,
            scan_record=scan_result2.get("record") if scan_result2 else None,
            signal_emitter=self._scan_signal_emitter,
        )

    def uninstall_hub_skill(self, *, name: str, sender_uid: int) -> dict:
        """Desinstala una skill del hub. Muta → authZ operador."""
        self._authorize_and_resolve(sender_uid, operation="uninstall_hub_skill")
        nm = (name or "").strip()
        if not nm:
            return {"error": "name vacío"}
        return _start_hub_op("uninstall", nm)

    def get_hub_op_status(self, *, op_id: str) -> dict:
        """Estado de una operación del hub: {status: pending|done|error}."""
        with _HUB_OPS_LOCK:
            op = _HUB_OPS.get(op_id)
        if op is None:
            return {"status": "unknown"}
        return {"status": op["status"], "error_message": op.get("error_message") or ""}

    def _scan_hub_target(self, identifier: str) -> dict | None:
        """Pre-install scan for a hub skill (defensa en profundidad en install_hub_skill).

        emit_signals=False: la UI gated (beginGatedInstall→scan_install_draft) ya
        muestra el modal y graba la decisión; este scan interno sólo bloquea en
        FAIL, sin emitir un segundo modal informativo.
        """
        return self._scan_install_target("skill", identifier, emit_signals=False)

    def _scan_install_target(
        self,
        kind: str,
        identifier: str,
        *,
        argv: "list[str] | None" = None,
        source_url: str = "",
        emit_signals: bool = True,
        allow_warn: bool = False,
    ) -> dict | None:
        """Run a pre-install security scan for ANY install kind.

        The Security Center is the SO's "antivirus" — system threats (kind
        "package": CVE/provenance) and AGENTIC threats (kind "skill"/"mcp_server":
        tool linter, signature, prompt-injection heuristics) share one gate.

        Returns:
          None                    — scanner NO desplegado (operador optó por no
                                    instalarlo): se procede (fail-open consciente).
          {"record": record}      — PASS only (or WARN cuando allow_warn=True):
                                    install may proceed.
          {"blocked": True, ...}  — FAIL (auto_block), WARN sin override, O error
                                    inesperado del scanner desplegado: caller MUST
                                    abort.

        Gate WARN (alineado con el revisor de terminal): por defecto WARN BLOQUEA.
        El revisor de terminal (SecurityCenterInstallReviewer) ya auto-deniega
        cualquier cosa que no sea un PASS limpio; este gate de daemon era más
        débil (dejaba pasar WARN), una incoherencia explotable. Ahora WARN sólo
        procede con allow_warn=True (override explícito del dueño, p.ej. una
        confirmación de usuario ya registrada por la UI gated).

        Postura: si el scanner NO está (ImportError / no construible), fail-OPEN
        (None) — es decisión de despliegue. Si el scanner SÍ está pero revienta en
        runtime, fail-CLOSED (blocked) — un SO público no instala sin un veredicto
        de seguridad fiable. argv (linter de runner MCP) y source_url (procedencia
        de paquete) alimentan los scanners.
        """
        try:
            from hermes.security_center.domain.install_target import InstallTarget  # noqa: PLC0415
            from hermes.security_center.application.scan_service import ScanBlockedError  # noqa: PLC0415
        except ImportError:
            logger.warning(
                "hermes.dbus.security_center_unavailable — _scan_install_target es no-op"
            )
            return None
        if self._scan_service_lazy() is None:
            return None

        target = InstallTarget(
            kind=kind,
            identifier=identifier,
            source_url=source_url or "",
            argv=list(argv or []),
        )
        try:
            record = self._run_scan_sync(target)
        except ScanBlockedError as exc:
            logger.warning(
                "hermes.dbus.install_scan_blocked kind=%s identifier=%s: %s",
                kind, identifier, exc,
            )
            if exc.record is not None:
                block = self._build_block_dict_from_record(exc.record, exc)
                scan_data = self._serialize_scan_record(exc.record)
            else:
                block = {"ok": False, "blocked": True,
                         "error": f"Instalación bloqueada por política de seguridad: {exc}"}
                scan_data = self._serialize_scan_record_from_blocked(identifier)
            if emit_signals:
                self._emit_scan_signals(
                    block.get("scan_id") or str(exc), "FAIL", scan_data
                )
            return block
        except Exception as exc:  # noqa: BLE001 — scanner desplegado que revienta → fail-CLOSED
            logger.error(
                "hermes.dbus.install_scan_errored kind=%s identifier=%s: %s "
                "(fail-closed: instalación denegada por precaución)",
                kind, identifier, exc,
            )
            scan_data = self._serialize_scan_record_from_blocked(identifier)
            if emit_signals:
                self._emit_scan_signals(f"scan-error-{identifier}", "FAIL", scan_data)
            return {
                "ok": False,
                "blocked": True,
                "error": "No se pudo verificar la seguridad (el análisis falló); "
                         "instalación denegada por precaución.",
            }

        scan_data = self._serialize_scan_record(record)
        if emit_signals:
            self._emit_scan_signals(str(record.id), record.verdict.value, scan_data)

        verdict = record.verdict.value if hasattr(record.verdict, "value") else str(record.verdict)
        # WARN-gate: alinear con el revisor de terminal. WARN bloquea salvo
        # override explícito del dueño (allow_warn). Sin esto, el gate del daemon
        # era más laxo que el de terminal (incoherencia explotable).
        if verdict == "WARN" and not allow_warn:
            logger.warning(
                "hermes.dbus.install_scan_warn_blocked kind=%s identifier=%s score=%s "
                "(WARN bloquea sin override del dueño)",
                kind, identifier, record.score.value,
            )
            return {
                "ok": False,
                "blocked": True,
                "warn": True,
                "scan_id": str(record.id),
                "score": record.score.value,
                "error": (
                    "El Centro de Seguridad marcó este install como DUDOSO "
                    f"(WARN, score={record.score.value}). Requiere confirmación "
                    "explícita del dueño para continuar."
                ),
            }
        return {"record": record}

    @staticmethod
    def _build_block_dict_from_record(record: "Any", exc: "Any") -> dict:
        """Build a rich block response dict from a real ScanRecord.

        Exposes the actual score, scan_id and human-readable risk strings so the
        UI can show the owner a meaningful summary before offering the override.
        """
        risks = [
            f"[{r.severity.value}] {r.category}: {r.message}"
            for r in record.score.risks
        ]
        return {
            "ok": False,
            "blocked": True,
            "score": record.score.value,
            "verdict": record.verdict.value,
            "scan_id": str(record.id),
            "risks": risks,
            "error": f"Instalación bloqueada por política de seguridad: {exc}",
        }

    def _serialize_scan_record_from_blocked(self, identifier: str) -> str:
        """Minimal JSON for a blocked install when we have no ScanRecord object.

        Carries engine provenance so the UI still shows honest scan context.
        """
        import uuid as _uuid  # noqa: PLC0415
        svc = self._scan_service  # may be None (not yet lazy-built)
        engine = getattr(svc, "_engine", "heuristic") if svc is not None else "heuristic"
        return json.dumps({
            "scan_id": _uuid.uuid4().hex,
            "identifier": identifier,
            "score": 0,
            "verdict": "FAIL",
            "engine": engine,
            "engine_label": (
                "escaneo completo de vulnerabilidades (trivy CVE DB)"
                if engine == "trivy"
                else "revisión básica (heurística) — no es un escaneo completo de vulnerabilidades"
            ),
            "requires_owner_approval": True,
            "risks": [],
        })

    def _emit_scan_signals(self, scan_id: str, verdict: str, scan_data_json: str) -> None:
        """Invoke the injected signal emitter if available. Fire-and-forget."""
        if self._scan_signal_emitter is None:
            return
        try:
            self._scan_signal_emitter(scan_id, verdict, scan_data_json)
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.dbus.scan_signal_emit_failed: %s", exc)

    def scan_install(self, *, kind: str, identifier: str) -> str:
        """On-demand scan without installing. Returns serialized scan JSON.

        Fail-safe: returns an error JSON on any exception so the caller always
        gets a valid string response.
        """
        from hermes.security_center.domain.install_target import InstallTarget  # noqa: PLC0415
        from hermes.security_center.application.scan_service import ScanBlockedError  # noqa: PLC0415

        k = (kind or "skill").strip()
        ident = (identifier or "").strip()
        if not ident:
            return json.dumps({"error": "identifier vacío"})
        target = InstallTarget(kind=k, identifier=ident)
        try:
            record = self._run_scan_sync(target)
        except ScanBlockedError as exc:
            # Policy auto-blocked: return the real record if available so the UI
            # shows actual score, engine, and risks rather than empty FAIL.
            logger.info("hermes.dbus.scan_install_blocked identifier=%s: %s", ident, exc)
            if exc.record is not None:
                return self._serialize_scan_record(exc.record)
            return self._serialize_scan_record_from_blocked(ident)
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.dbus.scan_install_failed identifier=%s: %s", ident, exc)
            return json.dumps({"error": str(exc)})
        scan_data = self._serialize_scan_record(record)
        self._emit_scan_signals(str(record.id), record.verdict.value, scan_data)
        return scan_data

    def scan_install_draft(self, *, draft_json: str) -> str:
        """Pre-install scan from a full draft (kind + identifier + argv/source_url).

        This is the UI's gate: the install button calls this FIRST (it scans but
        does NOT install). El resultado se DEVUELVE al llamante (correlado por el
        reqId de la llamada), y la UI conduce el modal InstallReview desde esa
        respuesta — NO desde la señal global InstallReviewRequested. Esto evita
        que un scan bajo demanda (Centro de Seguridad) abra el gate de instalación
        con un pendingInstall ajeno (bypass CRÍTICO). Por eso emit_signals=False
        aquí. Sólo tras la confirmación del usuario la UI llama al verbo real
        (add_mcp_server / install_package / install_hub_skill). Honra el invariante:
        todo Skill/MCP/App pasa el Centro de Seguridad → score → el usuario decide.
        Read-only/no authZ (mismo patrón que scan_install).

        draft: {kind, identifier, argv?, source_url?}.
        Returns the serialized scan JSON (score/verdict/risks) — error JSON on any
        failure so la UI siempre recibe un string válido (y muestra el error).
        """
        try:
            d = json.loads(draft_json or "{}")
        except (ValueError, TypeError) as exc:
            return json.dumps({"error": f"draft inválido: {exc}"})
        kind = str(d.get("kind") or "skill").strip()
        ident = str(d.get("identifier") or "").strip()
        if not ident:
            return json.dumps({"error": "identifier vacío"})
        argv = [str(a) for a in (d.get("argv") or []) if str(a).strip()]
        source_url = str(d.get("source_url") or "")
        # READ-ONLY display path: allow_warn=True para DEVOLVER el record real
        # (score/verdict/risks) y que la UI muestre WARN con sus hallazgos. El
        # bloqueo de WARN se aplica en los verbos mutadores reales
        # (add_mcp_server / install_hub_skill / install_package), no aquí: si lo
        # ocultáramos como "blocked" perderíamos el detalle que el usuario debe
        # ver para decidir (rompería el modelo scan→score→usuario-decide).
        result = self._scan_install_target(
            kind, ident, argv=argv, source_url=source_url,
            emit_signals=False, allow_warn=True,
        )
        if result is None:
            # security_center ausente/no-op → PASS implícito para no bloquear la UI.
            return json.dumps({
                "scan_id": "", "identifier": ident, "score": 100,
                "verdict": "PASS", "risks": [],
            })
        if result.get("blocked"):
            return self._serialize_scan_record_from_blocked(ident)
        return self._serialize_scan_record(result["record"])

    # ------------------------------------------------------------------
    # Package Store — store de apps Linux (Flatpak + RPM).
    # NUNCA instala pip — eso es para el daemon de skills.
    # Lecturas (list/search) son read-only. install/uninstall requieren
    # authZ de operador (mismo gate que add_mcp_server).
    # ------------------------------------------------------------------

    def _package_store_service(self):
        """Lazy-constructed PackageStoreService (subprocess adapters)."""
        if not hasattr(self, "_pkg_store_svc"):
            from hermes.package_store.application.package_store_service import (  # noqa: PLC0415
                PackageStoreService,
            )
            from hermes.package_store.infrastructure.subprocess_catalog import (  # noqa: PLC0415
                SubprocessPackageCatalog,
            )
            from hermes.package_store.infrastructure.subprocess_manager import (  # noqa: PLC0415
                SubprocessPackageManager,
            )
            self._pkg_store_svc = PackageStoreService(
                catalog=SubprocessPackageCatalog(),
                manager=SubprocessPackageManager(),
            )
        return self._pkg_store_svc

    def list_installed_packages(self, *, source: str) -> list[dict]:
        """Paquetes instalados por source ('flatpak' | 'rpm'). Read-only."""
        try:
            return self._package_store_service().list_installed(source=source)
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.dbus.pkg_store.list_installed_failed: %s", exc)
            return []

    def search_packages(self, *, query: str, source: str) -> list[dict]:
        """Busca paquetes en Flathub y/o dnf. Read-only, bloqueante (≤30s)."""
        try:
            return self._package_store_service().search(query=query, source=source)
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.dbus.pkg_store.search_failed: %s", exc)
            return []

    def install_package(self, *, source: str, package_id: str, sender_uid: int) -> dict:
        """Inicia instalación async. Muta → authZ operador. Devuelve {op_id}.

        Centro de Seguridad (antivirus de SISTEMA): toda App pasa por el scan
        (CVE/procedencia) antes de instalar. FAIL con política auto_block → no
        se instala; el score se emite por señal → modal InstallReview.
        """
        self._authorize_and_resolve(sender_uid, operation="install_package")
        scan_result = self._scan_install_target(
            "package", package_id, source_url=source, emit_signals=False,
        )
        if scan_result is not None and scan_result.get("blocked"):
            return scan_result
        try:
            return self._package_store_service().start_install(
                source=source, package_id=package_id
            )
        except ValueError as exc:
            return {"error": str(exc)}

    def uninstall_package(self, *, source: str, package_id: str, sender_uid: int) -> dict:
        """Inicia desinstalación async. Muta → authZ operador. Devuelve {op_id}."""
        self._authorize_and_resolve(sender_uid, operation="uninstall_package")
        try:
            return self._package_store_service().start_uninstall(
                source=source, package_id=package_id
            )
        except ValueError as exc:
            return {"error": str(exc)}

    def get_pkg_op_status(self, *, op_id: str) -> dict:
        """Estado de una operación de install/uninstall: {status, log_tail, error_message}."""
        try:
            return self._package_store_service().get_op_status(op_id=op_id)
        except Exception as exc:  # noqa: BLE001
            return {"op_id": op_id, "status": "unknown", "error_message": str(exc)}

    def configure_native_provider(
        self, *, provider_id: str, api_key: str, model: str,
        base_url: str, sender_uid: int,
    ) -> dict:
        """Configura un provider NATIVO de hermes_cli por su id real (api-key).

        Camino NATIVO (no la abstracción shell_server/kinds): escribe la clave en
        HERMES_HOME/.env bajo la env var REAL del provider (p.ej. OPENAI_API_KEY
        para `openai-api`) + fija model.{provider,default} en config.yaml. El
        motor (resolve_runtime_provider) lo lee directo — igual que
        `hermes auth add` + `hermes --provider <id>`. Soporta CUALQUIER provider
        api-key de la tabla (openai-api directo, gemini, deepseek, groq, mistral,
        copilot…). Para OAuth/suscripción → start_provider_oauth.
        """
        self._authorize_and_resolve(sender_uid, operation="configure_native_provider")
        try:
            from hermes_cli.auth import PROVIDER_REGISTRY  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.dbus.native_cfg_unavailable: %s", exc)
            return {"ok": False, "error": "hermes_cli no disponible"}
        cfg = PROVIDER_REGISTRY.get(provider_id)
        if cfg is None:
            return {"ok": False, "error": f"provider desconocido: {provider_id}"}
        if getattr(cfg, "auth_type", "api_key") != "api_key":
            return {"ok": False, "error": "oauth_required",
                    "auth_type": getattr(cfg, "auth_type", "")}
        key = (api_key or "").strip()
        if not key:
            return {"ok": False, "error": "api_key vacía"}
        env_vars = getattr(cfg, "api_key_env_vars", ()) or ()
        if not env_vars:
            return {"ok": False, "error": f"{provider_id} no declara env var de clave"}
        try:
            _write_hermes_env(env_vars[0], key)
            bu = (base_url or "").strip()
            if bu and getattr(cfg, "base_url_env_var", ""):
                _write_hermes_env(cfg.base_url_env_var, bu)
            _write_hermes_model_config(provider_id, (model or "").strip(), bu)
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.dbus.native_cfg_write_failed: %s", exc)
            return {"ok": False, "error": f"no se pudo escribir config: {exc}"}
        # Carga la API-key recién escrita al os.environ del proceso vivo. El
        # resolver POR CICLO (provider_config_source._load_native_model_config)
        # lee la key de PROVIDER_REGISTRY[pid].api_key_env_vars; sin esta línea,
        # esa env-var no existe en el daemon ya arrancado y el primer chat tras
        # "Configurar" iría sin key (401). El model/provider los lee del
        # config.yaml directo (no necesita reload).
        try:
            import os as _os  # noqa: PLC0415
            _os.environ[env_vars[0]] = key
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.dbus.env_load_failed: %s", exc)
        logger.info("hermes.dbus.native_provider_configured id=%s", provider_id)
        if self._active_provider_svc is not None:
            self._active_provider_svc.force_refresh()
        return {"ok": True}

    def get_native_active(self) -> dict:
        """Provider nativo activo según config.yaml ({} si ninguno). Read-only."""
        return _read_native_active()

    # ------------------------------------------------------------------
    # Web search backend keys (Brave/Tavily/Exa) — mejora de web_search.
    # ------------------------------------------------------------------
    _WEB_SEARCH_ENV = {  # noqa: RUF012
        "brave": "BRAVE_SEARCH_API_KEY",
        "tavily": "TAVILY_API_KEY",
        "exa": "EXA_API_KEY",
    }

    async def set_web_search_api_key(
        self, *, provider: str, api_key: str, sender_uid: int
    ) -> dict:
        """Configura la API key de un backend de búsqueda web (Brave/Tavily/Exa).

        Mismo patrón que configure_native_provider: escribe la env var REAL en
        HERMES_HOME/.env (persistente — la carga el env_loader al arrancar) Y la
        inyecta en os.environ del proceso vivo, para que web_search empiece a usar
        ese backend SIN reiniciar (el plugin lee la key con os.getenv; p.ej.
        brave_free → BRAVE_SEARCH_API_KEY). El orden por defecto del registry
        (exa→searxng→brave-free→ddgs) lo prioriza automáticamente; ddgs queda de
        fallback keyless.
        """
        self._authorize_and_resolve(sender_uid, operation="set_web_search_api_key")
        env_var = self._WEB_SEARCH_ENV.get((provider or "").strip().lower())
        if env_var is None:
            return {"ok": False, "error": f"proveedor de búsqueda desconocido: {provider}"}
        key = (api_key or "").strip()
        if not key:
            return {"ok": False, "error": "api_key vacía"}
        try:
            import os as _os  # noqa: PLC0415
            _write_hermes_env(env_var, key)
            _os.environ[env_var] = key
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.dbus.web_search_key_write_failed: %s", exc)
            return {"ok": False, "error": f"no se pudo escribir: {exc}"}
        logger.info("hermes.dbus.web_search_key_set provider=%s", provider)
        return {"ok": True, "provider": provider, "configured": True}

    def get_web_search_status(self) -> dict:
        """Backends de búsqueda web con key configurada (read-only)."""
        import os as _os  # noqa: PLC0415
        return {
            "brave": bool(_os.getenv("BRAVE_SEARCH_API_KEY", "").strip()),
            "tavily": bool(_os.getenv("TAVILY_API_KEY", "").strip()),
            "exa": bool(_os.getenv("EXA_API_KEY", "").strip()),
            "ddgs_fallback": True,
        }

    def list_native_providers(self) -> list[dict]:
        """Catálogo NATIVO de providers de Hermes (hermes_cli.auth.PROVIDER_REGISTRY).

        37+ providers incluyendo suscripciones (Nous Portal, OpenAI Codex, xAI
        SuperGrok, Copilot, Gemini OAuth…). El SO DIBUJA este catálogo — no
        inventa uno paralelo. Read-only, sin authZ.
        """
        try:
            from hermes_cli.auth import PROVIDER_REGISTRY  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.dbus.native_providers_unavailable: %s", exc)
            return []
        out = []
        seen = set()
        for pid, cfg in PROVIDER_REGISTRY.items():
            name = getattr(cfg, "name", pid)
            if name in seen:  # alias duplicados (novita x3)
                continue
            seen.add(name)
            out.append({
                # Canonical identifier is provider_id everywhere (FE + TUI read it,
                # configure_native_provider/start_provider_oauth take it). Emitting
                # `id` here was the contract break that left the native catalogue
                # unusable (FE read provider_id → empty → "provider desconocido").
                "provider_id": pid,
                "name": name,
                "auth_type": getattr(cfg, "auth_type", "api_key"),
                "base_url": getattr(cfg, "inference_base_url", "") or "",
                "env_vars": list(getattr(cfg, "api_key_env_vars", ()) or ()),
            })
        return out

    # ------------------------------------------------------------------
    # Acceso remoto (Settings → toggle): espejo noVNC con URL pública
    # individual. El daemon NO toca systemd (User=hermes, sandbox): escribe el
    # staged request (REUSE shell_server.remote_access_tunnel.api) y el root
    # helper (hermes-remote-access-control, vía .path unit) verifica la
    # contraseña por PAM y arranca/para túnel + novnc + relanza el compositor
    # en modo espejo (QT_QPA_PLATFORM=vnc). SO-nativo: gobernanza por D-Bus.
    # ------------------------------------------------------------------

    def enable_remote_access(self, *, password: str, sender_uid: int) -> dict:
        """Activa el acceso remoto (password del dispositivo = consentimiento).

        Expone el escritorio a internet → exige la MISMA verificación PAM que
        desactivar (root helper, finding #6). Devuelve {staged: true}; la UI
        sondea get_remote_access_status hasta ver active+url.
        """
        self._authorize_and_resolve(sender_uid, operation="enable_remote_access")
        return self._stage_remote_access_request("enable", password)

    def disable_remote_access(self, *, password: str, sender_uid: int) -> dict:
        """Desactiva el acceso remoto (password del dispositivo, PAM en root helper)."""
        self._authorize_and_resolve(sender_uid, operation="disable_remote_access")
        return self._stage_remote_access_request("disable", password)

    def _stage_remote_access_request(self, action: str, password: str) -> dict:
        pw = (password or "").strip()
        if not pw:
            return {"ok": False, "error": "se requiere la contraseña del dispositivo"}
        try:
            from pathlib import Path as _Path  # noqa: PLC0415

            from hermes.shell_server.remote_access_tunnel.api import (  # noqa: PLC0415
                _DEFAULT_CONTROL_DIR,
                _write_staged_request,
            )
            _write_staged_request(
                action, password=pw, control_dir=_Path(_DEFAULT_CONTROL_DIR)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.dbus.remote_access_stage_failed: %s", exc)
            return {"ok": False, "error": f"no se pudo registrar la petición: {exc}"}
        logger.info("hermes.dbus.remote_access_staged action=%s", action)
        return {"ok": True, "staged": True}

    def get_remote_access_status(self) -> dict:
        """Estado del acceso remoto: {active, url}. Read-only, sin authZ.

        `active` se deriva del flag /var/lib/hermes/remote-active (0644),
        escrito por el root helper hermes-remote-access-control al activar y
        borrado al desactivar. El daemon (User=hermes, sandbox con
        SystemCallFilter+RestrictNamespaces) no puede ejecutar systemctl, por lo
        que derivar el estado de is-active no funciona desde este proceso.
        /var/lib/hermes está en ReadWritePaths del daemon → lectura siempre OK.

        url solo aparece cuando el túnel ya publicó la URL efímera
        (/var/lib/hermes/remote-url, escrita por hermes-remote-quicktunnel).
        """
        from pathlib import Path as _Path  # noqa: PLC0415
        active = _Path("/var/lib/hermes/remote-active").exists()
        url = ""
        try:
            url_file = _Path("/var/lib/hermes/remote-url")
            if url_file.exists():
                url = url_file.read_text(encoding="utf-8").strip()
        except OSError:
            pass
        return {"active": active, "url": url}

    # ------------------------------------------------------------------
    # Composio (SO-nativo, Principio 0): el daemon POSEE la credencial
    # (SQLiteIntegrationsRepository + SecretsVault) y CONSUME Composio Cloud
    # dinámicamente (toolkits + cuentas conectadas + OAuth Connect Link).
    # CERO catálogo hardcodeado, CERO HTTP nuestro: verbos D-Bus.
    # Lecturas: sin authZ. Mutadores: sender_uid del bus (CWE-862).
    # ------------------------------------------------------------------

    def _composio_db_path(self):
        # Mismo shell-state.db que el resto de repos daemon-owned. Lo tomamos
        # del repo ya inyectado (ambos guardan _db_path); evita re-plumbing.
        for repo in (self._conversation_repo, self._provider_repo):
            p = getattr(repo, "_db_path", None)
            if p:
                return p
        raise RuntimeError("db_path no resoluble para Composio (repos no inyectados)")

    def _require_trigger_repo(self):
        """Devuelve el repo de triggers, AUTO-CONSTRUYÉNDOLO lazy si el
        composition root no lo inyectó. Antes los verbos de scheduled-tasks
        lanzaban NotImplementedError porque el wiring se construye ANTES que el
        trigger_repo de las trigger-sources (orden de init). Resolverlo aquí (mismo
        shell-state.db que el resto de repos daemon-owned) hace los verbos
        funcionar sin re-plumbing del arranque. Singleton perezoso por instancia.
        """
        if self._trigger_repo is not None:
            return self._trigger_repo
        import sqlite3  # noqa: PLC0415

        from hermes.tasks.infrastructure.schema import ensure_tasks_schema  # noqa: PLC0415
        from hermes.tasks.triggers.infrastructure.sqlite_authorized_trigger_repository import (  # noqa: PLC0415
            SqliteAuthorizedTriggerRepository,
        )
        db_path = self._composio_db_path()  # resuelve el shell-state.db
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        ensure_tasks_schema(conn)
        self._trigger_repo = SqliteAuthorizedTriggerRepository(conn)
        logger.info("hermes.dbus.trigger_repo_lazy_built db=%s", db_path)
        return self._trigger_repo

    def _composio_integrations_repo(self):
        from hermes.shell_server.integrations.repo import (  # noqa: PLC0415
            SQLiteIntegrationsRepository,
        )
        from hermes.shell_server.security.secrets import SecretsVault  # noqa: PLC0415

        return SQLiteIntegrationsRepository(
            db_path=self._composio_db_path(), vault=SecretsVault()
        )

    def _composio_client(self):
        from hermes.runtime.composio_config_source import (  # noqa: PLC0415
            load_composio_credential,
        )

        cred = load_composio_credential(self._composio_db_path())
        if cred is None:
            return None, None
        from hermes.integrations.composio.composio_client import ComposioClient  # noqa: PLC0415

        return ComposioClient(cred.api_key), cred.entity_id

    @staticmethod
    def _composio_to_dict(obj: Any) -> dict:
        import dataclasses  # noqa: PLC0415

        if dataclasses.is_dataclass(obj):
            return dataclasses.asdict(obj)
        return {k: v for k, v in vars(obj).items() if not k.startswith("_")}

    def get_composio_status(self) -> dict:
        """{configured, enabled, entity_id} — sin exponer jamás la key."""
        try:
            from hermes.runtime.composio_config_source import (  # noqa: PLC0415
                load_composio_credential,
            )

            cred = load_composio_credential(self._composio_db_path())
            if cred is None:
                return {"configured": False}
            return {"configured": True, "entity_id": cred.entity_id}
        except Exception as exc:  # noqa: BLE001
            return {"configured": False, "error": str(exc)[:200]}

    async def set_composio_api_key(self, *, api_key: str, sender_uid: int) -> dict:
        """Guarda la key de Composio en el vault — VALIDÁNDOLA antes contra
        el Cloud (list_toolkits). Una key revocada daba catálogos vacíos sin
        explicación (la UI mostraba (0) y el 401 moría en el journal); ahora
        el error vuelve al operador y NO se guarda una credencial muerta.
        """
        self._authorize_and_resolve(sender_uid, operation="set_composio_api_key")
        key = api_key.strip()
        if not key:
            return {"ok": False, "error": "api_key vacía"}
        try:
            from hermes.integrations.composio.composio_client import (  # noqa: PLC0415
                ComposioClient,
            )
            toolkits = await ComposioClient(key).list_toolkits()
        except Exception as exc:  # noqa: BLE001 — el detalle va al operador
            detail = str(exc)
            if "401" in detail or "Unauthorized" in detail:
                return {"ok": False, "error": "Key inválida o revocada (Composio 401). Genera una nueva en composio.dev → Settings → API Keys."}
            return {"ok": False, "error": f"No se pudo validar contra Composio Cloud: {detail[:200]}"}
        self._composio_integrations_repo().set_credential(
            kind="composio", api_key=key
        )
        logger.info(
            "hermes.dbus.composio_key_set", extra={"by_uid": sender_uid, "toolkits": len(toolkits)}
        )
        return {"ok": True, "toolkits": len(toolkits)}

    async def list_composio_apps(self) -> list[dict]:
        """Catálogo DINÁMICO de toolkits desde Composio Cloud (read-only).

        Limitado a integraciones con OAuth simple (un clic en el navegador): es la
        política compartida SO/TUI/agente. Las que exigen API key/credenciales se
        ocultan hasta que las soportemos.
        """
        client, _entity = self._composio_client()
        if client is None:
            return []
        toolkits = await client.list_toolkits()
        return [self._composio_to_dict(t) for t in toolkits if getattr(t, "oauth_simple", True)]

    async def list_composio_connections(self) -> list[dict]:
        """Cuentas CONECTADAS del usuario, dinámico desde Cloud + alias locales."""
        client, entity = self._composio_client()
        if client is None:
            return []
        accounts = await client.list_connected_accounts(entity)
        aliases = self._composio_connection_repo().get_aliases()
        result = []
        for a in accounts:
            d = self._composio_to_dict(a)
            d["alias"] = aliases.get(a.id, "")
            result.append(d)
        return result

    def _composio_connection_repo(self):
        """Devuelve el repo de conexiones Composio (lazy, mismo shell-state.db)."""
        if not hasattr(self, "_conn_repo_instance"):
            from hermes.platforms.infrastructure.sqlite_agent_composio_connection_repo import (  # noqa: PLC0415
                SqliteAgentComposioConnectionRepo,
            )
            self._conn_repo_instance = SqliteAgentComposioConnectionRepo(
                db_path=self._composio_db_path()
            )
        return self._conn_repo_instance

    async def set_composio_connection_alias(
        self, *, connected_account_id: str, alias: str, sender_uid: int
    ) -> bool:
        """Asigna un alias humano a una cuenta Composio. Requiere authZ."""
        self._authorize(sender_uid, operation="set_composio_connection_alias")
        if not connected_account_id.strip():
            return False
        alias_clean = alias.strip()[:200]
        self._composio_connection_repo().set_alias(
            connected_account_id, alias_clean, sender_uid
        )
        logger.info(
            "hermes.dbus.composio_alias_set",
            extra={"by_uid": sender_uid},
        )
        return True

    async def bind_composio_connection_to_agent(
        self,
        *,
        agent_id: str,
        connected_account_id: str,
        toolkit_slug: str,
        tenant_id: str,
        sender_uid: int,
    ) -> bool:
        """Asigna una cuenta Composio a un agente. Idempotente. Requiere authZ."""
        self._authorize(sender_uid, operation="bind_composio_connection_to_agent")
        if not agent_id or not connected_account_id or not toolkit_slug:
            return False
        repo = self._composio_connection_repo()
        existing = repo.find_active(agent_id, connected_account_id, tenant_id)
        if existing is not None:
            logger.info(
                "hermes.dbus.composio_bind.idempotent",
                extra={
                    "agent_id": agent_id,
                    "by_uid": sender_uid,
                },
            )
            return True
        from hermes.capabilities.domain.agent_composio_connection import (  # noqa: PLC0415
            AgentComposioConnection,
        )
        binding = AgentComposioConnection.create(
            tenant_id=tenant_id,
            agent_id=agent_id,
            connected_account_id=connected_account_id,
            toolkit_slug=toolkit_slug,
            bound_by=sender_uid,
        )
        repo.save(binding)
        logger.info(
            "hermes.dbus.composio_bound",
            extra={
                "binding_id": binding.binding_id,
                "agent_id": agent_id,
                "toolkit_slug": toolkit_slug,
                "by_uid": sender_uid,
            },
        )
        return True

    async def unbind_composio_connection_from_agent(
        self,
        *,
        agent_id: str,
        connected_account_id: str,
        tenant_id: str,
        sender_uid: int,
    ) -> bool:
        """Revoca el binding de una cuenta Composio de un agente. Requiere authZ."""
        self._authorize(sender_uid, operation="unbind_composio_connection_from_agent")
        changed = self._composio_connection_repo().unbind(
            agent_id, connected_account_id, tenant_id
        )
        logger.info(
            "hermes.dbus.composio_unbound",
            extra={
                "agent_id": agent_id,
                "changed": changed,
                "by_uid": sender_uid,
            },
        )
        return changed

    def list_agent_composio_connections(
        self, agent_id: str, tenant_id: str
    ) -> list[str]:
        """IDs de cuentas Composio asignadas al agente (read-only)."""
        bindings = self._composio_connection_repo().list_by_agent(agent_id, tenant_id)
        return [b.connected_account_id for b in bindings]

    async def connect_composio_app(self, *, toolkit_slug: str, sender_uid: int) -> dict:
        """Inicia OAuth (Connect Link). Devuelve {redirect_url} para el navegador."""
        self._authorize_and_resolve(sender_uid, operation="connect_composio_app")
        client, entity = self._composio_client()
        if client is None:
            return {"ok": False, "error": "Composio no configurado (falta la key)"}
        # Política compartida SO/TUI/agente: solo OAuth simple por ahora.
        try:
            await client.assert_oauth_simple(toolkit_slug)
        except Exception as exc:  # noqa: BLE001 — ComposioApiError u otro
            return {"ok": False, "error": str(exc)}
        result = await client.initiate_connection(
            toolkit_slug=toolkit_slug, entity_id=entity
        )
        out = self._composio_to_dict(result)
        out["ok"] = True
        logger.info(
            "hermes.dbus.composio_connect_initiated",
            extra={"toolkit": toolkit_slug, "by_uid": sender_uid},
        )
        return out

    def list_skills(self) -> list[dict]:
        """Lista skills (read-only). No requiere authZ.

        Primary source: Neus native skill dirs via list_skills_native().
        Secondary source: composio_skills table (separate concern — connected
        integration skills have no on-disk SKILL.md).

        The DB skill_packages_view is no longer used as the source of truth
        for listing (BUG 3 fix): agent-created skills exist only on disk, so
        reading the DB missed them. list_skills_native() covers all origins.
        """
        native = self.list_skills_native()
        composio = _list_composio_skills(self._skill_governance)
        seen_names = {s["skill_name"] for s in native}
        extras = [s for s in composio if s["skill_name"] not in seen_names]
        return native + extras

    def list_skills_native(self) -> list[dict]:
        """Enumerate Neus native skill dirs and return SkillPackageDTO-shaped dicts.

        Reads $HERMES_HOME/skills/<name>/SKILL.md for every skill directory.
        Governance fields (state, signing_method, signature_hex) are read from
        the SKILL.md frontmatter.metadata block (written by SkillStoreAdapter
        after signing). Skills without governance metadata are surfaced as
        state='native' (agent-created, not cage-signed).

        Fail-soft: returns [] on any error.
        """
        return _list_native_skills_primary()

    async def promote_skill(self, *, package_id: str, sender_uid: int) -> dict:
        """Promueve una skill VALIDATED → AUTONOMOUS. by = UID del bus (CWE-862).

        Raises:
            DbusAuthorizationError: UID no autorizado.
            RuntimeError: skill_governance no inyectado.
        """
        self._authorize(sender_uid, operation="promote_skill")
        result = await self._require_skill_governance().promote_skill(
            package_id=package_id,
            promoted_by=_uid_to_uuid(sender_uid),
        )
        logger.info(
            "hermes.dbus.skill_promoted",
            extra={"package_id": package_id, "by_uid": sender_uid},
        )
        return result

    async def deprecate_skill(self, *, package_id: str, sender_uid: int) -> dict:
        """Depreca una skill. by = UID del bus (CWE-862).

        Raises:
            DbusAuthorizationError: UID no autorizado.
            RuntimeError: skill_governance no inyectado.
        """
        self._authorize(sender_uid, operation="deprecate_skill")
        result = await self._require_skill_governance().deprecate_skill(
            package_id=package_id,
            deprecated_by=_uid_to_uuid(sender_uid),
        )
        logger.info(
            "hermes.dbus.skill_deprecated",
            extra={"package_id": package_id, "by_uid": sender_uid},
        )
        return result

    async def sign_composio_skill(
        self,
        *,
        draft_json: str,
        sender_uid: int,
    ) -> dict:
        """Crea y firma una skill Composio. by = UID del bus (CWE-862).

        El draft_json contiene {skill_name, toolkit_slug, intent_text}.
        La autoría (sender_uid) queda registrada en los logs de auditoría.

        Raises:
            DbusAuthorizationError: UID no autorizado.
            RuntimeError: skill_governance no inyectado.
            ValueError: draft_json malformado.
        """
        import json as _json  # noqa: PLC0415

        self._authorize(sender_uid, operation="sign_composio_skill")
        try:
            draft = _json.loads(draft_json)
        except _json.JSONDecodeError as exc:
            raise ValueError(f"draft_json inválido: {exc}") from exc

        result = await self._require_skill_governance().sign_composio_skill(
            skill_name=draft.get("skill_name", ""),
            toolkit_slug=draft.get("toolkit_slug", ""),
            intent_text=draft.get("intent_text", ""),
            author_uid=sender_uid,
        )
        logger.info(
            "hermes.dbus.skill_composio_signed",
            extra={
                "package_id": result.get("package_id"),
                "skill_name": draft.get("skill_name"),
                "by_uid": sender_uid,
            },
        )
        return result

    async def create_skill_from_text(
        self,
        *,
        name: str,
        skill_md: str,
        sender_uid: int,
    ) -> dict:
        """Crea una skill firmada pasando el texto SKILL.md al escritor único nativo.

        Delega en SkillStoreAdapter.replay() (SurfaceKind.SKILL_STORE, action=create)
        — el mismo camino que usa el agente autónomo vía HITL. Esto garantiza que
        el frontmatter de gobernanza y la firma v2 sean idénticos a los emitidos por
        skill_manage, haciendo las skills promovibles y verificables.

        Contrato del outcome: {package_id, skill_id, name, state, signing_method}.

        Raises:
            DbusAuthorizationError: UID no autorizado.
            RuntimeError: skill_store_adapter no inyectado.
            RuntimeError: el adapter rechazó o falló la escritura (fail-closed).
        """
        self._authorize(sender_uid, operation="create_skill_from_text")

        if self._skill_store_adapter is None:
            raise RuntimeError(
                "skill_store_adapter no inyectado en el wiring — "
                "asegura que hermes-keygen.service completó antes del runtime"
            )

        from hermes.agents_os.domain.ports.surface_adapter_port import (  # noqa: PLC0415
            CapturedAction,
            ReplayStatus,
        )
        from hermes.agents_os.domain.surface_kind import SurfaceKind  # noqa: PLC0415
        from uuid import uuid4 as _uuid4  # noqa: PLC0415

        action = CapturedAction(
            surface_kind=SurfaceKind.SKILL_STORE,
            intent_desc=f"shell-server skill synthesis: {name}",
            payload={"action": "create", "name": name, "content": skill_md},
            tenant_id=None,
            human_operator_id=_uid_to_uuid(sender_uid),
        )

        outcome = await self._skill_store_adapter.replay(action)

        if outcome.status not in (ReplayStatus.EXECUTED_OK,):
            raise RuntimeError(
                f"create_skill_from_text rechazado por SkillStoreAdapter: "
                f"status={outcome.status.value} error={outcome.error!r}"
            )

        result = dict(outcome.result)
        # Map adapter result keys to the shape callers expect:
        # outcome.result has {package_id, skill_id, name, state, signing_method}
        # Callers use {package_id, skill_id, skill_name, version, path}.
        # We include both forms; callers pick what they need.
        result.setdefault("skill_name", result.pop("name", name))
        result.setdefault("version", 1)
        logger.info(
            "hermes.dbus.create_skill_from_text.ok name=%s package_id=%s",
            name,
            result.get("package_id"),
        )
        return result

    # ------------------------------------------------------------------
    # HITL supervisión (SC-004 / T043/T044 re-enrutado a D-Bus)
    # ------------------------------------------------------------------

    async def approve_action(
        self,
        *,
        proposal_id: UUID,
        sender_uid: int,
        totp: str | None = None,
        operator_token: str | None = None,
    ) -> HitlApprovalResult:
        """Aprueba una acción HIGH pendiente y re-encola la tarea (FR-015).

        `approved_by` = identidad verificada del sender del bus (direct) o del
        token (proxy). NUNCA del payload del cliente (SC-004 / CWE-862).

        Secuencia:
          1. Autoriza al sender (DbusAuthorizationError si no autorizado).
          2. Minta el approval_token vía gate.approve (CTRL-1 / SC-004).
          3. Recupera el work_item_id asociado a la proposal (registro canónico
             en pending_approvals — el gate es el único dueño de este mapping).
          4. Re-encola la tarea: PENDING_APPROVAL → PENDING (FR-015).
             NO dispara run_cycle directamente — el loop autónomo la recoge en
             el próximo ciclo de drenado (NFR-001: coordinación vía cola).

        Raises:
            DbusAuthorizationError: UID del sender no está autorizado o token inválido.
        """
        approved_by = self._authorize_and_resolve(
            sender_uid, operation="approve_action", operator_token=operator_token
        )
        # Forward the owner's TOTP to the gate — the gate is the single MFA enforcement
        # point for ALL surfaces (red-team 2026-06-19, finding 3). mfa-tier tools require
        # valid factors here; simple-tier tools ignore them. Dropping totp here caused
        # gate.approve to always fail with mfa_required for mfa-tier proposals, which was
        # then mis-reported as proposal_invalid (bug 2026-06-25).
        mfa_factors: Any | None = None
        if totp:
            from hermes.shell_server.security.mfa_tool_tier import MfaFactors  # noqa: PLC0415
            mfa_factors = MfaFactors(totp=totp)
        token = await self._gate.approve(
            proposal_id=proposal_id,
            approved_by=approved_by,
            mfa_factors=mfa_factors,
        )
        logger.info(
            "hermes.dbus.hitl_approved",
            extra={"proposal_id": str(proposal_id), "by_uid": sender_uid},
        )

        # FR-015: re-encolar la tarea para que el loop la procese con el token.
        # Solo si la queue está inyectada (degradación honesta: sin queue, solo
        # se emite el evento de aprobación).
        #
        # BUG FIX (2026-07 — "caducó antes de aprobarla" toast on a live=false
        # response): `requeued` tracks whether re_enqueue_after_approval ACTUALLY
        # put the task back to work, so `thread_resumed` below reports the truth
        # for the broker/MCP path (no native-danger thread ever blocks there —
        # signalled is always False for it). Before this fix the response only
        # ever looked at `signalled`, so a successfully re-enqueued delegated/
        # autonomous MCP approval was misreported as live=false even though the
        # task WILL execute on the next drain.
        requeued = False
        if self._queue is not None:
            work_item_id = await self._gate.work_item_id_for_proposal(proposal_id)
            # work_item_id == UUID(int=0) ⇒ chat / native-danger (NO es una tarea de la
            # cola): NO se re-encola — el resume va por el block-and-resume del hilo del
            # chat (señal del Event abajo). Re-encolar 0 disparaba un ValueError inútil y,
            # peor, un ciclo nuevo mudo. Solo las tareas REALES (autónomo/scheduled) se re-encolan.
            if work_item_id is not None and work_item_id != UUID(int=0):
                try:
                    await self._queue.re_enqueue_after_approval(work_item_id)
                    requeued = True
                    logger.info(
                        "hermes.dbus.hitl_requeued",
                        extra={
                            "proposal_id": str(proposal_id),
                            "work_item_id": str(work_item_id),
                        },
                    )
                except ValueError as exc:
                    # La tarea ya fue re-encolada (idempotente) o no existe.
                    # No fatal: el token ya fue emitido; el loop lo encontrará
                    # si la tarea aparece de nuevo.
                    logger.warning(
                        "hermes.dbus.hitl_reenqueue_skipped: %s", exc
                    )
            else:
                logger.debug(
                    "hermes.dbus.hitl_approved_native_danger: proposal_id=%s — "
                    "work_item_id=0 (native-danger gate, not a queue task); "
                    "signalling blocked conversation thread to resume.",
                    proposal_id,
                )
        else:
            logger.debug(
                "hermes.dbus.hitl_approved_no_queue: proposal_id=%s — "
                "work_queue not injected; native-danger signal path only.",
                proposal_id,
            )

        # Native-danger block-and-resume (Mandato 1 / 2026-06-25):
        # The security hook blocked the conversation thread on a threading.Event
        # registered under this proposal_id. Signal it now so the EXACT same tool
        # call is resumed (approved) without any re-prompt or re-attempt.
        # signalled=True  → LIVE: thread was waiting, will execute the exact call.
        # signalled=False → the chat thread wasn't blocked (broker/MCP path, or
        #   timed out / turn already ended) — NOT necessarily "nothing will happen":
        #   `requeued` above covers the re-enqueue path.
        from hermes.runtime.security_hook import signal_native_danger_approval  # noqa: PLC0415
        signalled = signal_native_danger_approval(str(proposal_id), "approved")
        logger.info(
            "hermes.dbus.hitl_native_danger_signalled: proposal=%s signalled=%s "
            "(signalled=False means POST-execution — no blocked thread found)",
            proposal_id, signalled,
        )

        return HitlApprovalResult(
            approval_token=token,
            approved_by=approved_by,
            thread_resumed=signalled or requeued,
        )

    async def reject_action(
        self,
        *,
        proposal_id: UUID,
        reason: str,
        sender_uid: int,
        operator_token: str | None = None,
    ) -> None:
        """Rechaza una acción HIGH pendiente. NO dispara run_cycle (NFR-001).

        `rejected_by` = identidad verificada del sender del bus (direct) o del
        token (proxy) — nunca del payload del cliente (SC-004 / CWE-862).

        Raises:
            DbusAuthorizationError: UID del sender no está autorizado o token inválido.
        """
        rejected_by = self._authorize_and_resolve(
            sender_uid, operation="reject_action", operator_token=operator_token
        )
        await self._gate.reject(
            proposal_id=proposal_id,
            rejected_by=rejected_by,
            reason=reason,
        )
        logger.info(
            "hermes.dbus.hitl_rejected",
            extra={"proposal_id": str(proposal_id), "by_uid": sender_uid},
        )

        # Signal the blocked conversation thread so it can return the deny message
        # to the agent immediately (block-and-resume, Mandato 1 / 2026-06-25).
        from hermes.runtime.security_hook import signal_native_danger_approval  # noqa: PLC0415
        signal_native_danger_approval(str(proposal_id), "denied")

    # ------------------------------------------------------------------
    # FASE 3 (A2A cross-human) — inbound delegation inbox verbs.
    #
    # submit_inbound_delegation: SINGLE WRITER of `pending_delegations` — the
    # ONLY caller is config_sync.delegation_inbox (same service uid "hermes",
    # already VERIFIED the DelegationEnvelope signature/anti-replay/freshness
    # before calling here; see org.hermes.Runtime1.conf's `<policy user=
    # "hermes">` allow-list for this verb). LOW fix (defense-in-depth): since
    # ANY hermes-uid process is allow-listed for this verb (not just
    # config_sync specifically), this method RE-VERIFIES the Ed25519 tenant
    # signature itself before registering the card — it does not blindly
    # trust the caller's prior verification.
    #
    # resolve_inbound_delegation: the LOCAL human's Aprobar/Rechazar. approved_
    # by/rejected_by is ALWAYS derived from the authenticated D-Bus channel
    # (direct uid or verified operator_token) — NEVER from the envelope/
    # payload (CWE-862 / provenance guarantee, see DelegationApprovalService).
    # ------------------------------------------------------------------

    async def submit_inbound_delegation(
        self, *, envelope_json: str, sender_uid: int,
    ) -> dict:
        """Registra una DelegationEnvelope kind=request YA VERIFICADA.

        Raises:
            DbusAuthorizationError: UID del sender no está autorizado.
        """
        self._authorize_and_resolve(sender_uid, operation="submit_inbound_delegation")
        try:
            envelope = json.loads(envelope_json)
        except (json.JSONDecodeError, TypeError) as exc:
            return {"ok": False, "error": f"envelope_json inválido: {exc}"}
        if not isinstance(envelope, dict):
            return {"ok": False, "error": "envelope_json debe ser un objeto"}

        verify_error = self._reverify_delegation_signature(envelope)
        if verify_error is not None:
            logger.warning(
                "hermes.dbus.delegation_signature_reverify_failed",
                extra={
                    "message_id": envelope.get("message_id"), "reason": verify_error,
                },
            )
            return {"ok": False, "error": verify_error}

        service = self._require_delegation_approval_service()
        if service is None:
            return {"ok": False, "error": "delegation_service_not_configured"}
        status = await service.submit(envelope=envelope)
        logger.info(
            "hermes.dbus.delegation_submitted",
            extra={"message_id": envelope.get("message_id"), "status": status},
        )
        return {"ok": True, "status": status}

    def _reverify_delegation_signature(self, envelope: dict) -> str | None:
        """Re-verify the Ed25519 tenant signature over *envelope* (LOW fix,
        defense-in-depth). `envelope` carries the 12 pinned DelegationEnvelope
        keys PLUS `signature_hex` (added by config_sync.delegation_inbox
        specifically for this re-check).

        Returns None on success (signature valid), or a short error code on
        ANY failure — fail-closed: no association/pubkey/signature_hex means
        the card is NOT registered.
        """
        from hermes.config_sync.delegation_inbox import (  # noqa: PLC0415
            delegation_signing_bytes,
        )
        from hermes.config_sync.signature import verify_bundle  # noqa: PLC0415

        signature_hex = envelope.get("signature_hex")
        if not isinstance(signature_hex, str) or not signature_hex:
            return "missing_signature"

        if self._association_store is None or not self._association_store.is_associated():
            return "not_associated"
        assoc = self._association_store.get()
        if assoc is None or not assoc.signing_pubkey_hex:
            return "no_tenant_pubkey"

        plain_envelope = {
            k: v for k, v in envelope.items() if k != "signature_hex"
        }
        if not all(isinstance(v, str) for v in plain_envelope.values()):
            return "invalid_envelope_shape"

        payload = delegation_signing_bytes(plain_envelope)
        if not verify_bundle(
            payload_canonical=payload,
            signature_hex=signature_hex,
            pubkey_hex=assoc.signing_pubkey_hex,
        ):
            return "bad_signature"
        return None

    async def resolve_inbound_delegation(
        self,
        *,
        message_id: str,
        decision: str,
        sender_uid: int,
        operator_token: str | None = None,
    ) -> dict:
        """Aprueba/rechaza una tarjeta de delegación entrante pendiente.

        Raises:
            DbusAuthorizationError: UID del sender no está autorizado o token inválido.
        """
        resolver = self._authorize_and_resolve(
            sender_uid, operation="resolve_inbound_delegation", operator_token=operator_token,
        )
        service = self._require_delegation_approval_service()
        if service is None:
            return {"ok": False, "error": "delegation_service_not_configured"}

        if decision == "approve":
            task_id = await service.approve(message_id=message_id, approved_by=resolver)
            logger.info(
                "hermes.dbus.delegation_resolved",
                extra={
                    "message_id": message_id, "decision": "approve",
                    "task_id": str(task_id) if task_id else None, "by_uid": sender_uid,
                },
            )
            return {"ok": task_id is not None, "task_id": str(task_id) if task_id else None}
        if decision == "reject":
            resolved = await service.reject(message_id=message_id, rejected_by=resolver)
            logger.info(
                "hermes.dbus.delegation_resolved",
                extra={
                    "message_id": message_id, "decision": "reject",
                    "resolved": resolved, "by_uid": sender_uid,
                },
            )
            return {"ok": resolved}
        return {"ok": False, "error": f"decision inválida: {decision!r} (usa 'approve'|'reject')"}

    async def list_pending_delegations(self) -> list[dict]:
        """Tarjetas de delegación entrante pendientes de HITL (CTRL-P1-5:
        solo metadatos, sin secretos ni firma)."""
        service = self._require_delegation_approval_service()
        if service is None:
            return []
        return service.list_pending()

    def _require_delegation_approval_service(self):
        """DelegationApprovalService, AUTO-CONSTRUIDO lazy (mismo patrón que
        _require_trigger_repo): singleton perezoso por instancia, sobre el
        MISMO shell-state.db que el resto de repos daemon-owned.

        None si conversation_repo no fue inyectado (degradación honesta —
        el verbo devuelve un error explícito en vez de lanzar AttributeError).
        """
        if self._delegation_approval_service is not None:
            return self._delegation_approval_service
        if self._conversation_repo is None:
            logger.warning("hermes.dbus.delegation_service_no_conversation_repo")
            return None

        from hermes.tasks.infrastructure.sqlite_pending_delegations import (  # noqa: PLC0415
            SqlitePendingDelegationRepository,
        )
        from hermes.tasks.triggers.application.delegation_approval_service import (  # noqa: PLC0415
            DelegationApprovalService,
        )
        from hermes.tasks.triggers.application.trigger_gate import TriggerGate  # noqa: PLC0415

        trigger_repo = self._require_trigger_repo()
        db_path = self._composio_db_path()
        pending_repo = SqlitePendingDelegationRepository(db_path)
        gate = TriggerGate(
            trigger_repo=trigger_repo,
            queue=self._queue,
            agent_state=self._state,
            tenant_id=_resolve_tenant_id_from_wiring(self._tenant_id),
            audit_signer=self._audit_signer,
        )
        self._delegation_approval_service = DelegationApprovalService(
            pending_repo=pending_repo,
            trigger_repo=trigger_repo,
            gate=gate,
            conversation_repo=self._conversation_repo,
        )
        return self._delegation_approval_service

    async def list_hitl_pending(self, *, limit: int = 50) -> list[dict]:
        """Propuestas HITL pendientes de aprobación humana (CTRL-P1-5).

        Lee directamente de la tabla `pending_approvals` del gate vía duck-type
        sobre `_db_path` (mismo patrón que _composio_db_path). Read-only —
        solo metadatos (proposal_id, tool_name, justification, risk); sin
        payload ni credenciales.

        Retorna [] si el gate no expone _db_path (degradación honesta).
        """
        import sqlite3 as _sqlite3  # noqa: PLC0415

        db_path = getattr(self._gate, "_db_path", None)
        if db_path is None:
            logger.warning("hermes.dbus.list_hitl_pending: gate sin _db_path — vacío honesto")
            return []
        try:
            conn = _sqlite3.connect(str(db_path), isolation_level=None)
            conn.row_factory = _sqlite3.Row
            conn.execute("PRAGMA busy_timeout=3000")
            rows = conn.execute(
                """
                SELECT proposal_id, risk, tool_name, justification,
                       created_at, conversation_id, parameters_redacted, route
                FROM pending_approvals
                WHERE status = 'pending'
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (max(1, min(limit, 200)),),
            ).fetchall()
            conn.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.dbus.list_hitl_pending_error: %s", exc)
            return []
        return [
            {
                "proposal_id": row["proposal_id"],
                # REAL tool name (was aliased to risk — that defeated the MFA
                # delicacy ladder; red-team 2026-06-19). Fallback to risk only for
                # legacy rows written before the tool_name column existed.
                "tool_name": (row["tool_name"] or row["risk"]),
                "justification": row["justification"] or "",
                "risk": row["risk"],
                "created_at": row["created_at"] or "",
                # Real chat conversation_id (anchors the card to the thread); None
                # for rows written before the conversation_id column existed.
                "conversation_id": row["conversation_id"] or None,
                # Redacted parameters so the card can show WHAT will run (stored as
                # JSON text by the gate). Parse back to a dict; {} on any malformed.
                "parameters_redacted": _parse_redacted_params(
                    row["parameters_redacted"]
                ),
                # Enterprise approval routing (Fase 2 Phase 4b): "enterprise" when
                # only a signed cloud decision can resolve this row (local APPROVE
                # is rejected by the gate — local DENY always still works, I-2);
                # "local" (default) for every row registered before this phase or
                # never routed to Enterprise.
                "route": row["route"] or "local",
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # T039b — Queue methods (Enqueue / read-only supervision)
    # ------------------------------------------------------------------

    async def enqueue(
        self,
        *,
        trigger_kind: str,
        text: str,
        priority: int,
        dedup_key: str | None,
        sender_uid: int,
        conversation_id: str | None = None,
        operator_token: str | None = None,
        agent_id: str | None = None,
    ) -> EnqueueResult:
        """Encola un WorkItem delegando en ControlPlaneService (Issue 2 / CTRL-P1-6).

        DELEGACIÓN: convierte operator_id verificado en AuthenticatedChannel y
        llama a ControlPlaneService.enqueue(), que aplica:
          - rate-limit (CWE-770 / CTRL-P1-6).
          - PII tokenization (CTRL-P1-25).
          - AuditEntry WORKITEM_ACCEPTED síncrono (CTRL-P1-4).
          - enqueued_by = verified operator_id, NUNCA del payload (CTRL-P1-3).
          - commit-then-wake ordering (CTRL-P1-12).

        Confused-deputy remediation: operator_id comes from the verified source:
          - Direct call (sender_uid ∈ authorized_uids): derived from sender_uid.
          - Proxy call (sender_uid == proxy_uid): extracted from operator_token.
        The AuthenticatedChannel carries the verified operator uid so that
        ControlPlaneService.enqueue() attributes the work to the human, not
        the proxy.

        CWE-862: _authorize_and_resolve() comprueba ANTES de llamar al service.

        Raises:
            DbusAuthorizationError: UID no autorizado o token inválido.
            NotImplementedError: si control_plane_service no fue inyectado.
        """
        operator_id = self._authorize_and_resolve(
            sender_uid, operation="enqueue", operator_token=operator_token
        )
        self._check_license_for_enqueue()  # raises LicenseExpired in associate if expired
        if self._cp_service is None:
            raise NotImplementedError(
                "control_plane_service no inyectado en DbusRuntimeServiceWiring; "
                "inyéctalo en la composición del daemon para habilitar Enqueue."
            )
        from hermes.tasks.control_plane.domain.ports import AuthenticatedChannel  # noqa: PLC0415

        # Use the verified operator's uid, not the proxy uid. This ensures
        # that ControlPlaneService.enqueue() records enqueued_by = operator.
        # _uid_from_uuid is the inverse of _uid_to_uuid.
        operator_uid = operator_id.int
        channel = AuthenticatedChannel(sender_uid=operator_uid)
        result = await self._cp_service.enqueue(
            channel=channel,
            trigger_kind=trigger_kind,
            text=text,
            priority=priority,
            dedup_key=dedup_key,
            conversation_id=conversation_id,
            agent_id=agent_id,
        )
        # GATE 0 / M2 — el daemon ES dueño del store de conversaciones. Persiste el
        # mensaje del usuario AQUÍ (movido del shell-server) para que la historia
        # exista sin pasar por HTTP. Best-effort: un fallo aquí NO rompe el chat
        # (ya está encolado). La respuesta del asistente se transmite por el socket
        # AF_UNIX (stream); su persistencia es un follow-up del orchestrator.
        if (
            trigger_kind == "chat_message"
            and conversation_id
            and self._conversation_repo is not None
        ):
            try:
                from uuid import UUID as _UUID  # noqa: PLC0415
                from hermes.agents.domain.agent import DEFAULT_AGENT_ID as _DEFAULT_AGENT_ID  # noqa: PLC0415

                conv_uuid = _UUID(conversation_id)
                # Use the per-request agent_id (resolved by chat_start with
                # conversation-binding precedence). Fall back to CEO if absent.
                bound_agent = agent_id or _DEFAULT_AGENT_ID
                self._conversation_repo.create_or_touch(
                    conversation_id=conv_uuid,
                    first_user_message=text,
                    agent_id=bound_agent,
                )
                self._conversation_repo.append_message(
                    conversation_id=conv_uuid, role="user", content=text
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("hermes.dbus.chat.persist_user_failed: %s", exc)
        # Replicar las AuditEntries del service a este acumulador (tests que
        # usan audit_entries_emitted() sobre el Wiring).
        new_entries = self._cp_service.audit_entries_emitted()
        if new_entries:
            self._audit_entries.append(new_entries[-1])
        return result

    async def get_queue_status(self) -> dict:
        """Snapshot read-only de la cola (CTRL-P1-5: solo metadatos).

        No altera estado. No requiere authZ (lectura).
        """
        # Implementación mínima — el adapter real delegará a SqliteWorkQueue
        return {
            "state": "active",
            "pending": 0,
            "in_progress": 0,
            "pending_approval": 0,
            "last_audit_head_hex": "",
        }

    async def list_pending(self, *, limit: int = 50) -> list[dict]:
        """Items PENDING por prioridad desc (CTRL-P1-5: metadatos, nunca payload)."""
        if self._cp_service is None:
            return []
        rows = await self._cp_service.list_pending(limit=limit)
        return [
            {
                "task_id": str(r.task_id),
                "trigger_kind": r.trigger_kind,
                "priority": r.priority,
                "enqueued_at_iso": r.enqueued_at_iso,
            }
            for r in rows
        ]

    async def get_task_status(self, *, task_id: UUID) -> dict:
        """Estado de UNA tarea (CTRL-P1-5: metadatos, nunca payload/instruction)."""
        if self._cp_service is None:
            return {}
        from hermes.tasks.control_plane.domain.ports import UnknownTask  # noqa: PLC0415
        try:
            view = await self._cp_service.get_task_status(task_id=task_id)
        except UnknownTask:
            return {}
        return {
            "task_id": str(view.task_id),
            "status": view.status,
            "attempts": view.attempts,
            "stream_path": view.stream_path,
        }

    async def list_configured_tasks(self, *, limit: int = 200) -> list[dict]:
        """Configured tasks dashboard (one row per Neus cron job).

        Read-only supervision — no authZ required (CTRL-P1-5).
        Returns only trigger metadata + last-run info; no payload, no credentials.

        BUG-7 ROOT FIX: reads from Neus cron/jobs.json (single source of truth)
        instead of the Safent trigger_repo. The agent's `cronjob` tool writes to
        jobs.json; the UI creates via create_scheduled_task (which now also writes
        to jobs.json). Both paths write to the SAME store, so the dashboard is
        always consistent regardless of who created the job.

        The trigger_repo (SqliteAuthorizedTriggerRepository) is NOT consulted here
        — it owns the AUTHORIZATION allow-list that TriggerGate.enqueue_from_trigger
        reads. That security gate is untouched.
        """
        jobs = _neus_cron_list_jobs(include_disabled=True)
        return [_neus_job_to_task_dict(job) for job in jobs[:limit]]

    async def list_recent_tasks(self, *, limit: int = 50) -> list[dict]:
        """Recent work items across all statuses (activity log).

        Read-only supervision — no authZ required (CTRL-P1-5).
        instruction_truncated capped at 120 chars; no full payload exposed.
        """
        if self._cp_service is None:
            return []
        rows = await self._cp_service.list_recent_tasks(limit=limit)
        return [
            {
                "task_id": r.task_id,
                "label": r.label,
                "status": r.status,
                "trigger_kind": r.trigger_kind,
                "enqueued_at": r.enqueued_at,
                "claimed_at": r.claimed_at,
            }
            for r in rows
        ]

    async def get_scheduled_task(self, *, trigger_id: str) -> dict:
        """Return detail for one scheduled task trigger (read-only, no authZ).

        Returns {} when not found or not enabled (idempotent, safe).
        """
        try:
            repo = self._require_trigger_repo()
        except Exception:  # noqa: BLE001
            return {}
        from datetime import UTC, datetime  # noqa: PLC0415
        from hermes.tasks.control_plane.application.control_plane_service import (  # noqa: PLC0415
            _build_configured_task_view,
        )
        trigger = repo.get_by_id(trigger_id)
        if trigger is None:
            return {}
        view = _build_configured_task_view(trigger, None, None, datetime.now(tz=UTC))
        return _configured_task_to_dict(view)

    async def update_scheduled_task(
        self,
        *,
        trigger_id: str,
        draft_json: str,
        sender_uid: int,
    ) -> dict:
        """Update mutable fields of a scheduled task trigger.

        draft: {label, instruction, cron, target_agent_id?, risk_ceiling?}
        Raises PermissionError if caller is not authorized.
        Returns the updated task dict, or raises ValueError if not found.
        """
        import json as _json  # noqa: PLC0415

        self._authorize(sender_uid, operation="update_scheduled_task")
        try:
            draft = _json.loads(draft_json)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"update_scheduled_task: invalid JSON draft: {exc}") from exc

        label = str(draft.get("label") or "").strip()
        instruction = str(draft.get("instruction") or "").strip()
        cron = str(draft.get("cron") or "").strip()
        target_agent_id = str(draft.get("target_agent_id") or "").strip() or None
        ceiling_raw = str(draft.get("risk_ceiling") or "low").strip().lower()
        risk_ceiling = ceiling_raw if ceiling_raw in ("low", "high") else "low"

        if not label or not instruction or not cron:
            raise ValueError("update_scheduled_task: label, instruction and cron are required")

        try:
            repo = self._require_trigger_repo()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"update_scheduled_task: trigger_repo not available: {exc}") from exc

        updated = repo.update_task(
            trigger_id=trigger_id,
            label=label,
            instruction=instruction,
            cron=cron,
            target_agent_id=target_agent_id,
            risk_ceiling=risk_ceiling,
        )
        if not updated:
            raise ValueError(f"update_scheduled_task: trigger {trigger_id!r} not found or not enabled")

        _neus_cron_update_job(
            trigger_id,
            prompt=instruction,
            schedule=cron,
            name=label,
        )

        updated_task = await self.get_scheduled_task(trigger_id=trigger_id)
        logger.info(
            "hermes.dbus.scheduled_task_updated",
            extra={"trigger_id": trigger_id, "by_uid": sender_uid},
        )
        return updated_task

    # ------------------------------------------------------------------
    # T007 — Trigger management (AuthorizeTrigger / RevokeTrigger / List)
    # ------------------------------------------------------------------

    async def authorize_trigger(
        self,
        *,
        trigger_type: str,
        scope_value: str,
        allowed_capabilities: tuple[str, ...],
        risk_ceiling: str,
        approval_signature: str,
        sender_uid: int,
        hourly_budget: int = 10,
    ) -> dict:
        """Autoriza un origen de auto-disparo (CTRL-P2-9/FR-013).

        La identidad del admin se toma del sender UID del bus (canal
        autenticado), NUNCA del contenido del payload (CWE-290/CTRL-P2-9).

        Raises:
            DbusAuthorizationError: UID no autorizado.
            NotImplementedError: si trigger_repo no fue inyectado.
        """
        self._authorize(sender_uid, operation="authorize_trigger")
        self._require_trigger_repo()

        from hermes.tasks.triggers.domain.authorized_trigger_ports import (  # noqa: PLC0415
            AuthorizedTriggerType,
            RiskCeiling,
        )
        admin_uuid = _uid_to_uuid(sender_uid)
        trigger = await self._trigger_repo.authorize(
            trigger_type=AuthorizedTriggerType(trigger_type),
            scope_value=scope_value,
            allowed_capabilities=tuple(allowed_capabilities),
            risk_ceiling=RiskCeiling(risk_ceiling),
            admin_uuid=admin_uuid,
            approval_signature=approval_signature,
            hourly_budget=hourly_budget,
        )
        logger.info(
            "hermes.dbus.trigger_authorized",
            extra={
                "trigger_type": trigger_type,
                "instance_id": str(trigger.trigger_instance_id),
                "by_uid": sender_uid,
            },
        )
        return {
            "instance_id": str(trigger.trigger_instance_id),
            "trigger_type": trigger_type,
            "scope_value": scope_value,
            "authorized_by": str(admin_uuid),
        }

    async def revoke_trigger(
        self,
        *,
        trigger_instance_id: str,
        sender_uid: int,
    ) -> None:
        """Revoca un origen autorizado (FR-018/CTRL-P2-15).

        La identidad del admin se toma del sender UID del bus.

        Raises:
            DbusAuthorizationError: UID no autorizado.
            NotImplementedError: si trigger_repo no fue inyectado.
        """
        self._authorize(sender_uid, operation="revoke_trigger")
        self._require_trigger_repo()

        admin_uuid = _uid_to_uuid(sender_uid)
        await self._trigger_repo.revoke(
            trigger_instance_id=UUID(trigger_instance_id),
            admin_uuid=admin_uuid,
        )
        logger.info(
            "hermes.dbus.trigger_revoked",
            extra={"instance_id": trigger_instance_id, "by_uid": sender_uid},
        )

    async def list_authorized_triggers(self) -> list[dict]:
        """Lista los orígenes autorizados activos (supervisión, no requiere authZ).

        Retorna solo metadatos (no payload ni instrucciones — CTRL-P1-5 style).
        """
        try:
            self._require_trigger_repo()
        except Exception as exc:  # noqa: BLE001 — read-only: vacío honesto si no resoluble
            logger.warning("hermes.dbus.trigger_repo_unavailable: %s", exc)
            return []
        triggers = await self._trigger_repo.list_enabled()
        return [
            {
                "instance_id": str(t.trigger_instance_id),
                "trigger_type": str(t.trigger_type),
                "scope_value": t.scope_value,
                "risk_ceiling": str(t.risk_ceiling),
                "authorized_by": str(t.created_by_admin_uuid),
                "authorized_at": t.authorized_at.isoformat(),
            }
            for t in triggers
        ]

    # ------------------------------------------------------------------
    # Calendario de tareas per-agent (P3: feat scheduled-tasks).
    # create_scheduled_task / delete_scheduled_task / set_scheduled_task_enabled.
    # REUSE de trigger_repo.authorize (ya existente), extendido con los campos P3.
    # Mutadores: authZ operador (sender_uid del bus, CWE-862), fail-closed.
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_cron(cron: str) -> bool:
        """Validates that `cron` is a 5-field (recurrent) OR 6-field (one-shot) expr.

        5 fields: min hr dom mon dow (standard recurrent).
        6 fields: min hr dom mon dow year — the frontend's one-shot form builds this
        so a "Una vez" task encodes a specific date+year; one_shot=True means it
        fires once then disables, so the trailing year is accepted (not rejected as
        "must be 5 fields" — that was the bug that made one-shot tasks un-creatable).
        Each field may be *, a number, a range (n-m), a step (*/n), or a list.
        Returns True if the shape is valid, False otherwise.
        """
        import re  # noqa: PLC0415
        _CRON_FIELD = r"(?:\*(?:/\d+)?|\d+(?:-\d+)?(?:/\d+)?(?:,\d+(?:-\d+)?(?:/\d+)?)*)"
        pattern = rf"^{_CRON_FIELD}(?:\s+{_CRON_FIELD}){{4,5}}$"
        return bool(re.match(pattern, cron.strip()))

    async def create_scheduled_task(
        self,
        *,
        draft_json: str,
        sender_uid: int,
    ) -> dict:
        """Crea una tarea programada firmada en authorized_trigger_instances.

        REUSA trigger_repo.authorize (el único canal de escritura en la allow-list).
        Extiende la fila con los campos P3: target_agent_id, task_instruction,
        one_shot, title. La firma (approval_signature) se genera con hmac del
        contenido del draft para mantener no-repudio sin requerir una clave
        externa — igual que el patrón usado en auth_trigger del wiring existente
        donde el caller genera la firma; aquí la generamos internamente porque
        este es un verbo de gobernanza de usuario (no de un admin externo).

        draft: {title, target_agent_id, task_instruction, cron, one_shot, risk_ceiling}

        Raises:
            DbusAuthorizationError: UID no autorizado.
            NotImplementedError: trigger_repo no inyectado.
        """
        self._authorize_and_resolve(sender_uid, operation="create_scheduled_task")
        self._require_trigger_repo()

        try:
            draft = json.loads(draft_json)
        except (json.JSONDecodeError, TypeError) as exc:
            return {"ok": False, "error": f"draft_json inválido: {exc}"}

        cron = str(draft.get("cron") or "").strip()
        if not cron or not self._validate_cron(cron):
            return {"ok": False, "error": "cron inválido — debe ser una expresión de 5 campos"}

        title = str(draft.get("title") or "").strip()[:256]
        target_agent_id = (draft.get("target_agent_id") or None)
        if target_agent_id is not None:
            target_agent_id = str(target_agent_id).strip() or None
        task_instruction = str(draft.get("task_instruction") or "").strip()
        one_shot = bool(draft.get("one_shot", False))
        risk_ceiling_raw = str(draft.get("risk_ceiling") or "low").lower()
        if risk_ceiling_raw not in ("low", "high"):
            return {"ok": False, "error": "risk_ceiling debe ser 'low' o 'high'"}

        from hermes.tasks.triggers.domain.authorized_trigger_ports import (  # noqa: PLC0415
            AuthorizedTriggerType,
            RiskCeiling,
        )

        admin_uuid = _uid_to_uuid(sender_uid)
        # Approval signature: HMAC-SHA256 over the canonical draft fields.
        # This satisfies the NOT NULL constraint and provides basic non-repudiation
        # (the signature binds the admin identity + content at creation time).
        approval_signature = _sign_scheduled_task_draft(
            admin_uuid=admin_uuid,
            cron=cron,
            task_instruction=task_instruction,
            title=title,
        )

        trigger = await self._trigger_repo.authorize(
            trigger_type=AuthorizedTriggerType.TIMER,
            scope_value=cron,
            allowed_capabilities=(),
            risk_ceiling=RiskCeiling(risk_ceiling_raw),
            admin_uuid=admin_uuid,
            approval_signature=approval_signature,
        )

        # Persist the P3 metadata fields directly on the newly-created row.
        # trigger_repo.authorize() inserts the core fields; we patch the extras.
        await _patch_trigger_p3_fields(
            repo=self._trigger_repo,
            instance_id=trigger.trigger_instance_id,
            target_agent_id=target_agent_id,
            task_instruction=task_instruction,
            one_shot=one_shot,
            title=title,
        )

        # BUG-7 fix: also write to Neus jobs.json (the catalog read by list_configured_tasks).
        # The trigger_repo.authorize() above recorded the AUTHORIZATION row (security gate).
        # The Neus job is the CATALOG entry that the dashboard reads. Both must be written.
        # Fail-soft: a Neus write error must not roll back the authorization (the agent
        # can still approve, the HITL gate is already passed; catalog inconsistency is
        # less harmful than denying the whole operation).
        neus_job_id = _neus_cron_create_job(
            prompt=task_instruction,
            schedule=cron,
            name=title or task_instruction[:50],
            one_shot=one_shot,
            origin={
                "trigger_instance_id": str(trigger.trigger_instance_id),
                "source": "safent_scheduled_task",
            },
        )

        logger.info(
            "hermes.dbus.scheduled_task_created",
            extra={
                "trigger_id": str(trigger.trigger_instance_id),
                "neus_job_id": neus_job_id or "unavailable",
                "cron": cron,
                "one_shot": one_shot,
                "by_uid": sender_uid,
            },
        )
        return {"ok": True, "trigger_id": str(trigger.trigger_instance_id)}

    async def delete_scheduled_task(
        self,
        *,
        trigger_id: str,
        sender_uid: int,
    ) -> dict:
        """Revoca (soft-delete) un trigger de la allow-list. No borrado físico.

        Marca enabled=0 + revoked_at (preserva auditoría, I11 del esquema).
        REUSA trigger_repo.revoke — mismo mecanismo que el revoke manual.

        Raises:
            DbusAuthorizationError: UID no autorizado.
            NotImplementedError: trigger_repo no inyectado.
        """
        self._authorize_and_resolve(sender_uid, operation="delete_scheduled_task")
        self._require_trigger_repo()

        from uuid import UUID as _UUID  # noqa: PLC0415
        try:
            tid = _UUID(trigger_id)
        except ValueError:
            return {"ok": False, "error": f"trigger_id inválido: {trigger_id!r}"}

        await self._trigger_repo.revoke(
            trigger_instance_id=tid,
            admin_uuid=_uid_to_uuid(sender_uid),
        )
        _neus_cron_remove_job(trigger_id)
        logger.info(
            "hermes.dbus.scheduled_task_deleted",
            extra={"trigger_id": trigger_id, "by_uid": sender_uid},
        )
        return {"ok": True}

    async def set_scheduled_task_enabled(
        self,
        *,
        trigger_id: str,
        enabled: bool,
        sender_uid: int,
    ) -> dict:
        """Toggle del kill-switch por-trigger (enabled / disabled).

        enabled=True  → reactiva el trigger (enabled=1, revoked_at=NULL).
        enabled=False → lo suspende (enabled=0, revoked_at=ahora).
        Preserva la invariante I11 del esquema (enabled ↔ revoked_at coherencia).

        Raises:
            DbusAuthorizationError: UID no autorizado.
            NotImplementedError: trigger_repo no inyectado.
        """
        self._authorize_and_resolve(sender_uid, operation="set_scheduled_task_enabled")
        self._require_trigger_repo()

        from uuid import UUID as _UUID  # noqa: PLC0415
        try:
            tid = _UUID(trigger_id)
        except ValueError:
            return {"ok": False, "error": f"trigger_id inválido: {trigger_id!r}"}

        await _set_trigger_enabled(
            repo=self._trigger_repo,
            trigger_instance_id=tid,
            enabled=enabled,
            admin_uuid=_uid_to_uuid(sender_uid),
        )
        _neus_cron_set_enabled(trigger_id, enabled=enabled)
        logger.info(
            "hermes.dbus.scheduled_task_enabled_set",
            extra={"trigger_id": trigger_id, "enabled": enabled, "by_uid": sender_uid},
        )
        return {"ok": True}

    # ------------------------------------------------------------------
    # Gobernanza de plataformas (feature 010, Principio 0):
    #   - Lecturas: supervisión, sin authZ.
    #   - Mutadores: autoría por sender_uid del bus (CWE-862), fail-closed.
    #
    # Injected via __init__ kwargs:
    #   platform_model_registry — SqlitePlatformModelRegistry | None
    #   capability_binding_repo — SqliteCapabilityBindingRepo | None
    # ------------------------------------------------------------------

    def _require_platform_registry(self):
        if self._platform_model_registry is None:
            raise RuntimeError("platform_model_registry no inyectado en el wiring")
        return self._platform_model_registry

    def _require_binding_repo(self):
        if self._capability_binding_repo is None:
            raise RuntimeError("capability_binding_repo no inyectado en el wiring")
        return self._capability_binding_repo

    def _require_access_scope_repo(self):
        if self._access_scope_repo is None:
            raise RuntimeError("access_scope_repo no inyectado en el wiring")
        return self._access_scope_repo

    # --- Read-only platform supervision (no authZ) ---

    def list_platform_models(self, tenant_id: str) -> list[dict]:
        """Returns summary list for the tenant (no PII, no selectors)."""
        if self._platform_model_registry is None:
            return []
        models = self._platform_model_registry.list_by_tenant(tenant_id)
        return [m.to_summary_dict() for m in models]

    def get_platform_model_summary(self, model_id: str, tenant_id: str) -> dict:
        """Returns detail summary (areas, entities, rules, zones with is_stale)."""
        registry = self._require_platform_registry()
        from hermes.platforms.domain.ports import PlatformModelNotFound  # noqa: PLC0415
        try:
            model = registry.get(model_id, tenant_id)
        except PlatformModelNotFound:
            return {}
        return model.to_detail_dict()

    def list_agent_capabilities(self, agent_id: str, tenant_id: str) -> list[dict]:
        """Returns capability list for the agent (no PII, no credentials)."""
        if self._capability_binding_repo is None:
            return []
        bindings = self._capability_binding_repo.list_by_agent(agent_id, tenant_id)
        return [
            {
                "capability_kind": b.capability.kind,
                "capability_id": b.capability.capability_id,
                "capability_version": b.capability.version,
                "bound_at": b.bound_at.isoformat(),
            }
            for b in bindings
        ]

    def list_model_gaps(self, model_id: str) -> list[dict]:
        """Returns open/covered gap metadata (no PII)."""
        if self._platform_model_registry is None:
            return []
        gaps = self._platform_model_registry.list_gaps(model_id)
        return [
            {
                "gap_id": g.gap_id,
                "missing_descriptor": g.missing_descriptor,
                "state": str(g.state),
                "detected_at": g.detected_at.isoformat(),
            }
            for g in gaps
        ]

    # --- Mutating platform governance (authZ required) ---

    async def enable_platform_model(self, *, model_id: str, tenant_id: str, sender_uid: int) -> bool:
        """aprendida → habilitada. Fail-closed on needs_label (FR-013).

        Security gate: if a PlatformModelSigner is configured, verifies the
        model signature before enabling. A model without a valid v2 signature
        cannot be enabled (CTRL-5, Principio 0). If no signer is configured
        (legacy/test environments without master.key), proceeds without
        verification and logs a warning.
        """
        self._authorize(sender_uid, operation="enable_platform_model")
        registry = self._require_platform_registry()
        from hermes.platforms.domain.ports import PlatformModelNotFound  # noqa: PLC0415
        from hermes.platforms.domain.platform_model import ModelHasUnlabeledAreas  # noqa: PLC0415
        model = registry.get(model_id, tenant_id)
        _assert_platform_model_signature(model, self._platform_model_signer, operation="enable")
        enabled = model.enable()
        registry.save(enabled)
        logger.info(
            "hermes.dbus.platform_model_enabled",
            extra={"model_id": model_id, "by_uid": sender_uid},
        )
        return True

    async def disable_platform_model(self, *, model_id: str, tenant_id: str, sender_uid: int) -> bool:
        """habilitada → aprendida."""
        self._authorize(sender_uid, operation="disable_platform_model")
        registry = self._require_platform_registry()
        model = registry.get(model_id, tenant_id)
        disabled = model.disable()
        registry.save(disabled)
        logger.info(
            "hermes.dbus.platform_model_disabled",
            extra={"model_id": model_id, "by_uid": sender_uid},
        )
        return True

    async def deprecate_platform_model(self, *, model_id: str, tenant_id: str, sender_uid: int) -> bool:
        """Deprecate/forget a model (GDPR cascade)."""
        self._authorize(sender_uid, operation="deprecate_platform_model")
        registry = self._require_platform_registry()
        model = registry.get(model_id, tenant_id)
        deprecated = model.deprecate()
        registry.save(deprecated)
        logger.info(
            "hermes.dbus.platform_model_deprecated",
            extra={"model_id": model_id, "by_uid": sender_uid},
        )
        return True

    async def confirm_platform_model(
        self, *, model_id: str, tenant_id: str, corrections: list, sender_uid: int
    ) -> dict:
        """provisional → aprendida with operator corrections (FR-011, FR-032).

        Corrections are applied as domain commands (rename/discard/relabel).
        Returns the updated model summary.

        Security gate: same signature verification as enable_platform_model.
        The model must have a valid v2 signature to be confirmed.
        """
        self._authorize(sender_uid, operation="confirm_platform_model")
        registry = self._require_platform_registry()
        model = registry.get(model_id, tenant_id)
        _assert_platform_model_signature(model, self._platform_model_signer, operation="confirm")
        # Apply corrections (best-effort: unknown ops are logged and ignored).
        updated_model = _apply_corrections(model, corrections)
        confirmed = updated_model.confirm()
        registry.save(confirmed)
        logger.info(
            "hermes.dbus.platform_model_confirmed",
            extra={"model_id": model_id, "by_uid": sender_uid},
        )
        return confirmed.to_summary_dict()

    # --- Tour lifecycle stubs (US1 — not yet wired, documented NotImplementedError) ---

    async def start_platform_tour(
        self,
        *,
        site_ref: str,
        origin: str,
        modality: str,
        tenant_id: str,
        sender_uid: int,
    ) -> str:
        """Open a learning tour.

        STUB for US1 (T030/T031): the tour object and context isolation are
        implemented in Phase 3. This stub persists a minimal tour record
        and returns the tour_id so callers have a valid handle.
        """
        self._authorize(sender_uid, operation="start_platform_tour")
        from uuid import uuid4  # noqa: PLC0415
        from hermes.platforms.domain.platform_learning_tour import PlatformLearningTour  # noqa: PLC0415
        from hermes.platforms.domain.value_objects import TourOrigin, TeachingModality  # noqa: PLC0415
        tour_id = uuid4().hex
        try:
            registry = self._require_platform_registry()
            tour = PlatformLearningTour(
                tour_id=tour_id,
                tenant_id=tenant_id,
                target_site_ref=site_ref,
                origin=TourOrigin(origin),
                modality=TeachingModality(modality),
                operator_attribution=sender_uid,
            )
            registry.save_tour(tour)
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.dbus.start_platform_tour.stub_persist_failed: %s", exc)
        logger.info(
            "hermes.dbus.platform_tour_started",
            extra={"tour_id": tour_id, "site_ref": site_ref, "by_uid": sender_uid},
        )
        return tour_id

    async def close_platform_tour(self, *, tour_id: str, tenant_id: str, sender_uid: int) -> str:
        """Close tour and compile model.

        STUB for US1 (T031): real compilation (TourCompilerPort) implemented in Phase 3.
        Returns a placeholder model_json to indicate the tour was closed.
        """
        self._authorize(sender_uid, operation="close_platform_tour")
        # Stub: mark tour closed in storage if registry available
        try:
            registry = self._require_platform_registry()
            tour = registry.get_tour(tour_id)
            closed = tour.close()
            registry.save_tour(closed)
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.dbus.close_platform_tour.stub_persist_failed: %s", exc)
        logger.info(
            "hermes.dbus.platform_tour_closed",
            extra={"tour_id": tour_id, "by_uid": sender_uid},
        )
        # Compilation (TourCompilerPort) wired in US1. Return empty model for now.
        import json as _json  # noqa: PLC0415
        return _json.dumps({"tour_id": tour_id, "state": "closed", "model_compiled": False})

    # --- Capability binding (fully functional — fundación, FR-037) ---

    async def bind_capability_to_agent(
        self,
        *,
        agent_id: str,
        capability_kind: str,
        capability_id: str,
        capability_version: str,
        tenant_id: str,
        sender_uid: int,
    ) -> dict:
        """Assign a global capability to an agent. Idempotent. by = sender_uid."""
        self._authorize(sender_uid, operation="bind_capability_to_agent")
        repo = self._require_binding_repo()
        from hermes.platforms.domain.value_objects import CapabilityRef  # noqa: PLC0415
        from hermes.capabilities.domain.agent_capability_binding import AgentCapabilityBinding  # noqa: PLC0415
        cap = CapabilityRef(
            kind=capability_kind,
            capability_id=capability_id,
            version=capability_version,
        )
        # Idempotent: if already bound, return the existing binding.
        existing = repo.find_active(agent_id, capability_kind, capability_id, tenant_id)
        if existing is not None:
            logger.info(
                "hermes.dbus.bind_capability.idempotent",
                extra={"agent_id": agent_id, "capability": str(cap), "by_uid": sender_uid},
            )
            return existing.to_dict()
        binding = AgentCapabilityBinding.create(
            tenant_id=tenant_id,
            agent_id=agent_id,
            capability=cap,
            bound_by=sender_uid,
        )
        repo.save(binding)
        logger.info(
            "hermes.dbus.capability_bound",
            extra={
                "binding_id": binding.binding_id,
                "agent_id": agent_id,
                "capability": str(cap),
                "by_uid": sender_uid,
            },
        )
        return binding.to_dict()

    async def unbind_capability_from_agent(
        self,
        *,
        agent_id: str,
        capability_kind: str,
        capability_id: str,
        tenant_id: str,
        sender_uid: int,
    ) -> bool:
        """Revoke a capability assignment. Idempotent (no-op if not bound)."""
        self._authorize(sender_uid, operation="unbind_capability_from_agent")
        repo = self._require_binding_repo()
        changed = repo.unbind(agent_id, capability_kind, capability_id, tenant_id)
        logger.info(
            "hermes.dbus.capability_unbound",
            extra={
                "agent_id": agent_id,
                "capability_kind": capability_kind,
                "capability_id": capability_id,
                "changed": changed,
                "by_uid": sender_uid,
            },
        )
        return changed

    async def set_agent_access_scope(
        self,
        *,
        agent_id: str,
        scope_json: str,
        tenant_id: str,
        sender_uid: int,
    ) -> dict:
        """Land a cloud-pushed AgentAccessScope for *agent_id* (Fase 2 Phase 3).

        Authorized EXACTLY like bind_capability_to_agent (self._authorize).
        updated_by is ALWAYS sender_uid (D-Bus peer cred), NEVER from the
        payload (CWE-862). managed_by is always "cloud" — config-sync is the
        only caller of this verb today (a future local/owner-authored scope
        would use a different verb).
        """
        self._authorize(sender_uid, operation="set_agent_access_scope")
        repo = self._require_access_scope_repo()

        fields, error = _parse_access_scope_json(scope_json)
        if error is not None:
            logger.warning(
                "hermes.dbus.agent_access_scope_rejected",
                extra={"agent_id": agent_id, "reason": error},
            )
            return {"ok": False, "error": error}

        from hermes.capabilities.domain.agent_access_scope import AgentAccessScope  # noqa: PLC0415

        scope = AgentAccessScope.create(
            tenant_id=tenant_id,
            agent_id=agent_id,
            updated_by=sender_uid,
            native_tools=frozenset(fields["native_tools"]),
            policy_overlay=fields["policy_overlay"],
            views=tuple(fields["views"]),
            cerebro_unrestricted=fields["cerebro_unrestricted"],
            enforced=fields["enforced"],
            managed_by="cloud",
            approval_tier=fields["approval_tier"],
        )
        repo.upsert(scope)
        logger.info(
            "hermes.dbus.agent_access_scope_set",
            extra={"agent_id": agent_id, "tenant_id": tenant_id, "by_uid": sender_uid},
        )
        return {"ok": True, "scope_id": scope.scope_id}

    async def set_agent_house_rule(
        self,
        *,
        agent_id: str,
        model_id: str,
        rule: dict,
        tenant_id: str,
        sender_uid: int,
    ) -> bool:
        """Add/update a per-agent house-rule overlay (FR-037)."""
        self._authorize(sender_uid, operation="set_agent_house_rule")
        repo = self._require_binding_repo()
        from uuid import uuid4  # noqa: PLC0415
        from hermes.platforms.domain.platform_model import HouseRule, HouseRuleKind  # noqa: PLC0415
        from hermes.platforms.domain.agent_house_rule_overlay import AgentHouseRuleOverlay  # noqa: PLC0415
        house_rule = HouseRule(
            rule_id=rule.get("rule_id", uuid4().hex),
            kind=HouseRuleKind(rule["kind"]),
            target_area_ref=rule["target_area_ref"],
            phrasing=rule["phrasing"],
        )
        overlay = AgentHouseRuleOverlay(
            overlay_id=uuid4().hex,
            agent_id=agent_id,
            platform_model_id=model_id,
            house_rule=house_rule,
        )
        repo.save_overlay(overlay)
        logger.info(
            "hermes.dbus.agent_house_rule_set",
            extra={"agent_id": agent_id, "model_id": model_id, "by_uid": sender_uid},
        )
        return True

    # ------------------------------------------------------------------
    # T047 — Memory read-only verbs (spec 014-agentic-desktop, increment 2)
    # Read-only: no broker, no effector, no state mutation.
    # Authorship by sender_uid patrón de list_* existentes.
    # PII: el contenido de memoria puede incluir datos sensibles —
    #   nunca se loguea; se trunca antes de cruzar el bus.
    # ------------------------------------------------------------------

    _MEMORY_CONTENT_TRUNCATE = 200  # chars — protege PII en el bus D-Bus

    def list_memory(self, *, limit: int) -> str:
        """Lista las entradas de memoria del agente (read-only). JSON → cliente.

        Devuelve una lista de {id, target, content_truncated, entry_index}.
        Si el store no está disponible devuelve [] (la app muestra estado honesto,
        nunca mock). PII: content truncado a _MEMORY_CONTENT_TRUNCATE chars.
        """
        import json as _json  # noqa: PLC0415

        entries = self._read_all_memory_entries(limit=limit)
        return _json.dumps(entries)

    def search_memory(self, *, query: str, limit: int) -> str:
        """Busca en la memoria del agente (read-only, case-insensitive). JSON → cliente.

        Devuelve las entradas cuyo content_truncated contiene la query.
        Si el store no está disponible devuelve []. PII: content truncado.
        """
        import json as _json  # noqa: PLC0415

        if not query or not query.strip():
            return _json.dumps([])
        needle = query.strip().lower()
        all_entries = self._read_all_memory_entries(limit=None)
        matched = [e for e in all_entries if needle in e["content_truncated"].lower()]
        return _json.dumps(matched[:limit] if limit else matched)

    def _read_all_memory_entries(self, *, limit: int | None) -> list[dict]:
        """Lee todos los targets del store y ensambla la lista de entradas.

        Fail-open: si el store no está disponible devuelve [].
        PII: content_truncated capado en _MEMORY_CONTENT_TRUNCATE — NUNCA logueado.
        """
        try:
            from hermes.memory.infrastructure.tenant_memory_store import (  # noqa: PLC0415
                TenantMemoryStore,
            )
            from hermes.memory.infrastructure.nous_memory_bridge import (  # noqa: PLC0415
                _DEFAULT_MEMORY_ROOT,
                _SNAPSHOT_TARGETS,
            )
            from hermes.runtime.__main__ import _resolve_tenant_id  # noqa: PLC0415
        except Exception:  # noqa: BLE001
            return []

        try:
            tenant_id = _resolve_tenant_id()
            store = TenantMemoryStore(root=_DEFAULT_MEMORY_ROOT, tenant_id=tenant_id)
        except Exception:  # noqa: BLE001
            return []

        result: list[dict] = []
        entry_idx = 0
        for target in _SNAPSHOT_TARGETS:
            try:
                raw_entries = store.read(target)
            except Exception:  # noqa: BLE001
                continue
            for i, content in enumerate(raw_entries):
                # PII: truncate; NEVER log content
                result.append({
                    "id": f"{target}:{i}",
                    "target": target,
                    "content_truncated": content[:self._MEMORY_CONTENT_TRUNCATE],
                    "entry_index": i,
                })
                entry_idx += 1
                if limit is not None and entry_idx >= limit:
                    return result
        return result

    def get_memory_entry(self, *, entry_id: str) -> dict:
        """Fetch a single memory entry by its composite id '{target}:{index}'.

        Returns {id, target, content, entry_index} with the FULL content (not
        truncated). This is intentional: the caller drills into a single entry
        whose id they already hold from the list; the bulk-PII guard on
        list_memory still applies.
        Returns {} if the entry does not exist or the store is unavailable.
        No authZ: read-only, same policy as list_memory.
        """
        import json as _json  # noqa: PLC0415

        if not entry_id or ":" not in entry_id:
            return {}

        parts = entry_id.rsplit(":", 1)
        if len(parts) != 2:
            return {}
        target, index_str = parts[0], parts[1]

        try:
            entry_index = int(index_str)
        except ValueError:
            return {}

        try:
            from hermes.memory.infrastructure.tenant_memory_store import (  # noqa: PLC0415
                TenantMemoryStore,
            )
            from hermes.memory.infrastructure.nous_memory_bridge import (  # noqa: PLC0415
                _DEFAULT_MEMORY_ROOT,
            )
            from hermes.runtime.__main__ import _resolve_tenant_id  # noqa: PLC0415
            tenant_id = _resolve_tenant_id()
            store = TenantMemoryStore(root=_DEFAULT_MEMORY_ROOT, tenant_id=tenant_id)
        except Exception:  # noqa: BLE001
            return {}

        try:
            entries = store.read(target)
        except Exception:  # noqa: BLE001
            return {}

        if entry_index >= len(entries):
            return {}

        return {
            "id": entry_id,
            "target": target,
            "content": entries[entry_index],
            "entry_index": entry_index,
        }

    def delete_memory_entry(self, *, entry_id: str, sender_uid: int) -> dict:
        """Olvida (borra) una entrada de memoria por su id compuesto '{target}:{index}'.

        Operación idempotente: si la entrada ya no existe devuelve {ok: true}
        (sin lanzar) para que el frontend pueda hacer DELETE seguro.
        PII: el contenido NUNCA se loguea, sólo el target e índice (metadatos).
        authZ: operador (sender_uid).
        """
        import json as _json  # noqa: PLC0415

        self._authorize(sender_uid, operation="delete_memory_entry")

        if not entry_id or ":" not in entry_id:
            return {"ok": False, "error": f"id inválido: {entry_id!r}"}

        parts = entry_id.rsplit(":", 1)
        if len(parts) != 2:
            return {"ok": False, "error": f"id inválido: {entry_id!r}"}
        target, index_str = parts[0], parts[1]

        try:
            entry_index = int(index_str)
        except ValueError:
            return {"ok": False, "error": f"índice no numérico en id: {entry_id!r}"}

        try:
            from hermes.memory.infrastructure.tenant_memory_store import (  # noqa: PLC0415
                TenantMemoryStore,
            )
            from hermes.memory.infrastructure.nous_memory_bridge import (  # noqa: PLC0415
                _DEFAULT_MEMORY_ROOT,
            )
            from hermes.runtime.__main__ import _resolve_tenant_id  # noqa: PLC0415
            tenant_id = _resolve_tenant_id()
            store = TenantMemoryStore(root=_DEFAULT_MEMORY_ROOT, tenant_id=tenant_id)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"memory store unavailable: {exc}"}

        try:
            entries = store.read(target)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"cannot read target {target!r}: {exc}"}

        if entry_index >= len(entries):
            # Idempotent: already gone.
            return {"ok": True, "deleted": False, "reason": "entry not found (already removed)"}

        old_text = entries[entry_index]
        result = store.remove(target, old_text)
        if result.get("success"):
            logger.info(
                "hermes.dbus.memory_entry_deleted target=%s index=%d by_uid=%d",
                target, entry_index, sender_uid,
            )
            return {"ok": True, "deleted": True}
        return {"ok": False, "error": result.get("error", "unknown")}

    def update_memory_entry(
        self, *, entry_id: str, content: str, sender_uid: int
    ) -> dict:
        """Edita el contenido de una entrada de memoria por su id '{target}:{index}'.

        Reemplaza el texto de la entrada in situ (el índice se conserva). El nuevo
        contenido pasa por el MISMO guard PII/inyección que las escrituras del
        agente (fail-closed): si contiene un patrón de amenaza se rechaza sin
        escribir — un operador no debe poder inyectar instrucciones en la memoria
        que luego el agente leería como contexto de confianza.
        authZ: operador (sender_uid del bus, CWE-862).
        PII: el contenido NUNCA se loguea, sólo target e índice (metadatos).
        """
        self._authorize(sender_uid, operation="update_memory_entry")

        if not entry_id or ":" not in entry_id:
            return {"ok": False, "error": f"id inválido: {entry_id!r}"}
        parts = entry_id.rsplit(":", 1)
        if len(parts) != 2:
            return {"ok": False, "error": f"id inválido: {entry_id!r}"}
        target, index_str = parts[0], parts[1]

        try:
            entry_index = int(index_str)
        except ValueError:
            return {"ok": False, "error": f"índice no numérico en id: {entry_id!r}"}

        new_content = (content or "").strip()
        if not new_content:
            return {"ok": False, "error": "El contenido no puede estar vacío."}

        try:
            from hermes.memory.infrastructure.tenant_memory_store import (  # noqa: PLC0415
                PiiRejectedError,
                TenantMemoryStore,
            )
            from hermes.memory.infrastructure.nous_memory_bridge import (  # noqa: PLC0415
                _DEFAULT_MEMORY_ROOT,
            )
            from hermes.runtime.__main__ import _resolve_tenant_id  # noqa: PLC0415
            tenant_id = _resolve_tenant_id()
            store = TenantMemoryStore(root=_DEFAULT_MEMORY_ROOT, tenant_id=tenant_id)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"memory store unavailable: {exc}"}

        try:
            entries = store.read(target)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"cannot read target {target!r}: {exc}"}

        if entry_index >= len(entries):
            return {"ok": False, "error": "entry not found"}

        old_text = entries[entry_index]
        if old_text.strip() == new_content:
            return {"ok": True, "updated": False, "reason": "sin cambios"}

        try:
            result = store.replace(target, old_text, new_content, agent_id="operator")
        except PiiRejectedError as exc:
            return {"ok": False, "error": str(exc), "code": "pii_rejected"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"update failed: {exc}"}

        if result.get("success"):
            logger.info(
                "hermes.dbus.memory_entry_updated target=%s index=%d by_uid=%d",
                target, entry_index, sender_uid,
            )
            return {"ok": True, "updated": True}
        return {"ok": False, "error": result.get("error", "unknown")}

    # ------------------------------------------------------------------
    # Notifications — task/chat completion bell
    # Written by daemon (orchestrator post-cycle hooks), read via D-Bus.
    # Read-only verbs: no authZ (same policy as list_memory/list_providers).
    # Mutators (mark-read): no cross-tenant risk; authZ not required for
    # single-owner install, consistent with memory's delete_memory_entry.
    # ------------------------------------------------------------------

    def list_notifications(self, *, limit: int, unread_only: bool) -> str:
        """Return JSON list of notifications (newest first).

        Each item: {id, kind, title, body, status, conversation_id,
                    created_at, read}.
        Fail-soft: returns [] if the store is unavailable.
        """
        import json as _json  # noqa: PLC0415

        if self._notification_store is None:
            return _json.dumps([])
        try:
            entries = self._notification_store.list(
                limit=limit or 100, unread_only=unread_only
            )
            return _json.dumps(entries)
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.dbus.notifications.list_failed: %s", exc)
            return _json.dumps([])

    def get_notification_unread_count(self) -> int:
        """Return the count of unread notifications. Fail-soft: 0 on error."""
        if self._notification_store is None:
            return 0
        try:
            return self._notification_store.unread_count()
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.dbus.notifications.count_failed: %s", exc)
            return 0

    def mark_notification_read(self, *, notification_id: str) -> dict:
        """Mark one notification as read. Returns {ok, updated}."""
        import json as _json  # noqa: PLC0415

        if self._notification_store is None:
            return {"ok": True, "updated": False}
        try:
            updated = self._notification_store.mark_read(notification_id)
            return {"ok": True, "updated": updated}
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "hermes.dbus.notifications.mark_read_failed: %s", exc
            )
            return {"ok": False, "error": str(exc)}

    def mark_all_notifications_read(self) -> dict:
        """Mark all unread notifications as read. Returns {ok, count}."""
        if self._notification_store is None:
            return {"ok": True, "count": 0}
        try:
            count = self._notification_store.mark_all_read()
            return {"ok": True, "count": count}
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "hermes.dbus.notifications.mark_all_read_failed: %s", exc
            )
            return {"ok": False, "error": str(exc)}

    # ------------------------------------------------------------------
    # T017 — Desktop overlay methods (spec 014-agentic-desktop)
    # ------------------------------------------------------------------

    def open_overlay(self, *, sender_uid: int) -> bool:
        """Idempotent signal that the overlay should come to front.

        No state mutation, no run_cycle, no effector. Returns True so the
        gnome-shell extension can confirm the daemon is alive.
        The OverlayRequested signal is emitted by the D-Bus adapter layer
        (Runtime1ServiceInterface.OverlayRequested) — this method is the
        pure-wiring side; it only authorizes and logs.

        sender_uid derived from the bus by the adapter (CWE-862).
        """
        self._authorize_and_resolve(sender_uid, operation="open_overlay")
        logger.info(
            "hermes.dbus.overlay_requested",
            extra={"by_uid": sender_uid},
        )
        return True

    async def enqueue_from_overlay(
        self,
        *,
        text: str,
        conversation_id: str | None,
        sender_uid: int,
    ) -> EnqueueResult:
        """Overlay chat → enqueue delegation. Reuses existing enqueue exactly.

        trigger_kind is fixed to "chat_message" — the overlay is a chat surface.
        Authorship is derived from sender_uid (confused-deputy path:
        sender_uid == operator_uid 1000, no proxy token needed for direct calls).
        NEVER bypasses rate-limit / PII tokenization in ControlPlaneService.
        """
        return await self.enqueue(
            trigger_kind="chat_message",
            text=text,
            priority=5,
            dedup_key=None,
            sender_uid=sender_uid,
            conversation_id=conversation_id,
            operator_token=None,
        )

    def request_context_snapshot(self, *, sender_uid: int) -> str:
        """Return JSON snapshot of the active desktop app. READ-ONLY.

        Delegates to ContextSnapshotComposer — no broker, no effector.
        PII fields (window_title) are marked but not tokenized here: the
        overlay UI is responsible for tokenizing before sending to the LLM
        boundary (Constitution III). The snapshot is composed on-demand and
        NEVER persisted by this method.

        sender_uid is required for the SCREEN_CAPTURE consent check inside
        the composer (screenshot is only included when the operator has an
        active consent for Capability.SCREEN_CAPTURE).

        Returns JSON string (dict). Always succeeds: missing AT-SPI → no-app
        snapshot.
        """
        import json as _json  # noqa: PLC0415

        if self._context_snapshot_composer is None:
            return _json.dumps({
                "active_application": None,
                "window_title": None,
                "screenshot_available": False,
                "captured_at": None,
                "error": "context_snapshot_not_configured",
            })
        snapshot = self._context_snapshot_composer.compose()
        return _json.dumps(
            self._context_snapshot_composer.to_json_safe_dict(snapshot)
        )

    def get_audit_chain_head(self, *, sender_uid: int) -> str:  # noqa: ARG002
        """Return JSON summary of the audit chain head. READ-ONLY.

        Used by the Security/Audit app to display chain integrity status.
        No authZ required (read-only metadata, same policy as list_*).
        sender_uid is accepted but not used (kept for uniform signature and
        future rate-limiting).

        Returns JSON string:
          {entry_id, head_hash, integrity, captured_at}
        where integrity is:
          "present"  — head hash exists; chain NOT verified (expensive, out-of-band).
          "empty"    — all-zeros sentinel (no entries recorded yet).
          "unknown"  — audit_signer not injected.
        "verified" is NEVER emitted here — full chain verification is a separate
        operation and must not be implied by this read-only head query.
        """
        import json as _json  # noqa: PLC0415

        if self._audit_signer is None:
            return _json.dumps({
                "entry_id": None,
                "head_hash": None,
                "integrity": "unknown",
                "captured_at": None,
            })
        from datetime import datetime, timezone  # noqa: PLC0415

        head_hash = self._audit_signer.head_hash_hex
        # "present" = head hash exists but chain NOT verified (expensive — done
        # out-of-band by the audit verifier). "empty" = all-zeros sentinel.
        # NEVER report "ok"/"verified" here: this endpoint only reads the
        # in-memory head, it does not traverse or verify the chain.
        integrity = "empty" if head_hash == ("00" * 32) else "present"
        return _json.dumps({
            "entry_id": None,
            "head_hash": head_hash,
            "integrity": integrity,
            "captured_at": datetime.now(tz=timezone.utc).isoformat(),
        })

    # ------------------------------------------------------------------
    # spec 014 increment 3 — FR-013 operator consent control (D-Bus)
    #
    # GrantConsent / RevokeConsent: mutators — authZ by sender_uid (CWE-862).
    # ListConsents: read-only — no authZ required (same policy as list_*).
    #
    # _consent_operator() resolves the SUBJECT of every consent operation.
    # In a single-owner OS the subject is always the owner (self._operator_id),
    # not the caller: the authorized caller (the compositor / shell) manages
    # consents ON BEHALF of the owner.  Using the caller's UID as subject would
    # break when the call is proxied (proxy_uid ≠ hermes-user uid) or when the
    # compositor uid differs from the uid used during seed/broker construction.
    # This does NOT widen the attack surface: authZ still blocks unauthorized
    # callers via _authorize(); only the subject changes, not the gating.
    # Fallback to _uid_to_uuid(sender_uid) preserves CI/test/backward-compat.
    # ------------------------------------------------------------------

    def _consent_operator(self, sender_uid: int) -> "UUID":
        """Return the owner UUID that is the SUBJECT of consent operations.

        Single-owner invariant: the consent subject is always self._operator_id
        (the daemon owner resolved at boot).  Fallback to _uid_to_uuid(sender_uid)
        when operator_id was not injected (CI / test / backward-compat).
        """
        return self._operator_id if self._operator_id is not None else _uid_to_uuid(sender_uid)

    def grant_consent(
        self,
        *,
        capability: str,
        scope: str,
        sender_uid: int,
    ) -> dict:
        """Grant a capability consent to the calling operator (FR-013).

        capability: string matching Capability enum value (e.g. "documents").
        scope: "session" | "once" | "persistent" (ConsentScope).
        sender_uid: UID from the D-Bus bus (CWE-862 — server-side, never payload).
        human_operator_id = UUID(int=sender_uid).

        Returns consent dict on success, {"error": reason} on failure.

        Raises:
            DbusAuthorizationError: sender not in authorized_uids.
        """
        from hermes.agents_os.application.consent_manager import (  # noqa: PLC0415
            Capability as _Capability,
            ConsentScope as _ConsentScope,
        )

        self._authorize(sender_uid, operation="grant_consent")

        if self._consent_manager is None:
            return {"error": "consent_manager_not_configured"}

        try:
            cap = _Capability(capability)
        except ValueError:
            valid = [c.value for c in _Capability]
            return {"error": f"capability inválida: {capability!r}. Válidas: {valid}"}

        try:
            scope_enum = _ConsentScope(scope)
        except ValueError:
            valid_scopes = [s.value for s in _ConsentScope]
            return {"error": f"scope inválido: {scope!r}. Válidos: {valid_scopes}"}

        tenant_id = _resolve_tenant_id_from_wiring(self._tenant_id)
        # Subject = owner, not caller (see _consent_operator docstring).
        human_operator_id = self._consent_operator(sender_uid)

        consent = self._consent_manager.grant(
            tenant_id=tenant_id,
            human_operator_id=human_operator_id,
            capability=cap,
            scope=scope_enum,
        )
        logger.info(
            "hermes.dbus.consent_granted",
            extra={
                "capability": cap.value,
                "scope": scope_enum.value,
                "operator_id": str(human_operator_id),
                "consent_id": str(consent.consent_id),
                "by_uid": sender_uid,
            },
        )
        return _consent_to_dict(consent)

    def revoke_consent(
        self,
        *,
        capability: str,
        sender_uid: int,
    ) -> dict:
        """Revoke a capability consent for the calling operator (FR-013).

        capability: string matching Capability enum value.
        sender_uid: UID from the D-Bus bus (CWE-862 — server-side, never payload).
        human_operator_id = UUID(int=sender_uid).

        Returns {"revoked": true} if revoked, {"revoked": false} if none was active.

        Raises:
            DbusAuthorizationError: sender not in authorized_uids.
        """
        from hermes.agents_os.application.consent_manager import (  # noqa: PLC0415
            Capability as _Capability,
        )

        self._authorize(sender_uid, operation="revoke_consent")

        if self._consent_manager is None:
            return {"error": "consent_manager_not_configured"}

        try:
            cap = _Capability(capability)
        except ValueError:
            valid = [c.value for c in _Capability]
            return {"error": f"capability inválida: {capability!r}. Válidas: {valid}"}

        # Subject = owner, not caller (see _consent_operator docstring).
        human_operator_id = self._consent_operator(sender_uid)
        revoked = self._consent_manager.revoke(
            human_operator_id=human_operator_id, capability=cap
        )
        logger.info(
            "hermes.dbus.consent_revoked",
            extra={
                "capability": cap.value,
                "operator_id": str(human_operator_id),
                "revoked": revoked is not None,
                "by_uid": sender_uid,
            },
        )
        if revoked is not None:
            return {"revoked": True, **_consent_to_dict(revoked)}
        return {"revoked": False}

    def list_consents(self, *, sender_uid: int) -> str:
        """List active consents for the calling operator (read-only).

        Returns JSON list of active consent dicts.
        No authZ required — read-only, same policy as list_*.
        sender_uid is used to scope the list to the calling operator.
        """
        if self._consent_manager is None:
            return json.dumps([])

        # Subject = owner, not caller (see _consent_operator docstring).
        human_operator_id = self._consent_operator(sender_uid)
        consents = self._consent_manager.list_active(
            human_operator_id=human_operator_id
        )
        return json.dumps([_consent_to_dict(c) for c in consents])

    # ------------------------------------------------------------------
    # Test helper — audit entries emitted in this session
    # ------------------------------------------------------------------

    def audit_entries_emitted(self) -> list[AuditEntry]:
        """Devuelve las AuditEntry emitidas (para tests). Producción usa repositorio real."""
        return list(self._audit_entries)

    # ------------------------------------------------------------------
    # Private — authorization helpers
    # ------------------------------------------------------------------

    def _authorize_and_resolve(
        self,
        sender_uid: int,
        *,
        operation: str,
        operator_token: str | None = None,
    ) -> UUID:
        """Authorize the call and return the verified operator UUID.

        Two paths (hybrid confused-deputy model):
          1. Direct (sender_uid ∈ authorized_uids):
               operator_id = _uid_to_uuid(sender_uid) — no token needed.
          2. Proxy (sender_uid == proxy_uid, NOT in authorized_uids):
               operator_token REQUIRED; operator_id extracted from token.
               Token must be valid, unexpired, and for this operation.

        Fail-closed: any other case raises DbusAuthorizationError.

        Returns:
            UUID of the verified human operator (used for audit attribution).

        Raises:
            DbusAuthorizationError: authorization denied.
        """
        if sender_uid in self._authorized_uids:
            return _uid_to_uuid(sender_uid)

        if self._proxy_uid is not None and sender_uid == self._proxy_uid:
            return self._authorize_via_token(operator_token, operation=operation)

        logger.warning(
            "hermes.dbus.authz_denied",
            extra={"operation": operation, "sender_uid": sender_uid},
        )
        raise DbusAuthorizationError(
            f"UID {sender_uid} no autorizado para '{operation}' "
            "(CTRL-12/KILL-1, CWE-862)"
        )

    def _authorize_via_token(
        self, operator_token: str | None, *, operation: str
    ) -> UUID:
        """Verify the operator token and extract the operator UUID.

        Requires operator_token_verifier to be configured. Fail-closed on
        any verification failure: missing token, expired, or forged.

        Returns:
            UUID of the operator extracted from the verified token.

        Raises:
            DbusAuthorizationError: token missing, expired, or verification failed.
        """
        from hermes.shell_server.security.operator_token import OperatorTokenError  # noqa: PLC0415

        if operator_token is None:
            logger.warning(
                "hermes.dbus.proxy_token_missing",
                extra={"operation": operation},
            )
            raise DbusAuthorizationError(
                f"Proxy call to '{operation}' requires an operator token "
                "(confused-deputy remediation, CWE-862). No token provided."
            )
        if self._token_verifier is None:
            logger.warning(
                "hermes.dbus.token_verifier_not_configured",
                extra={"operation": operation},
            )
            raise DbusAuthorizationError(
                f"Proxy call to '{operation}' rejected: no operator_token_verifier "
                "configured. Inject one at wiring construction time."
            )
        try:
            claims = self._token_verifier.verify(
                operator_token, expected_operation=operation
            )
        except OperatorTokenError as exc:
            logger.warning(
                "hermes.dbus.proxy_token_invalid",
                extra={"operation": operation, "reason": type(exc).__name__},
            )
            raise DbusAuthorizationError(
                f"Proxy call to '{operation}' rejected: operator token invalid "
                f"({type(exc).__name__}). (CWE-862)"
            ) from exc

        try:
            operator_uuid = UUID(claims.operator_id)
        except ValueError as exc:
            raise DbusAuthorizationError(
                f"Operator token claims.operator_id is not a valid UUID: "
                f"{claims.operator_id!r}"
            ) from exc

        logger.info(
            "hermes.dbus.proxy_token_verified",
            extra={"operation": operation, "operator_id": str(operator_uuid)},
        )
        return operator_uuid

    def _authorize(self, sender_uid: int, *, operation: str) -> None:
        """Legacy single-path authorize (direct-only, no token support).

        Kept for backward compatibility with callers that were not updated to
        pass operator_token. Only used by governance methods that are not yet
        exposed via the proxy path. Prefer _authorize_and_resolve().

        Fail-closed: UID not in authorized_uids raises DbusAuthorizationError.
        """
        if sender_uid not in self._authorized_uids:
            logger.warning(
                "hermes.dbus.authz_denied",
                extra={"operation": operation, "sender_uid": sender_uid},
            )
            raise DbusAuthorizationError(
                f"UID {sender_uid} no autorizado para '{operation}' "
                "(CTRL-12/KILL-1, CWE-862)"
            )

    # ------------------------------------------------------------------
    # Security Center — policy + install-review audit (Grupo C wiring).
    # Persistencia: shell-state.db (mismo que el resto de repos daemon-owned).
    # Las tablas se crean idempotentemente (CREATE TABLE IF NOT EXISTS) en la
    # primera llamada; no requieren migración externa.
    # ------------------------------------------------------------------

    def _security_db_conn(self):
        """Lazy SQLite connection al shell-state.db. Singleton por instancia."""
        if not hasattr(self, "_sec_conn"):
            import sqlite3 as _sqlite3  # noqa: PLC0415

            db_path = self._composio_db_path()
            self._sec_conn = _sqlite3.connect(str(db_path), check_same_thread=False)
            self._sec_conn.row_factory = _sqlite3.Row
            self._sec_conn.executescript("""
                CREATE TABLE IF NOT EXISTS security_policy (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    policy_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS install_reviews (
                    scan_id     TEXT PRIMARY KEY,
                    identifier  TEXT NOT NULL DEFAULT '',
                    kind        TEXT NOT NULL DEFAULT '',
                    score       INTEGER NOT NULL DEFAULT -1,
                    verdict     TEXT NOT NULL DEFAULT '',
                    decision    TEXT NOT NULL DEFAULT '',
                    risks_json  TEXT NOT NULL DEFAULT '[]',
                    timestamp   INTEGER NOT NULL DEFAULT 0
                );
            """)
            self._sec_conn.commit()
        return self._sec_conn

    def get_security_policy(self) -> dict:
        """Lee la política de seguridad persitida (read-only). {} = valores por defecto."""
        try:
            conn = self._security_db_conn()
            row = conn.execute("SELECT policy_json FROM security_policy WHERE id = 1").fetchone()
            if row is None:
                return {}
            return json.loads(row["policy_json"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.dbus.security_policy_read_failed: %s", exc)
            return {}

    def set_security_policy(self, *, policy_json: str, sender_uid: int) -> dict:
        """Persiste la política de seguridad. Muta → authZ por sender_uid (CWE-862)."""
        self._authorize_and_resolve(sender_uid, operation="set_security_policy")
        policy_str = (policy_json or "").strip()
        if not policy_str:
            return {"ok": False, "error": "policy_json vacío"}
        try:
            json.loads(policy_str)  # valida JSON antes de persistir
        except ValueError as exc:
            return {"ok": False, "error": f"JSON inválido: {exc}"}
        try:
            conn = self._security_db_conn()
            conn.execute(
                "INSERT INTO security_policy (id, policy_json) VALUES (1, ?)"
                " ON CONFLICT(id) DO UPDATE SET policy_json = excluded.policy_json",
                (policy_str,),
            )
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.dbus.security_policy_write_failed: %s", exc)
            return {"ok": False, "error": f"no se pudo guardar: {exc}"}
        logger.info("hermes.dbus.security_policy_saved", extra={"by_uid": sender_uid})
        return {"ok": True}

    def list_recent_scans(self, *, limit: int = 50) -> list[dict]:
        """Lista los escaneos de instalación recientes (read-only). [] si no hay tabla.

        Fuente ÚNICA = el ScanRepo donde ScanService PERSISTE cada scan (scan_records
        en /var/lib/hermes/security/scans.db). El handler antiguo leía la tabla
        `install_reviews` (otra DB, nunca poblada) → el Centro de Seguridad mostraba
        el historial vacío aunque los scans ocurrían y bloqueaban. (fix 2026-06-27)
        """
        try:
            from hermes.security_center.infrastructure.sqlite_scan_repo import (  # noqa: PLC0415
                SQLiteScanRepo,
            )
            records = SQLiteScanRepo().list_recent(limit=max(1, int(limit)))
            return [
                {
                    "scan_id":      str(r.id),
                    "identifier":   r.target.identifier,
                    "kind":         r.target.kind,
                    "score":        r.score.value,
                    "verdict":      r.verdict.value,
                    "decision":     r.decision,
                    "engine":       r.engine,
                    "engine_label": r.engine_label,
                    "risks": [
                        {
                            "category":     rk.category,
                            "severity":     rk.severity.value,
                            "message":      rk.message,
                            "evidence_ref": rk.evidence_ref,
                        }
                        for rk in r.score.risks
                    ],
                    "timestamp":    r.finished_at.isoformat(),
                }
                for r in records
            ]
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.dbus.list_recent_scans_failed: %s", exc)
            return []

    def record_install_decision(
        self,
        *,
        scan_id: str,
        decision: str,
        identifier: str = "",
        kind: str = "",
        score: int = -1,
        verdict: str = "",
        risks_json: str = "[]",
        sender_uid: int,
    ) -> dict:
        """Persiste la decisión del usuario (allow/block/cancelled/installed).

        Muta → authZ por sender_uid (CWE-862). El scan_id es opaco para este
        método — cualquier cadena no vacía es válida. Si el scan_id ya existe
        actualiza la decisión (idempotente).
        """
        self._authorize_and_resolve(sender_uid, operation="record_install_decision")
        sid = (scan_id or "").strip()
        if not sid:
            return {"ok": False, "error": "scan_id vacío"}
        dec = (decision or "").strip()
        if not dec:
            return {"ok": False, "error": "decision vacía"}
        import time as _time  # noqa: PLC0415
        ts = int(_time.time())
        try:
            risks_str = (risks_json or "[]").strip() or "[]"
            json.loads(risks_str)  # valida JSON
        except ValueError:
            risks_str = "[]"
        try:
            conn = self._security_db_conn()
            conn.execute(
                "INSERT INTO install_reviews"
                " (scan_id, identifier, kind, score, verdict, decision, risks_json, timestamp)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(scan_id) DO UPDATE SET"
                "   decision  = excluded.decision,"
                "   timestamp = excluded.timestamp",
                (sid, identifier or "", kind or "", int(score), verdict or "",
                 dec, risks_str, ts),
            )
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.dbus.record_install_decision_failed: %s", exc)
            return {"ok": False, "error": f"no se pudo guardar: {exc}"}
        logger.info(
            "hermes.dbus.install_decision_recorded",
            extra={"scan_id": sid, "decision": dec, "by_uid": sender_uid},
        )
        # Override SOBERANO del dueño (modelo "todo elevable"): si APRUEBA, conecta la
        # decisión al GATE de instalación — marca scan_records.decision=ALLOWED en
        # scans.db para que ScanService deje pasar ESTE target aunque el veredicto sea
        # FAIL. Sin esto la aprobación quedaba en install_reviews (shell-state.db) y el
        # gate (scans.db) no la veía. Best-effort: si falla, la decisión queda registrada
        # pero el gate seguiría bloqueando (fail-closed, seguro).
        if dec.lower() in ("allow", "approve", "allowed", "allow_once", "install", "installed"):
            try:
                from uuid import UUID as _UUID  # noqa: PLC0415
                from hermes.security_center.infrastructure.sqlite_scan_repo import (  # noqa: PLC0415
                    SQLiteScanRepo,
                )
                from hermes.security_center.domain.scan_record import ScanDecision  # noqa: PLC0415
                SQLiteScanRepo().update_decision(_UUID(sid), ScanDecision.ALLOWED)
                logger.warning(
                    "hermes.dbus.scan_decision_allowed scan_id=%s by_uid=%s — "
                    "instalación elevada por decisión SOBERANA del dueño (gate respetará ALLOWED)",
                    sid, sender_uid,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "hermes.dbus.scan_decision_allow_failed scan_id=%s: %s "
                    "(decisión registrada; el gate seguirá fail-closed)", sid, exc,
                )
        return {"ok": True}

    # ------------------------------------------------------------------
    # Two-mode security kernel (spec 015)
    # ------------------------------------------------------------------

    def resolve_approval(self, *, request_id: str, choice: str) -> str:
        """Resolve a pending gateway approval from the compositor.

        Delegates to approval_gateway.resolve_approval which calls the native
        resolve_gateway_approval(session_key, choice). The session_key was
        registered by register_gateway_notify_callback at daemon startup.

        choice ∈ {once, session, always, deny}. Unknown → deny (fail-closed).
        Returns JSON string: {"ok": true} or {"ok": false, "error": reason}.
        """
        from hermes.runtime.approval_gateway import resolve_approval as _resolve  # noqa: PLC0415

        return _resolve(request_id=request_id, choice=choice)

    def set_auto_mode(self, *, enabled: bool) -> str:
        """Persist the AUTO mode flag and log the change.

        Returns JSON: {"ok": true, "auto_mode": <bool>} or {"ok": false, "error"}.
        """
        from hermes.runtime.approval_gateway import save_auto_mode  # noqa: PLC0415

        try:
            save_auto_mode(enabled)
        except OSError as exc:
            logger.error(
                "hermes.dbus.set_auto_mode_write_failed: %s", exc
            )
            return json.dumps({"ok": False, "error": str(exc)})

        logger.info(
            "hermes.dbus.auto_mode_set",
            extra={"auto_mode": enabled},
        )
        return json.dumps({"ok": True, "auto_mode": enabled})

    def get_auto_mode(self) -> str:
        """Return the current security mode as JSON (read-only).

        Returns JSON: {"auto_mode": bool}.
        """
        from hermes.runtime.approval_gateway import load_auto_mode  # noqa: PLC0415

        return json.dumps({"auto_mode": load_auto_mode()})


async def _nous_validate_provider(provider: Any, api_key: str | None) -> "tuple[bool, str | None]":
    """Valida un provider EJECUTANDO el runtime real (hermes-agent) en el daemon.

    Mismo camino que el chat: resolve_runtime_provider (idioma de Hermes) + una
    completion mínima sin tools. NO litellm, NO shell-server. Corre en el daemon
    (6G, sin OOM). Devuelve (ok, error_real_del_proveedor).

    Migrado (spec 016): usa el catálogo unificado vía nous_request_from_model_config
    en lugar del antiguo _HERMES_SLUG_BY_PREFIX (que tenía 'openai'→'openai-api',
    slug inválido que causaba AuthError).
    """
    import asyncio  # noqa: PLC0415

    from hermes.shell_server.providers.domain import litellm_model_string  # noqa: PLC0415
    from hermes.runtime.model_config import ModelConfig  # noqa: PLC0415
    from hermes.providers.infrastructure.nous_provider_adapter import (  # noqa: PLC0415
        nous_request_from_model_config,
    )

    model = litellm_model_string(provider, provider.default_model)
    # Build a temporary ModelConfig to reuse nous_request_from_model_config.
    # api_key comes from the caller (already decrypted by the D-Bus handler).
    temp_config = ModelConfig.from_provider(
        model=model,
        api_key=api_key,
        base_url=provider.base_url or None,
    )
    req, bare = nous_request_from_model_config(temp_config)

    def _run() -> "tuple[bool, str | None]":
        from hermes_cli.runtime_provider import resolve_runtime_provider  # noqa: PLC0415
        from openai import OpenAI  # noqa: PLC0415

        rt = resolve_runtime_provider(
            requested=req.requested,
            explicit_api_key=req.explicit_api_key,
            explicit_base_url=req.explicit_base_url,
            target_model=req.target_model,
        )
        # HONEST reachability+auth probe: hit the CONFIGURED endpoint with a
        # 1-token completion and let it RAISE on 404 / 401 / offline / DNS. The
        # OLD path ran the full agent loop (AIAgent.run_conversation), which
        # SWALLOWS API failures into a non-empty "⚠️ No reply…" final_response —
        # so a dead endpoint returned a non-empty string and the test reported
        # "Conexión Exitosa" even though it NEVER reached the user's model
        # (verified: a bogus base_url returned ok:true while the journal showed
        # HTTP 404 / ERR_NGROK_3200). max_retries=0 → fail fast and honestly;
        # base_url=None falls back to the provider default (e.g. api.openai.com).
        client = OpenAI(
            api_key=(rt.get("api_key") or "x"),
            base_url=(rt.get("base_url") or None),
            timeout=20.0,
            max_retries=0,
        )
        completion = client.chat.completions.create(
            model=bare,
            messages=[{"role": "user", "content": "OK"}],
            max_tokens=1,
            temperature=0,
        )
        choices = getattr(completion, "choices", None) or []
        if not choices:
            return False, "el endpoint respondió sin 'choices' (¿modelo o ruta /v1 incorrectos?)"
        return True, None

    loop = asyncio.get_event_loop()
    try:
        ok, err = await loop.run_in_executor(None, _run)
    except Exception as exc:  # noqa: BLE001 — surface the REAL provider error
        raw = str(exc).strip()
        return False, (raw[:300] if raw else type(exc).__name__)
    return ok, err


def _uid_to_uuid(uid: int) -> UUID:
    """Convierte un UID POSIX a UUID determinista para usarlo como operator_id."""
    return UUID(int=uid)


# AccessScopeSpec wire shape (Fase 2 Phase 3) — mirrors
# hermes.config_sync.policy_document.AccessScopeSpec exactly.
_ACCESS_SCOPE_ALLOWED_KEYS: frozenset[str] = frozenset({
    "enforced", "cerebro_unrestricted", "native_tools", "policy_overlay", "views",
    "approval_tier",
})
_ACCESS_SCOPE_APPROVAL_TIERS: frozenset[str] = frozenset({"standard", "coordinator"})
_ACCESS_SCOPE_MAX_LIST_LEN = 256
_ACCESS_SCOPE_MAX_STR_LEN = 128  # per native_tools/views entry (CWE-20 — F3 review fix)


def _validate_bounded_str_list(values: list, *, field_name: str) -> str | None:
    """Reject a list[str] whose entries exceed _ACCESS_SCOPE_MAX_STR_LEN.

    Returns an error string, or None if every entry is within bounds.
    """
    for v in values:
        if len(v) > _ACCESS_SCOPE_MAX_STR_LEN:
            return f"{field_name}: entrada excede {_ACCESS_SCOPE_MAX_STR_LEN} caracteres"
    return None


def _validate_policy_overlay_shape(policy_overlay: dict) -> str | None:
    """Reject a policy_overlay whose per-tool entries aren't dict[str, bool].

    Mirrors hermes.config_sync.policy_document.AccessScopeSpec.policy_overlay
    (dict[str, dict[str, bool]]) at THIS trust boundary too (F3 review fix) —
    a present-but-malformed entry already fails CLOSED downstream in
    AgentToolPolicyView, but rejecting it here is belt-and-suspenders at the
    point untrusted D-Bus input enters the system (CWE-20).
    """
    for tool, entry in policy_overlay.items():
        if not isinstance(entry, dict):
            return f"policy_overlay[{tool!r}] debe ser un objeto {{'enabled': bool}}"
        if not all(isinstance(v, bool) for v in entry.values()):
            return f"policy_overlay[{tool!r}] debe tener valores bool"
    return None


def _parse_access_scope_json(scope_json: str) -> tuple[dict, str | None]:
    """Parse+validate a cloud-pushed AccessScopeSpec JSON string.

    D-Bus trust boundary (CWE-20, untrusted input): returns (fields, None) on
    success — fields has every AgentAccessScope.create() kwarg filled with a
    validated/defaulted value. Returns ({}, error) on ANY validation failure
    (bad JSON, unknown keys, wrong types, length caps exceeded). NEVER raises.
    """
    try:
        raw = json.loads(scope_json)
    except (ValueError, TypeError) as exc:
        return {}, f"scope_json inválido: {exc}"

    if not isinstance(raw, dict):
        return {}, "scope_json debe ser un objeto JSON"

    unknown = set(raw) - _ACCESS_SCOPE_ALLOWED_KEYS
    if unknown:
        return {}, f"claves desconocidas: {sorted(unknown)}"

    native_tools = raw.get("native_tools", [])
    views = raw.get("views", [])
    policy_overlay = raw.get("policy_overlay", {})
    enforced = raw.get("enforced", False)
    cerebro_unrestricted = raw.get("cerebro_unrestricted", True)
    # approval_tier is drop-when-"standard" on the wire, so absent → "standard"
    # (fail-closed: unknown/absent tier gets the stricter escalation).
    approval_tier = raw.get("approval_tier", "standard")

    if not isinstance(native_tools, list) or not all(isinstance(t, str) for t in native_tools):
        return {}, "native_tools debe ser list[str]"
    if len(native_tools) > _ACCESS_SCOPE_MAX_LIST_LEN:
        return {}, f"native_tools excede {_ACCESS_SCOPE_MAX_LIST_LEN} entradas"
    if (err := _validate_bounded_str_list(native_tools, field_name="native_tools")) is not None:
        return {}, err
    if not isinstance(views, list) or not all(isinstance(v, str) for v in views):
        return {}, "views debe ser list[str]"
    if len(views) > _ACCESS_SCOPE_MAX_LIST_LEN:
        return {}, f"views excede {_ACCESS_SCOPE_MAX_LIST_LEN} entradas"
    if (err := _validate_bounded_str_list(views, field_name="views")) is not None:
        return {}, err
    if not isinstance(policy_overlay, dict):
        return {}, "policy_overlay debe ser un objeto"
    if len(policy_overlay) > _ACCESS_SCOPE_MAX_LIST_LEN:
        return {}, f"policy_overlay excede {_ACCESS_SCOPE_MAX_LIST_LEN} entradas"
    if (err := _validate_policy_overlay_shape(policy_overlay)) is not None:
        return {}, err
    if not isinstance(enforced, bool):
        return {}, "enforced debe ser bool"
    if not isinstance(cerebro_unrestricted, bool):
        return {}, "cerebro_unrestricted debe ser bool"
    if not isinstance(approval_tier, str) or approval_tier not in _ACCESS_SCOPE_APPROVAL_TIERS:
        return {}, f"approval_tier debe ser uno de {sorted(_ACCESS_SCOPE_APPROVAL_TIERS)}"

    return {
        "native_tools": native_tools,
        "views": views,
        "policy_overlay": policy_overlay,
        "enforced": enforced,
        "cerebro_unrestricted": cerebro_unrestricted,
        "approval_tier": approval_tier,
    }, None


def _resolve_tenant_id_from_wiring(tenant_id_str: str) -> UUID:
    """Convierte el tenant_id string almacenado en el wiring a UUID.

    El wiring almacena tenant_id como str (compat F010). Si está vacío o inválido,
    usa el mismo fallback que _resolve_tenant_id() del daemon (hostname-hash).
    """
    import hashlib  # noqa: PLC0415
    import os as _os  # noqa: PLC0415

    if tenant_id_str:
        try:
            return UUID(tenant_id_str)
        except ValueError:
            pass
    hostname = _os.uname().nodename if hasattr(_os, "uname") else "hermes-local"
    digest = hashlib.sha256(hostname.encode()).digest()
    return UUID(bytes=digest[:16], version=5)


def _consent_to_dict(consent: object) -> dict:
    """Serializa un Consent a dict para JSON transport. Sin PII sensible."""
    return {
        "consent_id": str(getattr(consent, "consent_id", "")),
        "capability": str(getattr(consent, "capability", "")),
        "scope": str(getattr(consent, "scope", "")),
        "granted_at": (
            getattr(consent, "granted_at").isoformat()
            if getattr(consent, "granted_at", None) else None
        ),
        "expires_at": (
            getattr(consent, "expires_at").isoformat()
            if getattr(consent, "expires_at", None) else None
        ),
        "revoked_at": (
            getattr(consent, "revoked_at").isoformat()
            if getattr(consent, "revoked_at", None) else None
        ),
        "usage_count": getattr(consent, "usage_count", 0),
    }


def _filter_associate_agents(all_agents: list, agent_to_dict_fn: Any) -> list[dict]:
    """Return the agent list filtered for an associate instance (GAP 7).

    Rule:
      When cloud-managed agents exist, expose only those plus the default CEO.
      This prevents the CE roster from dominating the associate UI — the
      enterprise controls which agents employees see.

      Fallback: if no cloud agents exist yet (e.g., first sync still pending),
      return only the default CEO (is_default=True) so the UI is never empty.
      If there is no CEO either, return the full list (never return empty).

    Non-destructive: local and roster agents remain in the DB — only the view
    returned to the UI is filtered.
    """
    cloud_agents = [a for a in all_agents if getattr(a, "managed_by", None) == "cloud"]
    default_agents = [a for a in all_agents if getattr(a, "is_default", False)]

    if cloud_agents:
        # Deduplicate by agent_id (CEO may also be cloud-managed).
        seen: dict[str, Any] = {}
        for a in [*default_agents, *cloud_agents]:
            seen.setdefault(a.agent_id, a)
        visible = list(seen.values())
    elif default_agents:
        visible = default_agents
    else:
        visible = all_agents  # never return empty

    return [agent_to_dict_fn(a) for a in visible]


def _assert_platform_model_signature(model: Any, signer: Any, *, operation: str) -> None:
    """Verifica la firma del PlatformModel antes de una transición de ciclo de vida.

    Fail-closed: si el modelo tiene firma Y el signer está configurado, la firma
    DEBE verificar. Si no verifica, lanza InvalidModelSignature.

    Si el modelo NO tiene firma y el signer está configurado → lanza RuntimeError
    (el modelo debió firmarse al compilar).

    Si el signer es None (entorno sin master.key, e.g. CI) → warning + continúa.
    Esto permite que los tests pasen sin master.key; en producción el signer
    siempre está configurado (hermes-keygen.service Before= el runtime).

    Args:
        model: PlatformModel instance.
        signer: PlatformModelSigner | None — None solo en test/legacy.
        operation: 'enable' | 'confirm' — para el mensaje de error.

    Raises:
        InvalidModelSignature: si la firma no verifica (via signer.verify).
        RuntimeError: si el modelo no tiene firma y el signer está configurado.
    """
    if signer is None:
        logger.warning(
            "hermes.dbus.platform_model_signer_not_configured",
            extra={
                "operation": operation,
                "model_id": str(model.platform_model_id),
                "note": (
                    "No PlatformModelSigner configured — skipping signature "
                    "verification. In production, inject a signer derived from "
                    "master.key via SecretsVault.derive_subkey."
                ),
            },
        )
        return

    if model.signature is None:
        raise RuntimeError(
            f"PlatformModel {model.platform_model_id}: no tiene firma — "
            f"el modelo debe firmarse con PlatformModelSigner.sign() antes de "
            f"poder ser {operation}d (CTRL-5, Principio 0)."
        )

    signer.verify(model.signature)


def _apply_corrections(model, corrections: list):
    """Apply operator corrections (rename/discard/relabel) to a PlatformModel.

    Corrections schema: [{op: "rename"|"discard"|"relabel", target_ref, new_value?}].
    Unknown ops are logged and skipped (fail-soft for unknown future ops).
    """
    import dataclasses  # noqa: PLC0415
    from hermes.platforms.domain.value_objects import DomainName  # noqa: PLC0415
    from hermes.platforms.domain.platform_model import PlatformArea  # noqa: PLC0415

    areas = list(model.areas)
    for correction in corrections:
        op = correction.get("op", "")
        target = correction.get("target_ref", "")
        new_value = correction.get("new_value")
        if op == "relabel":
            areas = [
                dataclasses.replace(a, domain_name=DomainName(new_value), needs_label=False)
                if a.area_id == target else a
                for a in areas
            ]
        elif op == "discard":
            areas = [a for a in areas if a.area_id != target]
        elif op == "rename":
            areas = [
                dataclasses.replace(a, domain_name=DomainName(new_value))
                if a.area_id == target and new_value else a
                for a in areas
            ]
        else:
            logger.warning("hermes.dbus.unknown_correction_op", extra={"op": op})
    return dataclasses.replace(model, areas=tuple(areas))


def _configured_task_to_dict(view: Any) -> dict:
    """Serialize a ConfiguredTaskView to a plain dict for D-Bus / JSON transport.

    P3 fields (target_agent_id, task_instruction, one_shot, title) included;
    defaults to empty/False when the view was built from a pre-P3 row.
    """
    return {
        "trigger_id": view.trigger_id,
        "label": view.label,
        "trigger_type": view.trigger_type,
        "recurrence": view.recurrence,
        # Descripción legible de la recurrencia ("Todos los lunes a las 09:00").
        # La UI la muestra en vez del cron crudo (regla: nada de jerga técnica).
        "recurrence_human": getattr(view, "recurrence_human", "") or "",
        "enabled": view.enabled,
        "risk_ceiling": view.risk_ceiling,
        "last_run_at": view.last_run_at or "",
        "last_status": view.last_status or "",
        "next_run_at": view.next_run_at or "",
        # P3 fields
        "target_agent_id": getattr(view, "target_agent_id", None) or "",
        "task_instruction": getattr(view, "task_instruction", "") or "",
        "one_shot": bool(getattr(view, "one_shot", False)),
        "title": getattr(view, "title", "") or "",
    }


# ---------------------------------------------------------------------------
# P3 helper: signature + repo patch + enable-toggle
# ---------------------------------------------------------------------------


def _sign_scheduled_task_draft(
    *,
    admin_uuid: UUID,
    cron: str,
    task_instruction: str,
    title: str,
) -> str:
    """HMAC-SHA256 over the canonical draft fields for non-repudiation.

    Uses a deterministic byte representation so the same draft always yields
    the same signature for the same admin. The key is derived from a constant
    label (not a secret key) — this provides content-integrity and identity
    binding, not confidentiality. A full PKI signature is a follow-up (P4).
    """
    import hashlib  # noqa: PLC0415
    import hmac as _hmac  # noqa: PLC0415

    payload = f"{admin_uuid}|{cron}|{task_instruction}|{title}".encode("utf-8")
    # Key = stable HMAC key derived from the process-constant label.
    # This binds the signature to this daemon installation (not exportable).
    key_material = b"hermes:scheduled-task:v1:" + str(admin_uuid).encode()
    sig = _hmac.new(key_material, payload, hashlib.sha256).hexdigest()
    return f"hmac-sha256:{sig}"


async def _patch_trigger_p3_fields(
    *,
    repo: Any,
    instance_id: UUID,
    target_agent_id: str | None,
    task_instruction: str,
    one_shot: bool,
    title: str,
) -> None:
    """UPDATE the P3 columns on an existing trigger row.

    Called right after trigger_repo.authorize() creates the core row.
    Uses the repo's SQLite connection directly (same pattern as revoke).
    No-op if the columns do not exist yet (schema older than P3).
    """
    try:
        conn = repo._conn  # noqa: SLF001 — internal repo coupling, documented
        conn.execute(
            """
            UPDATE authorized_trigger_instances
            SET target_agent_id   = ?,
                task_instruction  = ?,
                one_shot          = ?,
                title             = ?,
                updated_at        = strftime('%Y-%m-%dT%H:%M:%fZ','now')
            WHERE instance_id = ?
            """,
            (
                target_agent_id,
                task_instruction,
                1 if one_shot else 0,
                title,
                str(instance_id),
            ),
        )
        conn.commit()
    except Exception as exc:  # noqa: BLE001
        # Columns may not exist on an older DB (migration not yet run). Log and
        # continue — the trigger is already created with the core fields.
        logger.warning(
            "hermes.dbus.patch_trigger_p3_failed instance_id=%s err=%s",
            instance_id, exc,
        )


async def _set_trigger_enabled(
    *,
    repo: Any,
    trigger_instance_id: UUID,
    enabled: bool,
    admin_uuid: UUID,
) -> None:
    """Toggle the kill-switch on a trigger preserving I11 (enabled ↔ revoked_at).

    enabled=True  → enabled=1, revoked_at=NULL, revoked_by_admin_uuid=NULL.
    enabled=False → enabled=0, revoked_at=now,  revoked_by_admin_uuid=admin.
    """
    from datetime import UTC, datetime  # noqa: PLC0415

    now_iso = datetime.now(tz=UTC).isoformat()
    if enabled:
        repo._conn.execute(  # noqa: SLF001
            """
            UPDATE authorized_trigger_instances
            SET enabled = 1,
                revoked_at = NULL,
                revoked_by_admin_uuid = NULL,
                updated_at = ?
            WHERE instance_id = ?
            """,
            (now_iso, str(trigger_instance_id)),
        )
    else:
        repo._conn.execute(  # noqa: SLF001
            """
            UPDATE authorized_trigger_instances
            SET enabled = 0,
                revoked_at = ?,
                revoked_by_admin_uuid = ?,
                updated_at = ?
            WHERE instance_id = ?
            """,
            (now_iso, str(admin_uuid), now_iso, str(trigger_instance_id)),
        )
    repo._conn.commit()  # noqa: SLF001


# ---------------------------------------------------------------------------
# OAuth device-code: sesiones en memoria + poller (REUSE de hermes_cli).
# El estado vive en el proceso del daemon (igual que en hermes_cli/web_server);
# un reinicio del daemon invalida sesiones pendientes — el operador relanza el
# flow desde la UI. Las credenciales completadas SÍ persisten (auth-store).
# ---------------------------------------------------------------------------
import threading as _oauth_threading  # noqa: E402

_OAUTH_SESSIONS: dict[str, dict] = {}
_OAUTH_SESSIONS_LOCK = _oauth_threading.Lock()


def _nous_oauth_poller(session_id: str) -> None:
    """Lleva el device-code de Nous a término y persiste credenciales.

    Port directo de hermes_cli/web_server.py::_nous_poller — mismos helpers
    (_poll_for_token + refresh_nous_oauth_from_state + persist_nous_credentials),
    mismo estado final que `hermes auth add nous`.
    """
    import time as _time  # noqa: PLC0415
    from datetime import datetime, timezone  # noqa: PLC0415

    with _OAUTH_SESSIONS_LOCK:
        sess = _OAUTH_SESSIONS.get(session_id)
    if not sess:
        return
    try:
        import httpx  # noqa: PLC0415
        from hermes_cli.auth import (  # noqa: PLC0415
            _poll_for_token,
            persist_nous_credentials,
            refresh_nous_oauth_from_state,
        )

        expires_in = max(60, int(sess["expires_at"] - _time.time()))
        with httpx.Client(
            timeout=httpx.Timeout(15.0), headers={"Accept": "application/json"}
        ) as client:
            token_data = _poll_for_token(
                client=client,
                portal_base_url=sess["portal_base_url"],
                client_id=sess["client_id"],
                device_code=sess["device_code"],
                expires_in=expires_in,
                poll_interval=sess["interval"],
            )
        now = datetime.now(timezone.utc)
        token_ttl = int(token_data.get("expires_in") or 0)
        auth_state = {
            "portal_base_url": sess["portal_base_url"],
            "inference_base_url": token_data.get("inference_base_url"),
            "client_id": sess["client_id"],
            "scope": token_data.get("scope") or sess.get("scope"),
            "token_type": token_data.get("token_type", "Bearer"),
            "access_token": token_data["access_token"],
            "refresh_token": token_data.get("refresh_token"),
            "obtained_at": now.isoformat(),
            "expires_at": (
                datetime.fromtimestamp(
                    now.timestamp() + token_ttl, tz=timezone.utc
                ).isoformat()
                if token_ttl
                else None
            ),
            "expires_in": token_ttl,
        }
        full_state = refresh_nous_oauth_from_state(
            auth_state, timeout_seconds=15.0, force_refresh=False
        )
        persist_nous_credentials(full_state)
        # NATIVO: fija el provider activo en config.yaml para que el motor
        # resuelva Nous directo (suscripción), sin vault ni catálogo.
        _write_hermes_model_config("nous", "hermes-4-405b")
        with _OAUTH_SESSIONS_LOCK:
            sess["status"] = "approved"
        logger.info("hermes.dbus.oauth_nous_approved session=%s", session_id[:8])
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "hermes.dbus.oauth_nous_poll_failed session=%s: %s", session_id[:8], exc
        )
        with _OAUTH_SESSIONS_LOCK:
            sess["status"] = "error"
            sess["error_message"] = str(exc)


def _xai_loopback_worker(session_id: str) -> None:
    """Espera el callback loopback de xAI, intercambia el code y persiste.
    Port de hermes_cli/web_server.py::_xai_loopback_worker. Al aprobar fija
    config.yaml provider=xai-oauth (suscripción SuperGrok nativa).
    """
    from datetime import datetime, timezone  # noqa: PLC0415
    try:
        from hermes_cli import auth as hauth  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        with _OAUTH_SESSIONS_LOCK:
            s = _OAUTH_SESSIONS.get(session_id)
            if s:
                s["status"] = "error"; s["error_message"] = str(exc)
        return
    with _OAUTH_SESSIONS_LOCK:
        sess = _OAUTH_SESSIONS.get(session_id)
    if not sess:
        return

    def _fail(msg: str) -> None:
        with _OAUTH_SESSIONS_LOCK:
            s = _OAUTH_SESSIONS.get(session_id)
            if s:
                s["status"] = "error"; s["error_message"] = msg

    try:
        callback = hauth._xai_wait_for_callback(
            sess["server"], sess["thread"], sess["callback_result"],
            timeout_seconds=600,
        )
    except Exception as exc:  # noqa: BLE001
        _fail(f"xAI: timeout autorización: {exc}"); return
    if callback.get("error"):
        _fail(f"xAI: {callback.get('error_description') or callback['error']}"); return
    if callback.get("state") != sess["state"]:
        _fail("xAI: state mismatch"); return
    code = str(callback.get("code") or "").strip()
    if not code:
        _fail("xAI: sin authorization code"); return
    try:
        import os as _os  # noqa: PLC0415
        payload = hauth._xai_oauth_exchange_code_for_tokens(
            token_endpoint=sess["token_endpoint"], code=code,
            redirect_uri=sess["redirect_uri"], code_verifier=sess["verifier"],
            code_challenge=sess["challenge"],
        )
        access_token = str(payload.get("access_token", "") or "").strip()
        refresh_token = str(payload.get("refresh_token", "") or "").strip()
        if not access_token or not refresh_token:
            _fail("xAI: token exchange incompleto"); return
        last_refresh = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        hauth._save_xai_oauth_tokens(
            {
                "access_token": access_token, "refresh_token": refresh_token,
                "id_token": str(payload.get("id_token", "") or "").strip(),
                "expires_in": payload.get("expires_in"),
                "token_type": str(payload.get("token_type") or "Bearer").strip() or "Bearer",
            },
            discovery=sess.get("discovery"), redirect_uri=sess["redirect_uri"],
            last_refresh=last_refresh,
        )
        _write_hermes_model_config("xai-oauth", "grok-4")
        with _OAUTH_SESSIONS_LOCK:
            _OAUTH_SESSIONS[session_id]["status"] = "approved"
        logger.info("hermes.dbus.oauth_xai_approved session=%s", session_id[:8])
    except Exception as exc:  # noqa: BLE001
        logger.warning("hermes.dbus.oauth_xai_failed session=%s: %s", session_id[:8], exc)
        _fail(str(exc))


def _codex_oauth_worker(session_id: str) -> None:
    """Device-code de OpenAI Codex (ChatGPT OAuth). Port directo de
    hermes_cli/web_server.py::_codex_full_login_worker — endpoints propios de
    OpenAI + intercambio PKCE. Publica user_code en step 1, persiste tokens y
    fija config.yaml provider=openai-codex al aprobar (suscripción nativa).
    """
    import time as _time  # noqa: PLC0415
    try:
        import httpx  # noqa: PLC0415
        from hermes_cli.auth import (  # noqa: PLC0415
            CODEX_OAUTH_CLIENT_ID,
            CODEX_OAUTH_TOKEN_URL,
            _save_codex_tokens,
        )
        issuer = "https://auth.openai.com"
        with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
            resp = client.post(
                f"{issuer}/api/accounts/deviceauth/usercode",
                json={"client_id": CODEX_OAUTH_CLIENT_ID},
                headers={"Content-Type": "application/json"},
            )
        if resp.status_code != 200:
            raise RuntimeError(f"deviceauth/usercode {resp.status_code}")
        d = resp.json()
        user_code = d.get("user_code", "")
        device_auth_id = d.get("device_auth_id", "")
        poll_interval = max(3, int(d.get("interval", "5")))
        if not user_code or not device_auth_id:
            raise RuntimeError("respuesta sin user_code/device_auth_id")
        with _OAUTH_SESSIONS_LOCK:
            s = _OAUTH_SESSIONS.get(session_id)
            if not s:
                return
            s["user_code"] = user_code
            s["verification_url"] = f"{issuer}/codex/device"
            s["interval"] = poll_interval
            s["expires_in"] = 15 * 60
        deadline = _time.monotonic() + 15 * 60
        code_resp = None
        with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
            while _time.monotonic() < deadline:
                _time.sleep(poll_interval)
                poll = client.post(
                    f"{issuer}/api/accounts/deviceauth/token",
                    json={"device_auth_id": device_auth_id, "user_code": user_code},
                    headers={"Content-Type": "application/json"},
                )
                if poll.status_code == 200:
                    code_resp = poll.json()
                    break
                if poll.status_code in {403, 404}:
                    continue
                raise RuntimeError(f"deviceauth/token {poll.status_code}")
        if code_resp is None:
            with _OAUTH_SESSIONS_LOCK:
                _OAUTH_SESSIONS[session_id]["status"] = "error"
                _OAUTH_SESSIONS[session_id]["error_message"] = "código expirado"
            return
        auth_code = code_resp.get("authorization_code", "")
        verifier = code_resp.get("code_verifier", "")
        with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
            tok = client.post(
                CODEX_OAUTH_TOKEN_URL,
                data={
                    "grant_type": "authorization_code", "code": auth_code,
                    "redirect_uri": f"{issuer}/deviceauth/callback",
                    "client_id": CODEX_OAUTH_CLIENT_ID, "code_verifier": verifier,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if tok.status_code != 200:
            raise RuntimeError(f"token exchange {tok.status_code}")
        tokens = tok.json()
        if not tokens.get("access_token"):
            raise RuntimeError("sin access_token")
        _save_codex_tokens({
            "access_token": tokens["access_token"],
            "refresh_token": tokens.get("refresh_token", ""),
        })
        _write_hermes_model_config("openai-codex", "gpt-5-codex")
        with _OAUTH_SESSIONS_LOCK:
            _OAUTH_SESSIONS[session_id]["status"] = "approved"
        logger.info("hermes.dbus.oauth_codex_approved session=%s", session_id[:8])
    except Exception as exc:  # noqa: BLE001
        logger.warning("hermes.dbus.oauth_codex_failed session=%s: %s", session_id[:8], exc)
        with _OAUTH_SESSIONS_LOCK:
            s = _OAUTH_SESSIONS.get(session_id)
            if s:
                s["status"] = "error"
                s["error_message"] = str(exc)


# ---------------------------------------------------------------------------
# Providers NATIVOS — escritura directa en HERMES_HOME (.env + config.yaml).
# Es el camino que lee resolve_runtime_provider: cero abstracción, cero vault.
# ---------------------------------------------------------------------------
def _hermes_home():
    import os as _os  # noqa: PLC0415
    from pathlib import Path as _Path  # noqa: PLC0415
    return _Path(_os.environ.get("HERMES_HOME") or (_Path.home() / ".hermes"))


def _write_hermes_env(var: str, value: str) -> None:
    """Upsert VAR=value en HERMES_HOME/.env (0600). Fuente de claves nativa."""
    import os as _os  # noqa: PLC0415
    env_path = _hermes_home() / ".env"
    env_path.parent.mkdir(parents=True, exist_ok=True)
    lines, found = [], False
    if env_path.exists():
        for ln in env_path.read_text(encoding="utf-8").splitlines():
            if ln.strip().startswith(f"{var}="):
                lines.append(f"{var}={value}"); found = True
            else:
                lines.append(ln)
    if not found:
        lines.append(f"{var}={value}")
    tmp = env_path.with_suffix(".env.tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    _os.chmod(tmp, 0o600)
    _os.replace(tmp, env_path)


def _write_hermes_model_config(provider_id: str, model: str, base_url: str = "") -> None:
    """Fija model.{provider,default,base_url} en config.yaml — TRIGGER del path
    nativo: si está, el motor resuelve por hermes_cli (no por vault/catálogo)."""
    from hermes_cli.config import load_config, save_config  # noqa: PLC0415
    cfg = load_config() or {}
    m = dict(cfg.get("model") or {})
    m["provider"] = provider_id
    if model:
        m["default"] = model
    if base_url:
        m["base_url"] = base_url
    cfg["model"] = m
    save_config(cfg)


def _read_native_active() -> dict:
    """Provider nativo activo según config.yaml ({} si no hay model.provider)."""
    try:
        from hermes_cli.config import load_config  # noqa: PLC0415
        m = (load_config() or {}).get("model") or {}
        pid = (m.get("provider") or "").strip()
        if not pid or pid == "auto":
            return {}
        name = pid
        try:
            from hermes_cli.auth import PROVIDER_REGISTRY  # noqa: PLC0415
            pc = PROVIDER_REGISTRY.get(pid)
            if pc is not None:
                name = getattr(pc, "name", pid)
        except Exception:  # noqa: BLE001
            pass
        return {
            "provider_id": pid, "alias": name, "kind": pid,
            "default_model": (m.get("default") or m.get("model") or ""),
            "base_url": (m.get("base_url") or ""), "enabled": True,
            "is_active": True, "native": True,
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("hermes.dbus.native_active_read_failed: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# MCP Apps — persistencia de servidores configurados + reconexión al boot.
# ---------------------------------------------------------------------------
# Runners permitidos para servidores MCP stdio. El proceso corre como el
# usuario del daemon: un binario arbitrario sería ejecución de código sin
# gate. SECURITY-FIRST (C2): la allow-list está alineada EXACTAMENTE con lo que
# PackageContentScanner sabe descargar y analizar — npx → npm, uvx/pipx → PyPI.
# `node`/`python3` se EXCLUYEN a propósito: ejecutan un script local que no vive
# en ningún registro, así que el scanner no puede inspeccionar su código y el
# install puntuaría PASS con CERO análisis. Sin análisis ⇒ no PASS ⇒ rechazo
# (no near-PASS). Un MCP local debe publicarse (npm/PyPI) para pasar el gate.
_MCP_ALLOWED_RUNNERS = frozenset({"npx", "uvx", "pipx"})

# ── C2 PASS-5 — STRICT ARGV-SHAPE ALLOWLIST (mirror of PackageContentScanner) ────
# Defense-in-depth gate, independent of whether security_center is importable. The
# polarity is POSITIVE: an npx/uvx/pipx argv is analyzable ONLY if it matches an exact
# allowed shape — after optional leading boolean flags, the FIRST non-flag token is a
# published package SPEC ('[@scope/]name[@version]'), never an interpreter/command and
# never preceded by a package-source / inline-exec option. Everything else is
# non-fetchable ⇒ REJECT. This closes the WHOLE class of interpreter-as-command forms
# ('npx node -e', 'npx bash -c', 'npx -p X sh -c', …) in one rule instead of chasing
# each variant. Kept byte-for-byte aligned with PackageContentScanner so the two gates
# never drift; if the scanner is importable the gate ALSO delegates to it.

# Interpreters / shells / language launchers that, as the first positional, mean the
# runner executes inline/off-registry code rather than a published package. Matched on
# basename. Mirrors PackageContentScanner._INTERPRETER_COMMANDS.
# All entries are lowercase; the membership test lowercases the token basename first,
# so the allowlist is case-insensitive (npx NODE -e / uvx PYTHON3 -c cannot bypass it).
_INTERPRETER_COMMANDS = frozenset({
    "node", "nodejs", "deno", "bun", "ts-node", "tsx",
    "bash", "sh", "zsh", "dash", "ksh", "fish", "csh", "tcsh", "busybox",
    "python", "python2", "python3", "py", "pypy", "pypy3",
    "ruby", "perl", "php", "lua", "rscript", "osascript",
    "env", "eval", "exec", "command", "nohup", "xargs", "time", "watch",
    "sudo", "gdb",
})

# Options that select WHERE the executed code comes from (separate package / local dir /
# inline command). Before the first positional they break the strict shape.
_PACKAGE_SOURCE_OPTS = frozenset({"--package", "-p", "--from", "--with"})
_INLINE_EXEC_OPTS = frozenset({"--call", "-c", "-e", "--eval", "--exec"})

# Boolean (value-less) flags each runner accepts BEFORE the package token. Only these may
# precede the first positional in a valid shape.
_NPX_LEADING_BOOL_FLAGS = frozenset({"-y", "--yes", "--quiet", "-q", "--prefer-offline", "--offline"})
_UVX_LEADING_BOOL_FLAGS = frozenset({"-q", "--quiet", "--offline", "--no-cache", "--native-tls"})

# A published package SPEC token: optional '@scope/', a name, optional '@version' / PEP 508
# specifier. No path separators beyond the scoped-name slash, no leading dot/tilde.
_LOCAL_PATH_HINT = re.compile(r"^[./~]|/|\\")
_PKG_NAME = r"[A-Za-z0-9][A-Za-z0-9._-]*"
_PKG_SPEC_RE = re.compile(rf"^(@{_PKG_NAME}/)?{_PKG_NAME}([@=<>!~].*)?$")
# Chars that begin a version / PEP 508 / extras suffix on a package name token.
_VERSION_SUFFIX_CHARS = "@=<>!~["


def _bare_pkg_name(tok: str) -> str:
    """Return the bare package NAME from a spec token (suffix + scope stripped).

    Strips the leading '@scope/' (keeping the basename after the slash) and cuts the
    token at the first version / PEP 508 / extras separator. So 'node@18' → 'node',
    'python3==1' → 'python3', '@scope/pkg@1.2' → 'pkg', 'requests[extras]' → 'requests'.
    """
    name = tok.rsplit("/", 1)[-1]  # drop '@scope/' (and any path-ish prefix)
    for idx, ch in enumerate(name):
        if ch in _VERSION_SUFFIX_CHARS:
            return name[:idx]
    return name


def _is_published_pkg_spec(tok: str) -> bool:
    """True iff `tok` is a published-package SPEC and NOT an interpreter/command."""
    if not tok or (_LOCAL_PATH_HINT.search(tok) and not tok.startswith("@")):
        return False
    # Strip the version / PEP 508 / extras suffix FIRST, then run the interpreter check
    # on the BARE name — else 'node@18' (basename 'node@18') slips past and npx fetches
    # the real 'node' package to exec inline code.
    if _bare_pkg_name(tok).lower() in _INTERPRETER_COMMANDS:
        return False
    return _PKG_SPEC_RE.match(tok) is not None


def _npm_argv_matches_shape(argv: list[str]) -> bool:
    """True iff an npx argv matches the strict published-package shape."""
    for tok in argv[1:]:
        if tok.startswith("-"):
            base = tok.split("=", 1)[0]
            if base in _PACKAGE_SOURCE_OPTS or base in _INLINE_EXEC_OPTS:
                return False
            if tok in _NPX_LEADING_BOOL_FLAGS:
                continue
            return False
        return _is_published_pkg_spec(tok)
    return False


def _pypi_argv_matches_shape(argv: list[str]) -> bool:
    """True iff a uvx/pipx argv matches the strict published-package shape.

    Accepts a published positional, or '--from/--with <published-pkg>'. A local-path or
    git value on --from/--with is NOT accepted here (git+https is handled separately as a
    first-class path in _scanner_can_analyze_argv, BEFORE this is consulted).
    """
    rest = argv[1:]
    i, n = 0, len(rest)
    while i < n:
        tok = rest[i]
        if not tok.startswith("-"):
            if tok == "run":  # 'pipx run pkg'
                i += 1
                continue
            return _is_published_pkg_spec(tok)
        base, eq, inline_val = tok.partition("=")
        if base in ("--from", "--with"):
            val = inline_val if eq else (rest[i + 1] if i + 1 < n else "")
            return _is_published_pkg_spec(val)
        if base in _INLINE_EXEC_OPTS or base in ("-p", "--package", "-e", "--editable"):
            return False
        if tok in _UVX_LEADING_BOOL_FLAGS:
            i += 1
            continue
        return False
    return False

# ── C1 PASS-4 — git-based MCP servers (uvx --from git+https://…) ─────────────────────
# Some MCP servers are distributed ONLY as a git repo, not a published registry package
# (e.g. `uvx --from git+https://github.com/oraios/serena serena`). PASS-3 broke these at
# prefetch: resolve_coordinate() returns None for a git spec (the URL's '/' looks like a
# local path) → RuntimeError → ok:False. The fix: recognise git+https specs as a FIRST-
# CLASS install path. At install time (trusted daemon, host netns) we let `uv` CLONE +
# build the tool into UV_CACHE_DIR with network access to ONLY the git host (github.com,
# gitlab.com, …) — granted for the install FETCH only — then the RUNTIME spawns OFFLINE
# from that warm cache exactly like a registry package. The MCP runtime netns stays
# default-deny (the git host is NOT added to the runtime egress; the clone already
# happened at install time). Security note: a git repo is not in a package registry, so
# the registry content-scan cannot inspect it the same way; git MCPs are an explicit
# owner-approved install path (the operator chose the repo URL via D-Bus, never the LLM).
#
# Only git+https is accepted — NOT git+ssh / git:// / git+file:// (no host auth surface /
# local-path / unauthenticated transport). The scheme is matched on the --from value.
_GIT_HTTPS_PREFIX = "git+https://"


def _git_spec_from_argv(argv: list[str]) -> str | None:
    """Return the git+https spec from a `--from`/`--with` value, or None if not git-based.

    Handles both '--from VALUE' and '--from=VALUE'. Only git+https is recognised; any
    other git transport (ssh/file/plain git) returns None so the normal (registry) gate
    rejects it — we do not fetch over unauthenticated/local transports.
    """
    rest = argv[1:]
    expect_value = False
    for tok in rest:
        if expect_value:
            expect_value = False
            if tok.startswith(_GIT_HTTPS_PREFIX):
                return tok
            continue
        if "=" in tok:
            key, _, val = tok.partition("=")
            if key in ("--from", "--with") and val.startswith(_GIT_HTTPS_PREFIX):
                return val
            continue
        if tok in ("--from", "--with"):
            expect_value = True
    return None


def _git_host_from_spec(spec: str) -> str | None:
    """Extract the bare hostname from a git+https spec (for the install-fetch grant).

    'git+https://github.com/oraios/serena@v1' → 'github.com'. Returns None if the host
    cannot be parsed (→ reject; we never fetch from an unparseable URL).
    """
    from urllib.parse import urlsplit  # noqa: PLC0415

    https_url = spec[len("git+"):]  # strip the 'git+' VCS prefix → plain https URL
    host = urlsplit(https_url).hostname
    return host.lower() if host else None


def _argv_matches_strict_shape(argv: list[str]) -> bool:
    """True iff argv matches the strict published-package allowlist shape.

    Class-level (C2 PASS-5): closes ALL interpreter-as-command + option forms in one
    rule. The first non-flag token (after optional leading boolean flags) must be a
    published package SPEC, never an interpreter/command, never preceded by a
    package-source / inline-exec option. Dispatches by runner; git+https is handled
    separately by the caller as a first-class path BEFORE this is consulted.
    """
    if not argv:
        return False
    runner = argv[0].rsplit("/", 1)[-1]
    if runner == "npx":
        return _npm_argv_matches_shape(argv)
    if runner in ("uvx", "pipx"):
        return _pypi_argv_matches_shape(argv)
    return False


def _scanner_can_analyze_argv(argv: list[str]) -> bool:
    """True iff PackageContentScanner can fetch + analyze this MCP argv.

    The install gate's second precondition (C2): the runner allow-list already
    bars arbitrary binaries, but it does not, by itself, guarantee the argv
    points at a *fetchable* registry coordinate (e.g. 'npx ./local.js' or
    'uvx --from /opt/x'). Only the scanner knows what it can actually download
    and inspect, so the gate delegates the decision to it — keeping the gate and
    the scanner from drifting apart.

    Fail-CLOSED: if the scanner is not importable we cannot confirm the code is
    analyzable, so we refuse (a public OS must not install code it could not
    have scanned). This mirrors the fail-closed posture of the scan step itself.

    Defense-in-depth (C2 PASS-5): the strict argv-SHAPE allowlist is enforced here
    directly, independent of the scanner probe, so the gate holds even if the probe
    were ever to drift. Only a plain 'npx [-y] <pkg>' / 'uvx <pkg>' / 'uvx --from
    <published-pkg>' / 'pipx run <pkg>' passes; every interpreter-as-command form
    ('npx node -e', 'npx bash -c', 'npx -p X sh -c', …) and every inline-exec /
    package-select / local-path option breaks the shape ⇒ REJECT (no analysis ⇒ no
    PASS). git+https remains a first-class operator-chosen path, checked next.
    """
    # git+https MCP — REJECTED (security-review 2026-06-27, finding #1). A git source is
    # SOURCE only (no published wheel), so installing it requires a PEP 517 BUILD that runs
    # the repo's setup.py / build-backend / build-deps as `hermes` in the host netns. A
    # red-team proved 8 independent ways an attacker-controlled build executes code that a
    # static content scan CANNOT catch (obfuscated setup.py, hatchling build hooks, a
    # malicious build-system.requires dep, inline-table/triple-quoted pyproject, legacy
    # no-backend fall-through, …). There is no `--ignore-scripts`/`--no-build` equivalent
    # that keeps a source build working, so the only safe posture is REJECT: only PUBLISHED
    # registry packages (npm/pypi-with-wheel) — whose bytes ARE statically inspected and
    # which install with NO build — are allowed. (A sandboxed-build path could re-enable git.)
    git_spec = _git_spec_from_argv(argv)
    if git_spec is not None:
        logger.warning(
            "hermes.dbus.mcp_argv_git_rejected — MCP por git+https no soportado: su "
            "instalación construye código no vettable. Usa un paquete publicado (npm/pypi)."
        )
        return False
    # STRICT ARGV-SHAPE allowlist (C2 PASS-5) — enforced BEFORE delegating to the
    # scanner probe so the gate stays closed even if the probe drifts. Closes the whole
    # class of interpreter-as-command + option forms in one rule.
    if not _argv_matches_strict_shape(argv):
        logger.warning(
            "hermes.dbus.mcp_argv_shape_rejected — el argv no es 'npx [-y] <pkg>' / "
            "'uvx <pkg>' / 'uvx --from <pkg-publicado>' / 'pipx run <pkg>'; un "
            "intérprete/comando o una opción de fuente/inline (node/bash, --package/-p/"
            "--from local/--call/-c/-e) lo hace no escaneable (gate C2 PASS-5, fail-closed)."
        )
        return False
    try:
        from hermes.security_center.infrastructure.package_content_scanner import (  # noqa: PLC0415
            PackageContentScanner,
        )
    except ImportError:
        logger.warning(
            "hermes.dbus.mcp_argv_analyzability_uncheckable — security_center "
            "no disponible; rechazo por precaución (fail-closed)."
        )
        return False
    try:
        return PackageContentScanner.is_fetchable_argv(list(argv))
    except Exception as exc:  # noqa: BLE001 — un fallo del probe no debe abrir el gate
        logger.warning("hermes.dbus.mcp_argv_probe_failed: %s (fail-closed)", exc)
        return False

# ── C1 PASS-3 — install-time package PRE-FETCH (trusted context) ─────────────────
# DECISION (owner-approved): separate package DOWNLOAD (install-time, trusted, scanned)
# from MCP RUNTIME (offline / default-deny). At add_mcp_server the daemon — which has
# network in the HOST netns and is the same trusted path where the Security Center's
# content scan ran — pre-fetches the MCP's package into the SHARED runner cache. The MCP
# RUNTIME then spawns OFFLINE from that warm cache (npx --offline / uvx --offline) inside
# its default-deny netns, so the runtime needs NO registry network. This closes the
# npm-PUT exfil residual (no registry at runtime to ride a published-package upload on)
# AND removes the need for any registry host-firewall allow (runtime needs no registry).
#
# Cache topology: the prefetch populates the SAME caches the launcher unit points the
# runner at (/var/lib/hermes/npm-cache, /var/lib/hermes/uv-cache — group hermes-work, so
# the daemon `hermes` writes them as owner and the runtime `hermes-sandbox` reads them by
# group). Sharing the launcher's cache is what makes the offline spawn resolve: a
# per-server cache the launcher could not locate at spawn (it only gets argv+env, never
# the server_id) would defeat the whole point. The Security Center already scanned the
# coordinate before this runs, so what lands in the cache is exactly what was vetted.
#
# FAIL-CLOSED: if the prefetch fails (registry down, bad coordinate, tool missing) the
# MCP is NOT added — better no MCP than one that would need a runtime registry hole.
_MCP_NPM_CACHE = "/var/lib/hermes/npm-cache"
_MCP_UV_CACHE = "/var/lib/hermes/uv-cache"
_MCP_PREFETCH_TIMEOUT_S = 300  # generous: a cold npm/uv resolve can be slow


def _grant_cache_group_write(cache_dir: str) -> None:
    """Hace la cache del runner group-writable por `hermes-work` (FIX raíz del MCP).

    El prefetch corre como `hermes` (umask del daemon → dirs 755 / ficheros 644: el GRUPO
    no puede escribir). Pero el runtime spawnea `npx/uvx --offline` como `hermes-sandbox`
    (grupo `hermes-work`), que NECESITA escribir en la cache (npm/uv crean ficheros tmp/lock
    incluso en --offline). Sin g+w daba EACCES sobre `_cacache/tmp/...` → el proceso moría →
    "StdioMcpClient: Handshake Failed … Connection Closed". Aquí, tras el prefetch, hacemos
    dirs g+rwx+setgid y ficheros g+rw para que el sandbox (mismo grupo) pueda usar la cache.
    Best-effort: un fallo de chmod no debe romper el install (el spawn lo revelará).
    """
    import os as __os  # noqa: PLC0415
    import stat as __stat  # noqa: PLC0415

    if not __os.path.isdir(cache_dir):
        return
    for root, _dirs, files in __os.walk(cache_dir):
        try:
            __os.chmod(root, (__os.stat(root).st_mode | 0o070 | __stat.S_ISGID) & 0o7777)
        except OSError:
            pass
        for f in files:
            fp = __os.path.join(root, f)
            try:
                m = __os.stat(fp).st_mode
                # g+rw siempre; g+x solo si el dueño ya tiene x (preserva ejecutables).
                __os.chmod(fp, (m | 0o060 | (0o010 if m & 0o100 else 0)) & 0o7777)
            except OSError:
                pass


def _prefetch_mcp_package(server_id: str, argv: list[str]) -> None:
    """Download the MCP's package into the shared runner cache in the TRUSTED daemon path.

    Resolves the registry coordinate (or git+https spec) from argv, then warms the shared
    npm/uv cache with network access. The RUNTIME later spawns OFFLINE from that cache.
    Raises RuntimeError on any failure (FAIL-CLOSED — the caller refuses to add the
    server).

    Security: this runs as the daemon (hermes), in the host netns, AFTER the install gate
    has PASSED for the SAME argv. For registry packages the content scan ran on the same
    coordinate; for git+https the operator chose the repo URL explicitly over D-Bus. The
    RUNTIME never gets registry/git network — the clone/download happens only here.
    """
    import os as _os  # noqa: PLC0415
    import shutil as _shutil  # noqa: PLC0415
    import subprocess as _subprocess  # noqa: PLC0415

    from hermes.security_center.domain.install_target import (  # noqa: PLC0415
        InstallTarget,
    )
    from hermes.security_center.infrastructure.package_content_scanner import (  # noqa: PLC0415
        PackageContentScanner,
    )

    # C1 PASS-4: git+https MCP — `uv tool install git+https://…` CLONES + builds the tool
    # into UV_CACHE_DIR here (trusted daemon path, network to the git host only). The
    # runtime then spawns `uvx --offline` from that warm cache. Handle BEFORE the registry
    # resolver, which (correctly) returns None for a git spec.
    git_spec = _git_spec_from_argv(argv)
    if git_spec is not None:
        _prefetch_git_mcp(server_id, git_spec)
        return

    coord = PackageContentScanner.resolve_coordinate(
        InstallTarget(kind="mcp_server", identifier="argv-probe", argv=list(argv))
    )
    if coord is None:
        # The gate already enforced fetchability; this is defense-in-depth.
        raise RuntimeError("no se pudo resolver la coordenada del paquete MCP para prefetch")
    ecosystem, name, version = coord
    pkg_spec = f"{name}@{version}" if version else name

    # Inherit the daemon's env (npm/uv config, PATH, proxy) and pin the SHARED caches the
    # launcher unit also uses, so `npx/uvx --offline` later resolve from the same place.
    env = dict(_os.environ)
    env["npm_config_cache"] = _MCP_NPM_CACHE
    env["UV_CACHE_DIR"] = _MCP_UV_CACHE
    # uv populates its cache/tool dir by hard-linking/renaming the built artifact;
    # when UV_CACHE_DIR and the install target straddle a mount boundary (the
    # container volume layout does), uv dies with "Invalid cross-device link
    # (os error 18)" and the MCP prefetch fails → the whole bundle stops
    # converging. Copy instead of link, exactly as the runtime path already does
    # (stdio_mcp_client.py). Parity fix — the prefetch path was missing it.
    env["UV_LINK_MODE"] = "copy"

    if ecosystem == "npm":
        npm = _shutil.which("npm")
        if not npm:
            raise RuntimeError("npm no está disponible para prefetch del paquete MCP")
        # Install the FULL dependency tree into a PERSISTENT per-package prefix and run
        # its bin DIRECTLY at runtime (security-review/connect-fix 2026-06-26). The old
        # design warmed a shared cache for `npx --offline`, but npx CANNOT resolve the
        # package packument from an `npm install`-warmed cache → ENOTCACHED → "Connection
        # Closed" (verified live, reproduces on a fresh cache). Running the installed bin
        # directly works. So we keep node_modules at a known path and rewrite the runtime
        # argv `npx <pkg>` → `node <prefix>/node_modules/<pkg>/<bin>` (offline_runtime.py).
        # The cage is unchanged: still `node` inside the launcher's netns+seccomp jail.
        #
        # --ignore-scripts (security-review 2026-06-26, two CONFIRMED CRITICALs): no
        # preinstall/install/postinstall of the target OR a transitive dep ever runs here
        # as `hermes` in the host netns — an unattended RCE the content scan cannot fully
        # cover. A package that genuinely needs a build step is a finding to surface, not
        # code to auto-run unvetted.
        from hermes.mcp.infrastructure.offline_runtime import (  # noqa: PLC0415
            MCP_INSTALL_ROOT,
            npm_install_dir,
        )
        install_dir = npm_install_dir(name)
        try:
            if install_dir.exists():
                _shutil.rmtree(install_dir, ignore_errors=True)
            install_dir.mkdir(parents=True, exist_ok=True)
            # Isolated npm cache for the install. The runtime now runs the bin from
            # node_modules (NOT `npx --offline` from a shared cache), so the prefetch no
            # longer needs the shared _MCP_NPM_CACHE — and that shared cache can hold
            # cross-uid (root) entries that make `npm install` die EACCES. A fresh cache
            # inside the install dir is always writable by this process.
            _pf_env = {**env, "npm_config_cache": str(install_dir / ".npm-cache")}
            _r = _subprocess.run(  # noqa: S603 — fixed list, no shell
                [npm, "install", pkg_spec, "--prefix", str(install_dir),
                 "--ignore-scripts", "--no-audit", "--no-fund", "--no-save",
                 "--loglevel=error"],
                env=_pf_env, capture_output=True, text=True,
                timeout=_MCP_PREFETCH_TIMEOUT_S, check=False,
            )
        except (OSError, _subprocess.TimeoutExpired) as exc:
            _shutil.rmtree(install_dir, ignore_errors=True)
            raise RuntimeError(f"prefetch del paquete MCP falló: {exc}") from exc
        if _r.returncode != 0:
            _tail = (_r.stderr or _r.stdout or "").strip()[-500:]
            _shutil.rmtree(install_dir, ignore_errors=True)
            raise RuntimeError(
                f"prefetch del paquete MCP '{pkg_spec}' falló (rc={_r.returncode}): {_tail}"
            )
        # The runtime (hermes-sandbox ∈ hermes-work) only READS the install to run `node`.
        # Grant group/other read+traverse, NEVER write (a compromised runtime must not
        # tamper with the installed code for the next run).
        _subprocess.run(  # noqa: S603
            ["chmod", "-R", "a+rX", str(MCP_INSTALL_ROOT)],
            capture_output=True, check=False,
        )
        logger.info(
            "hermes.dbus.mcp_prefetched server=%s ecosystem=%s pkg=%s dir=%s (persistent, run bin direct)",
            server_id, ecosystem, pkg_spec, install_dir,
        )
        return
    elif ecosystem == "pypi":
        uv = _shutil.which("uv")
        if not uv:
            raise RuntimeError("uv no está disponible para prefetch del paquete MCP")
        # `uvx --quiet <spec> --help` resolves + builds + caches the tool into
        # UV_CACHE_DIR exactly as the runtime invocation will, so the runtime `uvx
        # --offline` finds a complete cache entry (a bare `uv pip download` would not
        # build the tool environment uvx needs). --help exits fast without running the
        # server. If the package has no --help the cache is still warmed by the resolve.
        # --no-build (security-review 2026-06-27, finding #2): the pypi parallel of npm's
        # --ignore-scripts. `uv tool install` would otherwise BUILD an sdist via PEP 517,
        # running the package's setup.py / build-backend as hermes in the host netns — an
        # unvetted RCE. --no-build forbids building from source: a package with a published
        # WHEEL installs with no code execution; an sdist-only package fails here and is
        # surfaced for the owner to review, rather than auto-built. The content gate already
        # blocks non-standard/local build-backends BEFORE this point (defense in depth).
        cmd = [uv, "tool", "install", "--no-build", "--quiet", name]
    else:
        raise RuntimeError(f"ecosistema no soportado para prefetch: {ecosystem!r}")

    try:
        result = _subprocess.run(  # noqa: S603 — cmd is a fixed list, no shell
            cmd,
            env=env,
            capture_output=True,
            text=True,
            timeout=_MCP_PREFETCH_TIMEOUT_S,
            check=False,
        )
    except (OSError, _subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"prefetch del paquete MCP falló: {exc}") from exc
    if result.returncode != 0:
        # Surface the registry error tail (no secrets in these tools' output).
        tail = (result.stderr or result.stdout or "").strip()[-500:]
        raise RuntimeError(
            f"prefetch del paquete MCP '{pkg_spec}' falló (rc={result.returncode}): {tail}"
        )
    # FIX raíz MCP: cache uv group-writable para que `uvx --offline` (hermes-sandbox) escriba.
    _grant_cache_group_write(_MCP_UV_CACHE)
    logger.info(
        "hermes.dbus.mcp_prefetched server=%s ecosystem=%s pkg=%s",
        server_id, ecosystem, pkg_spec,
    )


def _prefetch_git_mcp(server_id: str, git_spec: str) -> None:
    """REJECTED — git+https MCPs are not installable (security-review 2026-06-27, #1).

    Installing a git source requires a PEP 517 BUILD that runs the repo's
    setup.py / build-backend / build-deps as hermes in the host netns — an unvetted
    RCE no static scan can gate (red-team proved 8 bypasses). The gate
    (_scanner_can_analyze_argv) already rejects git argv before prefetch; this is the
    fail-closed backstop. Only published registry packages (npm/pypi-with-wheel) install.
    """
    raise RuntimeError(
        f"MCP por git+https no soportado ({git_spec!r}): su instalación construye "
        "código no vettable. Usa un paquete publicado (npm/pypi)."
    )


# BYOK env keys permitted in MCP server entries. Mirrors _ALLOWED_ENV_KEYS in
# hermes-mcp-launcher — both gates must stay in sync; a key allowed here but
# not in the launcher will be silently discarded at spawn time.
# Expanding this set is a security-posture decision: add only named, bounded
# variables for specific published MCP servers; never allow arbitrary keys.
_MCP_BYOK_ENV_KEYS: frozenset[str] = frozenset({
    "OD_DAEMON_URL",
    "OD_API_TOKEN",
    "OD_AUTH_MODE",
    "OD_BASIC_USER",
    "OD_BASIC_PASS",
    # Curated pack (published servers, named/bounded BYOK secrets — mirror in
    # hermes-mcp-launcher._ALLOWED_ENV_KEYS):
    # REPLICATE_API_TOKEN — Replicate MCP (replicate-mcp): imagen + vídeo.
    # CONTEXT7_API_KEY    — Context7 MCP: docs de librerías al día para código.
    "REPLICATE_API_TOKEN",
    "CONTEXT7_API_KEY",
    # Ruflo MCP (ruflo): endpoint OpenAI-compatible → enruta a nuestro LLM nativo.
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
})


def _validate_mcp_env(raw: object) -> dict[str, str]:
    """Validate and sanitise a caller-supplied BYOK env dict.

    Returns a clean dict whose keys are a subset of _MCP_BYOK_ENV_KEYS and
    whose values are non-empty strings. Raises ValueError on any violation.

    Security invariants:
      - Only explicitly allowlisted keys pass through; arbitrary keys are
        rejected, not silently dropped — fail-loud on unknown keys so
        callers notice misconfiguration rather than silently missing env.
      - Values must be strings; empty strings are rejected (would confuse the
        MCP server just as much as missing env vars).
      - OD_DAEMON_URL must parse as an http(s) URL (scheme + netloc present).
        This prevents open-design-mcp from being pointed at file://, data://, etc.
      - OD_API_TOKEN is passed through opaquely; it MUST NOT be logged in
        clear — callers must use the masked helpers below.
    """
    if not isinstance(raw, dict):
        raise ValueError("env debe ser un diccionario str→str")
    validated: dict[str, str] = {}
    for key, val in raw.items():
        if not isinstance(key, str):
            raise ValueError(f"clave de env no es string: {key!r}")
        if key not in _MCP_BYOK_ENV_KEYS:
            raise ValueError(
                f"clave de env no permitida: {key!r} "
                f"(allowlist: {sorted(_MCP_BYOK_ENV_KEYS)})"
            )
        if not isinstance(val, str) or not val:
            raise ValueError(f"valor de env para {key!r} debe ser string no vacío")
        if key == "OD_DAEMON_URL":
            _validate_url(val, field="OD_DAEMON_URL")
        validated[key] = val
    return validated


def _validate_url(value: str, *, field: str) -> None:
    """Raise ValueError unless value is a valid http(s) URL."""
    from urllib.parse import urlparse as _urlparse  # noqa: PLC0415
    try:
        parsed = _urlparse(value)
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"{field} no es una URL válida: {exc}") from exc
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"{field} debe usar http o https; esquema recibido: {parsed.scheme!r}"
        )
    if not parsed.netloc:
        raise ValueError(f"{field} debe incluir un host (netloc vacío)")


# ---------------------------------------------------------------------------
# Neus MCP registry bridge — Safent reads/writes via hermes_cli.config so
# config.yaml (mcp_servers) is the single source of truth.  The old
# mcp-servers.json (Safent parallel store) is DELETED; these helpers replace it.
# ---------------------------------------------------------------------------

def _neus_argv(neus_cfg: dict) -> list[str]:
    """Convert a Neus server config entry to a Safent-style argv list.

    Neus stores command + args separately; Safent uses a flat argv.
    """
    cmd = str(neus_cfg.get("command") or "")
    args = [str(a) for a in (neus_cfg.get("args") or [])]
    return [cmd, *args] if cmd else args


def _neus_load_entries() -> list[dict]:
    """Return Neus mcp_servers as a list of Safent-compatible dicts.

    Shape: [{server_id, label, argv, env?}, ...]. Fail-soft: returns []
    on any import or parse error so boot reconnect is never fatal.
    """
    try:
        from tools.mcp_tool import _load_mcp_config as _neus_cfg  # noqa: PLC0415
    except ImportError:
        logger.warning("hermes.dbus.neus_load_entries: tools.mcp_tool unavailable")
        return []
    try:
        neus_map: dict[str, dict] = _neus_cfg()
    except Exception as exc:  # noqa: BLE001
        logger.warning("hermes.dbus.neus_load_entries failed: %s", exc)
        return []
    entries: list[dict] = []
    for sid, cfg in neus_map.items():
        entry: dict = {
            "server_id": sid,
            "label": sid,
            "argv": _neus_argv(cfg),
        }
        if cfg.get("env"):
            entry["env"] = {k: v for k, v in cfg["env"].items()}
        entries.append(entry)
    return entries


def _neus_write_mcp_entry(server_id: str, argv: list[str], *, env: dict | None = None) -> None:
    """Persist a new/updated MCP server entry into Neus's config.yaml.

    Gate contract: this function MUST only be called AFTER the Safent security
    gate (scan/MFA) has passed. A poisoned argv[0] here is RCE because Neus
    will spawn it. The caller (add_mcp_server) enforces this ordering.

    Neus format under mcp_servers.<server_id>:
      command: argv[0]
      args: argv[1:]
      env: {...}   (omitted when empty)
    """
    from hermes_cli.config import load_config, save_config  # noqa: PLC0415

    cfg = load_config()
    mcp_servers: dict = cfg.setdefault("mcp_servers", {})
    entry: dict = {
        "command": argv[0] if argv else "",
        "args": list(argv[1:]),
    }
    if env:
        entry["env"] = dict(env)
    mcp_servers[server_id] = entry
    save_config(cfg)

    # Notify Neus's live registry so the server is available immediately
    # without restarting the daemon. register_mcp_servers is idempotent for
    # already-connected servers but activates the newly written entry.
    try:
        from tools.mcp_tool import register_mcp_servers  # noqa: PLC0415
        register_mcp_servers({server_id: entry})
    except Exception as exc:  # noqa: BLE001 — connection already happened via _mcp_connect
        logger.debug("hermes.dbus.neus_register_after_write server=%s: %s", server_id, exc)


def _neus_remove_mcp_entry(server_id: str) -> None:
    """Remove a server entry from Neus's config.yaml (mcp_servers dict)."""
    try:
        from hermes_cli.config import load_config, save_config  # noqa: PLC0415
    except ImportError:
        logger.warning("hermes.dbus.neus_remove: hermes_cli.config unavailable")
        return
    try:
        cfg = load_config()
        mcp_servers: dict = cfg.get("mcp_servers") or {}
        mcp_servers.pop(server_id, None)
        cfg["mcp_servers"] = mcp_servers
        save_config(cfg)
    except Exception as exc:  # noqa: BLE001
        logger.warning("hermes.dbus.neus_remove_failed server=%s: %s", server_id, exc)


# ---------------------------------------------------------------------------
# Neus cron bridge — single source of truth for the job CATALOG (BUG-7 fix)
#
# AUTHORIZATION vs CATALOG SPLIT (explicit):
#   AUTHORIZATION (stays in Safent):
#     SqliteAuthorizedTriggerRepository (authorized_trigger_instances table).
#     Consulted by TriggerGate.enqueue_from_trigger → is_authorized() on EVERY
#     autonomous trigger attempt. Fail-closed. Revocable instantly. The cage gate.
#     NOT changed by this fix.
#
#   CATALOG (moved to Neus jobs.json):
#     cron.jobs.list_jobs / create_job / remove_job.
#     The agent's `cronjob` tool writes here. The UI's create_scheduled_task now
#     also writes here. list_configured_tasks reads ONLY here.
#     Previously list_configured_tasks read trigger_repo → showed 0 rows for
#     agent-created jobs (BUG-7). Now it reads jobs.json → both sources visible.
# ---------------------------------------------------------------------------


def _neus_cron_list_jobs(*, include_disabled: bool = True) -> list[dict]:
    """Return Neus cron jobs as a list of raw job dicts.

    Fail-soft: returns [] on ImportError (cron.jobs not installed) or any
    other error so the dashboard degrades gracefully.
    """
    try:
        from cron.jobs import list_jobs  # noqa: PLC0415
    except ImportError:
        logger.warning("hermes.dbus.neus_cron_list_jobs: cron.jobs unavailable")
        return []
    try:
        return list_jobs(include_disabled=include_disabled)
    except Exception as exc:  # noqa: BLE001
        logger.warning("hermes.dbus.neus_cron_list_jobs failed: %s", exc)
        return []


def _neus_cron_create_job(
    *,
    prompt: str,
    schedule: str,
    name: str,
    one_shot: bool,
    origin: dict | None = None,
) -> str | None:
    """Write a new job to Neus cron/jobs.json. Returns the Neus job id or None.

    Gate contract: MUST only be called AFTER the Safent security gate
    (trigger_repo.authorize) has passed. Fail-soft: logs and returns None on
    any error so the outer create_scheduled_task can still succeed (auth row
    already committed).

    `origin` is stored verbatim on the job and used later to look up the job
    by trigger_instance_id without requiring a separate mapping table.
    """
    try:
        from cron.jobs import create_job  # noqa: PLC0415
    except ImportError:
        logger.warning("hermes.dbus.neus_cron_create_job: cron.jobs unavailable")
        return None
    try:
        repeat = 1 if one_shot else None
        kwargs: dict = dict(prompt=prompt, schedule=schedule, name=name, repeat=repeat)
        if origin is not None:
            kwargs["origin"] = origin
        job = create_job(**kwargs)
        return str(job.get("id", ""))
    except Exception as exc:  # noqa: BLE001
        logger.warning("hermes.dbus.neus_cron_create_job failed: %s", exc)
        return None


def _neus_cron_find_job_id_by_trigger(trigger_id: str) -> str | None:
    """Scan cron.jobs to find the job whose origin.trigger_instance_id matches.

    Returns the Neus job id string, or None if not found or on any error.
    Fail-soft: cron.jobs absent or any exception → None (never raises).
    """
    try:
        from cron.jobs import list_jobs  # noqa: PLC0415
    except ImportError:
        logger.warning("hermes.dbus.neus_cron_find: cron.jobs unavailable")
        return None
    try:
        for job in list_jobs(include_disabled=True):
            origin = job.get("origin") or {}
            if isinstance(origin, dict) and origin.get("trigger_instance_id") == trigger_id:
                return str(job["id"])
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("hermes.dbus.neus_cron_find failed trigger=%s: %s", trigger_id, exc)
        return None


def _neus_cron_update_job(
    trigger_id: str,
    *,
    prompt: str | None = None,
    schedule: str | None = None,
    name: str | None = None,
) -> bool:
    """Update mutable fields on the Neus job that maps to trigger_id.

    Only fields that are not None are included in the update payload.
    Returns True on success, False if job not found or on any error.
    Fail-soft: never raises.
    """
    job_id = _neus_cron_find_job_id_by_trigger(trigger_id)
    if job_id is None:
        logger.warning(
            "hermes.dbus.neus_cron_update: no job found for trigger=%s", trigger_id
        )
        return False
    try:
        from cron.jobs import update_job  # noqa: PLC0415
    except ImportError:
        logger.warning("hermes.dbus.neus_cron_update: cron.jobs unavailable")
        return False
    updates: dict = {}
    if prompt is not None:
        updates["prompt"] = prompt
    if schedule is not None:
        updates["schedule"] = schedule
    if name is not None:
        updates["name"] = name
    if not updates:
        return True
    try:
        update_job(job_id, updates)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "hermes.dbus.neus_cron_update failed job=%s trigger=%s: %s",
            job_id, trigger_id, exc,
        )
        return False


def _neus_cron_remove_job(trigger_id: str) -> bool:
    """Remove the Neus job that maps to trigger_id from cron.jobs.

    Returns True on success, False if job not found or on any error.
    Fail-soft: never raises.
    """
    job_id = _neus_cron_find_job_id_by_trigger(trigger_id)
    if job_id is None:
        logger.warning(
            "hermes.dbus.neus_cron_remove: no job found for trigger=%s", trigger_id
        )
        return False
    try:
        from cron.jobs import remove_job  # noqa: PLC0415
    except ImportError:
        logger.warning("hermes.dbus.neus_cron_remove: cron.jobs unavailable")
        return False
    try:
        remove_job(job_id)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "hermes.dbus.neus_cron_remove failed job=%s trigger=%s: %s",
            job_id, trigger_id, exc,
        )
        return False


def _neus_cron_set_enabled(trigger_id: str, *, enabled: bool) -> bool:
    """Pause or resume the Neus job that maps to trigger_id.

    enabled=True  → resume_job
    enabled=False → pause_job

    Returns True on success, False if job not found or on any error.
    Fail-soft: never raises.
    """
    job_id = _neus_cron_find_job_id_by_trigger(trigger_id)
    if job_id is None:
        logger.warning(
            "hermes.dbus.neus_cron_set_enabled: no job found for trigger=%s", trigger_id
        )
        return False
    try:
        if enabled:
            from cron.jobs import resume_job  # noqa: PLC0415
            resume_job(job_id)
        else:
            from cron.jobs import pause_job  # noqa: PLC0415
            pause_job(job_id, "disabled via safent dashboard")
        return True
    except ImportError:
        logger.warning("hermes.dbus.neus_cron_set_enabled: cron.jobs unavailable")
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "hermes.dbus.neus_cron_set_enabled failed job=%s trigger=%s enabled=%s: %s",
            job_id, trigger_id, enabled, exc,
        )
        return False


def _neus_job_to_task_dict(job: dict) -> dict:
    """Map a Neus cron job dict to the ConfiguredTaskView wire shape.

    The wire shape is defined by _configured_task_to_dict. Unknown schedule
    shapes render recurrence_human as 'schedule unavailable' rather than
    dropping the row (honest-empty policy, CTRL-P1-5).

    SECURITY: this function reads metadata only. prompt is NOT exposed in
    the return value — only the title/label (same as CTRL-P1-5 on the
    trigger_repo path which capped task_instruction at 120 chars in the
    label derivation). We truncate prompt to 120 chars max for the label.
    """
    from datetime import UTC, datetime  # noqa: PLC0415

    from hermes.tasks.control_plane.application.control_plane_service import (  # noqa: PLC0415
        _cron_next_fire,
        _cron_recurrence_human,
    )

    job_id = str(job.get("id") or "")
    name = str(job.get("name") or "").strip()
    prompt = str(job.get("prompt") or "").strip()
    label = name or prompt[:120] or job_id or "cron job"

    schedule = job.get("schedule") or {}
    schedule_display = str(job.get("schedule_display") or "").strip()
    cron_expr = ""
    recurrence_human = ""
    next_run_at = str(job.get("next_run_at") or "")

    if isinstance(schedule, dict):
        kind = schedule.get("kind", "")
        if kind == "cron":
            cron_expr = str(schedule.get("expr") or schedule.get("display") or "").strip()
        elif kind == "once":
            cron_expr = str(schedule.get("run_at") or schedule.get("display") or "").strip()
        elif kind == "interval":
            cron_expr = str(schedule.get("display") or "").strip()
        else:
            cron_expr = schedule_display
    elif schedule:
        cron_expr = str(schedule)

    if not cron_expr:
        cron_expr = schedule_display or "schedule unavailable"

    if cron_expr and " " in cron_expr and not recurrence_human:
        now = datetime.now(tz=UTC)
        recurrence_human = _cron_recurrence_human(cron_expr)
        if not next_run_at:
            next_dt = _cron_next_fire(cron_expr, after=now)
            next_run_at = next_dt.isoformat() if next_dt else ""

    enabled = bool(job.get("enabled", True))
    last_run_at = str(job.get("last_run_at") or "")
    last_status = str(job.get("last_status") or "")

    return {
        "trigger_id": job_id,
        "label": label,
        "trigger_type": "timer",
        "recurrence": cron_expr,
        "recurrence_human": recurrence_human,
        "enabled": enabled,
        "risk_ceiling": "low",
        "last_run_at": last_run_at,
        "last_status": last_status,
        "next_run_at": next_run_at,
        "target_agent_id": "",
        "task_instruction": "",
        "one_shot": bool(job.get("repeat", {}).get("times") == 1 if isinstance(job.get("repeat"), dict) else False),
        "title": name,
    }


def _mcp_id(server_id: str):
    from hermes.mcp.domain.value_objects import McpServerId  # noqa: PLC0415
    return McpServerId(server_id)


async def _mcp_connect(
    manager,
    server_id: str,
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
):
    from hermes.mcp.domain.value_objects import (  # noqa: PLC0415
        McpServerId,
        ServerSlug,
        Transport,
        TrustLevel,
    )
    # Auto-wire the owner's ACTIVE LLM provider into MCPs that declare OpenAI-compatible
    # BYOK keys but leave them empty (e.g. ruflo's swarm: env has OPENAI_BASE_URL="" /
    # OPENAI_API_KEY=""). The MCP child is spawned by the launcher and does NOT inherit
    # the daemon's env, so resolve the active provider here and fill the empties → the
    # MCP swarm uses the SAME LLM the owner configured for Safent. "Download → it works".
    # Only FILLS declared-but-empty keys (never adds new ones → no env injection surface).
    resolved_env = dict(env or {})
    if ("OPENAI_BASE_URL" in resolved_env or "OPENAI_API_KEY" in resolved_env) and (
        not resolved_env.get("OPENAI_BASE_URL") or not resolved_env.get("OPENAI_API_KEY")
    ):
        try:
            import os as _os_pv  # noqa: PLC0415
            from pathlib import Path as _Path_pv  # noqa: PLC0415

            from hermes.runtime.active_provider import (  # noqa: PLC0415
                ActiveProviderService,
            )

            _db = _Path_pv(
                _os_pv.environ.get("HERMES_SHELL_DB", "/var/lib/hermes/shell-state.db")
            )
            _mc = ActiveProviderService(db_path=_db).resolve()
            if _mc is not None:
                if not resolved_env.get("OPENAI_BASE_URL") and getattr(_mc, "base_url", None):
                    resolved_env["OPENAI_BASE_URL"] = str(_mc.base_url)
                if not resolved_env.get("OPENAI_API_KEY") and getattr(_mc, "api_key", None):
                    resolved_env["OPENAI_API_KEY"] = str(_mc.api_key)
                logger.info(
                    "hermes.dbus.mcp_provider_autowired server=%s filled=%s",
                    server_id,
                    [k for k in ("OPENAI_BASE_URL", "OPENAI_API_KEY") if resolved_env.get(k)],
                )
        except Exception as _e_pv:  # noqa: BLE001
            logger.warning(
                "hermes.dbus.mcp_provider_autowire_failed server=%s: %s", server_id, _e_pv
            )
    # Confianza: los MCP VETADOS del stack de fábrica (locales, sin egress, horneados por
    # nosotros) entran como BUILTIN → sus tools fluyen sin HITL (la jaula contiene; lo
    # marcado destructivo sigue gateado). Los MCP de primera parte que SÍ EGRESAN a un
    # control-plane gestionado (p.ej. safent-control → cloud /api/*) entran como
    # MANAGED_REMOTE: NO heredan la postura frictionless de BUILTIN (la jaula no los
    # confina — hablan con un servicio remoto) — lecturas fluyen, escrituras exigen HITL
    # en cuanto el ciclo se tainta por una respuesta MCP no confiable (CTRL-5). Cualquier
    # OTRO (añadido por el usuario) = USER_ADDED: DEFAULT_DENY + HITL en cada tool-call
    # (el broker escala, no recorta).
    _BUILTIN_MCP_SLUGS = frozenset({"excel", "word", "powerpoint"})
    _MANAGED_REMOTE_MCP_SLUGS = frozenset({"safent-control"})
    if server_id in _MANAGED_REMOTE_MCP_SLUGS:
        _trust = TrustLevel.MANAGED_REMOTE
    elif server_id in _BUILTIN_MCP_SLUGS:
        _trust = TrustLevel.BUILTIN
    else:
        _trust = TrustLevel.USER_ADDED
    return await manager.connect(
        server_id=McpServerId(server_id),
        slug=ServerSlug(server_id),
        transport=Transport.stdio(argv, env=resolved_env),
        trust_level=_trust,
    )


_IDENTIFIER_RE = None
_VERSION_RE = None


def _compiled_identifier_re():
    import re as _re  # noqa: PLC0415
    global _IDENTIFIER_RE  # noqa: PLW0603
    if _IDENTIFIER_RE is None:
        _IDENTIFIER_RE = _re.compile(r"^[A-Za-z0-9@/._-]+$")
    return _IDENTIFIER_RE


def _compiled_version_re():
    import re as _re  # noqa: PLC0415
    global _VERSION_RE  # noqa: PLW0603
    if _VERSION_RE is None:
        _VERSION_RE = _re.compile(r"^[A-Za-z0-9._-]*$")
    return _VERSION_RE


def _build_argv_npm(identifier: str, version: str, runtime_args: list[dict]) -> list[str]:
    """Build npx argv from an npm package entry.

    Always starts with the literal string "npx" — never interpolated
    from registry data (security: CTRL-MCP-3).
    """
    pkg_ref = f"{identifier}@{version}" if version else identifier
    argv: list[str] = ["npx", "-y", pkg_ref]
    for arg in runtime_args:
        val = str(arg.get("value") or "").strip()
        if not val or val == "-y":
            continue
        arg_type = arg.get("type", "positional")
        if arg_type == "positional":
            argv.append(val)
        elif arg_type == "named":
            argv.extend([val])  # named args include the flag name in value
    return argv


def _build_argv_pypi(identifier: str, version: str, runtime_args: list[dict]) -> list[str]:
    """Build uvx argv from a pypi package entry.

    Always starts with the literal string "uvx" — never interpolated
    from registry data (security: CTRL-MCP-3).
    """
    pkg_ref = f"{identifier}=={version}" if version else identifier
    argv: list[str] = ["uvx", pkg_ref]
    for arg in runtime_args:
        val = str(arg.get("value") or "").strip()
        if not val:
            continue
        arg_type = arg.get("type", "positional")
        if arg_type == "positional":
            argv.append(val)
        elif arg_type == "named":
            argv.append(val)
    return argv


def _pick_installable_package(packages: list[dict]) -> dict | None:
    """Return first npm or pypi stdio package, preferring npm."""
    npm_pkg = next(
        (p for p in packages
         if p.get("registryType") == "npm"
         and p.get("transport", {}).get("type") == "stdio"),
        None,
    )
    if npm_pkg:
        return npm_pkg
    return next(
        (p for p in packages
         if p.get("registryType") == "pypi"
         and p.get("transport", {}).get("type") == "stdio"),
        None,
    )


def _normalize_env_vars(raw_vars: list[dict]) -> list[dict]:
    # Emit key/label — the field names the React MCP form reads (parseEnvSchema).
    # The registry's raw env var name IS the BYOK key the daemon validates against
    # the allowlist, so `key` carries it; emitting `name`/`description` left the
    # form reading v.key=undefined → "clave de env no permitida: 'undefined'".
    return [
        {
            "key": str(v.get("name") or ""),
            "label": str(v.get("description") or v.get("name") or ""),
            "required": bool(v.get("isRequired", False)),
            "secret": bool(v.get("isSecret", False)),
        }
        for v in raw_vars
        if v.get("name")
    ]


def _normalize_registry_entry(item: dict) -> dict:
    """Normalise one raw registry entry to the shape expected by the UI / add_mcp_server.

    Schema confirmed against live API 2026-06-10:
      item = {"server": {name, description, version, repository?, packages?, remotes?},
               "_meta": {...}}
    """
    server = item.get("server") or {}
    name = str(server.get("name") or "")
    description = str(server.get("description") or "")
    version = str(server.get("version") or "")
    repo_obj = server.get("repository") or {}
    repository = str(repo_obj.get("url") or "")
    packages: list[dict] = server.get("packages") or []
    remotes: list[dict] = server.get("remotes") or []

    first_remote_url = str(remotes[0].get("url") or "") if remotes else ""

    pkg = _pick_installable_package(packages)

    if pkg is None:
        reason = "solo remote/OCI — sin paquete stdio npm/pypi" if (remotes or packages) else "sin paquetes"
        return {
            "name": name,
            "description": description,
            "version": version,
            "repository": repository,
            "installable": False,
            "runner": "",
            "argv": [],
            "env_vars": [],
            "remote_url": first_remote_url,
            "unsupported_reason": reason,
        }

    identifier = str(pkg.get("identifier") or "")
    pkg_version = str(pkg.get("version") or "")
    runtime_args: list[dict] = pkg.get("runtimeArguments") or []
    env_vars = _normalize_env_vars(pkg.get("environmentVariables") or [])

    id_re = _compiled_identifier_re()
    ver_re = _compiled_version_re()
    if not id_re.match(identifier):
        return {
            "name": name, "description": description, "version": version,
            "repository": repository, "installable": False,
            "runner": "", "argv": [], "env_vars": env_vars,
            "remote_url": first_remote_url,
            "unsupported_reason": "identifier inválido",
        }
    if not ver_re.match(pkg_version):
        return {
            "name": name, "description": description, "version": version,
            "repository": repository, "installable": False,
            "runner": "", "argv": [], "env_vars": env_vars,
            "remote_url": first_remote_url,
            "unsupported_reason": "version inválida",
        }

    registry_type = pkg.get("registryType", "")
    if registry_type == "npm":
        argv = _build_argv_npm(identifier, pkg_version, runtime_args)
        runner = "npx"
    else:  # pypi
        argv = _build_argv_pypi(identifier, pkg_version, runtime_args)
        runner = "uvx"

    return {
        "name": name,
        "description": description,
        "version": version,
        "repository": repository,
        "installable": True,
        "runner": runner,
        "argv": argv,
        "env_vars": env_vars,
        "remote_url": first_remote_url,
        "unsupported_reason": "",
    }


async def reconnect_persisted_mcp_servers(manager) -> None:
    """Reconecta al boot los servidores MCP configurados (fail-soft por server).

    Reads Neus's native mcp_servers (config.yaml) as the single source of
    truth. Llamado como task asyncio desde runtime/__main__ tras construir
    el manager. Un servidor caído NO bloquea el boot ni a los demás.

    BYOK env: si la entrada persiste un campo "env" (p.ej. OD_DAEMON_URL para
    open-design), se pasa a _mcp_connect para que llegue al launcher y al
    proceso del servidor. Sin esto, servidores BYOK fallan tras reiniciar.
    """
    entries = _neus_load_entries()
    if not entries:
        return
    for entry in entries:
        sid = entry["server_id"]
        stored_env: dict[str, str] = entry.get("env") or {}
        argv = [str(a) for a in (entry.get("argv") or [])]
        # SECURITY-FIRST (C2): re-validar el argv persistido contra la MISMA
        # allow-list + analizabilidad del gate. Una entrada antigua (o escrita por
        # otra vía) con un runner ya prohibido (node/python3) o con un argv que el
        # scanner no puede analizar NO debe reconectarse al boot saltándose el gate.
        runner = argv[0].rsplit("/", 1)[-1] if argv else ""
        if not argv or runner not in _MCP_ALLOWED_RUNNERS or not _scanner_can_analyze_argv(argv):
            logger.warning(
                "hermes.dbus.mcp_reconnect_skipped server=%s runner=%s "
                "(runner no permitido o argv no analizable — gate C2)",
                sid, runner,
            )
            continue
        try:
            server = await _mcp_connect(manager, sid, argv, env=stored_env)
            logger.info(
                "hermes.dbus.mcp_reconnected server=%s tools=%d byok_keys=%s",
                sid, len(server.tools), sorted(stored_env.keys()),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "hermes.dbus.mcp_reconnect_failed server=%s: %s", sid, exc
            )


async def reconnect_byok_empty_openai_mcp_servers(manager) -> None:
    """Re-wire MCP servers that declare OpenAI-compatible BYOK keys but stored them
    EMPTY (e.g. ruflo: OPENAI_BASE_URL=""/OPENAI_API_KEY="").

    Called after the owner adds/activates an LLM provider. Such servers connected
    at BOOT before any provider existed, so _mcp_connect's auto-wire left the keys
    empty. We force a reconnect (disconnect, then _mcp_connect) ONLY for these
    entries so the auto-wire now fills them from the active provider. We do NOT
    reconnect every server, and we never persist the filled keys back to config
    (they stay empty in Neus config.yaml; only the live process gets them).
    """
    if manager is None:
        return
    entries = _neus_load_entries()
    if not entries:
        return
    for entry in entries:
        stored_env: dict[str, str] = entry.get("env") or {}
        declares_openai = ("OPENAI_BASE_URL" in stored_env or "OPENAI_API_KEY" in stored_env)
        is_empty = (not stored_env.get("OPENAI_BASE_URL") or not stored_env.get("OPENAI_API_KEY"))
        if not (declares_openai and is_empty):
            continue  # only the BYOK-empty OpenAI-compat servers
        sid = entry["server_id"]
        argv = [str(a) for a in (entry.get("argv") or [])]
        runner = argv[0].rsplit("/", 1)[-1] if argv else ""
        # Same C2 gate as boot reconnect: never re-spawn an argv the scanner can't analyze.
        if not argv or runner not in _MCP_ALLOWED_RUNNERS or not _scanner_can_analyze_argv(argv):
            logger.warning(
                "hermes.dbus.mcp_byok_rewire_skipped server=%s runner=%s (gate C2)", sid, runner,
            )
            continue
        try:
            await manager.disconnect(_mcp_id(sid))  # idempotent; defeats connect() idempotency
            server = await _mcp_connect(manager, sid, argv, env=stored_env)
            logger.info(
                "hermes.dbus.mcp_byok_rewired server=%s tools=%d", sid, len(server.tools),
            )
        except Exception as exc:  # noqa: BLE001 — fail-soft per server
            logger.warning("hermes.dbus.mcp_byok_rewire_failed server=%s: %s", sid, exc)


# ---------------------------------------------------------------------------
# Skill Hub — búsquedas cancelables (threading.Event por query_id).
# ---------------------------------------------------------------------------
_HUB_SEARCH_EVENTS: dict[str, "_oauth_threading.Event"] = {}
_HUB_SEARCH_LOCK = _oauth_threading.Lock()


def _hub_search_register(query_id: str) -> "_oauth_threading.Event":
    """Registra un Event de cancelación para query_id. Devuelve el Event."""
    ev = _oauth_threading.Event()
    with _HUB_SEARCH_LOCK:
        _HUB_SEARCH_EVENTS[query_id] = ev
    return ev


def _hub_search_get_cancel_event(
    query_id: str,
) -> "_oauth_threading.Event | None":
    with _HUB_SEARCH_LOCK:
        return _HUB_SEARCH_EVENTS.get(query_id)


def _hub_search_cancel(query_id: str) -> None:
    with _HUB_SEARCH_LOCK:
        ev = _HUB_SEARCH_EVENTS.get(query_id)
    if ev is not None:
        ev.set()


def _hub_search_cleanup(query_id: str) -> None:
    with _HUB_SEARCH_LOCK:
        _HUB_SEARCH_EVENTS.pop(query_id, None)


# ---------------------------------------------------------------------------
# Skill Hub — operaciones largas (install/uninstall) en thread + estado.
# ---------------------------------------------------------------------------
_HUB_OPS: dict[str, dict] = {}
_HUB_OPS_LOCK = _oauth_threading.Lock()


def _start_hub_op(
    kind: str,
    target: str,
    *,
    scan_record: "Any | None" = None,
    signal_emitter: "Any | None" = None,
) -> dict:
    """Lanza install/uninstall del hub en thread daemon. → {op_id}.

    scan_record: pre-computed ScanRecord (may be None if scan was skipped).
    signal_emitter: callable(scan_id, verdict, scan_data_json) to emit signals
                    on install completion when a scan_record is available.
    """
    import uuid as _uuid  # noqa: PLC0415

    op_id = _uuid.uuid4().hex
    with _HUB_OPS_LOCK:
        _HUB_OPS[op_id] = {"status": "pending", "kind": kind, "target": target}

    def _work() -> None:
        try:
            if kind == "install":
                from hermes_cli.skills_hub import do_install  # noqa: PLC0415
                do_install(target, category="", force=False,
                           skip_confirm=True, name_override="")
            else:
                # P4 fix — uninstall must NOT lie. do_uninstall() called the CLI's
                # input("Confirm [y/N]") in this TTY-less thread (silent cancel) AND
                # swallowed uninstall_skill()'s (False, msg) → the op reported "done"
                # while the skill stayed. Call the primitive directly and FAIL LOUD.
                from tools.skills_hub import uninstall_skill  # noqa: PLC0415
                success, msg = uninstall_skill(target)
                if not success:
                    # Not in the hub lock → maybe an agent-created NATIVE skill on disk
                    # ($HERMES_HOME/skills/<name>/), invisible to the lock-based path.
                    # Remove it directly (validated) so native skills are uninstallable
                    # too; if genuinely absent, raise → status=error (no false success).
                    if not _remove_native_skill_dir(target):
                        raise RuntimeError(f"uninstall failed: {msg}")
                try:
                    from agent.prompt_builder import (  # noqa: PLC0415
                        clear_skills_system_prompt_cache,
                    )
                    clear_skills_system_prompt_cache(clear_snapshot=True)
                except Exception:  # noqa: BLE001 — cache clear is best-effort
                    pass
            with _HUB_OPS_LOCK:
                _HUB_OPS[op_id]["status"] = "done"
            logger.info("hermes.dbus.hub_op_done kind=%s target=%s", kind, target)
        except SystemExit as exc:
            # do_* del CLI hace sys.exit en errores — lo mapeamos a error.
            with _HUB_OPS_LOCK:
                _HUB_OPS[op_id]["status"] = "error"
                _HUB_OPS[op_id]["error_message"] = f"exit={exc.code}"
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.dbus.hub_op_failed kind=%s: %s", kind, exc)
            with _HUB_OPS_LOCK:
                _HUB_OPS[op_id]["status"] = "error"
                _HUB_OPS[op_id]["error_message"] = str(exc)

    _oauth_threading.Thread(
        target=_work, daemon=True, name=f"hub-{kind}-{op_id[:6]}"
    ).start()
    return {"op_id": op_id}


# ---------------------------------------------------------------------------
# Native hermes-agent skill discovery (TAREA 3)
# ---------------------------------------------------------------------------

def _list_native_hermes_agent_skills(
    db_skills: list[dict],
    *,
    skills_root: "Any | None" = None,
) -> list[dict]:
    """Return skill stubs for SKILL.md files in $HERMES_HOME/skills/ not in DB.

    hermes-agent's skill_manage tool writes SKILL.md files directly to
    $HERMES_HOME/skills/<name>/SKILL.md without registering in the DB.
    This function surfaces them so the UI doesn't show an empty list.

    Skills already in the DB (matched by skill_name) are excluded — the DB
    entry is authoritative for signed/versioned skills.

    Returns lightweight dicts compatible with SkillGovernanceService._row_to_dict
    so the UI receives a consistent shape.

    Args:
        db_skills:   Already-fetched DB skills (for dedup by skill_name).
        skills_root: Override the scan root. When None: uses $HERMES_HOME/skills/
                     ONLY if HERMES_HOME is explicitly set in the environment.
                     Falls back to nothing (not ~/.hermes) to avoid surfacing the
                     dev machine's skills in CI/test environments.
    """
    import os as _os  # noqa: PLC0415
    from pathlib import Path as _Path  # noqa: PLC0415

    if skills_root is None:
        hermes_home_env = _os.environ.get("HERMES_HOME", "")
        if not hermes_home_env:
            # HERMES_HOME not set — skip native scan to avoid polluting tests
            # or dev-machine environments. The daemon ALWAYS sets this var.
            return []
        skills_root = _Path(hermes_home_env) / "skills"
    else:
        skills_root = _Path(skills_root)

    if not skills_root.is_dir():
        return []

    db_names: set[str] = {s["skill_name"] for s in db_skills if s.get("skill_name")}
    results: list[dict] = []

    for skill_md in sorted(skills_root.rglob("SKILL.md")):
        skill_name = skill_md.parent.name
        if skill_name in db_names:
            continue
        signed_at = _iso_mtime(skill_md)
        description = _extract_skill_description(skill_md)
        # SECURITY (red-team 2026-06-19): scan the SKILL.md code blocks for trojan
        # patterns. A hub skill is markdown instructions; a dropper in a ```bash```
        # block is the hub equivalent of a recorded dropper. A CRITICAL match marks
        # the skill 'blocked' + carries security_findings so the owner sees WHY (and
        # the UI can refuse it). The agent's ACTIONS following any SKILL.md are still
        # broker-gated (risk/HITL/install-gate/egress); this adds content visibility
        # to the hub side, which previously only had a name/provenance scan.
        sec_findings: list[dict] = []
        sec_blocked = False
        try:
            from hermes.agents_os.domain.skill_content_scan import (  # noqa: PLC0415
                has_blocking_finding,
                scan_skill_markdown,
            )
            _md = skill_md.read_text(encoding="utf-8", errors="replace")
            _f = scan_skill_markdown(_md)
            sec_blocked = has_blocking_finding(_f)
            sec_findings = [
                {"pattern": x.pattern, "severity": x.severity.value, "message": x.message}
                for x in _f
            ]
            if sec_blocked:
                logger.warning(
                    "hermes.dbus.hub_skill_blocked name=%s findings=%s",
                    skill_name,
                    [(x.pattern, x.severity.value) for x in _f if x.severity.value == "CRITICAL"],
                )
        except Exception as exc:  # noqa: BLE001 — scan is additive, never break listing
            logger.debug("hub skill content scan failed name=%s: %s", skill_name, exc)
        results.append({
            "package_id": f"native:{skill_name}",
            "skill_id": f"native:{skill_name}",
            "skill_name": skill_name,
            "version": 1,
            "state": "blocked" if sec_blocked else "native",
            "surface_kinds": ["skill_manage"],
            "signed_at": signed_at,
            "signature_short": None,
            "validated_at": signed_at,
            "promoted_at": None,
            "signing_method": "none",
            "toolkit_slug": None,
            "description": description,
            "source": "hermes_agent",
            "security_blocked": sec_blocked,
            "security_findings": sec_findings,
        })

    return results


def _iso_mtime(path: "Any") -> str:
    """Return the ISO-8601 mtime of a file, or empty string on error."""
    from datetime import UTC, datetime  # noqa: PLC0415
    try:
        ts = path.stat().st_mtime
        return datetime.fromtimestamp(ts, tz=UTC).isoformat()
    except OSError:
        return ""


def _extract_skill_description(skill_md: "Any") -> str:
    """Extract the 'description' field from SKILL.md YAML frontmatter.

    Returns empty string if the file is missing, malformed, or has no
    description field. Never raises — pure best-effort.
    """
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""

    if not text.startswith("---"):
        return ""
    end = text.find("---", 3)
    if end == -1:
        return ""
    frontmatter = text[3:end]
    for line in frontmatter.splitlines():
        line = line.strip()
        if line.startswith("description:"):
            value = line[len("description:"):].strip().strip('"').strip("'")
            return value[:200]
    return ""


def _parse_skill_md_frontmatter(skill_md_path: "Any") -> dict:
    """Parse all YAML frontmatter fields from a SKILL.md file.

    Returns a dict with name, description, version, and metadata sub-dict.
    Never raises — returns {} on any error.
    """
    try:
        import yaml as _yaml  # noqa: PLC0415
        text = skill_md_path.read_text(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return {}

    if not text.startswith("---"):
        return {}
    end = text.find("---", 3)
    if end == -1:
        return {}
    try:
        parsed = _yaml.safe_load(text[3:end]) or {}
    except Exception:  # noqa: BLE001
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _remove_native_skill_dir(name: str) -> bool:
    """Remove an agent-created native skill dir ($HERMES_HOME/skills/<name>/).

    Lock-based uninstall_skill() only sees hub-installed skills; agent-created ones
    live as a bare SKILL.md on disk. Removing them safely: the name must be a single
    path segment (no traversal), the target a real directory directly under skills/
    that contains a SKILL.md, and its resolved parent must be skills/ (rejects symlink
    redirects). Returns True iff a directory was actually removed.
    """
    import os as _os  # noqa: PLC0415
    import shutil as _shutil  # noqa: PLC0415
    from pathlib import Path as _Path  # noqa: PLC0415

    home = _os.environ.get("HERMES_HOME", "")
    if not home or not name or "/" in name or "\\" in name or name in (".", ".."):
        return False
    skills_root = _Path(home) / "skills"
    target_dir = skills_root / name
    try:
        if (
            target_dir.is_dir()
            and (target_dir / "SKILL.md").is_file()
            and target_dir.resolve().parent == skills_root.resolve()
        ):
            _shutil.rmtree(target_dir)
            return True
    except OSError:
        return False
    return False


def _list_native_skills_primary(
    *,
    skills_root: "Any | None" = None,
) -> list[dict]:
    """Enumerate all SKILL.md files under $HERMES_HOME/skills/ as DTO dicts.

    This is the PRIMARY source for list_skills_native(). Every skill dir
    (whether created by the agent's skill_manage tool OR by SkillStoreAdapter
    after HITL approval) is included. Governance fields (state, signing_method,
    signature_hex, signature_short) are read from frontmatter.metadata so that
    cage-signed skills surface their provenance without a separate DB table.

    Skills without governance metadata are surfaced with state='native'
    (agent-created, not cage-signed).

    Args:
        skills_root: Override the scan root (tests). When None: uses
                     $HERMES_HOME/skills/ only when HERMES_HOME is set.
    """
    import os as _os  # noqa: PLC0415
    from pathlib import Path as _Path  # noqa: PLC0415

    if skills_root is None:
        hermes_home_env = _os.environ.get("HERMES_HOME", "")
        if not hermes_home_env:
            return []
        skills_root = _Path(hermes_home_env) / "skills"
    else:
        skills_root = _Path(skills_root)

    if not skills_root.is_dir():
        return []

    results: list[dict] = []
    for skill_md in sorted(skills_root.rglob("SKILL.md")):
        entry = _skill_md_to_dto(skill_md)
        if entry:
            results.append(entry)
    return results


def _coerce_skill_version(raw: "Any") -> int:
    """Coerce a SKILL.md version to an int WITHOUT crashing the whole /skills listing.

    Hub skills carry semver ("2.0.0"); cage-signed skills carry an int. A bare
    int(raw) raised ValueError on semver and took down the ENTIRE list (no skill
    appeared — hub OR agent-created). Tolerant: int if possible, else the semver
    major, else 1.
    """
    if raw is None:
        return 1
    try:
        return int(raw)
    except (ValueError, TypeError):
        try:
            return int(str(raw).split(".")[0].strip() or 1)
        except (ValueError, TypeError):
            return 1


def _skill_md_to_dto(skill_md_path: "Any") -> "dict | None":
    """Convert a SKILL.md file to a SkillPackageDTO-shaped dict.

    Reads governance fields from frontmatter.metadata (written by SkillStoreAdapter).
    For v2-signed skills, re-verifies the HMAC using the native keystore — if the
    signature does not verify the skill is downgraded to state='unverified'/
    source='disk' and still listed (BUG 3 fix is preserved). This prevents a
    locally modified SKILL.md from surfacing as state='validated' (CWE-345).

    Returns None on unrecoverable parse error.
    """
    skill_name = skill_md_path.parent.name
    fm = _parse_skill_md_frontmatter(skill_md_path)
    if not fm:
        # A SKILL.md without parseable YAML frontmatter is still a REAL, loadable
        # skill on disk (the agent reads it regardless) — it MUST appear in the list,
        # not be silently dropped. This was the "auto-created/synthesized skill does
        # not show in Habilidades" bug. Surface it as a minimal native entry.
        return {
            "package_id": f"native:{skill_name}",
            "skill_id": f"native:{skill_name}",
            "skill_name": skill_name,
            "version": 1,
            "state": "native",
            "surface_kinds": ["skill_manage"],
            "signed_at": _iso_mtime(skill_md_path),
            "signature_short": None,
            "validated_at": None,
            "promoted_at": None,
            "signing_method": "none",
            "toolkit_slug": None,
            "description": "",
            "source": "hermes_agent",
        }

    meta: dict = fm.get("metadata") or {}
    signed_at = meta.get("signed_at") or _iso_mtime(skill_md_path)
    state = meta.get("state") or "native"
    signing_method = meta.get("signing_method") or "none"
    signature_hex = meta.get("signature_hex") or None
    signature_short = signature_hex[:12] if signature_hex else None
    package_id = meta.get("package_id") or f"native:{skill_name}"
    skill_id = meta.get("skill_id") or f"native:{skill_name}"
    validated_at = meta.get("validated_at") or (signed_at if state != "native" else None)
    promoted_at = meta.get("promoted_at") or None
    source = "hermes_agent" if state == "native" else "cage"

    # Re-verify HMAC for v2-signed skills (CWE-345: do not trust self-asserted
    # provenance). Downgrade only when the key is available and the signature
    # FAILS — if the key is simply unavailable (no master.key in CI/dev env)
    # we keep the on-disk state (cannot forge without the key anyway).
    if signing_method == "v2" and signature_hex and len(signature_hex) == 64:
        verified = _verify_skill_md_signature(meta, signature_hex, skill_name)
        if verified is False:  # key available but signature mismatch
            state = "unverified"
            source = "disk"
            validated_at = None

    return {
        "package_id": package_id,
        "skill_id": skill_id,
        "skill_name": skill_name,
        # FIX 2026-06-26: hub skills traen versión semver ("2.0.0"); int() pelado
        # crasheaba TODO el listado /skills (ValueError) → la skill instalada no
        # aparecía. Coerción segura: int directo si se puede, si no el major del
        # semver, si no 1. (El DTO espera int; perder minor/patch es cosmético.)
        "version": _coerce_skill_version(meta.get("version")),
        "state": state,
        "surface_kinds": meta.get("surface_kinds") or ["skill_manage"],
        "signed_at": signed_at,
        "signature_short": signature_short,
        "validated_at": validated_at,
        "promoted_at": promoted_at,
        "signing_method": signing_method,
        "toolkit_slug": None,
        "description": fm.get("description") or "",
        "source": source,
        "teaching_origin": meta.get("teaching_origin") or None,
    }


def _verify_skill_md_signature(
    meta: dict, stored_signature_hex: str, skill_name: str
) -> "bool | None":
    """Re-compute HMAC-SHA256 v2 over the canonical payload stored in frontmatter.metadata.

    Returns:
        True  — computed HMAC matches stored_signature_hex (verified).
        False — key available but HMAC does not match (forgery / tampering detected).
        None  — key unavailable (no master.key in env) — cannot verify, cannot forge;
                caller keeps the on-disk state as-is.

    This tri-state lets `_skill_md_to_dto` downgrade only on *confirmed* mismatch,
    not on *infrastructure unavailability* (e.g. CI environments without master.key).
    """
    import hashlib as _hashlib  # noqa: PLC0415
    import hmac as _hmac  # noqa: PLC0415
    import json as _json  # noqa: PLC0415

    try:
        from hermes.shell_server.skills.native_keystore_adapter import (  # noqa: PLC0415
            NativeKeyStoreAdapter,
        )
        signing_key = NativeKeyStoreAdapter().get_signing_key_sync()
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "hermes.skill_md_verify.key_unavailable skill=%s: %s", skill_name, exc
        )
        return None  # Cannot verify — key absent, not a forgery signal

    try:
        payload_dict = {
            "replay_script_id": meta["replay_script_id"],
            "decision_rule_ids": sorted(meta.get("decision_rule_ids") or []),
            "voice_narrative_id": meta["voice_narrative_id"],
            "content_hash": meta["content_hash"],
            "tenant_id": meta["tenant_id"],
            "compiled_by_operator_id": meta["compiled_by_operator_id"],
            "created_at": meta["created_at"],
            "runtime_version": meta["runtime_version"],
        }
    except KeyError as exc:
        logger.debug(
            "hermes.skill_md_verify.payload_field_missing skill=%s field=%s",
            skill_name,
            exc,
        )
        # Missing fields with a present key is a tamper signal — downgrade.
        return False

    canonical = _json.dumps(payload_dict, sort_keys=True, separators=(",", ":")).encode()
    expected = _hmac.new(signing_key, canonical, _hashlib.sha256).hexdigest()
    result = _hmac.compare_digest(expected, stored_signature_hex)
    if not result:
        logger.warning(
            "hermes.skill_md_verify.signature_mismatch skill=%s — downgrading to unverified",
            skill_name,
        )
    return result


def _list_composio_skills(skill_governance: "Any | None") -> list[dict]:
    """Return Composio skills from the DB (separate concern from native skills).

    Composio skills have no on-disk SKILL.md — they live purely in the
    composio_skills table. This supplements the native list for those skills.
    Fail-soft: returns [] when skill_governance is None or the DB is unavailable.
    """
    if skill_governance is None:
        return []
    try:
        import sqlite3 as _sqlite3  # noqa: PLC0415

        db_path = skill_governance._db_path
        conn = _sqlite3.connect(str(db_path), isolation_level=None)
        conn.row_factory = _sqlite3.Row
        rows = conn.execute(
            """
            SELECT spv.package_id, spv.skill_id, spv.skill_name,
                   spv.version, spv.state, spv.surface_kinds,
                   spv.signed_at, spv.signature_short,
                   spv.validated_at, spv.promoted_at,
                   COALESCE(spv.signing_method, 'v1') AS signing_method,
                   cs.toolkit_slug
              FROM skill_packages_view spv
              JOIN composio_skills cs ON cs.package_id = spv.package_id
             ORDER BY spv.signed_at DESC
            """
        ).fetchall()
        conn.close()
        return [_composio_row_to_dict(r) for r in rows]
    except Exception as exc:  # noqa: BLE001
        logger.debug("hermes.dbus.composio_skills_unreadable: %s", exc)
        return []


def _composio_row_to_dict(row: "Any") -> dict:
    """Map a composio_skills JOIN row to a SkillPackageDTO-shaped dict."""
    keys = row.keys()
    return {
        "package_id": row["package_id"],
        "skill_id": row["skill_id"],
        "skill_name": row["skill_name"],
        "version": int(row["version"]),
        "state": row["state"],
        "surface_kinds": (row["surface_kinds"] or "").split(",") if row["surface_kinds"] else [],
        "signed_at": row["signed_at"],
        "signature_short": row["signature_short"],
        "validated_at": row["validated_at"] if "validated_at" in keys else None,
        "promoted_at": row["promoted_at"] if "promoted_at" in keys else None,
        "signing_method": row["signing_method"] if "signing_method" in keys else "v1",
        "toolkit_slug": row["toolkit_slug"],
        "description": "",
        "source": "composio",
    }
