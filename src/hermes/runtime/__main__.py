"""hermes-runtime daemon entrypoint — P0 loop autónomo (T027).

Cablea:
  1. SqliteWorkQueue + SqliteAgentState + SqliteAuditRepository sobre shell-state.db.
  2. AuditHashChainSigner sembrado con head_hash del repo al arrancar (AUD-1).
  3. AgentLoopOrchestrator que reemplaza _health_loop; mantiene WATCHDOG=1 (NFR-007).
  4. ConsentContext con operator_id leído de HERMES_OPERATOR_ID (None si no está).

CTRL-13: operator_id=None es válido para arrancar — el daemon NO inventa un
operador anónimo. Las writes fail-closed en el broker (US2) cuando operator=None.

Run:
    python3 -m hermes.runtime [--systemd-notify]
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path
from uuid import UUID

logger = logging.getLogger("hermes-runtime")

_DB_PATH = Path(
    os.environ.get("HERMES_SHELL_DB", "/var/lib/hermes/shell-state.db")
)
_CONSENT_DB_PATH = Path(
    os.environ.get("HERMES_CONSENT_DB", "/var/lib/hermes/consent.db")
)
_HEALTH_INTERVAL_S = float(os.environ.get("HERMES_HEALTH_INTERVAL_S", "30"))
_LEASE_SECONDS = int(os.environ.get("HERMES_LEASE_SECONDS", "60"))

# Semantic tool retrieval: present only the top-K integration tools relevant to the
# turn's intent (see _tools_source). Process-global index, lazily loaded.
_TOOL_RETRIEVAL_TOPK = int(os.environ.get("HERMES_TOOL_RETRIEVAL_TOPK", "12"))
_TOOL_INDEX_SINGLETON = None


def _tool_index():
    global _TOOL_INDEX_SINGLETON
    if _TOOL_INDEX_SINGLETON is None:
        from hermes.runtime.semantic_tool_index import SemanticToolIndex  # noqa: PLC0415

        _TOOL_INDEX_SINGLETON = SemanticToolIndex()
    return _TOOL_INDEX_SINGLETON


def _sd_notify(message: str) -> None:
    """Envía notificación al socket de systemd si NOTIFY_SOCKET está configurado."""
    notify_socket = os.environ.get("NOTIFY_SOCKET")
    if not notify_socket:
        return
    import socket  # noqa: PLC0415

    if notify_socket.startswith("@"):
        notify_socket = "\0" + notify_socket[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(notify_socket)
            sock.sendall(message.encode())
    except OSError as exc:
        logger.warning("sd_notify failed: %s", exc)


def _resolve_operator_uid() -> int:
    """Resolve the POSIX UID of the graphical operator (hermes-user session).

    Priority (CTRL-P1-7):
      1. HERMES_OPERATOR_UID env var — explicit override for non-standard installs.
      2. pwd lookup of "hermes-user" — the autologin session user in Agents OS Edition.
      3. os.getuid() — fallback for tests / dev containers that lack hermes-user.

    The daemon itself runs as "hermes" (uid 880 in production).  Using os.getuid()
    as fallback-only (not primary) prevents the daemon from accidentally authorizing
    its own UID as the operator when deployed as a system service.

    Fix (CTRL-P1-7 / Fix-9): uid 0 (root) is never a valid operator.
    Raises RuntimeError if the resolved uid is 0 — root must not be operator.
    """
    env_val = os.environ.get("HERMES_OPERATOR_UID", "").strip()
    if env_val:
        uid = int(env_val)
        _assert_operator_uid_not_root(uid, source="HERMES_OPERATOR_UID")
        return uid
    import pwd  # noqa: PLC0415

    try:
        uid = pwd.getpwnam("hermes-user").pw_uid
        _assert_operator_uid_not_root(uid, source="hermes-user pwd lookup")
        return uid
    except KeyError:
        uid = os.getuid()
        _assert_operator_uid_not_root(uid, source="os.getuid() fallback")
        return uid


def _assert_operator_uid_not_root(uid: int, *, source: str) -> None:
    """Fail-closed guard: root (uid 0) must never be the operator (Fix-9)."""
    if uid == 0:
        raise RuntimeError(
            f"hermes.runtime.operator_uid_is_root: uid=0 resolved via {source}. "
            "root must never be the operator — this is a misconfiguration. "
            "Set HERMES_OPERATOR_UID to the hermes-user uid (non-zero)."
        )


def _ensure_audit_dir(audit_dir: Path) -> None:
    """Crea el directorio de audit con permisos estrictos (0700). Fail-closed."""
    try:
        audit_dir.mkdir(parents=True, exist_ok=True)
        audit_dir.chmod(0o700)
    except PermissionError as exc:
        raise RuntimeError(
            f"hermes.runtime.audit_dir_unavailable: no se puede crear {audit_dir} — "
            f"el audit WORM anchor requiere permisos de escritura. Detalle: {exc}"
        ) from exc


def _resolve_operator_id() -> UUID | None:
    """Lee HERMES_OPERATOR_ID. None si no está — NO inventa operador anónimo (CTRL-13)."""
    raw = os.environ.get("HERMES_OPERATOR_ID", "").strip()
    if not raw:
        logger.info(
            "hermes.runtime.operator_id_absent: "
            "HERMES_OPERATOR_ID no configurado — "
            "operator_id=None (writes bloqueadas fail-closed en broker)"
        )
        return None
    try:
        return UUID(raw)
    except ValueError:
        logger.warning("HERMES_OPERATOR_ID is not a valid UUID — operator_id=None")
        return None


_CONSENTS_SEEDED_SENTINEL = Path(
    os.environ.get("HERMES_CONSENTS_SEEDED_SENTINEL", "/var/lib/hermes/consents-seeded")
)


def _build_consent_manager():
    from hermes.agents_os.application.consent_manager import ConsentManager  # noqa: PLC0415
    from hermes.agents_os.infrastructure.sqlite_consent_repo import (  # noqa: PLC0415
        SQLiteConsentRepository,
    )

    consent_db = Path(
        os.environ.get("HERMES_CONSENT_DB", "/var/lib/hermes/consent.db")
    )
    try:
        consent_db.parent.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        logger.warning(
            "Cannot create consent DB dir %s — using in-memory ConsentManager",
            consent_db.parent,
        )
        return ConsentManager()
    repo = SQLiteConsentRepository(db_path=consent_db)
    manager = ConsentManager(repo=repo)
    _seed_default_consents_once(manager)
    return manager


def _seed_default_consents_once(manager) -> None:
    """Seed all grantable capabilities as PERSISTENT on the very first boot.

    Guard: the sentinel file ``/var/lib/hermes/consents-seeded`` (path
    overridable via ``HERMES_CONSENTS_SEEDED_SENTINEL``) is created after a
    successful seed run.  Subsequent daemon restarts skip seeding entirely —
    any capability the user revoked stays revoked forever.

    Operator resolution mirrors the D-Bus control-plane:
      UUID(int=uid)  where uid = _resolve_operator_uid() (hermes-user, uid 1000).
    """
    if _CONSENTS_SEEDED_SENTINEL.exists():
        return

    from uuid import UUID  # noqa: PLC0415

    try:
        operator_uid = _resolve_operator_uid()
    except RuntimeError as exc:
        logger.warning(
            "hermes.runtime.consent_seed.operator_uid_failed: %s — "
            "skipping default consent seed (agent will start with no consents)",
            exc,
        )
        return

    operator_id = UUID(int=operator_uid)
    tenant_id = _resolve_tenant_id()

    seeded = manager.seed_defaults(
        human_operator_id=operator_id,
        tenant_id=tenant_id,
    )

    try:
        _CONSENTS_SEEDED_SENTINEL.parent.mkdir(parents=True, exist_ok=True)
        _CONSENTS_SEEDED_SENTINEL.touch(mode=0o600)
    except OSError as exc:
        logger.warning(
            "hermes.runtime.consent_seed.sentinel_write_failed: %s — "
            "seed completed but sentinel not written; will re-seed on next boot "
            "(idempotent: existing consents are not overwritten)",
            exc,
        )

    logger.info(
        "hermes.runtime.consent_seed.done: seeded %d capabilities for operator=%s",
        len(seeded),
        operator_id,
    )


def _build_tool_specs(consent_manager, operator_id):
    """Build the static tool set used at startup and as engine fallback."""
    native = _build_native_tool_specs(consent_manager, operator_id)
    composio = _build_composio_tool_specs_sync()
    return native + composio


def _build_native_tool_specs(
    consent_manager,
    operator_id,
    *,
    os_native_dispatcher=None,
) -> tuple:
    """Build OS-native tool specs (static — no network, always present)."""
    from hermes.shell_server.os_native_skills.tool_specs import (  # noqa: PLC0415
        build_os_native_tool_specs,
    )

    return build_os_native_tool_specs(
        consent_manager=consent_manager,
        human_operator_id=operator_id,
        os_native_dispatcher=os_native_dispatcher,
    )


def _build_os_native_dispatcher(
    *,
    consent_manager,
    operator_id,
    tenant_id,
) -> "OsNativeDispatcher | None":
    """Build OsNativeDispatcher with computer-use dependencies wired.

    The broker dependency is NOT injected here (circular dependency).
    Caller MUST call dispatcher.wire_computer_use_broker(broker) after the
    broker is built (step 3 of the two-step wiring described in that method).

    Model/api_key/base_url are read from resolve_model_config (same source
    as the engine). If no model is configured yet, model="" and the
    dispatcher fails gracefully when begin_computer_use is invoked
    (returns ok=False with a clear error — same UX as engine-degraded mode).

    Fail-soft: returns None if the dispatcher cannot be constructed.
    The broker then operates without os_native support (fail-closed per
    CTRL-P2-1: executor='os_native' proposals return REJECTED_BY_POLICY).
    """
    try:
        from hermes.capabilities.infrastructure.os_native_dispatcher import (  # noqa: PLC0415
            OsNativeDispatcher,
        )
        from hermes.runtime.active_provider import ActiveProviderService  # noqa: PLC0415

        model_cfg = ActiveProviderService(db_path=_DB_PATH).resolve()
        model = model_cfg.model if model_cfg is not None else ""
        api_key = model_cfg.api_key if model_cfg is not None else None
        base_url = model_cfg.base_url if model_cfg is not None else None

        dispatcher = OsNativeDispatcher(
            computer_use_consent_manager=consent_manager,
            # broker injected post-construction via wire_computer_use_broker()
            computer_use_broker=None,
            computer_use_operator_id=operator_id,
            computer_use_tenant_id=tenant_id,
            computer_use_model=model or "",
            computer_use_api_key=api_key,
            computer_use_base_url=base_url,
        )
        logger.info(
            "hermes.runtime.os_native_dispatcher.ready: "
            "model_configured=%s computer_use_ready=%s",
            bool(model),
            bool(model),  # broker wired post-construction; model is the binding constraint
        )
        return dispatcher
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "hermes.runtime.os_native_dispatcher.init_failed: %s — "
            "executor='os_native' proposals will be REJECTED_BY_POLICY",
            exc,
        )
        return None


def _build_composio_registry():
    """Construct the live-reloading ComposioToolsRegistry (fail-soft)."""
    try:
        from hermes.runtime.composio_tools_registry import (  # noqa: PLC0415
            ComposioToolsRegistry,
        )

        return ComposioToolsRegistry(db_path=_DB_PATH)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "hermes.runtime.composio_registry_init_failed: %s — "
            "using _NullComposioRegistry (no dynamic Composio tools)",
            exc,
        )
        return _NullComposioRegistry()


def _build_composio_registry_with_broker(*, broker, consent_context) -> "ComposioToolsRegistry | _NullComposioRegistry":
    """Construct ComposioToolsRegistry with a broker-aware tools_builder (KC-4).

    The tools_builder closure captures broker + consent_context so that every
    TTL-refresh call produces ToolSpecs whose READ handlers route through
    broker.dispatch (consent + audit + kill-switch).  Falls back to
    _NullComposioRegistry on any construction error (same as _build_composio_registry).

    Boot note: build_composio_tool_specs is imported INSIDE the async closure,
    not at construction time.  This defers the Composio SDK import (composio +
    fastapi + docker + paramiko chain, ~450ms cold) to the first TTL-refresh
    call, which happens POST-READY in the background poller.  The daemon
    announces READY=1 before Composio tools are loaded; native tools are always
    available from t=0.
    """
    try:
        from hermes.runtime.composio_tools_registry import (  # noqa: PLC0415
            ComposioToolsRegistry,
        )

        async def _broker_aware_tools_builder(credential) -> tuple:
            # Deferred import: avoids pulling composio+fastapi+docker+paramiko
            # (~450ms) into the pre-READY synchronous path.  This closure is
            # only called on the first TTL-refresh (background, POST-READY).
            from hermes.runtime.composio_tool_specs import build_composio_tool_specs  # noqa: PLC0415
            return await build_composio_tool_specs(
                credential,
                broker=broker,
                consent_context=consent_context,
            )

        return ComposioToolsRegistry(
            db_path=_DB_PATH,
            tools_builder=_broker_aware_tools_builder,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "hermes.runtime.composio_registry_with_broker_init_failed: %s — "
            "using _NullComposioRegistry (no dynamic Composio tools)",
            exc,
        )
        return _NullComposioRegistry()


def _seed_composio_registry_with(registry, specs: tuple) -> None:
    """Seed the registry cache with an already-fetched spec tuple.

    Avoids a second Composio API call on the first run_cycle: the startup
    fetch result is injected directly so get_composio_tools() is a cache hit.
    If the registry is a _NullComposioRegistry (no _cached attr) this is a no-op.
    """
    if not hasattr(registry, "_cached"):
        return
    import time  # noqa: PLC0415

    registry._cached = specs
    registry._cached_at = time.monotonic()
    logger.info(
        "hermes.runtime.composio_registry_seeded",
        extra={"tool_count": len(specs)},
    )


async def _composio_poller(registry) -> None:
    """Realtime: refresca el registro de tools de Composio en background.

    Mantiene el cache fresco para que una integración recién conectada se
    descubra SOLA (sin esperar a que el usuario chatee) y el ciclo de chat lea
    siempre tools frescas sin pagar latencia de red. El intervalo = TTL del
    registry. Fail-soft: cualquier error de fetch lo absorbe get_composio_tools()
    conservando el último cache bueno; el poller nunca tumba el daemon.
    """
    # Intervalo = TTL del registry (def. 10s). El registry ya gatea los refetch
    # reales por su TTL, así que el poller solo necesita un suelo mínimo para no
    # hacer busy-loop si el TTL fuese 0.
    interval = max(0.01, float(getattr(registry, "_ttl_s", 10.0)))
    logger.info(
        "hermes.runtime.composio_poller_started", extra={"interval_s": interval}
    )
    while True:
        await asyncio.sleep(interval)
        try:
            await registry.get_composio_tools()
        except Exception as exc:  # noqa: BLE001
            logger.debug("hermes.runtime.composio_poller_tick_failed: %s", exc)


def _build_mcp_server_manager():
    """Construct an empty McpServerManager (fail-soft).

    In P1 no MCP servers are pre-configured; the manager starts empty and
    yields zero ToolSpecs. MCP server catalog/connect UX is P3.
    Fail-soft: any construction error returns None (daemon runs without MCP).
    """
    try:
        from hermes.mcp.application.mcp_server_manager import McpServerManager  # noqa: PLC0415
        from hermes.mcp.infrastructure.stdio_mcp_client import StdioMcpClient  # noqa: PLC0415

        def _client_factory(transport) -> StdioMcpClient:
            # Presupuestos REALES de primer arranque (la caché npm/uv se vacía
            # en cada boot de VM snapshot — siempre es "primera descarga"):
            #   uvx git+…  : clone + build del repo entero (60-300s).
            #   npx/uvx    : descarga del paquete + deps en red de VM (30-120s).
            #   node/python: binario local, arranca al instante.
            argv = list(transport.argv) if transport.argv else []
            runner = argv[0].rsplit("/", 1)[-1] if argv else ""
            is_git_backed = runner == "uvx" and any(
                a.startswith("git+") for a in argv
            )
            if is_git_backed:
                timeout = 300.0
            elif runner in ("npx", "uvx"):
                timeout = 120.0
            else:
                timeout = 30.0
            return StdioMcpClient(transport=transport, timeout_sec=timeout)

        manager = McpServerManager(client_factory=_client_factory)
        logger.info("hermes.runtime.mcp_server_manager_ready (0 servers connected at startup)")
        return manager
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "hermes.runtime.mcp_server_manager_init_failed: %s — MCP tools unavailable",
            exc,
        )
        return None


def _build_mcp_capability_registry(*, inner_registry, mcp_server_manager):
    """Wrap inner_registry with McpCapabilityRegistry (fail-soft).

    If mcp_server_manager is None or construction fails, returns inner_registry
    unchanged (no MCP resolution, but broker still works for non-MCP tools).
    """
    if mcp_server_manager is None:
        return inner_registry
    try:
        from hermes.mcp.application.mcp_capability_registry import McpCapabilityRegistry  # noqa: PLC0415
        return McpCapabilityRegistry(
            static_registry=inner_registry,
            server_manager=mcp_server_manager,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "hermes.runtime.mcp_capability_registry_init_failed: %s — "
            "using inner registry (no MCP resolution)",
            exc,
        )
        return inner_registry


def _build_mcp_surface_adapter(mcp_server_manager):
    """Build McpSurfaceAdapter for the broker (fail-soft).

    Returns None when mcp_server_manager is None or construction fails.
    The broker returns REJECTED_BY_POLICY for executor="mcp" proposals when
    mcp_adapter is None (fail-closed per Constitución IV).
    """
    if mcp_server_manager is None:
        return None
    try:
        from hermes.mcp.infrastructure.mcp_surface_adapter import McpSurfaceAdapter  # noqa: PLC0415
        return McpSurfaceAdapter(server_manager=mcp_server_manager)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "hermes.runtime.mcp_surface_adapter_init_failed: %s — "
            "MCP tool execution unavailable (fail-closed)",
            exc,
        )
        return None


def _build_composio_tool_specs_sync() -> tuple:
    """DEPRECATED: broker-less Composio spec builder. NOT called from _run.

    This function is retained for backward-compat test calls only
    (test_runtime_entrypoint.py::test_build_tool_specs_returns_tuple via
    _build_tool_specs). It always returns () since build_composio_tool_specs
    now requires a broker and broker-less spec construction raises.

    Production path: _build_composio_registry_with_broker creates a
    broker-aware registry; the first run_cycle fetches specs via TTL cache.
    DO NOT add new callers of this function.
    """
    logger.debug(
        "hermes.composio_tools._build_composio_tool_specs_sync called — "
        "broker-less construction deprecated; returning empty tuple"
    )
    return ()


def _build_engine(tool_specs):
    """Alias público para compatibilidad con tests existentes."""
    return _build_reasoning_engine(tool_specs)


def _build_reasoning_engine(
    tool_specs,
    *,
    tools_source=None,
    agent_registry=None,
    broker=None,
    consent_context=None,
    tenant_id=None,
    capability_consent_ref=None,
    composio_connection_repo=None,
    capability_binding_repo=None,
    access_scope_repo=None,
    cerebro_browser_manager=None,
    jailed_browser_manager=None,
):
    # El cerebro del SO es Hermes (motor Nous de NousResearch). NO hay otra
    # opción: el motor Nous es el ÚNICO. El path litellm legacy se ELIMINÓ para
    # que no haya ambigüedad — el provider lo resuelve hermes_cli (los providers
    # NATIVOS de Hermes), no un dialecto paralelo. (HERMES_ENGINE se ignora;
    # siempre Nous.)
    return _build_nous_engine(
        broker=broker,
        consent_context=consent_context,
        tenant_id=tenant_id,
        tools_source=tools_source,
        capability_consent_ref=capability_consent_ref,
        agent_registry=agent_registry,
        composio_connection_repo=composio_connection_repo,
        capability_binding_repo=capability_binding_repo,
        access_scope_repo=access_scope_repo,
        cerebro_browser_manager=cerebro_browser_manager,
        jailed_browser_manager=jailed_browser_manager,
    )


def _build_nous_engine(
    *,
    broker=None,
    consent_context=None,
    tenant_id=None,
    tools_source=None,
    capability_consent_ref=None,
    agent_registry=None,
    composio_connection_repo=None,
    capability_binding_repo=None,
    access_scope_repo=None,
    cerebro_browser_manager=None,
    jailed_browser_manager=None,
):
    """Construye NousReasoningEngine (hermes-agent NousResearch, F1/F2/F3).

    F2: broker + consent_context + tenant_id se inyectan para que
    GovernedAIAgent._tool_gate enrute WRITEs al broker (CTRL-1..14).
    Sin broker, todas las WRITEs quedan bloqueadas fail-closed dentro
    del gate (GovernedAIAgent._dispatch_write_proposal lo garantiza).

    F3: tools_source se inyecta para que NousReasoningEngine resuelve
    los ToolSpecs externos (Composio + MCP) per-cycle y los registre
    en el Nous tools.registry. Sin tools_source, 0 tools externas
    (logged LOUD al primer ciclo).

    B4: agent_registry + composio_connection_repo + capability_binding_repo
    habilitan el filtrado runtime de tools por agente. Sin repos, fail-open.

    Dual-browser: CerebroBrowserManager is wired when HERMES_CEREBRO_BROWSER=1
    (default on node with a Wayland display). When absent (CI, no display) the
    Cerebro falls back to headless (same as workers). Fail-soft: the engine is
    always returned regardless of browser availability.

    Fail-closed: si hermes-agent no está instalado lanza NousAgentNotInstalledError
    con instrucciones claras. No usa _build_reasoning_engine como fallback
    (el operador eligió explícitamente HERMES_ENGINE=nous).
    """
    from hermes.agents.domain.agent import default_agent  # noqa: PLC0415
    from hermes.runtime.nous_engine import NousReasoningEngine  # noqa: PLC0415
    from hermes.runtime.active_provider import ActiveProviderService  # noqa: PLC0415

    persona = default_agent().to_persona()

    # Per-cycle: el provider ACTIVO de Hermes (tabla providers + SecretsVault,
    # configurado en onboarding/Settings). El runtime es Nous PERO usa los
    # providers de Hermes — no env vars. Mismo source que LiteLLMReasoningEngine.
    _active_provider_svc = ActiveProviderService(db_path=_DB_PATH)

    def _nous_model_source():
        return _active_provider_svc.resolve()

    # Per-agent provider binding (Fase 3c): resolves a specific provider by alias.
    # Built lazily at engine construction using the same vault as the global source.
    # Fail-soft: if the vault or repo are unavailable, the callable returns None
    # and the engine falls back to the global active provider.
    _model_config_for_alias = _build_model_config_for_alias(_DB_PATH)

    initial_model = _nous_model_source()
    if initial_model is None:
        logger.warning(
            "hermes.runtime.nous_model_not_configured — engine degradado hasta "
            "configurar provider en onboarding/Settings."
        )
    else:
        logger.info("hermes.runtime.nous_model_resolved: %s", initial_model.model)

    logger.info(
        "hermes.runtime.engine_kind=nous broker_wired=%s tools_source_wired=%s",
        broker is not None,
        tools_source is not None,
    )
    if tools_source is None:
        logger.warning(
            "hermes.runtime.nous_engine_no_tools_source: HERMES_ENGINE=nous but "
            "tools_source=None — 0 external tools (Composio/MCP) will be available. "
            "This is a wiring gap."
        )

    return NousReasoningEngine(
        persona=persona,
        model_config_source=_nous_model_source,
        model_config_for_alias=_model_config_for_alias,
        broker=broker,
        consent_context=consent_context,
        tenant_id=tenant_id,
        tools_source=tools_source,
        capability_consent_ref=capability_consent_ref,
        agent_registry=agent_registry,
        composio_connection_repo=composio_connection_repo,
        capability_binding_repo=capability_binding_repo,
        access_scope_repo=access_scope_repo,
        cerebro_browser_manager=cerebro_browser_manager,
        jailed_browser_manager=jailed_browser_manager,
    )


def _build_model_config_for_alias(db_path):
    """Build a callable(alias) -> ModelConfig|None for per-agent provider resolution.

    Fase 3c: construye el resolvedor por-alias usando el mismo vault y repo que
    el path global. Fail-soft: si las dependencias no están disponibles (CI
    headless, vault ausente), devuelve None y el engine usa el provider global.
    """
    try:
        from hermes.shell_server.providers.repo import SQLiteProviderRepository  # noqa: PLC0415
        from hermes.shell_server.security.secrets import SecretsVault  # noqa: PLC0415
        from hermes.providers.infrastructure.vault_provider_resolver import (  # noqa: PLC0415
            VaultProviderResolver,
        )
        from hermes.runtime.model_config import ModelConfig  # noqa: PLC0415
        from hermes.shell_server.providers.domain import provider_model_string  # noqa: PLC0415
    except Exception:  # noqa: BLE001 — optional deps not installed in CI
        logger.debug("hermes.runtime.per_agent_provider.unavailable")
        return None

    try:
        repo = SQLiteProviderRepository(db_path=db_path, vault=SecretsVault())
        resolver = VaultProviderResolver(repo=repo)
    except Exception:  # noqa: BLE001 — vault or DB init failure
        logger.warning("hermes.runtime.per_agent_provider.init_failed", exc_info=True)
        return None

    def _resolve_by_alias(alias: str) -> "ModelConfig | None":
        resolved = resolver.resolve_by_alias(alias)
        if resolved is None:
            return None
        provider = resolved.provider
        model = provider_model_string(provider, provider.default_model)
        return ModelConfig.from_provider(
            model=model,
            api_key=resolved.api_key,
            base_url=resolved.base_url,
        )

    return _resolve_by_alias


def _build_cerebro_browser_manager():
    """Instantiate CerebroBrowserManager when HERMES_CEREBRO_BROWSER=1.

    Fail-soft: returns None on any import or instantiation error so the engine
    continues without headed browser (headless fallback for all cycles).

    Guard: HERMES_CEREBRO_BROWSER env var (default "1" on node; "0" in CI).
    Set to "0" in CI unit test environments or when no Wayland display is
    available.
    """
    enabled = os.environ.get("HERMES_CEREBRO_BROWSER", "1")
    if enabled.strip() in ("0", "false", "no"):
        logger.debug(
            "hermes.runtime.cerebro_browser.disabled "
            "(HERMES_CEREBRO_BROWSER=%s)",
            enabled,
        )
        return None

    try:
        from hermes.runtime.cerebro_browser_manager import (  # noqa: PLC0415
            CerebroBrowserManager,
        )
        manager = CerebroBrowserManager()
        logger.info("hermes.runtime.cerebro_browser.manager_ready")
        return manager
    except Exception as exc:  # noqa: BLE001 — fail-soft: headed browser optional
        logger.warning(
            "hermes.runtime.cerebro_browser.init_failed: %s — "
            "Cerebro will use headless browser fallback",
            exc,
        )
        return None


def _build_jailed_browser_manager():
    """Instantiate JailedBrowserManager when HERMES_BROWSER_JAIL=1 (default).

    Fail-soft: returns None on any import or instantiation error so the daemon
    continues (browse capability degrades; the seatbelt will hard-fail any
    unconfined spawn attempt — see cycle_cdp_context.install_jail_block_local_session).

    Guard: HERMES_BROWSER_JAIL env var — defaults to "1" (jail active).
    Set to "0" in CI or desktop form-factor (where cerebro_browser_manager applies).
    """
    enabled = os.environ.get("HERMES_BROWSER_JAIL", "1")
    if enabled.strip() in ("0", "false", "no"):
        logger.debug(
            "hermes.runtime.jailed_browser.disabled (HERMES_BROWSER_JAIL=%s)",
            enabled,
        )
        return None

    try:
        from hermes.runtime.jailed_browser_manager import (  # noqa: PLC0415
            JailedBrowserManager,
        )
        manager = JailedBrowserManager()
        logger.info("hermes.runtime.jailed_browser.manager_ready")
        return manager
    except Exception as exc:  # noqa: BLE001 — fail-soft: jailed browser optional
        logger.warning(
            "hermes.runtime.jailed_browser.init_failed: %s — "
            "browse will be blocked by the seatbelt (no unconfined fallback)",
            exc,
        )
        return None


def _build_audit_components(db_path: Path):
    """Construye AuditHashChainSigner + SqliteAuditRepository.

    Clave de firma: misma prioridad que _load_signing_key_or_fail:
      1. HERMES_AUDIT_KEY (env, producción con LUKS/TPM2).
      2. SecretsVault.derive_subkey("audit-chain") (master.key, per-install, estable).
    Si ninguna fuente está disponible, retorna (None, None) — el caller decide
    si abortar o arrancar degradado (solo en tests).
    """
    from hermes.agents_os.infrastructure.sqlite_audit_repository import (  # noqa: PLC0415
        SqliteAuditRepository,
    )
    from hermes.agents_os.application.audit_hash_chain import AuditHashChainSigner  # noqa: PLC0415

    try:
        signing_key = _load_signing_key_or_fail()
    except RuntimeError as exc:
        logger.error(
            "hermes.runtime.audit_seal_absent: %s — "
            "audit chain no disponible (ni HERMES_AUDIT_KEY ni master.key). "
            "Configura el secreto LUKS/TPM2 o verifica /var/lib/hermes/master.key.",
            exc,
        )
        return None, None

    audit_repo = SqliteAuditRepository(db_path=db_path)
    firmer = AuditHashChainSigner(signing_key=signing_key)
    return firmer, audit_repo




def _build_composio_surface_adapter(db_path: Path):
    """Construye ComposioSurfaceAdapter para KC-4 (fail-soft).

    Inyectado en el CapabilityBroker como composio_adapter=. Cuando está
    presente, el broker ejecuta acciones READ de Composio a través de él
    (consent + audit + kill-switch). Sin él, las proposals executor="composio"
    devuelven REJECTED_BY_POLICY (fail-closed, correcto).

    Fail-soft: si no hay credenciales o el módulo no está disponible, devuelve
    None y el broker opera sin Composio (mismas garantías de seguridad, solo
    sin acceso a las tools de Composio).
    """
    try:
        # Import-check only (fail-soft if the module is unavailable). The actual
        # credential is resolved per-call by the lazy adapter below.
        from hermes.capabilities.infrastructure.composio_surface_adapter import (  # noqa: PLC0415,F401
            ComposioSurfaceAdapter,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "hermes.runtime.composio_surface_adapter.init_failed: %s — "
            "KC-4: Composio READ proposals will be REJECTED_BY_POLICY",
            exc,
        )
        return None

    # LAZY adapter: resolve the Composio credential on EACH call instead of once at
    # boot. Without this, a key set in the UI after the daemon started never takes
    # effect (the adapter was built None at boot) — every Composio tool call fails
    # "composio_adapter no configurado" until a restart. The lazy wrapper picks up a
    # newly-set key on the next call, and still fail-closes (same REJECTED_BY_POLICY)
    # when no key is present.
    return _LazyComposioSurfaceAdapter(db_path)


class _LazyComposioSurfaceAdapter:
    """composio_adapter that re-resolves the credential per call (see builder)."""

    def __init__(self, db_path: "Path") -> None:
        self._db_path = db_path
        self._cache: tuple[str, Any] | None = None  # (api_key, ComposioSurfaceAdapter)

    def _resolve(self):
        from hermes.runtime.composio_config_source import load_composio_credential  # noqa: PLC0415
        from hermes.capabilities.infrastructure.composio_surface_adapter import (  # noqa: PLC0415
            ComposioSurfaceAdapter,
        )
        cred = load_composio_credential(self._db_path)
        if cred is None:
            self._cache = None
            return None
        if self._cache is None or self._cache[0] != cred.api_key:
            self._cache = (
                cred.api_key,
                ComposioSurfaceAdapter(api_key=cred.api_key, entity_id=cred.entity_id),
            )
            logger.info(
                "hermes.runtime.composio_surface_adapter.ready entity_id=%s (lazy)",
                cred.entity_id,
            )
        return self._cache[1]

    async def replay(self, action):
        adapter = self._resolve()
        if adapter is None:
            from hermes.agents_os.domain.ports.surface_adapter_port import (  # noqa: PLC0415
                ReplayOutcome,
                ReplayStatus,
            )
            return ReplayOutcome(
                action_id=action.action_id,
                status=ReplayStatus.REJECTED_BY_POLICY,
                error="composio_adapter no configurado — fail-closed (KC-4)",
            )
        return await adapter.replay(action)

    async def capture(self, *args, **kwargs):
        adapter = self._resolve()
        if adapter is None:
            raise RuntimeError("composio_adapter no configurado — fail-closed (KC-4)")
        return await adapter.capture(*args, **kwargs)


def _build_skill_store_adapter(db_path: Path):
    """Construye SkillStoreAdapter para SurfaceKind.SKILL_STORE (F4 wiring).

    Fail-soft: si NativeKeyStoreAdapter no puede arrancar (master.key ausente
    en CI / dev sin keygen), el adapter no se registra. Las proposals skill_manage
    serán rechazadas con SurfaceAdapterNotFound en esos entornos. En producción
    (bootc con hermes-keygen) siempre está disponible.

    El skill_store_root por defecto es $HERMES_HOME/skills — EXACTAMENTE donde
    list_skills_native()/_list_native_skills_primary leen y donde el agente
    auto-descubre las skills para ejecutarlas. Forzar /var/lib/hermes/skills
    (el bug anterior) hacía que skill_manage escribiera en una carpeta que la
    vista Habilidades nunca lee → "todo ok" pero la skill no aparecía.
    Sobreponible via env HERMES_SKILL_STORE_ROOT para tests/deploys alternativos.
    """
    try:
        from hermes.capabilities.infrastructure.skill_store_adapter import SkillStoreAdapter  # noqa: PLC0415
        from hermes.shell_server.skills.native_keystore_adapter import NativeKeyStoreAdapter  # noqa: PLC0415
        from hermes.training.application.skill_signer import SigningKeyError  # noqa: PLC0415

        kms = NativeKeyStoreAdapter()
        # Canonical store = $HERMES_HOME/skills (the read/exec path). Only an
        # explicit HERMES_SKILL_STORE_ROOT overrides it.
        _hermes_home = os.environ.get("HERMES_HOME") or "/var/lib/hermes/hermes-home"
        skill_store_root = Path(
            os.environ.get("HERMES_SKILL_STORE_ROOT") or (Path(_hermes_home) / "skills")
        )
        adapter = SkillStoreAdapter(
            kms=kms,
            db_path=db_path,
            skill_store_root=skill_store_root,
        )
        logger.info(
            "hermes.runtime.skill_store_adapter.ready skill_store_root=%s",
            str(skill_store_root),
        )
        return adapter
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "hermes.runtime.skill_store_adapter.init_failed: %s — "
            "skill_manage proposals will be rejected (SurfaceAdapterNotFound). "
            "Ensure hermes-keygen.service has completed before the runtime.",
            exc,
        )
        return None


def _build_memory_surface_adapter():
    """Construye MemorySurfaceAdapter para SurfaceKind.MEMORY (F4 memory wiring).

    No external dependencies — always succeeds. The memory root defaults to
    /var/lib/hermes/memory, overridable via HERMES_MEMORY_ROOT.
    """
    from hermes.memory.infrastructure.memory_surface_adapter import MemorySurfaceAdapter  # noqa: PLC0415

    memory_root = Path(
        os.environ.get("HERMES_MEMORY_ROOT", "/var/lib/hermes/memory")
    )
    adapter = MemorySurfaceAdapter(memory_root=memory_root)
    logger.info(
        "hermes.runtime.memory_surface_adapter.ready memory_root=%s",
        str(memory_root),
    )
    return adapter


def _build_delegation_surface_adapter(db_path: Path):
    """Construye DelegationSurfaceAdapter para SurfaceKind.PEER_DELEGATION
    (FASE 3 A2A cross-human). Fail-soft: None si el association_store no es
    resoluble (p.ej. master.key ausente) — delegate_to_colleague queda
    indisponible (SurfaceAdapterNotFound) en vez de romper el arranque.
    """
    try:
        from hermes.agents_os.infrastructure.delegation_surface_adapter import (  # noqa: PLC0415
            DelegationSurfaceAdapter,
        )
        from hermes.instance.association_store import SQLiteAssociationStore  # noqa: PLC0415
        from hermes.shell_server.security.secrets import SecretsVault  # noqa: PLC0415

        association_store = SQLiteAssociationStore(db_path=db_path, vault=SecretsVault())
        adapter = DelegationSurfaceAdapter(
            association_store=association_store, db_path=db_path,
        )
        logger.info("hermes.runtime.delegation_surface_adapter.ready")
        return adapter
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "hermes.runtime.delegation_surface_adapter.init_failed: %s — "
            "delegate_to_colleague will be unavailable (SurfaceAdapterNotFound)",
            exc,
        )
        return None


def _build_real_broker(
    *,
    db_path: Path,
    consent_manager,
    firmer,
    audit_repo,
    agent_state,
    browser_adapter=None,
    mcp_server_manager=None,
    os_native_dispatcher=None,
    install_executor=None,
):
    """Construye el CapabilityBroker REAL con todas sus dependencias (B1).

    El broker es el único choke-point entre el agente y el SO. Debe recibir:
    - CapabilityRegistry: tabla declarativa de bindings de seguridad.
    - ConsentManager real (ya construido por _build_consent_manager).
    - SqliteApprovalGate con HitlApprovalMinter + AuditHashChainSigner.
    - SurfaceAdapterDispatcher con adapters reales por SurfaceKind.
    - IntentLog DURABLE (db_path != None) para idempotencia cross-restart.
    - WormFileAnchor para anclaje externo del audit chain.
    - agent_state para el kill-switch (Paso 0 de dispatch).
    """
    from hermes.capabilities.application.capability_broker import CapabilityBroker  # noqa: PLC0415
    from hermes.capabilities.application.capability_registry import CapabilityRegistry  # noqa: PLC0415
    from hermes.capabilities.application.hitl_approval_minter import HitlApprovalMinter  # noqa: PLC0415
    from hermes.capabilities.application.intent_log import IntentLog  # noqa: PLC0415
    from hermes.capabilities.infrastructure.sqlite_approval_gate import SqliteApprovalGate  # noqa: PLC0415
    from hermes.capabilities.infrastructure.surface_adapter_dispatcher import SurfaceAdapterDispatcher  # noqa: PLC0415
    from hermes.capabilities.infrastructure.tsa_external_anchor import (  # noqa: PLC0415
        CompositeExternalAnchor,
        TsaExternalAnchor,
        WormFileAnchor,
    )
    from hermes.agents_os.domain.surface_kind import SurfaceKind  # noqa: PLC0415
    from hermes.agents_os.infrastructure.filesystem_surface_adapter import FilesystemSurfaceAdapter  # noqa: PLC0415
    from hermes.agents_os.infrastructure.terminal_surface_adapter import TerminalSurfaceAdapter  # noqa: PLC0415
    from hermes.agents_os.infrastructure.api_call_surface_adapter import ApiCallSurfaceAdapter  # noqa: PLC0415
    from hermes.agents_os.infrastructure.libreoffice_uno_surface_adapter import LibreOfficeUnoSurfaceAdapter  # noqa: PLC0415
    from hermes.agents_os.infrastructure.app_launch_surface_adapter import AppLaunchSurfaceAdapter  # noqa: PLC0415

    signing_key = _load_signing_key_or_fail()
    minter = HitlApprovalMinter(signing_key=signing_key)

    # Inject the MFA tier verifier so EVERY approve surface (web + D-Bus) is MFA-gated
    # inside the gate — closes the D-Bus MFA-skip side-door (red-team 2026-06-19).
    from hermes.shell_server.security.mfa_tool_tier import MfaToolTierVerifier  # noqa: PLC0415

    approval_gate = SqliteApprovalGate(
        db_path=db_path,
        minter=minter,
        signer=firmer,
        audit_repo=audit_repo,
        mfa_verifier=MfaToolTierVerifier(),
    )

    # Surface adapters reales (path/host allowlists configurables via env).
    # API_CALL solo se registra si hay hosts en la allowlist (fail-closed del adapter).
    fs_allowlist = _resolve_fs_allowlist()
    api_hosts = _resolve_api_allowlist()
    # workspace escribible del agente: primer prefijo del allowlist de FS (área
    # acotada donde el terminal confinado puede escribir; fuera de ahí el scope
    # systemd-run es ProtectSystem=strict → read-only).
    agent_workspace = fs_allowlist[0] if fs_allowlist else None
    # Security Center reviewer for terminal installs (pip/npm/curl|sh/git-clone):
    # closes the side-door around the official install gate. None if the package is
    # absent → terminal installs fall back to the egress jail + broker HITL.
    _install_reviewer = None
    try:
        from hermes.security_center.application.composition import (  # noqa: PLC0415
            build_default_scan_service,
        )
        from hermes.agents_os.infrastructure.security_center_install_reviewer import (  # noqa: PLC0415
            SecurityCenterInstallReviewer,
        )

        _scan_svc = build_default_scan_service()
        if _scan_svc is not None:
            _install_reviewer = SecurityCenterInstallReviewer(_scan_svc)
    except Exception:  # noqa: BLE001 — review is additive; never block boot
        logger.warning("hermes.runtime.terminal_install_reviewer_unavailable", exc_info=True)
    adapters: dict = {
        SurfaceKind.FILESYSTEM: FilesystemSurfaceAdapter(allowed_prefixes=fs_allowlist),
        SurfaceKind.TERMINAL: TerminalSurfaceAdapter(
            workspace=agent_workspace, install_reviewer=_install_reviewer
        ),
    }
    if api_hosts:
        adapters[SurfaceKind.API_CALL] = ApiCallSurfaceAdapter(allowed_hosts=api_hosts)
    if browser_adapter is not None:
        adapters[SurfaceKind.BROWSER] = browser_adapter

    # T060 — LibreOfficeUnoSurfaceAdapter: adapter DESKTOP_APP preferido para LO.
    # Se registra SIEMPRE bajo DESKTOP_APP (import lazy; si UNO no disponible,
    # replay() devuelve EXECUTED_FAILED con mensaje diagnóstico, fail-closed).
    # El broker llega aquí SOLO via dispatcher.replay() tras kill-switch/consent/HITL.
    # allowed_prefixes=fs_allowlist: open_document/write/save confinados al MISMO
    # allowlist de FS (anti prompt-injection; sin esto el allowlist queda permisivo).
    adapters[SurfaceKind.DESKTOP_APP] = LibreOfficeUnoSurfaceAdapter(allowed_prefixes=fs_allowlist)

    # APP_LAUNCH — bridge daemon→compositor para lanzar apps VISIBLES nativas.
    # El daemon (hermes, sin display) nunca puede lanzar apps directamente.
    # Este adapter emite AppLaunchRequested(cmd) al system bus; el compositor
    # (lumenso-shell / hermes-user) ejecuta sysManager.launchNativeApp(cmd).
    # launch_emitter=None hasta que DbusRuntimeAdapter.start() inyecte el
    # emitter real (mismo patrón que _scan_signal_emitter).
    #
    # Form factor TERMINAL (TUI): no hay compositor ni apps visibles. NO se
    # registra el adapter → la capa de tool-specs no anuncia activate_app/
    # navigate_app/click_app_element/type_in_app (advertise ⟺ executable). El
    # Cerebro trabaja headless (navegador como herramienta, no como pantalla).
    app_launch_adapter = AppLaunchSurfaceAdapter(launch_emitter=None)
    if os.environ.get("HERMES_FORMFACTOR", "").strip().lower() != "terminal":
        adapters[SurfaceKind.APP_LAUNCH] = app_launch_adapter

    # F4 — SkillStoreAdapter: única superficie que escribe SKILL.md firmados.
    # Se registra SIEMPRE (no es condicional): toda propuesta skill_manage aprobada
    # por HITL debe aterrizar aquí. Fail-closed: sin este adapter, skill_manage
    # proposals levantarían SurfaceAdapterNotFound (fail-closed por diseño).
    skill_store_adapter = _build_skill_store_adapter(db_path)
    if skill_store_adapter is not None:
        adapters[SurfaceKind.SKILL_STORE] = skill_store_adapter

    # F4 — MemorySurfaceAdapter: agent memory writes (tenant-confined, PII-gated).
    # LOW + auto_executable in CapabilityRegistry: no HITL required.
    # Registered unconditionally — no external dependencies.
    adapters[SurfaceKind.MEMORY] = _build_memory_surface_adapter()

    # FASE 3 (A2A cross-human) — DelegationSurfaceAdapter: delegate_to_colleague's
    # execution (POST /v1/outbox). Registered unconditionally — a non-paired
    # (Community Edition) instance's association_store.is_associated() is
    # False, so replay() fails honestly ("instance_not_associated") instead of
    # SurfaceAdapterNotFound; no separate CE/EE branch needed here.
    delegation_adapter = _build_delegation_surface_adapter(db_path)
    if delegation_adapter is not None:
        adapters[SurfaceKind.PEER_DELEGATION] = delegation_adapter

    dispatcher = SurfaceAdapterDispatcher(adapters=adapters)

    # CompositeExternalAnchor:
    #   - WormFileAnchor: append-only local file, fast local truncation detection.
    #   - TsaExternalAnchor: RFC-3161 freeTSA.org, cryptographic non-repudiation.
    # Both layers fail-open: anchor failures do NOT block audit appends.
    tsa_token_dir = Path(
        os.environ.get("HERMES_TSA_TOKEN_DIR", "/var/lib/hermes/tsa_tokens")
    )
    # Fix: anchor en /var/lib/hermes/audit/ (persistente, 0700/0600).
    # tempfile.gettempdir() con PrivateTmp=yes en el unit → se borra en reboot
    # → no detecta truncado cross-restart (AUD-2 persistencia obligatoria).
    anchor_dir = Path(os.environ.get("HERMES_AUDIT_ANCHOR_DIR", "/var/lib/hermes/audit"))
    _ensure_audit_dir(anchor_dir)
    anchor_path = anchor_dir / "anchor.log"
    worm = WormFileAnchor(anchor_path=anchor_path)
    tsa = TsaExternalAnchor(token_dir=tsa_token_dir)
    anchor = CompositeExternalAnchor(worm=worm, tsa=tsa)
    # V-6: wire the anchor into the audit repo so EVERY signed entry is anchored
    # (WORM + RFC-3161 TSA). Without this, _try_anchor was a silent no-op and a
    # daemon-RCE could rewrite the hash chain undetected. The repo is built earlier
    # (boot order), so we attach the anchor here, once it exists.
    if audit_repo is not None and hasattr(audit_repo, "set_external_anchor"):
        audit_repo.set_external_anchor(anchor)

    # IntentLog DURABLE — db_path real para sobrevivir reinicios (I2/CTRL-11).
    intent_log = IntentLog(db_path=str(db_path))

    # KC-4: wrap static registry with ComposioCapabilityRegistry so that
    # Composio READ slugs (e.g. "gmail_get_email") resolve to LOW/auto_executable
    # bindings with executor="composio". Static tools keep priority.
    from hermes.capabilities.application.composio_capability_registry import (  # noqa: PLC0415
        ComposioCapabilityRegistry,
    )
    static_reg = CapabilityRegistry()
    composio_reg = ComposioCapabilityRegistry(static_registry=static_reg)

    # 013-P1: wrap with McpCapabilityRegistry so that mcp__<slug>__<tool>
    # qualified names resolve to LOW/HIGH bindings with executor="mcp".
    # Composio + static registries keep priority (checked first by the inner chain).
    registry = _build_mcp_capability_registry(
        inner_registry=composio_reg,
        mcp_server_manager=mcp_server_manager,
    )

    # KC-4: ComposioSurfaceAdapter — executes Composio READ actions inside the
    # broker gate (consent + audit + kill-switch). Built only when credentials
    # are available; without it the broker returns REJECTED_BY_POLICY for
    # executor="composio" proposals (fail-closed, no bypass).
    composio_adapter = _build_composio_surface_adapter(db_path)

    # 013-P1: McpSurfaceAdapter — executes MCP tool calls inside the broker gate.
    # Built only when mcp_server_manager is available; without it the broker
    # returns REJECTED_BY_POLICY for executor="mcp" proposals (fail-closed).
    mcp_adapter = _build_mcp_surface_adapter(mcp_server_manager)

    broker = CapabilityBroker(
        registry=registry,
        consent_manager=consent_manager,
        approval_gate=approval_gate,
        dispatcher=dispatcher,
        signer=firmer,
        audit_repo=audit_repo,
        intent_log=intent_log,
        anchor=anchor,
        agent_state=agent_state,
        composio_adapter=composio_adapter,
        mcp_adapter=mcp_adapter,
        os_native_dispatcher=os_native_dispatcher,
        install_executor=install_executor,
        # Default DENY+CONSENT (modelo del dueño): el agente solo hace lo concedido;
        # para lo no concedido / HIGH PIDE permiso (consent + HITL) y el dueño decide.
        # Full-autónomo NO es el default de fábrica (V-1 crítico): debe activarse por
        # acción explícita del operador, no por env silenciosa. Aun activado, el
        # taint-forced-HITL (anti-inyección) y HIGH NUNCA se eximen (ver broker).
        autonomous_default=os.environ.get("HERMES_AUTONOMOUS_DEFAULT", "0") != "0",
    )
    return broker, intent_log, approval_gate, app_launch_adapter, install_executor, skill_store_adapter


def _load_signing_key_or_fail() -> bytes:
    """Carga la signing key desde master.key via HKDF (estable, per-install).

    Prioridad:
      1. HERMES_AUDIT_KEY (hex) — inyectado por systemd credentials (producción).
      2. SecretsVault.derive_subkey("audit-chain") — derivada del master.key
         instalado en /var/lib/hermes/master.key (per-install, estable).

    En tests: monkeypatch HERMES_AUDIT_KEY o HERMES_MASTER_KEY_PATH.
    Fail-closed: si ninguna fuente produce una clave, lanza RuntimeError.
    NUNCA usa secrets.token_bytes() — la cadena no sería verificable cross-restart.
    """
    from hermes.runtime.audit_signing_key import load_signing_key, MissingAuditSeal  # noqa: PLC0415

    try:
        return load_signing_key()
    except MissingAuditSeal:
        pass

    # Fallback: deriva desde master.key via HKDF (per-install, estable).
    try:
        from hermes.shell_server.security.secrets import SecretsVault  # noqa: PLC0415
        vault = SecretsVault()
        key = vault.derive_subkey(label="audit-chain")
        logger.info(
            "hermes.runtime.audit_key_from_master_key — "
            "HERMES_AUDIT_KEY ausente; clave derivada de master.key (HKDF, per-install)"
        )
        return key
    except RuntimeError as exc:
        raise RuntimeError(
            "hermes.runtime.audit_key_unavailable: ni HERMES_AUDIT_KEY ni "
            f"master.key están disponibles — audit fail-closed. Detalle: {exc}"
        ) from exc


def _build_platform_repos(db_path: Path):
    """Construye SqlitePlatformModelRegistry + SqliteCapabilityBindingRepo (F010).

    Fail-soft: devuelve (None, None) si los módulos no están disponibles.
    El daemon sigue arrancando — las features de gobernanza de plataformas
    no estarán disponibles hasta que los módulos estén instalados.
    """
    try:
        from hermes.platforms.infrastructure.sqlite_platform_model_registry import (  # noqa: PLC0415
            SqlitePlatformModelRegistry,
        )
        from hermes.platforms.infrastructure.sqlite_capability_binding_repo import (  # noqa: PLC0415
            SqliteCapabilityBindingRepo,
        )
        return (
            SqlitePlatformModelRegistry(db_path=db_path),
            SqliteCapabilityBindingRepo(db_path=db_path),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "hermes.runtime.platform_repos_unavailable: %s — "
            "gobernanza de plataformas no disponible",
            exc,
        )
        return None, None


def _resolve_fs_allowlist() -> list[str]:
    """Lee HERMES_FS_ALLOWLIST (CSV de prefijos) o devuelve defaults seguros."""
    raw = os.environ.get("HERMES_FS_ALLOWLIST", "").strip()
    if raw:
        return [p.strip() for p in raw.split(",") if p.strip()]
    return ["/var/lib/hermes", "/tmp"]


def _resolve_api_allowlist() -> list[str]:
    """Lee HERMES_API_ALLOWLIST (CSV de hosts) o devuelve lista vacía (fail-closed)."""
    raw = os.environ.get("HERMES_API_ALLOWLIST", "").strip()
    if not raw:
        return []
    return [h.strip() for h in raw.split(",") if h.strip()]


def _ensure_state_db_secure() -> None:
    """Pre-crea shell-state.db owner-only (0600) ANTES de que nadie la abra.

    La BD guarda los providers del daemon (keys cifradas), conversaciones y config.
    SQLite la crearía 0644 — legible (y vía /proc/<pid>/fd hasta ESCRIBIBLE) por el
    exec sandbox del agente, que corre como un usuario DISTINTO y menos confiable
    (hermes-sandbox). El daemon es el primer proceso que la toca, así que crearla
    0600 aquí hace que SQLite abra un fichero ya owner-only. (Hallazgo red-team ALTO
    2026-06-19: el agente truncó la BD por /proc/fd; tmpfiles `z` la re-asserta en
    boots posteriores.)
    """
    db_path = os.environ.get("HERMES_SHELL_DB", "/var/lib/hermes/shell-state.db")
    try:
        os.close(os.open(db_path, os.O_CREAT | os.O_RDWR, 0o600))
        os.chmod(db_path, 0o600)
    except OSError as exc:  # noqa: BLE001 — best-effort hardening, never block boot
        logger.warning("hermes.runtime.state_db_secure_failed", extra={"error": str(exc)})


async def _run(*, systemd_notify: bool) -> None:
    import time as _time  # noqa: PLC0415
    _t_start = _time.perf_counter()

    from hermes.logging_setup import configure_structured_logging  # noqa: PLC0415

    configure_structured_logging(service="hermes-runtime", version="0.1.0")
    logger.info(
        "hermes.runtime.starting",
        extra={"boot_step": "logging_configured", "elapsed_ms": round((_time.perf_counter() - _t_start) * 1000, 1)},
    )

    # SECURITY (red-team HIGH 2026-06-19): own the DB file 0600 before any opener,
    # so the agent's hermes-sandbox exec can't read/write it via /proc/<daemon>/fd.
    _ensure_state_db_secure()

    # P0-2: autoconfinar el daemon con Landlock AQUÍ (tras configurar el logging,
    # para que el self-test quede registrado en el journal). El ruleset RUNTIME es
    # amplio (cubre todo lo que el daemon usa) → no rompe; deniega /boot /home /opt…
    _apply_runtime_landlock()

    operator_id = _resolve_operator_id()
    consent_manager = _build_consent_manager()

    # Native OS tools: built once at startup (static — always present).
    # Composio tools: live-reloaded per-cycle via ComposioToolsRegistry (TTL cache).
    # SECURITY: the broker is not yet built at this point; building Composio specs
    # here would produce specs whose READ handlers are fail-closed (no broker →
    # no ungated API calls). To avoid seeding the registry with broker-less specs,
    # we skip the startup Composio fetch entirely. The broker-aware registry will
    # fetch on the first get_composio_tools() call (within the first TTL window).
    # The ~10s startup latency for Composio tools is acceptable.
    # OsNativeDispatcher: built before broker (broker is wired post-construction
    # via wire_computer_use_broker to break the circular dependency).
    # Model config is read now; if none is configured the dispatcher is still
    # constructed — begin_computer_use will fail gracefully at call time.
    os_native_dispatcher = _build_os_native_dispatcher(
        consent_manager=consent_manager,
        operator_id=operator_id,
        tenant_id=_resolve_tenant_id(),
    )

    native_tool_specs = _build_native_tool_specs(
        consent_manager,
        operator_id,
        os_native_dispatcher=os_native_dispatcher,
    )
    tool_specs = native_tool_specs

    # Registro de agentes (roster multi-agente) propiedad del daemon. UNA instancia
    # compartida: el engine resuelve la persona por ciclo desde aquí, y el wiring
    # D-Bus expone la gobernanza (List/Create/.../SetActive) sobre el mismo registro.
    from hermes.agents.infrastructure.sqlite_agent_registry import (  # noqa: PLC0415
        SqliteAgentRegistry,
    )

    agent_registry = SqliteAgentRegistry(db_path=_DB_PATH)

    # Componentes del loop
    from hermes.tasks.infrastructure.sqlite_work_queue import SqliteWorkQueue  # noqa: PLC0415
    from hermes.tasks.infrastructure.sqlite_agent_state import SqliteAgentState  # noqa: PLC0415
    from hermes.tasks.application.agent_loop_orchestrator import AgentLoopOrchestrator  # noqa: PLC0415
    from hermes.capabilities.domain.ports import ConsentContext  # noqa: PLC0415

    db_path = Path(os.environ.get("HERMES_SHELL_DB", "/var/lib/hermes/shell-state.db"))
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        logger.warning("Cannot create DB dir %s", db_path.parent)

    queue = SqliteWorkQueue(db_path=db_path)
    state = SqliteAgentState(db_path=db_path)
    logger.info(
        "hermes.runtime.boot_step.sqlite_infra_ready",
        extra={"elapsed_ms": round((_time.perf_counter() - _t_start) * 1000, 1)},
    )

    # Phase 2a: BrowserAdmissionGuard — built AFTER native tool wiring so
    # MemAvailable reflects post-native-tool state. fail-soft: if construction
    # fails, the daemon continues without the guard (no browser cap).
    browser_guard = _build_browser_admission_guard()

    # The legacy BrowserSurfaceAdapter (a DUPLICATE browser-as-capability surface)
    # is removed: the agent browses via hermes-agent's NATIVE browser tools (gated by
    # NousRisk -> CapabilityBroker), and live teaching uses the CDP screencast live-view.
    # SurfaceKind.BROWSER stays unregistered — the broker already handles None (1062).
    browser_adapter = None

    firmer, audit_repo = _build_audit_components(db_path)
    logger.info(
        "hermes.runtime.boot_step.broker_deps_ready",
        extra={"elapsed_ms": round((_time.perf_counter() - _t_start) * 1000, 1)},
    )

    # 013-P1: McpServerManager — empty at startup (no servers connected in P1).
    # Yields zero ToolSpecs until MCP servers are connected (P3 catalog UX).
    # Fail-soft: daemon runs without MCP if construction fails.
    mcp_server_manager = _build_mcp_server_manager()

    # B1: CapabilityBroker REAL — no más stub. agent_state inyectado para
    # el kill-switch (Paso 0 de dispatch / CTRL-12).
    # Built BEFORE the engine so broker can be injected into NousReasoningEngine
    # (F2: GovernedAIAgent._tool_gate requires broker for WRITE gating).
    #
    # DbusInstallExecutor two-step construction (mirrors AppLaunchSurfaceAdapter):
    # 1. Built here with wiring=None; injected into the broker at build time.
    # 2. set_wiring(wiring) is called after the D-Bus wiring is built so that
    #    the live wiring functions are reachable.  Until then the executor
    #    is fail-closed (wiring=None → REJECTED_BY_POLICY in the broker).
    _install_executor_owner_uid = _resolve_operator_uid()
    try:
        from hermes.capabilities.infrastructure.dbus_install_executor import (  # noqa: PLC0415
            DbusInstallExecutor,
        )
        install_executor = DbusInstallExecutor(
            wiring=None,
            owner_uid=_install_executor_owner_uid,
        )
    except Exception as _ie_exc:  # noqa: BLE001
        logger.warning(
            "hermes.runtime.install_executor_unavailable: %s — install tools fail-closed",
            _ie_exc,
        )
        install_executor = None

    broker, intent_log, approval_gate, app_launch_adapter, _install_executor_ref, _skill_store_adapter_ref = _build_real_broker(
        db_path=db_path,
        consent_manager=consent_manager,
        firmer=firmer,
        audit_repo=audit_repo,
        agent_state=state,
        browser_adapter=browser_adapter,
        mcp_server_manager=mcp_server_manager,
        os_native_dispatcher=os_native_dispatcher,
        install_executor=install_executor,
    )
    logger.info(
        "hermes.runtime.boot_step.broker_ready",
        extra={"elapsed_ms": round((_time.perf_counter() - _t_start) * 1000, 1)},
    )

    # Step 3 of two-step wiring: inject the broker into the dispatcher so that
    # the computer-use loop can dispatch mouse/keyboard actions through the broker.
    # This resolves the OsNativeDispatcher ↔ CapabilityBroker circular dependency.
    if os_native_dispatcher is not None:
        os_native_dispatcher.wire_computer_use_broker(broker)

    # Enterprise Fase 2 Phase 1: per-agent access scope repo (SAME shell-state.db).
    # Built once here and reused by BOTH the security hook (native-tool floor)
    # and the Nous engine (CEO/Cerebro scopable bypass) — single source of truth.
    # Fail-soft: repo construction failure disables both (fail-open, unchanged
    # from before this feature existed).
    _access_scope_repo = None
    try:
        from hermes.capabilities.infrastructure.sqlite_agent_access_scope_repo import (  # noqa: PLC0415
            SqliteAgentAccessScopeRepo,
        )
        _access_scope_repo = SqliteAgentAccessScopeRepo(db_path=db_path)
    except Exception as _scope_repo_exc:  # noqa: BLE001
        logger.warning(
            "hermes.runtime.access_scope_repo_unavailable: %s — "
            "per-agent native-tool floor disabled (fail-open)",
            _scope_repo_exc,
        )

    # Register native security hooks on hermes-agent's global PluginManager.
    # pre_tool_call: kill-switch + access-scope floor + hardline floor +
    # command/code guards + denylist.
    # post_tool_call: signed audit entry for every tool execution (allow or deny).
    # Must run AFTER broker + signer + audit_repo are fully constructed and
    # BEFORE the engine is built (GovernedAIAgent is built per-cycle in run_cycle).
    # The engine_loop reference is the running asyncio event loop for this daemon.
    # Fail-soft: if hermes_cli.plugins is absent, register_security_hooks() logs
    # a warning and returns without crashing the boot sequence.
    if firmer is not None and audit_repo is not None:
        from hermes.runtime.security_hook import register_security_hooks  # noqa: PLC0415
        register_security_hooks(
            agent_state=state,
            engine_loop=asyncio.get_event_loop(),
            broker=broker,
            signer=firmer,
            audit_repo=audit_repo,
            access_scope_repo=_access_scope_repo,
            tenant_id=str(_resolve_tenant_id()),
        )
    else:
        logger.warning(
            "hermes.runtime.security_hook.skipped: signer or audit_repo absent — "
            "pre/post_tool_call hooks NOT registered (audit chain unavailable)"
        )

    consent_context = ConsentContext(
        tenant_id=_resolve_tenant_id(),
        operator_id=operator_id,
    )

    # Camino A: registrar tools MCP en el registry GLOBAL de Nous al conectar
    # (broker + consent del daemon). El agente del chat las ve sin depender del
    # path per-ciclo (run_cycle._resolve). Fail-soft.
    if mcp_server_manager is not None:
        try:
            from hermes.runtime.nous_engine import register_mcp_tools_in_nous_registry  # noqa: PLC0415
            mcp_server_manager._on_connect = (
                lambda _srv, _loop: register_mcp_tools_in_nous_registry(
                    _srv, broker, consent_context, _loop
                )
            )
        except Exception as _mcpw_exc:  # noqa: BLE001
            logger.warning("hermes.runtime.mcp_global_register_wiring_failed: %s", _mcpw_exc)

    # Build Composio registry WITH broker so every TTL-refresh produces
    # ToolSpecs whose READ handlers route through broker.dispatch (consent +
    # audit + kill-switch). The registry starts with an empty cache; the first
    # get_composio_tools() call (in the first run_cycle or poller tick) fetches
    # broker-aware specs. No broker-less startup seeding.
    composio_registry = _build_composio_registry_with_broker(
        broker=broker, consent_context=consent_context
    )

    # spec 014 inc. 3: build capability ToolSpecs for the DESKTOP_APP, FILESYSTEM
    # write, TERMINAL, BROWSER, API_CALL, SYSTEM_SETTINGS, and PACKAGE_MANAGER
    # tools.  These exist in the CapabilityRegistry (broker enforces policy on them)
    # but had NO ToolSpec objects — so they never appeared in the LLM's function
    # schema.  Built ONCE after the broker is ready (broker is captured in the
    # READ handlers' closures).  Static per daemon boot: risk is fixed server-side.
    #
    # CTRL-13 fix (spec 014 inc. 3): build_capability_tool_specs now returns a
    # (specs, consent_ref) pair. consent_ref is a mutable single-element list
    # shared by all READ handlers. The engine updates consent_ref[0] per-cycle
    # with the real per-task operator_id from item.payload["enqueued_by"]
    # (server-side, CWE-862 safe). This propagates the operator_id to READ
    # dispatches without rebuilding the specs.
    from hermes.runtime.capability_tool_specs import build_capability_tool_specs  # noqa: PLC0415
    capability_specs, _capability_consent_ref = build_capability_tool_specs(
        broker=broker,
        consent_context=consent_context,
        # Solo anunciamos tools de surface_adapter cuyo adapter está registrado en
        # ESTE proceso (advertise ⟺ executable): evita api_call sin allowlist o
        # tools visibles (APP_LAUNCH) en la variante terminal sin compositor.
        registered_surface_kinds=broker.registered_surface_kinds(),
    )

    async def _tools_source() -> tuple:
        # Blindaje: cada fuente puede devolver None (p.ej. composio con
        # credencial caducada → 401, o build_mcp en un fallo transitorio).
        # `or ()` evita que un None reviente la concatenación y deje al agente
        # SIN NINGUNA tool externa (incluidas las MCP). Fail-soft por fuente.
        composio = (await composio_registry.get_composio_tools()) or ()
        # 013-P1: MCP tool specs — built per-cycle from connected servers.
        # Empty tuple when no servers are connected (correct in P1).
        mcp_specs: tuple = ()
        if mcp_server_manager is not None:
            from hermes.runtime.mcp_tool_specs import build_mcp_tool_specs  # noqa: PLC0415
            mcp_specs = (await build_mcp_tool_specs(
                mcp_server_manager,
                broker=broker,
                consent_context=consent_context,
            )) or ()
        # Publish dynamic (MCP + Composio) tools to the process-scoped registry
        # so the Policies UI snapshot() can include them in the enriched catalog.
        # Fail-soft: a registry failure must never stall the agent cycle.
        try:
            from hermes.capabilities.dynamic_tool_registry import (  # noqa: PLC0415
                DynamicToolEntry,
                get_dynamic_tool_registry,
            )
            dynamic_entries = tuple(
                DynamicToolEntry(name=s.name, origin="composio")
                for s in composio
            ) + tuple(
                DynamicToolEntry(name=s.name, origin="mcp")
                for s in mcp_specs
            )
            get_dynamic_tool_registry().publish(dynamic_entries)
        except Exception as _dyn_exc:  # noqa: BLE001
            logger.debug("hermes.runtime.dynamic_registry_publish_failed: %s", _dyn_exc)
        # Intent-based tool retrieval: a connected integration can expose hundreds of
        # tools (gmail alone ~63). Presenting them all bloats context and trips
        # progressive disclosure, so the model never finds the right one. Instead,
        # embed the turn's user message and keep only the top-K most relevant
        # integration (composio + mcp) tools — the agent sees a handful of RELEVANT
        # tools directly. Fail-soft: no message / embedder unavailable → full set.
        integration = list(composio) + list(mcp_specs)
        try:
            from hermes.runtime.conversation_task_registry import (  # noqa: PLC0415
                get_current_message,
            )
            _msg = get_current_message()
            picked = _tool_index().retrieve(_msg, integration, k=_TOOL_RETRIEVAL_TOPK) if _msg else None
            if picked is not None and len(picked) < len(integration):
                logger.info(
                    "hermes.runtime.tools_source.retrieved %d/%d integration tools by intent",
                    len(picked), len(integration),
                )
                logger.debug(
                    "hermes.runtime.tools_source.retrieved names=%s",
                    [getattr(s, "name", "?") for s in picked],
                )
                integration = picked
        except Exception as _ret_exc:  # noqa: BLE001
            logger.debug("hermes.runtime.tool_retrieval_skipped: %s", _ret_exc)
        # spec 014 inc. 3: capability_specs are static (built once, always
        # present).  Included BEFORE composio + mcp so they appear first in
        # the LLM schema and are not filtered by _resolve_external_specs
        # (their names are NOT in the Nous native catalog).
        return (native_tool_specs or ()) + (capability_specs or ()) + tuple(integration)

    # B4: construir repos de filtrado runtime ANTES del engine.
    # Misma shell-state.db (db_path). Fail-soft: sin repos, el engine corre en modo
    # fail-open (todas las tools disponibles). La MISMA instancia se pasa al engine
    # y al wiring D-Bus para evitar dos fuentes de verdad.
    _composio_connection_repo = None
    _main_capability_binding_repo = None
    try:
        from hermes.platforms.infrastructure.sqlite_agent_composio_connection_repo import (  # noqa: PLC0415
            SqliteAgentComposioConnectionRepo,
        )
        from hermes.platforms.infrastructure.sqlite_capability_binding_repo import (  # noqa: PLC0415
            SqliteCapabilityBindingRepo,
        )
        _composio_connection_repo = SqliteAgentComposioConnectionRepo(db_path=db_path)
        _main_capability_binding_repo = SqliteCapabilityBindingRepo(db_path=db_path)
    except Exception as _repo_exc:  # noqa: BLE001
        logger.warning(
            "hermes.runtime.filter_repos_unavailable: %s — "
            "filtrado runtime B4 desactivado (fail-open)",
            _repo_exc,
        )

    # CerebroBrowserManager: built once here (NOT inside _build_nous_engine) so
    # the same instance can be injected into both the engine AND DbusRuntimeAdapter.
    # DbusRuntimeAdapter.start() injects the compositor launch emitter into it
    # after the D-Bus bus connects, enabling the headed Chromium to be spawned
    # through the compositor (AppLaunchRequested signal → launchNativeApp).
    cerebro_browser_manager = _build_cerebro_browser_manager()

    # JailedBrowserManager: headless Chromium confined in hermes-browser netns.
    # Built here (not inside _build_nous_engine) so the eager start below can
    # run before the engine's first cycle. HERMES_BROWSER_JAIL=1 by default on
    # the terminal form-factor (set in the drop-in); 0 on desktop (cerebro path).
    jailed_browser_manager = _build_jailed_browser_manager()

    # Engine is built AFTER broker so that HERMES_ENGINE=nous receives broker,
    # consent_context, and tenant_id (F2: GovernedAIAgent._tool_gate wiring).
    # litellm engine ignores these kwargs (they are irrelevant for that path).
    # CTRL-13 fix: _capability_consent_ref forwarded so the Nous engine can update
    # per-cycle operator_id in all READ handler closures (spec 014 inc. 3).
    # B4: composio_connection_repo + capability_binding_repo habilitan el filtrado
    # runtime de tools por agente activo (fail-open si no disponibles).
    engine = _build_reasoning_engine(
        tool_specs,
        tools_source=_tools_source,
        agent_registry=agent_registry,
        broker=broker,
        consent_context=consent_context,
        tenant_id=_resolve_tenant_id(),
        capability_consent_ref=_capability_consent_ref,
        composio_connection_repo=_composio_connection_repo,
        capability_binding_repo=_main_capability_binding_repo,
        access_scope_repo=_access_scope_repo,
        cerebro_browser_manager=cerebro_browser_manager,
        jailed_browser_manager=jailed_browser_manager,
    )

    if engine is not None:
        logger.info(
            "hermes.runtime.engine_ready",
            extra={"tool_count": len(tool_specs)},
        )
    else:
        logger.warning(
            "hermes.runtime.engine_degraded: HERMES_MODEL not set or no tool specs. "
            "AgentLoopOrchestrator will mark tasks failed (no_actions). "
            "Set HERMES_MODEL + HERMES_API_KEY to enable full agent loop."
        )

    def _watchdog() -> None:
        _sd_notify("WATCHDOG=1\nSTATUS=hermes-runtime loop active\n")

    # T050/T051: StreamBroker + ChunkSinkAdapter para el stream de chat.
    # Fail-closed si el socket no arranca: el daemon continúa sin streaming
    # (las tareas se procesan, pero los chunks no llegan al socket).
    from hermes.tasks.control_plane.application.stream_broker import StreamBroker  # noqa: PLC0415
    from hermes.tasks.control_plane.infrastructure.chunk_sink import ChunkSinkAdapter  # noqa: PLC0415
    from hermes.tasks.control_plane.infrastructure.unix_stream_socket import UnixStreamSocketServer  # noqa: PLC0415

    stream_broker = StreamBroker()
    chunk_sink = ChunkSinkAdapter(broker=stream_broker)

    # Socket de stream — daemon-owned, permisos 0660 hermes:hermes (chmod en el
    # adapter). El CONSUMIDOR es el operador gráfico (hermes-user), NO el daemon
    # (hermes). SO_PEERCRED debe autorizar el UID del operador, no os.getuid()
    # (= el del propio daemon, que nunca se conecta a su socket). Orden: override
    # explícito HERMES_OPERATOR_UID → hermes-user del SO → UID actual (tests).
    sock_path = os.environ.get("HERMES_TASKS_SOCK", "/run/hermes/tasks.sock")
    # Reuse _resolve_operator_uid() — same priority chain as the D-Bus control
    # plane authorized_uids.  A single source of truth prevents the stream
    # socket and the D-Bus gate from diverging (i.e., authorizing different UIDs).
    authorized_uid = _resolve_operator_uid()
    unix_socket = UnixStreamSocketServer(
        broker=stream_broker,
        authorized_uid=authorized_uid,
        sock_path=sock_path,
    )

    # ControlPlaneService con wake_signal del orchestrator.
    # La inyección de wake_signal ocurre DESPUÉS de construir el orchestrator
    # porque el wake viene del orchestrator mismo (property wake_signal).
    # Bug #2 fix: el orchestrator necesita conversation_repo para persistir la
    # respuesta del asistente en _handle_chat_narrative_reply. Se construye aquí
    # (antes que el orchestrator) usando el mismo _DB_PATH que usa el wiring D-Bus.
    # Best-effort: si falla, el orchestrator arranca sin persistencia del asistente
    # (degradación honesta: el stream llega pero GetConversation no devuelve assistant).
    _orchestrator_conversation_repo = None
    try:
        from hermes.tasks.infrastructure.sqlite_conversation_repo import (  # noqa: PLC0415
            SQLiteConversationRepository as _SQLiteConvRepo,
        )
        _orchestrator_conversation_repo = _SQLiteConvRepo(db_path=_DB_PATH)
    except Exception as _conv_exc:  # noqa: BLE001
        logger.warning(
            "hermes.runtime.orchestrator_conversation_repo_unavailable: %s — "
            "respuesta del asistente no se persistirá en GetConversation",
            _conv_exc,
        )

    # Notification store — written by the orchestrator at task/chat completion.
    # Best-effort: if unavailable (schema migration failure, etc.) the orchestrator
    # runs without notifications (honest degradation — no crash).
    _orchestrator_notification_store = None
    try:
        from hermes.notifications.infrastructure.sqlite_notification_store import (  # noqa: PLC0415
            SqliteNotificationStore as _SqliteNotifStore,
        )
        _orchestrator_notification_store = _SqliteNotifStore(db_path=_DB_PATH)
    except Exception as _notif_exc:  # noqa: BLE001
        logger.warning(
            "hermes.runtime.notification_store_unavailable: %s — "
            "bell notifications will not be persisted",
            _notif_exc,
        )

    # Usage repository — metering (tokens/cost per cycle). Best-effort: if
    # unavailable the orchestrator runs without metering (honest degradation).
    _orchestrator_usage_repo = None
    try:
        from hermes.shell_server.metering.usage_repo import (  # noqa: PLC0415
            SQLiteUsageRepository as _SQLiteUsageRepo,
        )
        _orchestrator_usage_repo = _SQLiteUsageRepo(db_path=_DB_PATH)
    except Exception as _usage_exc:  # noqa: BLE001
        logger.warning(
            "hermes.runtime.usage_repo_unavailable: %s — "
            "cycle metering will not be persisted",
            _usage_exc,
        )

    orchestrator = AgentLoopOrchestrator(
        queue=queue,
        state=state,
        engine=engine if engine is not None else _NoOpEngine(),
        broker=broker,
        consent_context=consent_context,
        notify_watchdog=_watchdog,
        idle_poll_s=_HEALTH_INTERVAL_S,
        pause_poll_s=_HEALTH_INTERVAL_S,
        firmer=firmer,
        audit_repo=audit_repo,
        approval_gate=approval_gate,
        intent_log=intent_log,
        chunk_sink=chunk_sink,
        browser_adapter=browser_adapter,
        conversation_repo=_orchestrator_conversation_repo,
        notification_store=_orchestrator_notification_store,
        usage_repo=_orchestrator_usage_repo,
    )

    # D-Bus adapter — falla silenciosamente si dbus-fast no está disponible
    # en el entorno (p.ej. tests sin bus de sistema). Fail-closed: sin D-Bus
    # el chat no puede encolar, pero el loop autónomo sigue activo.
    dbus_adapter, dbus_task = _start_dbus_adapter_if_available(
        queue=queue,
        state=state,
        broker=broker,
        approval_gate=approval_gate,
        wake_signal=orchestrator.wake_signal,
        tenant_id=_resolve_tenant_id(),
        operator_id=operator_id,
        agent_registry=agent_registry,
        firmer=firmer,
        consent_manager=consent_manager,
        mcp_server_manager=mcp_server_manager,
        app_launch_adapter=app_launch_adapter,
        cerebro_browser_manager=cerebro_browser_manager,
        nous_engine=engine,
        install_executor=_install_executor_ref,
        # Wire the live worker count so GetRuntimeStatus reflects real in-flight work.
        # orchestrator.active_worker_count() reads self._pool._active_count (set once
        # run_forever() starts the pool). Returns 0 before pool is running (correct).
        worker_count_fn=orchestrator.active_worker_count,
        # Notification store: written by orchestrator, read via D-Bus by shell-server.
        notification_store=_orchestrator_notification_store,
        # SkillStoreAdapter — único escritor de SKILL.md firmados (construido en
        # _build_real_broker para que use el mismo db_path que el SurfaceAdapterDispatcher).
        skill_store_adapter=_skill_store_adapter_ref,
        # Fase 2 Phase 3: same singleton the security hook + Nous engine use
        # (built once above, before register_security_hooks).
        access_scope_repo=_access_scope_repo,
    )

    # JailedBrowser eager start: pre-warm the confined headless Chromium so the
    # first browse cycle does not incur the 25s cold-start poll. Runs AFTER the
    # D-Bus adapter is up (so hermes-browser-launcher is available). Best-effort
    # with two retries; on failure we leave BROWSER_CDP_URL unset and the seatbelt
    # (cycle_cdp_context.install_jail_block_local_session) ensures any subsequent
    # browse call hard-fails instead of running unconfined.
    if jailed_browser_manager is not None:
        asyncio.create_task(
            _eager_start_jailed_browser(jailed_browser_manager),
            name="jailed-browser-eager-start",
        )

    # MCP: reconectar al boot los servidores que el operador configuró
    # (persisten en mcp-servers.json; fail-soft por servidor).
    if mcp_server_manager is not None:
        from hermes.agents_os.infrastructure.dbus_runtime_service import (  # noqa: PLC0415
            reconnect_persisted_mcp_servers,
        )
        asyncio.create_task(
            reconnect_persisted_mcp_servers(mcp_server_manager),
            name="mcp-reconnect",
        )

    # P3 — ModelHealthMonitor: detecta caída del LLM local y emite
    # AgentLivenessChanged(alive=True, has_model=False) via D-Bus.
    # El SC-4 (watchdog) ya está cubierto: WorkerPool._watchdog_loop emite
    # WATCHDOG=1 en task paralela, independiente del run_cycle. Un acompletion
    # de 180s NO bloquea el latido porque el watchdog es una corrutina separada
    # que duerme HERMES_WATCHDOG_INTERVAL_S (default 5s) en su propio bucle.
    model_monitor_task = _start_model_health_monitor(dbus_adapter=dbus_adapter)

    # P2 — Trigger sources: SchedulerTimerSource + SystemEventTriggerSource.
    # Fail-closed: con allow-list vacía no disparan nada (default-deny).
    trigger_tasks = _start_trigger_sources(
        queue=queue,
        state=state,
        db_path=db_path,
        tenant_id=_resolve_tenant_id(),
        audit_signer=firmer,
        audit_repo=audit_repo,
    )

    # Wire SIGTERM for clean shutdown
    event_loop = asyncio.get_event_loop()
    event_loop.add_signal_handler(signal.SIGTERM, orchestrator.request_shutdown)
    event_loop.add_signal_handler(signal.SIGTERM, unix_socket.close)
    if browser_guard is not None:
        event_loop.add_signal_handler(signal.SIGTERM, browser_guard.signal_shutdown)

    # Confinement self-check: refuse to start the autonomous loop if kernel
    # confinement gates are absent. Closes the red-team "written but never loaded"
    # gap. Runs after all services are wired so any missing socket/netns is real.
    _assert_confinement_active()

    await orchestrator.bootstrap()

    _ready_elapsed_ms = round((_time.perf_counter() - _t_start) * 1000, 1)
    if systemd_notify:
        _sd_notify("READY=1\nSTATUS=hermes-runtime ready\n")

    logger.info(
        "hermes.runtime.loop_started",
        extra={"time_to_ready_ms": _ready_elapsed_ms},
    )

    # Ejecutar en paralelo: loop principal + socket de stream + D-Bus (si disponible)
    # + ModelHealthMonitor (P3) + trigger sources (P2).
    tasks = [
        asyncio.create_task(orchestrator.run_forever(), name="agent-loop"),
        asyncio.create_task(_serve_unix_socket(unix_socket, sock_path), name="stream-socket"),
    ]
    if dbus_task is not None:
        tasks.append(dbus_task)
    if model_monitor_task is not None:
        tasks.append(model_monitor_task)
    # Realtime: refresca el registro de tools de Composio en BACKGROUND, para que
    # una integración recién conectada se descubra SOLA (sin esperar a un chat) y
    # el ciclo de chat lea siempre tools frescas sin pagar la latencia del refetch.
    if hasattr(composio_registry, "_ttl_s"):
        tasks.append(
            asyncio.create_task(
                _composio_poller(composio_registry), name="composio-poller"
            )
        )
    tasks.extend(trigger_tasks)

    await asyncio.gather(*tasks, return_exceptions=True)
    logger.info("hermes.runtime.loop_stopped")


async def _eager_start_jailed_browser(manager) -> None:
    """Pre-warm the confined headless Chromium at daemon boot.

    Best-effort with two retries (short delay between attempts). On success,
    BROWSER_CDP_URL is set in the process environment so Nous's native precedence
    routes EVERY thread through --cdp (attaches instead of spawning). On failure,
    the environment variable is left unset; the seatbelt installed by
    install_jail_block_local_session() ensures any browse call hard-fails instead
    of running unconfined in the host netns.

    The daemon continues regardless — browse is one capability, not the whole OS.
    """
    from hermes.runtime.jailed_browser_manager import (  # noqa: PLC0415
        JailedBrowserUnavailable,
    )

    _MAX_ATTEMPTS = 2
    _RETRY_DELAY_S = 3.0

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            await manager.ensure_running()
            cdp_url = manager.cdp_url
            if cdp_url:
                os.environ["BROWSER_CDP_URL"] = cdp_url
                logger.info(
                    "hermes.jailed_browser.eager_start_ok cdp_url=%s attempt=%d",
                    cdp_url,
                    attempt,
                )
                return
            logger.warning(
                "hermes.jailed_browser.eager_start_no_url attempt=%d — retrying",
                attempt,
            )
        except JailedBrowserUnavailable as exc:
            logger.error(
                "hermes.jailed_browser.unavailable attempt=%d/%d error=%s — "
                "BROWSER_CDP_URL not set; browse calls will fail-closed",
                attempt,
                _MAX_ATTEMPTS,
                exc,
            )
        if attempt < _MAX_ATTEMPTS:
            await asyncio.sleep(_RETRY_DELAY_S)

    logger.error(
        "hermes.jailed_browser.eager_start_failed after %d attempts — "
        "seatbelt active: unconfined browse blocked",
        _MAX_ATTEMPTS,
    )


def _build_context_snapshot_composer(*, consent_manager, operator_id) -> "object | None":
    """Construye ContextSnapshotComposer con LibAtSpiClient real (fail-soft).

    LibAtSpiClient requiere pyatspi (at-spi2-core), disponible solo en la imagen
    personal-desktop. En entornos headless/CI pyatspi no está instalado → la
    construcción falla con ImportError → devuelve None.

    Degradación honesta: si devuelve None, RequestContextSnapshot devolverá el
    stub JSON 'context_snapshot_not_configured', que la app de overlay muestra
    como "app activa desconocida" (nunca mock, nunca error de arranque).

    CRÍTICO: este fallo NO debe propagar al caller (_start_dbus_adapter_if_available)
    y mucho menos al boot del daemon. Toda excepción se captura aquí.
    """
    try:
        from hermes.agents_os.application.context_snapshot_composer import (  # noqa: PLC0415
            ContextSnapshotComposer,
        )
        from hermes.agents_os.infrastructure.libatspi_client import LibAtSpiClient  # noqa: PLC0415

        atspi_client = LibAtSpiClient()
        composer = ContextSnapshotComposer(
            atspi_client=atspi_client,
            consent_manager=consent_manager,
            screenshot_port=None,  # P1: sin screenshot; P2 añade ScreenshotPort
            operator_id=operator_id,
        )
        logger.info("hermes.runtime.context_snapshot_composer.ready")
        return composer
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "hermes.runtime.context_snapshot_composer.degraded: %s — "
            "RequestContextSnapshot devolverá stub (sin AT-SPI real)",
            exc,
        )
        return None


def _start_dbus_adapter_if_available(
    *,
    queue,
    state,
    broker,
    approval_gate,
    wake_signal,
    tenant_id,
    operator_id,
    agent_registry=None,
    firmer=None,
    consent_manager=None,
    mcp_server_manager=None,
    app_launch_adapter=None,
    cerebro_browser_manager=None,
    nous_engine=None,
    install_executor=None,
    worker_count_fn=None,  # Callable[[], int] | None — live in-flight count
    notification_store=None,  # SqliteNotificationStore | None — bell feature
    skill_store_adapter=None,  # SkillStoreAdapter | None — único escritor firmado
    access_scope_repo=None,  # SqliteAgentAccessScopeRepo (Fase 2 Phase 3)
) -> "tuple[object | None, asyncio.Task | None]":
    """Arranca el adapter D-Bus si dbus-fast está instalado y hay bus de sistema.

    Fail-closed: sin D-Bus el chat no puede encolar. El loop autónomo sigue activo.
    No lanza — devuelve (None, None) si no puede arrancar.

    Devuelve (adapter, task) para que el ModelHealthMonitor pueda llamar a
    emit_liveness_changed sin acoplarse al bus de sistema.

    ControlPlaneService inyecta wake_signal del orchestrator para el commit-then-wake
    ordering de CTRL-P1-12.

    firmer: AuditHashChainSigner ya construido en main() — se pasa a
        DbusRuntimeServiceWiring para que GetAuditChainHead devuelva el head real.
        None → método devuelve integrity="unknown" (degradación honesta).
    consent_manager: ConsentManager para ContextSnapshotComposer (gating screenshot).
        None → screenshot siempre denegado (degradación honesta, no error de boot).
    """
    try:
        from hermes.agents_os.infrastructure.dbus_runtime_service import (  # noqa: PLC0415
            DbusRuntimeServiceWiring,
        )
        from hermes.agents_os.infrastructure.dbus_fast_runtime_adapter import (  # noqa: PLC0415
            DbusRuntimeAdapter,
        )
    except ImportError as exc:
        logger.warning(
            "hermes.runtime.dbus_adapter_unavailable: %s — D-Bus deshabilitado", exc
        )
        return None, None

    # authorized_uids: UID del operador de login (hermes-user en producción).
    # _resolve_operator_uid(): HERMES_OPERATOR_UID env → hermes-user pwd → os.getuid().
    # El daemon corre como "hermes" (uid 880); si cayera en os.getuid() autorizaría
    # su propio UID en vez del del operador — por eso la lookup pwd es la segunda
    # opción, no un fallback a os.getuid() inmediato.
    authorized_uid = _resolve_operator_uid()
    # Authorize the operator (login user) AND the daemon's own service uid
    # (os.getuid() == hermes). The web UI's shell-server runs as the same service
    # user and proxies the operator's mutators (set_active_agent, add_mcp_server,
    # add_provider, …) over D-Bus; those verbs do NOT carry an operator_token
    # (only Enqueue does), so the proxy is authorized by uid. The shell-server is
    # first-party (holds master.key); its compromise is already game-over, so
    # trusting its uid here adds no new attack surface. Human authZ is enforced at
    # the device/HTTP layer of the shell-server.
    authorized_uids = frozenset({authorized_uid, os.getuid()})

    try:
        from hermes.tasks.control_plane.application.control_plane_service import (  # noqa: PLC0415
            ControlPlaneService,
        )
        from hermes.tasks.domain.ports import AgentStatePort as _AgentStatePort  # noqa: PLC0415

        # ControlPlaneService es la application layer que centraliza rate-limit,
        # PII tokenization, audit (CTRL-P1-6 / CTRL-P1-25 / CTRL-P1-4).
        # El Wiring delega enqueue() aquí — la ruta de producción (D-Bus) aplica
        # los MISMOS controles que la ruta testeable (Issue 2).
        cp_service = ControlPlaneService(
            queue=queue,
            agent_state=state,
            authorized_uids=authorized_uids,
            tenant_id=_resolve_tenant_id(),
            wake_signal=wake_signal,
        )
        from hermes.shell_server.skills.skill_governance_service import (  # noqa: PLC0415
            SkillGovernanceService,
        )

        skill_governance = SkillGovernanceService(db_path=_DB_PATH)
        # Feature 010: plataformas + asignación de capacidades.
        # Comparten la misma shell-state.db que el resto del daemon.
        _platform_registry, _capability_binding_repo = _build_platform_repos(_DB_PATH)

        # Confused-deputy remediation (security-hardening):
        # Resolve the shell-server's process uid (hermes system user) so the
        # wiring can require an OperatorToken for any D-Bus call that arrives
        # from that uid instead of from the direct operator (hermes-user).
        # HERMES_SHELL_SERVER_UID allows explicit override in non-standard setups;
        # the default resolves the "hermes" system account uid.
        proxy_uid = _resolve_shell_server_uid()
        operator_token_verifier = _build_operator_token_verifier()

        # GATE 0 / M1: el daemon POSEE los providers (misma shell-state.db que ya
        # usa para resolver el modelo activo). Repo+Vault aquí → verbos D-Bus
        # Providers; el HTTP del shell-server se borra al cerrar M1.
        try:
            from hermes.shell_server.providers.repo import SQLiteProviderRepository  # noqa: PLC0415
            from hermes.shell_server.security.secrets import SecretsVault  # noqa: PLC0415

            _provider_repo = SQLiteProviderRepository(db_path=_DB_PATH, vault=SecretsVault())
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.runtime.provider_repo_unavailable: %s", exc)
            _provider_repo = None

        # GATE 0 / M2 — conversaciones (chat) OS-nativas: el daemon es dueño del
        # store (mismas tablas conversations/messages en shell-state.db). Verbos
        # D-Bus de lectura/borrado; el HTTP del shell-server se borra al cerrar M2.
        try:
            from hermes.tasks.infrastructure.sqlite_conversation_repo import (  # noqa: PLC0415
                SQLiteConversationRepository,
            )

            _conversation_repo = SQLiteConversationRepository(db_path=_DB_PATH)
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.runtime.conversation_repo_unavailable: %s", exc)
            _conversation_repo = None

        # T027/T047 — ContextSnapshotComposer (RequestContextSnapshot) +
        # audit_signer (GetAuditChainHead). Construidos defensivamente:
        # un fallo NUNCA rompe el arranque del daemon; ambos degradan a None
        # y los métodos D-Bus correspondientes devuelven stubs informativos.
        _context_snapshot_composer = _build_context_snapshot_composer(
            consent_manager=consent_manager,
            operator_id=operator_id,
        )
        from hermes.runtime.active_provider import ActiveProviderService  # noqa: PLC0415
        _active_provider_svc = ActiveProviderService(db_path=_DB_PATH)

        # Enterprise license enforcement (Fase 3): the wiring's create_agent /
        # enqueue hard-block on the associate license. Built fail-soft — a failure
        # here NEVER breaks daemon boot; it degrades to None, which the wiring
        # treats as Community Edition (no license restriction). The vault is only
        # needed to reveal the instance secret (not used by the license checks),
        # but the store constructor requires it; SecretsVault() reads master.key.
        try:
            from hermes.instance.association_store import (  # noqa: PLC0415
                SQLiteAssociationStore,
            )
            from hermes.shell_server.security.secrets import (  # noqa: PLC0415
                SecretsVault,
            )

            _association_store = SQLiteAssociationStore(
                db_path=_DB_PATH, vault=SecretsVault()
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes.runtime.association_store_unavailable: %s", exc)
            _association_store = None

        wiring = DbusRuntimeServiceWiring(
            agent_state=state,
            approval_gate=approval_gate,
            authorized_uids=authorized_uids,
            work_queue=queue,
            wake_signal=wake_signal,
            control_plane_service=cp_service,
            agent_registry=agent_registry,
            skill_governance=skill_governance,
            platform_model_registry=_platform_registry,
            capability_binding_repo=_capability_binding_repo,
            access_scope_repo=access_scope_repo,
            provider_repo=_provider_repo,
            conversation_repo=_conversation_repo,
            tenant_id=str(_resolve_tenant_id()),
            proxy_uid=proxy_uid,
            operator_token_verifier=operator_token_verifier,
            context_snapshot_composer=_context_snapshot_composer,
            audit_signer=firmer,
            mcp_server_manager=mcp_server_manager,
            # spec 014 increment 3 — FR-013: el ConsentManager construido al
            # inicio del daemon se inyecta aquí para que GrantConsent/RevokeConsent
            # muten el mismo objeto que el broker usa en assert_active().
            consent_manager=consent_manager,
            active_provider_service=_active_provider_svc,
            # FR-013 consent subject alignment: pin the consent subject to the
            # daemon owner so the UI, seed, and broker all address the same record.
            operator_id=operator_id,
            # Live in-flight worker count: passed from the orchestrator instance
            # in _run() so GetRuntimeStatus returns the real count, not zero.
            worker_count_fn=worker_count_fn,
            # SqliteNotificationStore for the notification bell REST surface.
            notification_store=notification_store,
            # Fase 3 — Enterprise license hard-block (None → Community Edition).
            association_store=_association_store,
            # SkillStoreAdapter — único escritor de SKILL.md firmados. El mismo
            # que se registra en SurfaceAdapterDispatcher; se inyecta aquí para
            # que create_skill_from_text use el escritor nativo (no duplicado).
            skill_store_adapter=skill_store_adapter,
        )

        # R5 Stage C — one-shot migration: push SQL active provider → native config.
        # Must run before the first agent cycle reads resolve_model_config().
        # Fail-soft: any error inside migrate_active_provider_to_native is
        # caught and logged there; it NEVER propagates here.
        wiring.migrate_active_provider_to_native()

        # Step 2 of two-step DbusInstallExecutor construction: inject the live
        # wiring so install/search/connect tools can reach the wiring functions.
        # Fail-soft: if install_executor is None (build failed above), skip.
        if install_executor is not None and hasattr(install_executor, "set_wiring"):
            install_executor.set_wiring(wiring)
    except Exception as exc:
        logger.warning(
            "hermes.runtime.dbus_wiring_failed: %s — D-Bus no disponible", exc
        )
        return None, None

    try:
        adapter = DbusRuntimeAdapter(
            wiring=wiring,
            app_launch_adapter=app_launch_adapter,
            cerebro_browser_manager=cerebro_browser_manager,
            nous_engine=nous_engine,
        )
        task = asyncio.create_task(adapter.start(), name="dbus-adapter")
        return adapter, task
    except Exception as exc:
        logger.warning(
            "hermes.runtime.dbus_adapter_start_failed: %s", exc
        )
        return None, None


def _start_model_health_monitor(
    *,
    dbus_adapter: "object | None",
) -> "asyncio.Task | None":
    """Arranca ModelHealthMonitor como tarea asyncio (P3, SC-3).

    Emite AgentLivenessChanged(alive=True, has_model=<endpoint_healthy>) via
    el D-Bus adapter cuando el endpoint del LLM local cambia de estado.

    Fail-closed: si el monitor no puede iniciarse, el daemon continúa sin él
    (SC-2 garantizado — la salud del modelo NO está en la cadena del boot).
    """
    try:
        from hermes.runtime.model_health_monitor import (  # noqa: PLC0415
            HttpModelClient,
            ModelHealthMonitor,
        )
        from hermes.shell.domain.shell_session import RuntimeLinkState  # noqa: PLC0415
    except ImportError as exc:
        logger.warning(
            "hermes.runtime.model_health_monitor_unavailable: %s", exc
        )
        return None

    # In Lumen the model lives EXTERNAL (cloud, or a user-provided endpoint resolved
    # per-provider). There is no local inference server to poll, so probing a guessed
    # localhost:8000 only spams false "offline". Run the monitor ONLY when an explicit
    # local endpoint is configured. (We must never append paths like /v1/models onto a
    # provider's base_url to "health-check" it — that base_url belongs to the user,
    # verbatim; the OpenAI client adds /chat/completions per protocol and that's all.)
    import os  # noqa: PLC0415

    if not os.environ.get("HERMES_MODEL_BASE_URL"):
        logger.info("hermes.runtime.model_health_monitor_skipped_no_local_endpoint")
        return None

    http_client = HttpModelClient.from_env()

    def _on_state_change(new_state: RuntimeLinkState) -> None:
        if dbus_adapter is None:
            return
        has_model = new_state == RuntimeLinkState.CONNECTED
        try:
            dbus_adapter.emit_liveness_changed(alive=True, has_model=has_model)  # type: ignore[union-attr]
        except Exception as exc:
            logger.warning(
                "hermes.runtime.model_health_monitor.emit_failed: %s", exc
            )

    monitor = ModelHealthMonitor(
        http_client=http_client,
        on_state_change=_on_state_change,
    )
    logger.info("hermes.runtime.model_health_monitor_starting")
    return asyncio.create_task(monitor.run(), name="model-health-monitor")


def _start_trigger_sources(
    *,
    queue,
    state,
    db_path: Path,
    tenant_id: "UUID",
    audit_signer=None,
    audit_repo=None,
) -> "list[asyncio.Task]":
    """Arranca SchedulerTimerSource + SystemEventTriggerSource (P2, US3).

    Fail-closed: con allow-list vacía (defecto), ninguna tarea se encola.
    Si el repo SQLite no está disponible, las fuentes no se añaden al gather
    (error logueado, daemon continúa).

    Las fuentes son corrutinas paralelas del gather — su caída NO detiene
    el agent-loop principal (return_exceptions=True en asyncio.gather).
    """
    tasks: list[asyncio.Task] = []
    try:
        from hermes.tasks.triggers.application.timer_trigger_source import (  # noqa: PLC0415
            SchedulerTimerSource,
        )
        from hermes.tasks.triggers.application.system_event_trigger_source import (  # noqa: PLC0415
            SystemEventTriggerSource,
        )
        from hermes.tasks.triggers.application.trigger_gate import TriggerGate  # noqa: PLC0415
        from hermes.tasks.triggers.infrastructure.sqlite_authorized_trigger_repository import (  # noqa: PLC0415
            SqliteAuthorizedTriggerRepository,
        )
    except ImportError as exc:
        logger.warning("hermes.runtime.trigger_sources_unavailable: %s", exc)
        return tasks

    try:
        import sqlite3  # noqa: PLC0415
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        from hermes.tasks.infrastructure.schema import ensure_tasks_schema  # noqa: PLC0415
        ensure_tasks_schema(conn)
        trigger_repo = SqliteAuthorizedTriggerRepository(conn)
    except Exception as exc:
        logger.warning(
            "hermes.runtime.trigger_repo_init_failed: %s — trigger sources disabled",
            exc,
        )
        return tasks

    gate = TriggerGate(
        trigger_repo=trigger_repo,
        queue=queue,
        agent_state=state,
        tenant_id=tenant_id,
        audit_signer=audit_signer,
        audit_repo=audit_repo,
    )

    timer_source = SchedulerTimerSource(gate=gate, repo=trigger_repo)
    event_source = SystemEventTriggerSource(gate=gate)

    logger.info("hermes.runtime.trigger_sources_starting")
    tasks.append(
        asyncio.create_task(timer_source.run_forever(), name="trigger-timer")
    )
    tasks.append(
        asyncio.create_task(event_source.run_forever(), name="trigger-system-event")
    )
    return tasks


async def _serve_unix_socket(
    unix_socket: "UnixStreamSocketServer", sock_path: str
) -> None:
    """Arranca el servidor de stream. Fail-closed si el path no existe."""
    import os as _os  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    sock_dir = Path(sock_path).parent
    if not sock_dir.exists():
        try:
            sock_dir.mkdir(parents=True, mode=0o750, exist_ok=True)
        except PermissionError:
            logger.warning(
                "hermes.runtime.stream_socket_dir_unavailable: %s — "
                "stream de chunks deshabilitado (chat sin streaming visual).",
                sock_dir,
            )
            return

    try:
        await unix_socket.serve_forever()
    except Exception as exc:
        logger.error(
            "hermes.runtime.stream_socket_error: %s — stream de chunks detenido.", exc
        )


def _build_browser_admission_guard():
    """Construye BrowserAdmissionGuard (Phase 2a). Fail-soft: None si falla.

    Construido DESPUÉS de cablear el engine para que MemAvailable refleje
    la memoria post-carga del modelo LLM.
    """
    try:
        from hermes.execution.application.browser_admission_guard import (  # noqa: PLC0415
            BrowserAdmissionGuard,
            ProcMeminfoReader,
        )
        return BrowserAdmissionGuard(memory_reader=ProcMeminfoReader())
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "hermes.runtime.browser_admission_guard.init_failed: %s — "
            "browser sessions uncapped (no RAM guard)",
            exc,
        )
        return None


def _resolve_shell_server_uid() -> int | None:
    """Resolve the uid of the shell-server process (hermes system account).

    This uid is the confused-deputy proxy: D-Bus calls arriving from it with
    a valid OperatorToken are accepted; calls without a token are denied.

    Priority:
      1. HERMES_SHELL_SERVER_UID env var (explicit override).
      2. pwd lookup of "hermes" — the system service user in Agents OS.
      3. None — disables the proxy path (all non-operator calls denied).

    Returns None only if "hermes" account does not exist AND env is unset.
    In that case the runtime runs in direct-only mode (no proxied calls).
    """
    env_val = os.environ.get("HERMES_SHELL_SERVER_UID", "").strip()
    if env_val:
        try:
            uid = int(env_val)
            logger.info(
                "hermes.runtime.shell_server_uid_from_env",
                extra={"uid": uid},
            )
            return uid
        except ValueError:
            logger.warning(
                "hermes.runtime.shell_server_uid_invalid: "
                "HERMES_SHELL_SERVER_UID=%r is not an integer — "
                "proxy path disabled",
                env_val,
            )
            return None
    import pwd  # noqa: PLC0415

    try:
        uid = pwd.getpwnam("hermes").pw_uid
        logger.info(
            "hermes.runtime.shell_server_uid_from_pwd",
            extra={"uid": uid, "user": "hermes"},
        )
        return uid
    except KeyError:
        logger.info(
            "hermes.runtime.shell_server_uid_not_found: "
            "'hermes' system account absent — proxy path disabled"
        )
        return None


def _build_operator_token_verifier():
    """Build OperatorTokenVerifier from master.key subkey (fail-soft).

    Uses SecretsVault.derive_subkey(label="operator-token") — the same HKDF
    derivation used by the shell-server when minting tokens. Stable per-install:
    both processes share the same master.key, so tokens verify cross-process.

    Fail-soft: if master.key is absent (CI, test environments without keygen),
    returns None. The wiring's _authorize_via_token() will deny any proxy call
    when verifier is None (safe default — no master.key means no valid tokens).
    """
    try:
        from hermes.shell_server.security.secrets import SecretsVault  # noqa: PLC0415
        from hermes.shell_server.security.operator_token import OperatorTokenVerifier  # noqa: PLC0415

        vault = SecretsVault()
        key = vault.derive_subkey(label="operator-token")
        logger.info("hermes.runtime.operator_token_verifier_ready")
        return OperatorTokenVerifier(signing_key=key)
    except RuntimeError as exc:
        logger.warning(
            "hermes.runtime.operator_token_verifier_unavailable: %s — "
            "proxy path requires valid master.key; token verification disabled",
            exc,
        )
        return None


def _resolve_tenant_id() -> "UUID":
    """Lee o genera un tenant_id estable para el agente local."""
    import hashlib  # noqa: PLC0415
    from uuid import UUID  # noqa: PLC0415

    raw = os.environ.get("HERMES_TENANT_ID", "").strip()
    if raw:
        try:
            return UUID(raw)
        except ValueError:
            pass
    # Deriva un UUID determinista del hostname si no está configurado
    hostname = os.uname().nodename if hasattr(os, "uname") else "hermes-local"
    digest = hashlib.sha256(hostname.encode()).digest()
    return UUID(bytes=digest[:16], version=5)


class _NoOpEngine:
    """Motor stub para cuando HERMES_MODEL no está configurado.

    Devuelve proposals vacíos — el loop marca las tareas failed(no_actions).
    """

    async def run_cycle(self, context):
        from hermes.domain.cycle_output import CycleOutput  # noqa: PLC0415
        return CycleOutput()


class _NullComposioRegistry:
    """Stub registry used when ComposioToolsRegistry cannot be constructed.

    Always returns () so the daemon keeps running with native tools only.
    """

    async def get_composio_tools(self) -> tuple:
        return ()


_DEV_MODE_MARKER = Path("/etc/hermes/dev-mode")

# Rule files baked into the image — their presence + non-empty contents confirm
# that hermes-browser-netns.service loaded the restrictive policy, not a blank stub.
_NFT_HOST_RULE_FILE = Path("/etc/nftables/hermes/browser-host.nft")
_NFT_NS_RULE_FILE = Path("/etc/nftables/hermes/browser-ns.nft")


def _assert_confinement_active() -> None:
    """Fail-closed pre-flight: verify kernel confinement ENFORCEMENT before the autonomous loop.

    INVARIANT: every operation performed here must work as User=hermes with an EMPTY
    CapabilityBoundingSet (no CAP_NET_ADMIN, no CAP_SYS_ADMIN, no capabilities at all).
    No `nft`, `ip netns exec`, `ip link`, or any other privileged binary is called.
    Only: file-stat/read under /etc /run /sys (world-readable or hermes-readable),
    /proc scan (unprivileged), and `systemctl is-active`/`systemctl show` (read-only
    D-Bus, no capabilities required).

    Checks ENFORCEMENT, not mere presence (closes red-team "written but never loaded" gap):
      1. Browser netns enforcement — `systemctl is-active hermes-browser-netns.service`
         (active ⟺ nft rules loaded, because its ExecStart lines have no `|| true` on the
         nft calls). Reinforced by: /run/netns/hermes-browser exists (stat, unprivileged) +
         /etc/nftables/hermes/browser-{host,ns}.nft exist and are non-empty (confirms the
         restrictive policy was baked into the image and is the file the service loaded).
      2. Landlock ABI present AND browser jail NOT disabled — ensures the browser process
         will be Landlocked when launched (HERMES_BROWSER_JAIL must be "1" in release).
         Pure Python syscall + env var read — zero capabilities.
      3. Egress proxy enforcement — `systemctl is-active hermes-egress-proxy.service`
         (active ⟺ proxy is running and has not crashed). Reinforced by: socket exists
         at /run/hermes/egress-proxy.sock + /proc cmdline scan for hermes.egress_proxy.
      4. Browser cgroup slice present WITH memory limits set — reads
         /sys/fs/cgroup/agents-os-browser.slice/memory.max (world-readable sysfs).
         Rejects "max" (unlimited = no kernel resource guard).

    Skip policy (non-bypassable in release):
      The check can only be skipped if BOTH conditions hold:
        a. HERMES_CONFINEMENT_CHECK=0 is set.
        b. /etc/hermes/dev-mode exists (root-owned marker, NOT baked into release images).
      A repackager setting the env var on a release image (without the dev-mode marker)
      will hit sys.exit(1). This prevents the "set env var, ship broken confinement" attack.
    """
    if not _is_confinement_check_required():
        return

    failures: list[str] = []

    _check_netns_enforcement(failures)
    _check_landlock_enforcement(failures)
    _check_egress_proxy_enforcement(failures)
    _check_cgroup_limits(failures)

    if failures:
        for reason in failures:
            logger.error("hermes.runtime.confinement_check.FAILED: %s", reason)
        logger.error(
            "hermes.runtime.confinement_ABORT: %d confinement gate(s) not enforcing — "
            "refusing to start autonomous loop (fail-closed). "
            "Ensure hermes-browser-netns.service, hermes-egress-proxy.service, "
            "and agents-os-browser.slice are active and fully configured.",
            len(failures),
        )
        sys.exit(1)

    logger.info(
        "hermes.runtime.confinement_check.PASS: "
        "netns(service=active, rules-baked, netns-exists) + "
        "Landlock(abi-present, jail=1) + "
        "egress-proxy(service=active, socket-exists, process-alive) + "
        "cgroup(limits-set)"
    )


def _is_confinement_check_required() -> bool:
    """Return True when the self-check must run (i.e., cannot be skipped).

    The check is mandatory in release. It can ONLY be skipped when:
      - HERMES_CONFINEMENT_CHECK=0, AND
      - /etc/hermes/dev-mode exists on the filesystem.

    /etc/hermes/dev-mode is root-owned and NOT included in release images (bootc
    Containerfile.base never copies it). A repackager that only sets the env var
    on a release image will still hit the check.
    """
    skip_requested = os.environ.get("HERMES_CONFINEMENT_CHECK", "1") != "1"
    if not skip_requested:
        return True

    if _DEV_MODE_MARKER.exists():
        logger.info(
            "hermes.runtime.confinement_check.skipped: "
            "HERMES_CONFINEMENT_CHECK=0 + %s present — dev mode",
            _DEV_MODE_MARKER,
        )
        return False

    # Env var set but no dev-mode marker: treat as attempted bypass, enforce.
    logger.warning(
        "hermes.runtime.confinement_check.skip_denied: "
        "HERMES_CONFINEMENT_CHECK=0 requested but %s absent — "
        "confinement check is mandatory in release (fail-closed)",
        _DEV_MODE_MARKER,
    )
    return True


def _check_netns_enforcement(failures: list[str]) -> None:
    """Verify browser netns enforcement without any privileged capabilities.

    UNPRIVILEGED strategy:
      hermes-browser-netns.service is fail-closed (its nft ExecStart lines carry
      no `|| true`), so `systemctl is-active` == "active" is logically equivalent
      to "nft rules are loaded".  No `nft`, `ip netns exec`, or `ip link` needed.

    Three sub-checks (all zero-capability):
      a. hermes-browser-netns.service is active (systemctl is-active, read-only D-Bus).
      b. /run/netns/hermes-browser exists (bind-mount created by the service on success).
      c. Baked rule files exist and are non-empty — confirms the image ships the
         restrictive policy, not a placeholder (stat + file read, world-readable /etc).
    """
    _check_systemctl_unit_active("hermes-browser-netns.service", "netns", failures)
    _check_netns_path_exists(failures)
    # NO leemos los .nft de /etc/nftables/hermes/: el dir es root-only y el daemon
    # (User=hermes) recibe PermissionError al statear → crash de boot. Y es
    # REDUNDANTE: `systemctl is-active hermes-browser-netns` ya prueba que las nft
    # cargaron (el servicio es fail-closed sobre el `nft -f`, sin `|| true`).


def _check_systemctl_unit_active(
    unit: str,
    context: str,
    failures: list[str],
) -> None:
    """Assert `systemctl is-active <unit>` returns exactly "active".

    Unprivileged: `systemctl is-active` issues a read-only D-Bus query to
    systemd (PID 1).  No capabilities are required. Timeout 5 s; any exception
    or non-"active" stdout is treated as FAIL (fail-closed).
    """
    import subprocess  # noqa: PLC0415

    try:
        result = subprocess.run(
            ["/usr/bin/systemctl", "is-active", unit],
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        failures.append(
            f"{context}_service_check_error: "
            f"cannot run `systemctl is-active {unit}`: {exc}"
        )
        return

    status = result.stdout.strip()
    if status != "active":
        failures.append(
            f"{context}_service_inactive: "
            f"`systemctl is-active {unit}` returned {status!r} "
            f"(expected 'active') — service did not complete successfully"
        )
        return

    logger.debug(
        "hermes.runtime.confinement_check.%s_service_active unit=%s",
        context,
        unit,
    )


def _check_netns_path_exists(failures: list[str]) -> None:
    """Verify /run/netns/hermes-browser exists (bind-mount created by the service).

    This is a stat-only check — no capabilities required.
    """
    netns_path = Path("/run/netns/hermes-browser")
    if not netns_path.exists():
        failures.append(
            "netns_path_absent: /run/netns/hermes-browser not found — "
            "hermes-browser-netns.service ExecStart did not create the netns bind-mount"
        )
        return
    logger.debug("hermes.runtime.confinement_check.netns_path_exists: ok")


def _check_nft_rule_files_baked(failures: list[str]) -> None:
    """Verify the nftables rule files are baked into the image and non-empty.

    Reads /etc/nftables/hermes/browser-{host,ns}.nft — world-readable (or at
    minimum hermes-readable) /etc files. An absent or empty file means the image
    was built without the restrictive policy; the service would have loaded nothing.
    """
    for rule_file in (_NFT_HOST_RULE_FILE, _NFT_NS_RULE_FILE):
        if not rule_file.exists():
            failures.append(
                f"nft_rule_file_absent: {rule_file} not found — "
                "image was built without the browser egress policy; "
                "hermes-browser-netns.service loaded no rules"
            )
            continue
        try:
            content = rule_file.read_text(encoding="utf-8", errors="replace").strip()
        except OSError as exc:
            failures.append(
                f"nft_rule_file_unreadable: {rule_file}: {exc}"
            )
            continue
        if not content:
            failures.append(
                f"nft_rule_file_empty: {rule_file} exists but is empty — "
                "the egress policy file contains no rules"
            )
            continue
        logger.debug(
            "hermes.runtime.confinement_check.nft_rule_file_ok: %s (%d chars)",
            rule_file,
            len(content),
        )


def _check_landlock_enforcement(failures: list[str]) -> None:
    """Verify Landlock ABI is present AND the browser jail is not disabled.

    The daemon itself is Landlocked by _apply_runtime_landlock (already ran).
    This check ensures the BROWSER process will also be Landlocked:
      - HERMES_BROWSER_JAIL must not be "0" (the default is "1" = jail active).
    """
    try:
        from hermes.security.landlock_loader import _detect_abi  # noqa: PLC0415
        abi = _detect_abi()
        if abi is None:
            failures.append(
                "landlock_abi_absent: kernel Landlock not detected "
                "(requires kernel ≥ 5.13 with CONFIG_SECURITY_LANDLOCK). "
                "Browser filesystem confinement will NOT be enforced."
            )
            return
        logger.debug("hermes.runtime.confinement_check.landlock_abi=%d", abi)
    except Exception as exc:  # noqa: BLE001
        failures.append(f"landlock_abi_check_error: {exc}")
        return

    # browser jail env var: "0" means the browser runs without the jail (CI mode).
    # In release HERMES_BROWSER_JAIL must be "1" (or unset, which defaults to "1").
    if os.environ.get("HERMES_BROWSER_JAIL", "1") == "0":
        failures.append(
            "landlock_jail_disabled: HERMES_BROWSER_JAIL=0 — browser Landlock jail is off. "
            "This is only safe in CI environments with /etc/hermes/dev-mode. "
            "In release, the jail must be active (HERMES_BROWSER_JAIL=1 or unset)."
        )


def _check_egress_proxy_enforcement(failures: list[str]) -> None:
    """Verify the egress proxy is running WITHOUT any privileged capabilities.

    UNPRIVILEGED strategy (three layers, all zero-capability):
      a. `systemctl is-active hermes-egress-proxy.service` — read-only D-Bus query.
         Active ⟺ the proxy process is running and has not crashed/exited.
      b. /run/hermes/egress-proxy.sock exists — confirms the process opened its
         control socket (file-stat, no caps).
      c. /proc cmdline scan for `hermes.egress_proxy` — belt-and-suspenders liveness
         check that does not rely on systemd being reachable. Pure /proc reads, no caps.
    """
    # FATAL: el proxy debe estar active (systemctl is-active = proceso vivo).
    # Es la prueba autoritativa de que el egress está enforzando.
    _check_systemctl_unit_active("hermes-egress-proxy.service", "egress_proxy", failures)

    # NO-FATAL: socket + /proc son refuerzos redundantes con el is-active de arriba.
    # El socket puede ser 0600 de otro usuario y/o no estar listo en el instante
    # del check; un false-negative aquí NO debe brickear el boot (el is-active ya
    # cubre "el proxy corre"). Se loguea como warning para diagnóstico.
    try:
        if not Path("/run/hermes/egress-proxy.sock").exists():
            logger.warning(
                "hermes.runtime.confinement_check.egress_socket_absent "
                "(no-fatal; systemctl is-active es la prueba autoritativa)"
            )
        elif not _egress_proxy_process_alive():
            logger.warning(
                "hermes.runtime.confinement_check.egress_process_not_seen_in_proc (no-fatal)"
            )
    except OSError as exc:
        logger.warning("hermes.runtime.confinement_check.egress_aux_check_skip: %s", exc)


def _egress_proxy_process_alive() -> bool:
    """Return True if a `hermes.egress_proxy` process is currently running.

    Scans /proc/<pid>/cmdline for the module path. Does not require root
    or any external binary. Fail-safe: returns False on any read error.
    """
    proc_root = Path("/proc")
    try:
        pids = [p for p in proc_root.iterdir() if p.name.isdigit()]
    except OSError:
        return False

    for pid_dir in pids:
        try:
            cmdline = (pid_dir / "cmdline").read_bytes().replace(b"\x00", b" ").decode(errors="replace")
            if "hermes.egress_proxy" in cmdline or "hermes/egress_proxy" in cmdline:
                return True
        except OSError:
            continue
    return False


def _check_cgroup_limits(failures: list[str]) -> None:
    """Verify the browser cgroup slice exists AND has a MemoryMax limit set.

    UNPRIVILEGED strategy:
      Primary: read /sys/fs/cgroup/agents-os-browser.slice/memory.max directly.
        sysfs memory.max is world-readable (mode 0444) — no capabilities needed.
      Fallback: if the path is unreadable (unusual but possible with certain
        mount options), query `systemctl show agents-os-browser.slice -p MemoryMax`
        which is a read-only D-Bus query — no capabilities required.

    An unlimited MemoryMax ("max") means no kernel resource guard — a single
    Chromium tab could OOM the entire machine and indirectly crash the daemon.
    """
    # NO-FATAL: el límite de memoria del cgroup es un GUARD DE RECURSOS (anti-OOM),
    # NO la frontera anti-exfil/anti-escape (esa es netns+egress+Landlock, ya
    # verificadas fail-closed arriba). Además el slice de systemd puede crearse
    # lazy (al asignar el primer proceso del browser) → ausente en idle. Por eso
    # un fallo aquí se loguea como warning, NO brickea el boot. `failures` no se
    # toca. (Lista local de diagnóstico para no romper la firma del helper.)
    diag: list[str] = []
    try:
        cgroup_slice = Path("/sys/fs/cgroup/agents-os-browser.slice")
        if not cgroup_slice.exists():
            logger.warning(
                "hermes.runtime.confinement_check.cgroup_slice_absent_at_idle "
                "(no-fatal; el slice puede crearse al lanzar el browser)"
            )
            return
        raw = _read_memory_max_sysfs(cgroup_slice / "memory.max", diag)
        if raw is None:
            logger.warning("hermes.runtime.confinement_check.cgroup_memory_unreadable %s", diag)
        elif raw == "max":
            logger.warning(
                "hermes.runtime.confinement_check.cgroup_memory_unlimited "
                "(no-fatal; considerar MemoryMax en agents-os-browser.slice)"
            )
        else:
            logger.debug("hermes.runtime.confinement_check.cgroup_memory_max=%s", raw)
    except OSError as exc:
        logger.warning("hermes.runtime.confinement_check.cgroup_check_skip: %s", exc)


def _read_memory_max_sysfs(
    memory_max_path: Path,
    failures: list[str],
) -> str | None:
    """Read memory.max from sysfs, falling back to systemctl show on EACCES.

    Returns the stripped value string, or None (with an entry appended to
    failures) if neither path succeeds.
    """
    import subprocess  # noqa: PLC0415

    if memory_max_path.exists():
        try:
            return memory_max_path.read_text(encoding="ascii").strip()
        except PermissionError:
            pass  # fall through to systemctl show fallback
        except OSError as exc:
            failures.append(f"cgroup_memory_max_unreadable: {exc}")
            return None

    # Fallback: systemctl show — unprivileged read-only D-Bus query.
    try:
        result = subprocess.run(
            ["/usr/bin/systemctl", "show", "agents-os-browser.slice", "-p", "MemoryMax"],
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        failures.append(
            f"cgroup_memory_max_fallback_error: "
            f"cannot run `systemctl show agents-os-browser.slice -p MemoryMax`: {exc}"
        )
        return None

    # Output format: "MemoryMax=<value>" or "MemoryMax=18446744073709551615" (=max)
    for line in result.stdout.splitlines():
        if line.startswith("MemoryMax="):
            val = line.split("=", 1)[1].strip()
            # systemd encodes "max" as the uint64 sentinel 18446744073709551615
            if val == "18446744073709551615":
                return "max"
            return val

    failures.append(
        "cgroup_memory_max_absent: agents-os-browser.slice/memory.max not found and "
        "`systemctl show` returned no MemoryMax — "
        "cgroup v2 memory controller may not be enabled"
    )
    return None


def _apply_runtime_landlock() -> None:
    """P0-2: el daemon se AUTOCONFINA con Landlock — defense-in-depth, 2ª capa LSM.

    OS-NATIVO: confinamiento a nivel kernel vía syscalls Landlock, aplicado por el
    propio daemon a su proceso (NADA de backend/HTTP). Igual o superior a NemoHermes
    (fail-closed, no best-effort). No-fatal: si Landlock no está o el ruleset falla,
    el daemon sigue confinado por systemd (ProtectSystem=strict + ProtectHome +
    ReadWritePaths + CapabilityBoundingSet vacío + SystemCallFilter). Se desactiva
    con HERMES_RUNTIME_LANDLOCK=0 (CI/dev sin kernel Landlock).
    """
    if os.environ.get("HERMES_RUNTIME_LANDLOCK", "1") != "1":
        return
    try:
        from hermes.security.landlock_loader import load_and_apply

        rc = load_and_apply("runtime")
        # Self-test funcional: /boot NO está en el ruleset RUNTIME → debe denegar.
        # Sin Landlock /boot es world-readable; con Landlock enforcing → EACCES.
        # Es la prueba de que el confinamiento es REAL, no teatro ("escrito pero
        # no cargado" era el patrón raíz del red-team).
        try:
            os.listdir("/boot")
            enforcing = False
        except PermissionError:
            enforcing = True
        except OSError:
            enforcing = None
        if enforcing is True:
            logger.info("runtime_landlock.applied rc=%d ENFORCING — /boot denied (EACCES) ✓", rc)
        elif enforcing is False:
            logger.warning("runtime_landlock.applied rc=%d NOT_ENFORCING — /boot legible (¿degrade?)", rc)
        else:
            logger.info("runtime_landlock.applied rc=%d (self-test inconcluso)", rc)
    except Exception as exc:  # noqa: BLE001 — jamás debe tumbar el daemon
        logger.warning("runtime_landlock.skipped error=%r (sigue el confinamiento systemd)", exc)


def main() -> int:
    args = sys.argv[1:]
    systemd_notify = "--systemd-notify" in args
    try:
        asyncio.run(_run(systemd_notify=systemd_notify))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
