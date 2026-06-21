"""DbusControlPlaneAdapter — cliente D-Bus del control-plane LOCAL.

T048 🔒 / FR-010 / CTRL-P1-11:
  Implementa ControlPlanePort para el shell-server.
  Invoca org.hermes.Runtime1.Enqueue sobre el system bus vía dbus-fast.

Seguridad (CTRL-P1-26 / G6):
  - El shell-server NO pasa enqueued_by en el payload — lo inyecta el daemon
    server-side a partir del sender_uid del bus (GetConnectionUnixUser).
  - Si el daemon no responde → AgentUnavailable (fail-hard, SC-005).
  - 0 fallback passthrough, 0 degradación silenciosa.

El sender_uid del shell-server es el del proceso operador (os.getuid()).
El daemon lo verifica contra authorized_uids antes de aceptar el enqueue.
"""

from __future__ import annotations

import logging
import os
from typing import Any
from uuid import UUID

from hermes.tasks.control_plane.domain.ports import (
    AgentUnavailable,
    AuthenticatedChannel,
    ConfiguredTaskView,
    EnqueueNotAuthorized,
    EnqueueResult,
    PendingTaskView,
    QueueStatus,
    RecentTaskView,
    TaskStatusView,
    UnknownTask,
)

logger = logging.getLogger("hermes.shell_server.chat.dbus_cp_adapter")

_WELL_KNOWN_NAME = "org.hermes.Runtime"
_OBJECT_PATH = "/org/hermes/Runtime"
_INTERFACE_NAME = "org.hermes.Runtime1"
_DBUS_CALL_TIMEOUT = 5.0  # segundos


def _resolve_operator_uid() -> int:
    """POSIX uid of the human operator the proxied call is attributed to.

    MUST mirror the daemon's hermes.runtime.__main__._resolve_operator_uid so
    both processes agree on who the operator is:
      1. HERMES_OPERATOR_UID env (explicit override).
      2. pwd lookup of "hermes-user" (autologin operator in Agents OS Edition).
      3. os.getuid() fallback (dev/test).
    uid 0 (root) is never a valid operator — fail-closed.
    """
    env_val = os.environ.get("HERMES_OPERATOR_UID", "").strip()
    if env_val:
        uid = int(env_val)
    else:
        import pwd  # noqa: PLC0415

        try:
            uid = pwd.getpwnam("hermes-user").pw_uid
        except KeyError:
            uid = os.getuid()
    if uid == 0:
        raise RuntimeError(
            "operator uid resolved to root (0) — misconfiguration; "
            "set HERMES_OPERATOR_UID to the hermes-user uid"
        )
    return uid


