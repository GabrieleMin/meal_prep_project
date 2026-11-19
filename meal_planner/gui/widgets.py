"""Reusable widgets: table and form."""

from datetime import date
from tkinter import ttk
import tkinter as tk

from meal_planner.gui import theme
from meal_planner.gui.support import RED


def create_delete_icon(
    master=None,
    size=20,
    bg_color="#fbebec",
    border_color="#f0b8bd",
    cross_color="#c94a53",
    scale=4,
):
    """
    Renders an anti-aliased PhotoImage styled as a soft modern cross inside a rounded badge.
    """
    if not isinstance(size, (int, float)) or not isinstance(scale, (int, float)) or size <= 0 or scale <= 0:
        return None
    try:
        from PIL import Image, ImageDraw, ImageTk
        size_i = int(round(size))
        scale_i = int(round(scale))
        if size_i <= 0 or scale_i <= 0:
            return None
        s = size_i * scale_i
        pad = int(5.5 * scale_i)
        r = 4 * scale_i
        border_w = scale_i
        cross_w = max(1, round(1.6 * scale_i))

        im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        # Rounded square outline & fill
        d.rounded_rectangle(
            (border_w, border_w, s - border_w - 1, s - border_w - 1),
            radius=r,
            fill=bg_color,
            outline=border_color,
            width=border_w,
        )
        # Soft cross lines
        d.line([(pad, pad), (s - 1 - pad, s - 1 - pad)], fill=cross_color, width=cross_w)
        d.line([(pad, s - 1 - pad), (s - 1 - pad, pad)], fill=cross_color, width=cross_w)

        resample = getattr(Image, "Resampling", Image).LANCZOS
        im_resized = im.resize((size_i, size_i), resample) if scale_i > 1 else im
        return ImageTk.PhotoImage(im_resized, master=master)
    except Exception:
        return None


