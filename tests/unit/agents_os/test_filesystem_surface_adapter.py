"""Tests FilesystemSurfaceAdapter (FR-027/028 + fail-closed path allowlist)."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from hermes.agents_os.domain.ports.surface_adapter_port import ReplayStatus
from hermes.agents_os.domain.surface_kind import SurfaceKind
from hermes.agents_os.infrastructure.filesystem_surface_adapter import (
    FilesystemSurfaceAdapter,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class TestPathAllowlist:
    def test_empty_allowlist_rejected(self) -> None:
        with pytest.raises(ValueError, match="fail-closed"):
            FilesystemSurfaceAdapter(allowed_prefixes=())

    async def test_path_outside_allowlist_rejected_on_capture(
        self, tmp_path: Path
    ) -> None:
        outside = tmp_path / "denied.txt"
        outside.write_text("nope")
        adapter = FilesystemSurfaceAdapter(allowed_prefixes=("/usr/share/",))
        with pytest.raises(PermissionError, match="allowlist"):
            await adapter.capture(
                intent_desc="read outside",
                params={"op": "read_file", "path": str(outside)},
                tenant_id=uuid4(),
                human_operator_id=uuid4(),
            )

    async def test_path_outside_allowlist_rejected_on_replay(
        self, tmp_path: Path
    ) -> None:
        inside = tmp_path / "doc.txt"
        inside.write_text("ok")
        adapter = FilesystemSurfaceAdapter(allowed_prefixes=(str(tmp_path),))
        captured = await adapter.capture(
            intent_desc="read inside",
            params={"op": "read_file", "path": str(inside)},
            tenant_id=uuid4(),
            human_operator_id=uuid4(),
        )
        # Construir adapter más estricto para replay (no incluye tmp_path).
        narrow_adapter = FilesystemSurfaceAdapter(allowed_prefixes=("/usr/share/",))
        outcome = await narrow_adapter.replay(captured)
        assert outcome.status == ReplayStatus.REJECTED_BY_POLICY


class TestOperations:
    async def test_read_file(self, tmp_path: Path) -> None:
        f = tmp_path / "hello.txt"
        f.write_text("HOLA")
        adapter = FilesystemSurfaceAdapter(allowed_prefixes=(str(tmp_path),))
        action = await adapter.capture(
            intent_desc="read",
            params={"op": "read_file", "path": str(f)},
            tenant_id=uuid4(),
            human_operator_id=uuid4(),
        )
        assert action.surface_kind == SurfaceKind.FILESYSTEM
        outcome = await adapter.replay(action)
        assert outcome.status == ReplayStatus.EXECUTED_OK
        assert outcome.result["text"] == "HOLA"

    async def test_write_file_and_replay(self, tmp_path: Path) -> None:
        f = tmp_path / "out.txt"
        adapter = FilesystemSurfaceAdapter(allowed_prefixes=(str(tmp_path),))
        action = await adapter.capture(
            intent_desc="write",
            params={
                "op": "write_file",
                "path": str(f),
                "content": "DATOS NUEVOS",
            },
            tenant_id=uuid4(),
            human_operator_id=uuid4(),
        )
        assert f.read_text() == "DATOS NUEVOS"
        # Replay debería re-escribir (idempotente).
        f.unlink()
        # Reinyectar content en payload para que replay tenga qué escribir.
        # En la implementación, content NO se guarda en payload por defecto;
        # esto es un trade-off documentado: para replay de write_file la
        # skill debe traer el content (o un template tokenizado).
        action_with_content = type(action)(
            action_id=action.action_id,
            surface_kind=action.surface_kind,
            intent_desc=action.intent_desc,
            payload={**action.payload, "content": "DATOS NUEVOS"},
            tenant_id=action.tenant_id,
            human_operator_id=action.human_operator_id,
        )
        outcome = await adapter.replay(action_with_content)
        assert outcome.status == ReplayStatus.EXECUTED_OK
        assert f.read_text() == "DATOS NUEVOS"

    async def test_list_dir(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "b.txt").write_text("b")
        adapter = FilesystemSurfaceAdapter(allowed_prefixes=(str(tmp_path),))
        action = await adapter.capture(
            intent_desc="list",
            params={"op": "list_dir", "path": str(tmp_path)},
            tenant_id=uuid4(),
            human_operator_id=uuid4(),
        )
        outcome = await adapter.replay(action)
        assert outcome.status == ReplayStatus.EXECUTED_OK
        assert "a.txt" in outcome.result["entries"]
        assert "b.txt" in outcome.result["entries"]


class TestSurfaceMismatch:
    async def test_replay_rejects_browser_surface(self, tmp_path: Path) -> None:
        adapter = FilesystemSurfaceAdapter(allowed_prefixes=(str(tmp_path),))
        from hermes.agents_os.domain.ports.surface_adapter_port import (
            CapturedAction,
        )

        wrong = CapturedAction(
            surface_kind=SurfaceKind.BROWSER,
            intent_desc="wrong",
            payload={"op": "read_file", "path": str(tmp_path / "x.txt")},
        )
        outcome = await adapter.replay(wrong)
        assert outcome.status == ReplayStatus.REJECTED_BY_POLICY
