"""Unit tests — _mcp_connect slug-to-TrustLevel mapping.

Pins the routing table added for the MANAGED_REMOTE tier (013-P1 follow-up):
  - "safent-control" (first-party, egresses to the cloud control-plane) →
    TrustLevel.MANAGED_REMOTE. It must NOT be added to _BUILTIN_MCP_SLUGS —
    BUILTIN's frictionless posture assumes local/no-egress confinement, which
    does not hold for a server that talks to a remote control-plane.
  - "excel"/"word"/"powerpoint" (factory-baked, local, no egress) stay BUILTIN
    — regression: adding MANAGED_REMOTE must not touch this branch.
  - Any other slug (user-added) stays USER_ADDED — regression.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from hermes.agents_os.infrastructure.dbus_runtime_service import _mcp_connect
from hermes.mcp.domain.value_objects import TrustLevel

pytestmark = pytest.mark.unit


async def _connect_and_capture_trust_level(server_id: str) -> TrustLevel:
    manager = AsyncMock()
    await _mcp_connect(manager, server_id, ["npx", "-y", "@some/mcp"])
    _, kwargs = manager.connect.call_args
    return kwargs["trust_level"]


class TestMcpConnectTrustLevelMapping:
    @pytest.mark.asyncio
    async def test_safent_control_is_managed_remote(self) -> None:
        trust = await _connect_and_capture_trust_level("safent-control")
        assert trust is TrustLevel.MANAGED_REMOTE

    @pytest.mark.asyncio
    async def test_excel_stays_builtin(self) -> None:
        trust = await _connect_and_capture_trust_level("excel")
        assert trust is TrustLevel.BUILTIN

    @pytest.mark.asyncio
    async def test_word_stays_builtin(self) -> None:
        trust = await _connect_and_capture_trust_level("word")
        assert trust is TrustLevel.BUILTIN

    @pytest.mark.asyncio
    async def test_powerpoint_stays_builtin(self) -> None:
        trust = await _connect_and_capture_trust_level("powerpoint")
        assert trust is TrustLevel.BUILTIN

    @pytest.mark.asyncio
    async def test_random_slug_is_user_added(self) -> None:
        trust = await _connect_and_capture_trust_level("some-random-community-mcp")
        assert trust is TrustLevel.USER_ADDED
