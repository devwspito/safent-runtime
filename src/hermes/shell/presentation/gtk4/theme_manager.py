"""ThemeManager — carga y swap de temas claro/oscuro/auto + acento en caliente.

Arquitectura de providers GTK:
  1. provider_tokens (PRIORITY_USER)  ← tokens-light.css o tokens-dark.css (swap)
  2. provider_components (PRIORITY_USER) ← components.css (permanente)
  3. provider_accent (PRIORITY_USER + 1) ← CSS de acento generado en memoria

Los tres se registran en el mismo Gdk.Display. El provider_accent tiene la
prioridad más alta de los tres para que el acento siempre gane en cascada.

Trampas importantes (ver visual-system.md §2.4):
  - Los pins de libadwaita (window_bg_color, etc.) se replican en CADA tokens-*.css
    porque libadwaita los resuelve por provider activo, no por cascada CSS. Si
    tokens-dark.css no los tiene, los popovers/dialogs quedan blancos en dark.
  - El ancho de lectura se gestiona con Adw.Clamp en Python, nunca con max-width CSS.
  - Estado del tema almacenado en atributos Python (PyGObject eliminó set_data/get_data).
"""

from __future__ import annotations

import colorsys
import json
import logging
import os
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

ThemeMode = Literal["light", "dark", "auto"]

# 8 presets de acento (§1.3 del visual-system). El valor "on-accent" para
# Amarillo y Verde es #1D1D1F porque el contraste de #FFFFFF sobre esos
# colores claros no supera AA (4.5:1).
ACCENT_PRESETS: dict[str, tuple[str, str]] = {
    "Índigo":   ("#6E56CF", "#FFFFFF"),
    "Azul":     ("#0A84FF", "#FFFFFF"),
    "Morado":   ("#5E5CE6", "#FFFFFF"),
    "Rosa":     ("#FF375F", "#FFFFFF"),
    "Rojo":     ("#FF453A", "#FFFFFF"),
    "Naranja":  ("#FF9F0A", "#FFFFFF"),
    "Amarillo": ("#FFD60A", "#1D1D1F"),
    "Verde":    ("#30D158", "#1D1D1F"),
    "Grafito":  ("#8E8E93", "#FFFFFF"),
}

_DEFAULT_MODE: ThemeMode = "auto"
_DEFAULT_ACCENT = "Índigo"

# Nombre del archivo de persistencia dentro del directorio de config del SO.
_PERSIST_FILENAME = "hermes-theme.json"


def _config_dir() -> Path:
    """Retorna el directorio de config del shell, creándolo si no existe."""
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    d = Path(base) / "hermes-shell"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _hex_to_hsl(hex_color: str) -> tuple[float, float, float]:
    """Convierte #RRGGBB a (h, s, l) en rango 0-1."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16) / 255, int(h[2:4], 16) / 255, int(h[4:6], 16) / 255
    return colorsys.rgb_to_hls(r, g, b)[0], colorsys.rgb_to_hls(r, g, b)[2], colorsys.rgb_to_hls(r, g, b)[1]


def _hsl_to_hex(h: float, s: float, l: float) -> str:
    """Convierte (h, s, l) en rango 0-1 a #RRGGBB."""
    r, g, b = colorsys.hls_to_rgb(h, l, s)
    return "#{:02X}{:02X}{:02X}".format(int(r * 255), int(g * 255), int(b * 255))


def _derive_hover_press(accent_hex: str) -> tuple[str, str]:
    """Deriva hover (-8% lum) y press (-16% lum) desde el acento base en HSL."""
    h, s, l = _hex_to_hsl(accent_hex)
    hover = _hsl_to_hex(h, s, max(0.0, l - 0.08))
    press = _hsl_to_hex(h, s, max(0.0, l - 0.16))
    return hover, press


