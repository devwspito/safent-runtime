"""HermesSetupWizardView — slim 4-screen OOBE wizard (US1, spec 011).

Replaces the 10-step form wizard with a macOS-calm 4-screen flow:
    0  Bienvenida  — static splash; wizard_form_start() fires on __init__
    1  Idioma      — locale / timezone / keyboard; applied live; saved in memory
    2  Tu cuenta   — username + password (+ Advanced expander: profile + tenant)
    3  Listo       — fires the complete backend state-machine in background,
                     then emits wizard-finished

Backend state-machine order (FIXED — never reorder):
    set_profile → set_locale → set_network → set_tenant
    → set_consents → review_services → finalize

Calls that run independently (not part of the state machine):
    set_account  — called on leaving "Tu cuenta" screen
    wizard_form_start — called on __init__ to obtain session_id

Threading contract (unchanged from predecessor):
    All HTTP calls run in daemon threads.
    Results cross back to the GTK main loop via GLib.idle_add.

GObject signal:
    wizard-finished  (no args)  — emitted after finalize succeeds.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, GObject, Gtk  # noqa: E402

if TYPE_CHECKING:
    from hermes.shell.infrastructure.shell_backend_client import ShellBackendClient

logger = logging.getLogger(__name__)


def _prefers_reduced_motion() -> bool:
    """Return True when the system gtk-enable-animations setting is off."""
    try:
        settings = Gtk.Settings.get_default()
        if settings is None:
            return False
        return bool(settings.get_property("gtk-enable-animations") is False)
    except Exception:  # noqa: BLE001
        return False


# ------------------------------------------------------------------
# Constants
# ------------------------------------------------------------------

_MIN_PASSWORD_LEN = 8

# Profile kinds understood by the backend wizard_set_profile endpoint.
_PROFILE_KINDS: list[tuple[str, str]] = [
    ("personal_desktop", "Escritorio personal"),
    ("workspace_only", "Espacio de trabajo"),
    ("server", "Servidor sin interfaz"),
]

_LANGUAGES: list[tuple[str, str, str]] = [
    ("es", "Español", "Europe/Madrid"),
    ("en", "English", "UTC"),
    ("en-US", "English (US)", "America/New_York"),
    ("en-GB", "English (UK)", "Europe/London"),
    ("fr", "Français", "Europe/Paris"),
    ("de", "Deutsch", "Europe/Berlin"),
    ("pt", "Português", "Europe/Lisbon"),
    ("pt-BR", "Português (Brasil)", "America/Sao_Paulo"),
    ("zh", "中文", "Asia/Shanghai"),
    ("ja", "日本語", "Asia/Tokyo"),
]

_TIMEZONES: list[str] = [
    "Europe/Madrid",
    "Europe/London",
    "Europe/Paris",
    "Europe/Berlin",
    "Europe/Lisbon",
    "UTC",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/Sao_Paulo",
    "America/Mexico_City",
    "Asia/Shanghai",
    "Asia/Tokyo",
    "Asia/Seoul",
    "Asia/Kolkata",
    "Australia/Sydney",
    "Pacific/Auckland",
]

# (visible label, XKB identifier)
_KEYBOARD_LAYOUTS: list[tuple[str, str]] = [
    ("Español", "es"),
    ("Español (Mac)", "es+mac"),
    ("Español (Latinoamérica)", "latam"),
    ("English (US)", "us"),
    ("English (US, internacional)", "us+intl"),
    ("English (UK)", "gb"),
    ("Français", "fr"),
    ("Français (Mac)", "fr+mac"),
    ("Deutsch", "de"),
    ("Italiano", "it"),
    ("Português", "pt"),
    ("Português (Brasil)", "br"),
]

_LANG_TO_KEYBOARD_XKB: dict[str, str] = {
    "es": "es",
    "en": "us",
    "en-US": "us",
    "en-GB": "gb",
    "fr": "fr",
    "de": "de",
    "pt": "pt",
    "pt-BR": "br",
}

# Icons per screen (symbolic, tinted accent by the theme).
_SCREEN_ICONS: dict[str, str] = {
    "welcome": "computer-symbolic",
    "locale": "preferences-desktop-locale-symbolic",
    "account": "system-users-symbolic",
    "done": "emblem-ok-symbolic",
}

# Captions shown while the finish pipeline executes (one per step).
_PIPELINE_CAPTIONS: list[str] = [
    "Aplicando perfil…",
    "Aplicando idioma…",
    "Comprobando red…",
    "Vinculando organización…",
    "Configurando permisos…",
    "Revisando servicios…",
    "Finalizando…",
]

# Pages in order — used to drive stack navigation and dots.
_PAGES: list[str] = ["welcome", "locale", "account", "done"]

# Screens that contribute a visible dot (excludes welcome splash).
_DOT_PAGES: list[str] = ["locale", "account", "done"]


# ------------------------------------------------------------------
# Main wizard view
# ------------------------------------------------------------------


class HermesSetupWizardView(Gtk.Box):
    """4-screen slim OOBE wizard for Agents OS (spec 011, US1).

    Emits ``wizard-finished`` after wizard_form_finalize succeeds or
    best-effort completes (backend errors are surfaced as toasts but
    never block the user from reaching the desktop).
    """

    __gsignals__ = {
        "wizard-finished": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    def __init__(self, *, client: "ShellBackendClient") -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.add_css_class("hermes-wizard-root")
        self.set_hexpand(True)
        self.set_vexpand(True)

        self._client = client
        self._session_id: str | None = None
        self._in_flight = False
        self._finished = False  # one-shot guard para wizard-finished
        self._welcome_entrance_done = False  # stagger fires once only

        # Locale selections — stored in memory, sent all at once in "Listo".
        self._locale_lang_code: str = "es"
        self._locale_tz: str = "Europe/Madrid"
        self._locale_keyboard_xkb: str = "es"

        # Advanced options (inside "Tu cuenta" expander).
        self._profile_kind: str = "personal_desktop"
        self._tenant_bind: bool = False
        self._tenant_url: str = ""
        self._tenant_token: str = ""

        self._build_layout()

        # Start backend session eagerly — the result arrives while the user
        # reads the welcome screen (never blocks navigation).
        self._start_session()

    # ------------------------------------------------------------------
    # Layout scaffold
    # ------------------------------------------------------------------

    def _build_layout(self) -> None:
        # Brand mark — discrete, caption-strong secondary, top-left.
        self.append(self._build_brand_bar())

        # Toast overlay wraps the stack so toasts render above every screen.
        self._toast_overlay = Adw.ToastOverlay()
        self._toast_overlay.set_vexpand(True)

        self._stack = Gtk.Stack()
        if _prefers_reduced_motion():
            self._stack.set_transition_type(Gtk.StackTransitionType.NONE)
            self._stack.set_transition_duration(0)
        else:
            self._stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
            self._stack.set_transition_duration(280)
        self._stack.set_vexpand(True)
        self._stack.set_hexpand(True)

        self._build_screen_welcome()
        self._build_screen_locale()
        self._build_screen_account()
        self._build_screen_done()

        self._toast_overlay.set_child(self._stack)
        self.append(self._toast_overlay)

        # Footer: transparent, no border — Back / Continue buttons + dots.
        self.append(self._build_footer())

        # Start on welcome.
        self._go_to_screen("welcome")

    def _build_brand_bar(self) -> Gtk.Widget:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.set_margin_start(32)
        bar.set_margin_top(20)
        bar.set_margin_bottom(0)

        # Accent glyph mark — small rounded square in accent-subtle.
        glyph_container = Gtk.Box()
        glyph_container.set_size_request(22, 22)
        glyph_container.set_valign(Gtk.Align.CENTER)
        glyph_container.add_css_class("hermes-oobe-brand-glyph")

        glyph_icon = Gtk.Image.new_from_icon_name("emblem-system-symbolic")
        glyph_icon.set_pixel_size(13)
        glyph_icon.set_halign(Gtk.Align.CENTER)
        glyph_icon.set_valign(Gtk.Align.CENTER)
        glyph_container.append(glyph_icon)

        bar.append(glyph_container)

        # Wordmark — 15px / weight 600 / text_primary.
        wordmark = Gtk.Label(label="Agents OS")
        wordmark.add_css_class("hermes-oobe-brand-wordmark")
        wordmark.set_valign(Gtk.Align.CENTER)
        bar.append(wordmark)

        return bar

    def _build_footer(self) -> Gtk.Widget:
        footer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        footer.add_css_class("hermes-oobe-footer")

        # Dots row (3 dots for locale/account/done).
        self._dots_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._dots_row.set_halign(Gtk.Align.CENTER)
        self._dots_row.set_margin_top(12)
        self._dots_row.set_margin_bottom(12)
        self._dot_widgets: dict[str, Gtk.Widget] = {}
        for page in _DOT_PAGES:
            dot = Gtk.Box()
            dot.set_size_request(8, 8)
            dot.add_css_class("hermes-oobe-dot")
            self._dots_row.append(dot)
            self._dot_widgets[page] = dot
        footer.append(self._dots_row)

        # Navigation buttons row.
        nav = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        nav.set_margin_start(32)
        nav.set_margin_end(32)
        nav.set_margin_bottom(28)

        self._back_btn = Gtk.Button.new_with_label("Atrás")
        self._back_btn.add_css_class("hermes-ghost")
        self._back_btn.connect("clicked", self._on_back_clicked)
        nav.append(self._back_btn)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        nav.append(spacer)

        self._next_btn = Gtk.Button()
        self._next_btn.add_css_class("hermes-primary")
        self._next_btn.connect("clicked", self._on_next_clicked)
        nav.append(self._next_btn)

        footer.append(nav)
        return footer

    # ------------------------------------------------------------------
    # Screen builders
    # ------------------------------------------------------------------

    def _make_oobe_clamp(self) -> tuple[Gtk.ScrolledWindow, Adw.Clamp, Gtk.Box]:
        """Return (scroll, clamp, content_box) added to the stack.

        Layout: ScrolledWindow > Overlay > (radial-glow DrawingArea + Clamp).
        The DrawingArea is non-targetable so it never steals pointer/keyboard
        events. The content box is anchored to the upper-third (valign START,
        margin_top 64) so it never floats in dead centre.
        """
        scroll = Gtk.ScrolledWindow()
        scroll.set_hexpand(True)
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        # Overlay: glow behind, content in front.
        overlay = Gtk.Overlay()
        overlay.set_hexpand(True)
        overlay.set_vexpand(True)

        # Radial accent glow (~600px radius, upper-third, ~6% alpha).
        glow = Gtk.DrawingArea()
        glow.set_hexpand(True)
        glow.set_vexpand(True)
        glow.set_can_target(False)
        glow.set_can_focus(False)
        glow.set_draw_func(self._draw_oobe_glow)
        overlay.set_child(glow)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(560)
        clamp.set_vexpand(True)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        content.set_valign(Gtk.Align.START)
        content.set_margin_start(24)
        content.set_margin_end(24)
        content.set_margin_top(64)
        content.set_margin_bottom(48)

        clamp.set_child(content)
        overlay.add_overlay(clamp)
        scroll.set_child(overlay)
        return scroll, clamp, content

    def _draw_oobe_glow(
        self, area: Gtk.DrawingArea, cr: object, width: int, height: int
    ) -> None:
        """Paint a faint radial accent glow centred on the upper third."""
        if _prefers_reduced_motion():
            return
        try:
            import cairo  # type: ignore[import]

            cx = width / 2
            cy = height * 0.28
            radius = min(width, height) * 0.75
            grad = cairo.RadialGradient(cx, cy, 0, cx, cy, radius)  # type: ignore[attr-defined]
            # ~6% accent alpha at centre, transparent at edge.
            grad.add_color_stop_rgba(0.0, 0.039, 0.518, 1.0, 0.06)
            grad.add_color_stop_rgba(1.0, 0.039, 0.518, 1.0, 0.0)
            cr.set_source(grad)  # type: ignore[attr-defined]
            cr.paint()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — cairo optional; silently skip
            pass

    def _make_hero_icon(self, icon_name: str) -> Gtk.Widget:
        """80px hero icon in an accent-subtle circle (Tier 2: grown from 72px)."""
        container = Gtk.Box()
        container.set_size_request(80, 80)
        container.set_halign(Gtk.Align.CENTER)
        container.set_valign(Gtk.Align.CENTER)
        container.add_css_class("hermes-oobe-hero-icon")

        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_pixel_size(40)
        icon.set_halign(Gtk.Align.CENTER)
        icon.set_valign(Gtk.Align.CENTER)
        container.append(icon)

        return container

    # Screen 0 — Bienvenida
    def _build_screen_welcome(self) -> None:
        scroll, _clamp, content = self._make_oobe_clamp()
        self._stack.add_named(scroll, "welcome")
        content.set_spacing(0)

        hero = self._make_hero_icon(_SCREEN_ICONS["welcome"])
        content.append(hero)

        title = Gtk.Label(label="Bienvenido a Agents OS")
        title.add_css_class("hermes-type-display")
        title.add_css_class("hermes-oobe-heading")
        title.set_wrap(True)
        title.set_justify(Gtk.Justification.CENTER)
        title.set_halign(Gtk.Align.CENTER)
        title.set_margin_top(24)
        content.append(title)

        subtitle = Gtk.Label(
            label="Tu asistente personal vive aquí, en tu equipo. Nada sale sin tu permiso."
        )
        subtitle.add_css_class("hermes-oobe-subtitle")
        subtitle.set_wrap(True)
        subtitle.set_justify(Gtk.Justification.CENTER)
        subtitle.set_halign(Gtk.Align.CENTER)
        subtitle.set_margin_top(12)
        subtitle.set_margin_bottom(0)
        content.append(subtitle)

        # Entrance stagger — first paint only, gated behind reduced-motion.
        self._schedule_welcome_entrance([hero, title, subtitle])

    # Screen 1 — Idioma
    def _build_screen_locale(self) -> None:
        scroll, _clamp, content = self._make_oobe_clamp()
        self._stack.add_named(scroll, "locale")
        content.set_spacing(0)

        content.append(self._make_hero_icon(_SCREEN_ICONS["locale"]))

        title = Gtk.Label(label="Idioma y región")
        title.add_css_class("hermes-type-title-1")
        title.add_css_class("hermes-oobe-heading")
        title.set_wrap(True)
        title.set_justify(Gtk.Justification.CENTER)
        title.set_halign(Gtk.Align.CENTER)
        title.set_margin_top(24)
        content.append(title)

        group = Adw.PreferencesGroup()
        group.set_margin_top(28)

        # Language dropdown.
        lang_labels = [entry[1] for entry in _LANGUAGES]
        self._lang_combo = Gtk.DropDown.new_from_strings(lang_labels)
        self._lang_combo.set_selected(0)
        self._lang_combo.set_valign(Gtk.Align.CENTER)
        self._lang_combo.connect("notify::selected", self._on_locale_lang_changed)

        lang_row = Adw.ActionRow()
        lang_row.set_title("Idioma")
        lang_row.add_suffix(self._lang_combo)
        lang_row.set_activatable_widget(self._lang_combo)
        group.add(lang_row)

        # Timezone dropdown — auto-updated on language change.
        self._tz_combo = Gtk.DropDown.new_from_strings(_TIMEZONES)
        self._tz_combo.set_selected(0)
        self._tz_combo.set_valign(Gtk.Align.CENTER)
        self._tz_combo.connect("notify::selected", self._on_tz_changed)

        tz_row = Adw.ActionRow()
        tz_row.set_title("Zona horaria")
        tz_row.add_suffix(self._tz_combo)
        tz_row.set_activatable_widget(self._tz_combo)
        group.add(tz_row)

        # Keyboard layout — applied in-session immediately via GSettings.
        kb_labels = [entry[0] for entry in _KEYBOARD_LAYOUTS]
        self._kb_combo = Gtk.DropDown.new_from_strings(kb_labels)
        self._kb_combo.set_selected(0)
        self._kb_combo.set_valign(Gtk.Align.CENTER)
        self._kb_combo.connect("notify::selected", self._on_keyboard_layout_changed)

        kb_row = Adw.ActionRow()
        kb_row.set_title("Distribución de teclado")
        kb_row.set_subtitle("Se aplica ahora en tu sesión")
        kb_row.add_suffix(self._kb_combo)
        kb_row.set_activatable_widget(self._kb_combo)
        group.add(kb_row)

        content.append(group)

    # Screen 2 — Tu cuenta
    def _build_screen_account(self) -> None:
        scroll, _clamp, content = self._make_oobe_clamp()
        self._stack.add_named(scroll, "account")
        content.set_spacing(0)

        content.append(self._make_hero_icon(_SCREEN_ICONS["account"]))

        title = Gtk.Label(label="Crea tu cuenta")
        title.add_css_class("hermes-type-title-1")
        title.add_css_class("hermes-oobe-heading")
        title.set_wrap(True)
        title.set_justify(Gtk.Justification.CENTER)
        title.set_halign(Gtk.Align.CENTER)
        title.set_margin_top(24)
        content.append(title)

        subtitle = Gtk.Label(
            label="Solo tú podrás acceder a este equipo y a todo lo que configures."
        )
        subtitle.add_css_class("hermes-oobe-subtitle")
        subtitle.set_wrap(True)
        subtitle.set_justify(Gtk.Justification.CENTER)
        subtitle.set_halign(Gtk.Align.CENTER)
        subtitle.set_margin_top(12)
        subtitle.set_margin_bottom(0)
        content.append(subtitle)

        # Main credentials group.
        creds_group = Adw.PreferencesGroup()
        creds_group.set_margin_top(24)

        self._account_username_entry = Adw.EntryRow()
        self._account_username_entry.set_title("Nombre de usuario")
        self._account_username_entry.set_input_hints(Gtk.InputHints.LOWERCASE)
        self._account_username_entry.connect(
            "changed", lambda _r: self._validate_account_form()
        )
        creds_group.add(self._account_username_entry)

        self._account_password_entry = Adw.PasswordEntryRow()
        self._account_password_entry.set_title("Contraseña")
        self._account_password_entry.connect(
            "changed", lambda _r: self._validate_account_form()
        )
        creds_group.add(self._account_password_entry)

        self._account_confirm_entry = Adw.PasswordEntryRow()
        self._account_confirm_entry.set_title("Repite la contraseña")
        self._account_confirm_entry.connect(
            "changed", lambda _r: self._validate_account_form()
        )
        creds_group.add(self._account_confirm_entry)

        content.append(creds_group)

        # Inline validation error — hidden until the user types something invalid.
        self._account_error_label = Gtk.Label(label="")
        self._account_error_label.set_xalign(0)
        self._account_error_label.set_wrap(True)
        self._account_error_label.add_css_class("hermes-wizard-form-status-error")
        self._account_error_label.set_visible(False)
        self._account_error_label.set_margin_top(6)
        self._account_error_label.set_margin_start(4)
        content.append(self._account_error_label)

        # Advanced options — collapsed by default, zero visual weight when closed.
        advanced_group = Adw.PreferencesGroup()
        advanced_group.set_margin_top(16)

        self._advanced_expander = Adw.ExpanderRow()
        self._advanced_expander.set_title("Opciones avanzadas")
        self._advanced_expander.set_expanded(False)

        # Profile kind dropdown inside advanced.
        profile_labels = [p[1] for p in _PROFILE_KINDS]
        self._profile_combo = Gtk.DropDown.new_from_strings(profile_labels)
        self._profile_combo.set_selected(0)  # personal_desktop default
        self._profile_combo.set_valign(Gtk.Align.CENTER)
        self._profile_combo.connect("notify::selected", self._on_profile_changed)

        profile_row = Adw.ActionRow()
        profile_row.set_title("Perfil del sistema")
        profile_row.set_subtitle("Define las capacidades activas")
        profile_row.add_suffix(self._profile_combo)
        profile_row.set_activatable_widget(self._profile_combo)
        self._advanced_expander.add_row(profile_row)

        # Tenant binding switch + URL + token inside advanced.
        self._tenant_switch = Adw.SwitchRow()
        self._tenant_switch.set_title("Vínculo con organización")
        self._tenant_switch.set_subtitle("Conecta este equipo a un servidor de empresa")
        self._tenant_switch.set_active(False)
        self._tenant_switch.connect("notify::active", self._on_tenant_switch_changed)
        self._advanced_expander.add_row(self._tenant_switch)

        self._tenant_url_entry = Adw.EntryRow()
        self._tenant_url_entry.set_title("URL del servidor")
        self._tenant_url_entry.set_input_purpose(Gtk.InputPurpose.URL)
        self._tenant_url_entry.set_sensitive(False)
        self._advanced_expander.add_row(self._tenant_url_entry)

        self._tenant_token_entry = Adw.PasswordEntryRow()
        self._tenant_token_entry.set_title("Token de enrolamiento")
        self._tenant_token_entry.set_sensitive(False)
        self._advanced_expander.add_row(self._tenant_token_entry)

        advanced_group.add(self._advanced_expander)
        content.append(advanced_group)

    # Screen 3 — Listo
    def _build_screen_done(self) -> None:
        scroll, _clamp, content = self._make_oobe_clamp()
        self._stack.add_named(scroll, "done")
        content.set_spacing(0)

        # Loading state — spinner + determinate progress bar + caption.
        self._done_loading_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=16
        )
        self._done_loading_box.set_halign(Gtk.Align.CENTER)
        self._done_loading_box.set_valign(Gtk.Align.CENTER)
        self._done_loading_box.set_vexpand(True)

        loading_spinner = Gtk.Spinner()
        loading_spinner.set_spinning(True)
        loading_spinner.set_size_request(48, 48)
        loading_spinner.set_halign(Gtk.Align.CENTER)
        self._done_loading_box.append(loading_spinner)

        self._done_progress_bar = Gtk.ProgressBar()
        self._done_progress_bar.set_show_text(False)
        self._done_progress_bar.set_fraction(0.0)
        self._done_progress_bar.set_size_request(220, -1)
        self._done_progress_bar.set_halign(Gtk.Align.CENTER)
        self._done_loading_box.append(self._done_progress_bar)

        self._done_caption_label = Gtk.Label(label="Preparando tu entorno…")
        self._done_caption_label.add_css_class("hermes-oobe-subtitle")
        self._done_caption_label.set_halign(Gtk.Align.CENTER)
        self._done_loading_box.append(self._done_caption_label)

        content.append(self._done_loading_box)

        # Success state — checkmark + title.
        self._done_success_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=0
        )
        self._done_success_box.set_halign(Gtk.Align.CENTER)
        self._done_success_box.set_hexpand(True)
        self._done_success_box.set_visible(False)

        self._done_success_box.append(self._make_hero_icon(_SCREEN_ICONS["done"]))

        done_title = Gtk.Label(label="Todo listo")
        done_title.add_css_class("hermes-type-title-1")
        done_title.add_css_class("hermes-oobe-heading")
        done_title.set_halign(Gtk.Align.CENTER)
        done_title.set_justify(Gtk.Justification.CENTER)
        done_title.set_margin_top(24)
        self._done_success_box.append(done_title)

        done_body = Gtk.Label(
            label="Tu asistente está configurado y listo para trabajar contigo."
            " Empieza con algo sencillo."
        )
        done_body.add_css_class("hermes-oobe-subtitle")
        done_body.set_wrap(True)
        done_body.set_justify(Gtk.Justification.CENTER)
        done_body.set_halign(Gtk.Align.CENTER)
        done_body.set_margin_top(12)
        done_body.set_margin_bottom(0)
        self._done_success_box.append(done_body)

        # "Ir al escritorio" button — shown in the success box too for clarity.
        enter_btn = Gtk.Button(label="Ir al escritorio")
        enter_btn.add_css_class("hermes-primary")
        enter_btn.set_halign(Gtk.Align.CENTER)
        enter_btn.set_margin_top(32)
        enter_btn.connect("clicked", lambda _b: self._finish_once())
        self._done_success_box.append(enter_btn)

        content.append(self._done_success_box)

    # ------------------------------------------------------------------
    # Entrance stagger animation (welcome screen, first paint only)
    # ------------------------------------------------------------------

    def _schedule_welcome_entrance(self, widgets: list[Gtk.Widget]) -> None:
        """Stagger-fade hero→title→subtitle into view on first render.

        Each widget starts opacity 0 via the hermes-oobe-enter-hidden CSS class,
        then receives hermes-oobe-enter-visible after an increasing delay (~60ms
        steps). The CSS transition does the actual fade+rise. All gated behind
        _prefers_reduced_motion().
        """
        if _prefers_reduced_motion():
            return

        for w in widgets:
            w.add_css_class("hermes-oobe-enter-hidden")

        def _reveal(idx: int) -> bool:
            if idx < len(widgets):
                widgets[idx].remove_css_class("hermes-oobe-enter-hidden")
                widgets[idx].add_css_class("hermes-oobe-enter-visible")
            return False  # one-shot

        for i, _w in enumerate(widgets):
            delay_ms = 80 + i * 60
            GLib.timeout_add(delay_ms, _reveal, i)

    # ------------------------------------------------------------------
    # Locale event handlers (live apply + in-memory save)
    # ------------------------------------------------------------------

    def _on_locale_lang_changed(self, *_args: object) -> None:
        idx = self._lang_combo.get_selected()
        if idx < 0 or idx >= len(_LANGUAGES):
            return
        lang_code, _label, default_tz = _LANGUAGES[idx]
        self._locale_lang_code = lang_code

        # Auto-update timezone to the language default.
        if default_tz in _TIMEZONES:
            self._tz_combo.set_selected(_TIMEZONES.index(default_tz))

        # Auto-update keyboard to the language default.
        xkb = _LANG_TO_KEYBOARD_XKB.get(lang_code, _KEYBOARD_LAYOUTS[0][1])
        kb_xkb_values = [entry[1] for entry in _KEYBOARD_LAYOUTS]
        if xkb in kb_xkb_values:
            self._kb_combo.set_selected(kb_xkb_values.index(xkb))

    def _on_tz_changed(self, *_args: object) -> None:
        idx = self._tz_combo.get_selected()
        if 0 <= idx < len(_TIMEZONES):
            self._locale_tz = _TIMEZONES[idx]

    def _on_keyboard_layout_changed(self, *_args: object) -> None:
        idx = self._kb_combo.get_selected()
        if idx < 0 or idx >= len(_KEYBOARD_LAYOUTS):
            return
        _label, xkb = _KEYBOARD_LAYOUTS[idx]
        self._locale_keyboard_xkb = xkb
        # Apply immediately so the user can verify the layout as they type.
        self._apply_keyboard_layout(xkb)

    def _apply_keyboard_layout(self, xkb: str) -> None:
        """Write the chosen XKB layout to GSettings (per-user, no sudo needed)."""
        try:
            settings = Gio.Settings.new("org.gnome.desktop.input-sources")
            value = GLib.Variant("a(ss)", [("xkb", xkb)])
            settings.set_value("sources", value)
            settings.set_uint("current", 0)
        except Exception:  # noqa: BLE001
            logger.warning("could not apply keyboard layout '%s'", xkb, exc_info=True)

    # ------------------------------------------------------------------
    # Advanced options event handlers
    # ------------------------------------------------------------------

    def _on_profile_changed(self, *_args: object) -> None:
        idx = self._profile_combo.get_selected()
        if 0 <= idx < len(_PROFILE_KINDS):
            self._profile_kind = _PROFILE_KINDS[idx][0]

    def _on_tenant_switch_changed(self, *_args: object) -> None:
        enabled = self._tenant_switch.get_active()
        self._tenant_url_entry.set_sensitive(enabled)
        self._tenant_token_entry.set_sensitive(enabled)

    # ------------------------------------------------------------------
    # Account form validation
    # ------------------------------------------------------------------

    def _validate_account_form(self) -> bool:
        """Validate account fields and update Continue button + error label.

        Returns True when the form is valid.
        """
        username = self._account_username_entry.get_text().strip()
        password = self._account_password_entry.get_text()
        confirm = self._account_confirm_entry.get_text()

        error = ""
        if not username:
            error = "Elige un nombre de usuario para continuar."
        elif not all(c.isalnum() or c in "-_" for c in username):
            error = "Solo letras minúsculas, números y guiones. Sin espacios."
        elif len(password) < _MIN_PASSWORD_LEN:
            error = "La contraseña debe tener al menos 8 caracteres."
        elif password != confirm:
            error = "Las contraseñas no coinciden. Compruébalas."

        has_input = bool(username or password or confirm)
        if error and has_input:
            self._account_error_label.set_text(error)
            self._account_error_label.set_visible(True)
        else:
            self._account_error_label.set_visible(False)

        if self._stack.get_visible_child_name() == "account":
            valid = not error
            self._next_btn.set_sensitive(valid and not self._in_flight)

        return not error

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def _current_page(self) -> str:
        return self._stack.get_visible_child_name() or "welcome"

    def _current_index(self) -> int:
        try:
            return _PAGES.index(self._current_page())
        except ValueError:
            return 0

    def _go_to_screen(self, page: str) -> None:
        self._stack.set_visible_child_name(page)
        self._update_dots(page)
        self._update_footer_buttons(page)

        if page == "account":
            self._validate_account_form()

    def _update_dots(self, current_page: str) -> None:
        current_idx = _PAGES.index(current_page) if current_page in _PAGES else 0
        for dot_page, dot_widget in self._dot_widgets.items():
            dot_page_idx = _PAGES.index(dot_page)
            dot_widget.remove_css_class("hermes-oobe-dot-active")
            dot_widget.remove_css_class("hermes-oobe-dot-done")
            if dot_page_idx < current_idx:
                dot_widget.add_css_class("hermes-oobe-dot-done")
            elif dot_page == current_page:
                dot_widget.add_css_class("hermes-oobe-dot-active")

    def _update_footer_buttons(self, page: str) -> None:
        is_welcome = page == "welcome"
        is_done = page == "done"

        self._back_btn.set_visible(not is_welcome and not is_done)
        self._next_btn.set_visible(not is_done)

        # Button labels.
        if is_welcome:
            self._next_btn.set_label("Comenzar")
        elif page == "account":
            self._next_btn.set_label("Crear cuenta")
        else:
            self._next_btn.set_label("Continuar")

        # Sensitivity.
        self._back_btn.set_sensitive(not self._in_flight)
        if page == "account":
            self._next_btn.set_sensitive(
                self._validate_account_form() and not self._in_flight
            )
        else:
            self._next_btn.set_sensitive(not self._in_flight)

    def _on_back_clicked(self, _btn: Gtk.Button) -> None:
        idx = self._current_index()
        if idx > 0:
            self._go_to_screen(_PAGES[idx - 1])

    def _on_next_clicked(self, _btn: Gtk.Button) -> None:
        page = self._current_page()
        if page == "welcome":
            self._go_to_screen("locale")
        elif page == "locale":
            # Save locale values from the widgets into instance state.
            # wizard_set_locale is NOT called here — it fires in "Listo".
            self._snapshot_locale_selections()
            self._go_to_screen("account")
        elif page == "account":
            self._submit_account()
        # "done" has no next button.

    def _snapshot_locale_selections(self) -> None:
        """Read current dropdown values into instance fields before leaving locale."""
        idx_lang = self._lang_combo.get_selected()
        if 0 <= idx_lang < len(_LANGUAGES):
            self._locale_lang_code = _LANGUAGES[idx_lang][0]

        idx_tz = self._tz_combo.get_selected()
        if 0 <= idx_tz < len(_TIMEZONES):
            self._locale_tz = _TIMEZONES[idx_tz]

        idx_kb = self._kb_combo.get_selected()
        if 0 <= idx_kb < len(_KEYBOARD_LAYOUTS):
            self._locale_keyboard_xkb = _KEYBOARD_LAYOUTS[idx_kb][1]

    # ------------------------------------------------------------------
    # set_account — independent call (not part of state machine)
    # ------------------------------------------------------------------

    def _submit_account(self) -> None:
        if not self._validate_account_form():
            return

        username = self._account_username_entry.get_text().strip()
        password = self._account_password_entry.get_text()

        # Read advanced fields now (before navigating away).
        self._tenant_bind = self._tenant_switch.get_active()
        self._tenant_url = self._tenant_url_entry.get_text().strip()
        self._tenant_token = self._tenant_token_entry.get_text()

        self._set_loading(True)
        threading.Thread(
            target=self._thread_set_account,
            args=(username, password),
            daemon=True,
            name="hermes-wizard-set-account",
        ).start()

    def _thread_set_account(self, username: str, password: str) -> None:
        try:
            self._client.set_account(username, password)
        except Exception as exc:  # noqa: BLE001 — best-effort; advance regardless
            logger.warning("set_account backend error (advancing): %s", exc)
        GLib.idle_add(self._on_account_ok)

    def _on_account_ok(self) -> bool:
        self._set_loading(False)
        self._go_to_screen("done")
        # Kick off the full backend state machine immediately.
        self._run_finish_pipeline()
        return False

    # ------------------------------------------------------------------
    # "Listo" — fire the complete state machine in background.
    #
    # WHY all calls happen here rather than spread across steps:
    # The backend wizard form has a fixed state-machine order
    # (profile → locale → network → tenant → consents → services → finalize).
    # Deferring all calls to a single background sequence in "Listo" lets us:
    #   1. Keep screens clean (no per-screen spinner for state-machine calls).
    #   2. Guarantee the correct call order regardless of how fast the user
    #      moves through screens.
    #   3. Treat every step as best-effort without blocking navigation.
    # ------------------------------------------------------------------

    def _run_finish_pipeline(self) -> None:
        if self._session_id is None:
            # No session — show success immediately (offline/unregistered node).
            GLib.idle_add(self._on_pipeline_done)
            return

        threading.Thread(
            target=self._thread_finish_pipeline,
            args=(self._session_id,),
            daemon=True,
            name="hermes-wizard-finish-pipeline",
        ).start()

    def _thread_finish_pipeline(self, session_id: str) -> None:
        """Execute the full wizard state machine; report per-step progress to the GTK loop."""
        errors: list[str] = []
        total = len(_PIPELINE_CAPTIONS)

        def _report(step: int) -> None:
            """Cross to the GTK main loop to update fraction + caption."""
            GLib.idle_add(self._on_pipeline_step, step, total)

        # 1. set_profile
        _report(1)
        try:
            self._client.wizard_set_profile(
                session_id=session_id,
                profile_kind=self._profile_kind,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"perfil: {exc}")
            logger.warning("wizard set_profile error (continuing): %s", exc)

        # 2. set_locale
        _report(2)
        try:
            self._client.wizard_set_locale(
                session_id=session_id,
                language_code=self._locale_lang_code,
                keyboard_layout=self._locale_keyboard_xkb,
                timezone=self._locale_tz,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"idioma: {exc}")
            logger.warning("wizard set_locale error (continuing): %s", exc)

        # 3. set_network — always "connected" (network screen removed from rail;
        #    offline_continue is available via Settings post-boot if needed).
        _report(3)
        try:
            self._client.wizard_set_network(
                session_id=session_id,
                decision="connected",
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"red: {exc}")
            logger.warning("wizard set_network error (continuing): %s", exc)

        # 4. set_tenant — "bind_now" if user enabled the org switch, else "defer".
        _report(4)
        tenant_decision = "bind_now" if self._tenant_bind else "defer"
        try:
            self._client.wizard_set_tenant(
                session_id=session_id,
                decision=tenant_decision,
                tenant_endpoint_url=self._tenant_url or None,
                enrollment_token=self._tenant_token or None,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"organización: {exc}")
            logger.warning("wizard set_tenant error (continuing): %s", exc)

        # 5. set_consents — empty list (personal_desktop only; just-in-time
        #    consent happens via HITL broker after first boot).
        _report(5)
        try:
            self._client.wizard_set_consents(
                session_id=session_id,
                granted=[],
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"permisos: {exc}")
            logger.warning("wizard set_consents error (continuing): %s", exc)

        # 6. review_services — auto-acknowledged; user sees exposed services
        #    in the audit view post-boot, not during OOBE.
        _report(6)
        try:
            self._client.wizard_review_services(
                session_id=session_id,
                acknowledged=True,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(f"servicios: {exc}")
            logger.warning("wizard review_services error (continuing): %s", exc)

        # 7. finalize — emits wizard-finished on success.
        _report(7)
        try:
            self._client.wizard_form_finalize(session_id=session_id)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"finalizar: {exc}")
            logger.warning("wizard finalize error (completing anyway): %s", exc)

        GLib.idle_add(self._on_pipeline_done, errors)

    def _on_pipeline_done(self, errors: list[str] | None = None) -> bool:
        self._done_progress_bar.set_fraction(1.0)

        if errors:
            self._show_toast(
                "Algunos ajustes se aplicarán al iniciar. "
                "Puedes revisarlos en Ajustes."
            )

        # Transition done screen from loading → success.
        self._done_loading_box.set_visible(False)
        self._done_success_box.set_visible(True)

        # Auto-advance after 1.8 s — the user can also click the button in
        # the success box if they want to proceed immediately.
        GLib.timeout_add(1800, self._finish_once)
        return False

    # ------------------------------------------------------------------
    # Determinate progress (replaces blind pulse)
    # ------------------------------------------------------------------

    def _on_pipeline_step(self, step: int, total: int) -> bool:
        """Update the progress bar fraction and caption for a pipeline step.

        Called from the GTK main loop via GLib.idle_add — safe to touch widgets.
        """
        fraction = step / total
        self._done_progress_bar.set_fraction(fraction)
        if 0 < step <= len(_PIPELINE_CAPTIONS):
            self._done_caption_label.set_text(_PIPELINE_CAPTIONS[step - 1])
        return False

    # ------------------------------------------------------------------
    # Backend session start (wizard_form_start — independent call)
    # ------------------------------------------------------------------

    def _start_session(self) -> None:
        threading.Thread(
            target=self._thread_start_session,
            daemon=True,
            name="hermes-setup-wizard-start",
        ).start()

    def _thread_start_session(self) -> None:
        try:
            data = self._client.wizard_form_start()
            GLib.idle_add(self._on_session_started, data)
        except Exception as exc:  # noqa: BLE001
            GLib.idle_add(self._on_session_error, str(exc))

    def _on_session_started(self, data: dict) -> bool:
        self._session_id = data.get("session_id")
        return False

    def _on_session_error(self, error: str) -> bool:
        logger.warning(
            "wizard_form_start failed: %s — continuing without session", error
        )
        self._show_toast(
            "No se pudo conectar con el servidor. "
            "Algunos ajustes pueden no guardarse."
        )
        return False

    # ------------------------------------------------------------------
    # Loading state
    # ------------------------------------------------------------------

    def _set_loading(self, loading: bool) -> None:
        self._in_flight = loading
        page = self._current_page()
        self._back_btn.set_sensitive(not loading)
        if loading:
            self._next_btn.set_label("…")
            self._next_btn.set_sensitive(False)
        else:
            self._update_footer_buttons(page)

    # ------------------------------------------------------------------
    # Toast helper
    # ------------------------------------------------------------------

    def _show_toast(self, msg: str) -> None:
        toast = Adw.Toast.new(msg)
        toast.set_timeout(4)
        self._toast_overlay.add_toast(toast)

    # ------------------------------------------------------------------
    # Finish
    # ------------------------------------------------------------------

    def _finish_once(self) -> bool:
        """Emite wizard-finished UNA sola vez (auto-advance 1.8s o botón manual).

        Sin el guard, hacer click en "Ir al escritorio" dentro de la ventana de
        1.8s emitía la señal dos veces → segunda construcción de la shell.
        """
        if self._finished:
            return False
        self._finished = True
        self.emit("wizard-finished")
        return False
