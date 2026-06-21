"""ProvidersPane — LLM provider list + activate / test actions.

Pattern mirrors AgentsPane exactly: DataTable for the list, run_worker for every
async mutation, honest empty state, errors surfaced with notify, and a StatusBar
update after the active provider changes.
"""

from __future__ import annotations

from rich.text import Text
from textual.binding import Binding
from textual.widgets import DataTable, Static

from hermes.tui.modals.common import ConfirmModal, Field, FormModal
from hermes.tui.screens.base import Pane
from hermes.tui.theme import PALETTE

# Valid provider kinds the daemon accepts (ProviderKind enum).
PROVIDER_KINDS = (
    "openai", "anthropic", "gemini", "azure_openai", "mistral", "cohere", "groq",
    "deepseek", "moonshot", "zhipu", "doubao", "qwen_dashscope", "openrouter",
    "together", "fireworks", "vllm", "ollama", "lm_studio", "llama_cpp", "tgi",
    "openai_compatible", "nous", "bedrock",
)


class ProvidersPane(Pane):
    PANE_ID = "providers"
    TITLE = "Proveedores"
    SUBTITLE = "Modelos LLM y proveedor activo."

    BINDINGS = [
        Binding("enter", "set_active", "Activar", show=False),
        Binding("n", "new_provider", "Nuevo"),
        Binding("t", "test", "Probar"),
        Binding("d", "delete_provider", "Borrar"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._providers: list[dict] = []
        self._active: str = ""

    def build(self):
        yield DataTable(id="prov-table", cursor_type="row", zebra_stripes=True)
        yield Static(
            Text("enter activar · n nuevo · t probar · d borrar", style=PALETTE["text_faint"]),
            classes="pane-subtitle",
            id="prov-help",
        )

    def on_mount(self) -> None:
        self.query_one("#prov-table", DataTable).add_columns(
            "", "Proveedor", "Modelo", "Estado"
        )

    async def activate(self) -> None:
        await self.safe_refresh()
        try:
            self.query_one("#prov-table", DataTable).focus()
        except Exception:  # noqa: BLE001
            pass

    async def refresh_data(self) -> None:
        raw_providers = await self.bridge.list_providers()
        active_info = await self.bridge.get_active_provider()

        # Resolve the active provider id defensively — the dict may use
        # different key names depending on the daemon version.
        self._active = str(
            active_info.get("provider_id")
            or active_info.get("id")
            or ""
        )
        self._providers = raw_providers

        table = self.query_one("#prov-table", DataTable)
        table.clear()

        if not self._providers:
            return

        for p in self._providers:
            pid = str(p.get("provider_id") or p.get("id") or "")
            is_active = pid == self._active and bool(self._active)

            marker = "●" if is_active else "·"
            marker_style = PALETTE["amber"] if is_active else PALETTE["text_faint"]

            name = str(p.get("name") or p.get("provider") or "—")
            model = str(p.get("model") or "—")
            estado_label = "activo" if is_active else "—"

            table.add_row(
                Text(marker, style=marker_style),
                Text(name, style=PALETTE["text"]),
                Text(model, style=PALETTE["text_muted"]),
                Text(estado_label, style=marker_style if is_active else PALETTE["text_muted"]),
                key=pid,
            )

    # -- selection helper ---------------------------------------------------

    def _selected(self) -> dict | None:
        table = self.query_one("#prov-table", DataTable)
        idx = table.cursor_row
        if idx is None or idx < 0 or idx >= len(self._providers):
            return None
        return self._providers[idx]

    # -- event wiring -------------------------------------------------------

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.action_set_active()

    # -- actions ------------------------------------------------------------

    def action_set_active(self) -> None:
        provider = self._selected()
        if not provider:
            return
        self.run_worker(self._activate(provider), exclusive=True)

    async def _activate(self, provider: dict) -> None:
        pid = str(provider.get("provider_id") or provider.get("id") or "")
        name = str(provider.get("name") or provider.get("provider") or pid)
        model = str(provider.get("model") or "")
        try:
            await self.bridge.set_active_provider(pid)
            self.notify(f"Proveedor activo: {name}", timeout=3)
            await self.refresh_data()
            try:
                status_bar = self.app.query_one("StatusBar")
                status_bar.model_name = model or name  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 — StatusBar may not exist in all layouts
                pass
        except Exception as exc:  # noqa: BLE001
            self.notify(f"No se pudo activar: {exc}", severity="error", timeout=6)

    def action_test(self) -> None:
        provider = self._selected()
        if not provider:
            return
        self.run_worker(self._test(provider), exclusive=True)

    async def _test(self, provider: dict) -> None:
        pid = str(provider.get("provider_id") or provider.get("id") or "")
        name = str(provider.get("name") or provider.get("provider") or pid)
        try:
            res = await self.bridge.test_provider(pid)
            ok = res.get("ok") if isinstance(res, dict) else None
            if ok is True or str(ok).lower() in ("true", "1", "ok"):
                self.notify(f"{name}: Proveedor OK", timeout=4)
            else:
                error = (
                    res.get("error")
                    or res.get("status")
                    or res.get("message")
                    or "error desconocido"
                ) if isinstance(res, dict) else str(res)
                self.notify(f"{name}: {error}", severity="warning", timeout=6)
        except Exception as exc:  # noqa: BLE001
            self.notify(f"No se pudo probar: {exc}", severity="error", timeout=6)

    # -- add / delete -------------------------------------------------------

    def action_new_provider(self) -> None:
        fields = [
            Field("alias", "Nombre", "p.ej. OpenAI trabajo", required=True),
            Field("kind", "Tipo", "openai", value="openai", required=True),
            Field("default_model", "Modelo", "p.ej. gpt-4o / claude-opus-4", required=True),
            Field("api_key", "API key", "se guarda cifrada en el keystore", secret=True),
            Field("base_url", "Base URL (opcional)", "solo para openai_compatible/vllm/ollama"),
        ]
        note = "Tipos: " + ", ".join(PROVIDER_KINDS[:12]) + "…  (OAuth: usa el pane tras crearlo)"
        self.app.push_screen(
            FormModal("Nuevo proveedor LLM", fields, save_label="Crear y activar", note=note),
            self._on_new_result,
        )

    def _on_new_result(self, values: dict | None) -> None:
        if values is None:
            return
        kind = (values.get("kind") or "").strip().lower()
        if kind not in PROVIDER_KINDS:
            self.notify(f"Tipo «{kind}» no válido. Usa uno de: {', '.join(PROVIDER_KINDS[:8])}…",
                        severity="error", timeout=8)
            return
        draft = {
            "kind": kind,
            "alias": values.get("alias", "").strip(),
            "default_model": values.get("default_model", "").strip(),
            "base_url": values.get("base_url", "").strip() or None,
            "api_key": values.get("api_key", "").strip() or None,
            "set_active": True,
        }
        self.run_worker(self._create(draft), exclusive=True)

    async def _create(self, draft: dict) -> None:
        try:
            saved = await self.bridge.add_provider(draft)
            self.notify(f"Proveedor «{draft['alias']}» creado y activado", timeout=4)
            await self.refresh_data()
            model = (saved.get("model") if isinstance(saved, dict) else "") or draft["default_model"]
            try:
                self.app.query_one("StatusBar").model_name = model  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
        except Exception as exc:  # noqa: BLE001
            self.notify(f"No se pudo crear: {exc}", severity="error", timeout=8)

    def action_delete_provider(self) -> None:
        provider = self._selected()
        if not provider:
            return
        name = str(provider.get("name") or provider.get("provider") or "este proveedor")
        pid = str(provider.get("provider_id") or provider.get("id") or "")
        self.app.push_screen(
            ConfirmModal("Borrar proveedor", f"¿Borrar «{name}»?",
                         confirm_label="Borrar", danger=True),
            lambda ok: self.run_worker(self._delete(pid), exclusive=True) if ok else None,
        )

    async def _delete(self, pid: str) -> None:
        try:
            await self.bridge.delete_provider(pid)
            self.notify("Proveedor borrado", timeout=3)
            await self.refresh_data()
        except Exception as exc:  # noqa: BLE001
            self.notify(f"No se pudo borrar: {exc}", severity="error", timeout=6)
