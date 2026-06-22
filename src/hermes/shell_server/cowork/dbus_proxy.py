"""DbusRuntimeProxy — thin async D-Bus adapter for shell-server REST routers.

All shell-server routers that need to call org.hermes.Runtime1 share this
single proxy instead of each re-implementing the connection boilerplate.

Design contracts:
- One short-lived D-Bus connection per call (same pattern as
  DbusControlPlaneAdapter; avoids holding an idle persistent connection).
- JSON verbs: the daemon serialises complex return types as JSON strings.
- Mutators receive a signed OperatorToken via _mint_operator_token() from the
  operator-token subkey (shared with DbusControlPlaneAdapter — same key,
  same verifier on the daemon side).
- Fail-hard on mutators: raises AgentUnavailable → caller renders 503.
- Fail-soft on read-only list verbs: callers catch AgentUnavailable and
  return empty list / empty dict.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any
from uuid import UUID

from fastapi import HTTPException

from hermes.tasks.control_plane.domain.ports import AgentUnavailable

logger = logging.getLogger("hermes.shell_server.cowork.dbus_proxy")

_WELL_KNOWN_NAME = "org.hermes.Runtime"
_OBJECT_PATH = "/org/hermes/Runtime"
_INTERFACE_NAME = "org.hermes.Runtime1"
_DBUS_CALL_TIMEOUT = 8.0


class DbusRuntimeProxy:
    """Async D-Bus proxy for org.hermes.Runtime1 verbs used by REST routers.

    Instantiated once in create_app() and stored on app.state.dbus_proxy.
    Each call opens a fresh connection (dbus-fast pattern; avoids stale
    connection issues in a long-running async process).
    """

    def __init__(self) -> None:
        self._minter = None
        self._operator_id: str | None = None

    # ------------------------------------------------------------------
    # Public API — each method maps 1-to-1 to a D-Bus verb
    # ------------------------------------------------------------------

    async def call_list(self, member: str, *args: Any) -> list[dict]:
        """Call a D-Bus member that returns a JSON list. Fail-soft: [] on error."""
        raw = await self._call(member, *args)
        return _parse_list(raw)

    async def call_dict(self, member: str, *args: Any) -> dict:
        """Call a D-Bus member that returns a JSON dict. Fail-soft: {} on error."""
        raw = await self._call(member, *args)
        return _parse_dict(raw)

    async def call_bool(self, member: str, *args: Any) -> bool:
        """Call a D-Bus member that returns a bool. Fail-hard on AgentUnavailable."""
        raw = await self._call(member, *args)
        return bool(raw)

    async def call_str(self, member: str, *args: Any) -> str:
        """Call a D-Bus member that returns a plain string (not JSON).

        Fail-soft: returns "" on error — callers must handle the empty case.
        """
        try:
            raw = await self._call(member, *args)
            return str(raw) if raw is not None else ""
        except AgentUnavailable:
            return ""

    async def call_mutator(self, member: str, *args: Any) -> dict:
        """Call a mutating D-Bus verb as the shell-server proxy.

        The cowork mutator verbs (SetActiveAgent, CreateAgent, AddMcpServer,
        AddProvider, …) do NOT take an operator_token in their D-Bus signature —
        only Enqueue does. The daemon resolves the caller uid from the bus
        (GetConnectionUnixUser) and authorizes the shell-server's service uid
        directly (it is in the daemon's authorized_uids). So we call with the
        verb's own args; appending a token here would overrun the method
        signature and raise a DBusError ("too many arguments").
        """
        raw = await self._call(member, *args)
        return _parse_dict(raw)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _mint_operator_token(self, *, operation: str) -> str:
        """Mint a signed OperatorToken for a proxied mutating verb."""
        if self._minter is None or self._operator_id is None:
            try:
                from hermes.shell_server.security.secrets import SecretsVault  # noqa: PLC0415
                from hermes.shell_server.security.operator_token import (  # noqa: PLC0415
                    OperatorTokenMinter,
                )
                from hermes.shell_server.chat.dbus_control_plane_adapter import (  # noqa: PLC0415
                    _resolve_operator_uid,
                )

                key = SecretsVault().derive_subkey(label="operator-token")
                self._minter = OperatorTokenMinter(signing_key=key)
                self._operator_id = str(UUID(int=_resolve_operator_uid()))
            except Exception as exc:  # noqa: BLE001
                raise AgentUnavailable(
                    f"operator token unavailable (master.key/keygen): {exc}"
                ) from exc
        return self._minter.mint(operator_id=self._operator_id, operation=operation)

    async def _call(self, member: str, *args: Any) -> Any:
        """Open a D-Bus connection, call member, disconnect. Raises AgentUnavailable."""
        try:
            from dbus_fast.aio import MessageBus  # noqa: PLC0415
            from dbus_fast.errors import DBusError  # noqa: PLC0415
            from dbus_fast import BusType  # noqa: PLC0415
        except ImportError as exc:
            raise AgentUnavailable("dbus-fast not available in this environment") from exc

        bus = None
        try:
            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
            introspection = await bus.introspect(_WELL_KNOWN_NAME, _OBJECT_PATH)
            proxy = bus.get_proxy_object(_WELL_KNOWN_NAME, _OBJECT_PATH, introspection)
            iface = proxy.get_interface(_INTERFACE_NAME)
            fn = getattr(iface, f"call_{member}", None)
            if fn is None:
                raise AgentUnavailable(f"daemon does not expose verb {member!r}")
            return await fn(*args)
        except AgentUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            _translate_dbus_error(exc, member)
        finally:
            if bus is not None and getattr(bus, "connected", False):
                bus.disconnect()


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------


def _parse_list(raw: Any) -> list[dict]:
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        return raw
    try:
        result = json.loads(raw)
        return result if isinstance(result, list) else []
    except (TypeError, json.JSONDecodeError):
        return []


def _parse_dict(raw: Any) -> dict:
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        result = json.loads(raw)
        return result if isinstance(result, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _translate_dbus_error(exc: Exception, member: str) -> None:
    """Translate a D-Bus exception into the appropriate Python exception.

    Branches on the structured D-Bus error name before falling back to the
    error message string, so that daemon validation errors reach the caller
    as 4xx rather than collapsing everything into 503 agent_unavailable.

    Raises:
        HTTPException(422): daemon rejected the request as invalid input.
        HTTPException(401): daemon rejected the request as unauthorized.
        AgentUnavailable: genuine unavailability; callers render 503.
    """
    # dbus-fast exposes the error name on DBusError as .type or .text prefix.
    err_name: str = ""
    if hasattr(exc, "type") and exc.type:  # type: ignore[union-attr]
        err_name = str(exc.type)
    elif hasattr(exc, "text") and exc.text:  # type: ignore[union-attr]
        err_name = str(exc.text).split(":")[0].strip()

    if err_name == "org.hermes.Error.InvalidInput":
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_input", "message": str(exc)},
        ) from exc

    if err_name == "org.hermes.Error.Unauthorized":
        raise HTTPException(
            status_code=401,
            detail={"code": "unauthorized", "message": str(exc)},
        ) from exc

    # Legacy string-match kept as a secondary guard for older daemon builds
    # that may not emit the structured error name.
    err_str = str(exc).lower()
    if "notauthorized" in err_str or "accessdenied" in err_str:
        raise HTTPException(
            status_code=401,
            detail={"code": "unauthorized", "message": str(exc)},
        ) from exc

    raise AgentUnavailable(
        f"org.hermes.Runtime1.{member} unavailable: {exc}"
    ) from exc
