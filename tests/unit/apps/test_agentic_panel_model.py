"""Tests PanelModel (lógica del panel sin GTK)."""

from __future__ import annotations

import pytest

from apps.agentic_panel.agentic_panel import PanelModel

pytestmark = pytest.mark.unit


class TestPanelModel:
    def test_initial_label_idle_ish(self) -> None:
        m = PanelModel()
        assert "conectando" in m.label_state()

    def test_running_label_shows_task_count(self) -> None:
        m = PanelModel()
        m.update_from_snapshot(
            {
                "state": "running",
                "active_task_count": 3,
                "telemetry_enabled": False,
                "sandbox_count": 1,
                "consent_count": 2,
                "last_audit_head_hex": "ab" * 32,
            }
        )
        assert "trabajando" in m.label_state()
        assert "3" in m.label_state()

    def test_paused_label(self) -> None:
        m = PanelModel()
        m.update_from_snapshot(
            {"state": "paused", "active_task_count": 0}
        )
        assert "pausado" in m.label_state()

    def test_telemetry_label(self) -> None:
        m = PanelModel()
        assert "off" in m.telemetry_label()
        m.update_from_snapshot({"telemetry_enabled": True})
        assert "on" in m.telemetry_label()

    def test_update_handles_missing_keys(self) -> None:
        m = PanelModel()
        m.update_from_snapshot({})
        assert m.agent_state == "unknown"
        assert m.active_task_count == 0

    def test_unknown_state_falls_back(self) -> None:
        m = PanelModel()
        m.update_from_snapshot({"state": "rebooting"})
        assert "rebooting" in m.label_state()
