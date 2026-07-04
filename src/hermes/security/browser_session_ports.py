"""browser_session_ports — single source of truth for per-session port/display
derivation of the jailed browser (Xvfb display number, CDP port, RFB/VNC port).

FASE C1 (concurrent jailed-browser sessions): each browser session (one systemd
unit per session_name, all sharing the ONE hermes-browser netns) needs its OWN
Xvfb display + CDP port + RFB port so two sessions can run at once without
racing on ":99" / 9333 / 5900. The mapping is a pure function of session_name —
no state, no I/O — so every component that needs to know "which port is THIS
session on" (the launcher when it spawns the unit, JailedBrowserManager/vnc_proxy
when they poll or connect) computes the SAME answer independently.

Mirrors:
  - ops/agents-os-edition/scripts/hermes-browser-jail: `_session_display_num()`
    (bash) derives the SAME display number from $HERMES_BROWSER_SESSION.
  - ops/agents-os-edition/scripts/hermes-browser-launcher: an INLINED copy of
    this exact formula (kept self-contained deliberately — that script is the
    root privilege boundary and does not import the daemon-writable-adjacent
    `hermes` package tree; see the comment there).
Keep all three in lockstep if the formula or the base constants ever change.

Back-compat (HARD CONSTRAINT): with no session parameter anywhere, behavior
must stay byte-identical to today. "exec-browse" (the shared session the agent
uses for browsing) and "teaching-chromium" (the owner's persistent teaching
session — today it already shares exec-browse's ports, since the pre-C1 code
hardcoded them globally regardless of session_name) are PINNED to the legacy
values. Every other session name gets a slot-derived, distinct port triplet.
"""

from __future__ import annotations

from dataclasses import dataclass

# ── Derivation constants ──────────────────────────────────────────────────────

SLOT_COUNT = 40

_DISPLAY_BASE = 100
_CDP_PORT_BASE = 9400
_RFB_PORT_BASE = 5950

# Legacy pins — MUST stay these exact values (back-compat).
_LEGACY_DISPLAY = 99
_LEGACY_CDP_PORT = 9333
_LEGACY_RFB_PORT = 5900
_LEGACY_CLIP_PORT = 7519

PINNED_SESSIONS = frozenset({"exec-browse", "teaching-chromium"})


@dataclass(frozen=True, slots=True)
class SessionPorts:
    """The derived Xvfb display number + CDP port + RFB (VNC) port for a session."""

    display: int
    cdp_port: int
    rfb_port: int


def is_pinned_session(session_name: str) -> bool:
    """True for the two back-compat sessions that keep today's hardcoded ports."""
    return session_name in PINNED_SESSIONS


def session_slot(session_name: str) -> int:
    """Deterministic slot in [0, SLOT_COUNT) — sum of the name's UTF-8 bytes % 40.

    Session names are validated ASCII (the launcher's server-side regex is
    ``^(exec|teaching)-[a-z0-9]{1,64}$``), so a byte-sum is unambiguous and
    trivially mirrored in bash (each char is one byte, no multi-byte encoding
    to reason about).

    Collision note: two distinct session names CAN land on the same slot
    (birthday-bound, ~1-in-40 for any given pair). This is an accepted,
    documented trade-off for FASE C1 (infra-only): the launcher's concurrent
    exec-* session cap (default 3, HERMES_MAX_EXEC_SESSIONS) keeps the
    realistic collision probability low, and a colliding second session simply
    reuses the same port triplet as whichever session already holds that slot —
    it is not engineered around further in this pass.
    """
    return sum(session_name.encode("utf-8")) % SLOT_COUNT


def session_ports(session_name: str) -> SessionPorts:
    """Return the (display, cdp_port, rfb_port) triplet for *session_name*."""
    if is_pinned_session(session_name):
        return SessionPorts(_LEGACY_DISPLAY, _LEGACY_CDP_PORT, _LEGACY_RFB_PORT)
    slot = session_slot(session_name)
    return SessionPorts(
        display=_DISPLAY_BASE + slot,
        cdp_port=_CDP_PORT_BASE + slot,
        rfb_port=_RFB_PORT_BASE + slot,
    )


def clipboard_port(session_name: str) -> int | None:
    """Clipboard bridge port, or None if this session gets no clipboard bridge.

    The web UI's clipboard_bridge.py hits a FIXED path (10.200.0.2:7519, no
    session parameter — out of scope here, frontend excluded from this pass).
    Giving a non-pinned session its own clipboard port would be unreachable
    dead code, so only the pinned (legacy) sessions start a clipboard bridge.
    """
    return _LEGACY_CLIP_PORT if is_pinned_session(session_name) else None
