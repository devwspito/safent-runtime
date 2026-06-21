"""Tests DesktopAppSurfaceAdapter (FR-038 SurfaceKind.DESKTOP_APP)."""

from __future__ import annotations

import pytest

from hermes.agents_os.infrastructure.desktop_app_surface_adapter import (
    AtSpiPath,
    DesktopActionKind,
    DesktopAppSurfaceAdapter,
    FakeAtSpiClient,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def fake_client() -> FakeAtSpiClient:
    return FakeAtSpiClient(
        available_paths={
            "app=org.gnome.Nautilus|win=Files|role=push_button|name=Open|idx=0",
            "app=org.gnome.Nautilus|win=Files|role=text|name=Address|idx=0",
            "app=org.gnome.Calculator|win=Calc|role=push_button|name==|idx=0",
        },
        focused_app="org.gnome.Nautilus",
    )


@pytest.fixture
def adapter(fake_client: FakeAtSpiClient) -> DesktopAppSurfaceAdapter:
    return DesktopAppSurfaceAdapter(
        atspi_client=fake_client,
        allow_apps=frozenset(
            {"org.gnome.Nautilus", "org.gnome.Calculator", "org.gnome.gedit"}
        ),
    )


class TestCapture:
    def test_capture_records_focus(
        self, adapter: DesktopAppSurfaceAdapter
    ) -> None:
        path = AtSpiPath(
            application="org.gnome.Nautilus",
            window="Files",
            role="push_button",
            name="Open",
        )
        captured = adapter.capture(
            action_kind=DesktopActionKind.CLICK,
            accessible_path=path,
            payload={},
        )
        assert captured.action_kind == DesktopActionKind.CLICK
        assert captured.pre_focus_app == "org.gnome.Nautilus"

    def test_capture_app_not_in_allowlist_blocked(
        self, adapter: DesktopAppSurfaceAdapter
    ) -> None:
        path = AtSpiPath(
            application="org.gnome.Boxes",
            window="VMs",
            role="push_button",
            name="Run",
        )
        with pytest.raises(PermissionError):
            adapter.capture(
                action_kind=DesktopActionKind.CLICK,
                accessible_path=path,
                payload={},
            )


class TestReplay:
    def test_replay_click_happy(
        self,
        adapter: DesktopAppSurfaceAdapter,
        fake_client: FakeAtSpiClient,
    ) -> None:
        path = AtSpiPath(
            application="org.gnome.Nautilus",
            window="Files",
            role="push_button",
            name="Open",
        )
        captured = adapter.capture(
            action_kind=DesktopActionKind.CLICK,
            accessible_path=path,
            payload={},
        )
        outcome = adapter.replay(captured)
        assert outcome.success is True
        assert outcome.fallback_used is False
        assert ("click", {"path": path.to_canonical_string(), "double": False}) in fake_client.invocations

    def test_replay_with_fuzzy_fallback(
        self,
        adapter: DesktopAppSurfaceAdapter,
        fake_client: FakeAtSpiClient,
    ) -> None:
        # Path tiene idx=42 — find_by_path falla, find_by_fuzzy resuelve.
        path = AtSpiPath(
            application="org.gnome.Nautilus",
            window="Files",
            role="push_button",
            name="Open",
            index=42,
        )
        captured = adapter.capture(
            action_kind=DesktopActionKind.CLICK,
            accessible_path=path,
            payload={},
        )
        outcome = adapter.replay(captured)
        assert outcome.success is True
        assert outcome.fallback_used is True

    def test_replay_not_found_returns_failure(
        self,
        adapter: DesktopAppSurfaceAdapter,
        fake_client: FakeAtSpiClient,
    ) -> None:
        path = AtSpiPath(
            application="org.gnome.gedit",
            window="Editor",
            role="push_button",
            name="Save",
        )
        captured = adapter.capture(
            action_kind=DesktopActionKind.CLICK,
            accessible_path=path,
            payload={},
        )
        outcome = adapter.replay(captured)
        assert outcome.success is False
        assert outcome.error == "accessible_not_found"

    def test_replay_type_text_dispatches_correctly(
        self,
        adapter: DesktopAppSurfaceAdapter,
        fake_client: FakeAtSpiClient,
    ) -> None:
        path = AtSpiPath(
            application="org.gnome.Nautilus",
            window="Files",
            role="text",
            name="Address",
        )
        captured = adapter.capture(
            action_kind=DesktopActionKind.TYPE,
            accessible_path=path,
            payload={"text": "/home/hermes"},
        )
        outcome = adapter.replay(captured)
        assert outcome.success is True
        assert (
            "type",
            {"path": path.to_canonical_string(), "text": "/home/hermes"},
        ) in fake_client.invocations

    def test_replay_keyboard_shortcut_requires_payload(
        self,
        adapter: DesktopAppSurfaceAdapter,
        fake_client: FakeAtSpiClient,
    ) -> None:
        path = AtSpiPath(
            application="org.gnome.Nautilus",
            window="Files",
            role="push_button",
            name="Open",
        )
        captured = adapter.capture(
            action_kind=DesktopActionKind.KEYBOARD_SHORTCUT,
            accessible_path=path,
            payload={},
        )
        outcome = adapter.replay(captured)
        assert outcome.success is False
        assert "shortcut" in (outcome.error or "")
