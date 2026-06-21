"""IntegrationsPane — Composio connections only.

MCP servers have been moved to mcp.py (McpPane). This pane owns:
  - Composio API key configuration.
  - OAuth-simple app connections (Gmail, Drive, Slack, …).
  - Listing active connections.

Mirrors the AgentsPane convention: card + async refresh, notify for errors.
"""

from __future__ import annotations

from rich.text import Text
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Static

from hermes.tui.modals.common import Field, FormModal
from hermes.tui.screens.base import Pane
from hermes.tui.theme import PALETTE


class IntegrationsPane(Pane):
    PANE_ID = "integrations"
    TITLE = "Integraciones"
    SUBTITLE = "Conexiones Composio — apps OAuth conectadas al agente."

    BINDINGS = [
        Binding("k", "composio_key", "API key"),
        Binding("c", "composio_connect", "Conectar app"),
    ]

    # -- compose -----------------------------------------------------------

    def build(self):
        with VerticalScroll():
            yield Static(id="composio-card", classes="card")
            yield Static(
                Text(
                    "k configurar API key  ·  c conectar app OAuth",
                    style=PALETTE["text_faint"],
                ),
                classes="pane-subtitle",
            )

    # -- lifecycle ---------------------------------------------------------

    async def activate(self) -> None:
        await self.safe_refresh()

    async def refresh_data(self) -> None:
        await self._repopulate_composio_card()

    # -- internal helpers --------------------------------------------------

    async def _repopulate_composio_card(self) -> None:
        card = self.query_one("#composio-card", Static)

        status = await self.bridge.get_composio_status()
        configured = bool(status.get("configured"))

        lines: list[Text] = []
        if configured:
            header = Text("Composio conectado", style=PALETTE["success"])
            entity_id = status.get("entity_id")
            if entity_id:
                header.append(f"  ·  {entity_id}", style=PALETTE["text_muted"])
            lines.append(header)
        else:
            lines.append(Text("Composio sin configurar", style=PALETTE["text_muted"]))
            lines.append(
                Text(
                    "Pulsa k para añadir tu API key de Composio.",
                    style=PALETTE["text_faint"],
                )
            )

        connections = await self.bridge.list_composio_connections()
        if connections:
            lines.append(Text(""))
            for conn in connections:
                label = str(conn.get("name") or conn.get("app") or conn.get("slug") or "—")
                alias = conn.get("alias")
                row = Text(f"  · {label}", style=PALETTE["text"])
                if alias:
                    row.append(f"  ({alias})", style=PALETTE["text_muted"])
                lines.append(row)
        else:
            lines.append(Text(""))
            lines.append(Text("Sin cuentas conectadas.", style=PALETTE["text_faint"]))

        card.update(Text("\n").join(lines))

    # -- actions -----------------------------------------------------------

    def action_composio_key(self) -> None:
        self.app.push_screen(
            FormModal(
                "API key de Composio",
                [Field("api_key", "API key", "ck_…", secret=True, required=True)],
                save_label="Guardar",
                note="Composio gestiona las conexiones OAuth de tus apps en la nube.",
            ),
            lambda v: self.run_worker(self._set_composio_key(v), exclusive=True) if v else None,
        )

    async def _set_composio_key(self, values: dict) -> None:
        try:
            await self.bridge.set_composio_api_key(values["api_key"].strip())
            self.notify("Composio configurado", timeout=3)
            await self.refresh_data()
        except Exception as exc:  # noqa: BLE001
            self.notify(f"No se pudo configurar Composio: {exc}", severity="error", timeout=6)

    def action_composio_connect(self) -> None:
        self.app.push_screen(
            FormModal(
                "Conectar app Composio",
                [Field("slug", "App (slug)", "p.ej. gmail, googledrive, slack", required=True)],
                save_label="Conectar",
                note="Te daré un enlace para autorizar la app en tu navegador.",
            ),
            lambda v: self.run_worker(self._connect_composio(v), exclusive=True) if v else None,
        )

    async def _connect_composio(self, values: dict) -> None:
        slug = values["slug"].strip().lower()
        try:
            res = await self.bridge.connect_composio_app(slug)
            url = res.get("connect_url") or res.get("redirect_url") or res.get("url")
            if url:
                self.notify(
                    f"Abre este enlace para conectar {slug}:\n{url}",
                    title="Autoriza la app",
                    timeout=20,
                )
            else:
                self.notify(f"Conexión de {slug} iniciada: {res}", timeout=8)
            await self.refresh_data()
        except Exception as exc:  # noqa: BLE001
            self.notify(f"No se pudo conectar {slug}: {exc}", severity="error", timeout=6)
