"""Actual cancellation helper plus source wiring; no fake daemon/READY proof."""

import ast
import asyncio
from pathlib import Path

import pytest

from hermes.runtime import __main__ as daemon

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_repeated_shutdown_does_not_interrupt_startup_client_cleanup():
    entered, releasing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    cancellations = []

    async def connecting():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellations.append("cancel")
            releasing.set()
            await release.wait()  # real client/session close may yield here
            raise

    task = asyncio.create_task(connecting(), name="mcp-reconnect")
    await entered.wait()
    try:
        await daemon._cancel_runtime_tasks_after_grace([task], 0)
        await releasing.wait()
        await daemon._cancel_runtime_tasks_after_grace([task], 0)
        assert task.cancelling() == 1
        assert not task.done()
        release.set()
        result = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(result[0], asyncio.CancelledError)
        assert cancellations == ["cancel"]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_completed_startup_tasks_are_not_cancelled_again():
    task = asyncio.create_task(asyncio.sleep(0))
    await task
    await daemon._cancel_runtime_tasks_after_grace([task], 0)
    assert task.cancelling() == 0 and task.result() is None


def test_eager_and_reconnect_share_existing_gather_without_delaying_ready():
    # Composition contract: both existing fire-and-forget calls are now owned
    # by the same gather as the main loop. No extra wait before READY/watchdog.
    tree = ast.parse(Path(daemon.__file__).read_text())
    run = next(
        node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "_run"
    )
    additions = [
        node
        for node in ast.walk(run)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "append"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "startup_tasks"
    ]
    names = [
        next(keyword.value.value for keyword in call.args[0].keywords if keyword.arg == "name")
        for call in additions
    ]
    assert sorted(names) == ["jailed-browser-eager-start", "mcp-reconnect"]
    collection = next(
        node
        for node in run.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "tasks" for target in node.targets)
    )
    assert (
        len(
            [
                item
                for item in collection.value.elts
                if isinstance(item, ast.Starred)
                and isinstance(item.value, ast.Name)
                and item.value.id == "startup_tasks"
            ]
        )
        == 1
    )
    ready = next(
        node
        for node in ast.walk(run)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith("READY=1")
    )
    watchdog = next(
        node
        for node in ast.walk(run)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_start_model_health_monitor"
    )
    assert watchdog.lineno < ready.lineno < collection.lineno
