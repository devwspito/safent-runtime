"""Unit tests — Inc 5' (2026-07-07): Community does NOT seed the 27-template roster.

Coverage:
  - `seed_default_roster=False` (Community): only `default` is seeded, zero
    `roster-*` rows, `default_roster_enabled` flag defensively set to off.
  - `seed_default_roster=True` (default / Associate): today's behaviour,
    unchanged — the roster is seeded exactly once.
  - `_ensure_default()` seeds `default` regardless of `seed_default_roster`
    (Community keeps exactly one agent).
  - Reversibility: re-opening the SAME db with `seed_default_roster=True`
    (simulating pairing → edition flips to associate) seeds the roster.
  - `CannotDeleteLastAgent` / `CannotDeleteDefaultAgent` guards are untouched
    for a Community (roster-less) registry.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes.agents.domain.agent import DEFAULT_AGENT_ID
from hermes.agents.domain.default_roster import default_roster
from hermes.agents.domain.ports import CannotDeleteDefaultAgent, CannotDeleteLastAgent
from hermes.agents.infrastructure.sqlite_agent_registry import SqliteAgentRegistry

pytestmark = pytest.mark.unit


def _roster_ids(reg: SqliteAgentRegistry) -> list[str]:
    return [a.agent_id for a in reg.list_agents() if a.agent_id.startswith("roster-")]


class TestCommunityDoesNotSeedRoster:
    def test_fresh_community_seeds_only_default(self, tmp_path: Path) -> None:
        reg = SqliteAgentRegistry(db_path=tmp_path / "s.db", seed_default_roster=False)
        agents = reg.list_agents()
        assert len(agents) == 1
        assert agents[0].agent_id == DEFAULT_AGENT_ID
        assert agents[0].is_default is True

    def test_fresh_community_has_zero_roster_rows(self, tmp_path: Path) -> None:
        reg = SqliteAgentRegistry(db_path=tmp_path / "s.db", seed_default_roster=False)
        assert _roster_ids(reg) == []

    def test_default_roster_enabled_defensively_off(self, tmp_path: Path) -> None:
        reg = SqliteAgentRegistry(db_path=tmp_path / "s.db", seed_default_roster=False)
        assert reg.default_roster_enabled() is False

    def test_native_delegation_guards_intact_with_one_agent(
        self, tmp_path: Path
    ) -> None:
        """D3' regression: a roster-less registry still enforces the
        CannotDeleteLastAgent / CannotDeleteDefaultAgent invariants — Community
        simplification never weakens the single-agent guard."""
        reg = SqliteAgentRegistry(db_path=tmp_path / "s.db", seed_default_roster=False)
        with pytest.raises((CannotDeleteDefaultAgent, CannotDeleteLastAgent)):
            reg.delete_agent(DEFAULT_AGENT_ID)


class TestAssociateStillSeedsRoster:
    def test_default_ctor_seeds_roster(self, tmp_path: Path) -> None:
        """seed_default_roster defaults to True — zero behaviour change for
        every existing call site that doesn't pass the new kwarg."""
        reg = SqliteAgentRegistry(db_path=tmp_path / "s.db")
        assert len(_roster_ids(reg)) == len(default_roster())

    def test_explicit_true_seeds_roster(self, tmp_path: Path) -> None:
        reg = SqliteAgentRegistry(
            db_path=tmp_path / "s.db", seed_default_roster=True
        )
        assert len(_roster_ids(reg)) == len(default_roster())

    def test_roster_seeded_exactly_once_across_restarts(self, tmp_path: Path) -> None:
        db = tmp_path / "s.db"
        SqliteAgentRegistry(db_path=db, seed_default_roster=True)
        reg2 = SqliteAgentRegistry(db_path=db, seed_default_roster=True)
        assert len(_roster_ids(reg2)) == len(default_roster())


class TestReversibility:
    def test_pairing_after_community_boot_seeds_roster_rows(
        self, tmp_path: Path
    ) -> None:
        """Owner-decided reversibility (RC-3): a Community db paired later
        (edition -> associate) is re-opened with seed_default_roster=True and
        the roster rows are created — no data loss, no manual migration.

        Checked at the ROW level (raw SQL), independent of the SEPARATE
        `default_roster_enabled` visibility toggle: Community's defense-in-
        depth (set_default_roster_enabled(False) at first boot) is a STICKY
        hide, by design (RC-3) — it does not auto-flip back on. Seeding
        (existence) and visibility (list_agents filtering) are orthogonal;
        this test covers seeding only.
        """
        import sqlite3  # noqa: PLC0415

        db = tmp_path / "s.db"
        SqliteAgentRegistry(db_path=db, seed_default_roster=False)
        SqliteAgentRegistry(db_path=db, seed_default_roster=True)

        conn = sqlite3.connect(db)
        try:
            (count,) = conn.execute(
                "SELECT COUNT(*) FROM agents WHERE agent_id LIKE 'roster-%'"
            ).fetchone()
        finally:
            conn.close()
        assert count == len(default_roster())

    def test_default_agent_survives_across_the_reversal(self, tmp_path: Path) -> None:
        db = tmp_path / "s.db"
        SqliteAgentRegistry(db_path=db, seed_default_roster=False)
        reg2 = SqliteAgentRegistry(db_path=db, seed_default_roster=True)
        assert any(a.agent_id == DEFAULT_AGENT_ID for a in reg2.list_agents())
