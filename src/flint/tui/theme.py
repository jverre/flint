"""Flint color palette and Textual themes.

Defines a cohesive color palette for the Flint TUI with both dark and light
variants. Colors are designed around a cool blue-gray base with warm amber
accents, optimized for terminal readability.

Usage in TCSS:
    $primary, $secondary, $accent, $background, $surface, $panel,
    $text, $text-muted, $success, $warning, $error
"""

from textual.theme import Theme

# ── Color Palette ────────────────────────────────────────────
#
# Primary:    Blue – core brand color, interactive elements, focus rings
# Secondary:  Slate – subtle UI chrome, borders, inactive states
# Accent:     Amber – calls to action, highlights, notifications
# Success:    Green – healthy/running states
# Warning:    Amber/Orange – caution states
# Error:      Red – error/failed states

PALETTE = {
    "dark": {
        # Brand
        "primary": "#5B8DEF",       # Soft blue – buttons, links, focus
        "secondary": "#8B95A5",     # Cool gray – secondary text, borders
        "accent": "#F0A050",        # Warm amber – highlights, badges

        # Backgrounds (darkest to lightest)
        "background": "#0F1219",    # Deep blue-black – main bg
        "surface": "#1A1F2B",       # Raised surface – status bars, cards
        "panel": "#151A24",         # Side panels, secondary areas

        # Text
        "text": "#D4DAE5",          # Primary text – high contrast on dark
        "text-muted": "#6B7588",    # Secondary text – labels, timestamps

        # Borders
        "border": "#2A3040",        # Subtle borders between sections

        # Semantic
        "success": "#3DD68C",       # Running/healthy
        "warning": "#F0A050",       # Starting/caution (matches accent)
        "error": "#EF5B5B",         # Error/failed
    },
    "light": {
        # Brand
        "primary": "#2563EB",       # Vivid blue – stands out on white
        "secondary": "#64748B",     # Slate – secondary elements
        "accent": "#D97706",        # Deep amber – highlights

        # Backgrounds (lightest to darkest)
        "background": "#F8FAFC",    # Near-white – main bg
        "surface": "#E2E8F0",       # Raised surface – bars, cards
        "panel": "#F1F5F9",         # Side panels

        # Text
        "text": "#1E293B",          # Primary text – near black
        "text-muted": "#94A3B8",    # Secondary text – labels

        # Borders
        "border": "#CBD5E1",        # Visible but subtle borders

        # Semantic
        "success": "#16A34A",       # Running/healthy
        "warning": "#D97706",       # Starting/caution
        "error": "#DC2626",         # Error/failed
    },
}

# ── Textual Themes ───────────────────────────────────────────

flint_dark = Theme(
    name="flint-dark",
    primary=PALETTE["dark"]["primary"],
    secondary=PALETTE["dark"]["secondary"],
    accent=PALETTE["dark"]["accent"],
    background=PALETTE["dark"]["background"],
    surface=PALETTE["dark"]["surface"],
    panel=PALETTE["dark"]["panel"],
    foreground=PALETTE["dark"]["text"],
    success=PALETTE["dark"]["success"],
    warning=PALETTE["dark"]["warning"],
    error=PALETTE["dark"]["error"],
    dark=True,
    variables={
        "text": PALETTE["dark"]["text"],
        "text-muted": PALETTE["dark"]["text-muted"],
        "border": PALETTE["dark"]["border"],
    },
)

flint_light = Theme(
    name="flint-light",
    primary=PALETTE["light"]["primary"],
    secondary=PALETTE["light"]["secondary"],
    accent=PALETTE["light"]["accent"],
    background=PALETTE["light"]["background"],
    surface=PALETTE["light"]["surface"],
    panel=PALETTE["light"]["panel"],
    foreground=PALETTE["light"]["text"],
    success=PALETTE["light"]["success"],
    warning=PALETTE["light"]["warning"],
    error=PALETTE["light"]["error"],
    dark=False,
    variables={
        "text": PALETTE["light"]["text"],
        "text-muted": PALETTE["light"]["text-muted"],
        "border": PALETTE["light"]["border"],
    },
)
