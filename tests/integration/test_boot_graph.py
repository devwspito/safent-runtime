"""T018 — test_boot_graph.py: verifica la topología del grafo de boot de US1.

Comprueba que las directivas de orden y dependencia de los units de systemd
son coherentes con la inversión del boot (PIEZA 3):

  1. hermes-runtime-ready.target: Before=multi-user.target,
     Requires=hermes-runtime.service, After=hermes-runtime.service,
     DefaultDependencies=no.
  2. GDM drop-in: After=agents-os-healthy.target,
     Wants=agents-os-healthy.target.
  3. hermes-shell-server.service: After=hermes-runtime.service,
     Requires=hermes-runtime.service.
  4. hermes-runtime.service: NotifyAccess=main (no =all, no =none).
  5. 0 ciclos de dependencia detectados por systemd-analyze verify
     (cuando está disponible).

Estos tests parsean las directivas directamente de los ficheros para que
corran sin systemd instalado (CI base). La verificación con systemd-analyze
es opt-in — sólo si el binario está presente.

NO requieren root, VM ni servicios activos.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_OPS = Path(__file__).parents[2] / "ops" / "agents-os-edition"
_SYSTEMD = _OPS / "systemd"
_GDM_DROPIN = _OPS / "gdm" / "gdm.service.d" / "10-hermes-agent-gate.conf"

_READY_TARGET = _SYSTEMD / "hermes-runtime-ready.target"
_HEALTHY_TARGET = _SYSTEMD / "agents-os-healthy.target"
_SHELL_SERVER = _SYSTEMD / "hermes-shell-server.service"
_RUNTIME_SVC = _SYSTEMD / "hermes-runtime.service"
_RESCUE_TARGET = _SYSTEMD / "hermes-rescue.target"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _directives(path: Path) -> dict[str, list[str]]:
    """Parsea directivas key=value de un unit file systemd.

    Devuelve dict[directive_lower] -> [value1, value2, ...] acumulando
    múltiples líneas del mismo nombre (After=, Wants=, etc. pueden aparecer
    varias veces).
    """
    result: dict[str, list[str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().lower()
        value = value.strip()
        result.setdefault(key, []).append(value)
    return result


def _tokens(directives: dict[str, list[str]], key: str) -> set[str]:
    """Expande todos los valores de una directiva en tokens individuales."""
    tokens: set[str] = set()
    for val in directives.get(key.lower(), []):
        tokens.update(val.split())
    return tokens


# ---------------------------------------------------------------------------
# Tests: hermes-runtime-ready.target (T021)
# ---------------------------------------------------------------------------


class TestHermesRuntimeReadyTarget:
    """hermes-runtime-ready.target ancla la liveness del agente antes de multi-user."""

    def test_file_exists(self) -> None:
        assert _READY_TARGET.exists(), (
            f"hermes-runtime-ready.target no existe en {_READY_TARGET}"
        )

    def test_before_multi_user(self) -> None:
        """Before=multi-user.target: el gate de liveness se alcanza antes del grafo normal."""
        d = _directives(_READY_TARGET)
        before = _tokens(d, "before")
        assert "multi-user.target" in before, (
            f"hermes-runtime-ready.target debe tener Before=multi-user.target; "
            f"Before actual: {before}"
        )

    def test_requires_hermes_runtime(self) -> None:
        """Requires=hermes-runtime.service: el target falla si el servicio falla."""
        d = _directives(_READY_TARGET)
        requires = _tokens(d, "requires")
        assert "hermes-runtime.service" in requires, (
            f"hermes-runtime-ready.target debe tener Requires=hermes-runtime.service; "
            f"Requires actual: {requires}"
        )

    def test_after_hermes_runtime(self) -> None:
        """After=hermes-runtime.service: no evaluar el gate antes de que arranque el daemon."""
        d = _directives(_READY_TARGET)
        after = _tokens(d, "after")
        assert "hermes-runtime.service" in after, (
            f"hermes-runtime-ready.target debe tener After=hermes-runtime.service; "
            f"After actual: {after}"
        )

    def test_default_dependencies_no(self) -> None:
        """DefaultDependencies=no evita ciclos implícitos con targets de alto nivel."""
        d = _directives(_READY_TARGET)
        vals = d.get("defaultdependencies", [])
        assert any(v.lower() == "no" for v in vals), (
            f"hermes-runtime-ready.target debe tener DefaultDependencies=no; "
            f"valor actual: {vals}"
        )

    def test_after_sysinit(self) -> None:
        """After=sysinit.target: asegura dispositivos y mounts básicos antes del gate."""
        d = _directives(_READY_TARGET)
        after = _tokens(d, "after")
        assert "sysinit.target" in after, (
            f"hermes-runtime-ready.target debe tener After=sysinit.target; "
            f"After actual: {after}"
        )


# ---------------------------------------------------------------------------
# Tests: GDM drop-in (T022)
# ---------------------------------------------------------------------------


class TestGdmAgentGateDropin:
    """El drop-in de GDM ancla el arranque gráfico al estado del agente."""

    def test_dropin_file_exists(self) -> None:
        assert _GDM_DROPIN.exists(), (
            f"GDM drop-in no existe en {_GDM_DROPIN}"
        )

    def test_after_agents_os_healthy(self) -> None:
        """After=agents-os-healthy.target: GDM espera al target que lleva el JobTimeout."""
        d = _directives(_GDM_DROPIN)
        after = _tokens(d, "after")
        assert "agents-os-healthy.target" in after, (
            f"GDM drop-in debe tener After=agents-os-healthy.target; "
            f"After actual: {after}"
        )

    def test_wants_agents_os_healthy(self) -> None:
        """Wants=agents-os-healthy.target: GDM activa el target (sin hard-fail en dev)."""
        d = _directives(_GDM_DROPIN)
        wants = _tokens(d, "wants")
        assert "agents-os-healthy.target" in wants, (
            f"GDM drop-in debe tener Wants=agents-os-healthy.target; "
            f"Wants actual: {wants}"
        )

    def test_no_requires_to_avoid_brick_in_dev(self) -> None:
        """El drop-in NO usa Requires= (sería brick en imágenes sin agents-os targets)."""
        d = _directives(_GDM_DROPIN)
        requires = _tokens(d, "requires")
        assert "agents-os-healthy.target" not in requires, (
            "GDM drop-in NO debe usar Requires=agents-os-healthy.target "
            "(Wants es suficiente y no brickea entornos de desarrollo sin el target)"
        )


# ---------------------------------------------------------------------------
# Tests: hermes-shell-server.service (T023)
# ---------------------------------------------------------------------------


class TestShellServerDependency:
    """hermes-shell-server.service depende del daemon runtime."""

    def test_after_hermes_runtime(self) -> None:
        """After=hermes-runtime.service: el server no arranca antes del daemon."""
        d = _directives(_SHELL_SERVER)
        after = _tokens(d, "after")
        assert "hermes-runtime.service" in after, (
            f"hermes-shell-server.service debe tener After=hermes-runtime.service; "
            f"After actual: {after}"
        )

    def test_requires_hermes_runtime(self) -> None:
        """Requires=hermes-runtime.service: si el daemon falla, el server también falla."""
        d = _directives(_SHELL_SERVER)
        requires = _tokens(d, "requires")
        assert "hermes-runtime.service" in requires, (
            f"hermes-shell-server.service debe tener Requires=hermes-runtime.service; "
            f"Requires actual: {requires}"
        )


# ---------------------------------------------------------------------------
# Tests: hermes-runtime.service (T024)
# ---------------------------------------------------------------------------


class TestRuntimeNotifyAccess:
    """hermes-runtime.service tiene NotifyAccess=main (anti-spoof READY=1)."""

    def test_notify_access_is_main(self) -> None:
        """NotifyAccess=main: sólo el PID principal puede enviar READY=1/WATCHDOG=1."""
        d = _directives(_RUNTIME_SVC)
        vals = d.get("notifyaccess", [])
        assert vals, (
            "hermes-runtime.service debe tener NotifyAccess= configurado explícitamente"
        )
        assert all(v.lower() == "main" for v in vals), (
            f"hermes-runtime.service: NotifyAccess debe ser 'main', no {vals}. "
            "NotifyAccess=all permitiría que código de terceros falsifique READY=1."
        )

    def test_notify_access_not_all(self) -> None:
        """NotifyAccess≠all: cierra el vector de spoof de liveness."""
        d = _directives(_RUNTIME_SVC)
        vals = d.get("notifyaccess", [])
        assert not any(v.lower() == "all" for v in vals), (
            "hermes-runtime.service NO debe tener NotifyAccess=all (CTRL-P1-17 / G3)"
        )


# ---------------------------------------------------------------------------
# Tests: hermes-rescue.target (T026)
# ---------------------------------------------------------------------------


class TestHermesRescueTarget:
    """hermes-rescue.target provee consola AUTENTICADA (no auto-root)."""

    def test_file_exists(self) -> None:
        assert _RESCUE_TARGET.exists(), (
            f"hermes-rescue.target no existe en {_RESCUE_TARGET}"
        )

    def test_allow_isolate(self) -> None:
        """AllowIsolate=yes: se puede llegar con systemctl isolate."""
        d = _directives(_RESCUE_TARGET)
        vals = d.get("allowisolate", [])
        assert any(v.lower() == "yes" for v in vals), (
            "hermes-rescue.target debe tener AllowIsolate=yes"
        )

    def test_default_dependencies_no(self) -> None:
        """DefaultDependencies=no: target de bajo nivel, sin deps implícitas de alto nivel."""
        d = _directives(_RESCUE_TARGET)
        vals = d.get("defaultdependencies", [])
        assert any(v.lower() == "no" for v in vals), (
            "hermes-rescue.target debe tener DefaultDependencies=no"
        )

    def test_no_autologin_in_rescue_target(self) -> None:
        """El target de rescate NO configura autologin — autenticación obligatoria."""
        content = _RESCUE_TARGET.read_text(encoding="utf-8").lower()
        # ExecStart con --autologin sería un auto-root; lo detectamos aquí.
        assert "--autologin" not in content, (
            "hermes-rescue.target NO debe contener --autologin (F-1: rescate autenticado)"
        )


# ---------------------------------------------------------------------------
# Test: 0 ciclos de dependencia (systemd-analyze verify, opt-in)
# ---------------------------------------------------------------------------


class TestNoDependencyCycles:
    """systemd-analyze verify no detecta ciclos en los units nuevos (cuando disponible)."""

    @pytest.mark.skipif(
        shutil.which("systemd-analyze") is None,
        reason="systemd-analyze no disponible — skipping cycle check",
    )
    def test_no_cycles_in_new_units(self, tmp_path: Path) -> None:
        """systemd-analyze verify sobre los units del boot graph devuelve 0 errores de ciclo.

        Copia los units relevantes a un directorio temporal y los verifica con
        `systemd-analyze verify --root=<tmpdir>`. Sólo se consideran errores
        fatales las líneas que contienen 'cycle' o 'circular dependency'.

        Nota: systemd-analyze verify puede reportar warnings sobre units
        faltantes (e.g. dbus.socket no presente en el tmpdir). Estos warnings
        son esperados y se ignoran — sólo los ciclos son bloqueantes.
        """
        # Copiar los units relevantes al tmpdir para que systemd-analyze
        # pueda cargarlos sin acceder al sistema real.
        unit_dir = tmp_path / "etc" / "systemd" / "system"
        unit_dir.mkdir(parents=True)

        units_to_check = [
            _READY_TARGET,
            _HEALTHY_TARGET,
            _SHELL_SERVER,
            _RUNTIME_SVC,
            _RESCUE_TARGET,
        ]
        for u in units_to_check:
            if u.exists():
                (unit_dir / u.name).write_bytes(u.read_bytes())

        result = subprocess.run(
            [
                "systemd-analyze",
                "verify",
                "--root", str(tmp_path),
            ]
            + [str(unit_dir / u.name) for u in units_to_check if u.exists()],
            capture_output=True,
            text=True,
        )

        # Filtrar sólo líneas que indican ciclos — ignorar warnings de units
        # faltantes que son esperados en un directorio parcial.
        cycle_lines = [
            line
            for line in (result.stdout + result.stderr).splitlines()
            if "cycle" in line.lower() or "circular" in line.lower()
        ]

        assert not cycle_lines, (
            "systemd-analyze verify detectó ciclos de dependencia:\n"
            + "\n".join(cycle_lines)
        )
