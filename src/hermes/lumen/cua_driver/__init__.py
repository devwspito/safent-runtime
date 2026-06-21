"""hermes.lumen.cua_driver — MCP stdio server for LumenSO computer-use.

Exposes 13 MCP tools that translate to SessionInputBridge commands so
hermes-agent's native `computer_use` toolset can control the LumenSO
Wayland compositor.

Entry point:
    python3 -m hermes.lumen.cua_driver mcp

Environment:
    HERMES_CUA_DRIVER_CMD   path/command (set by compositor or systemd unit)
    SESSION_INPUT_SOCK      override socket path (test / debug)
    SESSION_INPUT_TOKEN     override token path (test / debug)
"""
