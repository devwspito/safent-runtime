"""TranscriptAssociator — algoritmo asimétrico -8s/+4s (T095, research §11).

Para cada VoiceFragment con ventana [start, end], asigna el StepRecord más
cercano en la ventana [step.event_ts - 8s, step.event_ts + 4s].

Empate entre dos steps con el mismo overlap → se elige el step anterior
(menor event_ts), porque el formador suele explicar antes de actuar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID


_BEFORE_OFFSET = timedelta(seconds=8)
_AFTER_OFFSET = timedelta(seconds=4)


@dataclass(frozen=True, slots=True)
class StepTimestamp:
    step_id: UUID
    event_ts: datetime


@dataclass(frozen=True, slots=True)
class FragmentWindow:
    fragment_id: UUID
    audio_start_ts: datetime
    audio_end_ts: datetime


def associate_fragments(
    steps: list[StepTimestamp],
    fragments: list[FragmentWindow],
) -> dict[UUID, list[UUID]]:
    """Devuelve {step_id: [fragment_id, ...]} con asignación asimétrica.

    Un fragment se asigna al step cuya ventana -8s/+4s contiene el mayor
    overlap con el fragment. En caso de empate → step anterior (menor ts).

    Fragments sin ningún step en rango quedan sin asignar (no aparecen).
    """
    result: dict[UUID, list[UUID]] = {}

    for frag in fragments:
        best = _find_best_step(frag, steps)
        if best is not None:
            result.setdefault(best, []).append(frag.fragment_id)

    return result


def _find_best_step(
    frag: FragmentWindow,
    steps: list[StepTimestamp],
) -> UUID | None:
    best_step_id: UUID | None = None
    best_overlap: float = 0.0
    best_ts: datetime | None = None

    for step in steps:
        window_start = step.event_ts - _BEFORE_OFFSET
        window_end = step.event_ts + _AFTER_OFFSET
        overlap = _overlap_seconds(
            frag.audio_start_ts, frag.audio_end_ts, window_start, window_end
        )
        if overlap <= 0.0:
            continue
        if _is_better(overlap, step.event_ts, best_overlap, best_ts):
            best_overlap = overlap
            best_step_id = step.step_id
            best_ts = step.event_ts

    return best_step_id


def _overlap_seconds(
    a_start: datetime,
    a_end: datetime,
    b_start: datetime,
    b_end: datetime,
) -> float:
    start = max(a_start, b_start)
    end = min(a_end, b_end)
    if end <= start:
        return 0.0
    return (end - start).total_seconds()


def _is_better(
    overlap: float,
    ts: datetime,
    best_overlap: float,
    best_ts: datetime | None,
) -> bool:
    """Empate → elegir step anterior (menor ts) — research §11."""
    if overlap > best_overlap:
        return True
    if overlap == best_overlap and best_ts is not None and ts < best_ts:
        return True
    return False
