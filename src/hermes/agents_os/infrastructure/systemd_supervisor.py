"""SystemdSupervisor — adapter SystemSupervisorPort sobre systemd real.

Solo se importa cuando estamos en un nodo con systemd disponible. En
unit tests usa el `_FakeSupervisor` de los tests; en integration tests
sobre VM usa este adapter.

NO se importa transitivamente desde el dominio — la única dependencia
externa es subprocess (estándar) + el path absoluto a `systemctl`.

Idempotencia:
  - `systemctl mask` aplicado dos veces no produce diff.
  - `tee /etc/systemd/logind.conf.d/agents-os-always-on.conf` reescribe
    el archivo completo cada vez (no append).
"""

from __future__ import annotations

import logging
import shlex
import subprocess
from collections.abc import Sequence
from pathlib import Path

from hermes.agents_os.domain.always_on_policy import CriticalService

logger = logging.getLogger(__name__)

_SYSTEMCTL = "/usr/bin/systemctl"
_LOGIND_DROPIN_PATH = Path(
    "/etc/systemd/logind.conf.d/agents-os-always-on.conf"
)


class SystemdSupervisor:
    """Implementación SystemSupervisorPort para nodos con systemd.

    Args:
        systemctl_path: override del binario (útil para integration tests
            en contenedor).
        logind_dropin_path: override del archivo de override.
        dry_run: si True, registra las acciones pero no las ejecuta.
            Útil para wizard de provisioning antes del bootc switch.
    """

    def __init__(
        self,
        *,
        systemctl_path: str = _SYSTEMCTL,
        logind_dropin_path: Path = _LOGIND_DROPIN_PATH,
        dry_run: bool = False,
    ) -> None:
        self._systemctl = systemctl_path
        self._logind_path = logind_dropin_path
        self._dry_run = dry_run

    def mask_targets(self, targets: Sequence[str]) -> None:
        for target in targets:
            self._run([self._systemctl, "mask", target])

    def unmask_targets(self, targets: Sequence[str]) -> None:
        for target in targets:
            self._run([self._systemctl, "unmask", target])

    def write_logind_override(self, key_values: dict[str, str]) -> None:
        body = "[Login]\n" + "\n".join(
            f"{key}={value}" for key, value in sorted(key_values.items())
        ) + "\n"
        if self._dry_run:
            logger.info("dry_run write %s\n%s", self._logind_path, body)
            return
        self._logind_path.parent.mkdir(parents=True, exist_ok=True)
        self._logind_path.write_text(body, encoding="utf-8")
        # Recarga sin reiniciar la sesión activa.
        self._run([self._systemctl, "kill", "-s", "HUP", "systemd-logind"])

    def ensure_service_unit(self, service: CriticalService) -> None:
        # Las unidades se baquean en la imagen OCI bootc. Aquí solo
        # garantizamos que estén `enabled` con la política Restart=always.
        self._run([self._systemctl, "enable", service.name])
        # Si está parada, la levantamos.
        self._run([self._systemctl, "start", service.name])

    def list_active_critical_services(self) -> tuple[str, ...]:
        if self._dry_run:
            return ()
        out = subprocess.run(
            [
                self._systemctl,
                "list-units",
                "--type=service",
                "--state=active",
                "--no-legend",
                "--plain",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if out.returncode != 0:
            logger.warning("list-units failed: %s", out.stderr)
            return ()
        return tuple(
            line.split()[0]
            for line in out.stdout.splitlines()
            if line.startswith("hermes-")
        )

    def suspend_system(self) -> None:
        # Solo se llama vía AlwaysOnSupervisor.suspend_with_authorization
        # — la ruta autorizada. Aquí asumimos que ya pasó por la guard.
        self._run([self._systemctl, "suspend"])

    def _run(self, argv: list[str]) -> None:
        if self._dry_run:
            logger.info("dry_run exec %s", shlex.join(argv))
            return
        result = subprocess.run(argv, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            logger.error(
                "systemctl failed cmd=%s rc=%d stderr=%s",
                shlex.join(argv),
                result.returncode,
                result.stderr,
            )
            raise RuntimeError(
                f"systemctl call {shlex.join(argv)} returned {result.returncode}: "
                f"{result.stderr.strip()}"
            )
