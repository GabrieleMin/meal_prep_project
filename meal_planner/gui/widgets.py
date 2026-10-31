"""Reusable widgets: table and form."""

from datetime import date
from tkinter import ttk
import tkinter as tk

from meal_planner.gui.support import RED


class Table(ttk.Frame):
    def __init__(self, master, columns, height=14):
        super().__init__(master)
        self.tv = ttk.Treeview(self, columns=[c for c, _, _ in columns], show="headings", height=height)
        for key, title, width in columns:
            self.tv.heading(key, text=title)
            self.tv.column(key, width=width, anchor="w")
        bar = ttk.Scrollbar(self, command=self.tv.yview)
        self.tv.configure(yscrollcommand=bar.set)
        self.tv.tag_configure("ko", foreground=RED)
        self.tv.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")

    def fill(self, rows):
        """rows = [(values, tag), ...]"""
        self.tv.delete(*self.tv.get_children())
        for values, tag in rows:
            self.tv.insert("", "end", values=values, tags=(tag,) if tag else ())


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
