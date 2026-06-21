"""VM validation harness — T076 — boot inversion assertions (requires_vm).

Verifica en la VM bootc real (slot D) los invariantes de arranque de la
feature 006 / US1 documentados en agents-os-healthy.target y
hermes-runtime-ready.target:

  (a) El daemon notifica READY=1 y ningún componente gráfico (GDM/GNOME)
      arrancar antes de que READY=1 sea emitido.

  (b) Daemon que no arranca → consola de rescate autenticada activada en
      ventana acotada (hermes-rescue.target ACTIVE), 0 brick.

  (c) Daemon sin modelo (HERMES_MODEL ausente) → estado sano-ocioso,
      boot completa hasta multi-user.target.

  (d) El JobTimeoutSec del gate gráfico (agents-os-healthy.target, 300s) se
      mide en wall-clock propio del slot B, no heredado del slot A anterior.

Cómo correr en la VM real:
    HERMES_VM_VALIDATION=1 pytest -m requires_vm tests/vm/ -v

Precondiciones:
  - El sistema es una imagen bootc agents-os-edition.
  - El marcador de entorno /run/agents-os existe O la variable de entorno
    HERMES_VM_VALIDATION=1 está seteada.
  - El usuario con que se corre pytest tiene acceso a `journalctl` y
    `systemctl` sin password (hermes-user en Agents OS, o sudoers).

Exclusión de CI:
    El marker `requires_vm` está en `addopts` de pyproject.toml como
    `-m 'not requires_vm'`; estos tests hacen skip limpio fuera de la VM.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Environment detection
# ---------------------------------------------------------------------------

_VM_MARKER_PATH = Path("/run/agents-os")
_VM_ENV_VAR = "HERMES_VM_VALIDATION"

_IN_VM = _VM_MARKER_PATH.exists() or os.environ.get(_VM_ENV_VAR) == "1"

_SKIP_REASON = (
    f"Requires Agents OS VM environment "
    f"({_VM_MARKER_PATH} or {_VM_ENV_VAR}=1 not detected). "
    f"Run with: HERMES_VM_VALIDATION=1 pytest -m requires_vm tests/vm/"
)


def _skip_if_not_vm() -> None:
    """Skip the current test when not running inside the VM."""
    if not _IN_VM:
        pytest.skip(_SKIP_REASON)


def _systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    """Run systemctl with the given arguments, return CompletedProcess."""
    return subprocess.run(
        ["systemctl", *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


def _journalctl(*args: str) -> str:
    """Run journalctl and return stdout."""
    result = subprocess.run(
        ["journalctl", *args],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout


# ---------------------------------------------------------------------------
# (a) READY=1 emitted before graphical components start
# ---------------------------------------------------------------------------


@pytest.mark.requires_vm
def test_runtime_emitted_ready_before_gdm() -> None:
    """hermes-runtime.service emits READY=1; GDM does not precede it.

    Parses the boot journal to verify that the hermes-runtime READY=1
    notification appears before gdm.service enters active state.
    """
    _skip_if_not_vm()

    # Fetch monotonic timestamps from this boot.
    runtime_log = _journalctl("-u", "hermes-runtime.service", "-b", "--no-pager", "-o", "short-monotonic")
    gdm_log = _journalctl("-u", "gdm.service", "-b", "--no-pager", "-o", "short-monotonic")

    # Extract first timestamp where READY=1 was sent (sd_notify log line).
    ready_match = re.search(r"^\s*\[?\s*([\d.]+)\s*\]?.*READY=1", runtime_log, re.MULTILINE)
    assert ready_match is not None, (
        "hermes-runtime.service did not emit READY=1 in this boot. "
        "Journal snippet:\n" + runtime_log[:2000]
    )
    ready_ts = float(ready_match.group(1))

    # Extract first timestamp where gdm.service became active.
    gdm_match = re.search(r"^\s*\[?\s*([\d.]+)\s*\]?.*Started.*GDM", gdm_log, re.MULTILINE | re.IGNORECASE)
    if gdm_match is None:
        # GDM not started (server profile) — invariant trivially holds.
        return

    gdm_ts = float(gdm_match.group(1))
    assert ready_ts < gdm_ts, (
        f"READY=1 at {ready_ts:.3f}s but GDM started at {gdm_ts:.3f}s — "
        "GDM preceded agent READY=1 (boot inversion violated)."
    )


# ---------------------------------------------------------------------------
# (b) Daemon not starting → rescue activated, 0 brick
# ---------------------------------------------------------------------------


@pytest.mark.requires_vm
def test_daemon_failure_activates_rescue_not_brick() -> None:
    """When hermes-runtime.service is in 'failed' state, hermes-rescue.target
    must be active. The system must NOT be in emergency/dracut-emergency mode.

    This test is designed to run AFTER a deliberate daemon failure has been
    induced in slot D (e.g., by setting ExecStart to /bin/false temporarily).
    It checks the resulting state rather than inducing it, to avoid bricking
    the test VM.
    """
    _skip_if_not_vm()

    runtime_state = _systemctl("is-failed", "hermes-runtime.service")
    if runtime_state.returncode != 0:
        pytest.skip(
            "hermes-runtime.service is not in 'failed' state in this boot. "
            "Induce failure (ExecStart=/bin/false) in slot D to exercise this path."
        )

    # Rescue target must be active.
    rescue_result = _systemctl("is-active", "hermes-rescue.target")
    assert rescue_result.returncode == 0, (
        f"hermes-rescue.target is not active after daemon failure. "
        f"stdout={rescue_result.stdout.strip()!r} stderr={rescue_result.stderr.strip()!r}"
    )

    # System must NOT be in emergency/dracut mode (0 brick guarantee).
    emergency_result = _systemctl("is-active", "emergency.target")
    assert emergency_result.returncode != 0, (
        "emergency.target is active — system is bricked instead of rescued. "
        "hermes-rescue.target should prevent this."
    )


# ---------------------------------------------------------------------------
# (c) Daemon without model → healthy-idle, boot completes
# ---------------------------------------------------------------------------


@pytest.mark.requires_vm
def test_daemon_without_model_reaches_multi_user() -> None:
    """With HERMES_MODEL unset, the runtime notifies READY=1 in idle mode and
    multi-user.target must be reached.

    This test checks the CURRENT boot state; it should be run on a boot where
    HERMES_MODEL env var was absent from the unit's EnvironmentFile.
    """
    _skip_if_not_vm()

    multi_user_result = _systemctl("is-active", "multi-user.target")
    assert multi_user_result.returncode == 0, (
        "multi-user.target is not active. Boot did not complete. "
        f"stdout={multi_user_result.stdout.strip()!r}"
    )

    runtime_result = _systemctl("is-active", "hermes-runtime.service")
    assert runtime_result.returncode == 0, (
        "hermes-runtime.service is not active. "
        "Expected sano-ocioso (idle) state when HERMES_MODEL is absent. "
        f"stdout={runtime_result.stdout.strip()!r}"
    )

    # Verify the runtime did NOT fail during this boot (no failed unit).
    failed_result = _systemctl("is-failed", "hermes-runtime.service")
    assert failed_result.returncode != 0, (
        "hermes-runtime.service entered 'failed' during an idle boot. "
        "The daemon must remain healthy-idle when no model is configured (FR-002/SC-003)."
    )


# ---------------------------------------------------------------------------
# (d) JobTimeoutSec measured in slot B wall-clock, not inherited from slot A
# ---------------------------------------------------------------------------


@pytest.mark.requires_vm
def test_job_timeout_budget_is_per_slot() -> None:
    """agents-os-healthy.target's 300s JobTimeoutSec applies to the CURRENT boot
    slot only, not carried over from a previous slot A boot.

    Verification strategy: the activation job for agents-os-healthy.target must
    have been evaluated WITHIN this boot (monotonic clock starting at 0). If the
    target activated, its job finished before 300s from boot start — independent
    of how long slot A took.

    We measure the monotonic timestamp of agents-os-healthy.target activation
    and assert it is < 300s from boot (monotonic 0).
    """
    _skip_if_not_vm()

    healthy_active = _systemctl("is-active", "agents-os-healthy.target")
    if healthy_active.returncode != 0:
        pytest.skip(
            "agents-os-healthy.target is not active in this boot. "
            "Cannot measure per-slot budget on a failed/timeout boot."
        )

    healthy_log = _journalctl(
        "-u", "agents-os-healthy.target", "-b", "--no-pager", "-o", "short-monotonic"
    )

    # Find first activation timestamp (monotonic seconds since boot = slot start).
    ts_match = re.search(r"^\s*\[?\s*([\d.]+)\s*\]?", healthy_log, re.MULTILINE)
    assert ts_match is not None, (
        "Could not parse monotonic timestamp from agents-os-healthy.target journal.\n"
        + healthy_log[:1000]
    )
    activation_mono = float(ts_match.group(1))

    # Must be well within 300s budget (use 290s margin to account for parsing lag).
    assert activation_mono < 290, (
        f"agents-os-healthy.target activated at {activation_mono:.1f}s monotonic — "
        "suspiciously close to or beyond the 300s slot budget. "
        "If slot A elapsed time was inherited, this would exceed 300s. "
        f"Check that JobTimeoutSec resets at each boot."
    )
