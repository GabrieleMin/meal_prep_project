"""Machine, display, DPI, and scaling probes with safe fallbacks for Tkinter GUI adaptation."""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
import re
import subprocess
import sys
from typing import Mapping, Optional, Tuple


MIN_DPI = 72.0
MAX_DPI = 480.0
MIN_SCALE = 0.75
MAX_SCALE = 4.0
DEFAULT_DPI = 96.0
DEFAULT_SCREEN_W = 1280
DEFAULT_SCREEN_H = 800


@dataclass(frozen=True)
class SystemInfo:
    platform: str
    windowingsystem: str
    desktop: str
    session_type: str
    cpu_count: int
    screen_width_px: int
    screen_height_px: int
    screen_mm_w: int
    screen_mm_h: int
    tk_fpixels_1i: Optional[float]
    xft_dpi: Optional[float]
    env_scale: Optional[float]
    dpi: float
    scale_factor: float
    tk_scaling: float
    used_fallback: bool


def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, val))


def _parse_positive_float(raw: object) -> Optional[float]:
    if raw is None:
        return None
    try:
        val = float(str(raw).strip())
    except (ValueError, TypeError):
        return None
    if not math.isfinite(val) or val <= 0.0:
        return None
    return val


def _parse_env_scale(env: Mapping[str, str]) -> Optional[float]:
    explicit = _parse_positive_float(env.get("MEAL_PLANNER_SCALE"))
    if explicit is not None:
        return _clamp(explicit, MIN_SCALE, MAX_SCALE)

    gdk_scale = _parse_positive_float(env.get("GDK_SCALE"))
    gdk_dpi_scale = _parse_positive_float(env.get("GDK_DPI_SCALE"))
    if gdk_scale is not None or gdk_dpi_scale is not None:
        combined = (gdk_scale or 1.0) * (gdk_dpi_scale or 1.0)
        if math.isfinite(combined) and combined > 0.0:
            return _clamp(combined, MIN_SCALE, MAX_SCALE)

    qt_scale = _parse_positive_float(env.get("QT_SCALE_FACTOR"))
    if qt_scale is not None:
        return _clamp(qt_scale, MIN_SCALE, MAX_SCALE)

    return None


