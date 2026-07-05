"""hermes.tui.theme — the "Sereno" design system for Safent Terminal.

One source of truth for color. The Textual Theme drives all `$variables` used
in safent.tcss; PALETTE exposes the same hex values for Rich renderables (badges,
markdown rules) so the TUI and the QML desktop read as the same product:
warm near-black canvas, amber (#F0A85A) accent, calm off-white text.
"""

from __future__ import annotations

from textual.theme import Theme

# Raw hex — shared by TCSS (via the Theme) and Rich renderables.
PALETTE = {
    "bg": "#0E0D0A",
    "surface": "#17150F",
    "panel": "#201D15",
    "panel_hi": "#2A2619",
    "border_solid": "#3A3526",
    "amber": "#F0A85A",
    "amber_dim": "#A8763B",
    "teal": "#8FB3C7",
    "text": "#EAE3D4",
    "text_muted": "#9C9486",
    "text_faint": "#6E675B",
    "success": "#8FB36B",
    "warning": "#E6B450",
    "error": "#E5544A",
    "high": "#E5544A",
    "medium": "#E6B450",
    "low": "#8FB36B",
}

SAFENT_THEME = Theme(
    name="safent",
    primary=PALETTE["amber"],
    secondary=PALETTE["teal"],
    accent=PALETTE["amber"],
    foreground=PALETTE["text"],
    background=PALETTE["bg"],
    surface=PALETTE["surface"],
    panel=PALETTE["panel"],
    success=PALETTE["success"],
    warning=PALETTE["warning"],
    error=PALETTE["error"],
    dark=True,
    variables={
        "block-cursor-foreground": PALETTE["bg"],
        "block-cursor-background": PALETTE["amber"],
        "block-cursor-text-style": "none",
        "border": PALETTE["border_solid"],
        "border-blurred": PALETTE["border_solid"],
        "scrollbar": PALETTE["panel_hi"],
        "scrollbar-hover": PALETTE["amber_dim"],
        "scrollbar-active": PALETTE["amber"],
        "footer-key-foreground": PALETTE["amber"],
        "footer-description-foreground": PALETTE["text_muted"],
        "input-cursor-background": PALETTE["amber"],
        "input-selection-background": f"{PALETTE['amber']} 35%",
        "text-muted": PALETTE["text_muted"],
        "panel-hi": PALETTE["panel_hi"],
    },
)


def risk_color(risk: str) -> str:
    """Map a risk label to a palette color (for badges / approval cards)."""
    return {
        "high": PALETTE["high"],
        "critical": PALETTE["high"],
        "medium": PALETTE["medium"],
        "moderate": PALETTE["medium"],
        "low": PALETTE["low"],
    }.get((risk or "").strip().lower(), PALETTE["text_muted"])
