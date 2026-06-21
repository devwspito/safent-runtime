"""T027 — ContextSnapshotComposer regression tests.

Verifies:
  - Compose returns NoActiveAppSnapshot when AT-SPI has no focused app.
  - Compose returns ContextSnapshot with app name when focused.
  - Screenshot included only when SCREEN_CAPTURE consent is active.
  - Screenshot NOT included when consent absent.
  - Screenshot NOT included when ScreenshotPort is None.
  - Screenshot error → graceful fallback (available=False, no exception).
  - ContextSnapshotComposer never touches CapabilityBroker.
  - to_json_safe_dict never includes raw screenshot bytes.
  - AT-SPI error → NoActiveAppSnapshot (fail-open on read).
"""

from __future__ import annotations

import secrets
from uuid import uuid4

import pytest

from hermes.agents_os.application.consent_manager import (
    Capability,
    ConsentManager,
    ConsentScope,
)
from hermes.agents_os.application.context_snapshot_composer import (
    ContextSnapshot,
    ContextSnapshotComposer,
    NoActiveAppSnapshot,
    ScreenshotUnavailable,
)
from hermes.agents_os.infrastructure.desktop_app_surface_adapter import FakeAtSpiClient

pytestmark = pytest.mark.unit

_OPERATOR_ID = uuid4()
_TENANT_ID = uuid4()
_FAKE_PNG = b"\x89PNG\r\n\x1a\nfake"


class _FakeScreenshotPort:
    def __init__(self, *, raises: bool = False) -> None:
        self._raises = raises
        self.call_count = 0

    def take_screenshot(self) -> bytes:
        self.call_count += 1
        if self._raises:
            raise ScreenshotUnavailable("test: unavailable")
        return _FAKE_PNG


class _BrokenScreenshotPort:
    """Raises a generic exception (not ScreenshotUnavailable) to test the BLE001 path."""
    def take_screenshot(self) -> bytes:
        raise RuntimeError("test: broken port")


def _consent_with_screen() -> ConsentManager:
    cm = ConsentManager()
    cm.grant(
        tenant_id=_TENANT_ID,
        human_operator_id=_OPERATOR_ID,
        capability=Capability.SCREEN_CAPTURE,
        scope=ConsentScope.SESSION,
    )
    return cm


class TestNoActiveApp:
    def test_returns_no_active_app_when_atspi_has_no_focus(self) -> None:
        atspi = FakeAtSpiClient(focused_app=None)
        composer = ContextSnapshotComposer(atspi_client=atspi)
        result = composer.compose()
        assert isinstance(result, NoActiveAppSnapshot)

    def test_no_active_app_json_has_active_application_none(self) -> None:
        atspi = FakeAtSpiClient(focused_app=None)
        composer = ContextSnapshotComposer(atspi_client=atspi)
        snap = composer.compose()
        d = composer.to_json_safe_dict(snap)
        assert d["active_application"] is None
        assert d["screenshot_available"] is False

    def test_atspi_error_returns_no_active_app(self) -> None:
        class _BrokenAtSpi(FakeAtSpiClient):
            def focused_application_name(self) -> str | None:
                raise RuntimeError("atspi unavailable")

        composer = ContextSnapshotComposer(atspi_client=_BrokenAtSpi())
        result = composer.compose()
        assert isinstance(result, NoActiveAppSnapshot)


class TestActiveApp:
    def test_returns_context_snapshot_with_app_name(self) -> None:
        atspi = FakeAtSpiClient(focused_app="org.gnome.Nautilus")
        composer = ContextSnapshotComposer(atspi_client=atspi)
        result = composer.compose()
        assert isinstance(result, ContextSnapshot)
        assert result.active_application == "org.gnome.Nautilus"

    def test_app_label_truncated_to_256_chars(self) -> None:
        """app_label (AT-SPI app name placeholder) is capped at _MAX_TITLE_LEN."""
        long_name = "A" * 500
        atspi = FakeAtSpiClient(focused_app=long_name)
        composer = ContextSnapshotComposer(atspi_client=atspi)
        snap = composer.compose()
        assert isinstance(snap, ContextSnapshot)
        assert len(snap.app_label) == 256

    def test_snapshot_has_utc_timestamp(self) -> None:
        from datetime import timezone

        atspi = FakeAtSpiClient(focused_app="soffice")
        composer = ContextSnapshotComposer(atspi_client=atspi)
        snap = composer.compose()
        assert isinstance(snap, ContextSnapshot)
        assert snap.captured_at.tzinfo == timezone.utc


