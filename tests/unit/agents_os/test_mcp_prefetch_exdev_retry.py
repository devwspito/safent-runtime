"""Regression tests — MCP prefetch runs detached and retries the residual EXDEV.

Root cause (spec 025 matriz item #5, 2026-09-10): `uv tool install` for a
NEVER-cached package, spawned directly off the daemon's own long-lived
asyncio process, intermittently died with "Invalid cross-device link (os
error 18)" even though cache/tool/tmp dirs all resolve to the SAME st_dev
(verified live in an isolated container — not a real mount-boundary
crossing). Spawning the identical command as its own session/process group
(`systemd-run --pipe`, or a plain fork with `start_new_session=True`)
instead of inheriting the daemon's own session never reproduced it across
dozens of live trials — `systemd-run` itself is NOT reachable from the
unprivileged daemon (User=hermes; the host's D-Bus policy denies
`StartTransientUnit` for it — "Access denied", verified live; loosening
that policy is a security-posture change out of scope here), but
`start_new_session=True` needs no new privilege and cleared the large
majority of trials outright. `_run_prefetch_subprocess`
(dbus_runtime_service.py) sets that flag on every prefetch spawn, with a
bounded retry of the identical command as the backstop for the remainder —
scoped to the EXDEV signature only, so a genuine error (bad coordinate,
registry down) returns immediately, never masked or delayed. The signature
match is on "os error 18" specifically: uv word-wraps its pretty-printed
error when stderr is a pipe, so a naive "Invalid cross-device link"
substring can be split across a newline and never match — a real bug
caught live while deploying this fix.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from hermes.agents_os.infrastructure.dbus_runtime_service import (
    _PREFETCH_EXDEV_RETRIES,
    _run_prefetch_subprocess,
)


def _completed(rc: int, stderr: str = "", stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["uv"], rc, stdout=stdout, stderr=stderr)


_EXDEV_STDERR = (
    "failed to rename file from /var/lib/hermes/uv-cache/.tmpXXXX to "
    "/var/lib/hermes/uv-cache/archive-v0/YYYY: Invalid cross-device link (os error 18)"
)
# uv pretty-prints its error word-wrapped when stderr is a pipe (capture_output=True):
# "Invalid" and "cross-device link" can land on DIFFERENT lines. A naive
# "Invalid cross-device link" substring match misses this — regression pin.
_EXDEV_STDERR_WRAPPED = (
    "  ╰─▶ failed to rename file from /var/lib/hermes/uv-cache/.tmpq5x7P0\n"
    "      to /var/lib/hermes/uv-cache/archive-v0/d-oI7MFyy_0rYB-z: Invalid\n"
    "      cross-device link (os error 18)\n"
)


class TestRunPrefetchSubprocessRetriesExdev:
    def test_succeeds_immediately_without_retry(self) -> None:
        calls: list[list[str]] = []

        def _fake_run(cmd, **kwargs):  # noqa: ANN001, ANN202
            calls.append(cmd)
            return _completed(0)

        with patch("subprocess.run", side_effect=_fake_run):
            result = _run_prefetch_subprocess(["uv", "tool", "install", "pkg"], {}, 30.0)

        assert result.returncode == 0
        assert len(calls) == 1

    def test_retries_only_on_exdev_signature_and_eventually_succeeds(self) -> None:
        calls: list[list[str]] = []

        def _fake_run(cmd, **kwargs):  # noqa: ANN001, ANN202
            calls.append(cmd)
            if len(calls) < 3:
                return _completed(1, stderr=_EXDEV_STDERR)
            return _completed(0)

        with patch("subprocess.run", side_effect=_fake_run), patch("time.sleep"):
            result = _run_prefetch_subprocess(["uv", "tool", "install", "pkg"], {}, 30.0)

        assert result.returncode == 0
        assert len(calls) == 3
        # every retry re-forks the IDENTICAL command — no argv mutation
        assert all(c == ["uv", "tool", "install", "pkg"] for c in calls)

    def test_matches_the_signature_even_when_uv_word_wraps_it(self) -> None:
        """Regression: a naive "Invalid cross-device link" substring match
        misses uv's word-wrapped form (live-reproduced 2026-09-10) and the
        retry never fires. Must match on "os error 18" instead."""
        calls: list[list[str]] = []

        def _fake_run(cmd, **kwargs):  # noqa: ANN001, ANN202
            calls.append(cmd)
            if len(calls) < 2:
                return _completed(1, stderr=_EXDEV_STDERR_WRAPPED)
            return _completed(0)

        with patch("subprocess.run", side_effect=_fake_run), patch("time.sleep"):
            result = _run_prefetch_subprocess(["uv", "tool", "install", "pkg"], {}, 30.0)

        assert result.returncode == 0
        assert len(calls) == 2

    def test_gives_up_after_the_retry_budget_and_returns_last_failure(self) -> None:
        calls: list[list[str]] = []

        def _fake_run(cmd, **kwargs):  # noqa: ANN001, ANN202
            calls.append(cmd)
            return _completed(1, stderr=_EXDEV_STDERR)

        with patch("subprocess.run", side_effect=_fake_run), patch("time.sleep"):
            result = _run_prefetch_subprocess(["uv", "tool", "install", "pkg"], {}, 30.0)

        assert result.returncode == 1
        assert _EXDEV_STDERR in result.stderr
        assert len(calls) == _PREFETCH_EXDEV_RETRIES

    def test_does_not_retry_a_different_failure(self) -> None:
        calls: list[list[str]] = []

        def _fake_run(cmd, **kwargs):  # noqa: ANN001, ANN202
            calls.append(cmd)
            return _completed(1, stderr="× No solution found: package not on PyPI")

        with patch("subprocess.run", side_effect=_fake_run):
            result = _run_prefetch_subprocess(["uv", "tool", "install", "missing"], {}, 30.0)

        assert result.returncode == 1
        assert len(calls) == 1  # not masked/delayed by the EXDEV retry loop

    def test_forwards_env_and_timeout_unchanged(self) -> None:
        captured: dict[str, object] = {}

        def _fake_run(cmd, **kwargs):  # noqa: ANN001, ANN202
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return _completed(0)

        with patch("subprocess.run", side_effect=_fake_run):
            _run_prefetch_subprocess(
                ["uv", "tool", "install", "pkg"], {"UV_CACHE_DIR": "/var/lib/hermes/uv-cache"}, 45.0,
            )

        assert captured["cmd"] == ["uv", "tool", "install", "pkg"]
        assert captured["kwargs"]["env"] == {"UV_CACHE_DIR": "/var/lib/hermes/uv-cache"}
        assert captured["kwargs"]["timeout"] == 45.0

    def test_spawns_detached_from_this_process_session(self) -> None:
        """The actual fix: start_new_session=True (+ explicit close_fds=True).
        No new privilege — this is what cleared the EXDEV in the large
        majority of live trials, verified in an isolated container."""
        captured: dict[str, object] = {}

        def _fake_run(cmd, **kwargs):  # noqa: ANN001, ANN202
            captured["kwargs"] = kwargs
            return _completed(0)

        with patch("subprocess.run", side_effect=_fake_run):
            _run_prefetch_subprocess(["uv", "tool", "install", "pkg"], {}, 30.0)

        assert captured["kwargs"]["start_new_session"] is True
        assert captured["kwargs"]["close_fds"] is True
