"""Light/dark theme (sv-ttk), semantic font registry, HiDPI scaling, and saved setting."""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
from typing import Optional

import sv_ttk
from meal_planner import config


CONFIG = config.CONFIG_PATH

_SV_FONT_BASE_PX = {
    "SunValleyCaptionFont": (12, None),
    "SunValleyBodyFont": (14, None),
    "SunValleyBodyStrongFont": (14, "bold"),
    "SunValleyBodyLargeFont": (18, None),
    "SunValleySubtitleFont": (20, "bold"),
    "SunValleyTitleFont": (28, "bold"),
    "SunValleyTitleLargeFont": (40, "bold"),
    "SunValleyDisplayFont": (68, "bold"),
}

_ROLE_SPECS = {
    "caption": ("SunValleyCaptionFont", 12, "normal", "roman"),
    "caption_italic": ("SunValleyCaptionFont", 12, "normal", "italic"),
    "body": ("SunValleyBodyFont", 14, "normal", "roman"),
    "body_bold": ("SunValleyBodyFont", 14, "bold", "roman"),
    "subhead": ("SunValleyBodyLargeFont", 16, "bold", "roman"),
    "heading": ("SunValleyBodyLargeFont", 18, "bold", "roman"),
    "overlay_title": ("SunValleySubtitleFont", 20, "bold", "roman"),
    "mono": ("TkFixedFont", 13, "normal", "roman"),
}

_ROOT_SCALES: dict[int, float] = {}
_ROOT_FONTS: dict[tuple[int, str], tkfont.Font] = {}


def _resolve_widget_root(widget=None):
    if widget is None:
        return getattr(tk, "_default_root", None)
    return widget


def _resolve_tk_root(widget=None):
    w = _resolve_widget_root(widget)
    while w is not None and not isinstance(w, tk.Tk):
        w = getattr(w, "master", None)
    return w if isinstance(w, tk.Tk) else getattr(tk, "_default_root", None)


def get_scale_factor(root=None) -> float:
    r = _resolve_tk_root(root)
    if r is None:
        return 1.0
    try:
        return _ROOT_SCALES.get(id(r.tk), 1.0)
    except Exception:
        return 1.0


def scale_px(px: float, root=None, scale_factor: Optional[float] = None) -> int:
    sf = get_scale_factor(root) if scale_factor is None else float(scale_factor)
    return max(1, round(px * sf))


def load():
    theme = config.get("default_theme") or config.get("theme", "dark")
    return theme if theme in ("light", "dark") else "dark"


def save_default(theme):
    """Persists default startup theme."""
    config.set_key("default_theme", theme)


def save(theme):
    """Backwards compatible alias for save_default."""
    save_default(theme)


def _sync_font_family(base_font_name: str, root) -> str:
    try:
        existing = set(tkfont.names(root=root))
        if base_font_name in existing:
            return tkfont.nametofont(base_font_name, root=root).actual("family")
        if "SunValleyBodyFont" in existing:
            return tkfont.nametofont("SunValleyBodyFont", root=root).actual("family")
    except Exception:
        pass
    try:
        return tkfont.nametofont("TkDefaultFont", root=root).actual("family")
    except Exception:
        return "TkDefaultFont"


def apply_font_scaling(root=None, scale_factor: Optional[float] = None) -> None:
    """Scales sv_ttk negative-pixel SunValley* fonts and synced semantic fonts."""
    r = _resolve_tk_root(root)
    if r is None:
        return
    sf = get_scale_factor(r) if scale_factor is None else float(scale_factor)
    _ROOT_SCALES[id(r.tk)] = sf

    try:
        existing = set(tkfont.names(root=r))
    except Exception:
        return

    for fname, (base_px, weight) in _SV_FONT_BASE_PX.items():
        if fname in existing:
            try:
                fobj = tkfont.nametofont(fname, root=r)
                cfg_kw = {"size": -max(8, round(base_px * sf))}
                if weight is not None:
                    cfg_kw["weight"] = weight
                fobj.configure(**cfg_kw)
            except Exception:
                pass

    tk_id = id(r.tk)
    for (fid, role), fobj in list(_ROOT_FONTS.items()):
        if fid != tk_id:
            continue
        spec = _ROLE_SPECS.get(role)
        if not spec:
            continue
        base_name, base_px, weight, slant = spec
        try:
            fobj.configure(
                family=_sync_font_family(base_name, r),
                size=-max(8, round(base_px * sf)),
                weight=weight,
                slant=slant,
            )
        except Exception:
            _ROOT_FONTS.pop((fid, role), None)


