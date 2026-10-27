"""Reusable widgets: table and form."""

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
