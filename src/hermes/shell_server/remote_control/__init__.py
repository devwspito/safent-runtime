"""agents-os-remote-control bounded context: WebRTC SO-level + token binding.

Pieces:
    repo.py     — SQLite persistence for remote_control_sessions.
    api.py      — REST endpoints exposed by shell-server.
    binding.py  — IP + UA + tenant + operator binding hash (FR-055).
    service.py  — standalone signaling/WebRTC process at :7518 (binary).
"""
