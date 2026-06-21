"""SecurityPane — audit chain, security policy and recent scan history.

Read-only view. No mutations are exposed here; the pane only surfaces what the
daemon reports through three bridge verbs:
  - get_audit_chain_head()  → audit chain integrity + head hash
  - get_security_policy()   → policy key/value pairs
  - list_recent_scans(50)   → chronological scan verdicts
"""

from __future__ import annotations

from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import DataTable, Static

from hermes.tui.screens.base import Pane
from hermes.tui.theme import PALETTE


def _verdict_color(verdict: str) -> str:
    key = (verdict or "").strip().lower()
    if key in ("pass", "clean", "ok"):
        return PALETTE["success"]
    if key in ("fail", "blocked", "malicious"):
        return PALETTE["error"]
    if key in ("warn", "suspicious"):
        return PALETTE["warning"]
    return PALETTE["text_muted"]


def _integrity_color(integrity: str) -> str:
    key = (integrity or "").strip().lower()
    if key in ("ok", "intact", "valid"):
        return PALETTE["success"]
    if key == "unknown":
        return PALETTE["text_muted"]
    return PALETTE["error"]


def _bool_es(value: object) -> str:
    return "Sí" if value else "No"


def _render_policy(policy: dict) -> Text:
    """Build a Rich Text block for up to 6 non-empty policy entries."""
    title = Text("Política de seguridad\n", style=PALETTE["amber"])
    if not policy:
        title.append("Política por defecto.", style=PALETTE["text_muted"])
        return title

    rendered = 0
    for key, val in policy.items():
        if val is None or val == "":
            continue
        display_val = _bool_es(val) if isinstance(val, bool) else str(val)
        title.append(f"{key}: ", style=PALETTE["text_muted"])
        title.append(f"{display_val}\n", style=PALETTE["text"])
        rendered += 1
        if rendered >= 6:
            break
    return title


def _render_audit_head(head: dict) -> Text:
    integrity = str(head.get("integrity") or "unknown")
    color = _integrity_color(integrity)

    line = Text("Cadena de auditoría: ", style=PALETTE["text_muted"])
    line.append(integrity, style=color)
    line.append("\n")

    raw_hash = str(head.get("head_hash") or head.get("head") or "")
    if raw_hash:
        short = raw_hash[:16] + "…"
        line.append("Hash: ", style=PALETTE["text_faint"])
        line.append(short, style=PALETTE["text_muted"])
        line.append("\n")

    captured_at = str(head.get("captured_at") or "")
    if captured_at:
        line.append("Capturado: ", style=PALETTE["text_faint"])
        line.append(captured_at, style=PALETTE["text_muted"])

    return line


class SecurityPane(Pane):
    PANE_ID = "security"
    TITLE = "Seguridad"
    SUBTITLE = "Auditoría, política y escaneos."

    def __init__(self) -> None:
        super().__init__()
        self._scans: list[dict] = []

    def build(self):
        with VerticalScroll():
            yield Static("", id="audit-card", classes="card")
            yield Static("", id="policy-card", classes="card")
            yield Static("Escaneos recientes", classes="pane-subtitle")
            yield DataTable(
                id="scans-table",
                cursor_type="row",
                zebra_stripes=True,
            )

    async def activate(self) -> None:
        await self.safe_refresh()

    async def refresh_data(self) -> None:
        head = await self.bridge.get_audit_chain_head()
        policy = await self.bridge.get_security_policy()
        self._scans = await self.bridge.list_recent_scans(50)

        self.query_one("#audit-card", Static).update(_render_audit_head(head))
        self.query_one("#policy-card", Static).update(_render_policy(policy))

        table = self.query_one("#scans-table", DataTable)
        table.clear(columns=True)
        table.add_columns("Veredicto", "Tipo", "Objetivo", "Cuándo")

        for scan in self._scans:
            verdict_raw = str(
                scan.get("verdict") or scan.get("result") or ""
            )
            kind = str(scan.get("kind") or scan.get("target_kind") or "—")
            target = str(
                scan.get("identifier")
                or scan.get("target")
                or scan.get("name")
                or "—"
            )
            when = str(scan.get("created_at") or scan.get("when") or "—")

            verdict_color = _verdict_color(verdict_raw)
            verdict_text = verdict_raw or "—"

            table.add_row(
                Text(verdict_text, style=verdict_color),
                Text(kind, style=PALETTE["text_muted"]),
                Text(target, style=PALETTE["text"]),
                Text(when, style=PALETTE["text_faint"]),
            )
