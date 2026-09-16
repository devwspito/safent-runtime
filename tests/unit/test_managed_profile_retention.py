"""Real private directories, liveness checks and cooperating-process races."""

import multiprocessing
import os
import stat
import subprocess
import sys
import time

import pytest

from hermes.runtime import managed_profile_retention as retention
from hermes.runtime.managed_llm_profile import create_profile_home, write_profile
from hermes.security.configuration_lock import configuration_lock

pytestmark = pytest.mark.unit


def abandoned(tmp_path, *, suffix="0123456789abcdef", config=True):
    # A real exited child, never an arbitrary PID assumed to be unused.
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=10)
    root = tmp_path / "managed-profiles"
    root.mkdir(mode=0o700, exist_ok=True)
    home = root / f"g1-p{child.pid}-{suffix}"
    home.mkdir(mode=0o700)
    if config:
        write_profile(home, {"model": {"provider": "custom"}})
    old = time.time() - retention.MIN_AGE_SECONDS - 60
    os.utime(home, (old, old))
    return home


def test_allocation_records_current_pid_and_prunes_only_old_dead_homes(tmp_path):
    old = abandoned(tmp_path)
    active = create_profile_home(tmp_path / "state.db", 2)
    assert not old.exists()
    assert active.name.startswith(f"g2-p{os.getpid()}-")
    assert stat.S_IMODE(active.stat().st_mode) == 0o700
    assert list(active.iterdir()) == []  # sealed bootstrap still sees a clean home
    stale = time.time() - retention.MIN_AGE_SECONDS - 60
    os.utime(active, (stale, stale))
    assert retention.prune_profile_homes(tmp_path / "state.db") == 0
    assert active.exists()


def test_other_live_process_profile_survives_retention(tmp_path):
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"], stdin=subprocess.PIPE
    )
    try:
        root = tmp_path / "managed-profiles"
        root.mkdir(mode=0o700)
        home = root / f"g1-p{child.pid}-0123456789abcdef"
        home.mkdir(mode=0o700)
        write_profile(home, {"model": {}})
        old = time.time() - retention.MIN_AGE_SECONDS - 60
        os.utime(home, (old, old))
        assert retention.prune_profile_homes(tmp_path / "state.db") == 0
        assert home.exists()
    finally:
        child.stdin.close()
        child.wait(timeout=10)


@pytest.mark.parametrize(
    "case",
    [
        "recent",
        "legacy",
        "unknown",
        "mode",
        "file_mode",
        "link",
        "hardlink",
        "environment",
        "pid_unknown",
        "owner",
    ],
)
def test_retains_any_uncertain_profile_without_deleting_content(tmp_path, monkeypatch, case):
    home = abandoned(tmp_path)
    if case == "recent":
        os.utime(home, None)
    elif case == "legacy":
        renamed = home.with_name("g1-legacy")
        home.rename(renamed)
        home = renamed
    elif case == "unknown":
        (home / "browser-cache").mkdir()
    elif case == "mode":
        home.chmod(0o755)
    elif case == "file_mode":
        (home / "config.yaml").chmod(0o644)
    elif case in {"link", "hardlink"}:
        outside = tmp_path / "retained.yaml"
        (home / "config.yaml").rename(outside)
        if case == "link":
            (home / "config.yaml").symlink_to(outside)
        else:
            os.link(outside, home / "config.yaml")
    elif case == "environment":
        monkeypatch.setenv("HERMES_HOME", str(home))
    elif case == "pid_unknown":
        monkeypatch.setattr(
            retention.os, "kill", lambda *_: (_ for _ in ()).throw(PermissionError())
        )
    elif case == "owner":
        original = retention._private_directory
        # No privileged chown required: exercise rejection of a different UID
        # through the actual stat predicate before any unlink.
        info = home.stat()
        foreign = os.stat_result(tuple(info)[:4] + (info.st_uid + 1,) + tuple(info)[5:])
        assert not original(foreign)
        monkeypatch.setattr(
            retention,
            "_private_directory",
            lambda value: original(value) and value.st_ino != info.st_ino,
        )
    if case != "recent":
        old = time.time() - retention.MIN_AGE_SECONDS - 60
        os.utime(home, (old, old))
    assert retention.prune_profile_homes(tmp_path / "state.db") == 0
    assert home.exists() and (home / "config.yaml").exists()


def test_root_symlink_and_world_readable_root_never_pruned(tmp_path):
    home = abandoned(tmp_path)
    root = home.parent
    root.chmod(0o755)
    with pytest.raises(PermissionError):
        retention.prune_profile_homes(tmp_path / "state.db")
    root.chmod(0o700)
    moved = tmp_path / "elsewhere"
    root.rename(moved)
    root.symlink_to(moved, target_is_directory=True)
    with pytest.raises(OSError):
        retention.prune_profile_homes(tmp_path / "state.db")
    assert (moved / home.name / "config.yaml").exists()


def test_directory_swap_during_open_never_follows_link_or_deletes_target(tmp_path, monkeypatch):
    home = abandoned(tmp_path)
    outside = tmp_path / "personal"
    outside.mkdir(mode=0o700)
    (outside / "keep").write_text("fixture")
    original = os.open

    def swap(path, flags, *args, **kwargs):
        if path == home.name:
            home.rename(home.with_name("retained-original"))
            home.symlink_to(outside, target_is_directory=True)
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(retention.os, "open", swap)
    assert retention.prune_profile_homes(tmp_path / "state.db") == 0
    assert (outside / "keep").read_text() == "fixture"
    assert (home.parent / "retained-original" / "config.yaml").exists()


def test_retention_limits_deletions_and_never_recurses(tmp_path, monkeypatch):
    for index in range(4):
        abandoned(tmp_path, suffix=f"{index:016x}")
    monkeypatch.setattr(retention, "MAX_REMOVED", 2)
    assert retention.prune_profile_homes(tmp_path / "state.db") == 2
    assert len(list((tmp_path / "managed-profiles").iterdir())) == 2
    monkeypatch.setattr(retention, "MAX_INSPECTED", 1)
    assert retention.prune_profile_homes(tmp_path / "state.db") == 1


def _prune_process(path, started, finished):
    started.set()
    retention.prune_profile_homes(path)
    finished.set()


def test_cleanup_waits_for_creator_configuration_lock_in_other_process(tmp_path):
    context = multiprocessing.get_context("spawn")
    started, finished = context.Event(), context.Event()
    path = tmp_path / "state.db"
    home = abandoned(tmp_path)
    process = context.Process(target=_prune_process, args=(path, started, finished))
    with configuration_lock(path):
        process.start()
        assert started.wait(10)
        assert not finished.wait(0.15)
        assert home.exists()
    process.join(timeout=10)
    assert process.exitcode == 0 and finished.is_set()
    assert not home.exists()
