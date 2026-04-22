from textual.theme import Theme

# High-contrast dark palette — distinct shades between bg / surface / panel so
# regions are visible on terminals that compress close hues.
BACKGROUND_HEX = "#151922"
SURFACE_HEX = "#1f2633"
PANEL_HEX = "#2b3446"
BORDER_HEX = "#5a6477"
BORDER_MUTED_HEX = "#3b4354"
TEXT_HEX = "#eef1f7"
MUTED_HEX = "#9ea7ba"
PRIMARY_HEX = "#7d96c3"
SECONDARY_HEX = "#5d6f91"
ACCENT_HEX = "#8eb5d9"
SUCCESS_HEX = "#8fbf8f"
WARNING_HEX = "#d6b16f"
ERROR_HEX = "#d38a94"


FLINT_THEME = Theme(
    name="flint-zed",
    primary=PRIMARY_HEX,
    secondary=SECONDARY_HEX,
    warning=WARNING_HEX,
    error=ERROR_HEX,
    success=SUCCESS_HEX,
    accent=ACCENT_HEX,
    foreground=TEXT_HEX,
    background=BACKGROUND_HEX,
    surface=SURFACE_HEX,
    panel=PANEL_HEX,
    dark=True,
    text_alpha=0.95,
    variables={
        "border": BORDER_HEX,
        "border-blurred": BORDER_MUTED_HEX,
        "button-color-foreground": TEXT_HEX,
        "footer-background": PANEL_HEX,
        "input-selection-background": f"{PRIMARY_HEX}40",
    },
)
