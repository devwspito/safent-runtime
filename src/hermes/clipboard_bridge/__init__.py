"""Hermes Clipboard Bridge — in-session HTTP bridge between noVNC and the
Wayland clipboard.

Runs as a systemd user service (hermes-clipboard-bridge.service) inside the
hermes-user graphical session so that WAYLAND_DISPLAY / XDG_RUNTIME_DIR are
inherited from the session environment.  It exposes a tiny localhost HTTP API on
127.0.0.1:7519 that the noVNC overlay (clipboard-overlay.js) calls from the
browser.

Port 7519 was chosen as the next free port after:
  7517 — hermes-shell-server
  7518 — hermes-remote-control signalling WS

Public surface
--------------
POST /clipboard   {"text": "..."} → writes to the session clipboard via wl-copy
GET  /clipboard   → reads the session clipboard via wl-paste --no-newline

Size cap: 256 KiB.  Content is never logged (may contain API keys / secrets).
"""
