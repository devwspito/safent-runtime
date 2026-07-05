"""hermes.tui — Safent Terminal.

A terminal-native frontend (Textual) for the Safent agent OS daemon.
Talks to the SAME org.hermes.Runtime1 D-Bus surface the QML compositor uses,
so the full security kernel (broker, netns/egress confinement, signed audit,
consent gate, HITL) is identical — only the presentation is a TUI.

Design contract (SRP / REUSE):
  - Zero new daemon code. The TUI is a pure client of the existing D-Bus verbs
    and signals + the AF_UNIX task-stream socket.
  - Reuses hermes.shell.infrastructure.dbus_fast_runtime_client.TaskStreamClient
    and StreamFrame for chat streaming.
  - Headless-testable: an OfflineRuntimeBridge yields canned data so the app
    boots and renders without a bus (dev + graceful degradation).
"""

from __future__ import annotations

__all__ = ["__version__"]

__version__ = "0.1.0"