def _build_accent_css(accent_hex: str, on_accent_hex: str) -> bytes:
    """Genera el bloque CSS de las 6 variables de acento a partir del color base.

    El CSS resultante se carga vía load_from_data a la prioridad más alta,
    sobreescribiendo los fallback de tokens-light/dark.css.
    """
    hover, press = _derive_hover_press(accent_hex)
    css = f"""
@define-color hermes_accent        {accent_hex};
@define-color hermes_accent_hover  {hover};
@define-color hermes_accent_press  {press};
@define-color hermes_accent_subtle alpha({accent_hex}, 0.12);
@define-color hermes_accent_ring   alpha({accent_hex}, 0.28);
@define-color hermes_on_accent     {on_accent_hex};
@define-color accent_bg_color      {accent_hex};
@define-color accent_fg_color      {on_accent_hex};
@define-color accent_color         {accent_hex};
"""
    return css.encode("utf-8")


def _load_css_resource(filename: str) -> bytes:
    """Carga un archivo CSS desde el paquete (importlib.resources o fallback disco)."""
    try:
        from importlib import resources  # noqa: PLC0415

        return (
            resources.files("hermes.shell.presentation.gtk4")
            .joinpath("css", filename)
            .read_bytes()
        )
    except Exception:  # noqa: BLE001
        path = Path(__file__).parent / "css" / filename
        return path.read_bytes()


