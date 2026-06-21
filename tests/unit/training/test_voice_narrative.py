"""Tests VoiceFragment + VoiceNarrative + compute_completeness."""
from __future__ import annotations

from uuid import uuid4

import pytest

from hermes.training.domain.narrative_completeness import NarrativeCompleteness
from hermes.training.domain.voice_narrative import (
    VoiceFragment,
    VoiceFragmentState,
    compute_completeness,
)

pytestmark = pytest.mark.unit


def _frag(step_id, conf=0.9, state=VoiceFragmentState.ASSOCIATED):
    return VoiceFragment(
        fragment_id=uuid4(),
        step_id=step_id,
        transcript="ok",
        confidence=conf,
        state=state,
    )


class TestVoiceFragment:
    def test_confidence_out_of_range_rejected(self) -> None:
        with pytest.raises(ValueError):
            VoiceFragment(confidence=1.2)

    def test_usable_only_when_associated_and_above_threshold(self) -> None:
        s = uuid4()
        assert _frag(s, conf=0.9).is_usable_for_rule_inference()
        assert not _frag(s, conf=0.4).is_usable_for_rule_inference()
        assert not _frag(s, state=VoiceFragmentState.RECORDING).is_usable_for_rule_inference()


class TestCompleteness:
    def test_mic_denied_returns_none(self) -> None:
        assert (
            compute_completeness((), 5, mic_granted=False)
            == NarrativeCompleteness.NONE
        )

    def test_zero_steps_returns_none(self) -> None:
        assert (
            compute_completeness((_frag(uuid4()),), 0, mic_granted=True)
            == NarrativeCompleteness.NONE
        )

    def test_no_usable_fragments_returns_none(self) -> None:
        frags = (_frag(uuid4(), conf=0.2),)
        assert (
            compute_completeness(frags, 1, mic_granted=True)
            == NarrativeCompleteness.NONE
        )

    def test_all_steps_covered_returns_full(self) -> None:
        s1, s2, s3 = uuid4(), uuid4(), uuid4()
        frags = (_frag(s1), _frag(s2), _frag(s3))
        assert (
            compute_completeness(frags, 3, mic_granted=True)
            == NarrativeCompleteness.FULL
        )

    def test_partial_coverage_returns_partial(self) -> None:
        s1, s2, s3 = uuid4(), uuid4(), uuid4()
        frags = (_frag(s1), _frag(s2))
        assert (
            compute_completeness(frags, 3, mic_granted=True)
            == NarrativeCompleteness.PARTIAL
        )