def configure_treeview_style(root=None, scale_factor: Optional[float] = None) -> int:
    """Configures Treeview rowheight after sv_ttk theme application so it persists across toggles."""
    r = _resolve_tk_root(root)
    sf = get_scale_factor(r) if scale_factor is None else float(scale_factor)
    linespace = 20
    if r is not None:
        try:
            if "SunValleyBodyFont" in tkfont.names(root=r):
                linespace = int(tkfont.nametofont("SunValleyBodyFont", root=r).metrics("linespace"))
        except Exception:
            pass
    rowheight = max(round(28 * sf), linespace + max(4, round(8 * sf)))
    try:
        ttk.Style(r).configure("Treeview", rowheight=rowheight)
    except Exception:
        pass
    return rowheight


def font(role: str, root=None) -> tkfont.Font:
    """Returns a named tkfont.Font synced to sv_ttk's SunValley* hierarchy."""
    if role not in _ROLE_SPECS:
        raise ValueError(f"Unknown theme font role: {role!r}")
    r = _resolve_tk_root(root) or _resolve_widget_root(root)
    if r is None:
        raise RuntimeError("No active Tk root available to create theme font.")
    tk_id = id(r.tk)
    cache_key = (tk_id, role)
    sf = _ROOT_SCALES.get(tk_id, 1.0)
    base_name, base_px, weight, slant = _ROLE_SPECS[role]
    family = _sync_font_family(base_name, r)
    size = -max(8, round(base_px * sf))

    cached = _ROOT_FONTS.get(cache_key)
    if cached is not None:
        try:
            cached.actual()
            return cached
        except Exception:
            _ROOT_FONTS.pop(cache_key, None)

    font_name = f"MealPlanner_{role}_{tk_id}"
    try:
        if font_name in tkfont.names(root=r):
            fobj = tkfont.nametofont(font_name, root=r)
            fobj.configure(family=family, size=size, weight=weight, slant=slant)
        else:
            fobj = tkfont.Font(root=r, name=font_name, family=family, size=size, weight=weight, slant=slant)
    except Exception:
        fobj = tkfont.Font(root=r, family=family, size=size, weight=weight, slant=slant)
    _ROOT_FONTS[cache_key] = fobj
    return fobj


def apply(theme, root=None, scale_factor: Optional[float] = None):
    r = _resolve_tk_root(root)
    if r is not None and scale_factor is not None:
        _ROOT_SCALES[id(r.tk)] = float(scale_factor)
    if root is None:
        sv_ttk.set_theme(theme)
    else:
        sv_ttk.set_theme(theme, root=r)
    sf = _ROOT_SCALES.get(id(r.tk), 1.0) if r is not None else (float(scale_factor) if scale_factor is not None else 1.0)
    apply_font_scaling(r, sf)
    configure_treeview_style(r, sf)


def palette(theme=None):
    """(background, foreground) matching native sv_ttk surfaces for non-ttk widgets."""
    if theme is None:
        theme = load()
    return ("#1c1c1c", "#fafafa") if theme == "dark" else ("#fafafa", "#1c1c1c")


def surface_palette(theme=None) -> dict[str, str]:
    """Native sv_ttk-aligned surface, card, border, and text colors."""
    if theme is None:
        theme = load()
    if theme == "dark":
        return {
            "bg": "#1c1c1c",
            "fg": "#fafafa",
            "card_bg": "#262626",
            "card_border": "#3d3d3d",
            "sub_fg": "#b3b3b3",
            "entry_bg": "#292929",
            "entry_border": "#444444",
            "accent": "#57c8ff",
        }
    return {
        "bg": "#fafafa",
        "fg": "#1c1c1c",
        "card_bg": "#ffffff",
        "card_border": "#d4d4d4",
        "sub_fg": "#5c5c5c",
        "entry_bg": "#ffffff",
        "entry_border": "#cccccc",
        "accent": "#005fb8",
    }


def style_text(widget, theme):
    """tk.Text is not a ttk widget: give it the colours of the theme."""
    background, foreground = palette(theme)
    widget.configure(background=background, foreground=foreground, insertbackground=foreground)
