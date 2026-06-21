"""Lumen — Qt6/QML desktop shell for the agentic OS.

Presentation layer only. Renders as a single Wayland client under mutter and
talks to the existing agent backend (shell-server HTTP API at 127.0.0.1:7517).
The OS, the agent daemon (run_cycle), the broker/gate and the security layers
are unchanged — Lumen is the face, not the body.
"""
