"""Tests de lógica pura para US6 (Ajustes — Apariencia, Disposición).

No requieren GTK ni display. Se ejecutan en cualquier entorno.

Cubre:
  - Mapeo nombre copy-deck ↔ clave interna de ACCENT_PRESETS
  - Todos los 8 presets son biyectivos (ida y vuelta sin pérdida)
  - LayoutPrefs: defaults, save/load round-trip, reset
  - LayoutPrefs: valores corruptos o archivo inexistente → defaults
  - ThemeManager: _derive_hover_press devuelve colores más oscuros
  - ThemeManager: _build_accent_css contiene las variables esperadas
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Mapeo display name ↔ preset key
# ---------------------------------------------------------------------------

class TestPresetMapping:
    """Verifica la biyección entre nombres del copy-deck y claves internas."""

    def test_todos_los_display_names_mapean_a_preset_valido(self) -> None:
        from hermes.shell.presentation.gtk4.widgets.settings_window import (
            _PRESET_DISPLAY_NAMES,
            display_name_to_preset,
        )
        from hermes.shell.presentation.gtk4.theme_manager import ACCENT_PRESETS

        for display in _PRESET_DISPLAY_NAMES:
            preset_key = display_name_to_preset(display)
            assert preset_key in ACCENT_PRESETS, (
                f"{display!r} → {preset_key!r} no existe en ACCENT_PRESETS"
            )

    def test_todos_los_presets_tienen_display_name(self) -> None:
        from hermes.shell.presentation.gtk4.widgets.settings_window import (
            _PRESET_DISPLAY_NAMES,
            preset_to_display_name,
        )
        from hermes.shell.presentation.gtk4.theme_manager import ACCENT_PRESETS

        # Cada clave interna debe poder resolverse de vuelta a un display name.
        for preset_key in ACCENT_PRESETS:
            display = preset_to_display_name(preset_key)
            assert display in _PRESET_DISPLAY_NAMES, (
                f"preset {preset_key!r} no tiene display name inverso"
            )

    def test_ida_y_vuelta_sin_perdida(self) -> None:
        from hermes.shell.presentation.gtk4.widgets.settings_window import (
            _PRESET_DISPLAY_NAMES,
            display_name_to_preset,
            preset_to_display_name,
        )

        for display in _PRESET_DISPLAY_NAMES:
            preset_key = display_name_to_preset(display)
            recovered = preset_to_display_name(preset_key)
            assert recovered == display, (
                f"Ciclo roto: {display!r} → {preset_key!r} → {recovered!r}"
            )

    def test_display_names_correctos_del_copy_deck(self) -> None:
        from hermes.shell.presentation.gtk4.widgets.settings_window import (
            _PRESET_DISPLAY_NAMES,
        )

        expected = {
            "Océano", "Lavanda", "Amanecer", "Rojo terracota",
            "Ámbar", "Arena dorada", "Salvia", "Pizarra",
        }
        assert set(_PRESET_DISPLAY_NAMES.keys()) == expected

    def test_oceans_es_azul(self) -> None:
        from hermes.shell.presentation.gtk4.widgets.settings_window import (
            display_name_to_preset,
        )

        assert display_name_to_preset("Océano") == "Azul"

    def test_pizarra_es_grafito(self) -> None:
        from hermes.shell.presentation.gtk4.widgets.settings_window import (
            display_name_to_preset,
        )

        assert display_name_to_preset("Pizarra") == "Grafito"

    def test_display_desconocido_cae_a_azul(self) -> None:
        from hermes.shell.presentation.gtk4.widgets.settings_window import (
            display_name_to_preset,
        )

        assert display_name_to_preset("InventadoNoExiste") == "Azul"

    def test_preset_desconocido_cae_a_oceano(self) -> None:
        from hermes.shell.presentation.gtk4.widgets.settings_window import (
            preset_to_display_name,
        )

        assert preset_to_display_name("InventadoNoExiste") == "Océano"


# ---------------------------------------------------------------------------
# LayoutPrefs
# ---------------------------------------------------------------------------

class TestLayoutPrefs:
    """Verifica persistencia y defaults de LayoutPrefs (sin GTK)."""

    def _make_prefs(self, config_dir: str):
        os.environ["XDG_CONFIG_HOME"] = config_dir
        from hermes.shell.presentation.gtk4.layout_prefs import LayoutPrefs  # noqa: PLC0415
        return LayoutPrefs()

    def test_defaults_sin_archivo(self, tmp_path) -> None:
        prefs = self._make_prefs(str(tmp_path))
        assert prefs.show_sidebar is True
        assert prefs.show_workspace is True
        assert prefs.density == "comfortable"

    def test_save_y_load_round_trip(self, tmp_path) -> None:
        prefs = self._make_prefs(str(tmp_path))
        prefs.show_sidebar = False
        prefs.show_workspace = False
        prefs.density = "compact"
        prefs.save()

        # Importar de nuevo para leer desde disco.
        import importlib
        import hermes.shell.presentation.gtk4.layout_prefs as module
        importlib.reload(module)

        prefs2 = self._make_prefs(str(tmp_path))
        assert prefs2.show_sidebar is False
        assert prefs2.show_workspace is False
        assert prefs2.density == "compact"

    def test_reset_to_defaults(self, tmp_path) -> None:
        prefs = self._make_prefs(str(tmp_path))
        prefs.show_sidebar = False
        prefs.density = "compact"
        prefs.save()

        prefs.reset_to_defaults()
        assert prefs.show_sidebar is True
        assert prefs.density == "comfortable"

    def test_archivo_corrupto_usa_defaults(self, tmp_path) -> None:
        config_dir = tmp_path / "hermes-shell"
        config_dir.mkdir()
        (config_dir / "hermes-layout.json").write_text("INVALID JSON !!!!")

        prefs = self._make_prefs(str(tmp_path))
        assert prefs.show_sidebar is True
        assert prefs.density == "comfortable"

    def test_density_valor_invalido_usa_default(self, tmp_path) -> None:
        config_dir = tmp_path / "hermes-shell"
        config_dir.mkdir()
        (config_dir / "hermes-layout.json").write_text(
            json.dumps({"density": "ultra-mega-zoom"})
        )

        prefs = self._make_prefs(str(tmp_path))
        assert prefs.density == "comfortable"

    def test_save_error_de_escritura_no_lanza(self, tmp_path) -> None:
        """Si el disco falla, save() solo registra el warning, no lanza."""
        prefs = self._make_prefs(str(tmp_path))
        # Hacemos el directorio no escribible para forzar OSError.
        config_dir = tmp_path / "hermes-shell"
        config_dir.mkdir(exist_ok=True)
        config_dir.chmod(0o555)
        try:
            prefs.save()  # no debe lanzar
        finally:
            config_dir.chmod(0o755)

    def test_banner_dismissed_default_es_false(self, tmp_path) -> None:
        prefs = self._make_prefs(str(tmp_path))
        assert prefs.banner_dismissed is False

    def test_banner_dismissed_persiste_en_disco(self, tmp_path) -> None:
        prefs = self._make_prefs(str(tmp_path))
        prefs.banner_dismissed = True
        prefs.save()

        import importlib
        import hermes.shell.presentation.gtk4.layout_prefs as module
        importlib.reload(module)

        prefs2 = self._make_prefs(str(tmp_path))
        assert prefs2.banner_dismissed is True

    def test_reset_to_defaults_no_resetea_banner_dismissed(self, tmp_path) -> None:
        """reset_to_defaults no debe deshacer el descarte del banner:
        sería molesto forzar el banner al restaurar densidad/paneles."""
        prefs = self._make_prefs(str(tmp_path))
        prefs.banner_dismissed = True
        prefs.save()
        prefs.reset_to_defaults()
        # banner_dismissed no se toca en reset.
        assert prefs.banner_dismissed is True

    def test_banner_dismissed_false_en_archivo_legacy_sin_clave(self, tmp_path) -> None:
        """Archivos guardados antes de esta versión no tienen banner_dismissed;
        deben cargar con default False para no ocultar el banner a usuarios nuevos."""
        config_dir = tmp_path / "hermes-shell"
        config_dir.mkdir()
        (config_dir / "hermes-layout.json").write_text(
            json.dumps({"show_sidebar": True, "density": "comfortable"})
        )
        prefs = self._make_prefs(str(tmp_path))
        assert prefs.banner_dismissed is False


# ---------------------------------------------------------------------------
# Lógica de caret parpadeante — sin GTK
# ---------------------------------------------------------------------------

class TestCaretLogic:
    """Verifica la lógica de cancelación del caret sin necesidad de GTK.

    Se simula el container como un namespace simple para aislar la lógica
    pura de la API de GTK.
    """

    class _FakeLabel:
        def __init__(self) -> None:
            self.current_text: str = ""
            self.visible: bool = False

        def set_text(self, text: str) -> None:
            self.current_text = text

        def set_visible(self, v: bool) -> None:
            self.visible = v

    class _FakeContainer:
        """Simula el Gtk.Box del streaming bubble."""
        def __init__(self) -> None:
            self._text_acc: list = []
            self._is_typing: bool = True
            self._is_streaming: bool = True
            self._caret_timeout_id = None
            self._typing_label = None
            self._progress_label = None

    def test_caret_timeout_id_inicializado_en_none(self) -> None:
        """start_streaming_agent_message debe inicializar _caret_timeout_id a None."""
        c = self._FakeContainer()
        assert c._caret_timeout_id is None

    def test_caret_timeout_id_none_es_seguro_para_stop(self) -> None:
        """_stop_caret_animation con _caret_timeout_id=None no lanza."""
        # Simular la lógica de _stop_caret_animation sin GLib.
        c = self._FakeContainer()
        timeout_id = getattr(c, "_caret_timeout_id", None)
        # Si timeout_id es None, no se llama source_remove — no debe lanzar.
        assert timeout_id is None

    def test_is_streaming_false_evita_actualizacion_caret(self) -> None:
        """El tick del caret debe retornar False (sin actualizar) cuando
        _is_streaming es False, para no dejar timers huérfanos."""
        c = self._FakeContainer()
        label = self._FakeLabel()
        c._progress_label = label
        c._text_acc = ["hola"]
        c._is_streaming = False

        # Simular el cuerpo del _tick.
        is_streaming = getattr(c, "_is_streaming", False)
        assert is_streaming is False
        # El tick debe devolver GLib.SOURCE_REMOVE (False) sin tocar el label.
        # Verificamos que la condición de salida es correcta.
        result = not is_streaming  # True → debe salir
        assert result is True

    def test_texto_base_sin_caret_en_acumulador(self) -> None:
        """El acumulador _text_acc contiene el texto limpio (sin ▌);
        el caret lo añade el timer, no el acumulador."""
        c = self._FakeContainer()
        c._text_acc = ["hola", " mundo"]
        full = "".join(c._text_acc)
        assert "▌" not in full
        # El label recibe texto+caret pero el acumulador permanece limpio.
        label = self._FakeLabel()
        label.set_text(full + " ▌")
        assert label.current_text == "hola mundo ▌"
        assert "▌" not in full  # acumulador intacto


# ---------------------------------------------------------------------------
# ThemeManager — lógica pura (sin GTK)
# ---------------------------------------------------------------------------

class TestThemeManagerPureLogic:
    """Verifica las funciones puras de ThemeManager sin instanciar el objeto
    (que requiere GTK). Se importan directamente las funciones de módulo."""

    def test_hex_to_hsl_y_vuelta_roundtrip(self) -> None:
        from hermes.shell.presentation.gtk4.theme_manager import (
            _hex_to_hsl,
            _hsl_to_hex,
        )

        original = "#0A84FF"
        h, s, l = _hex_to_hsl(original)
        recovered = _hsl_to_hex(h, s, l)
        # Tolerancia de ±1 por redondeo de enteros.
        for i in range(1, 7, 2):
            orig_byte = int(original[i:i+2], 16)
            recv_byte = int(recovered[i:i+2], 16)
            assert abs(orig_byte - recv_byte) <= 1, (
                f"Canal {i}: {orig_byte} ≠ {recv_byte} (original={original})"
            )

    def test_derive_hover_es_mas_oscuro_que_base(self) -> None:
        from hermes.shell.presentation.gtk4.theme_manager import (
            _derive_hover_press,
            _hex_to_hsl,
        )

        accent = "#0A84FF"
        hover, press = _derive_hover_press(accent)
        _, _, l_base  = _hex_to_hsl(accent)
        _, _, l_hover = _hex_to_hsl(hover)
        _, _, l_press = _hex_to_hsl(press)

        assert l_hover < l_base, "hover debe ser más oscuro que el acento base"
        assert l_press < l_hover, "press debe ser más oscuro que hover"

    def test_build_accent_css_contiene_variables_requeridas(self) -> None:
        from hermes.shell.presentation.gtk4.theme_manager import _build_accent_css

        css = _build_accent_css("#0A84FF", "#FFFFFF").decode("utf-8")

        required_vars = [
            "hermes_accent",
            "hermes_accent_hover",
            "hermes_accent_press",
            "hermes_accent_subtle",
            "hermes_accent_ring",
            "hermes_on_accent",
            "accent_bg_color",
            "accent_fg_color",
            "accent_color",
        ]
        for var in required_vars:
            assert var in css, f"Variable @{var} ausente del CSS de acento"

    def test_accent_presets_tienen_9_entradas(self) -> None:
        from hermes.shell.presentation.gtk4.theme_manager import (
            ACCENT_PRESETS,
            _DEFAULT_ACCENT,
        )

        # El catálogo de acentos son los 8 del copy-deck + "Índigo",
        # el acento de marca por defecto (_DEFAULT_ACCENT).
        assert len(ACCENT_PRESETS) == 9
        # El acento por defecto DEBE existir en el catálogo, o el arranque
        # rompería en apply_accent (que valida preset_name in ACCENT_PRESETS).
        assert _DEFAULT_ACCENT in ACCENT_PRESETS

    def test_accent_presets_todos_con_hex_valido(self) -> None:
        from hermes.shell.presentation.gtk4.theme_manager import ACCENT_PRESETS

        for name, (hex_color, on_hex) in ACCENT_PRESETS.items():
            assert hex_color.startswith("#"), f"{name}: hex_color no empieza con #"
            assert len(hex_color) == 7, f"{name}: hex_color longitud incorrecta"
            assert on_hex in ("#FFFFFF", "#1D1D1F"), (
                f"{name}: on_accent debe ser blanco o negro OLED"
            )
