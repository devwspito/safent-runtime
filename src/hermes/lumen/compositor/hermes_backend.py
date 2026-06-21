"""HermesBackend — puente QObject entre el desktop QML y el daemon Hermes.

Reemplaza el `api.js` HTTP de WhaleOS: el QML llama a Hermes por D-Bus REAL
(org.hermes.Runtime1 sobre el system bus), NO por HTTP. Cubre TODO el contrato
que el daemon expone hoy (Runtime1ServiceInterface): chat/conversaciones,
agentes (profiles), providers, tasks/cola, skills, consents, capability-binding,
plataformas, memoria, cuenta de SO. Lo que el daemon NO expone, NO se inventa
aquí — la app correspondiente muestra un estado vacío honesto.

Patrón async→QML (thread-safe):
  El QML llama `call(requestId, method, argsJson)`. Cuando la llamada D-Bus
  termina, se emite `result(requestId, ok, jsonResult)` en el hilo GUI (conexión
  encolada de Qt). `method` es snake_case; `argsJson` es un objeto JSON con los
  campos que el método necesita (ver _METHODS). El resultado SIEMPRE es JSON
  (string ya-JSON del daemon, o serializado aquí para bool/struct/dict).

Contrato (method snake_case  ←→  método D-Bus PascalCase):
  enqueue, get_conversation, list_conversations, delete_conversation,
  list_agents, get_active_agent, set_active_agent, create_agent, update_agent,
  delete_agent, list_providers, get_active_provider, add_provider,
  update_provider, delete_provider, set_active_provider, test_provider,
  list_skills, promote_skill, deprecate_skill, sign_composio_skill,
  list_recent_tasks, list_configured_tasks, list_pending, list_hitl_pending,
  get_task_status, get_queue_status, pause, resume, approve, reject, grant_consent,
  revoke_consent, list_consents, bind_capability_to_agent,
  unbind_capability_from_agent, set_agent_house_rule, list_agent_capabilities,
  list_platform_models, list_memory, search_memory, stage_account,
  get_audit_chain_head.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading

from PySide6.QtCore import QObject, Signal, Slot

logger = logging.getLogger(__name__)

_DBUS_NAME = "org.hermes.Runtime"
_DBUS_PATH = "/org/hermes/Runtime"
_DBUS_IFACE = "org.hermes.Runtime1"

# Tipos de arg: "s" cadena, "i"/"u" entero, "json" → objeto serializado a JSON.
# Cada entrada: method_snake → (dbus_call_attr, [(arg_key, default, type), ...]).
# El orden de la lista ES el orden posicional de la llamada D-Bus.
_METHODS: dict[str, tuple[str, list[tuple[str, object, str]]]] = {
    # ── Chat / conversaciones ────────────────────────────────────────────
    "enqueue": ("call_enqueue", [
        ("trigger_kind", "chat_message", "s"),
        ("text", "", "s"),
        ("priority", 0, "i"),
        ("dedup_key", "", "s"),
        ("conversation_id", "", "s"),
        ("operator_token", "", "s"),
    ]),
    "get_conversation": ("call_get_conversation", [("conversation_id", "", "s")]),
    "list_conversations": ("call_list_conversations", [("agent_id", "", "s")]),
    "delete_conversation": ("call_delete_conversation", [("conversation_id", "", "s")]),
    # ── Agentes (profiles) ───────────────────────────────────────────────
    "list_agents": ("call_list_agents", []),
    "get_active_agent": ("call_get_active_agent", []),
    "set_active_agent": ("call_set_active_agent", [("agent_id", "", "s")]),
    "create_agent": ("call_create_agent", [("draft_json", {}, "json")]),
    "update_agent": ("call_update_agent", [("agent_id", "", "s"), ("draft_json", {}, "json")]),
    "delete_agent": ("call_delete_agent", [("agent_id", "", "s")]),
    "list_agent_capabilities": ("call_list_agent_capabilities", [("agent_id", "", "s")]),
    "bind_capability_to_agent": ("call_bind_capability_to_agent", [
        ("agent_id", "", "s"), ("capability_kind", "", "s"),
        ("capability_id", "", "s"), ("capability_version", "", "s"),
    ]),
    "unbind_capability_from_agent": ("call_unbind_capability_from_agent", [
        ("agent_id", "", "s"), ("capability_kind", "", "s"), ("capability_id", "", "s"),
    ]),
    "set_agent_house_rule": ("call_set_agent_house_rule", [
        ("agent_id", "", "s"), ("model_id", "", "s"), ("rule_json", {}, "json"),
    ]),
    # ── Providers ────────────────────────────────────────────────────────
    "list_providers": ("call_list_providers", []),
    "get_active_provider": ("call_get_active_provider", []),
    "add_provider": ("call_add_provider", [("draft_json", {}, "json")]),
    "update_provider": ("call_update_provider", [("provider_id", "", "s"), ("draft_json", {}, "json")]),
    "delete_provider": ("call_delete_provider", [("provider_id", "", "s")]),
    "set_active_provider": ("call_set_active_provider", [("provider_id", "", "s")]),
    "test_provider": ("call_test_provider", [("provider_id", "", "s")]),
    # ── Skills ───────────────────────────────────────────────────────────
    "list_skills": ("call_list_skills", []),
    "promote_skill": ("call_promote_skill", [("package_id", "", "s")]),
    "deprecate_skill": ("call_deprecate_skill", [("package_id", "", "s")]),
    "sign_composio_skill": ("call_sign_composio_skill", [("draft_json", {}, "json")]),
    # ── Tasks / cola ─────────────────────────────────────────────────────
    "list_recent_tasks": ("call_list_recent_tasks", [("limit", 50, "u")]),
    "list_configured_tasks": ("call_list_configured_tasks", [("limit", 200, "u")]),
    # P3 — calendario per-agent: crear/borrar/toggle (mutadores de operador).
    "create_scheduled_task": ("call_create_scheduled_task", [("draft_json", {}, "json")]),
    "delete_scheduled_task": ("call_delete_scheduled_task", [("trigger_id", "", "s")]),
    "set_scheduled_task_enabled": ("call_set_scheduled_task_enabled", [
        ("trigger_id", "", "s"),
        ("enabled", True, "b"),
    ]),
    "list_pending": ("call_list_pending", [("limit", 50, "u")]),
    "get_task_status": ("call_get_task_status", [("task_id", "", "s")]),
    "get_queue_status": ("call_get_queue_status", []),
    "pause": ("call_pause", [("reason", "", "s")]),
    "resume": ("call_resume", []),
    # ── HITL ─────────────────────────────────────────────────────────────
    "list_hitl_pending": ("call_list_hitl_pending", [("limit", 50, "u")]),
    "approve": ("call_approve", [("proposal_id", "", "s")]),
    "reject": ("call_reject", [("proposal_id", "", "s"), ("reason", "", "s")]),
    # ── Security Approval (Modo Guardado) ────────────────────────────────
    # ResolveApproval: choice ∈ "once" | "session" | "always" | "deny"
    "resolve_approval": ("call_resolve_approval", [("request_id", "", "s"), ("choice", "deny", "s")]),
    # SetAutoMode / GetAutoMode: toggles autonomous execution.
    "set_auto_mode": ("call_set_auto_mode", [("enabled", False, "b")]),
    "get_auto_mode": ("call_get_auto_mode", []),
    # ── Consents (Permisos) ──────────────────────────────────────────────
    "grant_consent": ("call_grant_consent", [("capability", "", "s"), ("scope", "session", "s")]),
    "revoke_consent": ("call_revoke_consent", [("capability", "", "s")]),
    "list_consents": ("call_list_consents", []),
    "list_native_providers": ("call_list_native_providers", []),
    # OAuth device-code de suscripciones (Nous Portal): start devuelve
    # {session_id, user_code, verification_url}; status sondea hasta approved.
    "configure_native_provider": ("call_configure_native_provider", [("draft_json", {}, "json")]),
    "get_native_active": ("call_get_native_active", []),
    "start_provider_oauth": ("call_start_provider_oauth", [("provider_id", "", "s")]),
    "get_provider_oauth_status": ("call_get_provider_oauth_status", [("session_id", "", "s")]),
    # ── Skill Hub de Hermes ──────────────────────────────────────────────
    # Devuelve {query_id, results: [...], cancelled: bool}.
    "search_skills_hub": ("call_search_skills_hub", [("query", "", "s"), ("source", "all", "s"), ("limit", 20, "u")]),
    "cancel_skills_hub_search": ("call_cancel_skills_hub_search", [("query_id", "", "s")]),
    "list_hub_skills": ("call_list_hub_skills", []),
    "install_hub_skill": ("call_install_hub_skill", [("identifier", "", "s")]),
    "uninstall_hub_skill": ("call_uninstall_hub_skill", [("name", "", "s")]),
    "get_hub_op_status": ("call_get_hub_op_status", [("op_id", "", "s")]),
    # ── MCP Apps (servidores MCP del operador) ──────────────────────────
    "list_mcp_servers": ("call_list_mcp_servers", []),
    "add_mcp_server": ("call_add_mcp_server", [("draft_json", {}, "json")]),
    "remove_mcp_server": ("call_remove_mcp_server", [("server_id", "", "s")]),
    "search_mcp_registry": ("call_search_mcp_registry", [("query", "", "s"), ("limit", 20, "u")]),
    # ── Composio (SO-nativo: consumo dinámico de Composio Cloud) ────────
    "get_composio_status": ("call_get_composio_status", []),
    "set_composio_api_key": ("call_set_composio_api_key", [("api_key", "", "s")]),
    "list_composio_apps": ("call_list_composio_apps", []),
    "list_composio_connections": ("call_list_composio_connections", []),
    "connect_composio_app": ("call_connect_composio_app", [("toolkit_slug", "", "s")]),
    "set_composio_connection_alias": ("call_set_composio_connection_alias", [
        ("connection_id", "", "s"), ("alias", "", "s"),
    ]),
    "bind_composio_connection_to_agent": ("call_bind_composio_connection_to_agent", [
        ("agent_id", "", "s"), ("connection_id", "", "s"), ("toolkit_slug", "", "s"),
    ]),
    "unbind_composio_connection_from_agent": ("call_unbind_composio_connection_from_agent", [
        ("agent_id", "", "s"), ("connection_id", "", "s"),
    ]),
    "list_agent_composio_connections": ("call_list_agent_composio_connections", [
        ("agent_id", "", "s"),
    ]),
    # ── App Store del SO (dnf + Flathub via PackageStoreService) ────────
    "list_installed_packages": ("call_list_installed_packages", [("source", "rpm", "s")]),
    "search_packages": ("call_search_packages", [("query", "", "s"), ("source", "all", "s")]),
    "install_package": ("call_install_package", [("source", "", "s"), ("package_id", "", "s")]),
    "uninstall_package": ("call_uninstall_package", [("source", "", "s"), ("package_id", "", "s")]),
    "get_pkg_op_status": ("call_get_pkg_op_status", [("op_id", "", "s")]),
    # ── Acceso remoto (espejo noVNC + URL pública individual) ───────────
    "enable_remote_access": ("call_enable_remote_access", [("password", "", "s")]),
    "disable_remote_access": ("call_disable_remote_access", [("password", "", "s")]),
    "get_remote_access_status": ("call_get_remote_access_status", []),
    # ── Plataformas / memoria / auditoría / cuenta ───────────────────────
    "list_platform_models": ("call_list_platform_models", []),
    "list_memory": ("call_list_memory", [("limit", 50, "u")]),
    "search_memory": ("call_search_memory", [("query", "", "s"), ("limit", 50, "u")]),
    "get_audit_chain_head": ("call_get_audit_chain_head", []),
    "stage_account": ("call_stage_account", [("username", "", "s"), ("password", "", "s")]),
    "set_locale_keymap": ("call_set_locale_keymap", [("locale", "", "s"), ("keymap", "", "s")]),
    # ── Security Center (Grupo C) ────────────────────────────────────────
    "get_security_policy": ("call_get_security_policy", []),
    "set_security_policy": ("call_set_security_policy", [("policy_json", "{}", "s")]),
    "list_recent_scans": ("call_list_recent_scans", [("limit", 50, "u")]),
    # On-demand scan (no install). Returns JSON {scan_id, identifier, score, verdict, risks}.
    "scan_install": ("call_scan_install", [("kind", "skill", "s"), ("identifier", "", "s")]),
    # Pre-install gate desde draft completo (kind+identifier+argv/source_url) —
    # la UI lo llama ANTES de instalar; emite el modal con el score → usuario decide.
    "scan_install_draft": ("call_scan_install_draft", [("draft_json", "{}", "s")]),
    "record_install_decision": ("call_record_install_decision", [
        ("scan_id", "", "s"),
        ("decision", "", "s"),
        ("identifier", "", "s"),
        ("kind", "", "s"),
        ("score", -1, "i"),
        ("verdict", "", "s"),
        ("risks_json", "[]", "s"),
    ]),
}

# Métodos cuyo retorno D-Bus es una struct (task_id, stream_path).
_STRUCT_ENQUEUE = frozenset({"enqueue", "enqueue_from_overlay"})


def _unwrap(value: object) -> object:
    """Desempaqueta Variants de dbus-fast (a{sv}/a(...)) a tipos Python planos."""
    from dbus_fast import Variant  # noqa: PLC0415

    if isinstance(value, Variant):
        return _unwrap(value.value)
    if isinstance(value, dict):
        return {k: _unwrap(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_unwrap(v) for v in value]
    return value


def _to_json(method: str, result: object) -> str:
    """Normaliza cualquier retorno D-Bus a una cadena JSON para el QML."""
    if result is None:
        return ""
    if method in _STRUCT_ENQUEUE and isinstance(result, (list, tuple)) and len(result) >= 2:
        return json.dumps({"task_id": str(result[0]), "stream_path": str(result[1])})
    # Los métodos JSON del daemon ya devuelven una cadena: pásala tal cual.
    if isinstance(result, str):
        return result
    return json.dumps(_unwrap(result))


class HermesBackend(QObject):
    # (requestId, ok, jsonResult) — emitida cuando una llamada D-Bus termina.
    result = Signal(str, bool, str)
    # Security Center signals forwarded from the daemon D-Bus signals.
    # installReviewRequested(scan_id, scan_data_json): open InstallReview modal.
    # scanCompleted(scan_id, verdict): update shield icon / Active Scan tab.
    installReviewRequested = Signal(str, str)
    scanCompleted = Signal(str, str)
    # appLaunchRequested(cmd): el daemon (hermes, sin sesión) pide al compositor
    # (hermes-user, en la sesión) que LANCE una app — puente activate_app. El
    # compositor lo ejecuta con sysManager.launchNativeApp(cmd).
    appLaunchRequested = Signal(str)
    # approvalRequested(payload_json): emitida cuando el daemon necesita que el
    # propietario apruebe un comando peligroso (Modo Guardado). payload_json es
    # el JSON crudo de ApprovalRequested tal como llega del daemon.
    approvalRequested = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._loop = asyncio.new_event_loop()
        self._iface = None
        self._iface_lock = asyncio.Lock()
        self._signals_subscribed = False
        threading.Thread(target=self._run_loop, daemon=True).start()
        # Suscripción EAGER a las señales del daemon (AppLaunchRequested, etc.).
        # _iface_get() era LAZY: solo conectaba/suscribía en el primer method-call
        # D-Bus del QML. Pero el chat usa la API HTTP del shell-server (no D-Bus)
        # → _iface_get nunca corría → la señal de lanzamiento de apps NO se recibía
        # (activate_app "completaba" en el daemon pero la app no abría). Priming
        # con reintentos por si el daemon aún no está en el bus al arrancar.
        asyncio.run_coroutine_threadsafe(self._prime_signals(), self._loop)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _prime_signals(self) -> None:
        """Conecta al D-Bus del daemon y suscribe sus señales al arrancar.

        Reintenta hasta ~2 min porque el compositor puede arrancar antes de que
        el daemon (hermes-runtime) reclame el nombre de bus org.hermes.Runtime.
        """
        for _attempt in range(60):
            try:
                await self._iface_get()
                logger.info(
                    "hermes_backend.signals_primed: suscrito a AppLaunchRequested "
                    "(+ scan/install) en el arranque"
                )
                return
            except Exception as exc:  # noqa: BLE001 — daemon aún no en el bus
                await asyncio.sleep(2)
        logger.warning(
            "hermes_backend.signals_prime_failed: el daemon no apareció en el bus "
            "tras reintentos — AppLaunchRequested no suscrita"
        )

    async def _iface_get(self):
        if self._iface is not None:
            return self._iface
        async with self._iface_lock:
            if self._iface is not None:
                return self._iface
            from dbus_fast import BusType  # noqa: PLC0415
            from dbus_fast.aio import MessageBus  # noqa: PLC0415

            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            intro = await bus.introspect(_DBUS_NAME, _DBUS_PATH)
            obj = bus.get_proxy_object(_DBUS_NAME, _DBUS_PATH, intro)
            self._iface = obj.get_interface(_DBUS_IFACE)
            self._subscribe_daemon_signals(self._iface)
            return self._iface

    def _subscribe_daemon_signals(self, iface: object) -> None:
        """Subscribe to D-Bus signals from the daemon and forward them as Qt signals.

        The D-Bus signal callbacks run on the asyncio background loop thread.
        Qt signal emission is thread-safe from any thread, so direct emit is safe.
        """
        if self._signals_subscribed:
            return
        self._signals_subscribed = True

        def on_scan_completed(scan_id: str, verdict: str) -> None:
            try:
                self.scanCompleted.emit(scan_id, verdict)
            except Exception as exc:  # noqa: BLE001
                logger.warning("hermes_backend.scan_completed_emit_failed: %r", exc)

        def on_install_review_requested(scan_id: str, scan_data_json: str) -> None:
            try:
                self.installReviewRequested.emit(scan_id, scan_data_json)
            except Exception as exc:  # noqa: BLE001
                logger.warning("hermes_backend.install_review_emit_failed: %r", exc)

        def on_app_launch_requested(cmd: str) -> None:
            try:
                self.appLaunchRequested.emit(cmd)
            except Exception as exc:  # noqa: BLE001
                logger.warning("hermes_backend.app_launch_emit_failed: %r", exc)

        try:
            iface.on_scan_completed(on_scan_completed)
            iface.on_install_review_requested(on_install_review_requested)
        except Exception as exc:  # noqa: BLE001
            # Daemon may not expose these signals yet (older build); degrade silently.
            logger.warning("hermes_backend.signal_subscribe_failed: %r", exc)
        # Suscripción separada para no romper las anteriores si el daemon es viejo.
        try:
            iface.on_app_launch_requested(on_app_launch_requested)
        except Exception as exc:  # noqa: BLE001
            logger.warning("hermes_backend.app_launch_subscribe_failed: %r", exc)

        def on_approval_requested(payload_json: str) -> None:
            try:
                self.approvalRequested.emit(payload_json)
            except Exception as exc:  # noqa: BLE001
                logger.warning("hermes_backend.approval_requested_emit_failed: %r", exc)

        try:
            iface.on_approval_requested(on_approval_requested)
        except Exception as exc:  # noqa: BLE001
            # Daemon version may not expose this signal yet; degrade silently.
            logger.warning("hermes_backend.approval_requested_subscribe_failed: %r", exc)

    @staticmethod
    def _coerce(value: object, kind: str) -> object:
        if kind in ("i", "u"):
            try:
                return int(value)
            except (TypeError, ValueError):
                return 0
        if kind == "b":
            # D-Bus boolean: Python True/False. Accept truthy values from QML JSON.
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.lower() not in ("false", "0", "no", "")
            return bool(value)
        if kind == "json":
            return value if isinstance(value, str) else json.dumps(value or {})
        return "" if value is None else str(value)

    async def _dispatch(self, method: str, args: dict) -> object:
        iface = await self._iface_get()
        spec = _METHODS.get(method)
        if spec is not None:
            call_attr, arg_specs = spec
            fn = getattr(iface, call_attr, None)
            if fn is None:
                raise ValueError(f"el daemon no expone {call_attr} (método {method})")
            ordered = [
                self._coerce(args.get(key, default), kind)
                for (key, default, kind) in arg_specs
            ]
            return await fn(*ordered)
        # Fallback: para un método nuevo del daemon aún no tabulado. Best-effort
        # con un único arg JSON (o sin args). No inventa firmas multi-arg.
        fn = getattr(iface, "call_" + method, None)
        if fn is None:
            raise ValueError(f"método desconocido: {method}")
        return await (fn(json.dumps(args)) if args else fn())

    @Slot(str, str, str)
    def call(self, request_id: str, method: str, args_json: str) -> None:
        """Llamada async desde QML. Responde por la señal `result`."""
        try:
            args = json.loads(args_json) if args_json else {}
        except Exception:  # noqa: BLE001
            args = {}
        if not isinstance(args, dict):
            args = {}

        async def runner():
            try:
                res = await self._dispatch(method, args)
                self.result.emit(request_id, True, _to_json(method, res))
            except Exception as exc:  # noqa: BLE001 — el error vuelve a la UI
                logger.warning("hermes_backend.call_failed method=%s err=%r", method, exc)
                self.result.emit(request_id, False, json.dumps({"error": repr(exc)}))

        asyncio.run_coroutine_threadsafe(runner(), self._loop)
