"""Regression: registry.create_agent must honor a caller-provided agent_id.

The cloud config-sync upsert is idempotent ONLY if the native agent id equals the
cloud agent_template_id. The registry used to always mint uuid4().hex, ignoring the
id in the draft, so every sync re-created the agent with a fresh id → the applier's
"already exists?" check never matched → unbounded duplicates → LicenseExceeded →
the whole policy bundle stopped converging. This pins the fix.
"""

from __future__ import annotations

import pytest

from hermes.agents.application.serialization import draft_from_dict
from hermes.agents.infrastructure.sqlite_agent_registry import SqliteAgentRegistry

pytestmark = pytest.mark.unit


def test_create_agent_uses_provided_id(tmp_path) -> None:
    reg = SqliteAgentRegistry(db_path=tmp_path / "shell-state.db")
    draft = draft_from_dict({"name": "Cloud Agent", "agent_id": "cloud-tmpl-123", "managed_by": "cloud"})
    agent = reg.create_agent(draft)
    assert agent.agent_id == "cloud-tmpl-123"
    assert agent.managed_by == "cloud"


def test_create_agent_mints_id_when_absent(tmp_path) -> None:
    reg = SqliteAgentRegistry(db_path=tmp_path / "shell-state.db")
    agent = reg.create_agent(draft_from_dict({"name": "Local Agent"}))
    assert agent.agent_id  # a uuid was minted
    assert agent.agent_id != "Local Agent"


def test_cloud_resync_does_not_duplicate(tmp_path) -> None:
    """Two creates with the SAME provided id must not yield two distinct agents —
    the second collides on the stable id (the applier routes it to update_agent)."""
    reg = SqliteAgentRegistry(db_path=tmp_path / "shell-state.db")
    draft = draft_from_dict({"name": "Cloud Agent", "agent_id": "stable-1", "managed_by": "cloud"})
    reg.create_agent(draft)
    cloud_ids = [a.agent_id for a in reg.list_agents() if a.managed_by == "cloud"]
    assert cloud_ids == ["stable-1"]
