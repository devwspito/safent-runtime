"""NarrativeCompleteness — enum de cobertura narrativa (data-model §6)."""

from __future__ import annotations

from enum import StrEnum


class NarrativeCompleteness(StrEnum):
    NONE = "none"       # sin micrófono o sin fragmentos usables
    PARTIAL = "partial" # algunos steps con narración
    FULL = "full"       # todos los steps con narración usable
