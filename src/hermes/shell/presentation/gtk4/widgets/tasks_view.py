"""HermesTasksView — Dashboard de tareas programadas del agente.

Presenta dos secciones:
  1. Tareas configuradas (configured tasks) — triggers con recurrencia, estado
     del último ciclo, y próxima ejecución prevista.
  2. Actividad reciente (recent tasks) — últimas ejecuciones de cualquier origen.

Threading contract (idéntico a skills_view.py / integrations_view.py):
    Todas las llamadas HTTP corren en daemon threads.
    Los resultados cruzan al main loop GTK via GLib.idle_add.
    NUNCA tocar widgets desde el thread HTTP.

Auto-refresh: GLib.timeout_add_seconds cada 5 s; se cancela al desmontar la
vista (señal "unmap") para evitar fugas de callback.
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from hermes.shell.infrastructure.shell_backend_client import (
    ShellBackendClient,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Status badge mapping
# ---------------------------------------------------------------------------

# Maps last_status values to CSS class suffix and human label.
_STATUS_INFO: dict[str | None, tuple[str, str]] = {
    "completed":        ("success",  "Completado"),
    "failed":           ("error",    "Error"),
    "rejected":         ("error",    "Rechazado"),
    "in_progress":      ("running",  "En curso"),
    "pending":          ("neutral",  "Pendiente"),
    "pending_approval": ("neutral",  "Pendiente aprobación"),
    None:               ("neutral",  "Sin datos"),
}

# Risk ceiling display labels.
_RISK_LABELS: dict[str, str] = {
    "low":      "Riesgo bajo",
    "medium":   "Riesgo medio",
    "high":     "Riesgo alto",
    "critical": "Riesgo crítico",
}


# ---------------------------------------------------------------------------
# Datetime formatting helpers (pure, no GTK dependency — unit-testable)
# ---------------------------------------------------------------------------

def _fmt_datetime(iso: str | None) -> str:
    """Return a short human-readable form of an ISO-8601 datetime string.

    Returns "—" for None or empty.  Never raises.
    """
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        now = datetime.now(tz=UTC)
        delta = now - dt
        if delta.total_seconds() < 0:
            # Future timestamp — show short date+time.
            return dt.strftime("%Y-%m-%d %H:%M")
        if delta.days == 0:
            hour_min = dt.strftime("%H:%M")
            return f"hoy {hour_min}"
        if delta.days == 1:
            return f"ayer {dt.strftime('%H:%M')}"
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return iso or "—"


def _fmt_next_run(iso: str | None) -> str:
    """Format next_run_at for display; 'Próxima: …' prefix is added by the row."""
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        now = datetime.now(tz=UTC)
        delta = dt - now
        if delta.total_seconds() < 0:
            return dt.strftime("%Y-%m-%d %H:%M")
        if delta.days == 0:
            hours, rem = divmod(int(delta.total_seconds()), 3600)
            minutes = rem // 60
            if hours > 0:
                return f"en {hours}h {minutes}min"
            return f"en {minutes}min"
        if delta.days == 1:
            return f"mañana {dt.strftime('%H:%M')}"
        return dt.strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return iso or "—"


# ---------------------------------------------------------------------------
# Main widget
# ---------------------------------------------------------------------------


class HermesTasksView(Gtk.Box):
    """Vista de tareas — configuradas + actividad reciente."""

    def __init__(self, *, client: ShellBackendClient) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._client = client
        self._auto_refresh_source: int | None = None
        self.set_hexpand(True)
        self.set_vexpand(True)

        # Toast overlay wraps the full view so toasts render above the list.
        self._toast_overlay = Adw.ToastOverlay()
        self._toast_overlay.set_vexpand(True)
        self._toast_overlay.set_hexpand(True)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        inner.set_hexpand(True)
        inner.set_vexpand(True)
        inner.append(self._build_toolbar())

        # Content stack: loading | disconnected | content
        self._content_stack = Gtk.Stack()
        self._content_stack.set_vexpand(True)
        self._content_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._content_stack.set_transition_duration(160)

        self._content_stack.add_named(self._build_loading_page(), "loading")
        self._content_stack.add_named(self._build_disconnected_page(), "disconnected")
        self._content_stack.add_named(self._build_content_page(), "content")

        inner.append(self._content_stack)
        self._toast_overlay.set_child(inner)
        self.append(self._toast_overlay)

        # Cancel auto-refresh timer when widget is hidden/destroyed.
        self.connect("unmap", self._on_unmap)

        self._reload()
        self._start_auto_refresh()

    # ------------------------------------------------------------------
    # Toolbar
    # ------------------------------------------------------------------

    def _build_toolbar(self) -> Gtk.Widget:
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        toolbar.add_css_class("hermes-view-toolbar")

        title = Gtk.Label(label="Tareas")
        title.add_css_class("hermes-page-title")
        title.set_xalign(0)
        title.set_hexpand(True)
        toolbar.append(title)

        refresh_btn = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        refresh_btn.add_css_class("flat")
        refresh_btn.set_tooltip_text("Refrescar ahora")
        refresh_btn.connect("clicked", lambda _b: self._reload())
        toolbar.append(refresh_btn)

        return toolbar

    # ------------------------------------------------------------------
    # Static pages (loading / disconnected)
    # ------------------------------------------------------------------

    def _build_loading_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_halign(Gtk.Align.CENTER)
        box.set_valign(Gtk.Align.CENTER)
        box.set_vexpand(True)

        spinner = Gtk.Spinner()
        spinner.set_size_request(36, 36)
        spinner.start()
        box.append(spinner)

        lbl = Gtk.Label(label="Cargando tareas…")
        lbl.add_css_class("hermes-wizard-form-subheading")
        box.append(lbl)

        return box

    def _build_disconnected_page(self) -> Gtk.Widget:
        status = Adw.StatusPage()
        status.set_icon_name("network-offline-symbolic")
        status.set_title("Runtime no disponible")
        status.set_description(
            "El daemon del agente no está activo.\n"
            "Las tareas se mostrarán en cuanto se restablezca la conexión."
        )
        status.set_vexpand(True)

        retry_btn = Gtk.Button.new_with_label("Reintentar")
        retry_btn.add_css_class("hermes-ghost")
        retry_btn.set_halign(Gtk.Align.CENTER)
        retry_btn.connect("clicked", lambda _b: self._reload())
        status.set_child(retry_btn)

        return status

    # ------------------------------------------------------------------
    # Scrollable content page
    # ------------------------------------------------------------------

    def _build_content_page(self) -> Gtk.Widget:
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(820)
        clamp.set_margin_top(16)
        clamp.set_margin_bottom(24)
        clamp.set_margin_start(24)
        clamp.set_margin_end(24)

        self._content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)

        # Section A — configured tasks.
        self._configured_group = Adw.PreferencesGroup()
        self._configured_group.set_title("Tareas configuradas")
        self._configured_group.set_description(
            "Tareas que el agente ejecuta de forma autónoma según su programación"
        )
        self._content_box.append(self._configured_group)
        self._configured_rows: list[Gtk.Widget] = []

        # Section B — recent activity.
        self._recent_group = Adw.PreferencesGroup()
        self._recent_group.set_title("Actividad reciente")
        self._recent_group.set_description("Últimas ejecuciones del agente")
        self._content_box.append(self._recent_group)
        self._recent_rows: list[Gtk.Widget] = []

        clamp.set_child(self._content_box)
        scroll.set_child(clamp)
        return scroll

    # ------------------------------------------------------------------
    # Reload — fire two parallel HTTP calls in a single daemon thread
    # ------------------------------------------------------------------

    def _reload(self) -> None:
        self._content_stack.set_visible_child_name("loading")

        def runner() -> None:
            configured_result: dict = {}
            recent_result: dict = {}

            try:
                configured_result = self._client.list_configured_tasks()
            except Exception as exc:  # noqa: BLE001
                logger.warning("tasks/configured: %s", exc)
                configured_result = {"available": False, "tasks": []}

            try:
                recent_result = self._client.list_recent_tasks()
            except Exception as exc:  # noqa: BLE001
                logger.warning("tasks/recent: %s", exc)
                recent_result = {"available": False, "tasks": []}

            GLib.idle_add(lambda: self._render(configured_result, recent_result))

        threading.Thread(target=runner, daemon=True, name="hermes-tasks-reload").start()

    def _render(self, configured: dict, recent: dict) -> bool:
        # If either endpoint says available=False, the daemon is down.
        if not configured.get("available", False) and not recent.get("available", False):
            self._content_stack.set_visible_child_name("disconnected")
            return False

        self._render_configured(configured.get("tasks", []))
        self._render_recent(recent.get("tasks", []))
        self._content_stack.set_visible_child_name("content")
        return False

    # ------------------------------------------------------------------
    # Section A — configured tasks
    # ------------------------------------------------------------------

    def _render_configured(self, tasks: list[dict]) -> None:
        for row in self._configured_rows:
            self._configured_group.remove(row)
        self._configured_rows = []

        if not tasks:
            row = Adw.ActionRow()
            row.set_title("No hay tareas configuradas todavía")
            row.set_subtitle(
                "Las tareas configuradas aparecerán aquí con su programación y estado"
            )
            self._configured_group.add(row)
            self._configured_rows.append(row)
            return

        for task in tasks:
            row = self._build_configured_row(task)
            self._configured_group.add(row)
            self._configured_rows.append(row)

    def _build_configured_row(self, task: dict) -> Gtk.Widget:
        row = Adw.ExpanderRow()
        row.set_title(task.get("label") or "(sin nombre)")

        recurrence = task.get("recurrence") or ""
        last_run_at = task.get("last_run_at")
        last_status = task.get("last_status")
        next_run_at = task.get("next_run_at")
        enabled = task.get("enabled", True)
        risk_ceiling = task.get("risk_ceiling") or ""
        trigger_type = task.get("trigger_type") or ""

        # Subtitle: recurrence or trigger type.
        subtitle_parts = []
        if recurrence:
            subtitle_parts.append(recurrence)
        elif trigger_type:
            subtitle_parts.append(trigger_type)
        if not enabled:
            subtitle_parts.append("desactivada")
        row.set_subtitle("  ·  ".join(subtitle_parts) if subtitle_parts else "—")

        # Status badge (prefix area).
        badge = self._build_status_badge(last_status)
        row.add_prefix(badge)

        # Enabled switch (display-only — no endpoint yet).
        # TODO: wire to PATCH /api/v1/tasks/configured/{id}/enabled when endpoint lands.
        sw = Gtk.Switch()
        sw.set_active(enabled)
        sw.set_sensitive(False)
        sw.set_valign(Gtk.Align.CENTER)
        sw.set_tooltip_text(
            "Activada" if enabled else "Desactivada — (activación no disponible aún)"
        )
        row.add_suffix(sw)

        # Risk ceiling tag.
        if risk_ceiling:
            risk_lbl = Gtk.Label.new(_RISK_LABELS.get(risk_ceiling, risk_ceiling))
            risk_lbl.add_css_class("hermes-task-risk-tag")
            risk_lbl.set_valign(Gtk.Align.CENTER)
            row.add_suffix(risk_lbl)

        # Expanded detail rows — last run and next run.
        detail_last = Adw.ActionRow()
        detail_last.set_title("Última ejecución")
        last_run_fmt = _fmt_datetime(last_run_at) if last_run_at else "Nunca"
        _, status_label = _STATUS_INFO.get(last_status, _STATUS_INFO[None])
        detail_last.set_subtitle(
            f"{last_run_fmt}  ·  {status_label}" if last_run_at else "Nunca"
        )
        row.add_row(detail_last)

        detail_next = Adw.ActionRow()
        detail_next.set_title("Próxima ejecución prevista")
        detail_next.set_subtitle(_fmt_next_run(next_run_at))
        row.add_row(detail_next)

        return row

    # ------------------------------------------------------------------
    # Section B — recent tasks
    # ------------------------------------------------------------------

    def _render_recent(self, tasks: list[dict]) -> None:
        for row in self._recent_rows:
            self._recent_group.remove(row)
        self._recent_rows = []

        if not tasks:
            row = Adw.ActionRow()
            row.set_title("Sin actividad reciente")
            row.set_subtitle("Las ejecuciones del agente aparecerán aquí")
            self._recent_group.add(row)
            self._recent_rows.append(row)
            return

        for task in tasks:
            row = self._build_recent_row(task)
            self._recent_group.add(row)
            self._recent_rows.append(row)

    def _build_recent_row(self, task: dict) -> Gtk.Widget:
        row = Adw.ActionRow()
        row.set_title(task.get("label") or task.get("task_id") or "(sin nombre)")

        status = task.get("status")
        trigger_kind = task.get("trigger_kind") or ""
        enqueued_at = task.get("enqueued_at")
        claimed_at = task.get("claimed_at")

        enqueued_fmt = _fmt_datetime(enqueued_at)
        claimed_fmt = _fmt_datetime(claimed_at) if claimed_at else None

        subtitle_parts = []
        if trigger_kind:
            subtitle_parts.append(trigger_kind)
        subtitle_parts.append(f"iniciada {enqueued_fmt}")
        if claimed_fmt:
            subtitle_parts.append(f"tomada {claimed_fmt}")
        row.set_subtitle("  ·  ".join(subtitle_parts))

        badge = self._build_status_badge(status)
        row.add_prefix(badge)

        return row

    # ------------------------------------------------------------------
    # Status badge widget builder
    # ------------------------------------------------------------------

    def _build_status_badge(self, status: str | None) -> Gtk.Widget:
        css_modifier, label_text = _STATUS_INFO.get(status, _STATUS_INFO[None])

        if status == "in_progress":
            # Live spinner for in-progress tasks.
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            box.set_valign(Gtk.Align.CENTER)
            spinner = Gtk.Spinner()
            spinner.add_css_class("hermes-task-status-spinner")
            spinner.start()
            box.append(spinner)
            lbl = Gtk.Label.new(label_text)
            lbl.add_css_class(f"hermes-task-status-{css_modifier}")
            box.append(lbl)
            return box

        icon_name = {
            "success": "emblem-ok-symbolic",
            "error":   "dialog-warning-symbolic",
            "neutral": "emblem-default-symbolic",
            "running": "media-playback-start-symbolic",
        }.get(css_modifier, "emblem-default-symbolic")

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_valign(Gtk.Align.CENTER)

        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.add_css_class(f"hermes-task-status-icon-{css_modifier}")
        icon.set_pixel_size(14)
        box.append(icon)

        lbl = Gtk.Label.new(label_text)
        lbl.add_css_class(f"hermes-task-status-{css_modifier}")
        box.append(lbl)

        return box

    # ------------------------------------------------------------------
    # Auto-refresh
    # ------------------------------------------------------------------

    def _start_auto_refresh(self) -> None:
        _AUTO_REFRESH_SECONDS = 5

        def _tick() -> bool:
            self._reload()
            return True  # GLib.SOURCE_CONTINUE

        self._auto_refresh_source = GLib.timeout_add_seconds(
            _AUTO_REFRESH_SECONDS, _tick
        )

    def _on_unmap(self, _widget) -> None:
        if self._auto_refresh_source is not None:
            GLib.source_remove(self._auto_refresh_source)
            self._auto_refresh_source = None

    # ------------------------------------------------------------------
    # Toast helper
    # ------------------------------------------------------------------

    def _show_toast(self, msg: str) -> bool:
        toast = Adw.Toast.new(msg)
        toast.set_timeout(4)
        self._toast_overlay.add_toast(toast)
        return False
