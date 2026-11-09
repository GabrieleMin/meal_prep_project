"""Light/dark theme (sv-ttk) and its saved setting."""

from pathlib import Path
import json
import os

import sv_ttk
from meal_planner import config


CONFIG = config.CONFIG_PATH


def load():
    theme = config.get("default_theme") or config.get("theme", "dark")
    return theme if theme in ("light", "dark") else "dark"


def save_default(theme):
    """Persists default startup theme."""
    config.set_key("default_theme", theme)


def save(theme):
    """Backwards compatible alias for save_default."""
    save_default(theme)


def apply(theme):
    sv_ttk.set_theme(theme)


def palette(theme):
    """(background, foreground) for the non-ttk widgets."""
    return ("#2b2b2b", "#fafafa") if theme == "dark" else ("#f3f3f3", "#1c1c1c")


def style_text(widget, theme):
    """tk.Text is not a ttk widget: give it the colours of the theme."""
    background, foreground = palette(theme)
    widget.configure(background=background, foreground=foreground, insertbackground=foreground)
