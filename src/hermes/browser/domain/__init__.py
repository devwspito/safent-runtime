"""Domain layer del browser-automation: VOs puros, sin frameworks."""

from hermes.browser.domain.port import BrowserPort
from hermes.browser.domain.selector import Selector, SelectorRegistry
from hermes.browser.domain.snapshot import DomSnapshot, Screenshot, ScreenshotDiff
from hermes.browser.domain.step import (
    Step,
    StepKind,
    StepOutcome,
    StepRisk,
    StepStatus,
)

__all__ = [
    "BrowserPort",
    "DomSnapshot",
    "Screenshot",
    "ScreenshotDiff",
    "Selector",
    "SelectorRegistry",
    "Step",
    "StepKind",
    "StepOutcome",
    "StepRisk",
    "StepStatus",
]