class Table(ttk.Frame):
    def __init__(
        self,
        master,
        columns,
        height=14,
        show_delete=False,
        on_delete=None,
        on_select=None,
        on_double_click=None,
        is_dark=None,
    ):
        super().__init__(master)
        self.show_delete = show_delete
        self.on_delete = on_delete
        self.on_select = on_select
        self.on_double_click = on_double_click
        self._delete_icon = None
        self._delete_icon_ref = None
        self._delete_icons_cache = {}
        self._delete_icon_key = None
        self._is_dark = is_dark

        col_keys = [c for c, _, _ in columns]
        show_mode = ("tree", "headings") if show_delete else "headings"
        self.tv = ttk.Treeview(self, columns=col_keys, show=show_mode, height=height)

        if show_delete:
            self.tv.heading("#0", text="")
            self.tv.column(
                "#0",
                width=theme.scale_px(42, self),
                minwidth=theme.scale_px(36, self),
                stretch=False,
                anchor="center",
            )
            self.update_delete_icon(is_dark=is_dark)
            self.tv.bind("<ButtonRelease-1>", self._handle_click)
            self.tv.bind("<Double-1>", self._handle_double_click)
        elif on_select or on_double_click:
            self.tv.bind("<ButtonRelease-1>", self._handle_click)
            self.tv.bind("<Double-1>", self._handle_double_click)

        for key, title, width in columns:
            self.tv.heading(key, text=title)
            self.tv.column(key, width=theme.scale_px(width, self), anchor="w")

        bar = ttk.Scrollbar(self, command=self.tv.yview)
        self.tv.configure(yscrollcommand=bar.set)
        self.tv.tag_configure("ko",       foreground=RED)
        self.tv.tag_configure("expiring", foreground="#f97316")          # orange: expires within 7 days
        self.tv.tag_configure("expired",  foreground="#e5484d",          # red alert: already expired
                              font=theme.font("body_bold", self))
        self.tv.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")

    def update_delete_icon(self, is_dark=None):
        """Refreshes or creates the PhotoImage used for delete buttons and updates all visible rows."""
        if not self.show_delete:
            return

        if is_dark is None:
            try:
                import sv_ttk
                theme_name = sv_ttk.get_theme()
                if theme_name in ("dark", "light"):
                    is_dark = (theme_name == "dark")
                elif getattr(self, "_is_dark", None) is not None:
                    is_dark = self._is_dark
                else:
                    is_dark = (theme.load() == "dark")
            except Exception:
                is_dark = getattr(self, "_is_dark", False) or False

        self._is_dark = bool(is_dark)
        size_px = theme.scale_px(20, self)
        cache_key = (self._is_dark, size_px)

        if not hasattr(self, "_delete_icons_cache"):
            self._delete_icons_cache = {}

        if self._delete_icon is None:
            self._delete_icons_cache.clear()
            self._delete_icon_key = None

        icon = None
        active_key = getattr(self, "_delete_icon_key", None)
        if active_key != cache_key:
            cached = self._delete_icons_cache.get(cache_key)
            if cached is not None:
                try:
                    if self.tv.tk.call("image", "type", str(cached)) == "photo":
                        icon = cached
                    else:
                        self._delete_icons_cache.pop(cache_key, None)
                except (tk.TclError, Exception):
                    self._delete_icons_cache.pop(cache_key, None)

        if icon is None:
            try:
                bg = "#2d1f22" if self._is_dark else "#fbebec"
                border = "#52272e" if self._is_dark else "#f0b8bd"
                cross = "#f87171" if self._is_dark else "#c94a53"
                icon = create_delete_icon(
                    master=self,
                    size=size_px,
                    bg_color=bg,
                    border_color=border,
                    cross_color=cross,
                )
            except Exception:
                icon = None

        if icon is not None:
            self._delete_icons_cache[cache_key] = icon
            self._delete_icon = icon
            self._delete_icon_ref = icon
            self._delete_icon_key = cache_key
        else:
            self._delete_icons_cache.pop(cache_key, None)
            self._delete_icon = None
            self._delete_icon_ref = None
            self._delete_icon_key = None

        # Propagate updated icon to all currently rendered treeview rows
        img_val = self._delete_icon if self._delete_icon is not None else ""
        for child in self.tv.get_children(""):
            try:
                self.tv.item(child, image=img_val)
            except (tk.TclError, Exception):
                pass

    def _ensure_delete_icon(self):
        """Ensures the delete icon exists and is valid in the current Tcl interpreter."""
        if not self.show_delete:
            return
        if self._delete_icon is None:
            self.update_delete_icon(getattr(self, "_is_dark", None))
            return
        try:
            self.tk.call("image", "type", str(self._delete_icon))
        except (tk.TclError, Exception):
            self.update_delete_icon(getattr(self, "_is_dark", None))

    def _handle_click(self, event):
        region = self.tv.identify("region", event.x, event.y)
        if region == "heading":
            return
        row_id = self.tv.identify_row(event.y)
        if not row_id:
            return
        col = self.tv.identify_column(event.x)
        if self.show_delete and (col == "#0" or region in ("tree", "image")):
            if self.on_delete:
                self.on_delete(row_id)
        else:
            if self.on_select:
                self.on_select(row_id)

    def _handle_double_click(self, event):
        region = self.tv.identify("region", event.x, event.y)
        if region == "heading":
            return
        row_id = self.tv.identify_row(event.y)
        if not row_id:
            return
        col = self.tv.identify_column(event.x)
        if self.show_delete and (col == "#0" or region in ("tree", "image")):
            if self.on_delete:
                self.on_delete(row_id)
        else:
            if self.on_double_click:
                self.on_double_click(row_id)
            elif self.on_select:
                self.on_select(row_id)

    def fill(self, rows):
        """
        rows = [(values, tag), ...] or [(values, tag, row_id), ...]
        """
        self.tv.delete(*self.tv.get_children())
        if self.show_delete:
            self._ensure_delete_icon()

        for item in rows:
            if len(item) == 3:
                values, tag, row_id = item
            else:
                values, tag = item
                row_id = None

            kwargs = {"values": values}
            if tag:
                kwargs["tags"] = (tag,)
            if row_id is not None:
                kwargs["iid"] = str(row_id)
            if self.show_delete and self._delete_icon is not None:
                kwargs["image"] = self._delete_icon

            try:
                self.tv.insert("", "end", **kwargs)
            except tk.TclError as e:
                if "image" in str(e).lower():
                    self.update_delete_icon(getattr(self, "_is_dark", None))
                    if self._delete_icon is not None:
                        kwargs["image"] = self._delete_icon
                    else:
                        kwargs.pop("image", None)
                    try:
                        self.tv.insert("", "end", **kwargs)
                    except tk.TclError:
                        kwargs.pop("image", None)
                        self.tv.insert("", "end", **kwargs)
                else:
                    raise