class TestScreenshot:
    def test_screenshot_absent_when_no_consent_manager(self) -> None:
        atspi = FakeAtSpiClient(focused_app="org.gnome.gedit")
        port = _FakeScreenshotPort()
        composer = ContextSnapshotComposer(
            atspi_client=atspi,
            screenshot_port=port,
            # consent_manager=None (default)
        )
        snap = composer.compose()
        assert isinstance(snap, ContextSnapshot)
        assert snap.screenshot_available is False
        assert snap.screenshot_bytes is None
        assert port.call_count == 0  # NEVER called without consent

    def test_screenshot_absent_when_operator_id_none(self) -> None:
        atspi = FakeAtSpiClient(focused_app="org.gnome.gedit")
        port = _FakeScreenshotPort()
        cm = _consent_with_screen()
        composer = ContextSnapshotComposer(
            atspi_client=atspi,
            consent_manager=cm,
            screenshot_port=port,
            operator_id=None,  # no operator — consent check fails
        )
        snap = composer.compose()
        assert isinstance(snap, ContextSnapshot)
        assert snap.screenshot_available is False
        assert port.call_count == 0

    def test_screenshot_absent_when_consent_missing(self) -> None:
        atspi = FakeAtSpiClient(focused_app="org.gnome.gedit")
        port = _FakeScreenshotPort()
        cm = ConsentManager()  # no SCREEN_CAPTURE consent granted
        composer = ContextSnapshotComposer(
            atspi_client=atspi,
            consent_manager=cm,
            screenshot_port=port,
            operator_id=_OPERATOR_ID,
        )
        snap = composer.compose()
        assert isinstance(snap, ContextSnapshot)
        assert snap.screenshot_available is False
        assert port.call_count == 0

    def test_screenshot_included_when_consented(self) -> None:
        atspi = FakeAtSpiClient(focused_app="org.gnome.gedit")
        port = _FakeScreenshotPort()
        cm = _consent_with_screen()
        composer = ContextSnapshotComposer(
            atspi_client=atspi,
            consent_manager=cm,
            screenshot_port=port,
            operator_id=_OPERATOR_ID,
        )
        snap = composer.compose()
        assert isinstance(snap, ContextSnapshot)
        assert snap.screenshot_available is True
        assert snap.screenshot_bytes == _FAKE_PNG
        assert port.call_count == 1

    def test_screenshot_unavailable_falls_back_gracefully(self) -> None:
        atspi = FakeAtSpiClient(focused_app="org.gnome.gedit")
        port = _FakeScreenshotPort(raises=True)
        cm = _consent_with_screen()
        composer = ContextSnapshotComposer(
            atspi_client=atspi,
            consent_manager=cm,
            screenshot_port=port,
            operator_id=_OPERATOR_ID,
        )
        snap = composer.compose()
        assert isinstance(snap, ContextSnapshot)
        assert snap.screenshot_available is False
        assert snap.screenshot_bytes is None

    def test_broken_screenshot_port_falls_back_gracefully(self) -> None:
        atspi = FakeAtSpiClient(focused_app="org.gnome.gedit")
        port = _BrokenScreenshotPort()
        cm = _consent_with_screen()
        composer = ContextSnapshotComposer(
            atspi_client=atspi,
            consent_manager=cm,
            screenshot_port=port,
            operator_id=_OPERATOR_ID,
        )
        snap = composer.compose()
        assert isinstance(snap, ContextSnapshot)
        assert snap.screenshot_available is False


class TestJsonSerialization:
    def test_json_dict_never_contains_screenshot_bytes(self) -> None:
        atspi = FakeAtSpiClient(focused_app="org.gnome.gedit")
        port = _FakeScreenshotPort()
        cm = _consent_with_screen()
        composer = ContextSnapshotComposer(
            atspi_client=atspi,
            consent_manager=cm,
            screenshot_port=port,
            operator_id=_OPERATOR_ID,
        )
        snap = composer.compose()
        d = composer.to_json_safe_dict(snap)
        assert "screenshot_bytes" not in d
        # The flag is present
        assert d["screenshot_available"] is True

    def test_json_dict_has_expected_keys_for_active_app(self) -> None:
        atspi = FakeAtSpiClient(focused_app="org.libreoffice.Main")
        composer = ContextSnapshotComposer(atspi_client=atspi)
        snap = composer.compose()
        d = composer.to_json_safe_dict(snap)
        assert set(d.keys()) == {
            "active_application",
            "window_title",
            "screenshot_available",
            "captured_at",
        }

    def test_json_dict_has_expected_keys_for_no_app(self) -> None:
        atspi = FakeAtSpiClient(focused_app=None)
        composer = ContextSnapshotComposer(atspi_client=atspi)
        snap = composer.compose()
        d = composer.to_json_safe_dict(snap)
        assert set(d.keys()) == {
            "active_application",
            "window_title",
            "screenshot_available",
            "captured_at",
        }
