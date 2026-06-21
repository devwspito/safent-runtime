"""BrowserSurfaceAdapter — SurfaceAdapterPort implementation for SurfaceKind.BROWSER.

Navigation policy (user decision, revisada):
  Las fronteras reales son (1) confinamiento del kernel del SO y (2) HITL del
  broker sobre acciones peligrosas. Este adapter NO es la frontera de seguridad.
  READ verbs  (navigate, snapshot, read_url) → web abierta (discovery).
  WRITE verbs (click, type_) → HIGH en registry (HITL obligatorio upstream).
      El adapter aplica además el allowlist de sitios: vacío = WRITE denegado
      (fail-closed Fix-5), no vacío = WRITE restringidos a esos hosts.
  Anti-exfiltración real → egress de red del SO (netns/nftables), NO app-layer.
  storage_state → MVP: TODO seam (restore/persist de EncryptedStorageState).

Security invariants:
  - storage_state is NEVER placed on CapturedAction.payload.
  - storage_state is NEVER logged.
  - factory.open (permit acquisition + browser spawn) happens ONLY inside
    replay(), never at propose/HITL time.
  - Unknown op → rejected_by_policy (fail-closed on unknown verbs).
  - Surface mismatch → rejected_by_policy.

TODO (storage_state seam):
  When the project adds a StorageStatePort + key provider to this adapter,
  wire them here in _restore_storage_state() and _persist_storage_state().
  The approved-site check and the session lifecycle are already correct.
  See hermes.browser.domain.ports.storage_state_port.StorageStatePort for
  the contract. The seam is _restore_storage_state / _persist_storage_state
  below; both are no-ops until the KMS integration is available.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable
from urllib.parse import urlparse
from uuid import UUID, uuid4

from hermes.agents_os.domain.ports.surface_adapter_port import (
    CapturedAction,
    ReplayOutcome,
    ReplayStatus,
)
from hermes.agents_os.domain.surface_kind import SurfaceKind
from hermes.browser.application.browser_session_registry import (
    BrowserSessionRegistry,
    BrowserTaskSession,
)
from hermes.browser.infrastructure.agent_browser_cli import (
    AgentBrowserCli,
    AgentBrowserCommandError,
    AgentBrowserNotInstalledError,
)
from hermes.execution.domain.ports import (
    ExecutionContextId,
    InputOwnerKind,
    InputSurfaceKind,
)

logger = logging.getLogger("hermes.agents_os.browser_surface_adapter")

# Verbs that only read the page; open web allowed.
# F-06: esquemas de URL permitidos para navigate (fail-closed contra file:/chrome:/javascript:/data:).
_ALLOWED_URL_SCHEMES: frozenset[str] = frozenset({"http", "https"})

_READ_VERBS: frozenset[str] = frozenset({"navigate", "snapshot", "read_url"})

# Verbs that mutate page state; require approved site.
_WRITE_VERBS: frozenset[str] = frozenset({"click", "type_"})

_ALL_VERBS: frozenset[str] = _READ_VERBS | _WRITE_VERBS

# Callable type for approved-sites provider.
ApprovedSitesProvider = Callable[[UUID], frozenset[str]]


def _empty_approved_sites(_tenant_id: UUID) -> frozenset[str]:
    """Default provider: sin allowlist configurado.

    Devuelve frozenset() vacío. La lógica de _verb_write interpreta VACÍO como
    fail-closed (WRITE denegado) a menos que el broker upstream haya aprobado
    la acción vía HITL. Ver Fix-5 / CTRL-5 preventivo.
    """
    return frozenset()


class BrowserSurfaceAdapter:
    """SurfaceAdapterPort for SurfaceKind.BROWSER.

    Inject:
        factory:        IsolatedExecutionContextFactory — open/close sessions.
        registry:       BrowserSessionRegistry — per-task session tracking.
        approved_sites: Callable[[UUID], frozenset[str]] — approved hostnames
                        per tenant. Default (empty set) → all writes denied.
    """

    def __init__(
        self,
        *,
        factory: Any,  # IsolatedExecutionContextFactory — avoid circular import
        registry: BrowserSessionRegistry,
        approved_sites: ApprovedSitesProvider = _empty_approved_sites,
    ) -> None:
        self._factory = factory
        self._registry = registry
        self._approved_sites = approved_sites

    @property
    def surface_kind(self) -> SurfaceKind:
        return SurfaceKind.BROWSER

    # ------------------------------------------------------------------
    # SurfaceAdapterPort — capture (passive, records payload only)
    # ------------------------------------------------------------------

    async def capture(
        self,
        *,
        intent_desc: str,
        params: dict[str, Any],
        tenant_id: UUID,
        human_operator_id: UUID,
    ) -> CapturedAction:
        """Passive capture — records verb + params, no execution."""
        return CapturedAction(
            action_id=uuid4(),
            surface_kind=SurfaceKind.BROWSER,
            intent_desc=intent_desc,
            payload=_sanitize_payload(params),
            tenant_id=tenant_id,
            human_operator_id=human_operator_id,
        )

    # ------------------------------------------------------------------
    # SurfaceAdapterPort — replay (execution path)
    # ------------------------------------------------------------------

    async def replay(
        self,
        action: CapturedAction,
        *,
        hitl_approval_token: str | None = None,
        consent_token: str | None = None,
    ) -> ReplayOutcome:
        """Execute a browser verb. Fail-closed on mismatch, unknown op, policy."""
        if action.surface_kind != SurfaceKind.BROWSER:
            return ReplayOutcome.rejected_by_policy(
                action.action_id,
                reason=f"surface mismatch: expected BROWSER, got {action.surface_kind}",
            )

        op = action.payload.get("op", "")
        if op not in _ALL_VERBS:
            return ReplayOutcome.rejected_by_policy(
                action.action_id,
                reason=f"unknown browser op={op!r}; allowed: {sorted(_ALL_VERBS)}",
            )

        work_item_id = action.work_item_id
        if work_item_id is None:
            return ReplayOutcome.rejected_by_policy(
                action.action_id,
                reason="work_item_id absent on BROWSER action — cannot correlate session",
            )

        tenant_id = action.tenant_id

        start_ms = int(time.monotonic() * 1000)
        try:
            session = await self._get_or_open_session(
                work_item_id=work_item_id,
                tenant_id=tenant_id,
            )
            result = await self._dispatch_verb(
                op=op,
                payload=action.payload,
                session=session,
                action_id=action.action_id,
                tenant_id=tenant_id,
            )
        except _PolicyViolation as exc:
            return ReplayOutcome.rejected_by_policy(action.action_id, reason=str(exc))
        except (AgentBrowserNotInstalledError, AgentBrowserCommandError) as exc:
            logger.error(
                "hermes.browser_surface_adapter.browser_error "
                "work_item_id=%s op=%s error=%s",
                work_item_id,
                op,
                exc,
            )
            return ReplayOutcome.failed(action.action_id, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "hermes.browser_surface_adapter.unexpected_error "
                "work_item_id=%s op=%s error=%s",
                work_item_id,
                op,
                exc,
            )
            return ReplayOutcome.failed(
                action.action_id, error=f"{type(exc).__name__}: {exc}"
            )

        duration_ms = int(time.monotonic() * 1000) - start_ms
        return ReplayOutcome.ok(
            action.action_id, duration_ms=duration_ms, result=result
        )

    # ------------------------------------------------------------------
    # SurfaceAdapterPort — serialize_for_signing
    # ------------------------------------------------------------------

    def serialize_for_signing(self, action: CapturedAction) -> bytes:
        """Canonical serialization for HMAC signing.

        Deterministic: same action → same bytes.
        Excludes storage_state (never in payload) and mutable timestamps.
        """
        canonical = {
            "surface_kind": action.surface_kind.value,
            "intent_desc": action.intent_desc,
            "op": action.payload.get("op", ""),
            "url": action.payload.get("url", ""),
            "ref": action.payload.get("ref", ""),
        }
        return json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()

    # ------------------------------------------------------------------
    # close_task — called from WorkerPool._process finally
    # ------------------------------------------------------------------

    async def close_task(self, work_item_id: UUID) -> None:
        """Close the browser session for work_item_id (idempotent).

        Sequence:
          1. Pop session from registry.
          2. Best-effort persist storage_state (TODO seam — no-op now).
          3. Close factory context (releases RAM permit + browser process).
        """
        session = self._registry.pop(work_item_id)
        if session is None:
            logger.debug(
                "hermes.browser_surface_adapter.close_task.noop work_item_id=%s",
                work_item_id,
            )
            return

        await self._persist_storage_state(session)

        context_id = ExecutionContextId(
            value=session.context_id,
            owner_kind=InputOwnerKind.AGENT_TASK,
        )
        try:
            await self._factory.close(context_id=context_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "hermes.browser_surface_adapter.close_task.factory_close_error "
                "work_item_id=%s error=%s",
                work_item_id,
                exc,
            )

        logger.info(
            "hermes.browser_surface_adapter.close_task.done work_item_id=%s",
            work_item_id,
        )

    # ------------------------------------------------------------------
    # Private: session get-or-open under per-task lock
    # ------------------------------------------------------------------

    async def _get_or_open_session(
        self,
        *,
        work_item_id: UUID,
        tenant_id: UUID | None,
    ) -> BrowserTaskSession:
        """Return existing session or open a new one (double-checked under lock)."""
        lock = self._registry.lock_for(work_item_id)
        async with lock:
            existing = self._registry.get(work_item_id)
            if existing is not None:
                return existing

            session = await self._open_new_session(
                work_item_id=work_item_id,
                tenant_id=tenant_id,
            )
            self._registry.put(work_item_id, session)
            return session

    async def _open_new_session(
        self,
        *,
        work_item_id: UUID,
        tenant_id: UUID | None,
    ) -> BrowserTaskSession:
        """Open a new isolated browser context for work_item_id."""
        context_id_value = _deterministic_context_id(work_item_id)
        context_id = ExecutionContextId(
            value=context_id_value,
            owner_kind=InputOwnerKind.AGENT_TASK,
        )

        ctx = await self._factory.open(
            context_id=context_id,
            surface_kind=InputSurfaceKind.BROWSER,
            isolation_seed=str(work_item_id),
        )

        # factory.open returns an ExecutionContext whose process_handle IS
        # the AgentBrowserCli (set in _open_browser_session). We need the
        # CLI reference; we reconstruct it from the isolation_key because
        # the factory stores the handle internally and closes it in close().
        # We spawn a CLI wrapper that matches the same session-name the
        # factory spawned — the daemon is already running, so this is cheap.
        session_name = f"exec-{ctx.isolation_key}"
        cli = AgentBrowserCli(session_name=session_name)

        # TODO: restore storage_state for approved (tenant_id, site_id)
        # when StorageStatePort + key provider are wired.
        # Call _restore_storage_state(cli, tenant_id, site_id) here.

        session = BrowserTaskSession(
            context_id=context_id_value,
            cli=cli,
            site_id=None,
        )

        logger.info(
            "hermes.browser_surface_adapter.session_opened "
            "work_item_id=%s session=%s",
            work_item_id,
            session_name,
        )
        return session

    # ------------------------------------------------------------------
    # Private: verb dispatch
    # ------------------------------------------------------------------

    async def _dispatch_verb(
        self,
        *,
        op: str,
        payload: dict[str, Any],
        session: BrowserTaskSession,
        action_id: UUID,
        tenant_id: UUID | None,
    ) -> dict[str, Any]:
        cli = session.cli
        if op == "navigate":
            return await self._verb_navigate(cli, payload)
        if op == "snapshot":
            return await self._verb_snapshot(cli)
        if op == "read_url":
            return await self._verb_read_url(cli)
        if op == "click":
            return await self._verb_write(
                "click", cli, payload, session, tenant_id
            )
        if op == "type_":
            return await self._verb_write(
                "type_", cli, payload, session, tenant_id
            )
        # Unreachable: guarded above in replay() — but fail-closed.
        raise _PolicyViolation(f"op={op!r} not reachable (logic error)")

    async def _verb_navigate(
        self, cli: AgentBrowserCli, payload: dict[str, Any]
    ) -> dict[str, Any]:
        url = str(payload.get("url", ""))
        # F-06: sólo http/https. file:/chrome:/javascript:/data: pueden leer
        # ficheros locales o escalar dentro del navegador → rechazo fail-closed.
        scheme = urlparse(url).scheme.lower()
        if scheme not in _ALLOWED_URL_SCHEMES:
            raise _PolicyViolation(
                f"navigate denegado: esquema {scheme!r} no permitido "
                f"(sólo http/https)"
            )
        await cli.navigate(url)
        return {"navigated_to": url}

    async def _verb_snapshot(self, cli: AgentBrowserCli) -> dict[str, Any]:
        text = await cli.snapshot()
        return {"snapshot": text}

    async def _verb_read_url(self, cli: AgentBrowserCli) -> dict[str, Any]:
        url = await cli.current_url()
        return {"current_url": url}

    async def _verb_write(
        self,
        op: str,
        cli: AgentBrowserCli,
        payload: dict[str, Any],
        session: BrowserTaskSession,
        tenant_id: UUID | None,
    ) -> dict[str, Any]:
        """Execute a WRITE verb.

        Fix-5 (CTRL-5 preventivo — fail-closed):
        El gate de site-allowlist es ahora FAIL-CLOSED para WRITE verbs:
          - Set vacío (sin allowlist configurado) → WRITE denegado a nivel adapter.
            La razón: click/type_ ya pasaron por HITL upstream (broker clasificó
            como HIGH); si llegan aquí sin un allowlist, es un despliegue sin
            restricción de site — denegar es la postura más segura. El operador
            debe configurar approved_sites para permitir WRITE en ese host.
          - Set no vacío → exigir host aprobado (comportamiento previo).
        La frontera anti-exfiltración principal sigue siendo egress del SO
        (netns/nftables), no este adapter. Pero este gate previene WRITE en hosts
        no autorizados explícitamente, incluso si HITL fue aprobado.
        """
        approved = self._approved_sites(tenant_id) if tenant_id else frozenset()
        # Fix-5: vacío = fail-closed (no se permiten WRITE sin allowlist).
        if not approved:
            raise _PolicyViolation(
                f"WRITE op={op!r} denegado: no hay approved_sites configurado para "
                f"tenant={tenant_id}. Configure approved_sites para permitir WRITE."
            )
        current_host = await self._current_host(cli)
        if not _host_is_approved(current_host, approved):
            raise _PolicyViolation(
                f"WRITE op={op!r} denegado: host={current_host!r} fuera del "
                f"allowlist del tenant={tenant_id}"
            )
        if op == "click":
            ref = str(payload.get("ref", ""))
            await cli.click(ref)
            return {"clicked": ref}
        if op == "type_":
            ref = str(payload.get("ref", ""))
            text = str(payload.get("text", ""))
            await cli.type_(ref, text)
            return {"typed": {"ref": ref, "length": len(text)}}
        raise _PolicyViolation(f"op={op!r} not a known write verb (logic error)")

    async def _current_host(self, cli: AgentBrowserCli) -> str:
        """Read current URL from the browser and extract the hostname."""
        try:
            url = await cli.current_url()
            return urlparse(url).hostname or ""
        except (AgentBrowserCommandError, AgentBrowserNotInstalledError):
            return ""

    # ------------------------------------------------------------------
    # Private: storage_state seam (TODO — no-op MVP)
    # ------------------------------------------------------------------

    async def _persist_storage_state(self, session: BrowserTaskSession) -> None:
        """Persist storage_state for approved (tenant, site) after task closes.

        TODO: wire StorageStatePort + key provider here when available.
        storage_state MUST NOT be placed on CapturedAction.payload or logged.
        """

    async def _restore_storage_state(
        self,
        cli: AgentBrowserCli,
        tenant_id: UUID | None,
        site_id: str | None,
    ) -> None:
        """Restore storage_state for an approved (tenant, site) on session open.

        TODO: wire StorageStatePort + key provider here when available.
        Only called when site is in approved_sites(tenant_id).
        """


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


class _PolicyViolation(RuntimeError):
    """Internal signal for hybrid policy rejections (converted to rejected_by_policy)."""


def _host_is_approved(host: str, approved: frozenset[str]) -> bool:
    """True if host matches an approved entry (exact or subdomain).

    Empty approved set → no writes allowed (fail-closed).
    """
    if not host or not approved:
        return False
    host_lower = host.lower()
    for entry in approved:
        entry_lower = entry.lower()
        if host_lower == entry_lower or host_lower.endswith("." + entry_lower):
            return True
    return False


def _deterministic_context_id(work_item_id: UUID) -> UUID:
    """Derive a deterministic context UUID from work_item_id.

    Determinism is required so that: if replay() is called twice for the
    same work_item_id (after a restart), the isolation_key is the same.
    We use UUID version 5 with the work_item_id as the name.
    """
    import uuid  # noqa: PLC0415
    return uuid.uuid5(uuid.NAMESPACE_OID, str(work_item_id))


def _sanitize_payload(params: dict[str, Any]) -> dict[str, Any]:
    """Remove storage_state from payload before storing in CapturedAction.

    storage_state must NEVER appear on CapturedAction (security invariant).
    """
    return {k: v for k, v in params.items() if k != "storage_state"}
