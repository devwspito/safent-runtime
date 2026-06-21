"""hermes.lumen.dbus_client.runtime1_client — async D-Bus client for org.hermes.Runtime1.

Thin transport wrappers extracted from lumen/__main__.py::Backend._dbus_call.
All callers (overlay, capability apps) import this module; they do NOT
duplicate the connection plumbing.

Design rules (non-negotiable):
  - Transport only. No business logic, no caching, no state.
  - authorship is the bus sender_uid — never passed in args (CWE-862).
  - Async (QDBusPendingCallWatcher): never blocks the GUI thread.
  - Callers receive results via callbacks, consistent with Backend._dbus_call.

Usage::

    client = Runtime1Client(parent=some_qobject)
    client.enqueue(
        trigger_kind="chat_message",
        text="hello",
        conversation_id="<uuid>",
        priority=0,
        on_reply=lambda task_id, stream_path: ...,
        on_error=lambda err: ...,
    )
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QObject

# D-Bus constants — match lumen/__main__.py exactly.
_DBUS_SERVICE = "org.hermes.Runtime"
_DBUS_PATH = "/org/hermes/Runtime"
_DBUS_IFACE = "org.hermes.Runtime1"
_DBUS_IFACE_DESKTOP = "org.hermes.Runtime1"  # Desktop methods live on same iface until T017


class Runtime1Client(QObject):
    """Async D-Bus transport over org.hermes.Runtime1 (system bus).

    Owns the QDBusPendingCallWatcher lifetime: watchers are reparented to
    self so they are cleaned up when the client is destroyed.

    All public methods are fire-and-forget with explicit callbacks.
    """

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)

    # ------------------------------------------------------------------
    # Internal transport (extracted verbatim from Backend._dbus_call)
    # ------------------------------------------------------------------

    def _call(
        self,
        member: str,
        args: tuple,
        on_reply: Callable,
        on_error: Callable | None = None,
        *,
        multi: bool = False,
    ) -> None:
        """Async D-Bus call on org.hermes.Runtime1.

        Mirrors Backend._dbus_call exactly so the overlay and apps can
        reuse the same plumbing without duplication.

        Args:
            member:   D-Bus method name on _DBUS_IFACE.
            args:     Positional arguments passed to setArguments (list).
            on_reply: Callback(raw) or Callback(list) when multi=True.
            on_error: Optional callback(err_str) on D-Bus error reply.
            multi:    If True, on_reply receives the full argument list
                      (used for methods that return multiple out params,
                       e.g. Enqueue → (task_id, stream_path)).
        """
        from PySide6.QtDBus import (  # noqa: PLC0415
            QDBusConnection,
            QDBusMessage,
            QDBusPendingCallWatcher,
        )

        msg = QDBusMessage.createMethodCall(
            _DBUS_SERVICE, _DBUS_PATH, _DBUS_IFACE, member
        )
        if args:
            msg.setArguments(list(args))

        pending = QDBusConnection.systemBus().asyncCall(msg)
        watcher = QDBusPendingCallWatcher(pending, self)

        def _finished(w: QDBusPendingCallWatcher) -> None:
            reply = w.reply()
            if reply.type() == QDBusMessage.MessageType.ErrorMessage:
                if on_error is not None:
                    on_error(reply.errorMessage() or "")
            else:
                a = reply.arguments()
                on_reply(list(a) if multi else (a[0] if a else None))
            w.deleteLater()

        watcher.finished.connect(_finished)

    # ------------------------------------------------------------------
    # Chat / queue
    # ------------------------------------------------------------------

    def enqueue(
        self,
        *,
        trigger_kind: str,
        text: str,
        conversation_id: str,
        priority: int = 0,
        on_reply: Callable[[str, str], None],
        on_error: Callable[[str], None],
    ) -> None:
        """Enqueue a WorkItem via D-Bus Enqueue (daemon).

        GATE 0 / M2 — zero HTTP. Identical semantics to Backend.send().
        The overlay calls this; it NEVER calls run_cycle (Const §0.1).

        Args:
            trigger_kind:    Workflow kind, e.g. "chat_message".
            text:            User-supplied text payload.
            conversation_id: UUID string (non-empty).
            priority:        Integer priority (0 = normal).
            on_reply:        Called with (task_id, stream_path) on success.
            on_error:        Called with error string on D-Bus error.
        """
        import hashlib as _hashlib  # noqa: PLC0415
        dedup_key = f"chat:{conversation_id}:{_hashlib.sha256(text.encode()).hexdigest()[:16]}"

        def _on_multi(rv: list) -> None:
            task_id = rv[0] if rv and len(rv) > 0 else ""
            stream_path = rv[1] if rv and len(rv) > 1 else ""
            on_reply(task_id, stream_path)

        self._call(
            "Enqueue",
            (trigger_kind, text, priority, dedup_key, conversation_id, ""),
            _on_multi,
            on_error,
            multi=True,
        )

    # ------------------------------------------------------------------
    # Health / status
    # ------------------------------------------------------------------

    def get_status(
        self,
        on_reply: Callable[[str | None], None],
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        """GetActiveProvider — lightweight liveness probe.

        Returns the raw JSON string of the active provider (or None).
        Used to confirm the daemon is reachable before showing the overlay.
        """
        self._call("GetActiveProvider", (), on_reply, on_error)

    def healthz(
        self,
        on_reply: Callable[[bool], None],
    ) -> None:
        """Probe daemon liveness and report True/False.

        on_reply is called with True if the daemon responds to D-Bus,
        False on any error (unreachable, timeout, etc.).
        """
        self._call(
            "GetActiveProvider",
            (),
            lambda _raw: on_reply(True),
            lambda _err: on_reply(False),
        )

    # ------------------------------------------------------------------
    # HITL — approve / reject
    # ------------------------------------------------------------------

    def approve_action(
        self,
        proposal_id: str,
        on_reply: Callable[[str | None], None],
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        """ApproveAction (D-Bus) — resolves a pending HITL gate.

        Returns the single-use approval token; the daemon re-dispatches
        the pending proposal. authorship = sender_uid (CWE-862).
        """
        self._call("ApproveAction", (proposal_id,), on_reply, on_error)

    def reject_action(
        self,
        proposal_id: str,
        reason: str,
        on_reply: Callable[[str | None], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        """RejectAction (D-Bus) — cancels a pending HITL gate."""
        self._call(
            "RejectAction",
            (proposal_id, reason),
            on_reply or (lambda _: None),
            on_error,
        )

    # ------------------------------------------------------------------
    # List methods (read-only supervision)
    # ------------------------------------------------------------------

    def list_recent_tasks(
        self,
        limit: int = 50,
        on_reply: Callable[[str | None], None] = lambda _: None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        """ListRecentTasks (D-Bus) → JSON array string."""
        self._call("ListRecentTasks", (limit,), on_reply, on_error)

    def list_configured_tasks(
        self,
        limit: int = 50,
        on_reply: Callable[[str | None], None] = lambda _: None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        """ListConfiguredTasks (D-Bus) → JSON array string."""
        self._call("ListConfiguredTasks", (limit,), on_reply, on_error)

    def list_pending(
        self,
        limit: int = 50,
        on_reply: Callable[[str | None], None] = lambda _: None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        """ListPending (D-Bus) → JSON array string of pending approvals."""
        self._call("ListPending", (limit,), on_reply, on_error)

    def list_skills(
        self,
        on_reply: Callable[[str | None], None] = lambda _: None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        """ListSkills (D-Bus) → JSON array string."""
        self._call("ListSkills", (), on_reply, on_error)

    def list_agents(
        self,
        on_reply: Callable[[str | None], None] = lambda _: None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        """ListAgents (D-Bus) → JSON array string."""
        self._call("ListAgents", (), on_reply, on_error)

    def list_providers(
        self,
        on_reply: Callable[[str | None], None] = lambda _: None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        """ListProviders (D-Bus) → JSON array string."""
        self._call("ListProviders", (), on_reply, on_error)

    def get_active_provider(
        self,
        on_reply: Callable[[str | None], None] = lambda _: None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        """GetActiveProvider (D-Bus) → JSON object string or None."""
        self._call("GetActiveProvider", (), on_reply, on_error)

    def list_authorized_triggers(
        self,
        on_reply: Callable[[str | None], None] = lambda _: None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        """ListAuthorizedTriggers (D-Bus) → JSON array of authorized trigger configs.

        Read-only. Used by the Integrations app to display connected triggers.
        T047 dependency: may not yet be exposed by the daemon.
        """
        self._call("ListAuthorizedTriggers", (), on_reply, on_error)

    # ------------------------------------------------------------------
    # Desktop-specific (org.hermes.Runtime1 / T017 additions)
    # ------------------------------------------------------------------

    def open_overlay(
        self,
        trigger: str,
        active_app: str,
        on_reply: Callable[[str | None], None] = lambda _: None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        """OpenOverlay (D-Bus) — signals the daemon that the overlay opened.

        Returns an invocation_id handle. Presentation only: does not
        start any agent work (Const §0.1).

        Args:
            trigger:    How the overlay was invoked: "keybind", "indicator", "voice".
            active_app: Best-effort AppRef string from the extension (may be "").
        """
        self._call("OpenOverlay", (trigger, active_app), on_reply, on_error)

    def request_context_snapshot(
        self,
        invocation_id: str,
        include_screenshot: bool,
        on_reply: Callable[[list], None] = lambda _: None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        """RequestContextSnapshot (D-Bus) — read-only view of the active app.

        The daemon composes the snapshot from AT-SPI focus + optional
        screenshot (consent-gated). PII-tokenized. Not persisted.

        on_reply receives a list:
            [active_application, focused_path, window_title_tokenized, screenshot_handle]
        """
        self._call(
            "RequestContextSnapshot",
            (invocation_id, include_screenshot),
            on_reply,
            on_error,
            multi=True,
        )

    # ------------------------------------------------------------------
    # Audit chain (T042 — Security app)
    # ------------------------------------------------------------------

    def get_audit_chain_head(
        self,
        on_reply: Callable[[str | None], None] = lambda _: None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        """GetAuditChainHead (D-Bus) → JSON with head hash + timestamp.

        Read-only audit integrity indicator. Declared T047 dependency:
        if the daemon does not yet expose this verb the on_error callback
        is invoked and callers show an honest "not available" state.
        """
        self._call("GetAuditChainHead", (), on_reply, on_error)

    # ------------------------------------------------------------------
    # Skills governance (T043 — Skills app)
    # ------------------------------------------------------------------

    def promote_skill(
        self,
        skill_id: str,
        on_reply: Callable[[str | None], None] = lambda _: None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        """PromoteSkill (D-Bus) — validated → autonomous. Authorship = sender_uid."""
        self._call("PromoteSkill", (skill_id,), on_reply, on_error)

    def deprecate_skill(
        self,
        skill_id: str,
        on_reply: Callable[[str | None], None] = lambda _: None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        """DeprecateSkill (D-Bus) — autonomous → deprecated. Authorship = sender_uid."""
        self._call("DeprecateSkill", (skill_id,), on_reply, on_error)

    # ------------------------------------------------------------------
    # Memory (T045 — Memory app) — T047 dependency
    # ------------------------------------------------------------------

    def list_memory(
        self,
        limit: int = 50,
        on_reply: Callable[[str | None], None] = lambda _: None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        """ListMemory (D-Bus) → JSON array.

        T047 dependency: declared but not yet implemented in the daemon.
        on_error is called with the D-Bus error string when absent.
        """
        self._call("ListMemory", (limit,), on_reply, on_error)

    def search_memory(
        self,
        query: str,
        on_reply: Callable[[str | None], None] = lambda _: None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        """SearchMemory (D-Bus) → JSON array.

        T047 dependency: declared but not yet implemented in the daemon.
        """
        self._call("SearchMemory", (query,), on_reply, on_error)

    # ------------------------------------------------------------------
    # spec 014 increment 3 — FR-013 operator consent control (D-Bus)
    # GrantConsent / RevokeConsent: mutators — authorship = sender_uid (CWE-862).
    # ListConsents: read-only — no authZ required.
    # human_operator_id ALWAYS resolved server-side from sender_uid.
    # ------------------------------------------------------------------

    def grant_consent(
        self,
        capability: str,
        scope: str,
        on_reply: Callable[[str | None], None] = lambda _: None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        """GrantConsent (D-Bus) — grant capability consent for the calling operator.

        capability: Capability enum value string (e.g. "documents").
        scope: "session" | "once" | "persistent".
        Authorship = sender_uid resolved server-side (CWE-862 — never from payload).
        Returns JSON consent dict on success, {"error": reason} on failure.
        """
        self._call("GrantConsent", (capability, scope), on_reply, on_error)

    def revoke_consent(
        self,
        capability: str,
        on_reply: Callable[[str | None], None] = lambda _: None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        """RevokeConsent (D-Bus) — revoke capability consent for the calling operator.

        capability: Capability enum value string.
        Authorship = sender_uid resolved server-side (CWE-862).
        Returns JSON: {"revoked": true/false, ...consent_fields}.
        """
        self._call("RevokeConsent", (capability,), on_reply, on_error)

    def list_consents(
        self,
        on_reply: Callable[[str | None], None] = lambda _: None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        """ListConsents (D-Bus) → JSON array of active consent dicts.

        Read-only. Scoped to the calling operator by sender_uid server-side.
        Returns [] when no consents are active (honest empty state, never mock).
        """
        self._call("ListConsents", (), on_reply, on_error)
