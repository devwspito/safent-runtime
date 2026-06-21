"""McpPane — MCP server management.

Extracted from IntegrationsPane. Shows the MCP servers DataTable with
add/remove actions. Composio connections live in integrations.py.

Mirrors the AgentsPane convention exactly: DataTable + row actions via
FormModal/ConfirmModal, async work through run_worker, notify for errors.
"""

from __future__ import annotations

from rich.text import Text
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import DataTable, Static

from hermes.tui.modals.common import ConfirmModal, Field, FormModal
from hermes.tui.screens.base import Pane
from hermes.tui.theme import PALETTE


def _parse_env(raw: str) -> dict[str, str]:
    """Parse a comma-separated KEY=VALUE string into a dict."""
    env: dict[str, str] = {}
    for token in raw.split(","):
        token = token.strip()
        if not token or "=" not in token:
            continue
        key, _, value = token.partition("=")
        env[key.strip()] = value.strip()
    return env


def _status_color(status: str) -> str:
    normalized = (status or "").strip().lower()
    if normalized in ("connected", "ok"):
        return PALETTE["success"]
    if normalized == "error":
        return PALETTE["error"]
    return PALETTE["text_muted"]


class McpPane(Pane):
    PANE_ID = "mcp"
    TITLE = "MCP"
    SUBTITLE = "Servidores Model Context Protocol."

    BINDINGS = [
        Binding("n", "add_mcp", "Añadir"),
        Binding("x", "remove_mcp", "Quitar"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._mcp: list[dict] = []

    def build(self):
        with VerticalScroll():
            table = DataTable(id="mcp-table", cursor_type="row", zebra_stripes=True)
            table.add_columns("Servidor", "Estado", "Tools")
            yield table
            yield Static(
                Text("n añadir servidor · x quitar", style=PALETTE["text_faint"]),
                classes="pane-subtitle",
            )

    async def activate(self) -> None:
        await self.safe_refresh()
        try:
            self.query_one("#mcp-table", DataTable).focus()
        except Exception:  # noqa: BLE001
            pass

    async def refresh_data(self) -> None:
        self._mcp = await self.bridge.list_mcp_servers()
        self._repopulate_table()

    def _repopulate_table(self) -> None:
        table = self.query_one("#mcp-table", DataTable)
        table.clear()
        if not self._mcp:
            return
        for server in self._mcp:
            name = str(server.get("name") or server.get("id") or "—")
            raw_status = str(server.get("status") or server.get("health") or "—")
            tool_count_raw = server.get("tool_count") or server.get("tools")
            tool_count = (
                str(tool_count_raw)
                if isinstance(tool_count_raw, int)
                else (str(len(tool_count_raw)) if isinstance(tool_count_raw, list) else "—")
            )
            color = _status_color(raw_status)
            table.add_row(
                Text(name, style=PALETTE["text"]),
                Text(raw_status, style=color),
                Text(tool_count, style=PALETTE["text_muted"]),
                key=str(server.get("id") or name),
            )

    def _selected_mcp(self) -> dict | None:
        table = self.query_one("#mcp-table", DataTable)
        idx = table.cursor_row
        if idx is None or idx < 0 or idx >= len(self._mcp):
            return None
        return self._mcp[idx]

    def action_add_mcp(self) -> None:
        fields = [
            Field("name", "Nombre", "p.ej. open-design", required=True),
            Field("command", "Comando", "p.ej. npx -y open-design-mcp", required=True),
            Field("env", "Variables (opcional)", "OD_DAEMON_URL=https://… , OD_API_TOKEN=…"),
        ]
        self.app.push_screen(
            FormModal(
                "Añadir servidor MCP",
                fields,
                save_label="Conectar",
                note=(
                    "Para servidores BYOK como open-design, pon sus variables "
                    "separadas por comas."
                ),
            ),
            self._on_add_result,
        )

    def _on_add_result(self, values: dict | None) -> None:
        if values is None:
            return
        draft = {
            "name": values["name"],
            "argv": values["command"].split(),
            "env": _parse_env(values["env"]),
        }
        self.run_worker(self._add_mcp(draft), exclusive=True)

    async def _add_mcp(self, draft: dict) -> None:
        try:
            await self.bridge.add_mcp_server(draft)
            self.notify(f"Servidor «{draft['name']}» conectado", timeout=3)
            await self.refresh_data()
        except Exception as exc:  # noqa: BLE001
            self.notify(f"No se pudo añadir: {exc}", severity="error", timeout=6)

    def action_remove_mcp(self) -> None:
        server = self._selected_mcp()
        if not server:
            self.notify("Selecciona un servidor primero.", severity="warning", timeout=4)
            return
        name = str(server.get("name") or server.get("id") or "servidor")
        self.app.push_screen(
            ConfirmModal(
                "Quitar servidor MCP",
                f"¿Quitar «{name}»? El agente perderá acceso a sus herramientas.",
                confirm_label="Quitar",
                danger=True,
            ),
            lambda ok, s=server: self._on_remove_result(s, ok),
        )

    def _on_remove_result(self, server: dict, ok: bool | None) -> None:
        if not ok:
            return
        self.run_worker(self._remove_mcp(server), exclusive=True)

    async def _remove_mcp(self, server: dict) -> None:
        server_id = str(server.get("id") or server.get("name") or "")
        name = str(server.get("name") or server_id)
        try:
            await self.bridge.remove_mcp_server(server_id)
            self.notify(f"Servidor «{name}» eliminado", timeout=3)
            await self.refresh_data()
        except Exception as exc:  # noqa: BLE001
            self.notify(f"No se pudo quitar: {exc}", severity="error", timeout=6)
