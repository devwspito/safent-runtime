"""Tests del NarrativeAggregator (T096, FR-018)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from hermes.training.application.narrative_aggregator import NarrativeAggregator
from hermes.training.domain.narrative_completeness import NarrativeCompleteness
from hermes.training.domain.voice_narrative import VoiceFragment, VoiceFragmentState

pytestmark = pytest.mark.unit


def _frag(step_id: object = None, conf: float = 0.9) -> VoiceFragment:
    return VoiceFragment(
        fragment_id=uuid4(),
        step_id=step_id or uuid4(),
        transcript="ok",
        confidence=conf,
        state=VoiceFragmentState.ASSOCIATED,
    )


class TestNarrativeAggregatorCompleteness:
    def setup_method(self) -> None:
        self._agg = NarrativeAggregator()
        self._session_id = uuid4()
        self._tenant_id = uuid4()

    def _aggregate(
        self,
        fragments: list[VoiceFragment],
        total_steps: int,
        mic_granted: bool = True,
    ):
        return self._agg.aggregate(
            training_session_id=self._session_id,
            tenant_id=self._tenant_id,
            fragments=fragments,
            total_steps=total_steps,
            mic_granted=mic_granted,
        )

    def test_mic_denied_is_none(self) -> None:
        narrative = self._aggregate([_frag()], 3, mic_granted=False)
        assert narrative.completeness == NarrativeCompleteness.NONE

    def test_no_fragments_is_none(self) -> None:
        narrative = self._aggregate([], 3)
        assert narrative.completeness == NarrativeCompleteness.NONE

    def test_all_steps_covered_is_full(self) -> None:
        s1, s2 = uuid4(), uuid4()
        narrative = self._aggregate([_frag(s1), _frag(s2)], 2)
        assert narrative.completeness == NarrativeCompleteness.FULL

    def test_partial_coverage_is_partial(self) -> None:
        narrative = self._aggregate([_frag()], 3)
        assert narrative.completeness == NarrativeCompleteness.PARTIAL

    def test_low_confidence_fragments_excluded(self) -> None:
        frag_low = _frag(conf=0.2)
        narrative = self._aggregate([frag_low], 1)
        assert narrative.completeness == NarrativeCompleteness.NONE

    def test_narrative_preserves_fragments(self) -> None:
        s1, s2 = uuid4(), uuid4()
        fragments = [_frag(s1), _frag(s2)]
        narrative = self._aggregate(fragments, 2)
        assert len(narrative.fragments) == 2

    def test_narrative_has_correct_tenant(self) -> None:
        narrative = self._aggregate([_frag()], 1)
        assert narrative.tenant_id == self._tenant_id

    def test_narrative_has_correct_session(self) -> None:
        narrative = self._aggregate([_frag()], 1)
        assert narrative.training_session_id == self._session_id