def _probe_xft_dpi() -> Optional[float]:
    try:
        proc = subprocess.run(
            ["xrdb", "-query"],
            capture_output=True,
            text=True,
            timeout=0.5,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout:
            m = re.search(r"^\s*Xft\.dpi\s*:\s*([0-9]+(?:\.[0-9]+)?)\s*$", proc.stdout, re.MULTILINE)
            if m:
                val = _parse_positive_float(m.group(1))
                if val is not None:
                    return _clamp(val, MIN_DPI, MAX_DPI)
    except Exception:
        pass
    return None


def enable_windows_dpi_awareness() -> bool:
    """Enables DPI awareness on Windows before Tk initialization; safe no-op elsewhere."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        shcore = getattr(ctypes, "windll", None)
        if shcore is not None and hasattr(shcore, "shcore"):
            shcore.shcore.SetProcessDpiAwareness(1)
            return True
    except Exception:
        pass
    try:
        import ctypes
        user32 = getattr(ctypes, "windll", None)
        if user32 is not None and hasattr(user32, "user32"):
            user32.user32.SetProcessDPIAware()
            return True
    except Exception:
        pass
    return False


def collect_system_info(
    root=None,
    *,
    env: Optional[Mapping[str, str]] = None,
    screen_metrics: Optional[Mapping[str, object]] = None,
) -> SystemInfo:
    """Collects OS, windowing system, display metrics, and safe HiDPI scaling settings."""
    env_map = os.environ if env is None else env
    plat = sys.platform
    desktop = (
        str(env_map.get("XDG_CURRENT_DESKTOP") or env_map.get("DESKTOP_SESSION") or "").strip()
        or ("Windows" if plat == "win32" else ("macOS" if plat == "darwin" else "unknown"))
    )
    session_type = (
        str(env_map.get("XDG_SESSION_TYPE") or "").strip().lower()
        or ("win32" if plat == "win32" else ("aqua" if plat == "darwin" else "unknown"))
    )
    cpu_count = os.cpu_count() or 1

    used_fallback = False
    windowingsystem = "unknown"
    screen_w = DEFAULT_SCREEN_W
    screen_h = DEFAULT_SCREEN_H
    screen_mm_w = 0
    screen_mm_h = 0
    tk_fpixels_1i: Optional[float] = None
    xft_dpi: Optional[float] = None

    if screen_metrics is not None:
        windowingsystem = str(screen_metrics.get("windowingsystem") or session_type or "unknown")
        try:
            sw = int(screen_metrics.get("width_px", DEFAULT_SCREEN_W))
            sh = int(screen_metrics.get("height_px", DEFAULT_SCREEN_H))
            if sw > 0 and sh > 0:
                screen_w, screen_h = sw, sh
            else:
                used_fallback = True
        except (ValueError, TypeError):
            used_fallback = True
        try:
            mm_w = int(screen_metrics.get("width_mm", 0) or 0)
            mm_h = int(screen_metrics.get("height_mm", 0) or 0)
            screen_mm_w = max(0, mm_w)
            screen_mm_h = max(0, mm_h)
        except (ValueError, TypeError):
            screen_mm_w, screen_mm_h = 0, 0
        tk_fpixels_1i = _parse_positive_float(screen_metrics.get("fpixels_1i"))
        raw_xft = _parse_positive_float(screen_metrics.get("xft_dpi"))
        xft_dpi = _clamp(raw_xft, MIN_DPI, MAX_DPI) if raw_xft is not None else None
    elif root is not None:
        try:
            windowingsystem = str(root.tk.call("tk", "windowingsystem")).strip().lower() or "unknown"
        except Exception:
            windowingsystem = "unknown"
            used_fallback = True
        try:
            sw = int(root.winfo_screenwidth())
            sh = int(root.winfo_screenheight())
            if sw > 0 and sh > 0:
                screen_w, screen_h = sw, sh
            else:
                used_fallback = True
        except Exception:
            used_fallback = True
        try:
            screen_mm_w = max(0, int(root.winfo_screenmmwidth()))
            screen_mm_h = max(0, int(root.winfo_screenmmheight()))
        except Exception:
            screen_mm_w, screen_mm_h = 0, 0
        try:
            tk_fpixels_1i = _parse_positive_float(root.winfo_fpixels("1i"))
        except Exception:
            tk_fpixels_1i = None
        if windowingsystem == "x11":
            xft_dpi = _probe_xft_dpi()
    else:
        used_fallback = True

    env_scale = _parse_env_scale(env_map)

    if env_scale is not None:
        scale_factor = env_scale
        dpi = _clamp(DEFAULT_DPI * scale_factor, MIN_DPI, MAX_DPI)
    elif xft_dpi is not None:
        dpi = xft_dpi
        scale_factor = 1.0 if abs(dpi - DEFAULT_DPI) <= 4.0 else _clamp(round(dpi / DEFAULT_DPI, 2), MIN_SCALE, MAX_SCALE)
    elif tk_fpixels_1i is not None:
        dpi = _clamp(tk_fpixels_1i, MIN_DPI, MAX_DPI)
        scale_factor = 1.0 if abs(dpi - DEFAULT_DPI) <= 4.0 else _clamp(round(dpi / DEFAULT_DPI, 2), MIN_SCALE, MAX_SCALE)
    elif screen_mm_w >= 50 and screen_mm_h >= 50 and screen_w > 0:
        phys_dpi = screen_w / (screen_mm_w / 25.4)
        dpi = _clamp(phys_dpi, MIN_DPI, MAX_DPI)
        scale_factor = 1.0 if abs(dpi - DEFAULT_DPI) <= 4.0 else _clamp(round(dpi / DEFAULT_DPI, 2), MIN_SCALE, MAX_SCALE)
    else:
        dpi = DEFAULT_DPI
        scale_factor = 1.0
        used_fallback = True

    tk_scaling = dpi / 72.0

    return SystemInfo(
        platform=plat,
        windowingsystem=windowingsystem,
        desktop=desktop,
        session_type=session_type,
        cpu_count=cpu_count,
        screen_width_px=screen_w,
        screen_height_px=screen_h,
        screen_mm_w=screen_mm_w,
        screen_mm_h=screen_mm_h,
        tk_fpixels_1i=tk_fpixels_1i,
        xft_dpi=xft_dpi,
        env_scale=env_scale,
        dpi=dpi,
        scale_factor=scale_factor,
        tk_scaling=tk_scaling,
        used_fallback=used_fallback,
    )


def apply_tk_scaling(root, info: SystemInfo) -> None:
    """Applies computed tk scaling to the Tk root (preserving native Aqua Retina backing unless overridden)."""
    if root is None or info is None:
        return
    if info.windowingsystem == "aqua" and info.env_scale is None:
        return
    try:
        root.tk.call("tk", "scaling", float(info.tk_scaling))
    except Exception:
        pass


def clamp_geometry(
    target_w: int,
    target_h: int,
    screen_w: int,
    screen_h: int,
    min_w: Optional[int] = None,
    min_h: Optional[int] = None,
    margin_w_ratio: float = 0.95,
    margin_h_ratio: float = 0.92,
) -> Tuple[int, int, Optional[int], Optional[int]]:
    """Clamps target and minimum window dimensions to safely fit within the active screen."""
    w = max(1, int(target_w))
    h = max(1, int(target_h))
    if screen_w > 0:
        max_w = max(320, int(screen_w * margin_w_ratio))
        w = min(w, max_w)
    if screen_h > 0:
        max_h = max(240, int(screen_h * margin_h_ratio))
        h = min(h, max_h)

    clamped_min_w = min(int(min_w), w) if min_w is not None else None
    clamped_min_h = min(int(min_h), h) if min_h is not None else None
    return w, h, clamped_min_w, clamped_min_h
