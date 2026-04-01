from textual.theme import Theme

BACKGROUND_HEX = "#252b36"
SURFACE_HEX = "#2c3340"
PANEL_HEX = "#363f4f"
BORDER_HEX = "#485162"
BORDER_MUTED_HEX = "#353d4c"
TEXT_HEX = "#d7dbe5"
MUTED_HEX = "#97a0b3"
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
