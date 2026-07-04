"""Unit tests — hermes.security.browser_session_ports (C1: per-session port
derivation for concurrent jailed-browser sessions).

Covers:
  - exec-browse / teaching-chromium pin to the legacy display/CDP/RFB values
    (back-compat: no session parameter anywhere must stay byte-identical).
  - Two distinct exec-<id> session names map to DISTINCT slots (for the pair
    exercised here) and therefore distinct, non-legacy port triplets.
  - Collision handling is graceful (documented, not an error): a forced
    collision between two different session names still returns a valid,
    identical triplet for both — it never raises.
  - clipboard_port(): legacy sessions get the pinned clipboard port; every
    other session gets None (no clipboard bridge — documented C1 deviation).
  - The bash mirror in hermes-browser-jail (`_session_display_num`) computes
    the IDENTICAL display number as this Python module, for the same set of
    session names — extracted and run against the SHIPPED script (not a
    hand-copied stand-in), the same pattern used by
    test_browser_jail_ephemeral_profile.py.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hermes.security.browser_session_ports import (
    PINNED_SESSIONS,
    SessionPorts,
    clipboard_port,
    is_pinned_session,
    session_ports,
    session_slot,
)

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_JAIL_SCRIPT = (
    _REPO_ROOT / "ops" / "agents-os-edition" / "scripts" / "hermes-browser-jail"
)


class TestPinnedSessions:
    """exec-browse / teaching-chromium keep TODAY's hardcoded ports exactly."""

    def test_exec_browse_is_pinned(self) -> None:
        assert is_pinned_session("exec-browse") is True

    def test_teaching_chromium_is_pinned(self) -> None:
        assert is_pinned_session("teaching-chromium") is True

    def test_other_session_is_not_pinned(self) -> None:
        assert is_pinned_session("exec-abc123") is False

    def test_exec_browse_ports_match_legacy_values(self) -> None:
        ports = session_ports("exec-browse")
        assert ports == SessionPorts(display=99, cdp_port=9333, rfb_port=5900)

    def test_teaching_chromium_ports_match_legacy_values(self) -> None:
        """teaching-chromium shares exec-browse's ports today (pre-C1 global
        constants applied regardless of session name) — pinning it preserves
        that exact (if collision-prone) back-compat behavior."""
        ports = session_ports("teaching-chromium")
        assert ports == SessionPorts(display=99, cdp_port=9333, rfb_port=5900)

    def test_pinned_sessions_set_contains_exactly_two(self) -> None:
        assert PINNED_SESSIONS == frozenset({"exec-browse", "teaching-chromium"})


class TestDerivedSessions:
    """Every other exec-<id> session gets a slot-derived, non-legacy triplet."""

    def test_derived_session_is_not_the_legacy_triplet(self) -> None:
        ports = session_ports("exec-abc123")
        assert ports != SessionPorts(display=99, cdp_port=9333, rfb_port=5900)

    def test_derived_ports_are_in_the_documented_ranges(self) -> None:
        ports = session_ports("exec-abc123")
        assert 100 <= ports.display <= 139
        assert 9400 <= ports.cdp_port <= 9439
        assert 5950 <= ports.rfb_port <= 5989

    def test_two_distinct_names_map_to_distinct_slots(self) -> None:
        """exec-abc123 and exec-test77 (used in the C1 E2E validation) must
        land on DIFFERENT slots so they get DIFFERENT ports — this is the
        exact pair verified concurrently alive in the container."""
        a = session_ports("exec-abc123")
        b = session_ports("exec-test77")
        assert a != b
        assert a.display != b.display
        assert a.cdp_port != b.cdp_port
        assert a.rfb_port != b.rfb_port

    def test_derivation_is_deterministic(self) -> None:
        assert session_ports("exec-abc123") == session_ports("exec-abc123")

    def test_cdp_and_rfb_ports_share_the_same_slot(self) -> None:
        """Both ports are offset by the SAME slot from their respective
        bases — proves they're derived from one formula, not two."""
        ports = session_ports("exec-myid")
        slot = session_slot("exec-myid")
        assert ports.cdp_port == 9400 + slot
        assert ports.rfb_port == 5950 + slot
        assert ports.display == 100 + slot


class TestSlotCollisionIsGraceful:
    """A forced collision (two names landing on the same slot) never raises —
    it is a documented, accepted trade-off, not engineered around further."""

    def test_forced_collision_returns_identical_valid_triplet(self) -> None:
        # session_slot is sum(bytes) % 40; swapping two chars of equal value
        # sum but different order still collides. Construct two DIFFERENT
        # session names guaranteed to collide: same bytes, different order.
        name_a = "exec-ab"
        name_b = "exec-ba"
        assert session_slot(name_a) == session_slot(name_b)
        ports_a = session_ports(name_a)
        ports_b = session_ports(name_b)
        assert ports_a == ports_b  # graceful reuse, not a crash
        assert 100 <= ports_a.display <= 139


class TestClipboardPort:
    """Clipboard bridge is pinned-sessions-only (frontend has no session
    parameter yet — a per-session port would be unreachable dead code)."""

    def test_exec_browse_gets_clipboard(self) -> None:
        assert clipboard_port("exec-browse") == 7519

    def test_teaching_chromium_gets_clipboard(self) -> None:
        assert clipboard_port("teaching-chromium") == 7519

    def test_other_session_gets_no_clipboard(self) -> None:
        assert clipboard_port("exec-abc123") is None


# ---------------------------------------------------------------------------
# Bash mirror parity — extract and RUN the SHIPPED `_session_display_num()`
# from hermes-browser-jail (not a hand-copied stand-in), same pattern as
# test_browser_jail_ephemeral_profile.py.
# ---------------------------------------------------------------------------

_FUNC_START_MARKER = "readonly HERMES_SESSION_SLOT_COUNT=40"
_FUNC_END_MARKER = "readonly HERMES_JAIL_SESSION_NAME="


def _extract_display_num_function() -> str:
    content = _JAIL_SCRIPT.read_text()
    start = content.index(_FUNC_START_MARKER)
    end = content.index(_FUNC_END_MARKER, start)
    return content[start:end]


def _bash_session_display_num(session_name: str) -> int:
    func_src = _extract_display_num_function()
    script = f"{func_src}\n_session_display_num {session_name!r}\n"
    result = subprocess.run(
        ["bash", "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return int(result.stdout.strip())


class TestBashMirrorParity:
    """The shell derivation in hermes-browser-jail must equal the Python one."""

    def test_jail_script_present(self) -> None:
        assert _JAIL_SCRIPT.is_file(), f"expected shipped script at {_JAIL_SCRIPT}"

    @pytest.mark.parametrize(
        "name",
        ["exec-browse", "teaching-chromium", "exec-abc123", "exec-test77", "exec-zz9"],
    )
    def test_bash_matches_python_for(self, name: str) -> None:
        assert _bash_session_display_num(name) == session_ports(name).display
