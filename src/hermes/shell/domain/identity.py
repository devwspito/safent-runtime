"""Identidad visual Hermes — design tokens del dominio.

NO importa GTK. Solo describe la identidad de marca como datos.
La capa presentation traduce esto a CSS / GTK style classes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class HermesPalette:
    """Paleta de colores Hermes — dark theme por default (FR producto)."""

    # Backgrounds (de más oscuro a más claro).
    bg_canvas: str = "#0B0E14"           # canvas principal
    bg_surface: str = "#11151D"          # paneles
    bg_surface_raised: str = "#171C27"   # cards
    bg_inset: str = "#0E1117"            # inputs / code

    # Bordes + separadores.
    border_subtle: str = "#1F2532"
    border_strong: str = "#2A3142"

    # Texto.
    text_primary: str = "#E6E9F2"
    text_secondary: str = "#9BA3B5"
    text_disabled: str = "#5A6478"

    # Acentos Hermes (azul violáceo — color de marca propio).
    accent_primary: str = "#7C5CFF"      # Hermes violet
    accent_primary_hover: str = "#9078FF"
    accent_primary_press: str = "#6A4DEB"
    accent_on_primary: str = "#FFFFFF"

    # Estados semánticos.
    success: str = "#3DD68C"
    warning: str = "#F0B72F"
    danger: str = "#E5484D"
    info: str = "#5BAEF8"

    # Agentic indicators.
    agent_thinking: str = "#7C5CFF"      # spinner mientras el LLM piensa
    agent_acting: str = "#3DD68C"        # ejecutando una tool
    agent_waiting_hitl: str = "#F0B72F"  # esperando consent humano


@dataclass(frozen=True, slots=True)
class HermesTypography:
    """Tipografías Hermes."""

    font_sans: str = "Inter, 'Adwaita Sans', system-ui, sans-serif"
    font_mono: str = "'JetBrains Mono', 'Adwaita Mono', monospace"

    size_hero: int = 28      # títulos de página
    size_h1: int = 22
    size_h2: int = 18
    size_body: int = 14
    size_small: int = 12
    size_micro: int = 11

    weight_regular: int = 400
    weight_medium: int = 500
    weight_semibold: int = 600
    weight_bold: int = 700


@dataclass(frozen=True, slots=True)
class HermesSpacing:
    """Espaciados base — múltiplos de 4px."""

    xs: int = 4
    sm: int = 8
    md: int = 12
    lg: int = 16
    xl: int = 24
    xxl: int = 32
    xxxl: int = 48


@dataclass(frozen=True, slots=True)
class HermesRadii:
    """Bordes redondeados."""

    none: int = 0
    sm: int = 6
    md: int = 10
    lg: int = 14
    xl: int = 20
    pill: int = 9999


HERMES_PALETTE: Final = HermesPalette()
HERMES_TYPOGRAPHY: Final = HermesTypography()
HERMES_SPACING: Final = HermesSpacing()
HERMES_RADII: Final = HermesRadii()
