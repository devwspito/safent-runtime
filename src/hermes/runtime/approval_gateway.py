"""approval_gateway — Two-mode security kernel: gateway wiring + AUTO mode.

Bridges the Hermes-native approval system (tools.approval) to the D-Bus
compositor layer.

Responsibilities:
  1. Session-key lifecycle: a stable per-owner key ("cerebro") is set on the
     executor thread BEFORE run_conversation so that register_gateway_notify
     and resolve_gateway_approval operate on the same key.
  2. Gateway notify registration: register_gateway_notify(session_key, cb) is
     called once at daemon startup. The cb emits ApprovalRequested on D-Bus.
  3. AUTO mode persistence: reads/writes a boolean to settings.json.
  4. Per-cycle AUTO application: before run_conversation, enable/disable
     session YOLO based on the persisted AUTO flag.

Design invariants:
  - The hardline floor (detect_hardline_command) is UNCONDITIONAL regardless
    of AUTO mode. AUTO only bypasses the dangerous-pattern / gateway HITL layer.
  - Failure to wire the gateway NEVER makes the system fail-open for dangerous
    commands: any error falls back to fail-CLOSED (gateway blocks the command).
  - All tools.approval imports are lazy: if hermes-agent is absent the module
    loads cleanly (used in CI without the agent installed).
  - No state is stored at module level beyond the settings path and the
    in-flight request registry. Thread-safety is guaranteed by the asyncio
    event loop for D-Bus signal emission.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    pass

logger = logging.getLogger("hermes.runtime.approval_gateway")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SESSION_KEY: str = "cerebro"
_SETTINGS_FILENAME: str = "security_mode.json"

# ---------------------------------------------------------------------------
# Settings persistence (AUTO mode flag)
# ---------------------------------------------------------------------------


def _settings_path() -> Path:
    """Return the path to the security-mode settings file.

    Uses HERMES_HOME (same root used by hermes_cli) so the setting survives
    daemon restarts and is co-located with the rest of the daemon's state.
    """
    hermes_home = os.environ.get("HERMES_HOME", "/var/lib/hermes/hermes-home")
    return Path(hermes_home) / _SETTINGS_FILENAME


def load_auto_mode() -> bool:
    """Return the persisted AUTO mode flag (default: False = Modo Guardado).

    Fail-safe: any read/parse error returns False (conservative default).
    """
    path = _settings_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return bool(data.get("auto_mode", False))
    except FileNotFoundError:
        return False
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "hermes.approval_gateway.load_auto_mode_failed: %s — defaulting to False",
            exc,
        )
        return False


def save_auto_mode(enabled: bool) -> None:
    """Persist the AUTO mode flag to disk.

    Raises OSError if the settings directory is not writable (caller must
    catch and surface as a D-Bus error).
    """
    path = _settings_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data: dict = {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            pass
        data["auto_mode"] = bool(enabled)
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        logger.info(
            "hermes.approval_gateway.auto_mode_saved: enabled=%s",
            enabled,
        )
    except OSError as exc:
        logger.error(
            "hermes.approval_gateway.save_auto_mode_failed: %s",
            exc,
        )
        raise


# ---------------------------------------------------------------------------
# In-flight approval request registry
# ---------------------------------------------------------------------------

# Maps request_id (str) → session_key (str).  Entries are added by the
# gateway notify callback and removed by ResolveApproval.
_pending_approvals: dict[str, str] = {}


def register_pending(request_id: str, session_key: str) -> None:
    """Track an in-flight approval request."""
    _pending_approvals[request_id] = session_key


def pop_pending(request_id: str) -> str | None:
    """Remove and return the session_key for a request, or None if not found."""
    return _pending_approvals.pop(request_id, None)


# ---------------------------------------------------------------------------
# Session-key helpers
# ---------------------------------------------------------------------------


def apply_auto_mode_for_cycle() -> None:
    """Enable or disable session YOLO for the current cycle.

    Called from the executor thread INSIDE _run_conversation_with_cdp, AFTER
    set_current_session_key has been called, so the session key is already set.

    YOLO is enabled ONLY when AUTO mode is on AND the owner has turned OFF
    "MFA on dangers" (the escape hatch). The danger gate wins over AUTO:

      AUTO ON  + mfa_on_dangers ON (default) → session_yolo DISABLED: gateway HITL
               stays engaged even in autonomous mode → dangerous native commands
               (terminal/write_file/execute_code) surface the ApprovalRequested card
               and PAUSE for owner MFA. Safe reads still run free (the gateway only
               cards dangerous patterns). This is "DANGERS piden MFA sí o sí".
      AUTO ON  + mfa_on_dangers OFF → session_yolo ENABLED: full autonomy, dangers
               run free (owner accepted responsibility via the UI alert).
      AUTO OFF → session_yolo DISABLED: gateway HITL engaged regardless.

    Fail-soft: any approval API error is logged but does not raise. Fail-SAFE on the
    danger gate: if the flag can't be read it defaults to ON (gate stays up). The
    hardline floor (security_hook) is independent of session YOLO either way.
    """
    try:
        from tools.approval import (  # noqa: PLC0415
            enable_session_yolo,
            disable_session_yolo,
        )
    except ImportError:
        logger.debug(
            "hermes.approval_gateway.apply_auto_mode: tools.approval unavailable — skip"
        )
        return

    auto_mode = load_auto_mode()
    mfa_on_dangers = _load_mfa_on_dangers()
    yolo = auto_mode and not mfa_on_dangers
    try:
        if yolo:
            enable_session_yolo(_SESSION_KEY)
            logger.info(
                "hermes.approval_gateway.cycle.yolo_on: full autonomy "
                "(auto_mode=on, mfa_on_dangers=OFF — owner-accepted)"
            )
        else:
            disable_session_yolo(_SESSION_KEY)
            logger.debug(
                "hermes.approval_gateway.cycle.yolo_off: gateway HITL engaged "
                "(auto_mode=%s, mfa_on_dangers=%s) — dangers pause for owner MFA",
                auto_mode, mfa_on_dangers,
            )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "hermes.approval_gateway.apply_auto_mode_failed: %s — "
            "falling back to fail-closed (dangerous commands will be blocked)",
            exc,
        )


def _load_mfa_on_dangers() -> bool:
    """Read the owner's MFA-on-dangers flag (fail-SAFE to True = gate up).

    Lazy import keeps approval_gateway loadable without the capabilities layer
    (CI / minimal contexts), and never fails open: a missing flag or any error
    keeps the danger gate engaged.
    """
    try:
        from hermes.capabilities.tool_policy import ToolPolicyStore  # noqa: PLC0415
        return ToolPolicyStore().mfa_on_dangers()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "hermes.approval_gateway.mfa_on_dangers_read_failed: %s — defaulting ON",
            exc,
        )
        return True


def set_session_key_for_thread() -> None:
    """Set the stable session key on the current executor thread.

    Must be called BEFORE run_conversation in the executor thread.
    Fail-soft: logs and continues if tools.approval is unavailable (CI).
    """
    try:
        from tools.approval import set_current_session_key  # noqa: PLC0415
        set_current_session_key(_SESSION_KEY)
    except ImportError:
        logger.debug(
            "hermes.approval_gateway.set_session_key: tools.approval unavailable — skip"
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "hermes.approval_gateway.set_session_key_failed: %s", exc
        )


def clear_session_key_for_thread() -> None:
    """Reset the session key after run_conversation returns.

    Fail-soft: logs and continues.
    """
    try:
        from tools.approval import set_current_session_key  # noqa: PLC0415
        set_current_session_key(None)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Gateway notify registration
# ---------------------------------------------------------------------------


def register_gateway_notify_callback(
    emit_approval_requested: Callable[[str], None],
) -> None:
    """Register the D-Bus emit callback with the native Hermes gateway.

    Called once at daemon startup (after the D-Bus adapter bus is live and
    the emitter is injectable).

    The callback `emit_approval_requested(payload_json: str)` is called by
    the Hermes gateway when a dangerous command needs owner approval. It runs
    ON THE AGENT THREAD (executor) — it must NOT block (the blocking wait is
    inside Hermes's _await_gateway_decision; the callback only fires the
    D-Bus signal).

    Fail-soft: any import or registration error is logged but does not crash
    the daemon. If registration fails, dangerous commands are fail-CLOSED
    (the gateway blocks them permanently until they are resolved via some
    other path, or the operator restarts in AUTO mode).
    """
    try:
        from tools.approval import register_gateway_notify  # noqa: PLC0415
    except ImportError:
        logger.warning(
            "hermes.approval_gateway.register: tools.approval unavailable — "
            "ApprovalRequested signal will NOT be emitted (hermes-agent not installed)"
        )
        return

    def _cb(approval_data: dict) -> None:
        request_id = str(uuid.uuid4())
        command = approval_data.get("command", "")
        description = approval_data.get("description", "")
        pattern_keys = approval_data.get("pattern_keys", [])

        register_pending(request_id, _SESSION_KEY)

        payload = json.dumps({
            "request_id": request_id,
            "command": command,
            "description": description,
            "pattern_keys": pattern_keys,
        }, ensure_ascii=False)

        logger.info(
            "hermes.approval_gateway.notify: request_id=%s command=%r",
            request_id,
            command[:120],
        )
        try:
            emit_approval_requested(payload)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "hermes.approval_gateway.emit_failed: request_id=%s error=%s — "
                "auto-denying (fail-closed)",
                request_id,
                exc,
            )
            _auto_deny_on_emit_failure(request_id)

    try:
        register_gateway_notify(_SESSION_KEY, _cb)
        logger.info(
            "hermes.approval_gateway.registered: session_key=%r — "
            "ApprovalRequested signal wired to D-Bus compositor",
            _SESSION_KEY,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "hermes.approval_gateway.register_failed: %s — "
            "gateway HITL card NOT wired (dangerous commands will be blocked)",
            exc,
        )


def _auto_deny_on_emit_failure(request_id: str) -> None:
    """Resolve a pending request as 'deny' if the D-Bus emit failed.

    Fail-soft: logs any error from resolve_gateway_approval.
    """
    session_key = pop_pending(request_id)
    if session_key is None:
        return
    try:
        from tools.approval import resolve_gateway_approval  # noqa: PLC0415
        resolve_gateway_approval(session_key, "deny")
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "hermes.approval_gateway.auto_deny_failed: request_id=%s error=%s",
            request_id,
            exc,
        )


# ---------------------------------------------------------------------------
# Resolve an approval from the D-Bus compositor
# ---------------------------------------------------------------------------

_VALID_CHOICES: frozenset[str] = frozenset({"once", "session", "always", "deny"})


def resolve_approval(request_id: str, choice: str) -> str:
    """Resolve a pending gateway approval from the compositor.

    Called by the D-Bus ResolveApproval method handler.

    Args:
        request_id: UUID string from the ApprovalRequested payload.
        choice: one of "once", "session", "always", "deny".

    Returns:
        JSON string: {"ok": true} or {"ok": false, "error": reason}.

    Fail-closed: unknown choice → "deny".
    """
    if choice not in _VALID_CHOICES:
        logger.warning(
            "hermes.approval_gateway.resolve.invalid_choice: choice=%r — defaulting to deny",
            choice,
        )
        choice = "deny"

    session_key = pop_pending(request_id)
    if session_key is None:
        return json.dumps({
            "ok": False,
            "error": f"request_id {request_id!r} not found (expired or already resolved)",
        })

    try:
        from tools.approval import resolve_gateway_approval  # noqa: PLC0415
        resolve_gateway_approval(session_key, choice)
        logger.info(
            "hermes.approval_gateway.resolved: request_id=%s choice=%s",
            request_id,
            choice,
        )
        return json.dumps({"ok": True})
    except ImportError:
        return json.dumps({"ok": False, "error": "tools.approval unavailable"})
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "hermes.approval_gateway.resolve_failed: request_id=%s error=%s",
            request_id,
            exc,
        )
        return json.dumps({"ok": False, "error": str(exc)})
