"""IsolatedExecutionContextFactory — aislamiento FÍSICO de contextos de ejecución.

T064 (CTRL-P1-19, G5, FR-028, Constitución I).

Implementa `ExecutionContextFactory` (port en `execution/domain/ports.py`).

Aislamiento físico:
  - BROWSER: arranca un proceso `agent-browser --session {isolation_key}` por
    contexto. Cada session tiene su propio daemon, cookies, tabs y estado de
    navegador. IDÉNTICO al patrón TeachingContextFactory (spec 004), que ya
    arranca browsers con `--session teach-{id}`.
  - KEYBOARD/MOUSE/SCREEN: reserva el seat/headless display indicado en
    `isolation_seed`. En este adaptador el aislamiento físico de
    teclado/ratón/display es declarativo (no lanza procesos de SO): el sistema
    ya garantiza exclusividad vía el registro in-memory + el UNIQUE parcial de
    SQLite (FR-021). La factory anota la superficie en el registry.

Constitución I (FR-028): NUNCA modifica BrowserPort / SelectorRegistry /
BrowserSession / StorageStatePort. El aislamiento ocurre AQUÍ, en la factory,
instanciando AgentBrowserCli con el session-name derivado de `isolation_seed`.
El test `tests/security/test_public_contracts_frozen.py` debe permanecer verde.

Ciclo de vida:
  1. `open(context_id, surface_kind, isolation_seed)` → deriva isolation_key
     canónico (IsolationKeyMapper), registra el dueño en el registry (claim),
     inicia el proceso de SO (si aplica), devuelve ExecutionContext.
  2. `close(context_id)` → libera TODAS las superficies del owner en el
     registry (release_all_for), termina el proceso de SO (si aplica).

Error handling:
  - Si el claim falla (InputOwnershipViolation), NO se inicia ningún proceso
    de SO (fail-closed antes de tocar recursos).
  - Si el proceso de SO falla al iniciarse, el claim se revierte (release).
"""

from __future__ import annotations

import inspect
import logging
from uuid import UUID

from hermes.execution.application.browser_admission_guard import BrowserAdmissionGuard
from hermes.execution.application.execution_context_registry import (
    ExecutionContextRegistry,
)
from hermes.execution.domain.isolation_key_mapper import (
    IsolationKeyMapper,
    PhysicalSurface,
)
from hermes.execution.domain.ports import (
    ExecutionContext,
    ExecutionContextId,
    InputSurfaceKey,
    InputSurfaceKind,
)

logger = logging.getLogger(__name__)

# Isolation key prefixes — distinct namespaces prevent cross-kind collisions.
_EXEC_BROWSER_PREFIX = "exec"
_EXEC_PHYSICAL_PREFIX = "phys"

# Surface kinds that require a browser RAM permit before spawning an OS process.
_BROWSER_SURFACES: frozenset[InputSurfaceKind] = frozenset({
    InputSurfaceKind.BROWSER,
})


