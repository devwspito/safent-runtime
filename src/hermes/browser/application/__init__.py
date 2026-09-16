"""Application layer del browser: recorder + gates.

BrowserSession/BrowserSessionConfig/HitlApprovalRequired (orquestador
spec-002) se archivaron en `archive/browser-spec-002` — NO-GO fase 4,
parcado. Ver specs/025-safent-repaso/oleada-1.md §L1b.
"""

from hermes.browser.application.step_recorder import StepRecord, StepRecorder

__all__ = [
    "StepRecord",
    "StepRecorder",
]