class ThemeManager:
    """Gestiona los 3 providers CSS del sistema de temas Sereno.

    Uso:
        mgr = ThemeManager(display)
        mgr.apply_theme("auto")
        mgr.apply_accent("Azul")

    El display se obtiene de Gdk.Display.get_default() en app.py justo
    después de que GTK haya inicializado el display (dentro de do_activate).
    """

    def __init__(self, display) -> None:
        # Importación lazy para no romper imports en entornos sin GTK.
        import gi  # noqa: PLC0415

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gtk  # noqa: PLC0415

        self._Gtk = Gtk
        self._Adw = Adw
        self._display = display

        self._provider_tokens: Gtk.CssProvider | None = None
        self._provider_components: Gtk.CssProvider | None = None
        self._provider_accent: Gtk.CssProvider | None = None

        self._mode: ThemeMode = _DEFAULT_MODE
        self._accent_name: str = _DEFAULT_ACCENT

        # Registrar listener del StyleManager para modo "auto".
        self._style_mgr = Adw.StyleManager.get_default()
        self._dark_handler_id: int | None = None

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------

    def bootstrap(self) -> None:
        """Carga la configuración persistida y aplica el tema inicial.

        Debe llamarse una vez, dentro de do_activate (display disponible).
        """
        self._load_components_provider()
        self._restore_or_defaults()

    def apply_theme(self, mode: ThemeMode) -> None:
        """Cambia el tema a "light", "dark" o "auto" en caliente."""
        self._mode = mode
        self._apply_mode(mode)
        self._persist()

    def apply_accent(self, preset_name: str) -> None:
        """Cambia el color de acento en caliente. preset_name debe estar en ACCENT_PRESETS."""
        if preset_name not in ACCENT_PRESETS:
            logger.warning("accent preset desconocido: %s — usando Azul", preset_name)
            preset_name = _DEFAULT_ACCENT
        self._accent_name = preset_name
        self._apply_accent_provider(preset_name)
        self._persist()

    # ------------------------------------------------------------------
    # Implementación interna
    # ------------------------------------------------------------------

    def _load_components_provider(self) -> None:
        """Carga components.css una sola vez (permanente, no se intercambia)."""
        data = _load_css_resource("components.css")
        provider = self._Gtk.CssProvider()
        provider.load_from_data(data)
        self._Gtk.StyleContext.add_provider_for_display(
            self._display,
            provider,
            self._Gtk.STYLE_PROVIDER_PRIORITY_USER,
        )
        self._provider_components = provider
        logger.info("components.css cargado (%d bytes)", len(data))

    def _apply_mode(self, mode: ThemeMode) -> None:
        """Fuerza Adwaita-dark siempre. El modo guardado se conserva pero no
        cambia el esquema de color — la shell es dark-only."""
        # Desconectar listener previo si existía.
        if self._dark_handler_id is not None:
            self._style_mgr.disconnect(self._dark_handler_id)
            self._dark_handler_id = None

        # FORCE_DARK: Adwaita-dark es el sustrato visual. No se negocia con
        # el tema del sistema. Los tokens hermes_* se cargan siempre desde
        # tokens-dark.css para que las referencias @hermes_* en components.css
        # resuelvan correctamente.
        self._style_mgr.set_color_scheme(self._Adw.ColorScheme.FORCE_DARK)
        self._swap_tokens("dark")

    def _on_system_dark_changed(self, _style_mgr, _param) -> None:
        """Callback de Adw.StyleManager::notify::dark para el modo "auto"."""
        is_dark = self._style_mgr.get_dark()
        logger.debug("system dark changed: %s", is_dark)
        self._swap_tokens("dark" if is_dark else "light")

    def _swap_tokens(self, variant: Literal["light", "dark"]) -> None:
        """Intercambia el provider de tokens activo por el de la variante dada."""
        filename = f"tokens-{variant}.css"
        data = _load_css_resource(filename)

        # Eliminar el provider anterior del display antes de añadir el nuevo.
        # GTK no tiene un API de "replace", así que quitamos + añadimos.
        if self._provider_tokens is not None:
            self._Gtk.StyleContext.remove_provider_for_display(
                self._display,
                self._provider_tokens,
            )

        provider = self._Gtk.CssProvider()
        provider.load_from_data(data)
        self._Gtk.StyleContext.add_provider_for_display(
            self._display,
            provider,
            self._Gtk.STYLE_PROVIDER_PRIORITY_USER,
        )
        self._provider_tokens = provider
        logger.info("tokens swapped → %s (%d bytes)", variant, len(data))

    def _apply_accent_provider(self, preset_name: str) -> None:
        """Genera el CSS de acento en memoria y lo aplica al display."""
        accent_hex, on_accent_hex = ACCENT_PRESETS[preset_name]
        data = _build_accent_css(accent_hex, on_accent_hex)

        if self._provider_accent is not None:
            self._Gtk.StyleContext.remove_provider_for_display(
                self._display,
                self._provider_accent,
            )

        provider = self._Gtk.CssProvider()
        provider.load_from_data(data)
        # PRIORITY_USER + 1 para que el acento gane sobre los tokens del tema.
        self._Gtk.StyleContext.add_provider_for_display(
            self._display,
            provider,
            self._Gtk.STYLE_PROVIDER_PRIORITY_USER + 1,
        )
        self._provider_accent = provider
        logger.info("accent aplicado: %s (%s)", preset_name, accent_hex)

    # ------------------------------------------------------------------
    # Persistencia — JSON en XDG_CONFIG_HOME/hermes-shell/
    # ------------------------------------------------------------------

    def _persist(self) -> None:
        path = _config_dir() / _PERSIST_FILENAME
        try:
            path.write_text(
                json.dumps({"mode": self._mode, "accent": self._accent_name}),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("no se pudo persistir el tema: %s", exc)

    def _restore_or_defaults(self) -> None:
        """Lee la configuración guardada y aplica; si no existe, usa defaults."""
        path = _config_dir() / _PERSIST_FILENAME
        mode: ThemeMode = _DEFAULT_MODE
        accent: str = _DEFAULT_ACCENT
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("mode") in ("light", "dark", "auto"):
                    mode = data["mode"]  # type: ignore[assignment]
                if data.get("accent") in ACCENT_PRESETS:
                    accent = data["accent"]
            except Exception as exc:  # noqa: BLE001
                logger.warning("config de tema corrupta, usando defaults: %s", exc)
        self._mode = mode
        self._accent_name = accent
        self._apply_mode(mode)
        self._apply_accent_provider(accent)