class IsolatedExecutionContextFactory:
    """Factory that creates physically-isolated execution contexts.

    Analogous to AgentBrowserTeachingContext (spec 004) but for agent task
    execution contexts. One factory instance per daemon; shared across workers.

    When a BrowserAdmissionGuard is injected, BROWSER surfaces acquire a RAM
    permit before spawning any OS process (Phase 2a RAM-safety layer). If guard
    is None, behavior is unchanged (backwards compat for non-browser paths and
    existing tests).
    """

    def __init__(
        self,
        *,
        registry: ExecutionContextRegistry,
        guard: BrowserAdmissionGuard | None = None,
    ) -> None:
        self._registry = registry
        self._guard = guard
        # context_id.value (UUID) → (InputSurfaceKey, process handle | None, admitted: bool)
        self._open_contexts: dict[UUID, tuple[InputSurfaceKey, object | None, bool]] = {}

    async def open(
        self,
        *,
        context_id: ExecutionContextId,
        surface_kind: InputSurfaceKind,
        isolation_seed: str,
        domains_whitelist: tuple[str, ...] = (),
    ) -> ExecutionContext:
        """Open an isolated context, claim its surface, and start the OS resource.

        For BROWSER surfaces, acquires a RAM permit from BrowserAdmissionGuard
        (if one is configured) AFTER the registry claim and BEFORE spawning the
        OS process. On any failure in this path the permit is released so no
        orphan permits can accumulate.

        Raises:
            InputOwnershipViolation: surface already owned (fail-closed).
            BrowserAdmissionDenied: shutdown during RAM wait.
        """
        isolation_key = _derive_isolation_key(surface_kind, isolation_seed)
        surface = InputSurfaceKey(kind=surface_kind, surface_id=isolation_seed)

        # Claim BEFORE touching any OS resource (fail-closed).
        self._registry.claim(
            surface=surface,
            owner=context_id,
            isolation_key=isolation_key,
        )

        needs_permit = surface_kind in _BROWSER_SURFACES and self._guard is not None
        admitted = False
        process_handle: object | None = None

        try:
            if needs_permit:
                # Acquire RAM permit after registry claim, before OS spawn.
                # BrowserAdmissionGuard.acquire parks under memory pressure;
                # it never rejects unless shutdown is signaled.
                assert self._guard is not None
                await self._guard.acquire(str(context_id.value))
                admitted = True

            process_handle = await self._start_os_resource(
                surface_kind, isolation_key, domains_whitelist
            )
        except Exception:
            # Release guard permit (if acquired) before propagating.
            if admitted and self._guard is not None:
                self._guard.release(str(context_id.value))
            # Release registry claim to avoid orphan surface.
            self._registry.release(surface=surface)
            raise

        self._open_contexts[context_id.value] = (surface, process_handle, admitted)

        ctx = ExecutionContext(
            context_id=context_id,
            surface=surface,
            isolation_key=isolation_key,
        )
        logger.info(
            "exec_context.opened context_id=%s surface=%s isolation_key=%s admitted=%s",
            context_id.value,
            f"{surface.kind}:{surface.surface_id}",
            isolation_key,
            admitted,
        )
        return ctx

    async def close(self, *, context_id: ExecutionContextId) -> None:
        """Close the context, terminate OS resources, and release all surfaces.

        Release order (invariant): stop OS process → registry release → guard release.
        The guard permit is released even if _stop_os_resource raises, so no
        orphan permits accumulate.
        """
        entry = self._open_contexts.pop(context_id.value, None)
        if entry is None:
            logger.warning(
                "exec_context.close called for unknown context_id=%s (no-op)",
                context_id.value,
            )
            return

        _, process_handle, admitted = entry
        try:
            await self._stop_os_resource(process_handle)
        finally:
            # release_all_for runs unconditionally even if _stop_os_resource
            # raises. Without try/finally a future change that lets
            # _stop_os_resource propagate would leave the surface claimed
            # until the next daemon reconcile (orphan surface).
            released = self._registry.release_all_for(owner=context_id)
            logger.info(
                "exec_context.closed context_id=%s surfaces_released=%d",
                context_id.value,
                released,
            )
            # Guard permit released AFTER registry (process stopped, surface freed).
            # Idempotent: guard.release is no-op for unknown context_ids.
            if admitted and self._guard is not None:
                self._guard.release(str(context_id.value))

    # ------------------------------------------------------------------
    # OS resource management (browser --session / headless display)
    # ------------------------------------------------------------------

    async def _start_os_resource(
        self,
        surface_kind: InputSurfaceKind,
        isolation_key: str,
        domains_whitelist: tuple[str, ...] = (),
    ) -> object | None:
        """Start the OS-level isolation resource for *surface_kind*."""
        if surface_kind == InputSurfaceKind.BROWSER:
            return self._open_browser_session(isolation_key, domains_whitelist)
        # Physical surfaces (keyboard, mouse, screen) are isolated declaratively
        # via the registry + UNIQUE index. No OS process to start here.
        return None

    async def _stop_os_resource(self, process_handle: object | None) -> None:
        """Terminate the OS resource if one was started."""
        if process_handle is None:
            return
        closer = getattr(process_handle, "close", None)
        if closer is None:
            return
        try:
            # AgentBrowserCli.close es async; un fake/handle síncrono no lo es.
            # Await sólo si devuelve un awaitable → no se filtra el proceso.
            result = closer()
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001
            logger.warning("exec_context: error closing OS resource", exc_info=True)

    def _open_browser_session(
        self,
        isolation_key: str,
        domains_whitelist: tuple[str, ...] = (),
    ) -> object | None:
        """Open an agent-browser session for the given isolation_key.

        When HERMES_BROWSER_SANDBOX=openshell: returns a CdpBrowserCliAdapter
        that attaches to the Chromium already running inside the OpenShell sandbox
        via CDP. The CDP URL is read from HERMES_CDP_URL (default 127.0.0.1:9222).
        No local browser process is spawned; the sandbox owns the Chromium lifecycle.

        Otherwise: launches a local AgentBrowserCli session (the original path).
        The session is launched inside the browser jail (systemd-run --scope
        + Landlock) when HERMES_BROWSER_JAIL=1 (default on node).
        The egress policy is pushed to the proxy socket at start() time.

        Returns the CLI handle or None if neither backend is available.
        """
        import os  # noqa: PLC0415

        sandbox_mode = os.environ.get("HERMES_BROWSER_SANDBOX", "").lower()
        if sandbox_mode == "openshell":
            return self._open_cdp_browser_session(isolation_key)

        try:
            from hermes.browser.infrastructure.agent_browser_cli import (  # noqa: PLC0415
                AgentBrowserCli,
            )

            session_name = f"exec-{isolation_key}"
            cli = AgentBrowserCli(
                session_name=session_name,
                domains_whitelist=domains_whitelist,
                teaching_mode=False,
            )
            logger.info("exec_context.browser_session opened session=%s", session_name)
            return cli
        except ImportError:
            # Browser stack not installed — run in no-browser mode.
            logger.debug("exec_context: agent-browser not available; running headless")
            return None

    def _open_cdp_browser_session(self, isolation_key: str) -> object | None:
        """Open a CdpBrowserCliAdapter backed by the OpenShell sandbox Chromium.

        Reuses the existing sandbox CDP forward (HERMES_CDP_URL, default 9222).
        The adapter lazy-starts on the first navigation; no process is spawned here.
        Returns None if CdpPlaywrightDriver cannot be imported (playwright not installed).
        """
        try:
            from hermes.browser.infrastructure.cdp_playwright_driver import (  # noqa: PLC0415
                CdpPlaywrightDriver,
            )
            from hermes.browser.infrastructure.cdp_browser_cli_adapter import (  # noqa: PLC0415
                CdpBrowserCliAdapter,
            )

            driver = CdpPlaywrightDriver.from_env()
            adapter = CdpBrowserCliAdapter(driver=driver)
            logger.info(
                "exec_context.cdp_browser_session opened isolation_key=%s cdp_url=%s",
                isolation_key,
                driver._cdp_url,
            )
            return adapter
        except ImportError:
            logger.warning(
                "exec_context: CdpPlaywrightDriver not available (playwright not installed); "
                "browser surface will be unavailable in openshell mode"
            )
            return None


def _derive_isolation_key(kind: InputSurfaceKind, seed: str) -> str:
    """Derive the isolation_key from surface kind and isolation seed.

    Injective: distinct (kind, seed) pairs always produce distinct keys.
    Deterministic: same inputs always produce the same key.
    """
    physical = PhysicalSurface(kind=kind, surface_id=seed)
    return IsolationKeyMapper.key_for(physical)
