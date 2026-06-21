"""HermesSkillsView — Biblioteca de SkillPackages + modo enseñanza aislado."""

from __future__ import annotations

import logging
import threading
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from hermes.shell.infrastructure.shell_backend_client import (
    ShellBackendClient,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pure helpers — no GTK dependency; unit-testable headlessly.
# ---------------------------------------------------------------------------


def _site_id_from_input(site: str) -> str:
    """Extrae el dominio para la clave de aislamiento. '' si no hay sitio."""
    s = site.strip()
    if not s:
        return ""
    s = s.removeprefix("https://").removeprefix("http://")
    return s.split("/", 1)[0].strip().lower()


def _site_url_from_input(site: str) -> str:
    """Normaliza la entrada del usuario a una URL navegable. '' si está vacío."""
    s = site.strip()
    if not s:
        return ""
    if s.startswith(("http://", "https://")):
        return s
    return f"https://{s}"


def _composio_badge_label(skill: dict) -> str | None:
    """Return a human-readable integration badge string, or None for screen-recorded skills.

    Reads defensively — backend DTO field names are not yet finalized at the
    time of writing.  Expected fields (any one sufficient):
      • skill_kind == "composio"  +  toolkit / composio_toolkit
    Falls back to None (no badge) when field is absent or empty.
    """
    kind = skill.get("skill_kind") or skill.get("kind") or ""
    if kind != "composio":
        return None
    toolkit = (
        skill.get("toolkit_slug")
        or skill.get("toolkit")
        or skill.get("composio_toolkit")
        or ""
    )
    if not toolkit:
        return "Integración"
    # Render the slug in title-case as a friendly name (e.g. "GMAIL" → "Gmail").
    return f"Integración: {toolkit.replace('_', ' ').title()}"


def _connected_account_display_name(acc: dict) -> str:
    """Return a friendly display name for a connected Composio account."""
    name = acc.get("name") or acc.get("toolkit_slug") or acc.get("app_name") or ""
    return name.replace("_", " ").title() if name else "(sin nombre)"


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Human-readable labels for each skill lifecycle state.
_STATE_LABELS: dict[str, str] = {
    "draft": "Borrador",
    "validated": "Revisada",
    "autonomous": "Autónoma",
    # Legacy persisted value from spec 002 — treated as validated.
    "signed": "Revisada",
    "deprecated": "Obsoleta",
}

# States that allow the "Promover a autónoma" action.
_PROMOTABLE_STATES = {"validated", "signed"}

# States that allow the "Deprecar" action.
_DEPRECABLE_STATES = {"draft", "validated", "signed", "autonomous"}


class HermesSkillsView(Gtk.Box):
    def __init__(self, *, client: ShellBackendClient) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._client = client
        self.set_hexpand(True)
        self.set_vexpand(True)

        # Toast overlay wraps the whole view so toasts render above the list.
        self._toast_overlay = Adw.ToastOverlay()
        self._toast_overlay.set_vexpand(True)
        self._toast_overlay.set_hexpand(True)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        inner.set_hexpand(True)
        inner.set_vexpand(True)

        inner.append(self._build_toolbar())

        # Container that holds either the list or the empty state.
        self._content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._content_box.set_vexpand(True)
        inner.append(self._content_box)

        self._list_box = Gtk.ListBox()
        self._list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self._list_box.add_css_class("boxed-list")
        self._list_box.set_margin_start(24)
        self._list_box.set_margin_end(24)
        self._list_box.set_margin_bottom(24)
        self._list_box.set_margin_top(8)

        self._scroll = Gtk.ScrolledWindow()
        self._scroll.set_child(self._list_box)
        self._scroll.set_vexpand(True)
        self._content_box.append(self._scroll)

        self._empty_state: Gtk.Widget | None = None

        self._toast_overlay.set_child(inner)
        self.append(self._toast_overlay)

        self._reload()

    # ------------------------------------------------------------------
    # Toolbar
    # ------------------------------------------------------------------

    def _build_toolbar(self) -> Gtk.Widget:
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        toolbar.add_css_class("hermes-view-toolbar")

        title = Gtk.Label(label="Habilidades aprendidas")
        title.add_css_class("hermes-page-title")
        title.set_xalign(0)
        title.set_hexpand(True)
        toolbar.append(title)

        teach_btn = Gtk.Button.new_with_label("+ Enseñar skill")
        teach_btn.add_css_class("hermes-primary")
        teach_btn.set_tooltip_text(
            "Enseñar una nueva habilidad en un espacio aislado — "
            "el agente sigue trabajando sin interrupción"
        )
        teach_btn.connect("clicked", lambda _b: self._open_teach_dialog())
        toolbar.append(teach_btn)

        refresh_btn = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        refresh_btn.add_css_class("flat")
        refresh_btn.set_tooltip_text("Refrescar lista")
        refresh_btn.connect("clicked", lambda _b: self._reload())
        toolbar.append(refresh_btn)

        return toolbar

    # ------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------

    def _reload(self) -> None:
        while (child := self._list_box.get_first_child()) is not None:
            self._list_box.remove(child)
        self._scroll.set_visible(True)

        def runner() -> None:
            try:
                skills = self._client.list_skills()
            except Exception as exc:  # noqa: BLE001
                logger.warning("skills list: %s", exc)
                skills = []
            GLib.idle_add(lambda: self._render(skills))

        threading.Thread(target=runner, daemon=True).start()

    def _render(self, skills: list[dict]) -> bool:
        # Remove any previous empty state widget before re-rendering.
        if hasattr(self, "_empty_state") and self._empty_state is not None:
            self._content_box.remove(self._empty_state)
            self._empty_state = None

        if not skills:
            self._scroll.set_visible(False)
            empty = Adw.StatusPage()
            empty.set_icon_name("starred-symbolic")
            empty.set_title("Aún no has enseñado ninguna skill")
            empty.set_description(
                "Enséñale una tarea repetitiva una vez; la aprende y la repite sola."
            )
            empty.set_vexpand(True)
            cta = Gtk.Button.new_with_label("Enseñar mi primera skill")
            cta.add_css_class("hermes-primary")
            cta.set_halign(Gtk.Align.CENTER)
            cta.connect("clicked", lambda _b: self._open_teach_dialog())
            empty.set_child(cta)
            self._empty_state = empty
            self._content_box.append(self._empty_state)
            return False

        self._scroll.set_visible(True)
        for s in skills:
            self._list_box.append(self._build_row(s))
        return False

    # ------------------------------------------------------------------
    # Row builder
    # ------------------------------------------------------------------

    def _build_row(self, s: dict) -> Gtk.Widget:
        row = Adw.ActionRow()
        row.set_title(s.get("skill_name", "(sin nombre)"))

        version = s.get("version", "?")
        state = s.get("state", "?")
        surfaces = ", ".join(s.get("surface_kinds", [])) or "?"
        state_label = _STATE_LABELS.get(state, state)
        row.set_subtitle(f"v{version} · {state_label} · {surfaces}")

        # Composio integration badge — shown instead of (or alongside) the
        # "Autónoma" badge for skills backed by a connected integration.
        integration_label = _composio_badge_label(s)
        if integration_label is not None:
            integ_badge = Gtk.Label.new(integration_label)
            integ_badge.add_css_class("hermes-skill-badge-integration")
            integ_badge.set_valign(Gtk.Align.CENTER)
            row.add_suffix(integ_badge)

        # Badge "Autónoma" — rightmost, before action buttons.
        if state == "autonomous":
            badge = Gtk.Label.new("Autónoma")
            badge.add_css_class("hermes-skill-badge-autonomous")
            badge.set_valign(Gtk.Align.CENTER)
            row.add_suffix(badge)

        # "Promover a autónoma" — only for validated/signed skills.
        if state in _PROMOTABLE_STATES:
            promote_btn = Gtk.Button.new_with_label("Promover a autónoma")
            promote_btn.add_css_class("hermes-ghost")
            promote_btn.set_valign(Gtk.Align.CENTER)
            promote_btn.set_tooltip_text(
                "Autorizar a Hermes para ejecutar esta habilidad de forma autónoma"
            )
            promote_btn.connect(
                "clicked",
                lambda _b, pid=s["package_id"]: self._promote(pid),
            )
            row.add_suffix(promote_btn)

        # "Deprecar" — for all non-deprecated states.
        if state in _DEPRECABLE_STATES:
            depr_btn = Gtk.Button.new_with_label("Deprecar")
            depr_btn.add_css_class("flat")
            depr_btn.add_css_class("destructive-action")
            depr_btn.set_valign(Gtk.Align.CENTER)
            depr_btn.set_tooltip_text("Marcar como obsoleta; ya no se usará")
            depr_btn.connect(
                "clicked",
                lambda _b, pid=s["package_id"]: self._deprecate(pid),
            )
            row.add_suffix(depr_btn)

        return row

    # ------------------------------------------------------------------
    # Actions — deprecate / promote
    # ------------------------------------------------------------------

    def _deprecate(self, package_id: str) -> None:
        def runner() -> None:
            try:
                self._client.deprecate_skill(package_id=package_id)
                GLib.idle_add(self._reload)
            except Exception as exc:  # noqa: BLE001
                msg = f"No se pudo deprecar: {exc}"
                logger.warning("deprecate: %s", exc)
                GLib.idle_add(lambda m=msg: self._show_toast(m))

        threading.Thread(target=runner, daemon=True).start()

    def _promote(self, package_id: str) -> None:
        def runner() -> None:
            try:
                self._client.promote_skill(package_id=package_id)
                GLib.idle_add(self._reload)
                GLib.idle_add(
                    lambda: self._show_toast(
                        "Skill promovida — Hermes la ejecutará de forma autónoma"
                    )
                )
            except Exception as exc:  # noqa: BLE001
                msg = f"No se pudo promover: {exc}"
                logger.warning("promote: %s", exc)
                GLib.idle_add(lambda m=msg: self._show_toast(m))

        threading.Thread(target=runner, daemon=True).start()

    # ------------------------------------------------------------------
    # Teach dialog — branch chooser → recording OR Composio form
    # ------------------------------------------------------------------

    def _open_teach_dialog(self) -> None:
        chooser = _TeachBranchChooser(
            parent=self.get_root(),
            on_recording=self._open_recording_teach_dialog,
            on_composio=self._open_composio_teach_dialog,
        )
        chooser.present()

    def _open_recording_teach_dialog(self) -> None:
        dlg = _TeachSkillDialog(
            parent=self.get_root(),
            on_start=self._launch_teaching_session,
        )
        dlg.present()

    def _open_composio_teach_dialog(self) -> None:
        dlg = _TeachComposioDialog(
            parent=self.get_root(),
            client=self._client,
            on_saved=self._on_composio_skill_saved,
        )
        dlg.present()

    def _on_composio_skill_saved(self) -> None:
        """Called after a composio skill is saved — reload the skills list."""
        GLib.idle_add(self._reload)

    def _launch_teaching_session(
        self, *, skill_name: str, description: str, site: str = ""
    ) -> None:
        """Call start_teaching, then open the training panel bound to that session."""
        site_id = _site_id_from_input(site)
        site_url = _site_url_from_input(site)

        def runner() -> None:
            try:
                data = self._client.start_teaching(
                    skill_name=skill_name,
                    description=description or None,
                    site_id=site_id,
                )
            except Exception as exc:  # noqa: BLE001
                msg = f"No se pudo iniciar la sesión: {exc}"
                logger.warning("start_teaching: %s", exc)
                GLib.idle_add(lambda m=msg: self._show_toast(m))
                return
            # Enrich the teaching context with the URL the panel should open
            # the isolated Chromium at (the backend only tracks site_id).
            tc = data.get("teaching_context")
            if tc is not None and site_url:
                tc["site_url"] = site_url
            GLib.idle_add(lambda: self._open_teaching_panel(skill_name, data))

        threading.Thread(target=runner, daemon=True).start()

    def _open_teaching_panel(self, skill_name: str, session_data: dict) -> bool:
        """Open the HermesTrainingPanel in a dedicated window for the teach session.

        The skill name/description/site are already known from the first dialog
        (_TeachSkillDialog), so they are passed directly to the panel.  The
        panel must NOT ask for them again (change #3).
        """
        from hermes.shell.presentation.gtk4.widgets.training_panel import (  # noqa: PLC0415
            HermesTrainingPanel,
        )

        teaching_ctx = session_data.get("teaching_context")

        win = Adw.Window()
        win.set_transient_for(self.get_root())
        win.set_modal(False)  # Non-modal: agent keeps running while user teaches.
        win.set_default_size(700, 560)
        win.set_title(f"Enseñando: {skill_name}")
        win.set_decorated(True)

        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar_view.add_top_bar(header)

        # "Minimizar" en la cabecera — oculta la ventana pero el agente
        # sigue trabajando (change #2).
        minimize_btn = Gtk.Button.new_with_label("Minimizar")
        minimize_btn.add_css_class("flat")
        minimize_btn.set_tooltip_text(
            "Oculta este panel; el agente de enseñanza sigue activo"
        )
        minimize_btn.connect("clicked", lambda _b: win.minimize())
        header.pack_end(minimize_btn)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        outer.set_vexpand(True)

        # Skill name/description/site collected in the first dialog are injected
        # here so the panel starts directly in the correct state without a
        # second form.  session_id was already minted by start_teaching().
        site_url = (teaching_ctx or {}).get("site_url", "")
        panel = HermesTrainingPanel(
            client=self._client,
            teaching_context=teaching_ctx,
            prefilled_skill_name=skill_name,
            prefilled_description=session_data.get("description", ""),
            prefilled_site_url=site_url,
            prefilled_session_id=session_data.get("session_id"),
            prefilled_state=session_data.get("state", "idle"),
        )
        panel.set_vexpand(True)
        outer.append(panel)

        toolbar_view.set_content(outer)
        win.set_content(toolbar_view)

        # Reload skills list when the teach window is closed (skill may now exist).
        win.connect("close-request", lambda _w: self._reload() or False)

        win.present()
        return False

    @staticmethod
    def _build_isolation_banner(
        teaching_ctx: dict, session_id: str
    ) -> Gtk.Widget:
        """Infobar explaining the isolated context to the operator."""
        banner_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        banner_box.add_css_class("hermes-teach-banner")
        banner_box.set_margin_top(0)
        banner_box.set_margin_bottom(0)

        icon = Gtk.Image.new_from_icon_name("system-run-symbolic")
        icon.set_pixel_size(16)
        banner_box.append(icon)

        isolation_key = teaching_ctx.get("isolation_key") or session_id
        surface = teaching_ctx.get("surface_kind", "browser")
        lbl = Gtk.Label()
        lbl.set_markup(
            f"<b>Espacio aislado</b>  ·  superficie: {surface}  ·  clave: {isolation_key}\n"
            "<small>El agente sigue trabajando sin interrupción en su contexto propio.</small>"
        )
        lbl.set_xalign(0)
        lbl.set_wrap(True)
        lbl.set_hexpand(True)
        banner_box.append(lbl)

        return banner_box

    # ------------------------------------------------------------------
    # Toast helper
    # ------------------------------------------------------------------

    def _show_toast(self, msg: str) -> bool:
        toast = Adw.Toast.new(msg)
        toast.set_timeout(4)
        self._toast_overlay.add_toast(toast)
        return False


# ---------------------------------------------------------------------------
# Teach skill dialog — name + optional description
# ---------------------------------------------------------------------------


class _TeachSkillDialog(Adw.Window):
    """Modal form: skill name + description → calls on_start with keyword args."""

    def __init__(
        self,
        *,
        parent: Gtk.Window,
        on_start: Callable[..., None],
    ) -> None:
        super().__init__()
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_title("Enseñar nueva skill")
        self.set_default_size(480, 300)
        self._on_start = on_start

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)

        cancel_btn = Gtk.Button.new_with_label("Cancelar")
        cancel_btn.connect("clicked", lambda _b: self.close())
        header.pack_start(cancel_btn)

        start_btn = Gtk.Button.new_with_label("Comenzar enseñanza")
        start_btn.add_css_class("hermes-primary")
        start_btn.connect("clicked", lambda _b: self._start())
        header.pack_end(start_btn)

        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup()
        group.set_title("Nueva habilidad")
        group.set_description(
            "Se abrirá un espacio aislado para la demostración. "
            "El agente continúa trabajando sin interrupción."
        )

        self._name_row = Adw.EntryRow()
        self._name_row.set_title("Nombre de la habilidad")
        group.add(self._name_row)

        self._desc_row = Adw.EntryRow()
        self._desc_row.set_title("Descripción (opcional)")
        group.add(self._desc_row)

        self._site_row = Adw.EntryRow()
        self._site_row.set_title("Sitio web (p.ej. amazon.es — opcional)")
        group.add(self._site_row)

        page.add(group)
        toolbar.set_content(page)
        self.set_content(toolbar)

    def _start(self) -> None:
        name = self._name_row.get_text().strip()
        if not name:
            return
        self._on_start(
            skill_name=name,
            description=self._desc_row.get_text().strip(),
            site=self._site_row.get_text().strip(),
        )
        self.close()


