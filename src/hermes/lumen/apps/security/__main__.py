"""hermes.lumen.apps.security — Seguridad/Audit standalone app.

Entry point: ``python3 -m hermes.lumen.apps.security``

D-Bus verbs used:
  - ListRecentTasks(limit: int) → JSON   (recent agent activity, polled 6 s)
  - ListPending(limit: int)     → JSON   (HITL pending queue)
  - GetAuditChainHead()         → JSON   (audit chain integrity indicator;
                                          declared dependency — backend emits
                                          the raw result via listLoaded("audit_head", …)
                                          when the verb exists; honest empty state if not)
  - ApproveAction(proposal_id)           (HITL approve — sender_uid auth)
  - RejectAction(proposal_id, reason)    (HITL reject — sender_uid auth)
  - GrantConsent(capability, scope)      (FR-013 — operator consent panel)
  - RevokeConsent(capability)            (FR-013 — operator consent panel)
  - ListConsents()                       (FR-013 — read active consents)

Supervision + HITL governance only. No effectors. No broker calls. No HTTP.

DEPENDENCY NOTE:
  GetAuditChainHead is declared as a required D-Bus verb (T047 backend task).
  Until it exists the app shows an honest "sin datos" state in the chain head
  section — never a mock value.

  Consent verbs (GrantConsent / RevokeConsent / ListConsents) are fully wired
  in spec 014 increment 3 (FR-013). The panel shows honest "sin consents activos"
  when the list is empty — never a mock state.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, QUrl, Slot, Signal

_HERE = Path(__file__).resolve().parent
_QML_DIR = _HERE.parent.parent / "qml"


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "wayland;xcb")

    _src = str(_QML_DIR.parent.parent.parent)
    if _src not in sys.path:
        sys.path.insert(0, _src)

    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlApplicationEngine

    from hermes.lumen.apps._base.app_main import AppBackend

    class SecurityBackend(AppBackend):
        """AppBackend + audit chain head probing + FR-013 consent control.

        Adds:
          - GetAuditChainHead probing (T047 dependency): honest empty state if
            the verb is not yet present on the daemon.
          - GrantConsent / RevokeConsent / ListConsents (spec 014 inc. 3, FR-013):
            the operator panel for granting/revoking capability consent. Authorship
            is resolved server-side from sender_uid (CWE-862 — never payload).
        """

        # Emitted when the consent list is refreshed: (json_array_str)
        consentsLoaded = Signal(str)
        # Emitted with the result of a grant/revoke: (capability, ok, message)
        consentActionResult = Signal(str, bool, str)

        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)

            # Audit chain head: probe at startup and every 30 s.
            self._audit_timer = QTimer(self)
            self._audit_timer.timeout.connect(self._probe_audit_head)
            self._audit_timer.start(30_000)
            self.connectedChanged.connect(self._on_connected_for_audit)

            # Consent list: probe at startup and every 15 s (FR-013 liveness).
            self._consent_timer = QTimer(self)
            self._consent_timer.timeout.connect(self._probe_consents)
            self._consent_timer.start(15_000)
            self.connectedChanged.connect(self._on_connected_for_consents)

        # ------------------------------------------------------------------
        # Audit chain head probing
        # ------------------------------------------------------------------

        def _on_connected_for_audit(self) -> None:
            if self.connected:
                self._probe_audit_head()

        def _probe_audit_head(self) -> None:
            if not self.connected:
                return
            self._client._call(
                "GetAuditChainHead",
                (),
                lambda raw: self.listLoaded.emit("audit_head", raw if raw else "{}"),
                lambda _err: self.listLoaded.emit("audit_head", "{}"),
            )

        # ------------------------------------------------------------------
        # FR-013 consent probing (read-only refresh)
        # ------------------------------------------------------------------

        def _on_connected_for_consents(self) -> None:
            if self.connected:
                self._probe_consents()

        def _probe_consents(self) -> None:
            if not self.connected:
                return
            self._client.list_consents(
                on_reply=lambda raw: self.consentsLoaded.emit(raw if raw else "[]"),
                on_error=lambda _err: self.consentsLoaded.emit("[]"),
            )

        # ------------------------------------------------------------------
        # FR-013 consent mutators (exposed as QML Slots)
        # Authorship = sender_uid resolved by the daemon (CWE-862).
        # ------------------------------------------------------------------

        @Slot(str, str)
        def grantConsent(self, capability: str, scope: str) -> None:
            """GrantConsent via D-Bus. Authorship = sender_uid (CWE-862).

            capability: Capability enum value (e.g. "documents").
            scope: "session" | "once" | "persistent".
            On success: refreshes the consent list and emits consentActionResult.
            On error: emits consentActionResult(capability, False, error_msg).
            """
            def _on_reply(raw: str | None) -> None:
                try:
                    data = json.loads(raw) if raw else {}
                except (ValueError, TypeError):
                    data = {}
                if "error" in data:
                    self.consentActionResult.emit(capability, False, str(data["error"]))
                else:
                    self._probe_consents()
                    self.consentActionResult.emit(capability, True, "")

            def _on_error(err: str) -> None:
                self.consentActionResult.emit(capability, False, err or "Error al otorgar consent")

            self._client.grant_consent(capability, scope, _on_reply, _on_error)

        @Slot(str)
        def revokeConsent(self, capability: str) -> None:
            """RevokeConsent via D-Bus. Authorship = sender_uid (CWE-862).

            capability: Capability enum value.
            On success: refreshes the consent list and emits consentActionResult.
            On error: emits consentActionResult(capability, False, error_msg).
            """
            def _on_reply(raw: str | None) -> None:
                try:
                    data = json.loads(raw) if raw else {}
                except (ValueError, TypeError):
                    data = {}
                if "error" in data:
                    self.consentActionResult.emit(capability, False, str(data["error"]))
                else:
                    self._probe_consents()
                    revoked = bool(data.get("revoked", False))
                    self.consentActionResult.emit(capability, revoked, "")

            def _on_error(err: str) -> None:
                self.consentActionResult.emit(capability, False, err or "Error al revocar consent")

            self._client.revoke_consent(capability, _on_reply, _on_error)

        @Slot()
        def refreshConsents(self) -> None:
            """Manual refresh of the consent list."""
            self._probe_consents()

    from PySide6.QtGui import QGuiApplication

    app = QGuiApplication(sys.argv)
    app.setApplicationName("Seguridad — Hermes")
    app.setOrganizationName("hermes")

    backend = SecurityBackend(
        auto_load_keys=["recent_tasks", "pending"],
        poll_interval_ms=6_000,
    )

    engine = QQmlApplicationEngine()
    ctx = engine.rootContext()
    ctx.setContextProperty("backend", backend)
    ctx.setContextProperty("qmlBaseDir", str(_QML_DIR))

    engine.addImportPath(str(_QML_DIR))

    engine.load(QUrl.fromLocalFile(str(_HERE / "SecurityWindow.qml")))
    if not engine.rootObjects():
        return 1

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
