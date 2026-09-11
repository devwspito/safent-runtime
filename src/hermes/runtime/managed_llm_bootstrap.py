"""Clean exec of the existing daemon before importing native Hermes modules.

The inherited sealed descriptor contains only a generation/profile receipt,
never an inference credential. It is not an authority grant: current SQLite
authority and profile ownership are checked again after exec.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path

from hermes.runtime.managed_llm_lifecycle import (
    Generation,
    LifecycleUnavailable,
    ProcessAdmission,
    acknowledge_boot,
    reconcile_generation,
)
from hermes.runtime.managed_llm_profile import (
    create_profile_home,
    daemon_environment,
    write_profile,
)
from hermes.security.configuration_lock import configuration_lock

_RECEIPT_ARG = "--managed-bootstrap-fd="
_RECEIPT_LIMIT = 4096
_DIGEST_LENGTH = 64
_PRIVATE_DIRECTORY_MODE = 0o700
_FIRST_NONSTANDARD_DESCRIPTOR = 3
_SEALS = (
    fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE
    if hasattr(fcntl, "F_SEAL_SEAL")
    else 0
)
_ADMISSION: ProcessAdmission | None = None


@dataclass(frozen=True)
class PendingBootstrap:
    db_path: Path
    generation: Generation
    profile: Path | None
    pid: int


def process_admission() -> ProcessAdmission | None:
    return _ADMISSION


def assert_process_admission(db_path: Path | None = None, *, managed: bool | None = None):
    """Always run BEFORE falling back to local config or building another agent."""
    admission = _ADMISSION
    if admission is None:
        if managed:
            raise LifecycleUnavailable("Corporate process bootstrap is required")
        return None
    if db_path is not None and db_path.resolve() != admission.db_path.resolve():
        admission.close()
        raise LifecycleUnavailable("Runtime configuration scope changed")
    current = admission.check()
    if managed is not None and (current.mode == "managed") != managed:
        admission.close()
        raise LifecycleUnavailable("Runtime inference identity changed")
    return current


def _receipt_fd(generation: Generation, profile: Path) -> int:
    if not _SEALS or not hasattr(os, "memfd_create"):
        raise LifecycleUnavailable("Corporate daemon bootstrap requires Linux sealed descriptors")
    fd = os.memfd_create("hermes-bootstrap", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    try:
        data = json.dumps(
            {
                "pid": os.getpid(),
                "generation": generation.generation,
                "fingerprint": generation.fingerprint,
                "mode": generation.mode,
                "profile": str(profile),
            },
            separators=(",", ":"),
        ).encode()
        if len(data) > _RECEIPT_LIMIT:
            raise LifecycleUnavailable("Corporate bootstrap receipt is invalid")
        os.write(fd, data)
        os.lseek(fd, 0, os.SEEK_SET)
        fcntl.fcntl(fd, fcntl.F_ADD_SEALS, _SEALS)
        os.set_inheritable(fd, True)
        return fd
    except Exception:
        os.close(fd)
        raise


def _read_receipt(fd: int, db_path: Path) -> tuple[Generation, Path]:
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or not 0 < info.st_size <= _RECEIPT_LIMIT
            or fcntl.fcntl(fd, fcntl.F_GET_SEALS) & _SEALS != _SEALS
        ):
            raise ValueError("unsealed receipt")
        data = json.loads(os.read(fd, _RECEIPT_LIMIT + 1))
        if (
            set(data) != {"pid", "generation", "fingerprint", "mode", "profile"}
            or data["pid"] != os.getpid()
            or type(data["generation"]) is not int
            or data["generation"] < 1
            or not isinstance(data["fingerprint"], str)
            or len(data["fingerprint"]) != _DIGEST_LENGTH
            or data["mode"] not in {"managed", "blocked"}
        ):
            raise ValueError("invalid receipt")
        profile = Path(data["profile"])
        expected_root = db_path.absolute().parent.resolve(strict=True) / "managed-profiles"
        if (
            profile.parent != expected_root
            or not profile.name.startswith(f"g{data['generation']}-")
            or os.environ.get("HERMES_HOME") != str(profile)
            or os.environ.get("HOME") != str(profile)
        ):
            raise ValueError("invalid profile scope")
        if dict(os.environ) != daemon_environment(os.environ, profile):
            raise ValueError("process environment was not cleanly projected")
        for path in (expected_root, profile):
            entry = path.lstat()
            if (
                not stat.S_ISDIR(entry.st_mode)
                or entry.st_uid != os.geteuid()
                or stat.S_IMODE(entry.st_mode) != _PRIVATE_DIRECTORY_MODE
            ):
                raise ValueError("unsafe profile")
        if any(profile.iterdir()):
            raise ValueError("profile was used before bootstrap")
        if any(
            name == "run_agent" or name.startswith(("agent.", "hermes_cli."))
            for name in sys.modules
        ):
            raise ValueError("native runtime imported before clean bootstrap")
        return Generation(data["generation"], data["fingerprint"], data["mode"], True), profile
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        raise LifecycleUnavailable("Corporate bootstrap receipt could not be verified") from None
    finally:
        os.close(fd)


def initialize_process(db_path: Path, *, argv=None, exec_fn=os.execve):
    """Called by daemon main before any native runtime import or admission.

    Metadata only: no factory, native import, vault access or admission here.
    _run completes the pending bootstrap only after its existing confinement.
    Tests may inject exec_fn, never an alternate production inference resolver.
    """
    if _ADMISSION is not None:
        raise LifecycleUnavailable("Runtime bootstrap may only run once per process")
    args = list(sys.argv[1:] if argv is None else argv)
    markers = [arg for arg in args if arg.startswith(_RECEIPT_ARG)]
    if len(markers) > 1:
        raise LifecycleUnavailable("Corporate bootstrap receipt is ambiguous")
    with configuration_lock(db_path):
        if markers:
            try:
                fd = int(markers[0].removeprefix(_RECEIPT_ARG))
                if fd < _FIRST_NONSTANDARD_DESCRIPTOR:
                    raise ValueError("reserved descriptor")
            except ValueError:
                raise LifecycleUnavailable("Corporate bootstrap receipt is invalid") from None
            expected, profile = _read_receipt(fd, db_path)
            current = reconcile_generation(db_path)
            if (current.generation, current.fingerprint, current.mode) != (
                expected.generation,
                expected.fingerprint,
                expected.mode,
            ):
                raise LifecycleUnavailable("Corporate authority changed during clean exec")
            return PendingBootstrap(db_path, current, profile, os.getpid())
        current = reconcile_generation(db_path)
        if current.mode == "local":
            return PendingBootstrap(db_path, current, None, os.getpid())
        profile = create_profile_home(db_path, current.generation)
        fd = _receipt_fd(current, profile)
        env = daemon_environment(os.environ, profile)
        env["HERMES_SHELL_DB"] = str(db_path.absolute())
    # No open SQLite transaction or held config lock may survive exec.
    try:
        exec_fn(
            sys.executable,
            [sys.executable, "-m", "hermes.runtime", *args, f"{_RECEIPT_ARG}{fd}"],
            env,
        )
        raise LifecycleUnavailable("Corporate daemon exec unexpectedly returned")
    finally:
        os.close(fd)


def complete_process_bootstrap(pending: PendingBootstrap, *, profile_factory):
    """Called only AFTER _run's existing kernel confinement has succeeded."""
    global _ADMISSION  # noqa: PLW0603 - one process identity, installed once after confinement
    if _ADMISSION is not None or pending.pid != os.getpid():
        raise LifecycleUnavailable("Runtime bootstrap is not pending in this process")
    with configuration_lock(pending.db_path):
        current = reconcile_generation(pending.db_path)
        if (current.generation, current.fingerprint, current.mode) != (
            pending.generation.generation,
            pending.generation.fingerprint,
            pending.generation.mode,
        ):
            raise LifecycleUnavailable("Runtime authority changed before confined bootstrap")
        if pending.profile is not None:
            write_profile(pending.profile, profile_factory(current))
        boot = acknowledge_boot(pending.db_path, current)
        _ADMISSION = ProcessAdmission(pending.db_path, boot)
        return _ADMISSION
