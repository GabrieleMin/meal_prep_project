"""Light/dark theme (sv-ttk) and its saved setting."""

from pathlib import Path
import json
import os

import sv_ttk


CONFIG = Path(os.environ.get("MEAL_PLANNER_CONFIG") or Path(__file__).resolve().parents[2] / "config.json")


def load():
    try:
        theme = json.loads(CONFIG.read_text(encoding="utf-8")).get("theme")
    except (OSError, ValueError, AttributeError):
        theme = None
    return theme if theme in ("light", "dark") else "dark"


def save(theme):
    CONFIG.write_text(json.dumps({"theme": theme}, indent=2), encoding="utf-8")


def apply(theme):
    sv_ttk.set_theme(theme)


def palette(theme):
    """(background, foreground) for the non-ttk widgets."""
    return ("#2b2b2b", "#fafafa") if theme == "dark" else ("#f3f3f3", "#1c1c1c")


def style_text(widget, theme):
    """tk.Text is not a ttk widget: give it the colours of the theme."""
    background, foreground = palette(theme)
    widget.configure(background=background, foreground=foreground, insertbackground=foreground)
