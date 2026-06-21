"""ContextSnapshotComposer — T027 (spec 014-agentic-desktop).

Read-only composition of what the active desktop app is. Used by
RequestContextSnapshot on org.hermes.Runtime1 so the overlay can
show the agent what the human has in front.

RULES (from threat-model + Constitution III):
- Read-only: NEVER touches CapabilityBroker or any effector.
- Screenshot only when Capability.SCREEN_CAPTURE consent is active.
- PII tokenized before the snapshot leaves this module (caller is
  responsible for passing the snapshot to DefaultPIITokenizer before
  any LLM boundary — this module marks PII-eligible fields).
- Content of screenshot bytes NEVER persisted and NEVER logged.
- If no app is active, returns a snapshot that says so — no invented
  context.

Dependency injection strategy:
- AtSpiClient: the focused_application_name() call — real impl
  (LibAtSpiClient) is injected by the daemon; CI uses FakeAtSpiClient.
- ConsentManager: to gate screenshot inclusion under SCREEN_CAPTURE.
- ScreenshotPort: abstraction over session-input.sock so callers
  can stub it in tests without importing the real socket adapter.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

if TYPE_CHECKING:
    from hermes.agents_os.application.consent_manager import ConsentManager
    from hermes.agents_os.infrastructure.desktop_app_surface_adapter import AtSpiClient

logger = logging.getLogger("hermes.agents_os.context_snapshot_composer")

# Hard cap on window-title length exposed in the snapshot (CWE-116/PII).
_MAX_TITLE_LEN = 256


class ScreenshotPort(Protocol):
    """Minimal port over /run/hermes/session-input.sock screenshot capability.

    The real adapter wraps the MutterRemoteDesktop / PipeWire path already
    implemented in hermes-session-input. The fake returns a fixed sentinel.
    Never returns raw PII bytes in production — the caller tokenizes before
    handing to LLM.
    """

    def take_screenshot(self) -> bytes:
        """Capture the current screen.

        Returns raw PNG bytes. May raise ScreenshotUnavailable.
        """
        ...


class ScreenshotUnavailable(RuntimeError):
    """Screenshot cannot be taken (service unavailable or consent absent)."""


@dataclass(frozen=True, slots=True)
class ContextSnapshot:
    """Read-only snapshot of the focused desktop context.

    Fields are PII-eligible — tokenize before crossing any LLM boundary
    (Constitution III). screenshot_available signals whether bytes were
    captured; the actual bytes are held ephemerally by callers, never here.

    active_application:
        D-Bus well-known name or human-readable name from AT-SPI.
        Empty string when no app is focused (not None — makes serialization
        unconditional).
    app_label:
        Truncated to _MAX_TITLE_LEN. IMPORTANT: this is the AT-SPI application
        name used as a label placeholder — NOT a real window title. AT-SPI does
        not expose a direct window-title query in the current minimal interface.
        Serialized as "window_title" in the D-Bus JSON contract (wire name kept
        stable; semantic limit documented here so callers don't infer a real title).
        May contain PII (app name visible on screen); mark for tokenization before LLM.
    screenshot_available:
        True only when consent for SCREEN_CAPTURE was active at capture time
        AND the screenshot port returned bytes without error.
    screenshot_bytes:
        Raw PNG. Ephemeral — not persisted, not logged. None when unavailable.
    captured_at:
        UTC timestamp of composition.
    """

    active_application: str
    app_label: str
    screenshot_available: bool
    screenshot_bytes: bytes | None
    captured_at: datetime


@dataclass(frozen=True, slots=True)
class NoActiveAppSnapshot:
    """Returned when AT-SPI reports no focused application.

    Distinct type so callers can pattern-match without inspecting
    active_application == "".
    """

    captured_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))

    def to_json_safe_dict(self) -> dict:
        return {
            "active_application": None,
            "window_title": None,
            "screenshot_available": False,
            "captured_at": self.captured_at.isoformat(),
        }


class ContextSnapshotComposer:
    """Compose a read-only context snapshot for the overlay.

    The composer is a pure application-layer service:
    - It reads from AT-SPI (focused app) and optionally from the
      screenshot port (gated by SCREEN_CAPTURE consent).
    - It NEVER writes to the broker, NEVER persists, NEVER logs content.

    Wiring (ctor injection):
        atspi_client:       AtSpiClient (FakeAtSpiClient in tests)
        consent_manager:    ConsentManager (None → screenshot always denied)
        screenshot_port:    ScreenshotPort | None (None → screenshot denied)
        operator_id:        UUID of the current human operator (for consent check)
    """

    def __init__(
        self,
        *,
        atspi_client: AtSpiClient,
        consent_manager: ConsentManager | None = None,
        screenshot_port: ScreenshotPort | None = None,
        operator_id: UUID | None = None,
    ) -> None:
        self._atspi = atspi_client
        self._consent = consent_manager
        self._screenshot_port = screenshot_port
        self._operator_id = operator_id

    def compose(self) -> ContextSnapshot | NoActiveAppSnapshot:
        """Return the current snapshot. Read-only; never raises on missing data.

        Returns NoActiveAppSnapshot when AT-SPI reports no focused app.
        Returns ContextSnapshot otherwise, with screenshot if consented.
        """
        app_name = self._read_focused_app()
        if app_name is None:
            logger.debug("hermes.context_snapshot.no_active_app")
            return NoActiveAppSnapshot()

        label = self._read_app_label(app_name)
        screenshot_bytes, screenshot_available = self._try_screenshot()

        return ContextSnapshot(
            active_application=app_name,
            app_label=label,
            screenshot_available=screenshot_available,
            screenshot_bytes=screenshot_bytes,
            captured_at=datetime.now(tz=UTC),
        )

    def to_json_safe_dict(
        self, snapshot: ContextSnapshot | NoActiveAppSnapshot
    ) -> dict:
        """Serialize to a dict safe for D-Bus JSON transport.

        Screenshot bytes are stripped (never cross D-Bus as raw bytes).
        The caller is responsible for PII tokenization before any LLM boundary.
        """
        if isinstance(snapshot, NoActiveAppSnapshot):
            return snapshot.to_json_safe_dict()

        return {
            "active_application": snapshot.active_application,
            # Wire key kept as "window_title" for D-Bus contract stability.
            # Semantic: this is the app label (AT-SPI name), NOT a real window title.
            "window_title": snapshot.app_label,
            "screenshot_available": snapshot.screenshot_available,
            # bytes never serialized — flag only
            "captured_at": snapshot.captured_at.isoformat(),
        }

    # ------------------------------------------------------------------
    # Private — read helpers (all fail-open: errors produce empty/None)
    # ------------------------------------------------------------------

    def _read_focused_app(self) -> str | None:
        try:
            return self._atspi.focused_application_name()
        except Exception:  # noqa: BLE001
            logger.warning("hermes.context_snapshot.atspi_error", exc_info=False)
            return None

    def _read_app_label(self, app_name: str) -> str:
        # AT-SPI does not expose a direct window-title query in the current
        # minimal AtSpiClient interface. Returns the truncated app name as a
        # label placeholder. Named app_label (not window_title) to make the
        # semantic limit explicit — callers should not infer a real window title.
        return app_name[:_MAX_TITLE_LEN]

    def _try_screenshot(self) -> tuple[bytes | None, bool]:
        """Attempt a screenshot if consented. Returns (bytes|None, available)."""
        if not self._screen_capture_consented():
            return None, False
        if self._screenshot_port is None:
            return None, False
        try:
            png_bytes = self._screenshot_port.take_screenshot()
            return png_bytes, True
        except ScreenshotUnavailable:
            logger.debug("hermes.context_snapshot.screenshot_unavailable")
            return None, False
        except Exception:  # noqa: BLE001
            logger.warning(
                "hermes.context_snapshot.screenshot_error", exc_info=False
            )
            return None, False

    def _screen_capture_consented(self) -> bool:
        """True iff SCREEN_CAPTURE consent is active for the current operator."""
        if self._consent is None or self._operator_id is None:
            return False
        from hermes.agents_os.application.consent_manager import (  # noqa: PLC0415
            Capability,
            ConsentDenied,
        )
        try:
            self._consent.assert_active(
                human_operator_id=self._operator_id,
                capability=Capability.SCREEN_CAPTURE,
            )
            return True
        except ConsentDenied:
            return False
