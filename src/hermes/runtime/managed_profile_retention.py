"""Conservative retention of abandoned, metadata-only corporate homes.

Cooperating creators/cleaners use the configuration lock. A process running as
the state-directory owner is not an adversary this lock can isolate. Never
recurse, follow links, infer death from permission errors, or delete legacy
homes whose owning process was not recorded in their allocation name.
"""

from __future__ import annotations

import os
import re
import stat
import time
from pathlib import Path

from hermes.security.configuration_lock import configuration_lock

PROFILE_NAME = re.compile(r"g[1-9][0-9]*-p([1-9][0-9]*)-[0-9a-f]{16}\Z")
MIN_AGE_SECONDS = 7 * 24 * 60 * 60
MAX_INSPECTED = 128
MAX_REMOVED = 8
_DIRECTORY_MODE = 0o700
_FILE_MODE = 0o600


def _private_directory(info: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.geteuid()
        and stat.S_IMODE(info.st_mode) == _DIRECTORY_MODE
    )


def _dead(pid: int) -> bool:
    if pid == os.getpid():
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except (OSError, OverflowError):
        return False
    return False


def _same(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _prune_one(root_fd: int, name: str, now: float) -> bool:  # noqa: PLR0911 - explicit fail-closed checks
    match = PROFILE_NAME.fullmatch(name)
    if not match or not _dead(int(match[1])):
        return False
    original = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
    if not _private_directory(original) or now - original.st_mtime < MIN_AGE_SECONDS:
        return False
    home_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
    try:
        opened = os.fstat(home_fd)
        if not _same(original, opened) or not _private_directory(opened):
            return False
        # Unknown runtime state is deliberately retained. No recursive deletion
        # of browser profiles, sockets, logs, auxiliary caches or plugin data.
        with os.scandir(home_fd) as entries:
            first = next(entries, None)
            if next(entries, None) is not None:
                return False
        config = None
        if first is not None:
            if first.name != "config.yaml":
                return False
            config = os.stat("config.yaml", dir_fd=home_fd, follow_symlinks=False)
            if not (
                stat.S_ISREG(config.st_mode)
                and config.st_uid == os.geteuid()
                and stat.S_IMODE(config.st_mode) == _FILE_MODE
                and config.st_nlink == 1
            ):
                return False
        if not _dead(int(match[1])) or not _same(
            opened, os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        ):
            return False
        if config is not None:
            if not _same(config, os.stat("config.yaml", dir_fd=home_fd, follow_symlinks=False)):
                return False
            os.unlink("config.yaml", dir_fd=home_fd)
        os.rmdir(name, dir_fd=root_fd)
        return True
    finally:
        os.close(home_fd)


def prune_profile_homes(db_path: Path) -> int:
    """Best-effort bounded retention; unsafe roots fail closed before deletion.

    PID reuse conservatively retains a home. An old process is never resumed
    into an old profile: bootstrap receipts require the current PID and clean
    allocation. Only a later boot invokes this maintenance, not a background
    deletion worker. Failure to prune must not grant or revoke inference.
    """
    with configuration_lock(db_path):
        root = db_path.absolute().parent.resolve(strict=True) / "managed-profiles"
        try:
            descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return 0
        try:
            if not _private_directory(os.fstat(descriptor)):
                raise PermissionError("Corporate profile directory is unsafe")
            removed = 0
            now = time.time()
            with os.scandir(descriptor) as entries:
                for index, entry in enumerate(entries):
                    if index >= MAX_INSPECTED or removed >= MAX_REMOVED:
                        break
                    # Explicitly preserve the process environment's current home,
                    # even if its name was supplied by an older implementation.
                    if str(root / entry.name) == os.environ.get("HERMES_HOME"):
                        continue
                    try:
                        removed += _prune_one(descriptor, entry.name, now)
                    except OSError:
                        # Changed, busy or inaccessible entries remain untouched.
                        continue
            return removed
        finally:
            os.close(descriptor)
