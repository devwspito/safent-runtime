"""Bounded death of the existing daemon, including non-cooperative SDK threads.

Async task cancellation cannot terminate run_in_executor's running thread.
The timer is armed only when shutdown is requested, not during normal work.
"""

from __future__ import annotations

import os
import threading


class ShutdownDeadline:
    def __init__(self, *, seconds: float = 8.0, exit_process=os._exit):
        self._seconds = seconds
        self._exit = exit_process
        self._pid = os.getpid()
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    def arm(self) -> None:
        with self._lock:
            if self._timer is not None:
                return

            def expire():
                if os.getpid() == self._pid:
                    # 75 = temporary failure. Existing Restart=always replaces
                    # the process only after systemd has reaped its cgroup.
                    self._exit(75)

            self._timer = threading.Timer(self._seconds, expire)
            self._timer.daemon = True
            self._timer.start()