class LineChart(tk.Canvas):
    """Minimal line chart of (ISO date, value) points, oldest first."""

    def __init__(self, master, height=150):
        super().__init__(master, height=height, highlightthickness=0)
        self.points, self.fg = [], "#000000"
        self._ordinals = []
        self._redraw_job = None
        self.bind("<Configure>", self._on_configure)

    def _on_configure(self, event=None):
        if self._redraw_job is not None:
            try:
                self.after_cancel(self._redraw_job)
            except Exception:
                pass
            self._redraw_job = None
        self._redraw_job = self.after_idle(self._debounced_draw)

    def _debounced_draw(self):
        self._redraw_job = None
        self.draw()

    def plot(self, points, background, foreground):
        self.points, self.fg = points, foreground
        try:
            self._ordinals = [date.fromisoformat(d).toordinal() for d, _ in points]
        except Exception:
            self._ordinals = list(range(len(points)))
        self.configure(background=background)
        self.draw()

    def draw(self):
        if self._redraw_job is not None:
            try:
                self.after_cancel(self._redraw_job)
            except Exception:
                pass
            self._redraw_job = None
        self.delete("all")
        if not self.points:
            return
        w, h = self.winfo_width(), self.winfo_height()
        pad = theme.scale_px(30, self)
        if self._ordinals and len(self._ordinals) == len(self.points):
            xs = self._ordinals
        else:
            try:
                xs = [date.fromisoformat(d).toordinal() for d, _ in self.points]
            except Exception:
                xs = list(range(len(self.points)))
        ys = [y for _, y in self.points]
        x0, y0 = min(xs), min(ys)
        xy = [(pad + (x - x0) / ((max(xs) - x0) or 1) * (w - 2 * pad),
               h - pad - (y - y0) / ((max(ys) - y0) or 1) * (h - 2 * pad)) for x, y in zip(xs, ys)]
        if len(xy) > 1:
            self.create_line(*[c for p in xy for c in p], fill="#3b82f6", width=max(1, theme.scale_px(2, self)))
        r = max(2, theme.scale_px(3, self))
        for x, y in xy:
            self.create_oval(x - r, y - r, x + r, y + r, fill="#3b82f6", outline="")
        cap_font = theme.font("caption", self)
        x_lbl = max(4, theme.scale_px(4, self))
        y_lbl = max(8, theme.scale_px(8, self))
        self.create_text(x_lbl, pad, text=f"{max(ys):g}", anchor="w", fill=self.fg, font=cap_font)
        self.create_text(x_lbl, h - pad, text=f"{y0:g}", anchor="w", fill=self.fg, font=cap_font)
        self.create_text(pad, h - y_lbl, text=self.points[0][0], anchor="w", fill=self.fg, font=cap_font)
        self.create_text(w - pad, h - y_lbl, text=self.points[-1][0], anchor="e", fill=self.fg, font=cap_font)


class Form(ttk.LabelFrame):
    def __init__(self, master, title, fields, button_text, command):
        super().__init__(master, text=title, padding=10)
        self.v, self.w = {}, {}
        for r, (key, label, values) in enumerate(fields):
            ttk.Label(self, text=label).grid(row=r, column=0, sticky="w", pady=3)
            self.v[key] = tk.StringVar()
            cls = ttk.Combobox if values is not None else ttk.Entry
            self.w[key] = cls(self, textvariable=self.v[key], width=24,
                                 **({"values": values} if values is not None else {}))
            self.w[key].grid(row=r, column=1, padx=(8, 0), pady=3)
        ttk.Button(self, text=button_text, command=command).grid(
            row=len(fields), column=0, columnspan=2, sticky="ew", pady=(8, 0))

    def read(self):
        return {k: v.get().strip() for k, v in self.v.items()}

    def clear(self):
        for v in self.v.values():
            v.set("")
