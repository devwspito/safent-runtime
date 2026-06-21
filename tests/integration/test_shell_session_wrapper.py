"""T020 — test_shell_session_wrapper.py: el wrapper hace exit 1 si runtime≠active.

Verifica el comportamiento del hermes-shell-session-wrapper (T025 / CTRL-P1-16):

  1. Si `systemctl is-active hermes-runtime.service` devuelve != 'active',
     el wrapper termina con exit code 1 (fail-hard, sin silencio).
  2. Si el runtime está active, el wrapper llega hasta exec mutter (o el
     punto equivalente en el test).
  3. No existe un loop de poll silencioso de /healthz (el poll fue eliminado
     en T025).
  4. No hay TOCTOU observable entre el check y el exec — el check es el único
     punto de decisión antes de exec mutter.

Estrategia: se inyecta un 'systemctl' falso (mock) en el PATH del subproceso
para controlar la respuesta de `is-active` sin requerir systemd real.
El test ejecuta el script en bash con el PATH modificado.

NO requiere root, VM ni systemd activo.
"""

from __future__ import annotations

import os
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

_WRAPPER = (
    Path(__file__).parents[2]
    / "ops"
    / "agents-os-edition"
    / "scripts"
    / "hermes-shell-session-wrapper"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_systemctl(tmp_path: Path, is_active_output: str, exit_code: int) -> Path:
    """Crea un 'systemctl' falso que responde a 'is-active' con la salida dada.

    Para otros subcomandos (--quiet, etc.) también devuelve el exit_code.
    El script imprime is_active_output en stdout cuando se llama con is-active.
    """
    fake = tmp_path / "systemctl"
    # El script falso responde a 'is-active' y 'is-active --quiet'.
    # Cualquier otro subcomando sale con 0 para no interferir.
    fake.write_text(
        textwrap.dedent(f"""\
            #!/usr/bin/env bash
            # Fake systemctl para tests de hermes-shell-session-wrapper
            for arg in "$@"; do
                case "$arg" in
                    is-active)
                        echo "{is_active_output}"
                        exit {exit_code}
                        ;;
                    --quiet)
                        # Modo silencioso: sólo exit code
                        exit {exit_code}
                        ;;
                esac
            done
            exit 0
        """),
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return fake.parent


def _run_wrapper(
    tmp_path: Path,
    *,
    is_active_output: str,
    systemctl_exit: int,
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Ejecuta el wrapper con un systemctl falso inyectado en PATH.

    El script se ejecuta hasta que:
    - exit 1 (runtime no active) → se captura el código de salida.
    - exec mutter → fallará porque mutter no existe en el entorno CI;
      el exit code será != 0 pero != 1 (error de 'command not found').

    Para distinguir entre exit-1-por-liveness y exit-por-mutter-no-encontrado,
    se inyecta también un 'mutter' falso que siempre sale con código 42.
    """
    bin_dir = _make_fake_systemctl(tmp_path, is_active_output, systemctl_exit)

    # Fake mutter: si llegamos aquí, el check de liveness pasó.
    fake_mutter = tmp_path / "mutter"
    fake_mutter.write_text(
        textwrap.dedent("""\
            #!/usr/bin/env bash
            # Fake mutter: señaliza que el liveness check pasó
            exit 42
        """),
        encoding="utf-8",
    )
    fake_mutter.chmod(fake_mutter.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP)

    # Fake gsettings (silencioso, no interfiere).
    fake_gsettings = tmp_path / "gsettings"
    fake_gsettings.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    fake_gsettings.chmod(fake_gsettings.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP)

    # Fake systemd-cat (logging).
    fake_sdcat = tmp_path / "systemd-cat"
    fake_sdcat.write_text("#!/usr/bin/env bash\ncat\n", encoding="utf-8")
    fake_sdcat.chmod(fake_sdcat.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP)

    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}"}
    if env_overrides:
        env.update(env_overrides)

    return subprocess.run(
        ["bash", str(_WRAPPER)],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestShellSessionWrapperLivenessCheck:
    """El wrapper falla duro (exit 1) si hermes-runtime.service no está active."""

    def test_wrapper_exists(self) -> None:
        assert _WRAPPER.exists(), f"Wrapper no encontrado en {_WRAPPER}"

    def test_exit_1_when_runtime_inactive(self, tmp_path: Path) -> None:
        """exit 1 inmediato cuando systemctl is-active devuelve 'inactive'."""
        result = _run_wrapper(
            tmp_path,
            is_active_output="inactive",
            systemctl_exit=3,  # systemd: 3 = unit not active
        )
        assert result.returncode == 1, (
            f"El wrapper debe salir con exit 1 cuando el runtime está inactive; "
            f"salió con {result.returncode}.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_exit_1_when_runtime_failed(self, tmp_path: Path) -> None:
        """exit 1 cuando systemctl is-active devuelve 'failed' (agotó StartLimitBurst)."""
        result = _run_wrapper(
            tmp_path,
            is_active_output="failed",
            systemctl_exit=3,
        )
        assert result.returncode == 1, (
            f"El wrapper debe salir con exit 1 cuando el runtime está failed; "
            f"salió con {result.returncode}.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_exit_1_when_runtime_activating(self, tmp_path: Path) -> None:
        """exit 1 cuando systemctl is-active devuelve 'activating' (aún no listo).

        El gate de boot garantiza que este wrapper sólo corre tras READY=1,
        pero puede re-ejecutarse después de un relogueo. En ese momento el
        daemon podría estar reiniciándose — fail-hard también aquí.
        """
        result = _run_wrapper(
            tmp_path,
            is_active_output="activating",
            systemctl_exit=3,
        )
        assert result.returncode == 1, (
            f"El wrapper debe salir con exit 1 cuando el runtime está activating; "
            f"salió con {result.returncode}.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_reaches_mutter_when_runtime_active(self, tmp_path: Path) -> None:
        """Cuando el runtime está active, el wrapper llega a exec mutter (exit 42 del fake)."""
        result = _run_wrapper(
            tmp_path,
            is_active_output="active",
            systemctl_exit=0,  # systemd: 0 = unit is active
        )
        # exit 42 = fake mutter fue invocado → liveness check pasó
        assert result.returncode == 42, (
            f"Cuando el runtime está active, el wrapper debe llegar a exec mutter "
            f"(exit 42 del fake); salió con {result.returncode}.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_no_silent_healthz_poll_in_script(self) -> None:
        """El wrapper no contiene código activo que consulte /healthz (eliminado en T025).

        Los comentarios que explican POR QUÉ se eliminó el poll son aceptables
        (documentan la decisión). Lo que no debe existir son llamadas activas
        a /healthz (curl, wget, etc.) fuera de líneas comentadas.
        """
        content = _WRAPPER.read_text(encoding="utf-8")
        # Filtrar líneas activas (no comentarios) que contengan /healthz.
        active_healthz_lines = [
            line for line in content.splitlines()
            if "/healthz" in line and not line.strip().startswith("#")
        ]
        assert not active_healthz_lines, (
            "hermes-shell-session-wrapper NO debe tener código activo con '/healthz' — "
            "el poll silencioso fue eliminado en T025. "
            "La liveness se verifica via 'systemctl is-active', no via HTTP. "
            f"Líneas activas encontradas: {active_healthz_lines}"
        )

    def test_no_curl_poll_loop(self) -> None:
        """El wrapper no contiene un bucle curl de best-effort (TOCTOU eliminado)."""
        content = _WRAPPER.read_text(encoding="utf-8")
        # Buscar el patrón del loop anterior: `for i in ... curl`
        # Permitimos `curl` en comentarios (WHY), pero no en código activo.
        active_lines = [
            line for line in content.splitlines()
            if "curl" in line and not line.strip().startswith("#")
        ]
        assert not active_lines, (
            "hermes-shell-session-wrapper NO debe tener llamadas activas a curl — "
            f"líneas encontradas: {active_lines}"
        )

    def test_error_message_mentions_runtime_unit(self, tmp_path: Path) -> None:
        """El mensaje de error al fallar menciona el nombre de la unidad (diagnóstico claro)."""
        result = _run_wrapper(
            tmp_path,
            is_active_output="inactive",
            systemctl_exit=3,
        )
        combined = result.stdout + result.stderr
        assert "hermes-runtime.service" in combined, (
            "El mensaje de error al fallar debe mencionar 'hermes-runtime.service' "
            "para facilitar el diagnóstico desde el journal."
        )

    def test_single_check_point_before_exec(self) -> None:
        """El wrapper tiene exactamente un punto de decisión de liveness (sin TOCTOU loop)."""
        content = _WRAPPER.read_text(encoding="utf-8")
        # Contar las llamadas activas a systemctl is-active (no en comentarios).
        active_checks = [
            line for line in content.splitlines()
            if "systemctl" in line
            and "is-active" in line
            and not line.strip().startswith("#")
        ]
        # Debe haber exactamente 1 check (el if) o 2 como máximo si hay un mensaje
        # de diagnóstico adicional (el `systemctl is-active` en el echo de error).
        # Lo importante es que NO hay un loop (for/while).
        assert len(active_checks) <= 2, (
            f"El wrapper debe tener como máximo 2 llamadas a 'systemctl is-active' "
            f"(1 check + 1 diagnóstico opcional); encontradas {len(active_checks)}:\n"
            + "\n".join(active_checks)
        )

        # Verificar que no hay un loop while/for que llame systemctl.
        lines = content.splitlines()
        in_loop = False
        loop_checks: list[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith(("for ", "while ")):
                in_loop = True
            if in_loop and "systemctl" in stripped and not stripped.startswith("#"):
                loop_checks.append(line)
            if stripped in ("done", "esac"):
                in_loop = False

        assert not loop_checks, (
            "El wrapper NO debe tener 'systemctl' dentro de un loop — "
            f"líneas encontradas: {loop_checks}"
        )
