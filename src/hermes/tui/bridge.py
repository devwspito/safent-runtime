"""hermes.tui.bridge — RuntimeBridge: the TUI's single door to the daemon.

Two implementations behind one contract (Liskov):
  - RealRuntimeBridge:    dbus-fast proxy on the Textual event loop + the reused
                          TaskStreamClient (AF_UNIX) for chat streaming.
  - OfflineRuntimeBridge: canned data so the app boots + renders with no bus
                          (development, and honest degraded mode in the VM if
                          the daemon is down — never a fake presented as real).

The bus is created INSIDE Textual's running loop, so D-Bus signal callbacks fire
on that same loop — handlers can post Textual messages directly (no thread hop).

Authorship is the bus sender_uid, resolved server-side (CWE-862): the TUI never
passes a uid/operator id in any call. Mutators on the system bus are authorized
by the daemon exactly as they are for the QML shell.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

from hermes.shell.infrastructure.dbus_fast_runtime_client import (
    StreamFrame,
    TaskStreamClient,
)

logger = logging.getLogger("hermes.tui.bridge")

_WELL_KNOWN_NAME = "org.hermes.Runtime"
_OBJECT_PATH = "/org/hermes/Runtime"
_INTERFACE_NAME = "org.hermes.Runtime1"

SignalHandler = Callable[..., None]


class BridgeError(RuntimeError):
    """Raised when a D-Bus call fails. Carries a human-readable message."""


class RuntimeBridge:
    """Typed convenience layer over the daemon. Subclasses implement transport.

    Every JSON verb returns parsed Python (list/dict); booleans/strings pass
    through. Call sites stay free of json.loads noise. New verbs the daemon
    exposes can be reached generically via `call(member_snake, *args)` without
    touching this class.
    """

    connected: bool = False

    # -- transport (subclass responsibility) ------------------------------
    async def connect(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    async def disconnect(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    async def call(self, member_snake: str, *args: Any) -> Any:  # pragma: no cover
        raise NotImplementedError

    def on(self, signal_snake: str, handler: SignalHandler) -> None:  # pragma: no cover
        raise NotImplementedError

    def stream(self, stream_path: str) -> AsyncIterator[StreamFrame]:  # pragma: no cover
        raise NotImplementedError

    # -- helpers ----------------------------------------------------------
    async def _json(self, member_snake: str, *args: Any) -> Any:
        raw = await self.call(member_snake, *args)
        if raw in (None, ""):
            return None
        try:
            return json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return raw

    # -- health / liveness ------------------------------------------------
    async def get_queue_status(self) -> dict:
        raw = await self.call("get_queue_status")
        return _unwrap_variants(raw) if isinstance(raw, dict) else {}

    async def get_auto_mode(self) -> dict:
        return (await self._json("get_auto_mode")) or {}

    async def set_auto_mode(self, enabled: bool) -> dict:
        return (await self._json("set_auto_mode", enabled)) or {}

    # -- kill switch ------------------------------------------------------
    async def pause(self, reason: str) -> bool:
        return bool(await self.call("pause", reason))

    async def resume(self) -> bool:
        return bool(await self.call("resume"))

    # -- HITL -------------------------------------------------------------
    async def approve(self, proposal_id: str, totp: str = "") -> str:
        # The owner's TOTP is forwarded to the daemon gate (single MFA enforcement
        # point for ALL surfaces — web/QML/TUI). Empty string ⇒ the gate fails closed.
        return await self.call("approve", proposal_id, totp)

    async def reject(self, proposal_id: str, reason: str) -> bool:
        return bool(await self.call("reject", proposal_id, reason))

    async def list_hitl_pending(self, limit: int = 50) -> list[dict]:
        return (await self._json("list_hitl_pending", limit)) or []

    # -- chat -------------------------------------------------------------
    async def enqueue_chat(self, text: str, conversation_id: str) -> tuple[str, str]:
        dedup = f"chat:{conversation_id}:{hashlib.sha256(text.encode()).hexdigest()[:16]}"
        rv = await self.call(
            "enqueue", "chat_message", text, 0, dedup, conversation_id, ""
        )
        # dbus-fast returns multi-out as [task_id, stream_path]
        if isinstance(rv, (list, tuple)) and len(rv) >= 2:
            return str(rv[0]), str(rv[1])
        raise BridgeError("Enqueue devolvió una respuesta inesperada")

    async def list_conversations(self, agent_id: str = "") -> list[dict]:
        return (await self._json("list_conversations", agent_id)) or []

    async def get_conversation(self, conversation_id: str) -> dict:
        return (await self._json("get_conversation", conversation_id)) or {}

    # -- agents -----------------------------------------------------------
    async def list_agents(self) -> list[dict]:
        return (await self._json("list_agents")) or []

    async def get_active_agent(self) -> str:
        return (await self.call("get_active_agent")) or ""

    async def set_active_agent(self, agent_id: str) -> bool:
        return bool(await self.call("set_active_agent", agent_id))

    async def create_agent(self, draft: dict) -> dict:
        return (await self._json("create_agent", json.dumps(draft))) or {}

    async def update_agent(self, agent_id: str, draft: dict) -> dict:
        return (await self._json("update_agent", agent_id, json.dumps(draft))) or {}

    async def delete_agent(self, agent_id: str) -> bool:
        return bool(await self.call("delete_agent", agent_id))

    # -- tasks / scheduler ------------------------------------------------
    async def list_recent_tasks(self, limit: int = 50) -> list[dict]:
        return (await self._json("list_recent_tasks", limit)) or []

    async def list_pending(self, limit: int = 50) -> list:
        raw = await self.call("list_pending", limit)
        return raw if isinstance(raw, list) else []

    async def list_configured_tasks(self, limit: int = 50) -> list[dict]:
        return (await self._json("list_configured_tasks", limit)) or []

    async def create_scheduled_task(self, draft: dict) -> dict:
        return (await self._json("create_scheduled_task", json.dumps(draft))) or {}

    async def delete_scheduled_task(self, trigger_id: str) -> dict:
        return (await self._json("delete_scheduled_task", trigger_id)) or {}

    async def set_scheduled_task_enabled(self, trigger_id: str, enabled: bool) -> dict:
        return (await self._json("set_scheduled_task_enabled", trigger_id, enabled)) or {}

    # -- skills -----------------------------------------------------------
    async def list_skills(self) -> list[dict]:
        return (await self._json("list_skills")) or []

    async def promote_skill(self, package_id: str) -> dict:
        return (await self._json("promote_skill", package_id)) or {}

    async def deprecate_skill(self, package_id: str) -> dict:
        return (await self._json("deprecate_skill", package_id)) or {}

    # -- MCP / integrations ----------------------------------------------
    async def list_mcp_servers(self) -> list[dict]:
        return (await self._json("list_mcp_servers")) or []

    async def add_mcp_server(self, draft: dict) -> dict:
        return (await self._json("add_mcp_server", json.dumps(draft))) or {}

    async def remove_mcp_server(self, server_id: str) -> dict:
        return (await self._json("remove_mcp_server", server_id)) or {}

    async def get_composio_status(self) -> dict:
        return (await self._json("get_composio_status")) or {}

    async def list_composio_connections(self) -> list[dict]:
        return (await self._json("list_composio_connections")) or []

    async def set_composio_api_key(self, api_key: str) -> dict:
        return (await self._json("set_composio_api_key", api_key)) or {}

    async def list_composio_apps(self) -> list[dict]:
        return (await self._json("list_composio_apps")) or []

    async def connect_composio_app(self, toolkit_slug: str) -> dict:
        return (await self._json("connect_composio_app", toolkit_slug)) or {}

    # -- skills hub (marketplace) -----------------------------------------
    async def search_skills_hub(self, query: str, source: str = "all", limit: int = 20) -> dict:
        return (await self._json("search_skills_hub", query, source, limit)) or {}

    async def list_hub_skills(self) -> list[dict]:
        return (await self._json("list_hub_skills")) or []

    async def install_hub_skill(self, identifier: str) -> dict:
        return (await self._json("install_hub_skill", identifier)) or {}

    async def uninstall_hub_skill(self, name: str) -> dict:
        return (await self._json("uninstall_hub_skill", name)) or {}

    async def get_hub_op_status(self, op_id: str) -> dict:
        return (await self._json("get_hub_op_status", op_id)) or {}

    # -- providers --------------------------------------------------------
    async def list_providers(self) -> list[dict]:
        return (await self._json("list_providers")) or []

    async def get_active_provider(self) -> dict:
        return (await self._json("get_active_provider")) or {}

    async def set_active_provider(self, provider_id: str) -> dict:
        return (await self._json("set_active_provider", provider_id)) or {}

    async def test_provider(self, provider_id: str) -> dict:
        return (await self._json("test_provider", provider_id)) or {}

    async def add_provider(self, draft: dict) -> dict:
        # daemon draft: {kind, alias, default_model, base_url, api_key, set_active}
        return (await self._json("add_provider", json.dumps(draft))) or {}

    async def delete_provider(self, provider_id: str) -> bool:
        return bool(await self.call("delete_provider", provider_id))

    async def list_native_providers(self) -> list[dict]:
        return (await self._json("list_native_providers")) or []

    async def configure_native_provider(self, draft: dict) -> dict:
        return (await self._json("configure_native_provider", json.dumps(draft))) or {}

    async def start_provider_oauth(self, provider_id: str) -> dict:
        return (await self._json("start_provider_oauth", provider_id)) or {}

    async def get_provider_oauth_status(self, session_id: str) -> dict:
        return (await self._json("get_provider_oauth_status", session_id)) or {}

    # -- security center --------------------------------------------------
    async def list_recent_scans(self, limit: int = 50) -> list[dict]:
        return (await self._json("list_recent_scans", limit)) or []

    async def get_security_policy(self) -> dict:
        return (await self._json("get_security_policy")) or {}

    async def get_audit_chain_head(self) -> dict:
        return (await self._json("get_audit_chain_head")) or {}

    async def scan_install(self, kind: str, identifier: str) -> dict:
        return (await self._json("scan_install", kind, identifier)) or {}

    async def record_install_decision(
        self,
        scan_id: str,
        decision: str,
        *,
        identifier: str = "",
        kind: str = "",
        score: int = -1,
        verdict: str = "",
        risks_json: str = "[]",
    ) -> dict:
        raw = await self.call(
            "record_install_decision", scan_id, decision, identifier, kind, score, verdict, risks_json
        )
        if isinstance(raw, str) and raw:
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {"ok": True}
        return {"ok": bool(raw)}

    # -- memory -----------------------------------------------------------
    async def list_memory(self, limit: int = 50) -> list[dict]:
        return (await self._json("list_memory", limit)) or []

    async def search_memory(self, query: str, limit: int = 50) -> list[dict]:
        return (await self._json("search_memory", query, limit)) or []

    # -- consents ---------------------------------------------------------
    async def list_consents(self) -> list[dict]:
        return (await self._json("list_consents")) or []

    # -- packages (app store) ---------------------------------------------
    async def list_installed_packages(self, source: str = "flatpak") -> list[dict]:
        return (await self._json("list_installed_packages", source)) or []

    async def search_packages(self, query: str, source: str = "all") -> list[dict]:
        return (await self._json("search_packages", query, source)) or []

    async def install_package(self, source: str, package_id: str) -> dict:
        return (await self._json("install_package", source, package_id)) or {}

    async def uninstall_package(self, source: str, package_id: str) -> dict:
        return (await self._json("uninstall_package", source, package_id)) or {}

    async def get_pkg_op_status(self, op_id: str) -> dict:
        return (await self._json("get_pkg_op_status", op_id)) or {}


def _unwrap_variants(raw: dict) -> dict:
    """dbus-fast returns a{sv} as dict[str, Variant]; unwrap .value."""
    return {k: (v.value if hasattr(v, "value") else v) for k, v in raw.items()}


def new_conversation_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Real bridge — dbus-fast proxy on the Textual loop
# ---------------------------------------------------------------------------


class RealRuntimeBridge(RuntimeBridge):
    """Live D-Bus transport. Built lazily in connect() on Textual's loop."""

    def __init__(self) -> None:
        self._iface: Any = None
        self._bus: Any = None

    async def connect(self) -> None:
        from dbus_fast import BusType  # noqa: PLC0415
        from dbus_fast.aio import MessageBus  # noqa: PLC0415

        self._bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        introspection = await self._bus.introspect(_WELL_KNOWN_NAME, _OBJECT_PATH)
        proxy = self._bus.get_proxy_object(_WELL_KNOWN_NAME, _OBJECT_PATH, introspection)
        self._iface = proxy.get_interface(_INTERFACE_NAME)
        self.connected = True
        logger.info("RealRuntimeBridge conectado a %s", _INTERFACE_NAME)

    async def disconnect(self) -> None:
        if self._bus is not None:
            self._bus.disconnect()
        self.connected = False

    async def call(self, member_snake: str, *args: Any) -> Any:
        fn = getattr(self._iface, f"call_{member_snake}", None)
        if fn is None:
            raise BridgeError(f"el daemon no expone el verbo {member_snake!r}")
        try:
            return await fn(*args)
        except Exception as exc:  # noqa: BLE001 — surface a clean message to the UI
            raise BridgeError(str(exc)) from exc

    def on(self, signal_snake: str, handler: SignalHandler) -> None:
        register = getattr(self._iface, f"on_{signal_snake}", None)
        if register is None:
            logger.warning("señal desconocida: %s (ignorada)", signal_snake)
            return
        # Resilient subscription: dbus-fast validates handler arity against the
        # introspected signal at registration. If the daemon's signature drifts
        # (e.g. an arg added), don't let one bad signal abort ALL subscriptions —
        # log it and keep going. The others (incl. ApprovalRequested) still wire.
        try:
            register(handler)
        except Exception as exc:  # noqa: BLE001
            logger.warning("no se pudo suscribir a %s: %s", signal_snake, exc)

    def stream(self, stream_path: str) -> AsyncIterator[StreamFrame]:
        return TaskStreamClient(stream_path=stream_path).frames()


