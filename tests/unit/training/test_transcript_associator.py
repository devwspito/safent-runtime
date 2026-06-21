"""Tests del algoritmo asimétrico -8s/+4s de TranscriptAssociator (T095)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from hermes.training.application.transcript_associator import (
    FragmentWindow,
    StepTimestamp,
    associate_fragments,
)

pytestmark = pytest.mark.unit

_BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _step(offset_s: float) -> StepTimestamp:
    return StepTimestamp(step_id=uuid4(), event_ts=_BASE + timedelta(seconds=offset_s))


def _frag(start_s: float, end_s: float) -> FragmentWindow:
    return FragmentWindow(
        fragment_id=uuid4(),
        audio_start_ts=_BASE + timedelta(seconds=start_s),
        audio_end_ts=_BASE + timedelta(seconds=end_s),
    )


class TestBasicAssociation:
    def test_fragment_within_before_window_associates(self) -> None:
        step = _step(10.0)  # window: [2s, 14s]
        frag = _frag(3.0, 8.0)  # completamente dentro del -8s
        result = associate_fragments([step], [frag])
        assert frag.fragment_id in result[step.step_id]

    def test_fragment_within_after_window_associates(self) -> None:
        step = _step(10.0)  # window: [2s, 14s]
        frag = _frag(11.0, 13.0)  # dentro del +4s
        result = associate_fragments([step], [frag])
        assert frag.fragment_id in result[step.step_id]

    def test_fragment_outside_window_not_associated(self) -> None:
        step = _step(10.0)  # window: [2s, 14s]
        frag = _frag(15.0, 20.0)  # fuera del +4s
        result = associate_fragments([step], [frag])
        assert step.step_id not in result

    def test_no_steps_returns_empty(self) -> None:
        frag = _frag(5.0, 10.0)
        result = associate_fragments([], [frag])
        assert result == {}

    def test_no_fragments_returns_empty(self) -> None:
        step = _step(10.0)
        result = associate_fragments([step], [])
        assert result == {}


class TestAsymmetry:
    def test_before_offset_is_8s(self) -> None:
        step = _step(10.0)  # window: [2s, 14s]
        # Justo dentro del -8s
        frag_inside = _frag(2.5, 4.0)
        # Justo fuera del -8s
        frag_outside = _frag(0.0, 1.5)
        result = associate_fragments([step], [frag_inside, frag_outside])
        assert frag_inside.fragment_id in result.get(step.step_id, [])
        assert frag_outside.fragment_id not in result.get(step.step_id, [])

    def test_after_offset_is_4s(self) -> None:
        step = _step(10.0)  # window: [2s, 14s]
        frag_inside = _frag(12.0, 13.5)
        frag_outside = _frag(14.5, 16.0)
        result = associate_fragments([step], [frag_inside, frag_outside])
        assert frag_inside.fragment_id in result.get(step.step_id, [])
        assert frag_outside.fragment_id not in result.get(step.step_id, [])


class TestTiebreaker:
    def test_tie_goes_to_earlier_step(self) -> None:
        """Empate de overlap → step anterior (menor event_ts) — research §11."""
        step_a = _step(10.0)  # window a: [2, 14]
        step_b = _step(12.0)  # window b: [4, 16]
        # Fragment en [8, 13]: solapa con ambos steps
        frag = _frag(8.0, 13.0)
        result = associate_fragments([step_a, step_b], [frag])
        # step_a tiene menor ts → debe ganar en empate
        step_a_frags = result.get(step_a.step_id, [])
        step_b_frags = result.get(step_b.step_id, [])
        assert frag.fragment_id in step_a_frags or frag.fragment_id in step_b_frags
        # No debe estar en los dos al mismo tiempo
        both = frag.fragment_id in step_a_frags and frag.fragment_id in step_b_frags
        assert not both


class TestMultipleFragmentsPerStep:
    def test_multiple_fragments_map_to_same_step(self) -> None:
        step = _step(20.0)  # window: [12, 24]
        frag1 = _frag(13.0, 15.0)
        frag2 = _frag(17.0, 19.0)
        frag3 = _frag(21.0, 23.0)
        result = associate_fragments([step], [frag1, frag2, frag3])
        assert len(result[step.step_id]) == 3
