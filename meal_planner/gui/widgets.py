"""Reusable widgets: table and form."""

from datetime import date
from tkinter import ttk
import tkinter as tk

from meal_planner.gui.support import RED


def create_delete_icon(master=None, size=20, bg_color="#fef2f2", border_color="#991b1b", cross_color="#7f1d1d"):
    """
    Renders a PhotoImage styled as a dark red cross surrounded by a rounded square.
    """
    try:
        from PIL import Image, ImageDraw, ImageTk
        im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        # Rounded square outline & fill
        d.rounded_rectangle((1, 1, size - 2, size - 2), radius=4, fill=bg_color, outline=border_color, width=1)
        # Dark red cross
        pad = 5
        d.line([(pad, pad), (size - 1 - pad, size - 1 - pad)], fill=cross_color, width=2)
        d.line([(pad, size - 1 - pad), (size - 1 - pad, pad)], fill=cross_color, width=2)
        return ImageTk.PhotoImage(im, master=master)
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
    ):
        super().__init__(master)
        self.show_delete = show_delete
        self.on_delete = on_delete
        self.on_select = on_select
        self.on_double_click = on_double_click
        self._delete_icon = None

        col_keys = [c for c, _, _ in columns]
        show_mode = ("tree", "headings") if show_delete else "headings"
        self.tv = ttk.Treeview(self, columns=col_keys, show=show_mode, height=height)

        if show_delete:
            self.tv.heading("#0", text="")
            self.tv.column("#0", width=42, minwidth=36, stretch=False, anchor="center")
            self.update_delete_icon()
            self.tv.bind("<ButtonRelease-1>", self._handle_click)
            self.tv.bind("<Double-1>", self._handle_double_click)
        elif on_select or on_double_click:
            self.tv.bind("<ButtonRelease-1>", self._handle_click)
            self.tv.bind("<Double-1>", self._handle_double_click)

        for key, title, width in columns:
            self.tv.heading(key, text=title)
            self.tv.column(key, width=width, anchor="w")

        bar = ttk.Scrollbar(self, command=self.tv.yview)
        self.tv.configure(yscrollcommand=bar.set)
        self.tv.tag_configure("ko",       foreground=RED)
        self.tv.tag_configure("expiring", foreground="#f97316")          # orange: expires within 7 days
        self.tv.tag_configure("expired",  foreground="#e5484d",          # red alert: already expired
                              font=("", 0, "bold"))
        self.tv.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")

    def update_delete_icon(self, is_dark=False):
        """Refreshes or creates the PhotoImage used for delete buttons."""
        if self.show_delete:
            try:
                bg = "#2a1518" if is_dark else "#fef2f2"
                border = "#b91c1c" if is_dark else "#991b1b"
                cross = "#ef4444" if is_dark else "#7f1d1d"
                self._delete_icon = create_delete_icon(master=self, bg_color=bg, border_color=border, cross_color=cross)
            except Exception:
                self._delete_icon = None

    def _ensure_delete_icon(self):
        """Ensures the delete icon exists and is valid in the current Tcl interpreter."""
        if not self.show_delete:
            return
        if self._delete_icon is None:
            self.update_delete_icon()
            return
        try:
            self.tk.call("image", "type", str(self._delete_icon))
        except (tk.TclError, Exception):
            self.update_delete_icon()

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
                    self.update_delete_icon()
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
        self.bind("<Configure>", lambda e: self.draw())

    def plot(self, points, background, foreground):
        self.points, self.fg = points, foreground
        self.configure(background=background)
        self.draw()

    def draw(self):
        self.delete("all")
        if not self.points:
            return
        w, h, pad = self.winfo_width(), self.winfo_height(), 30
        xs = [date.fromisoformat(d).toordinal() for d, _ in self.points]
        ys = [y for _, y in self.points]
        x0, y0 = min(xs), min(ys)
        xy = [(pad + (x - x0) / ((max(xs) - x0) or 1) * (w - 2 * pad),
               h - pad - (y - y0) / ((max(ys) - y0) or 1) * (h - 2 * pad)) for x, y in zip(xs, ys)]
        if len(xy) > 1:
            self.create_line(*[c for p in xy for c in p], fill="#3b82f6", width=2)
        for x, y in xy:
            self.create_oval(x - 3, y - 3, x + 3, y + 3, fill="#3b82f6", outline="")
        self.create_text(4, pad, text=f"{max(ys):g}", anchor="w", fill=self.fg)
        self.create_text(4, h - pad, text=f"{y0:g}", anchor="w", fill=self.fg)
        self.create_text(pad, h - 8, text=self.points[0][0], anchor="w", fill=self.fg)
        self.create_text(w - pad, h - 8, text=self.points[-1][0], anchor="e", fill=self.fg)


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
