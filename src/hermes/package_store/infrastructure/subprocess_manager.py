"""Subprocess-backed package manager adapter.

Runs `flatpak install/uninstall` and `dnf install/remove` in daemon threads
and tracks operation status by op_id — same pattern as _start_hub_op in
dbus_runtime_service.py.

Security notes:
- argv is constructed from PackageRef (domain type with validated package_id).
- shell=False everywhere. No user-supplied string enters subprocess as a
  single shell argument.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import uuid

from hermes.package_store.domain.package import (
    PackageOpResult,
    PackageOpStatus,
    PackageOpStatusSnapshot,
    PackageRef,
    PackageSource,
)

logger = logging.getLogger("hermes.package_store.manager")

_SUBPROCESS_TIMEOUT = 300  # 5 min max for installs

_OPS: dict[str, dict] = {}
_OPS_LOCK = threading.Lock()


class SubprocessPackageManager:
    """Starts install/uninstall threads; state polled via get_op_status."""

    def start_install(self, ref: PackageRef) -> PackageOpResult:
        op_id = _start_op("install", ref)
        return PackageOpResult(op_id=op_id, ref=ref)

    def start_uninstall(self, ref: PackageRef) -> PackageOpResult:
        op_id = _start_op("uninstall", ref)
        return PackageOpResult(op_id=op_id, ref=ref)

    def get_op_status(self, op_id: str) -> PackageOpStatusSnapshot:
        with _OPS_LOCK:
            op = _OPS.get(op_id)
        if op is None:
            return PackageOpStatusSnapshot(op_id=op_id, status=PackageOpStatus.UNKNOWN)
        return PackageOpStatusSnapshot(
            op_id=op_id,
            status=PackageOpStatus(op["status"]),
            log_tail=op.get("log_tail", ""),
            error_message=op.get("error_message", ""),
        )


# ---------------------------------------------------------------------------
# Module-level state + thread launcher
# ---------------------------------------------------------------------------


def _start_op(kind: str, ref: PackageRef) -> str:
    op_id = uuid.uuid4().hex
    with _OPS_LOCK:
        _OPS[op_id] = {"status": PackageOpStatus.PENDING.value, "kind": kind}

    argv = _build_argv(kind, ref)
    thread = threading.Thread(
        target=_run_op,
        args=(op_id, kind, ref, argv),
        daemon=True,
        name=f"pkg-{kind}-{op_id[:6]}",
    )
    thread.start()
    return op_id


def _build_argv(kind: str, ref: PackageRef) -> list[str]:
    if ref.source == PackageSource.FLATPAK:
        return _flatpak_argv(kind, ref.package_id)
    return _rpm_argv(kind, ref.package_id)


def _flatpak_argv(kind: str, package_id: str) -> list[str]:
    if kind == "install":
        return ["flatpak", "install", "--user", "--noninteractive", "flathub", package_id]
    return ["flatpak", "uninstall", "--user", "--noninteractive", package_id]


def _rpm_argv(kind: str, package_id: str) -> list[str]:
    if kind == "install":
        return ["pkexec", "dnf", "install", "-y", package_id]
    return ["pkexec", "dnf", "remove", "-y", package_id]


def _run_op(op_id: str, kind: str, ref: PackageRef, argv: list[str]) -> None:
    _set_status(op_id, PackageOpStatus.RUNNING)
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_SUBPROCESS_TIMEOUT,
            shell=False,
        )
        log_tail = _tail(result.stdout + result.stderr)
        if result.returncode == 0:
            _set_done(op_id, log_tail)
            logger.info(
                "hermes.package_store.op_done",
                extra={"kind": kind, "source": ref.source, "package_id": ref.package_id},
            )
        else:
            _set_error(op_id, f"exit={result.returncode}", log_tail)
            logger.warning(
                "hermes.package_store.op_failed",
                extra={"kind": kind, "source": ref.source, "exit": result.returncode},
            )
    except subprocess.TimeoutExpired:
        _set_error(op_id, f"timeout after {_SUBPROCESS_TIMEOUT}s")
    except Exception as exc:  # noqa: BLE001
        _set_error(op_id, str(exc))


def _set_status(op_id: str, status: PackageOpStatus) -> None:
    with _OPS_LOCK:
        _OPS[op_id]["status"] = status.value


def _set_done(op_id: str, log_tail: str = "") -> None:
    with _OPS_LOCK:
        _OPS[op_id]["status"] = PackageOpStatus.SUCCESS.value
        _OPS[op_id]["log_tail"] = log_tail


def _set_error(op_id: str, error_message: str, log_tail: str = "") -> None:
    with _OPS_LOCK:
        _OPS[op_id]["status"] = PackageOpStatus.ERROR.value
        _OPS[op_id]["error_message"] = error_message
        _OPS[op_id]["log_tail"] = log_tail


def _tail(text: str, chars: int = 2000) -> str:
    return text[-chars:] if len(text) > chars else text