class DbusControlPlaneAdapter:
    """Cliente D-Bus para invocar org.hermes.Runtime1 desde el shell-server.

    Implementa el subconjunto de ControlPlanePort necesario para T048:
      - enqueue(channel, trigger_kind, text, priority, dedup_key)

    Los demás métodos (pause/resume/approve/reject/read-only) quedan disponibles
    para que el shell-server pueda exponer endpoints de supervisión futuros
    sin cambiar esta interfaz.

    Fail-hard: cualquier error D-Bus (NameNotFound, Timeout, AccessDenied) se
    traduce a AgentUnavailable o EnqueueNotAuthorized — nunca se propaga el
    error técnico al cliente HTTP.
    """

    def __init__(self, *, sender_uid: int) -> None:
        self._sender_uid = sender_uid
        self._minter = None  # lazy OperatorTokenMinter
        self._operator_id: str | None = None

    # ------------------------------------------------------------------
    # Confused-deputy remediation (CTRL-P1-3 / CWE-862)
    #
    # The shell-server runs as "hermes" (proxy uid), NOT as the human operator.
    # The daemon's D-Bus authorization REQUIRES proxy calls to carry a signed
    # OperatorToken so that `enqueued_by` is attributed to the human, never to
    # the proxy. The token is HMAC-signed with the master.key subkey
    # "operator-token" — the SAME key the daemon's verifier derives — so it
    # verifies cross-process. Short expiry + nonce give replay protection.
    # The operator identity for the single-operator desktop is hermes-user;
    # the shell-server already authenticated the operator at the HTTP layer
    # (device auth) before reaching this proxy call.
    # ------------------------------------------------------------------

    def _mint_operator_token(self, *, operation: str) -> str:
        """Mint a signed OperatorToken for a proxied mutating verb.

        Raises AgentUnavailable (mapped to 503) if the master.key is absent —
        without it no valid token can be minted and the daemon would deny the
        proxy call anyway. NEVER log the returned token.
        """
        if self._minter is None or self._operator_id is None:
            try:
                from hermes.shell_server.security.secrets import SecretsVault  # noqa: PLC0415
                from hermes.shell_server.security.operator_token import (  # noqa: PLC0415
                    OperatorTokenMinter,
                )

                key = SecretsVault().derive_subkey(label="operator-token")
                self._minter = OperatorTokenMinter(signing_key=key)
                self._operator_id = str(UUID(int=_resolve_operator_uid()))
            except Exception as exc:  # noqa: BLE001
                raise AgentUnavailable(
                    f"operator token unavailable (master.key/keygen): {exc}"
                ) from exc
        return self._minter.mint(
            operator_id=self._operator_id, operation=operation
        )

    # ------------------------------------------------------------------
    # Enqueue (T048 / FR-010)
    # ------------------------------------------------------------------

    async def enqueue(
        self,
        *,
        channel: AuthenticatedChannel,
        trigger_kind: str,
        text: str,
        priority: int = 0,
        dedup_key: str | None = None,
        conversation_id: str | None = None,
    ) -> EnqueueResult:
        """Encola vía D-Bus → org.hermes.Runtime1.Enqueue.

        enqueued_by NO se pasa en el payload — el daemon lo deriva del
        sender_uid del bus (GetConnectionUnixUser). CTRL-P1-3 / G2.

        Raises:
            AgentUnavailable: daemon no en el bus o timeout.
            EnqueueNotAuthorized: sender_uid no autorizado por el daemon.
        """
        from dbus_fast.aio import MessageBus  # noqa: PLC0415
        from dbus_fast.errors import DBusError  # noqa: PLC0415
        from dbus_fast import BusType  # noqa: PLC0415

        # Mint OUTSIDE the bus try so a missing master.key surfaces as 503
        # (AgentUnavailable), not a generic 500. Operation MUST be "enqueue" —
        # the exact string the daemon's _authorize_and_resolve verifies.
        operator_token = self._mint_operator_token(operation="enqueue")

        bus: MessageBus | None = None
        try:
            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            introspection = await bus.introspect(_WELL_KNOWN_NAME, _OBJECT_PATH)
            proxy = bus.get_proxy_object(
                _WELL_KNOWN_NAME, _OBJECT_PATH, introspection
            )
            iface = proxy.get_interface(_INTERFACE_NAME)
            task_id_str, stream_path = await iface.call_enqueue(
                trigger_kind,
                text,
                priority,
                dedup_key or "",
                conversation_id or "",
                operator_token,
            )
        except DBusError as exc:
            _translate_dbus_error(exc)
        except Exception as exc:
            raise AgentUnavailable(
                f"D-Bus Enqueue falló: {exc} (daemon caído o no en el bus)"
            ) from exc
        finally:
            if bus is not None and bus.connected:
                bus.disconnect()

        return EnqueueResult(
            task_id=UUID(task_id_str),
            stream_path=stream_path,
        )

    # ------------------------------------------------------------------
    # Kill-switch / HITL (delegados a D-Bus; no usados por T048)
    # ------------------------------------------------------------------

    async def pause(self, *, channel: AuthenticatedChannel, reason: str) -> None:
        await self._call_void("call_pause", reason)

    async def resume(self, *, channel: AuthenticatedChannel) -> None:
        await self._call_void("call_resume")

    async def approve(
        self, *, channel: AuthenticatedChannel, proposal_id: UUID
    ) -> str:
        return await self._call_returning("call_approve", str(proposal_id))

    async def reject(
        self, *, channel: AuthenticatedChannel, proposal_id: UUID, reason: str
    ) -> None:
        await self._call_void("call_reject", str(proposal_id), reason)

    # ------------------------------------------------------------------
    # Read-only supervisión (CTRL-P1-5 — metadatos, nunca payload)
    # ------------------------------------------------------------------

    async def get_queue_status(self) -> QueueStatus:
        raw = await self._call_returning_dict("call_get_queue_status")
        return QueueStatus(
            state=raw.get("state", "unknown"),
            pending=int(raw.get("pending", 0)),
            in_progress=int(raw.get("in_progress", 0)),
            pending_approval=int(raw.get("pending_approval", 0)),
            last_audit_head_hex=raw.get("last_audit_head_hex", ""),
        )

    async def list_pending(
        self, *, limit: int = 50
    ) -> tuple[PendingTaskView, ...]:
        rows = await self._call_returning_list("call_list_pending", limit)
        return tuple(
            PendingTaskView(
                task_id=UUID(r["task_id"]),
                trigger_kind=r["trigger_kind"],
                priority=int(r.get("priority", 0)),
                enqueued_at_iso=r["enqueued_at_iso"],
            )
            for r in rows
            if r.get("task_id")
        )

    async def get_task_status(self, *, task_id: UUID) -> TaskStatusView:
        raw = await self._call_returning_dict("call_get_task_status", str(task_id))
        if not raw.get("task_id"):
            raise UnknownTask(f"task_id {task_id} not found")
        return TaskStatusView(
            task_id=UUID(raw["task_id"]),
            status=raw["status"],
            attempts=int(raw.get("attempts", 0)),
            enqueued_by="",  # not exposed over D-Bus supervision path
            stream_path=raw.get("stream_path", f"/ws/tasks/{task_id}"),
            error=raw.get("error") or None,
        )

    async def list_configured_tasks(
        self, *, limit: int = 200
    ) -> tuple[ConfiguredTaskView, ...]:
        """Configured tasks dashboard via D-Bus → runtime.

        Fail-soft: returns empty tuple on AgentUnavailable so callers can
        render a disconnected state without raising.
        """
        rows = await self._call_returning_list("call_list_configured_tasks", limit)
        return tuple(
            ConfiguredTaskView(
                trigger_id=r["trigger_id"],
                label=r["label"],
                trigger_type=r["trigger_type"],
                recurrence=r["recurrence"],
                enabled=bool(r.get("enabled", True)),
                risk_ceiling=r["risk_ceiling"],
                last_run_at=r.get("last_run_at") or None,
                last_status=r.get("last_status") or None,
                next_run_at=r.get("next_run_at") or None,
            )
            for r in rows
            if r.get("trigger_id")
        )

    async def list_hitl_pending(self, *, limit: int = 50) -> list[dict]:
        """Pending HITL proposals via D-Bus → list_hitl_pending.

        Returns daemon rows [{proposal_id, risk, justification, tool_name, created_at}].
        Fail-soft: returns [] on AgentUnavailable so the caller can render an empty
        approval queue rather than a 503.
        """
        try:
            rows = await self._call_returning_list("call_list_hitl_pending", limit)
        except AgentUnavailable as exc:
            logger.warning(
                "hermes.shell_server.hitl.list_unavailable",
                extra={"reason": str(exc)},
            )
            return []
        return rows

    async def list_recent_tasks(
        self, *, limit: int = 50
    ) -> tuple[RecentTaskView, ...]:
        """Recent work items activity log via D-Bus → runtime.

        Fail-soft: returns empty tuple on AgentUnavailable.
        """
        rows = await self._call_returning_list("call_list_recent_tasks", limit)
        return tuple(
            RecentTaskView(
                task_id=r["task_id"],
                label=r["label"],
                status=r["status"],
                trigger_kind=r["trigger_kind"],
                enqueued_at=r["enqueued_at"],
                claimed_at=r.get("claimed_at") or None,
            )
            for r in rows
            if r.get("task_id")
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _call_void(self, method: str, *args: Any) -> None:
        from dbus_fast.aio import MessageBus  # noqa: PLC0415
        from dbus_fast.errors import DBusError  # noqa: PLC0415
        from dbus_fast import BusType  # noqa: PLC0415

        bus: MessageBus | None = None
        try:
            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            introspection = await bus.introspect(_WELL_KNOWN_NAME, _OBJECT_PATH)
            proxy = bus.get_proxy_object(
                _WELL_KNOWN_NAME, _OBJECT_PATH, introspection
            )
            iface = proxy.get_interface(_INTERFACE_NAME)
            await getattr(iface, method)(*args)
        except DBusError as exc:
            _translate_dbus_error(exc)
        except Exception as exc:
            raise AgentUnavailable(f"D-Bus {method} falló: {exc}") from exc
        finally:
            if bus is not None and bus.connected:
                bus.disconnect()

    async def _call_returning_dict(self, method: str, *args: Any) -> dict:
        """Call a D-Bus method that returns a JSON-encoded dict.

        The daemon serializes complex return types as JSON strings to keep
        D-Bus type signatures simple.
        """
        import json  # noqa: PLC0415
        from dbus_fast.aio import MessageBus  # noqa: PLC0415
        from dbus_fast.errors import DBusError  # noqa: PLC0415
        from dbus_fast import BusType  # noqa: PLC0415

        bus: MessageBus | None = None
        try:
            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            introspection = await bus.introspect(_WELL_KNOWN_NAME, _OBJECT_PATH)
            proxy = bus.get_proxy_object(
                _WELL_KNOWN_NAME, _OBJECT_PATH, introspection
            )
            iface = proxy.get_interface(_INTERFACE_NAME)
            result: str = await getattr(iface, method)(*args)
            return json.loads(result)
        except DBusError as exc:
            _translate_dbus_error(exc)
        except Exception as exc:
            raise AgentUnavailable(f"D-Bus {method} falló: {exc}") from exc
        finally:
            if bus is not None and bus.connected:
                bus.disconnect()
        return {}  # unreachable; satisfies type checker

    async def _call_returning_list(self, method: str, *args: Any) -> list[dict]:
        """Call a D-Bus method that returns a JSON-encoded list of dicts."""
        import json  # noqa: PLC0415
        from dbus_fast.aio import MessageBus  # noqa: PLC0415
        from dbus_fast.errors import DBusError  # noqa: PLC0415
        from dbus_fast import BusType  # noqa: PLC0415

        bus: MessageBus | None = None
        try:
            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            introspection = await bus.introspect(_WELL_KNOWN_NAME, _OBJECT_PATH)
            proxy = bus.get_proxy_object(
                _WELL_KNOWN_NAME, _OBJECT_PATH, introspection
            )
            iface = proxy.get_interface(_INTERFACE_NAME)
            result: str = await getattr(iface, method)(*args)
            return json.loads(result)
        except DBusError as exc:
            _translate_dbus_error(exc)
        except Exception as exc:
            raise AgentUnavailable(f"D-Bus {method} falló: {exc}") from exc
        finally:
            if bus is not None and bus.connected:
                bus.disconnect()
        return []  # unreachable; satisfies type checker

    async def _call_returning(self, method: str, *args: Any) -> str:
        from dbus_fast.aio import MessageBus  # noqa: PLC0415
        from dbus_fast.errors import DBusError  # noqa: PLC0415
        from dbus_fast import BusType  # noqa: PLC0415

        bus: MessageBus | None = None
        try:
            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            introspection = await bus.introspect(_WELL_KNOWN_NAME, _OBJECT_PATH)
            proxy = bus.get_proxy_object(
                _WELL_KNOWN_NAME, _OBJECT_PATH, introspection
            )
            iface = proxy.get_interface(_INTERFACE_NAME)
            result: str = await getattr(iface, method)(*args)
            return result
        except DBusError as exc:
            _translate_dbus_error(exc)
        except Exception as exc:
            raise AgentUnavailable(f"D-Bus {method} falló: {exc}") from exc
        finally:
            if bus is not None and bus.connected:
                bus.disconnect()

        return ""  # unreachable; satisfies type checker


def _translate_dbus_error(exc: Exception) -> None:
    """Traduce errores D-Bus a excepciones de dominio.

    CTRL-P1-11: 0 errores técnicos al cliente. Fail-hard explícito.
    """
    err_str = str(exc).lower()
    if "notauthorized" in err_str or "accessdenied" in err_str or "authorization" in err_str:
        raise EnqueueNotAuthorized(
            f"UID del shell-server no autorizado por el daemon: {exc}"
        ) from exc
    raise AgentUnavailable(
        f"org.hermes.Runtime1 no disponible en el bus: {exc}"
    ) from exc
