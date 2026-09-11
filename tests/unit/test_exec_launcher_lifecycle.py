"""Launcher lifecycle tests; real guard subprocesses, no host systemd mutations."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[2] / "ops/agents-os-edition/scripts/hermes-exec-launcher"
SPEC = importlib.util.spec_from_loader(
    "exec_launcher", importlib.machinery.SourceFileLoader("exec_launcher", str(PATH))
)
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


@pytest.fixture
def channel():
    if not hasattr(os, "pidfd_open"):
        pytest.skip("Linux pidfd lifecycle; unsupported product hosts fail closed")
    left, right = socket.socketpair()
    pidfd = os.pidfd_open(os.getpid())
    try:
        yield left, right, pidfd
    finally:
        left.close()
        right.close()
        os.close(pidfd)


def guarded(code):
    return subprocess.Popen(
        [sys.executable, "-I", "-S", "-c", launcher._EXEC_GUARD, sys.executable, "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )


def cleanup(proc):
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=2)
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        stream.close()


def watch(proc, channel):
    left, _, pidfd = channel
    return launcher._watch_command(proc, left, pidfd, os.getpid(), os.getuid(), "start", 3)


def test_unit_never_activates_runtime_and_guard_is_immutable():
    cmd = launcher._build_cmd(["/bin/echo", "user-supplied"], None, "/tmp/work", 60)
    assert "--property=Requisite=hermes-runtime.service" in cmd
    assert "--property=After=hermes-runtime.service" in cmd
    assert "--property=PartOf=hermes-runtime.service" in cmd
    assert "--property=KillMode=control-group" in cmd
    assert "--property=TimeoutStopSec=2s" in cmd
    assert not any("BindsTo=" in value or "Requires=" in value for value in cmd)
    assert cmd[cmd.index("--") + 1 :] == [
        "/usr/bin/python3",
        "-I",
        "-S",
        "-c",
        launcher._EXEC_GUARD,
        "/bin/echo",
        "user-supplied",
    ]


def test_guard_no_authorization_never_executes(tmp_path):
    sentinel = tmp_path / "executed"
    proc = guarded(f"open({str(sentinel)!r},'w').write('yes')")
    try:
        assert proc.stdout.readline() == launcher._READY
        proc.stdin.close()
        assert proc.wait(timeout=2) == 125
        assert not sentinel.exists()
    finally:
        cleanup(proc)


def test_guard_preserves_successful_output_and_bounds_bytes(monkeypatch, channel):
    monkeypatch.setattr(launcher, "_current_peer", lambda *_args: True)
    proc = guarded("import os;os.write(1,b'x'*600000);os.write(2,b'y'*600000)")
    try:
        result = watch(proc, channel)
        assert result["ok"] and result["exit_code"] == 0
        assert result["stdout"] == "x" * launcher._MAX_OUTPUT_BYTES
        assert result["stderr"] == "y" * launcher._MAX_OUTPUT_BYTES
    finally:
        cleanup(proc)


def test_restart_after_guard_ready_denies_before_argv(monkeypatch, channel, tmp_path):
    values = iter([True, False])
    monkeypatch.setattr(launcher, "_current_peer", lambda *_args: next(values, False))
    sentinel = tmp_path / "executed"
    proc = guarded(f"open({str(sentinel)!r},'w').write('yes')")
    try:
        with pytest.raises(PermissionError, match="Stale"):
            watch(proc, channel)
        assert not sentinel.exists()
    finally:
        cleanup(proc)


def test_disconnect_after_recheck_before_release_denies(monkeypatch, channel, tmp_path):
    calls = 0

    def current(*_args):
        nonlocal calls
        calls += 1
        if calls == 2:
            channel[1].close()
        return True

    monkeypatch.setattr(launcher, "_current_peer", current)
    sentinel = tmp_path / "executed"
    proc = guarded(f"open({str(sentinel)!r},'w').write('yes')")
    try:
        with pytest.raises(PermissionError):
            watch(proc, channel)
        assert not sentinel.exists()
    finally:
        cleanup(proc)


@pytest.mark.parametrize("mode", ["closed", "half_closed", "extra"])
def test_protocol_abandonment_or_extra_input_denies(monkeypatch, channel, mode):
    monkeypatch.setattr(launcher, "_current_peer", lambda *_args: True)
    if mode == "closed":
        channel[1].close()
    elif mode == "half_closed":
        channel[1].shutdown(socket.SHUT_WR)
    else:
        channel[1].sendall(b"unsolicited")
    assert not launcher._peer_open(channel[0], channel[2])


def test_inflight_disconnect_observed_without_waiting_command(monkeypatch, channel):
    monkeypatch.setattr(launcher, "_current_peer", lambda *_args: True)
    proc = guarded("import time;time.sleep(30)")
    timer = threading.Timer(0.2, channel[1].close)
    timer.start()
    started = time.monotonic()
    try:
        with pytest.raises(PermissionError):
            watch(proc, channel)
        assert time.monotonic() - started < 1
    finally:
        timer.join(timeout=1)
        cleanup(proc)


@pytest.mark.parametrize(
    "state",
    [
        b"",
        b"MainPID=999\nActiveState=active\nSubState=running\n",
        b"MainPID=1\nActiveState=deactivating\nSubState=stop\n",
    ],
)
def test_unknown_or_replaced_systemd_state_fails_closed(monkeypatch, state):
    monkeypatch.setattr(launcher, "_process_identity", lambda _pid: "start")
    monkeypatch.setattr(
        launcher.subprocess, "run", lambda *a, **_kw: subprocess.CompletedProcess(a, 0, state)
    )
    assert not launcher._current_peer(os.getpid(), os.getuid(), "start")


def test_missing_pidfd_cannot_start_unit(monkeypatch):
    monkeypatch.setattr(launcher, "_process_identity", lambda _pid: "start")
    monkeypatch.delattr(launcher.os, "pidfd_open", raising=False)

    def no_spawn(*_args, **_kwargs):
        pytest.fail("No process may start without pidfd authorization")

    monkeypatch.setattr(launcher.subprocess, "Popen", no_spawn)
    assert launcher._run_command(["/bin/true"], None, None, 1, conn=None, pid=1, uid=0) == {
        "ok": False,
        "error": "execution_unavailable",
    }


def test_cleanup_never_accepts_unowned_unit(monkeypatch):
    monkeypatch.setattr(
        launcher.subprocess, "run", lambda *_a, **_kw: pytest.fail("Unexpected mutation")
    )
    for unit in ("hermes-runtime.service", "*", "../escape", "hermes-exec-fake.service"):
        with pytest.raises(ValueError):
            launcher._stop_unit(unit)


def test_pidfd_marks_original_process_dead(channel):
    peer = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
    descriptor = os.pidfd_open(peer.pid)
    try:
        assert launcher._peer_open(channel[0], descriptor)
        peer.kill()
        peer.wait(timeout=2)
        assert not launcher._peer_open(channel[0], descriptor)
    finally:
        if peer.poll() is None:
            peer.kill()
        peer.wait(timeout=2)
        os.close(descriptor)


def test_stop_timeout_force_kills_only_owned_cgroup(monkeypatch):
    calls = []

    def run(args, **_kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(args, 4)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(launcher.subprocess, "run", run)
    unit = "hermes-exec-1234567890abcdef.service"
    launcher._stop_unit(unit)
    assert calls[0][-2:] == ["stop", unit]
    assert calls[1][-4:] == ["kill", "--kill-whom=all", "--signal=KILL", unit]
