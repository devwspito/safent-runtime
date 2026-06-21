"""MCP stdio server entry point for the LumenSO cua_driver.

Usage:
    python3 -m hermes.lumen.cua_driver mcp

hermes-agent launches this process via HERMES_CUA_DRIVER_CMD and speaks
MCP JSON-RPC 2.0 over stdin/stdout.

Architecture: thin presentation layer — validates MCP args, delegates to
CuaActionExecutor (application layer), formats MCP CallToolResult.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger("hermes.lumen.cua_driver")


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )


def _build_bridge() -> "SessionBridgeClient":
    """Construct SessionBridgeClient, honouring env-var overrides."""
    import os  # noqa: PLC0415
    from hermes.capabilities.infrastructure.session_bridge_client import (  # noqa: PLC0415
        SessionBridgeClient,
    )

    socket_path = os.environ.get("SESSION_INPUT_SOCK")
    token_path = os.environ.get("SESSION_INPUT_TOKEN")
    kwargs: dict[str, Any] = {}
    if socket_path:
        kwargs["socket_path"] = Path(socket_path)
    if token_path:
        kwargs["token_path"] = Path(token_path)
    return SessionBridgeClient(**kwargs)


# Tool schema constants — intentionally minimal; hermes-agent validates at call time.
_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "list_windows",
        "description": "List visible windows. v1: returns single fullscreen entry (v2/AT-SPI planned).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "on_screen_only": {"type": "boolean", "default": True}
            },
        },
    },
    {
        "name": "screenshot",
        "description": "Capture the compositor framebuffer and return a JPEG/PNG image.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "window_id": {"type": "integer"},
                "format": {"type": "string", "enum": ["jpeg", "png"], "default": "jpeg"},
                "quality": {"type": "integer", "default": 85},
            },
        },
    },
    {
        "name": "get_window_state",
        "description": "Get accessibility state of a window. v1: text summary only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pid": {"type": "integer"},
                "window_id": {"type": "integer"},
            },
        },
    },
    {
        "name": "click",
        "description": "Single left-click at (x, y).",
        "inputSchema": {
            "type": "object",
            "required": ["pid", "x", "y"],
            "properties": {
                "pid": {"type": "integer"},
                "x": {"type": "number"},
                "y": {"type": "number"},
                "modifier": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
    {
        "name": "double_click",
        "description": "Double left-click at (x, y).",
        "inputSchema": {
            "type": "object",
            "required": ["pid", "x", "y"],
            "properties": {
                "pid": {"type": "integer"},
                "x": {"type": "number"},
                "y": {"type": "number"},
            },
        },
    },
    {
        "name": "right_click",
        "description": "Right-click at (x, y).",
        "inputSchema": {
            "type": "object",
            "required": ["pid", "x", "y"],
            "properties": {
                "pid": {"type": "integer"},
                "x": {"type": "number"},
                "y": {"type": "number"},
            },
        },
    },
    {
        "name": "drag",
        "description": "Drag pointer from (from_x, from_y) to (to_x, to_y).",
        "inputSchema": {
            "type": "object",
            "required": ["pid", "from_x", "from_y", "to_x", "to_y"],
            "properties": {
                "pid": {"type": "integer"},
                "from_x": {"type": "number"},
                "from_y": {"type": "number"},
                "to_x": {"type": "number"},
                "to_y": {"type": "number"},
            },
        },
    },
    {
        "name": "scroll",
        "description": "Scroll in direction up|down|left|right by amount steps.",
        "inputSchema": {
            "type": "object",
            "required": ["pid", "direction", "amount"],
            "properties": {
                "pid": {"type": "integer"},
                "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
                "amount": {"type": "integer", "minimum": 1, "maximum": 50},
                "x": {"type": "number"},
                "y": {"type": "number"},
            },
        },
    },
    {
        "name": "type_text",
        "description": "Type text via keyboard event synthesis.",
        "inputSchema": {
            "type": "object",
            "required": ["pid", "text"],
            "properties": {
                "pid": {"type": "integer"},
                "text": {"type": "string"},
            },
        },
    },
    {
        "name": "press_key",
        "description": "Press and release a named key (e.g. 'return', 'escape', 'tab').",
        "inputSchema": {
            "type": "object",
            "required": ["pid", "key"],
            "properties": {
                "pid": {"type": "integer"},
                "key": {"type": "string"},
            },
        },
    },
    {
        "name": "hotkey",
        "description": "Press a key chord (modifiers + key). E.g. ['ctrl', 'c'].",
        "inputSchema": {
            "type": "object",
            "required": ["pid", "keys"],
            "properties": {
                "pid": {"type": "integer"},
                "keys": {"type": "array", "items": {"type": "string"}},
            },
        },
    },
    {
        "name": "set_value",
        "description": "Set element value (v1: not implemented — use click + type_text).",
        "inputSchema": {
            "type": "object",
            "required": ["pid", "window_id", "element_index", "value"],
            "properties": {
                "pid": {"type": "integer"},
                "window_id": {"type": "integer"},
                "element_index": {"type": "integer"},
                "value": {"type": "string"},
            },
        },
    },
    {
        "name": "list_apps",
        "description": "List running applications (name + pid).",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


async def _run_mcp_server() -> None:
    """Run the MCP stdio server loop."""
    try:
        from mcp.server import Server  # noqa: PLC0415
        from mcp.server.stdio import stdio_server  # noqa: PLC0415
        import mcp.types as mcp_types  # noqa: PLC0415
    except ImportError as exc:
        logger.error("cua_driver.mcp_import_failed: %s — install mcp>=1.0", exc)
        sys.exit(1)

    from hermes.lumen.cua_driver._action_executor import (  # noqa: PLC0415
        CuaActionExecutor,
        NoActiveWindowError,
    )
    from hermes.capabilities.infrastructure.session_bridge_client import (  # noqa: PLC0415
        SessionBridgeError,
        SessionBridgeUnavailable,
    )

    bridge = _build_bridge()
    executor = CuaActionExecutor(bridge)
    server = Server("hermes-lumen-cua-driver")

    @server.list_tools()  # type: ignore[misc]
    async def _list_tools() -> list[mcp_types.Tool]:
        return [
            mcp_types.Tool(
                name=s["name"],
                description=s["description"],
                inputSchema=s["inputSchema"],
            )
            for s in _TOOL_SCHEMAS
        ]

    @server.call_tool()  # type: ignore[misc]
    async def _call_tool(
        name: str, arguments: dict[str, Any]
    ) -> list[mcp_types.TextContent | mcp_types.ImageContent | mcp_types.EmbeddedResource]:
        try:
            result = await _dispatch(executor, name, arguments)
            return _format_result(result, mcp_types)
        except (NoActiveWindowError, ValueError) as exc:
            return [mcp_types.TextContent(type="text", text=f"error: {exc}")]
        except (SessionBridgeUnavailable, SessionBridgeError) as exc:
            logger.warning("cua_driver.bridge_error tool=%s: %s", name, exc)
            return [mcp_types.TextContent(type="text", text=f"bridge error: {exc}")]
        except Exception as exc:  # noqa: BLE001
            logger.exception("cua_driver.unexpected_error tool=%s", name)
            return [mcp_types.TextContent(type="text", text=f"unexpected error: {exc}")]

    async with stdio_server() as streams:
        await server.run(
            streams[0],
            streams[1],
            server.create_initialization_options(),
        )


async def _dispatch(
    executor: "CuaActionExecutor",
    name: str,
    args: dict[str, Any],
) -> Any:
    """Route an MCP tool call to the executor."""
    if name == "list_windows":
        return await executor.list_windows(bool(args.get("on_screen_only", True)))
    if name == "screenshot":
        return await executor.capture(
            window_id=args.get("window_id"),
            fmt=str(args.get("format", "jpeg")),
            quality=int(args.get("quality", 85)),
        )
    if name == "get_window_state":
        text, _ = await executor.get_window_state(
            pid=args.get("pid"),
            window_id=args.get("window_id"),
        )
        return {"text": text}
    if name == "click":
        return await executor.click(
            x=float(args["x"]), y=float(args["y"]),
            button=0, count=1,
        )
    if name == "double_click":
        return await executor.click(
            x=float(args["x"]), y=float(args["y"]),
            button=0, count=2,
        )
    if name == "right_click":
        return await executor.click(
            x=float(args["x"]), y=float(args["y"]),
            button=1, count=1,
        )
    if name == "drag":
        return await executor.drag(
            from_x=float(args["from_x"]), from_y=float(args["from_y"]),
            to_x=float(args["to_x"]), to_y=float(args["to_y"]),
        )
    if name == "scroll":
        return await executor.scroll(
            direction=str(args["direction"]),
            amount=int(args["amount"]),
            x=args.get("x"), y=args.get("y"),
        )
    if name == "type_text":
        return await executor.type_text(str(args["text"]))
    if name == "press_key":
        return await executor.press_key(str(args["key"]))
    if name == "hotkey":
        return await executor.hotkey(list(args["keys"]))
    if name == "set_value":
        return await executor.set_value(
            window_id=int(args["window_id"]),
            element_index=int(args["element_index"]),
            value=str(args["value"]),
        )
    if name == "list_apps":
        return await executor.list_apps()
    raise ValueError(f"Unknown tool: {name!r}")


def _format_result(
    result: Any, mcp_types: Any
) -> list[Any]:
    """Convert an executor result dict to MCP content parts."""
    if isinstance(result, dict):
        # Image content part (from capture/screenshot)
        if result.get("type") == "image":
            return [
                mcp_types.ImageContent(
                    type="image",
                    data=result["data"],
                    mimeType=result["mimeType"],
                )
            ]
        # Error from set_value or bridge
        if result.get("isError"):
            return [mcp_types.TextContent(type="text", text=result.get("message", str(result)))]
        # Structured content (list_windows, list_apps, etc.)
        import json  # noqa: PLC0415
        return [
            mcp_types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))
        ]
    return [mcp_types.TextContent(type="text", text=str(result))]


def main() -> None:
    _configure_logging()
    if len(sys.argv) < 2 or sys.argv[1] != "mcp":
        print(
            "Usage: python3 -m hermes.lumen.cua_driver mcp",
            file=sys.stderr,
        )
        sys.exit(1)
    asyncio.run(_run_mcp_server())


if __name__ == "__main__":
    main()
