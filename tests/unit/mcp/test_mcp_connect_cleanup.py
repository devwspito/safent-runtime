"""Failed admission never leaves an unregistered MCP client owned by nobody."""

import asyncio
import sys
from unittest.mock import AsyncMock, Mock

import pytest

from hermes.mcp.application.errors import McpConnectionError
from hermes.mcp.application.mcp_server_manager import McpServerManager
from hermes.mcp.domain.value_objects import McpServerId, ServerSlug, Transport, TrustLevel

pytestmark = pytest.mark.unit


def connection(client):
    manager = McpServerManager(client_factory=lambda _: client)
    manager._on_connect = Mock()
    options = dict(
        server_id=McpServerId.generate(),
        slug=ServerSlug("fixture"),
        transport=Transport.stdio(["fixture"]),
        trust_level=TrustLevel.USER_ADDED,
    )
    return manager, options


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase,cancelled",
    [
        ("initialize", False),
        ("list_tools", False),
        ("decode", False),
        ("initialize", True),
        ("list_tools", True),
    ],
)
async def test_failure_or_cancellation_closes_exactly_once_before_registration(
    phase, cancelled, caplog
):
    failure = (
        asyncio.CancelledError("fixture-secret") if cancelled else RuntimeError("fixture-secret")
    )
    client = Mock(initialize=AsyncMock(), list_tools=AsyncMock(return_value=[]), close=AsyncMock())
    if phase == "decode":
        client.list_tools.return_value = [None]
    else:
        getattr(client, phase).side_effect = failure
    manager, options = connection(client)
    with pytest.raises(asyncio.CancelledError if cancelled else McpConnectionError) as caught:
        await manager.connect(**options)
    if cancelled:
        assert caught.value is failure
    else:
        assert "fixture-secret" not in str(caught.value)
    assert "fixture-secret" not in caplog.text
    client.close.assert_awaited_once()
    manager._on_connect.assert_not_called()
    assert manager.snapshot() == {}
    await manager.disconnect(options["server_id"])
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_cleanup_failure_does_not_replace_original_cancellation(caplog):
    cancellation = asyncio.CancelledError()
    client = Mock(
        initialize=AsyncMock(side_effect=cancellation),
        list_tools=AsyncMock(),
        close=AsyncMock(side_effect=RuntimeError("fixture-secret-cleanup")),
    )
    manager, options = connection(client)
    with pytest.raises(asyncio.CancelledError) as caught:
        await manager.connect(**options)
    assert caught.value is cancellation
    assert "fixture-secret" not in caplog.text
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_handshake_reaps_real_child_and_stdio():
    class ChildClient:
        def __init__(self):
            self.started = asyncio.Event()
            self.closed = 0
            self.child = None

        async def initialize(self):
            self.child = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                "import sys; sys.stdin.buffer.read()",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
            )
            self.started.set()
            await asyncio.Event().wait()

        async def list_tools(self):
            pytest.fail("cancelled handshake must not enumerate tools")

        async def close(self):
            self.closed += 1
            self.child.stdin.close()
            await self.child.stdin.wait_closed()
            await asyncio.wait_for(self.child.wait(), 3)

    client = ChildClient()
    manager, options = connection(client)
    task = asyncio.create_task(manager.connect(**options))
    try:
        await asyncio.wait_for(client.started.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client.closed == 1
        assert client.child.returncode == 0
        assert client.child.stdin.is_closing()
        assert manager.snapshot() == {}
    finally:
        if client.child is not None and client.child.returncode is None:
            client.child.kill()
            await client.child.wait()
