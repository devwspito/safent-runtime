"""Bind an owner's one-use MCP approval to the exact reviewed operation."""
from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class McpDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    server_id: str = Field(min_length=1, max_length=120)
    label: str | None = None
    argv: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict, repr=False)


class AddMcpApproval(McpDraft):
    operation: Literal["add"] = "add"


class ManagedMcpApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal["managed_remote"] = "managed_remote"
    slug: str = Field(min_length=1, max_length=120)
    url: str = Field(min_length=1, max_length=2048)


McpApproval = Annotated[AddMcpApproval | ManagedMcpApproval, Field(discriminator="operation")]


def mcp_approval_identifier(intent: AddMcpApproval | ManagedMcpApproval) -> str:
    data = intent.model_dump()
    if isinstance(intent, AddMcpApproval):
        data["label"] = intent.label or intent.server_id
    # Include env values, but never expose them in grants, logs or identifiers.
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
