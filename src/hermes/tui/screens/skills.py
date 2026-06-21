"""SkillsPane — list, promote, and deprecate agent skills.

Mirrors AgentsPane idioms exactly: DataTable list, row actions via ConfirmModal,
async work through run_worker, honest empty state, errors via notify. Skills flow
from 'validating' → 'validated' → 'autonomous'; deprecating moves any live skill
out of rotation without deleting the package.
"""

from __future__ import annotations

from rich.text import Text
from textual.binding import Binding
from textual.widgets import DataTable, Static

from hermes.tui.modals.common import ConfirmModal
from hermes.tui.modals.search_install import SearchInstallModal
from hermes.tui.screens.base import Pane
from hermes.tui.theme import PALETTE

# Maps internal state values to human-readable Spanish labels.
_STATE_LABELS: dict[str, str] = {
    "validated": "validada",
    "autonomous": "autónoma",
    "validating": "en validación",
    "deprecated": "retirada",
}

# Maps internal state values to palette color keys.
_STATE_COLORS: dict[str, str] = {
    "autonomous": PALETTE["success"],
    "validated": PALETTE["amber"],
    "validating": PALETTE["text_muted"],
    "deprecated": PALETTE["text_faint"],
}


def _state_color(state: str) -> str:
    return _STATE_COLORS.get(state, PALETTE["text_muted"])


def _state_label(state: str) -> str:
    return _STATE_LABELS.get(state, state)


def _skill_state(skill: dict) -> str:
    """Read state/status defensively — dicts from different daemon versions vary."""
    return str(skill.get("state") or skill.get("status") or "")


def _skill_package_id(skill: dict) -> str:
    return str(skill.get("package_id") or skill.get("id") or "")


def _skill_name(skill: dict) -> str:
    return str(skill.get("name") or skill.get("title") or skill.get("id") or "—")


def _skill_origin(skill: dict) -> str:
    return str(skill.get("source") or skill.get("kind") or skill.get("origin") or "—")


class SkillsPane(Pane):
    PANE_ID = "skills"
    TITLE = "Skills"
    SUBTITLE = "Skills que el agente ha aprendido."

    BINDINGS = [
        Binding("p", "promote", "Promover"),
        Binding("x", "deprecate", "Retirar"),
        Binding("h", "hub", "Hub"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._skills: list[dict] = []

    def build(self):
        table = DataTable(id="skills-table", cursor_type="row", zebra_stripes=True)
        table.add_columns("Skill", "Estado", "Origen")
        yield table
        yield Static(
            Text(
                "p promover (validada → autónoma) · x retirar",
                style=PALETTE["text_faint"],
            ),
            id="skills-help",
            classes="pane-subtitle",
        )

    async def activate(self) -> None:
        await self.safe_refresh()
        try:
            self.query_one("#skills-table", DataTable).focus()
        except Exception:  # noqa: BLE001
            pass

    async def refresh_data(self) -> None:
        self._skills = await self.bridge.list_skills()
        table = self.query_one("#skills-table", DataTable)
        table.clear()
        if not self._skills:
            return
        for skill in self._skills:
            state = _skill_state(skill)
            table.add_row(
                Text(_skill_name(skill), style=PALETTE["text"]),
                Text(_state_label(state), style=_state_color(state)),
                Text(_skill_origin(skill), style=PALETTE["text_muted"]),
                key=_skill_package_id(skill),
            )

    # -- selection helper -------------------------------------------------

    def _selected(self) -> dict | None:
        table = self.query_one("#skills-table", DataTable)
        idx = table.cursor_row
        if idx is None or idx < 0 or idx >= len(self._skills):
            return None
        return self._skills[idx]

    # -- actions ----------------------------------------------------------

    def action_promote(self) -> None:
        skill = self._selected()
        if not skill:
            return
        if _skill_state(skill) != "validated":
            self.notify(
                "Solo las skills validadas se pueden promover.",
                severity="warning",
                timeout=4,
            )
            return
        self.run_worker(self._promote(skill), exclusive=True)

    async def _promote(self, skill: dict) -> None:
        try:
            await self.bridge.promote_skill(_skill_package_id(skill))
            self.notify(f"Skill «{_skill_name(skill)}» promovida a autónoma", timeout=3)
            await self.refresh_data()
        except Exception as exc:  # noqa: BLE001
            self.notify(f"No se pudo promover: {exc}", severity="error", timeout=6)

    def action_deprecate(self) -> None:
        skill = self._selected()
        if not skill:
            return
        self.app.push_screen(
            ConfirmModal(
                "Retirar skill",
                f"¿Retirar «{_skill_name(skill)}»?",
                confirm_label="Retirar",
                danger=True,
            ),
            lambda ok, s=skill: self._on_deprecate_result(s, ok),
        )

    def _on_deprecate_result(self, skill: dict, ok: bool | None) -> None:
        if not ok:
            return
        self.run_worker(self._deprecate(skill), exclusive=True)

    async def _deprecate(self, skill: dict) -> None:
        try:
            await self.bridge.deprecate_skill(_skill_package_id(skill))
            self.notify(f"Skill «{_skill_name(skill)}» retirada", timeout=3)
            await self.refresh_data()
        except Exception as exc:  # noqa: BLE001
            self.notify(f"No se pudo retirar: {exc}", severity="error", timeout=6)

    # -- Skills Hub (marketplace) -----------------------------------------

    def action_hub(self) -> None:
        async def _search(q: str) -> list[dict]:
            res = await self.bridge.search_skills_hub(q)
            return res.get("results", []) if isinstance(res, dict) else []

        async def _install(identifier: str) -> None:
            await self.bridge.install_hub_skill(identifier)
            await self.refresh_data()

        self.app.push_screen(
            SearchInstallModal(
                title="Skills Hub",
                placeholder="Buscar skill en el Hub… (Enter)",
                columns=("Skill", "Origen"),
                search_fn=_search,
                install_fn=_install,
                row_cells=lambda i: (i.get("name") or i.get("identifier") or "—", i.get("source") or "hub"),
                row_id=lambda i: str(i.get("identifier") or i.get("name") or ""),
            )
        )
