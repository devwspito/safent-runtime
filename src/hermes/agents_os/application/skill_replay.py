"""SkillReplayer — ejecuta un SkillPackage firmado paso a paso.

Spec 003 FR-027, FR-029 — núcleo del "Hermes opera 24/7": dado un
SkillPackage firmado, ejecuta los steps en orden vía los
SurfaceAdapters correspondientes. Sin LLM en el path crítico
(route memorization).

Errores recuperables:
  - replay step falla con `outcome.success=False`:
      * por defecto STOP_ON_FIRST_FAILURE (fail-closed).
      * con policy=CONTINUE_AND_REPORT registra y sigue.
  - signature inválida: NUNCA se ejecuta (FR-031 invariante).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable
from uuid import UUID, uuid4

from hermes.agents_os.application.skill_compiler import (
    SkillCompiler,
    SkillPackage,
)
from hermes.agents_os.domain.surface_kind import SurfaceKind

logger = logging.getLogger(__name__)


class ReplayFailurePolicy(StrEnum):
    STOP_ON_FIRST_FAILURE = "stop_on_first_failure"
    CONTINUE_AND_REPORT = "continue_and_report"


class SkillReplayError(RuntimeError):
    pass


class InvalidSignatureError(SkillReplayError):
    """FR-031 invariante."""


class MissingSurfaceAdapterError(SkillReplayError):
    """No hay adapter registrado para el surface_kind del step."""


@runtime_checkable
class SurfaceReplayPort(Protocol):
    """Sub-puerto de SurfaceAdapterPort — solo replay."""

    surface_kind: SurfaceKind

    def replay_payload(self, payload: dict[str, Any]) -> bool:
        """Ejecuta una acción a partir de su payload. True si éxito."""
        ...


@dataclass(slots=True)
class StepReplayResult:
    sequence_index: int
    surface_kind: SurfaceKind
    success: bool
    error: str | None = None
    started_at: datetime = field(
        default_factory=lambda: datetime.now(tz=UTC)
    )
    completed_at: datetime | None = None


@dataclass(slots=True)
class ReplayRun:
    run_id: UUID
    package_id: UUID
    skill_id: str
    started_at: datetime
    completed_at: datetime | None
    step_results: list[StepReplayResult]
    policy: ReplayFailurePolicy
    aborted_due_to_failure: bool

    @property
    def succeeded(self) -> bool:
        return all(r.success for r in self.step_results)


class SkillReplayer:
    """Ejecuta SkillPackages firmados."""

    def __init__(
        self,
        *,
        compiler: SkillCompiler,
        adapters_by_surface: dict[SurfaceKind, SurfaceReplayPort],
        _allow_ungated_replay: bool = False,
    ) -> None:
        self._compiler = compiler
        self._adapters = adapters_by_surface
        # SECURITY (red-team 2026-06-19): replaying a skill step-by-step via the
        # adapters DIRECTLY bypasses the CapabilityBroker — it skips kill-switch,
        # consent, HITL, idempotency and the broker audit chain. A learned skill must
        # NEVER be a bypass of the live cage. This component is currently UNWIRED (no
        # autonomous runner calls it); when one is built it MUST route each step
        # through CapabilityBroker.dispatch() so every replayed action re-gates.
        # Until then replay() is FAIL-CLOSED: it refuses unless this test-only escape
        # hatch is set (the name is deliberately ugly so it never ships in production
        # — a code reviewer will catch it). The adapter-level gates (egress netns
        # jail, terminal install-gate) still apply, but the broker gates do not.
        self._allow_ungated_replay = _allow_ungated_replay

    def replay(
        self,
        *,
        package: SkillPackage,
        policy: ReplayFailurePolicy = ReplayFailurePolicy.STOP_ON_FIRST_FAILURE,
    ) -> ReplayRun:
        if not self._allow_ungated_replay:
            raise SkillReplayError(
                "SkillReplayer refuses to replay: stepping the surface adapters "
                "directly bypasses the CapabilityBroker (kill-switch/consent/HITL/"
                "audit). Route each step through CapabilityBroker.dispatch() so a "
                "learned skill re-gates like any live action. (red-team 2026-06-19)"
            )
        if not self._compiler.verify(package):
            raise InvalidSignatureError(
                f"SkillPackage {package.package_id} signature inválida"
            )
        run = ReplayRun(
            run_id=uuid4(),
            package_id=package.package_id,
            skill_id=package.skill_id,
            started_at=datetime.now(tz=UTC),
            completed_at=None,
            step_results=[],
            policy=policy,
            aborted_due_to_failure=False,
        )
        # Reconstruir el orden global mediante sequence_index.
        ordered_steps = sorted(
            (step for steps in package.steps_by_surface_kind.values() for step in steps),
            key=lambda s: s.sequence_index,
        )
        for step in ordered_steps:
            adapter = self._adapters.get(step.surface_kind)
            if adapter is None:
                raise MissingSurfaceAdapterError(
                    f"sin adapter para {step.surface_kind}"
                )
            res = StepReplayResult(
                sequence_index=step.sequence_index,
                surface_kind=step.surface_kind,
                success=False,
            )
            try:
                ok = adapter.replay_payload(step.action_payload)
                res.success = bool(ok)
            except Exception as exc:  # noqa: BLE001
                res.success = False
                res.error = str(exc)
            res.completed_at = datetime.now(tz=UTC)
            run.step_results.append(res)

            if not res.success and policy == ReplayFailurePolicy.STOP_ON_FIRST_FAILURE:
                run.aborted_due_to_failure = True
                break

        run.completed_at = datetime.now(tz=UTC)
        return run
