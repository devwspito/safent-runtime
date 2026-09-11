"""Bounded, reentrant process lock for cooperating writers of one local DB.

POSIX-only and fail-closed elsewhere. The stable empty lock inode is never
unlinked on release. This is not a boundary against a hostile process running
as the state-directory owner: that principal already controls the database.
"""

from __future__ import annotations

import errno
import math
import os
import stat
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from weakref import WeakValueDictionary

try:
    import fcntl
except ImportError:  # pragma: no cover - explicit unsupported-platform guard
    fcntl = None


class ConfigurationLockError(PermissionError):
    """Configuration authority could not be serialized safely."""


@dataclass
class _State:
    gate: threading.RLock = field(default_factory=threading.RLock)
    fd: int | None = None
    directory_fd: int | None = None
    depth: int = 0


_REGISTRY: WeakValueDictionary[str, _State] = WeakValueDictionary()
_REGISTRY_GUARD = threading.Lock()
_PRIVATE_MODE = 0o600
_MAX_TIMEOUT = 30


def _before_fork():
    # Opening/closing descriptors is protected by this same short-lived guard.
    # Never hold it while waiting on a thread gate or flock.
    _REGISTRY_GUARD.acquire()


def _after_fork_parent():
    _REGISTRY_GUARD.release()


def _after_fork_child():
    global _REGISTRY, _REGISTRY_GUARD  # noqa: PLW0603 - replace inherited thread locks
    for state in list(_REGISTRY.values()):
        for descriptor in (state.fd, state.directory_fd):
            if descriptor is not None:
                # CLOSE THE CHILD'S DUPLICATE ONLY. LOCK_UN here would release
                # the parent's shared open-file-description lock as well.
                os.close(descriptor)
        state.fd = state.directory_fd = None
        state.depth = 0
    _REGISTRY = WeakValueDictionary()
    _REGISTRY_GUARD = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(
        before=_before_fork, after_in_parent=_after_fork_parent, after_in_child=_after_fork_child
    )


def _private_directory(info):
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise ConfigurationLockError("Configuration lock directory is unsafe")


def _safe_file(info):
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != _PRIVATE_MODE
        or info.st_nlink != 1
        or info.st_size != 0
    ):
        raise ConfigurationLockError("Configuration lock file is unsafe")


def _location(db_path):
    path = Path(db_path).absolute()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent = path.parent.resolve(strict=True)
    _private_directory(parent.stat())
    # Different hardlink/symlink spellings of a DB must not produce distinct
    # locks for the same state. Parent aliases (/tmp on macOS) canonicalize.
    try:
        info = (parent / path.name).lstat()
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
            raise ConfigurationLockError("Configuration database path is unsafe")
    return parent, path.name + ".configuration.lock"


def _acquire_process_lock(state, parent, name, deadline):
    with _REGISTRY_GUARD:
        state.directory_fd = os.open(
            parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
        _private_directory(os.fstat(state.directory_fd))
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
        try:
            state.fd = os.open(
                name, flags | os.O_CREAT | os.O_EXCL, _PRIVATE_MODE, dir_fd=state.directory_fd
            )
            os.fchmod(state.fd, _PRIVATE_MODE)
        except FileExistsError:
            state.fd = os.open(name, flags, dir_fd=state.directory_fd)
        _safe_file(os.fstat(state.fd))
    while True:
        try:
            fcntl.flock(state.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError as exc:
            if exc.errno not in {errno.EAGAIN, errno.EACCES, errno.EINTR}:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ConfigurationLockError("Configuration lock timed out") from None
            time.sleep(min(0.02, remaining))
    current = os.stat(name, dir_fd=state.directory_fd, follow_symlinks=False)
    held = os.fstat(state.fd)
    _safe_file(current)
    _safe_file(held)
    _private_directory(os.fstat(state.directory_fd))
    if (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino):
        raise ConfigurationLockError("Configuration lock file changed")


@contextmanager
def configuration_lock(db_path: Path, *, timeout: float = 5.0):  # noqa: PLR0912 - cleanup mirrors acquisition and fork states
    """Serialize a bounded local commit, not network polling or inference.

    Reentry in the same thread/process is safe across separately opened SQLite
    repositories. The lock itself holds no SQLite transaction or connection.
    A process crash releases the kernel lock; no PID files or stale-lock erase.
    """
    if (
        fcntl is None
        or not hasattr(os, "O_NOFOLLOW")
        or not hasattr(os, "O_DIRECTORY")
        or isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or not 0 <= timeout <= _MAX_TIMEOUT
    ):
        raise ConfigurationLockError("Configuration lock unavailable")
    deadline = time.monotonic() + timeout
    state = None
    acquired = False
    entered = False
    pid = os.getpid()
    try:
        parent, name = _location(db_path)
        identity = str(parent / name)
        with _REGISTRY_GUARD:
            state = _REGISTRY.get(identity)
            if state is None:
                state = _State()
                _REGISTRY[identity] = state
        acquired = state.gate.acquire(timeout=max(0.0, deadline - time.monotonic()))
        if not acquired:
            raise ConfigurationLockError("Configuration lock timed out")
        if state.depth == 0:
            _acquire_process_lock(state, parent, name, deadline)
        state.depth += 1
        entered = True
        try:
            yield
        finally:
            if os.getpid() == pid:
                state.depth -= 1
    except OSError as exc:
        if entered or isinstance(exc, ConfigurationLockError):
            raise
        raise ConfigurationLockError("Configuration lock unavailable") from None
    finally:
        if acquired and os.getpid() == pid:
            if state.depth == 0:
                with _REGISTRY_GUARD:
                    # Closing our final descriptor releases flock. Never unlink
                    # the inode, which could split contenders across two locks.
                    for descriptor in (state.fd, state.directory_fd):
                        if descriptor is not None:
                            os.close(descriptor)
                    state.fd = state.directory_fd = None
            state.gate.release()
