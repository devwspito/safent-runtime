"""WhisperModelIntegrityChecker — verificación runtime del modelo Whisper (T091).

BLOQUEANTE CTRL-10 / THR-31.

Responsabilidades:
1. Post-boot: calcula SHA-256 del binario ``/opt/models/distil-large-v3/model.bin``.
2. Reporta el digest al control plane via WS channel (``whisper_model_loaded``).
3. Si el control plane devuelve mismatch → workspace ``state=closed`` ANTES
   de emitir ``training_ready`` + AuditEntry ``whisper_model_tampered``.
4. Si el archivo no existe → AuditEntry ``whisper_model_missing`` + cierre.

Diseño:
- La verificación es síncrona (bloqueante) en el threadpool: SHA-256 de ~800 MB
  toma ~3-5 s en CPU típica; aceptable en el boot pre-training_ready.
- El canal del control plane se inyecta (DI) — no hay acoplamiento a la
  implementación concreta.
- CTRL-10 exige que esta verificación ocurra ANTES de emitir ``training_ready``:
  el caller (workspace boot orchestrator) debe esperar a ``verify()`` antes
  de cambiar el estado.

Constitución IV (fail-closed): cualquier fallo en la verificación → cierre.
FR-059, research §6.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

logger = logging.getLogger(__name__)

__all__ = [
    "ModelIntegrityResult",
    "WhisperModelIntegrityChecker",
    "WhisperModelTampered",
    "WhisperModelMissing",
]

_DEFAULT_MODEL_BIN = Path("/opt/models/distil-large-v3/model.bin")
_READ_CHUNK = 1 << 20  # 1 MiB


class ModelIntegrityError(RuntimeError):
    """Base."""


class WhisperModelTampered(ModelIntegrityError):
    """SHA-256 del modelo no coincide con el digest firmado del control plane."""


class WhisperModelMissing(ModelIntegrityError):
    """El archivo del modelo no existe en la ruta esperada."""


@dataclass(frozen=True, slots=True)
class ModelIntegrityResult:
    """Resultado de la verificación."""

    workspace_id: UUID
    tenant_id: UUID
    model_path: str
    sha256_hex: str
    verified_at: datetime
    tampered: bool
    missing: bool


class _ControlPlaneChannelProtocol(Protocol):
    async def send_command(self, method: str, params: dict[str, Any]) -> None: ...


class WhisperModelIntegrityChecker:
    """Verifica la integridad del modelo Whisper en boot.

    Uso::

        checker = WhisperModelIntegrityChecker(
            workspace_id=ws_id,
            tenant_id=tenant_id,
            channel=cp_channel,
            expected_sha256_hex=digest_firmado,
        )
        result = await checker.verify()
        # Si lanza → workspace cerrado; no emitir training_ready.
    """

    def __init__(
        self,
        *,
        workspace_id: UUID,
        tenant_id: UUID,
        channel: _ControlPlaneChannelProtocol,
        expected_sha256_hex: str,
        model_bin_path: Path = _DEFAULT_MODEL_BIN,
    ) -> None:
        self._workspace_id = workspace_id
        self._tenant_id = tenant_id
        self._channel = channel
        self._expected = expected_sha256_hex.lower()
        self._model_path = model_bin_path

    async def verify(self) -> ModelIntegrityResult:
        """Ejecuta la verificación. Lanza si el modelo está ausente o tampered.

        Debe llamarse ANTES de que el workspace emita ``training_ready``.

        Raises:
            WhisperModelMissing: si el archivo no existe.
            WhisperModelTampered: si el digest no coincide.
        """
        if not self._model_path.exists():
            await self._emit_audit("whisper_model_missing")
            result = ModelIntegrityResult(
                workspace_id=self._workspace_id,
                tenant_id=self._tenant_id,
                model_path=str(self._model_path),
                sha256_hex="",
                verified_at=datetime.now(tz=UTC),
                tampered=False,
                missing=True,
            )
            await self._close_workspace("whisper_model_missing")
            raise WhisperModelMissing(
                f"Modelo Whisper no encontrado en {self._model_path}. "
                "Workspace cerrado antes de training_ready."
            )

        sha256_hex = await self._compute_sha256()

        await self._report_loaded(sha256_hex)

        if sha256_hex.lower() != self._expected:
            await self._emit_audit("whisper_model_tampered")
            await self._close_workspace("whisper_model_tampered")
            result = ModelIntegrityResult(
                workspace_id=self._workspace_id,
                tenant_id=self._tenant_id,
                model_path=str(self._model_path),
                sha256_hex=sha256_hex,
                verified_at=datetime.now(tz=UTC),
                tampered=True,
                missing=False,
            )
            raise WhisperModelTampered(
                f"Modelo Whisper tampered: digest {sha256_hex[:16]}... "
                f"no coincide con el firmado {self._expected[:16]}... "
                "Workspace cerrado antes de training_ready."
            )

        logger.info(
            "whisper_model_integrity.ok",
            extra={
                "workspace_id": str(self._workspace_id),
                "sha256_hex_prefix": sha256_hex[:16],
            },
        )
        return ModelIntegrityResult(
            workspace_id=self._workspace_id,
            tenant_id=self._tenant_id,
            model_path=str(self._model_path),
            sha256_hex=sha256_hex,
            verified_at=datetime.now(tz=UTC),
            tampered=False,
            missing=False,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _compute_sha256(self) -> str:
        """SHA-256 del modelo en el threadpool (no bloquea el event loop)."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._sha256_file)

    def _sha256_file(self) -> str:
        h = hashlib.sha256()
        with open(self._model_path, "rb") as f:
            while chunk := f.read(_READ_CHUNK):
                h.update(chunk)
        return h.hexdigest()

    async def _report_loaded(self, sha256_hex: str) -> None:
        """Reporta al control plane el digest calculado."""
        await self._channel.send_command(
            "whisper_model_loaded",
            {
                "workspace_id": str(self._workspace_id),
                "tenant_id": str(self._tenant_id),
                "sha256_hex": sha256_hex,
                "model_path": str(self._model_path),
                "reported_at": datetime.now(tz=UTC).isoformat(),
            },
        )

    async def _emit_audit(self, audit_kind: str) -> None:
        logger.error(
            f"whisper_model_integrity.{audit_kind}",
            extra={
                "workspace_id": str(self._workspace_id),
                "tenant_id": str(self._tenant_id),
                "model_path": str(self._model_path),
            },
        )
        await self._channel.send_command(
            "audit_entry",
            {
                "workspace_id": str(self._workspace_id),
                "tenant_id": str(self._tenant_id),
                "audit_kind": audit_kind,
                "occurred_at": datetime.now(tz=UTC).isoformat(),
            },
        )

    async def _close_workspace(self, reason: str) -> None:
        """Ordena al control plane cerrar el workspace (state=closed)."""
        await self._channel.send_command(
            "close_workspace",
            {
                "workspace_id": str(self._workspace_id),
                "tenant_id": str(self._tenant_id),
                "reason": reason,
                "closed_at": datetime.now(tz=UTC).isoformat(),
            },
        )