# ---------------------------------------------------------------------------
# Offline bridge — canned data, no bus (dev + honest degraded mode)
# ---------------------------------------------------------------------------


class OfflineRuntimeBridge(RuntimeBridge):
    """In-memory bridge: lets the app boot and render with no daemon.

    Streams a short canned assistant reply so the chat UX is demonstrable
    offline. Clearly labelled in the header as 'sin conexión' so it is never
    mistaken for the real agent.
    """

    def __init__(self) -> None:
        self._responses: dict[str, Any] = {
            "get_queue_status": {
                "state": "idle",
                "pending": 0,
                "in_progress": 0,
                "pending_approval": 0,
                "last_audit_head": "",
            },
            "get_auto_mode": json.dumps({"enabled": False}),
            "get_active_agent": "",
            "list_agents": json.dumps(
                [
                    {
                        "id": "00000000-0000-0000-0000-000000000000",
                        "name": "Safent",
                        "role": "Cerebro del sistema",
                        "is_default": True,
                        "autonomy_level": "omnipotente",
                    },
                    {
                        "id": "11111111-1111-1111-1111-111111111111",
                        "name": "Analista de facturas",
                        "role": "Contable junior",
                        "is_default": False,
                        "autonomy_level": "supervised",
                    },
                    {
                        "id": "22222222-2222-2222-2222-222222222222",
                        "name": "Vigía de correo",
                        "role": "Triaje de bandeja de entrada",
                        "is_default": False,
                        "autonomy_level": "autonomous",
                    },
                ]
            ),
            "list_skills": json.dumps(
                [
                    {"name": "Conciliar banco", "state": "autonomous", "source": "aprendida", "package_id": "sk-1"},
                    {"name": "Descargar nóminas", "state": "validated", "source": "aprendida", "package_id": "sk-2"},
                    {"name": "Clasificar gastos", "state": "validating", "source": "enseñada", "package_id": "sk-3"},
                ]
            ),
            "list_mcp_servers": json.dumps(
                [
                    {"name": "serena", "status": "connected", "tool_count": 18, "server_id": "mcp-1"},
                    {"name": "open-design", "status": "connected", "tool_count": 10, "server_id": "mcp-2"},
                    {"name": "context7", "status": "error", "tool_count": 0, "server_id": "mcp-3"},
                ]
            ),
            "list_providers": json.dumps(
                [
                    {"provider_id": "p-1", "name": "Tu proveedor", "model": "gpt-5.4-mini (demo)", "active": True},
                    {"provider_id": "p-2", "name": "Anthropic", "model": "claude-opus-4", "active": False},
                    {"provider_id": "p-3", "name": "OpenAI", "model": "gpt-4o", "active": False},
                ]
            ),
            "get_active_provider": json.dumps({"provider_id": "p-1", "name": "Tu proveedor", "model": "gpt-5.4-mini (demo)"}),
            "list_recent_tasks": json.dumps(
                [
                    {"title": "Conciliar extracto BBVA", "kind": "chat_message", "status": "done", "created_at": "10:02"},
                    {"title": "Revisar bandeja", "kind": "scheduled", "status": "running", "created_at": "10:15"},
                    {"title": "Descargar modelo 303", "kind": "chat_message", "status": "error", "created_at": "09:41"},
                ]
            ),
            "list_configured_tasks": json.dumps(
                [
                    {"title": "Triaje de correo", "cron": "0 9 * * 1-5", "target_agent_id": "", "enabled": True, "trigger_id": "t-1"},
                    {"title": "Cierre diario", "cron": "0 18 * * 1,3,5", "target_agent_id": "11111111", "enabled": False, "trigger_id": "t-2"},
                ]
            ),
            "list_recent_scans": json.dumps(
                [
                    {"verdict": "pass", "kind": "skill", "identifier": "Conciliar banco", "created_at": "10:00"},
                    {"verdict": "blocked", "kind": "mcp", "identifier": "servidor-desconocido", "created_at": "09:30"},
                ]
            ),
            "list_memory": json.dumps(
                [
                    {"entry_index": 1, "target": "usuario", "content": "Prefiere informes en tono operativo, sin floritura."},
                    {"entry_index": 2, "target": "empresa", "content": "Cierre trimestral el día 20 de cada trimestre."},
                ]
            ),
            "list_installed_packages": json.dumps(
                [
                    {"name": "org.gnome.TextEditor", "version": "46.0", "source": "flatpak"},
                    {"name": "chromium", "version": "126.0", "source": "rpm"},
                ]
            ),
            "list_consents": "[]",
            "list_conversations": "[]",
            "list_hitl_pending": json.dumps(
                [
                    {"tool": "delete_file", "risk": "high", "justification": "Borrar informe_viejo.pdf", "proposal_id": "prop-1"},
                ]
            ),
            "get_audit_chain_head": json.dumps(
                {"integrity": "intact", "head_hash": "9f2a4c7e10b3d8f6aa12", "captured_at": "10:16"}
            ),
            "get_security_policy": json.dumps(
                {"default": "deny", "require_hitl_high": True, "autoaprobar_bajo_riesgo": True}
            ),
            "get_composio_status": json.dumps({"configured": True, "entity_id": "luis@empresa"}),
            "list_composio_connections": json.dumps(
                [{"name": "Gmail", "alias": "correo trabajo"}, {"name": "Google Drive", "alias": ""}]
            ),
        }
        self._handlers: dict[str, list[SignalHandler]] = {}

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    async def call(self, member_snake: str, *args: Any) -> Any:
        if member_snake == "enqueue":
            tid = str(uuid.uuid4())
            conv = args[4] if len(args) > 4 else ""
            # Fire canned ChatDelta/ChatStreamEnd signals so the offline TUI
            # streams a demo reply token-by-token (mirrors the real signal path).
            try:
                asyncio.get_running_loop().create_task(self._fake_chat_signals(conv))
            except RuntimeError:
                pass
            return [tid, f"/ws/tasks/{tid}"]
        if member_snake in ("pause", "resume", "set_active_agent", "delete_agent",
                            "delete_provider"):
            return True
        if member_snake == "set_auto_mode":
            return json.dumps({"enabled": bool(args[0]) if args else False})
        if member_snake in ("add_provider", "configure_native_provider"):
            try:
                d = json.loads(args[0]) if args else {}
            except (TypeError, json.JSONDecodeError):
                d = {}
            return json.dumps(
                {
                    "provider_id": str(uuid.uuid4()),
                    "name": d.get("alias") or d.get("kind") or "proveedor",
                    "model": d.get("default_model") or "—",
                    "is_active": bool(d.get("set_active")),
                }
            )
        if member_snake == "list_native_providers":
            return json.dumps(
                [
                    {"kind": "openai", "name": "OpenAI"},
                    {"kind": "anthropic", "name": "Anthropic"},
                    {"kind": "nous", "name": "Nous Portal"},
                ]
            )
        if member_snake == "set_composio_api_key":
            return json.dumps({"configured": True, "entity_id": "demo@local"})
        if member_snake == "connect_composio_app":
            slug = args[0] if args else "app"
            return json.dumps({"status": "pending", "connect_url": f"https://composio.dev/connect/{slug}"})
        if member_snake == "list_composio_apps":
            return json.dumps([{"slug": "gmail", "name": "Gmail"}, {"slug": "googledrive", "name": "Google Drive"}])
        if member_snake == "search_skills_hub":
            return json.dumps({"query_id": "q1", "cancelled": False, "results": [
                {"identifier": "pdf-tools", "name": "PDF tools", "source": "hub"},
                {"identifier": "csv-cleaner", "name": "CSV cleaner", "source": "hub"}]})
        if member_snake == "list_hub_skills":
            return json.dumps([])
        if member_snake in ("install_hub_skill", "uninstall_hub_skill"):
            return json.dumps({"op_id": str(uuid.uuid4()), "status": "done"})
        if member_snake == "get_hub_op_status":
            return json.dumps({"op_id": args[0] if args else "", "status": "done"})
        if member_snake == "search_packages":
            return json.dumps([
                {"name": "org.gimp.GIMP", "version": "2.10", "source": "flatpak"},
                {"name": "htop", "version": "3.3", "source": "rpm"}])
        if member_snake in ("install_package", "uninstall_package"):
            return json.dumps({"op_id": str(uuid.uuid4()), "status": "started"})
        if member_snake == "get_pkg_op_status":
            return json.dumps({"op_id": args[0] if args else "", "status": "done", "log_tail": "", "error_message": ""})
        if member_snake == "scan_install":
            return json.dumps({"scan_id": str(uuid.uuid4()), "verdict": "pass", "score": 92,
                               "kind": args[0] if args else "", "identifier": args[1] if len(args) > 1 else "",
                               "risks": []})
        if member_snake == "record_install_decision":
            return json.dumps({"ok": True})
        return self._responses.get(member_snake, "")

    def on(self, signal_snake: str, handler: SignalHandler) -> None:
        self._handlers.setdefault(signal_snake, []).append(handler)

    async def _fake_chat_signals(self, conversation_id: str) -> None:
        """Emit canned ChatDelta/ChatStreamEnd signals (offline demo streaming)."""
        await asyncio.sleep(0.15)
        canned = (
            "Estás en **modo sin conexión** — respuesta de ejemplo. Cuando Safent "
            "Terminal corre sobre el SO, este texto llega token a token desde el "
            "Cerebro real. Mismo broker, mismo confinamiento, misma auditoría."
        )
        seq = 0
        for word in canned.split(" "):
            for h in list(self._handlers.get("chat_delta", [])):
                h(conversation_id, seq, word + " ")
            seq += 1
            await asyncio.sleep(0.01)
        for h in list(self._handlers.get("chat_stream_end", [])):
            h(conversation_id)

    async def stream(self, stream_path: str) -> AsyncIterator[StreamFrame]:  # type: ignore[override]
        tid = stream_path.rsplit("/", 1)[-1]
        canned = (
            "Estás en **modo sin conexión** — el daemon no está disponible, "
            "así que esto es una respuesta de ejemplo. Cuando Safent Terminal "
            "corre sobre el SO, este texto llega *token a token* desde el "
            "Cerebro real por el socket de tareas.\n\n"
            "- Mismo broker, mismo confinamiento, misma auditoría.\n"
            "- Solo cambia la presentación: una TUI.\n"
        )
        for word in canned.split(" "):
            yield StreamFrame(kind="delta", task_id=tid, payload={"delta": word + " "})
        yield StreamFrame(kind="done", task_id=tid, payload={})