# ---------------------------------------------------------------------------
# Branch chooser — presented first; user picks recording vs. integration.
# ---------------------------------------------------------------------------


class _TeachBranchChooser(Adw.Window):
    """One-step modal: choose the teach method before entering details.

    Two cards:
      • "Conectar una integración" → _TeachComposioDialog
      • "Grabar en pantalla"       → _TeachSkillDialog (existing flow)
    """

    def __init__(
        self,
        *,
        parent: Gtk.Window,
        on_recording: Callable[[], None],
        on_composio: Callable[[], None],
    ) -> None:
        super().__init__()
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_title("Enseñar nueva habilidad")
        self.set_default_size(480, 360)
        self._on_recording = on_recording
        self._on_composio = on_composio

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)

        cancel_btn = Gtk.Button.new_with_label("Cancelar")
        cancel_btn.connect("clicked", lambda _b: self.close())
        header.pack_start(cancel_btn)

        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup()
        group.set_title("¿Cómo quieres enseñar esta habilidad?")
        group.set_description(
            "Elige el método que mejor se adapte a la tarea."
        )
        page.add(group)

        # Card: Composio integration branch.
        composio_row = Adw.ActionRow()
        composio_row.set_title("Conectar una integración")
        composio_row.set_subtitle(
            "Para tareas con apps que ya tienes conectadas: correo, calendario, Drive…"
        )
        composio_row.set_activatable(True)
        composio_icon = Gtk.Image.new_from_icon_name("network-transmit-receive-symbolic")
        composio_icon.set_pixel_size(20)
        composio_row.add_prefix(composio_icon)
        composio_chevron = Gtk.Image.new_from_icon_name("go-next-symbolic")
        composio_chevron.set_pixel_size(16)
        composio_row.add_suffix(composio_chevron)
        composio_row.connect("activated", lambda _r: self._choose_composio())
        group.add(composio_row)

        # Card: screen recording branch.
        recording_row = Adw.ActionRow()
        recording_row.set_title("Grabar en pantalla")
        recording_row.set_subtitle(
            "Para tareas donde demuestras los pasos navegando un sitio o app."
        )
        recording_row.set_activatable(True)
        rec_icon = Gtk.Image.new_from_icon_name("media-record-symbolic")
        rec_icon.set_pixel_size(20)
        recording_row.add_prefix(rec_icon)
        rec_chevron = Gtk.Image.new_from_icon_name("go-next-symbolic")
        rec_chevron.set_pixel_size(16)
        recording_row.add_suffix(rec_chevron)
        recording_row.connect("activated", lambda _r: self._choose_recording())
        group.add(recording_row)

        toolbar.set_content(page)
        self.set_content(toolbar)

    def _choose_composio(self) -> None:
        self.close()
        self._on_composio()

    def _choose_recording(self) -> None:
        self.close()
        self._on_recording()


