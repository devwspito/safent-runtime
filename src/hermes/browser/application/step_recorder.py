"""StepRecorder: captura screenshot + DOM pre/post de cada step.

Diseno:
  - Cada step ejecutado por `BrowserSession` se persiste como `StepRecord`.
  - Pre/post: snapshot ANTES de ejecutar y DESPUES (si exito).
  - Si fallo: solo pre + error.
  - El storage real (S3 / FS encriptado) es responsabilidad de un `StepArtifactStore`
    inyectable. Aqui solo modelamos la unidad.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from hermes.browser.domain.snapshot import DomSnapshot, Screenshot
from hermes.browser.domain.step import Step, StepOutcome

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StepRecord:
    """Persistente: step + outcome + artifacts."""

    step: Step
    outcome: StepOutcome
    screenshots: tuple[Screenshot, ...] = ()
    dom_snapshots: tuple[DomSnapshot, ...] = ()
    recorded_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))


class StepArtifactStore(Protocol):
    """Persiste bytes de screenshot y DOM en almacenamiento externo.

    Implementaciones:
      - InMemoryArtifactStore (testing).
      - S3ArtifactStore (produccion: cifrado SSE-KMS).
      - FilesystemArtifactStore (dev / single host).
    """

    async def put_screenshot(
        self, *, tenant_id: UUID, step_id: UUID, moment: str, data: bytes
    ) -> str: ...

    async def put_dom_snapshot(
        self, *, tenant_id: UUID, step_id: UUID, moment: str, text: str
    ) -> str: ...


class StepRecordSink(Protocol):
    """Persiste el `StepRecord` (sin los bytes — esos van al ArtifactStore)."""

    async def append(self, record: StepRecord) -> None: ...


class StepRecorder:
    """Orquesta artifact-store + record-sink + diff opcional.

    `BrowserSession` lo invoca en cada step:
        await recorder.record_pre(step, screenshot, dom)
        outcome = await driver.execute(step, ...)
        await recorder.record_post(step, outcome, screenshot_post, dom_post)
    """

    def __init__(
        self,
        *,
        artifact_store: StepArtifactStore,
        sink: StepRecordSink,
    ) -> None:
        self._artifacts = artifact_store
        self._sink = sink
        # Buffer mientras se construye el record
        self._pending: dict[UUID, _PendingRecord] = {}

    async def record_pre(
        self,
        step: Step,
        *,
        screenshot: bytes,
        dom_text: str,
    ) -> tuple[Screenshot, DomSnapshot]:
        screenshot_uri = await self._artifacts.put_screenshot(
            tenant_id=step.tenant_id,
            step_id=step.step_id,
            moment="pre",
            data=screenshot,
        )
        dom_uri = await self._artifacts.put_dom_snapshot(
            tenant_id=step.tenant_id,
            step_id=step.step_id,
            moment="pre",
            text=dom_text,
        )
        from uuid import uuid4 as _uuid4  # noqa: PLC0415

        screenshot_obj = Screenshot(
            screenshot_id=_uuid4(),
            step_id=step.step_id,
            moment="pre",
            content_hash=Screenshot.hash_bytes(screenshot),
            width_px=0,
            height_px=0,
            storage_uri=screenshot_uri,
        )
        dom_obj = DomSnapshot(
            snapshot_id=_uuid4(),
            step_id=step.step_id,
            moment="pre",
            content_hash=DomSnapshot.hash_text(dom_text),
            char_count=len(dom_text),
            storage_uri=dom_uri,
        )
        self._pending[step.step_id] = _PendingRecord(
            step=step,
            screenshots=[screenshot_obj],
            dom_snapshots=[dom_obj],
        )
        return screenshot_obj, dom_obj

    async def record_post(
        self,
        step: Step,
        outcome: StepOutcome,
        *,
        screenshot: bytes | None = None,
        dom_text: str | None = None,
    ) -> StepRecord:
        pending = self._pending.pop(step.step_id, None)
        if pending is None:
            logger.warning(
                "hermes.browser.recorder.no_pre",
                extra={"step_id": str(step.step_id)},
            )
            pending = _PendingRecord(step=step, screenshots=[], dom_snapshots=[])

        if screenshot is not None:
            screenshot_uri = await self._artifacts.put_screenshot(
                tenant_id=step.tenant_id,
                step_id=step.step_id,
                moment="post",
                data=screenshot,
            )
            from uuid import uuid4 as _uuid4  # noqa: PLC0415

            pending.screenshots.append(
                Screenshot(
                    screenshot_id=_uuid4(),
                    step_id=step.step_id,
                    moment="post",
                    content_hash=Screenshot.hash_bytes(screenshot),
                    width_px=0,
                    height_px=0,
                    storage_uri=screenshot_uri,
                )
            )
        if dom_text is not None:
            dom_uri = await self._artifacts.put_dom_snapshot(
                tenant_id=step.tenant_id,
                step_id=step.step_id,
                moment="post",
                text=dom_text,
            )
            from uuid import uuid4 as _uuid4  # noqa: PLC0415

            pending.dom_snapshots.append(
                DomSnapshot(
                    snapshot_id=_uuid4(),
                    step_id=step.step_id,
                    moment="post",
                    content_hash=DomSnapshot.hash_text(dom_text),
                    char_count=len(dom_text),
                    storage_uri=dom_uri,
                )
            )

        record = StepRecord(
            step=step,
            outcome=outcome,
            screenshots=tuple(pending.screenshots),
            dom_snapshots=tuple(pending.dom_snapshots),
        )
        await self._sink.append(record)
        return record


@dataclass(slots=True)
class _PendingRecord:
    step: Step
    screenshots: list[Screenshot] = field(default_factory=list)
    dom_snapshots: list[DomSnapshot] = field(default_factory=list)


# ---------------------------------------------------------------------------
# In-memory implementations for tests
# ---------------------------------------------------------------------------


class InMemoryArtifactStore:
    """`StepArtifactStore` que guarda bytes/text en `dict`. Para tests."""

    def __init__(self) -> None:
        self.screenshots: dict[str, bytes] = {}
        self.dom_snapshots: dict[str, str] = {}

    async def put_screenshot(
        self, *, tenant_id: UUID, step_id: UUID, moment: str, data: bytes
    ) -> str:
        uri = f"mem://screenshot/{tenant_id}/{step_id}/{moment}"
        self.screenshots[uri] = data
        return uri

    async def put_dom_snapshot(
        self, *, tenant_id: UUID, step_id: UUID, moment: str, text: str
    ) -> str:
        uri = f"mem://dom/{tenant_id}/{step_id}/{moment}"
        self.dom_snapshots[uri] = text
        return uri


class InMemoryRecordSink:
    """`StepRecordSink` que acumula registros en `list`. Para tests."""

    def __init__(self) -> None:
        self.records: list[StepRecord] = []

    async def append(self, record: StepRecord) -> None:
        self.records.append(record)

    def step_records(self) -> Sequence[StepRecord]:
        return tuple(self.records)
