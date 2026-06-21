"""FakeBrowserDriver: implementacion `BrowserPort` deterministica para tests.

Permite escriptar outcomes por step kind/instruction, sin chromium real.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from hermes.browser.domain.port import BrowserPort  # noqa: F401  (Protocol check)
from hermes.browser.domain.step import Step, StepOutcome


@dataclass(frozen=True, slots=True)
class ScriptedStep:
    """Respuesta scripted que el `FakeBrowserDriver` devuelve cuando matchea."""

    matches_kind: str | None = None  # None = cualquier kind
    matches_intent_substr: str | None = None  # None = cualquier intent
    outcome_status_ok: bool = True
    result: Mapping[str, Any] = field(default_factory=dict)
    error: str = ""
    duration_ms: int = 10


def scripted_step(
    *,
    matches_kind: str | None = None,
    matches_intent_substr: str | None = None,
    result: Mapping[str, Any] | None = None,
    error: str = "",
    ok: bool = True,
    duration_ms: int = 10,
) -> ScriptedStep:
    """Builder ergonomico."""
    return ScriptedStep(
        matches_kind=matches_kind,
        matches_intent_substr=matches_intent_substr,
        outcome_status_ok=ok,
        result=dict(result or {}),
        error=error,
        duration_ms=duration_ms,
    )


class FakeBrowserDriver:
    """Driver no-real, scriptable. Implementa `BrowserPort` Protocol."""

    def __init__(
        self,
        *,
        scripted: Sequence[ScriptedStep] = (),
        default_screenshot: bytes = b"\x89PNG\r\n\x1a\n",
        default_dom: str = "<root></root>",
        default_url: str = "about:blank",
    ) -> None:
        self._scripted: list[ScriptedStep] = list(scripted)
        self._default_screenshot = default_screenshot
        self._default_dom = default_dom
        self._url = default_url
        self.executed_steps: list[Step] = []
        self.executed_with_token: list[str | None] = []
        self.closed = False

    async def execute(
        self,
        step: Step,
        *,
        hitl_approval_token: str | None = None,
    ) -> StepOutcome:
        self.executed_steps.append(step)
        self.executed_with_token.append(hitl_approval_token)
        scripted = self._find_match(step)
        if scripted is None:
            return StepOutcome.ok(
                step_id=step.step_id,
                duration_ms=1,
                result={"fake": True, "kind": step.kind.value},
            )
        if not scripted.outcome_status_ok:
            return StepOutcome.failed(
                step_id=step.step_id,
                error=scripted.error or "scripted_failure",
                duration_ms=scripted.duration_ms,
            )
        return StepOutcome.ok(
            step_id=step.step_id,
            duration_ms=scripted.duration_ms,
            result=dict(scripted.result),
        )

    def _find_match(self, step: Step) -> ScriptedStep | None:
        for spec in self._scripted:
            if spec.matches_kind is not None and spec.matches_kind != step.kind.value:
                continue
            if (
                spec.matches_intent_substr is not None
                and spec.matches_intent_substr not in step.intent_desc
            ):
                continue
            return spec
        return None

    async def take_screenshot(self) -> bytes:
        return self._default_screenshot

    async def take_dom_snapshot(self) -> str:
        return self._default_dom

    async def current_url(self) -> str:
        return self._url

    async def close(self) -> None:
        self.closed = True

    @property
    def driver_name(self) -> str:
        return "fake"

    @property
    def capabilities(self) -> dict[str, Any]:
        return {"fake": True, "supports_vision": False}
