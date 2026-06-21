"""Tests para las funciones puras del control de autonomía en AgentDialog.

Cubre los helpers autonomy_value_for_index / autonomy_index_for_value sin
instanciar GTK (testable en cualquier entorno headless).
"""

from __future__ import annotations

import pytest

from hermes.shell.presentation.gtk4.widgets.agent_dialog import (
    _AUTONOMY_OPTIONS,
    _AUTONOMOUS_IDX,
    _BALANCED_IDX,
    autonomy_index_for_value,
    autonomy_value_for_index,
)

pytestmark = pytest.mark.unit


class TestAutonomyValueForIndex:
    def test_returns_correct_value_for_each_index(self) -> None:
        for i, (_, expected_value, _s) in enumerate(_AUTONOMY_OPTIONS):
            assert autonomy_value_for_index(i) == expected_value

    def test_out_of_range_negative_returns_balanced(self) -> None:
        assert autonomy_value_for_index(-1) == "balanced"

    def test_out_of_range_high_returns_balanced(self) -> None:
        assert autonomy_value_for_index(len(_AUTONOMY_OPTIONS)) == "balanced"

    def test_autonomous_index_yields_autonomous(self) -> None:
        assert autonomy_value_for_index(_AUTONOMOUS_IDX) == "autonomous"

    def test_balanced_index_yields_balanced(self) -> None:
        assert autonomy_value_for_index(_BALANCED_IDX) == "balanced"


class TestAutonomyIndexForValue:
    def test_returns_correct_index_for_each_value(self) -> None:
        for i, (_, value, _s) in enumerate(_AUTONOMY_OPTIONS):
            assert autonomy_index_for_value(value) == i

    def test_unknown_value_returns_balanced_index(self) -> None:
        assert autonomy_index_for_value("god_mode") == _BALANCED_IDX

    def test_empty_string_returns_balanced_index(self) -> None:
        assert autonomy_index_for_value("") == _BALANCED_IDX

    def test_ask_always_round_trips(self) -> None:
        idx = autonomy_index_for_value("ask_always")
        assert autonomy_value_for_index(idx) == "ask_always"

    def test_autonomous_round_trips(self) -> None:
        idx = autonomy_index_for_value("autonomous")
        assert autonomy_value_for_index(idx) == "autonomous"

    def test_balanced_round_trips(self) -> None:
        idx = autonomy_index_for_value("balanced")
        assert autonomy_value_for_index(idx) == "balanced"


class TestAutonomyConstants:
    """Invariantes estructurales de la tabla — falla si se reordena por accidente."""

    def test_all_three_options_present(self) -> None:
        values = {v for _, v, _ in _AUTONOMY_OPTIONS}
        assert values == {"ask_always", "balanced", "autonomous"}

    def test_autonomous_idx_points_to_autonomous(self) -> None:
        assert _AUTONOMY_OPTIONS[_AUTONOMOUS_IDX][1] == "autonomous"

    def test_balanced_idx_points_to_balanced(self) -> None:
        assert _AUTONOMY_OPTIONS[_BALANCED_IDX][1] == "balanced"

    def test_all_options_have_non_empty_subtitle(self) -> None:
        for label, value, subtitle in _AUTONOMY_OPTIONS:
            assert label.strip(), f"label vacío para {value!r}"
            assert subtitle.strip(), f"subtítulo vacío para {value!r}"
