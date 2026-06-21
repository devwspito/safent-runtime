"""bootc_updater — adapter sobre `bootc` para OTA A/B (FR-008, FR-009).

Es el lado infrastructure de `OtaOrchestrator`. Wraps `bootc upgrade`,
`bootc rollback` y `bootc status` con subprocess + parsing.

Estados que producimos hacia OtaOrchestrator vía transition():
  - DOWNLOADING: `bootc upgrade --quiet` lanzado
  - VERIFYING: termina con rc=0 + cosign verification interna pasó
  - STAGED: el target está en el slot inactivo, no booteado aún
  - BOOTING_TARGET: se invocó `systemctl reboot --boot-loader-menu=0`
  - PROMOTED: tras reboot, bootc status reporta target como booted
  - ROLLED_BACK: si el healthy_target no se alcanzó en N min, ejecutamos
    `bootc rollback` automáticamente desde el supervisor de boot.

Este adapter NO toma decisiones de promoción — solo ejecuta. Las guards
(monotonic versioning, revocation cache, drain completed) viven en
OtaOrchestrator (application). Aquí asumimos invocación autorizada.
"""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_BOOTC = "/usr/bin/bootc"


class BootcCommandError(RuntimeError):
    """bootc subprocess returned non-zero."""


@dataclass(frozen=True, slots=True)
class BootcStatus:
    """Snapshot del estado de bootc en el nodo."""

    booted_image: str | None
    booted_digest: str | None
    booted_version: str | None
    staged_image: str | None
    staged_digest: str | None
    staged_version: str | None
    rollback_image: str | None
    captured_at: datetime


class BootcUpdater:
    """Adapter de bootc para OtaOrchestrator."""

    def __init__(
        self,
        *,
        bootc_path: str = _BOOTC,
        dry_run: bool = False,
    ) -> None:
        self._bootc = bootc_path
        self._dry_run = dry_run

    def fetch_and_stage(self, image_ref: str) -> None:
        """`bootc upgrade` con la imagen objetivo.

        Args:
            image_ref: registry/image:tag@sha256:digest (recomendado con
                digest para verificación de inmutabilidad).
        """
        self._run([self._bootc, "upgrade", "--apply", "--quiet"])

    def switch_to(self, image_ref: str) -> None:
        """Cambia el target a una imagen específica (downgrade o canal)."""
        self._run([self._bootc, "switch", "--apply", image_ref])

    def reboot_to_staged(self) -> None:
        """Reinicia al slot inactivo."""
        # systemd-reboot via bootc; no usamos `systemctl reboot` directo
        # porque bootc gestiona el bootloader entry.
        self._run([self._bootc, "reboot"])

    def rollback(self) -> None:
        """Vuelve al slot previo (no boot ejecutado, mark fallido)."""
        self._run([self._bootc, "rollback", "--apply"])

    def status(self) -> BootcStatus:
        """`bootc status --json` parseado."""
        if self._dry_run:
            return BootcStatus(
                booted_image=None,
                booted_digest=None,
                booted_version=None,
                staged_image=None,
                staged_digest=None,
                staged_version=None,
                rollback_image=None,
                captured_at=datetime.now(tz=UTC),
            )
        result = subprocess.run(
            [self._bootc, "status", "--json"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise BootcCommandError(
                f"bootc status failed rc={result.returncode}: "
                f"{result.stderr.strip()}"
            )
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise BootcCommandError(f"bootc status JSON invalid: {exc}") from exc
        return self._parse_status(data)

    @staticmethod
    def _parse_status(data: dict) -> BootcStatus:
        """Mapea el JSON de bootc al BootcStatus inmutable."""
        def _slot(name: str) -> tuple[str | None, str | None, str | None]:
            slot = data.get("status", {}).get(name)
            if not isinstance(slot, dict):
                return None, None, None
            image = slot.get("image", {})
            return (
                image.get("image") if isinstance(image, dict) else None,
                image.get("imageDigest") if isinstance(image, dict) else None,
                slot.get("ostree", {}).get("commit") if isinstance(slot.get("ostree"), dict) else None,
            )

        booted_img, booted_dig, booted_ver = _slot("booted")
        staged_img, staged_dig, staged_ver = _slot("staged")
        rollback_img, _, _ = _slot("rollback")
        return BootcStatus(
            booted_image=booted_img,
            booted_digest=booted_dig,
            booted_version=booted_ver,
            staged_image=staged_img,
            staged_digest=staged_dig,
            staged_version=staged_ver,
            rollback_image=rollback_img,
            captured_at=datetime.now(tz=UTC),
        )

    def parse_status(self, data: dict) -> BootcStatus:
        """Variante pública para tests con JSON sintético."""
        return self._parse_status(data)

    def _run(self, argv: list[str]) -> None:
        if self._dry_run:
            logger.info("dry_run exec %s", shlex.join(argv))
            return
        result = subprocess.run(argv, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise BootcCommandError(
                f"bootc cmd {shlex.join(argv)} returned "
                f"{result.returncode}: {result.stderr.strip()}"
            )


def _read_revocation_list_from_path(path: Path) -> dict:
    """Carga la revocation list firmada desde un path local.

    El verificador real (cosign) vive aparte; aquí solo deserializamos.
    """
    if not path.exists():
        raise FileNotFoundError(f"revocation list not found at {path}")
    return json.loads(path.read_text(encoding="utf-8"))
