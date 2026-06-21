from __future__ import annotations

from uuid import uuid4

import pytest

from hermes import ToolCallProposal


def test_requires_tool_name() -> None:
    with pytest.raises(ValueError, match="tool_name is required"):
        ToolCallProposal(
            proposal_id=uuid4(),
            tool_name="",
            tenant_id=uuid4(),
            entity_id="cli-1",
            entity_type="cliente",
            parameters={},
            justification="",
        )


def test_requires_entity_id() -> None:
    with pytest.raises(ValueError, match="entity_id is required"):
        ToolCallProposal(
            proposal_id=uuid4(),
            tool_name="pause_campaign",
            tenant_id=uuid4(),
            entity_id="",
            entity_type="campaign",
            parameters={},
            justification="",
        )


def test_requires_entity_type() -> None:
    with pytest.raises(ValueError, match="entity_type is required"):
        ToolCallProposal(
            proposal_id=uuid4(),
            tool_name="pause_campaign",
            tenant_id=uuid4(),
            entity_id="c-1",
            entity_type="",
            parameters={},
            justification="",
        )