# ---------------------------------------------------------------------------
# Composio teach dialog — name + integration picker + intent text.
# ---------------------------------------------------------------------------


class _TeachComposioDialog(Adw.Window):
    """Modal form: skill_name + connected integration + intent → create_composio_skill."""

    def __init__(
        self,
        *,
        parent: Gtk.Window,
        client: ShellBackendClient,
        on_saved: Callable[[], None],
    ) -> None:
        super().__init__()
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_title("Habilidad con integración")
        self.set_default_size(520, 440)
        self._client = client
        self._on_saved = on_saved

        # Stash connected accounts as list of dicts; populated asynchronously.
        self._accounts: list[dict] = []

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)

        cancel_btn = Gtk.Button.new_with_label("Cancelar")
        cancel_btn.connect("clicked", lambda _b: self.close())
        header.pack_start(cancel_btn)

        self._save_btn = Gtk.Button.new_with_label("Guardar habilidad")
        self._save_btn.add_css_class("hermes-primary")
        self._save_btn.set_sensitive(False)
        self._save_btn.connect("clicked", lambda _b: self._save())
        header.pack_end(self._save_btn)

        # Outer scroll so the form works at any window height.
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)

        page = Adw.PreferencesPage()

        # Group 1 — name + integration picker.
        self._form_group = Adw.PreferencesGroup()
        self._form_group.set_title("Nueva habilidad")
        self._form_group.set_description(
            "Define qué debe hacer Hermes usando una app que ya tienes conectada."
        )

        self._name_row = Adw.EntryRow()
        self._name_row.set_title("Nombre de la habilidad")
        self._name_row.connect("changed", lambda _r: self._refresh_save_btn())
        self._form_group.add(self._name_row)

        # Integration picker — populated after the async load completes.
        self._picker_row = Adw.ComboRow()
        self._picker_row.set_title("Integración")
        self._picker_row.set_subtitle("Cargando integraciones conectadas…")
        self._form_group.add(self._picker_row)

        page.add(self._form_group)

        # Group 2 — intent text (multi-line Gtk.TextView in a framed row).
        intent_group = Adw.PreferencesGroup()
        intent_group.set_title("Instrucción")
        intent_group.set_description("Describe qué debe hacer Hermes con esta integración.")
        page.add(intent_group)

        # Gtk.TextView wrapped in a frame to visually match the row style.
        intent_frame = Gtk.Frame()
        intent_frame.add_css_class("hermes-composio-intent-frame")
        intent_frame.set_margin_start(4)
        intent_frame.set_margin_end(4)
        intent_frame.set_margin_top(4)
        intent_frame.set_margin_bottom(4)

        self._intent_view = Gtk.TextView()
        self._intent_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._intent_view.set_accepts_tab(False)
        self._intent_view.set_pixels_above_lines(8)
        self._intent_view.set_pixels_below_lines(8)
        self._intent_view.set_left_margin(12)
        self._intent_view.set_right_margin(12)
        self._intent_view.set_size_request(-1, 120)
        self._intent_view.get_buffer().connect("changed", lambda _b: self._refresh_save_btn())

        # GTK4 has no built-in placeholder for TextView — render manually.
        self._intent_placeholder = Gtk.Label(
            label='Ej. "Cada mañana, resume los correos sin leer y envíame un resumen"'
        )
        self._intent_placeholder.set_xalign(0)
        self._intent_placeholder.set_yalign(0)
        self._intent_placeholder.add_css_class("hermes-composio-intent-placeholder")
        self._intent_placeholder.set_margin_start(14)
        self._intent_placeholder.set_margin_top(10)

        intent_overlay = Gtk.Overlay()
        intent_overlay.set_child(self._intent_view)
        intent_overlay.add_overlay(self._intent_placeholder)

        intent_frame.set_child(intent_overlay)
        intent_group.add(intent_frame)

        # Inline error label — hidden until an error occurs.
        self._error_label = Gtk.Label()
        self._error_label.add_css_class("hermes-composio-error")
        self._error_label.set_xalign(0)
        self._error_label.set_wrap(True)
        self._error_label.set_visible(False)
        self._error_label.set_margin_start(24)
        self._error_label.set_margin_end(24)
        self._error_label.set_margin_top(8)
        self._error_label.set_margin_bottom(4)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        outer.append(self._error_label)
        outer.append(page)
        scroll.set_child(outer)

        toolbar.set_content(scroll)
        self.set_content(toolbar)

        # Placeholder visibility: hide when user starts typing.
        self._intent_view.get_buffer().connect(
            "changed", lambda _b: self._update_intent_placeholder()
        )

        # Load connected accounts asynchronously.
        self._load_accounts()

    # ------------------------------------------------------------------
    # Async account loading
    # ------------------------------------------------------------------

    def _load_accounts(self) -> None:
        def runner() -> None:
            try:
                accounts = self._client.composio_connected()
            except Exception as exc:  # noqa: BLE001
                logger.warning("composio_connected (teach dialog): %s", exc)
                accounts = []
            GLib.idle_add(lambda: self._on_accounts_loaded(accounts))

        threading.Thread(
            target=runner, daemon=True, name="hermes-teach-composio-accounts"
        ).start()

    def _on_accounts_loaded(self, accounts: list[dict]) -> bool:
        self._accounts = [a for a in accounts if a.get("status") == "ACTIVE"]

        if not self._accounts:
            self._picker_row.set_subtitle(
                "Sin integraciones conectadas — ve a Integraciones para conectar una app"
            )
            self._picker_row.set_sensitive(False)
            return False

        string_list = Gtk.StringList()
        for acc in self._accounts:
            string_list.append(_connected_account_display_name(acc))

        self._picker_row.set_model(string_list)
        self._picker_row.set_subtitle("")
        self._picker_row.set_sensitive(True)
        self._refresh_save_btn()
        return False

    # ------------------------------------------------------------------
    # UI helpers
    # ------------------------------------------------------------------

    def _refresh_save_btn(self) -> None:
        name_ok = bool(self._name_row.get_text().strip())
        intent_ok = bool(self._intent_text().strip())
        accounts_ok = bool(self._accounts)
        self._save_btn.set_sensitive(name_ok and intent_ok and accounts_ok)

    def _update_intent_placeholder(self) -> None:
        buf = self._intent_view.get_buffer()
        empty = buf.get_start_iter().equal(buf.get_end_iter())
        self._intent_placeholder.set_visible(empty)

    def _intent_text(self) -> str:
        buf = self._intent_view.get_buffer()
        return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)

    def _selected_account(self) -> dict | None:
        if not self._accounts:
            return None
        idx = self._picker_row.get_selected()
        if idx < 0 or idx >= len(self._accounts):
            return None
        return self._accounts[idx]

    def _show_error(self, msg: str) -> None:
        self._error_label.set_text(msg)
        self._error_label.set_visible(True)

    # ------------------------------------------------------------------
    # Save action
    # ------------------------------------------------------------------

    def _save(self) -> None:
        skill_name = self._name_row.get_text().strip()
        intent_text = self._intent_text().strip()
        account = self._selected_account()

        if not skill_name or not intent_text or account is None:
            return

        toolkit_slug = account.get("toolkit_slug") or account.get("app_name") or ""

        self._save_btn.set_sensitive(False)
        self._save_btn.set_label("Guardando…")
        self._error_label.set_visible(False)

        def runner() -> None:
            try:
                self._client.create_composio_skill(
                    skill_name=skill_name,
                    toolkit_slug=toolkit_slug,
                    intent_text=intent_text,
                )
                GLib.idle_add(self._on_save_success)
            except Exception as exc:  # noqa: BLE001
                msg = f"No se pudo guardar la habilidad: {exc}"
                logger.warning("create_composio_skill: %s", exc)
                GLib.idle_add(lambda m=msg: self._on_save_error(m))

        threading.Thread(
            target=runner, daemon=True, name="hermes-teach-composio-save"
        ).start()

    def _on_save_success(self) -> bool:
        self._on_saved()
        self.close()
        return False

    def _on_save_error(self, msg: str) -> bool:
        self._save_btn.set_sensitive(True)
        self._save_btn.set_label("Guardar habilidad")
        self._show_error(msg)
        return False
