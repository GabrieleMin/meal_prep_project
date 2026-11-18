"""Modal dialogs."""

from __future__ import annotations

import contextlib
from datetime import date
import math
import re
from tkinter import messagebox, ttk
import tkinter as tk
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from meal_planner.gui.support import number
from meal_planner.gui.widgets import Form
from meal_planner.inventory import (
    convert_receipt_quantity_unit,
    get_catalog_for_reconciliation,
    get_inventory_item,
    get_locations,
    get_next_product_id,
    is_product_macro_linked,
    update_inventory_item,
)


def _center_and_lift(dialog: tk.Toplevel, master=None):
    """Centers the dialog relative to master (or screen) and lifts/focuses it."""
    try:
        dialog.update_idletasks()
        if master and hasattr(master, "winfo_ismapped") and master.winfo_ismapped():
            mx, my = master.winfo_rootx(), master.winfo_rooty()
            mw, mh = master.winfo_width(), master.winfo_height()
            dw, dh = dialog.winfo_reqwidth(), dialog.winfo_reqheight()
            x = max(0, mx + (mw - dw) // 2)
            y = max(0, my + (mh - dh) // 2)
            dialog.geometry(f"+{x}+{y}")
        else:
            sw, sh = dialog.winfo_screenwidth(), dialog.winfo_screenheight()
            dw, dh = dialog.winfo_reqwidth(), dialog.winfo_reqheight()
            dialog.geometry(f"+{max(0, (sw - dw) // 2)}+{max(0, (sh - dh) // 2)}")
    except Exception:
        pass
    try:
        dialog.lift()
        dialog.focus_force()
    except Exception:
        pass


class BaseDialog(tk.Toplevel):
    """Standard base dialog providing window lifecycle, centering, and DB context."""

    def __init__(self, master=None, title="", wait=True, minsize=None, resizable=(True, True)):
        super().__init__(master)
        if title:
            self.title(title)
        if master:
            self.transient(master)
        self.resizable(*resizable)
        if minsize:
            self.minsize(*minsize)
        self.result = None
        self.last_error = None
        self._wait = wait
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.bind("<Escape>", lambda e: self._on_cancel())

    def _finish_init(self, master=None, wait=True):
        _center_and_lift(self, master)
        try:
            self.grab_set()
        except Exception:
            pass
        if wait:
            self.wait_window(self)

    @contextlib.contextmanager
    def _get_conn(self):
        from meal_planner.db.connection import get_connection
        conn = get_connection(getattr(self, "db_path", None))
        try:
            yield conn
        finally:
            conn.close()

    def _on_cancel(self):
        self.result = None
        self.destroy()

    def cancel(self):
        self._on_cancel()

    def save(self) -> bool:
        return self._on_save() if hasattr(self, "_on_save") else False

    on_save = save
    on_cancel = cancel


class ProductDialog(BaseDialog):
    """Asks for the data of a product not yet in the archive."""

    def __init__(self, master, name, suggested_id):
        super().__init__(master, "New product", wait=False, resizable=(False, False))
        ttk.Label(self, wraplength=380, padding=10, text=(
            f"'{name}' is not in the archive yet.\nEnter the ID: if it already exists the name becomes an alias "
            "of that product; if it is new, also fill in unit and values per 100 g.")).pack()
        self.form = Form(self, "", [("id", "Product ID", None), ("unit", "Unit", ["kg", "L", "pcs"]),
                                    ("kcal", "Kcal", None), ("prot", "Protein (g)", None),
                                    ("carb", "Carbs (g)", None), ("fat", "Fat (g)", None)],
                         "Confirm", self.confirm)
        self.form.pack(padx=10, pady=(0, 10))
        self.form.v["id"].set(suggested_id)
        self._finish_init(master, wait=True)

    def confirm(self):
        v = self.form.read()
        try:
            r = {"fallback_id": int(v["id"])}
            if v["unit"]:
                r["new_product_unit"] = v["unit"]
            if all(v[k] for k in ("kcal", "prot", "carb", "fat")):
                r["new_product_macros"] = {"Kcal_100g": number(v["kcal"]), "Protein_100g": number(v["prot"]),
                                             "Carbs_100g": number(v["carb"]), "Fat_100g": number(v["fat"])}
        except ValueError as e:
            messagebox.showerror("Invalid data", str(e), parent=self)
            return
        self.result = r
        self.destroy()


class ChoiceDialog(BaseDialog):
    """Asks the user to pick a value from a list."""

    def __init__(self, master, title, values):
        super().__init__(master, title, wait=False, resizable=(False, False))
        self.v = tk.StringVar()
        ttk.Label(self, text=title, padding=10).pack()
        ttk.Combobox(self, textvariable=self.v, values=values, state="readonly", width=34).pack(padx=10)
        ttk.Button(self, text="Confirm", command=self.confirm).pack(pady=10)
        self._finish_init(master, wait=True)

    def confirm(self):
        self.result = self.v.get() or None
        self.destroy()


class AliasMapDialog(BaseDialog):
    """Lets the user map unrecognized receipt labels (aliases) to existing DB products."""

    def __init__(self, parent, items: list, known_names: list):
        super().__init__(parent, "Map Receipt Items to Database Products", wait=False)
        self.result = {}
        ttk.Label(self, text="Map unrecognized receipt items to known products:", padding=(10, 8)).pack(anchor="w")
        frame = ttk.Frame(self, padding=10)
        frame.pack(fill="both", expand=True)

        self._vars = {}
        for i, item in enumerate(items):
            ttk.Label(frame, text=item, width=25, anchor="w").grid(row=i, column=0, sticky="w", pady=2)
            var = tk.StringVar(value="(skip)")
            cb = ttk.Combobox(frame, textvariable=var, values=["(skip)"] + known_names, width=30)
            cb.grid(row=i, column=1, sticky="ew", padx=(8, 0), pady=2)
            self._vars[item] = var
        frame.columnconfigure(1, weight=1)

        btn_frame = ttk.Frame(self, padding=(10, 0, 10, 10))
        btn_frame.pack(fill="x")
        ttk.Button(btn_frame, text="OK", command=self._ok).pack(side="right")
        ttk.Button(btn_frame, text="Cancel", command=self._on_cancel).pack(side="right", padx=(0, 6))
        self._finish_init(parent, wait=True)

    def _ok(self):
        self.result = {lbl: (var.get() if var.get() != "(skip)" else None) for lbl, var in self._vars.items()}
        self.destroy()


class MealPrepDialog(BaseDialog):
    """Modal dialog to schedule an advance meal prep."""

    def __init__(self, parent, recipes: list, default_cook_date: str = None):
        super().__init__(parent, "Advance Meal Prep", wait=False)
        self.recipes = recipes
        self._name_to_id = {name: rid for rid, name in recipes}
        ttk.Label(self, text="Schedule cooking ahead for future meals:", padding=(12, 10)).pack(anchor="w")
        frame = ttk.Frame(self, padding=12)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="Cook on date:").grid(row=0, column=0, sticky="w", pady=4)
        self.v_cook = tk.StringVar(value=default_cook_date or date.today().isoformat())
        ttk.Entry(frame, textvariable=self.v_cook, width=20).grid(row=0, column=1, sticky="w", padx=(8, 0), pady=4)

        ttk.Label(frame, text="Recipe:").grid(row=1, column=0, sticky="w", pady=4)
        self.v_recipe = tk.StringVar()
        recipe_names = [name for _, name in recipes]
        self.cb_recipe = ttk.Combobox(frame, textvariable=self.v_recipe, values=recipe_names, state="readonly", width=35)
        if recipe_names:
            self.cb_recipe.current(0)
        self.cb_recipe.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=4)

        ttk.Label(frame, text="For meals on dates\n(comma-separated):").grid(row=2, column=0, sticky="w", pady=4)
        self.v_dates = tk.StringVar()
        ttk.Entry(frame, textvariable=self.v_dates, width=35).grid(row=2, column=1, sticky="ew", padx=(8, 0), pady=4)
        ttk.Label(frame, text="e.g. 2026-11-04, 2026-11-06", font=("TkDefaultFont", 8, "italic")).grid(row=3, column=1, sticky="w", padx=(8, 0))
        frame.columnconfigure(1, weight=1)

        btn_frame = ttk.Frame(self, padding=(12, 10))
        btn_frame.pack(fill="x")
        ttk.Button(btn_frame, text="Schedule Prep", command=self._ok).pack(side="right")
        ttk.Button(btn_frame, text="Cancel", command=self._on_cancel).pack(side="right", padx=(0, 6))
        self._finish_init(parent, wait=True)

    def _ok(self):
        cook_date = self.v_cook.get().strip()
        recipe_name = self.v_recipe.get().strip()
        dates_raw = self.v_dates.get().strip()
        if not cook_date:
            messagebox.showerror("Validation Error", "Cook date is required.", parent=self)
            return
        try:
            date.fromisoformat(cook_date)
        except ValueError:
            messagebox.showerror("Validation Error", "Cook date must be in YYYY-MM-DD format.", parent=self)
            return
        recipe_id = self._name_to_id.get(recipe_name)
        if not recipe_id:
            messagebox.showerror("Validation Error", "Please select a valid recipe.", parent=self)
            return
        target_dates = [d.strip() for d in dates_raw.split(",") if d.strip()]
        if not target_dates:
            messagebox.showerror("Validation Error", "At least one target date is required.", parent=self)
            return
        for td in target_dates:
            try:
                date.fromisoformat(td)
            except ValueError:
                messagebox.showerror("Validation Error", f"Target date '{td}' is not valid YYYY-MM-DD.", parent=self)
                return
        self.result = {"recipe_id": recipe_id, "cook_date": cook_date, "target_dates": target_dates}
        self.destroy()


COLOR_RECOGNIZED = "#30a46c"
COLOR_UNRECOGNIZED = "#f5a524"
_RECONCILIATION_FLASH_STYLES = {}


class RoundedEntry(ttk.Entry):
    """Fluent / sv_ttk rounded entry matching the modern application theme."""

    def __init__(self, master=None, dialog=None, **kwargs):
        self._dialog = dialog
        self._bg = kwargs.pop("bg", kwargs.pop("background", getattr(dialog, "color_std", "#2b2b2b")))
        self._fg = kwargs.pop("fg", kwargs.pop("foreground", None))
        for k in ("insertbackground", "relief", "bd", "highlightthickness", "highlightbackground", "highlightcolor", "font"):
            kwargs.pop(k, None)
        super().__init__(master, **kwargs)

    def cget(self, key):
        if key in ("bg", "background"):
            return self._bg
        if key in ("fg", "foreground"):
            return self._fg
        return super().cget(key)

    def configure(self, cnf=None, **kwargs):
        if "bg" in kwargs:
            self._bg = kwargs.pop("bg")
            self._apply_bg_style()
        if "background" in kwargs:
            self._bg = kwargs.pop("background")
            self._apply_bg_style()
        if "fg" in kwargs or "foreground" in kwargs:
            self._fg = kwargs.pop("fg", kwargs.pop("foreground", None))
        if kwargs:
            super().configure(cnf, **kwargs)

    def _apply_bg_style(self):
        if not self._dialog:
            return
        if self._bg == getattr(self._dialog, "color_std", None):
            try:
                super().configure(style="TEntry")
            except Exception:
                pass
        elif self._bg == getattr(self._dialog, "color_yellow", None):
            try:
                super().configure(style="RecFlash_25.TEntry")
            except Exception:
                pass

    __getitem__ = cget
    __setitem__ = configure


class ReceiptReconciliationDialog(BaseDialog):
    """Modal dialog to reconcile receipt items with the product catalog and inventory."""

    def __init__(self, master, items: List[Any], db_path: Optional[str] = None, wait: bool = True):
        super().__init__(master, "Receipt Reconciliation - Review & Inventory Import", wait=False, minsize=(860, 420))
        self.geometry("1180x620")
        self.db_path = db_path

        from meal_planner.gui import theme
        current_theme = getattr(master, "theme", None) or theme.load()
        self.is_dark = (current_theme == "dark")
        bg_std, fg_std = theme.palette(current_theme)
        self.color_std = bg_std
        self.color_fg = fg_std
        self.color_red = "#991b1b" if self.is_dark else "#ef4444"
        self.color_yellow = "#524314" if self.is_dark else "#fef9c3"

        self._active_error_widgets = set()
        self._anim_timer_id = None
        self._init_rounded_styles()
        self._convert_unit = convert_receipt_quantity_unit

        try:
            self.catalog = get_catalog_for_reconciliation(db_path=self.db_path)
        except Exception:
            self.catalog = {"products": [], "alias_map": {}}
        self.product_by_name = {p["canonical_name"]: p for p in self.catalog.get("products", [])}
        self.alias_map = self.catalog.get("alias_map", {})

        try:
            self.locations = get_locations(db_path=self.db_path) or ["freezer", "fridge", "pantry"]
        except Exception:
            self.locations = ["freezer", "fridge", "pantry"]

        try:
            self.next_product_id = get_next_product_id(db_path=self.db_path)
        except Exception:
            self.next_product_id = 1

        self.rows = []
        self._rows = self.rows
        self._setup_ui(items)
        self._finish_init(master, wait=wait)

    def destroy(self):
        if hasattr(self, "_anim_timer_id") and self._anim_timer_id is not None:
            try:
                self.after_cancel(self._anim_timer_id)
            except Exception:
                pass
            self._anim_timer_id = None
        if hasattr(self, "_style_photos"):
            self._style_photos = []
        super().destroy()

    def _init_rounded_styles(self):
        cache_key = (id(self.tk), bool(self.is_dark))
        if cache_key in _RECONCILIATION_FLASH_STYLES:
            self._style_photos = _RECONCILIATION_FLASH_STYLES[cache_key]
            return
        self._style_photos = []
        try:
            from PIL import Image, ImageDraw, ImageTk
            style = ttk.Style(self)
            theme_tag = "dark" if self.is_dark else "light"
            for i in range(26):
                if i == 0:
                    fill, border = self.color_std, "#444444" if self.is_dark else "#cccccc"
                elif i <= 12:
                    t = i / 12.0
                    fill = self._interpolate_hex(self.color_std, self.color_red, t)
                    border = self._interpolate_hex("#444444" if self.is_dark else "#cccccc", "#ef4444" if self.is_dark else "#b91c1c", t)
                else:
                    t = (i - 12) / 13.0
                    fill = self._interpolate_hex(self.color_red, self.color_yellow, t)
                    border = self._interpolate_hex("#ef4444" if self.is_dark else "#b91c1c", "#eab308" if self.is_dark else "#ca8a04", t)

                im = Image.new("RGBA", (36, 26), (0, 0, 0, 0))
                d = ImageDraw.Draw(im)
                d.rounded_rectangle((1, 1, 34, 24), radius=5, fill=fill, outline=border, width=1)
                ph = ImageTk.PhotoImage(im)
                self._style_photos.append(ph)

                elem_name = f"RecFlash_{theme_tag}_{i}.field"
                style_name = f"RecFlash_{i}.TEntry"
                try:
                    self.tk.call("ttk::style", "element", "create", elem_name, "image", ph, "-border", 6, "-sticky", "nsew")
                except tk.TclError:
                    pass
                try:
                    style.layout(style_name, [(elem_name, {"sticky": "nswe", "children": [("Entry.padding", {"sticky": "nswe", "children": [("Entry.textarea", {"sticky": "nswe"})]})]})])
                    style.configure(style_name, padding=(6, 1, 4, 2))
                except Exception:
                    pass
            _RECONCILIATION_FLASH_STYLES[cache_key] = self._style_photos
        except Exception:
            pass

    def _create_entry(self, textvariable, width: int, justify: str = "left"):
        return RoundedEntry(self.table_frame, dialog=self, textvariable=textvariable, width=width, justify=justify,
                            bg=self.color_std, fg=self.color_fg)

    @staticmethod
    def _interpolate_hex(c1: str, c2: str, t: float) -> str:
        t = max(0.0, min(1.0, t))
        try:
            r1, g1, b1 = int(c1[1:3], 16), int(c1[3:5], 16), int(c1[5:7], 16)
            r2, g2, b2 = int(c2[1:3], 16), int(c2[3:5], 16), int(c2[5:7], 16)
            return f"#{int(r1 + (r2 - r1) * t):02x}{int(g1 + (g2 - g1) * t):02x}{int(b1 + (b2 - b1) * t):02x}"
        except Exception:
            return c2

    @staticmethod
    def _set_widget_bg(widget, color: str):
        for attr in ("bg", "background"):
            try:
                widget.configure(**{attr: color})
                break
            except Exception:
                pass

    def _flash_error_widgets(self, widgets):
        if self._anim_timer_id is not None:
            try:
                self.after_cancel(self._anim_timer_id)
            except Exception:
                pass
            self._anim_timer_id = None
        if not widgets:
            return

        def _step(current_step):
            if not self.winfo_exists():
                return
            t = (current_step / 12.0) if current_step <= 12 else ((current_step - 12) / 13.0)
            c_from, c_to = (self.color_std, self.color_red) if current_step <= 12 else (self.color_red, self.color_yellow)
            hex_col = self._interpolate_hex(c_from, c_to, t)

            for w in widgets:
                if hasattr(w, "winfo_exists") and w.winfo_exists():
                    self._set_widget_bg(w, hex_col)
                    if isinstance(w, RoundedEntry):
                        try:
                            w.configure(style=f"RecFlash_{current_step}.TEntry")
                        except Exception:
                            pass

            if current_step < 25:
                self._anim_timer_id = self.after(40, lambda: _step(current_step + 1))
            else:
                self._anim_timer_id = None
                for w in widgets:
                    if hasattr(w, "winfo_exists") and w.winfo_exists():
                        self._set_widget_bg(w, self.color_yellow)
                        if isinstance(w, RoundedEntry):
                            try:
                                w.configure(style="RecFlash_25.TEntry")
                            except Exception:
                                pass

        _step(0)

    def _setup_ui(self, items: list):
        header = ttk.Frame(self, padding=(14, 10, 14, 4))
        header.pack(fill="x")
        ttk.Label(header, text="Receipt Items Reconciliation", font=("TkDefaultFont", 11, "bold")).pack(anchor="w")
        ttk.Label(header, text="Review parsed receipt items below. Map items to existing products or define new products with nutritional values.").pack(anchor="w", pady=(2, 6))

        legend = ttk.Frame(header)
        legend.pack(anchor="w", pady=(0, 4))
        for col, txt in ((COLOR_RECOGNIZED, " Recognized / Catalog product    "), (COLOR_UNRECOGNIZED, " Unrecognized / New product (requires macros)")):
            c = tk.Canvas(legend, width=16, height=16, highlightthickness=0, bg=self.color_std)
            c.create_oval(2, 2, 14, 14, fill=col, outline=col)
            c.pack(side="left")
            ttk.Label(legend, text=txt).pack(side="left")

        container = ttk.Frame(self, style="Card.TFrame", padding=6)
        container.pack(fill="both", expand=True, padx=12, pady=4)
        bg = None
        try:
            bg = ttk.Style(self).lookup("TFrame", "background")
        except Exception:
            pass
        self.canvas = tk.Canvas(container, highlightthickness=0, background=bg or self.color_std)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.table_frame = ttk.Frame(self.canvas)
        self.canvas_win = self.canvas.create_window((0, 0), window=self.table_frame, anchor="nw")
        self.table_frame.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfig(self.canvas_win, width=max(e.width, self.table_frame.winfo_reqwidth())) if self.winfo_exists() else None)

        for w in (self, container, self.canvas, self.table_frame):
            for ev, d in (("<Button-4>", -1), ("<Button-5>", 1)):
                w.bind(ev, lambda e, delta=d: self.canvas.yview_scroll(delta, "units") if self.winfo_exists() else None, add="+")
            w.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(-1 if e.delta > 0 else 1, "units") if self.winfo_exists() else None, add="+")

        headers = [("", 2), ("Receipt Name", 18), ("Catalog Product", 22), ("Inventory Name", 18), ("Unit", 5),
                   ("Weight/Vol", 8), ("Packs", 5), ("Kcal", 6), ("Prot (g)", 6), ("Carb (g)", 6), ("Fat (g)", 6),
                   ("Location", 10), ("Expiry (YYYY-MM-DD)", 12)]
        for col_idx, (text, width) in enumerate(headers):
            ttk.Label(self.table_frame, text=text, font=("TkDefaultFont", 9, "bold")).grid(row=0, column=col_idx, padx=3, pady=(2, 6), sticky="w")

        catalog_names = sorted(list(self.product_by_name.keys()), key=lambda s: s.lower())
        self.product_options = ["[New Product]"] + catalog_names

        for row_idx, item in enumerate(items, start=1):
            self._create_row(row_idx, item)

        btn_frame = ttk.Frame(self, padding=(14, 10))
        btn_frame.pack(fill="x", side="bottom")
        self.btn_cancel = ttk.Button(btn_frame, text="Cancel", command=self._on_cancel)
        self.btn_cancel.pack(side="right", padx=(6, 0))
        self.btn_save = ttk.Button(btn_frame, text="Save to Database", style="Accent.TButton", command=self._on_save)
        self.btn_save.pack(side="right")

    def _create_row(self, row_idx: int, item: Any):
        if isinstance(item, (tuple, list)):
            raw_name = str(item[0]) if len(item) > 0 else ""
            raw_qty = item[1] if len(item) > 1 else 1.0
            raw_unit = str(item[2]) if len(item) > 2 else "pcs"
            raw_packs = item[3] if len(item) > 3 else 1
        elif isinstance(item, dict):
            raw_name = str(item.get("name", item.get("nome", "")))
            raw_qty = item.get("quantity", item.get("qta", 1.0))
            raw_unit = str(item.get("unit", item.get("unita", "pcs")))
            raw_packs = item.get("packages", item.get("confezioni", 1))
        else:
            raw_name = getattr(item, "nome", getattr(item, "name", str(item)))
            raw_qty = getattr(item, "qta", getattr(item, "quantity", 1.0))
            raw_unit = getattr(item, "unita", getattr(item, "unit", "pcs"))
            raw_packs = getattr(item, "confezioni", getattr(item, "packages", 1))

        try: q_val = float(str(raw_qty).replace(",", "."))
        except Exception: q_val = 1.0
        try: packs_val = int(raw_packs)
        except Exception: packs_val = 1

        matched_prod = self.alias_map.get(raw_name.strip().lower())
        target_u = matched_prod.get("unit") if matched_prod else None
        g_piece = matched_prod.get("grams_per_piece") if matched_prod else None
        dens = matched_prod.get("density_kg_per_l") if matched_prod else None

        try:
            norm_qty, norm_unit = self._convert_unit(q_val, str(raw_unit), target_unit=target_u, grams_per_piece=g_piece, density_kg_per_l=dens)
        except Exception:
            try: norm_qty, norm_unit = self._convert_unit(q_val, str(raw_unit))
            except Exception:
                norm_qty = q_val
                u_str = str(raw_unit).strip().lower()
                norm_unit = "kg" if u_str in ("kg", "kgs", "g", "gr") else ("L" if u_str in ("l", "lt", "liter", "litro", "ml", "cl") else "pcs")

        is_rec = (matched_prod is not None)
        status_canvas = tk.Canvas(self.table_frame, width=16, height=16, highlightthickness=0, bg=self.color_std)
        rect_id = status_canvas.create_oval(2, 2, 14, 14, fill=COLOR_RECOGNIZED if is_rec else COLOR_UNRECOGNIZED,
                                            outline=COLOR_RECOGNIZED if is_rec else COLOR_UNRECOGNIZED)
        status_canvas.grid(row=row_idx, column=0, padx=2, pady=3)

        lbl_raw = ttk.Label(self.table_frame, text=raw_name, width=18, anchor="w")
        lbl_raw.grid(row=row_idx, column=1, padx=3, pady=3, sticky="w")

        initial_prod_choice = matched_prod["canonical_name"] if is_rec else "[New Product]"
        product_var = tk.StringVar(value=initial_prod_choice)
        cb_prod = ttk.Combobox(self.table_frame, textvariable=product_var, values=self.product_options, state="readonly", width=22)
        cb_prod.grid(row=row_idx, column=2, padx=3, pady=3, sticky="ew")

        name_var = tk.StringVar(value=raw_name.strip())
        ent_name = self._create_entry(name_var, width=18)
        ent_name.grid(row=row_idx, column=3, padx=3, pady=3, sticky="ew")

        frame_unit = tk.Frame(self.table_frame, bg=self.color_std, padx=1, pady=1)
        frame_unit.grid(row=row_idx, column=4, padx=3, pady=3, sticky="w")
        unit_var = tk.StringVar(value=norm_unit)
        cb_unit = ttk.Combobox(frame_unit, textvariable=unit_var, values=["kg", "L", "pcs"], state="readonly", width=5)
        cb_unit.pack()

        weight_var = tk.StringVar(value=f"{norm_qty:g}")
        ent_weight = self._create_entry(weight_var, width=8, justify="right")
        ent_weight.grid(row=row_idx, column=5, padx=3, pady=3, sticky="ew")

        packs_var = tk.StringVar(value=str(packs_val))
        ent_packs = self._create_entry(packs_var, width=5, justify="right")
        ent_packs.grid(row=row_idx, column=6, padx=3, pady=3, sticky="ew")

        macro_vars = {}
        macro_ents = {}
        for c_idx, key in enumerate(("kcal", "protein", "carbs", "fat"), start=7):
            val = f"{matched_prod[key]:g}" if is_rec and matched_prod.get(key) is not None else ""
            var_name = f"{'prot' if key=='protein' else ('carb' if key=='carbs' else key)}_var"
            ent_name_k = f"ent_{'prot' if key=='protein' else ('carb' if key=='carbs' else key)}"
            v = tk.StringVar(value=val)
            e = self._create_entry(v, width=6, justify="right")
            e.grid(row=row_idx, column=c_idx, padx=3, pady=3, sticky="ew")
            macro_vars[var_name] = v
            macro_ents[ent_name_k] = e

        def_loc = "pantry" if "pantry" in self.locations else (self.locations[0] if self.locations else "pantry")
        loc_var = tk.StringVar(value=def_loc)
        cb_loc = ttk.Combobox(self.table_frame, textvariable=loc_var, values=self.locations, state="readonly", width=10)
        cb_loc.grid(row=row_idx, column=11, padx=3, pady=3, sticky="ew")

        exp_var = tk.StringVar(value="")
        ent_exp = self._create_entry(exp_var, width=12)
        ent_exp.grid(row=row_idx, column=12, padx=3, pady=3, sticky="ew")

        row_record = {
            "status_canvas": status_canvas, "canvas": status_canvas, "rect_id": rect_id, "lbl_raw": lbl_raw,
            "product_var": product_var, "cb_prod": cb_prod, "name_var": name_var, "ent_name": ent_name,
            "unit_var": unit_var, "frame_unit": frame_unit, "cb_unit": cb_unit, "weight_var": weight_var,
            "ent_weight": ent_weight, "packs_var": packs_var, "ent_packs": ent_packs,
            **macro_vars, **macro_ents,
            "loc_var": loc_var, "cb_loc": cb_loc, "exp_var": exp_var, "ent_exp": ent_exp,
            "raw_name": raw_name, "raw_qty": raw_qty, "raw_unit": raw_unit, "raw_packs": raw_packs,
            "status": COLOR_RECOGNIZED if is_rec else COLOR_UNRECOGNIZED, "_last_choice": initial_prod_choice,
        }
        self.rows.append(row_record)

        cb_prod.bind("<<ComboboxSelected>>", lambda e, r=row_record: self._on_product_change(r))
        product_var.trace_add("write", lambda *args, r=row_record: self._on_product_change(r))
        for v in (row_record["kcal_var"], row_record["prot_var"], row_record["carb_var"], row_record["fat_var"]):
            v.trace_add("write", lambda *args, r=row_record: self._update_row_status(r))

    def _update_row_status(self, row: dict):
        choice = row["product_var"].get().strip()
        matched = self.product_by_name.get(choice) or self.alias_map.get(choice.lower())
        color = COLOR_UNRECOGNIZED
        if choice != "[New Product]" and matched is not None:
            color = COLOR_RECOGNIZED
        else:
            vals = [row[k].get().strip().replace(",", ".") for k in ("kcal_var", "prot_var", "carb_var", "fat_var")]
            if all(vals):
                try:
                    fvals = [float(v) for v in vals]
                    if all(math.isfinite(v) and v >= 0 for v in fvals):
                        color = COLOR_RECOGNIZED
                except ValueError:
                    pass
        row["status_canvas"].itemconfig(row["rect_id"], fill=color, outline=color)
        row["status"] = color

    def _on_product_change(self, row: dict):
        last_choice = row.get("_last_choice")
        choice = row["product_var"].get().strip()
        row["_last_choice"] = choice
        prod = self.product_by_name.get(choice) or self.alias_map.get(choice.lower())
        if choice != "[New Product]" and prod is not None:
            if prod.get("unit"):
                row["unit_var"].set(prod["unit"])
            try:
                raw_q = float(str(row.get("raw_qty", 1.0)).replace(",", "."))
                c_qty, c_u = self._convert_unit(raw_q, str(row.get("raw_unit", "pcs")), target_unit=prod.get("unit"),
                                                grams_per_piece=prod.get("grams_per_piece"), density_kg_per_l=prod.get("density_kg_per_l"))
                row["weight_var"].set(f"{c_qty:g}")
                if c_u: row["unit_var"].set(c_u)
            except Exception:
                pass
            for k, vk in (("kcal", "kcal_var"), ("protein", "prot_var"), ("carbs", "carb_var"), ("fat", "fat_var")):
                row[vk].set(f"{prod[k]:g}" if prod.get(k) is not None else "")
        elif choice == "[New Product]" and last_choice is not None and last_choice != "[New Product]":
            for vk in ("kcal_var", "prot_var", "carb_var", "fat_var"):
                row[vk].set("")
            try:
                raw_q = float(str(row.get("raw_qty", 1.0)).replace(",", "."))
                orig_qty, orig_u = self._convert_unit(raw_q, str(row.get("raw_unit", "pcs")))
                row["weight_var"].set(f"{orig_qty:g}")
                row["unit_var"].set(orig_u)
            except Exception:
                pass
        self._update_row_status(row)

    def _on_save(self) -> bool:
        errors = []
        result = []
        assigned_new_ids = {}
        current_error_widgets = set()
        try: base_id = get_next_product_id(db_path=self.db_path)
        except Exception: base_id = getattr(self, "next_product_id", 1) or 1

        for i, row in enumerate(self.rows, start=1):
            row_has_err = False
            inv_name = row["name_var"].get().strip()
            if not inv_name:
                errors.append(f"Row {i}: Name cannot be empty.")
                current_error_widgets.add(row["ent_name"])
                row_has_err = True

            unit = row["unit_var"].get().strip()
            if unit not in ("kg", "L", "pcs"):
                errors.append(f"Row {i} ({inv_name or 'item'}): Unit must be 'kg', 'L', or 'pcs'.")
                current_error_widgets.add(row.get("frame_unit", row.get("cb_unit")))
                row_has_err = True

            try:
                weight_val = float(row["weight_var"].get().strip().replace(",", "."))
                if not math.isfinite(weight_val) or weight_val <= 0: raise ValueError
            except ValueError:
                errors.append(f"Row {i} ({inv_name or 'item'}): Weight/Volume must be a positive number.")
                current_error_widgets.add(row["ent_weight"])
                row_has_err = True
                weight_val = 1.0

            try:
                pf = float(row["packs_var"].get().strip().replace(",", "."))
                if not math.isfinite(pf) or not pf.is_integer() or int(pf) < 1: raise ValueError
                packs_val = int(pf)
            except ValueError:
                errors.append(f"Row {i} ({inv_name or 'item'}): Packages must be an integer >= 1.")
                current_error_widgets.add(row["ent_packs"])
                row_has_err = True
                packs_val = 1

            exp_str = row["exp_var"].get().strip()
            exp_date = None
            if exp_str:
                if not re.match(r"^\d{4}-\d{2}-\d{2}$", exp_str):
                    errors.append(f"Row {i} ({inv_name or 'item'}): Expiration date must be YYYY-MM-DD (e.g. 2026-12-31).")
                    current_error_widgets.add(row["ent_exp"])
                    row_has_err = True
                else:
                    try:
                        date.fromisoformat(exp_str)
                        exp_date = exp_str
                    except ValueError:
                        errors.append(f"Row {i} ({inv_name or 'item'}): Invalid date '{exp_str}'.")
                        current_error_widgets.add(row["ent_exp"])
                        row_has_err = True

            loc = row["loc_var"].get().strip() or "pantry"
            prod_choice = row["product_var"].get().strip()
            matched_prod = self.product_by_name.get(prod_choice) or self.alias_map.get(prod_choice.lower())

            if prod_choice != "[New Product]" and matched_prod is not None:
                is_new, canonical_name, fallback_id, new_unit, new_macros = False, matched_prod["canonical_name"], matched_prod["id"], None, None
            else:
                is_new, canonical_name = True, inv_name
                macro_err = False
                for vk, ek in (("kcal_var", "ent_kcal"), ("prot_var", "ent_prot"), ("carb_var", "ent_carb"), ("fat_var", "ent_fat")):
                    val_str = row[vk].get().strip().replace(",", ".")
                    try:
                        if not val_str or not math.isfinite(float(val_str)) or float(val_str) < 0: raise ValueError
                    except ValueError:
                        current_error_widgets.add(row[ek])
                        macro_err = True
                if macro_err:
                    errors.append(f"Row {i} ({inv_name or 'item'}): For new products, nutritional macros (Kcal, Protein, Carbs, Fat) per 100g must be valid numbers >= 0.")
                    row_has_err = True
                else:
                    new_unit = unit
                    new_macros = {
                        "Kcal_100g": float(row["kcal_var"].get().strip().replace(",", ".")),
                        "Protein_100g": float(row["prot_var"].get().strip().replace(",", ".")),
                        "Carbs_100g": float(row["carb_var"].get().strip().replace(",", ".")),
                        "Fat_100g": float(row["fat_var"].get().strip().replace(",", ".")),
                    }
                    norm_k = inv_name.lower()
                    if norm_k in assigned_new_ids:
                        fallback_id = assigned_new_ids[norm_k]
                    else:
                        fallback_id = base_id + len(assigned_new_ids)
                        assigned_new_ids[norm_k] = fallback_id

            if not row_has_err:
                unit_w = 1.0 if unit == "pcs" else weight_val
                intact = max(1, int(round(weight_val * packs_val))) if unit == "pcs" else packs_val
                result.append({
                    "name": inv_name, "canonical_name": canonical_name, "unit_weight": unit_w,
                    "intact_items": intact, "location": loc, "expiration_date": exp_date,
                    "fallback_id": fallback_id, "new_product_unit": new_unit, "new_product_macros": new_macros,
                    "is_new_product": is_new,
                })

        for w in getattr(self, "_active_error_widgets", set()) - current_error_widgets:
            if hasattr(w, "winfo_exists") and w.winfo_exists():
                self._set_widget_bg(w, self.color_std)
                if isinstance(w, RoundedEntry):
                    try: w.configure(style="TEntry")
                    except Exception: pass

        if errors or current_error_widgets:
            self.last_error = "\n".join(errors)
            self._active_error_widgets = current_error_widgets
            self._flash_error_widgets(current_error_widgets)
            return False

        for w in self._active_error_widgets:
            if hasattr(w, "winfo_exists") and w.winfo_exists():
                self._set_widget_bg(w, self.color_std)
                if isinstance(w, RoundedEntry):
                    try: w.configure(style="TEntry")
                    except Exception: pass
        self._active_error_widgets.clear()
        self.last_error = None
        self.result = result
        self.destroy()
        return True


class MacroPromptDialog(BaseDialog):
    """Prompt dialog shown when modifying macronutrients of a product linked across instances."""

    def __init__(self, parent, product_name: str, product_id: int = None, wait: bool = True):
        super().__init__(parent, "Macronutrient Modification", wait=False, resizable=(False, False))
        c = ttk.Frame(self, padding=16)
        c.pack(fill="both", expand=True)
        ttk.Label(c, text=f"Modify Macronutrients for '{product_name}'", font=("TkDefaultFont", 11, "bold"), wraplength=440).pack(anchor="w", pady=(0, 8))
        ttk.Label(c, text="This product's macronutrients are linked across multiple items in the database.\nPlease choose how to apply your changes:", wraplength=440).pack(anchor="w", pady=(0, 14))

        f1 = ttk.LabelFrame(c, text="Option 1: Modify only that specific item", padding=10)
        f1.pack(fill="x", pady=4)
        ttk.Button(f1, text="Modify only that specific item", command=self._choose_single).pack(anchor="w", pady=(0, 4))
        ttk.Label(f1, text="Allocates a new product ID. Updates only this inventory item.", font=("TkDefaultFont", 8, "italic"), wraplength=420).pack(anchor="w")

        f2 = ttk.LabelFrame(c, text="Option 2: Propagate modification backwards across all linked instances", padding=10)
        f2.pack(fill="x", pady=6)
        ttk.Button(f2, text="Propagate the modification backwards across all linked instances", command=self._choose_propagate).pack(anchor="w", pady=(0, 4))
        ttk.Label(f2, text="Updates the product record in place, preserving its product ID.", font=("TkDefaultFont", 8, "italic"), wraplength=420).pack(anchor="w")

        b_bar = ttk.Frame(c, padding=(0, 10, 0, 0))
        b_bar.pack(fill="x")
        ttk.Button(b_bar, text="Cancel", command=self._cancel).pack(side="right")
        self._finish_init(parent, wait=wait)

    def _choose_single(self):
        self.result = "single"
        self.destroy()

    def _choose_propagate(self):
        self.result = "propagate"
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()

    choose_single = _choose_single
    choose_propagate = _choose_propagate


class InventoryItemEditDialog(BaseDialog):
    """Modal dialog allowing the user to view and edit all characteristics of an inventory item."""

    def __init__(self, parent, row_id: int, db_path: str = None, wait: bool = True):
        super().__init__(parent, "Edit Inventory Item", wait=False)
        self.row_id = row_id
        self.db_path = db_path
        self.item_data = get_inventory_item(self.row_id, db_path=self.db_path)
        if not self.item_data:
            messagebox.showerror("Item Not Found", f"Inventory row #{row_id} does not exist.", parent=self)
            self.destroy()
            return
        self._init_variables()
        self._init_ui()
        self._finish_init(parent, wait=wait)

    def _init_variables(self):
        d = self.item_data
        self.v_name = tk.StringVar(value=d.get("Name") or "")
        self.v_location = tk.StringVar(value=d.get("Location") or "")
        self.v_expiration = tk.StringVar(value=d.get("Expiration_Date") or "")
        self.v_unit_weight = tk.StringVar(value=f"{d['Unit_Weight']:g}" if d.get("Unit_Weight") is not None else "")
        self.v_intact = tk.StringVar(value=str(d.get("Intact_Items", 0)))
        self.v_remainder = tk.StringVar(value=f"{d['Open_Remainder']:g}" if d.get("Open_Remainder") is not None else "0")
        self.v_unit = tk.StringVar(value=d.get("Unit") or "kg")

        for key, var_name in (
            ("Kcal_100g", "v_kcal"), ("Protein_100g", "v_protein"), ("Carbs_100g", "v_carbs"),
            ("Fat_100g", "v_fat"), ("Sugars_100g", "v_sugars"), ("Saturated_fat_100g", "v_saturated_fat"),
            ("Fiber_100g", "v_fiber"), ("Salt_100g", "v_salt"), ("Grams_per_piece", "v_grams_per_piece"),
            ("Density_kg_per_L", "v_density"),
        ):
            val = f"{d[key]:g}" if d.get(key) is not None else ""
            setattr(self, var_name, tk.StringVar(value=val))

    def _init_ui(self):
        main = ttk.Frame(self, padding=14)
        main.pack(fill="both", expand=True)
        ttk.Label(main, text=f"Editing Inventory Item #{self.row_id} — {self.item_data.get('Name')}", font=("TkDefaultFont", 11, "bold")).pack(anchor="w", pady=(0, 12))

        f_inv = ttk.LabelFrame(main, text="Inventory Characteristics", padding=10)
        f_inv.pack(fill="x", pady=(0, 10))
        ttk.Label(f_inv, text="Name:").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(f_inv, textvariable=self.v_name, width=28).grid(row=0, column=1, sticky="w", padx=(6, 16), pady=4)
        ttk.Label(f_inv, text="Location:").grid(row=0, column=2, sticky="w", pady=4)
        try: locs = get_locations(db_path=self.db_path)
        except Exception: locs = ["freezer", "fridge", "pantry"]
        ttk.Combobox(f_inv, textvariable=self.v_location, values=locs, width=20).grid(row=0, column=3, sticky="w", padx=(6, 0), pady=4)

        ttk.Label(f_inv, text="Expiration (YYYY-MM-DD):").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(f_inv, textvariable=self.v_expiration, width=28).grid(row=1, column=1, sticky="w", padx=(6, 16), pady=4)
        ttk.Label(f_inv, text="Package Unit Weight:").grid(row=1, column=2, sticky="w", pady=4)
        ttk.Entry(f_inv, textvariable=self.v_unit_weight, width=20).grid(row=1, column=3, sticky="w", padx=(6, 0), pady=4)

        ttk.Label(f_inv, text="Intact Packages:").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(f_inv, textvariable=self.v_intact, width=28).grid(row=2, column=1, sticky="w", padx=(6, 16), pady=4)
        ttk.Label(f_inv, text="Open Remainder:").grid(row=2, column=2, sticky="w", pady=4)
        ttk.Entry(f_inv, textvariable=self.v_remainder, width=20).grid(row=2, column=3, sticky="w", padx=(6, 0), pady=4)

        f_macro = ttk.LabelFrame(main, text="Product & Macronutrients (per 100g / package unit)", padding=10)
        f_macro.pack(fill="x", pady=(0, 10))
        ttk.Label(f_macro, text="Unit:").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Combobox(f_macro, textvariable=self.v_unit, values=["kg", "L", "pcs"], width=20, state="readonly").grid(row=0, column=1, sticky="w", padx=(6, 16), pady=4)
        ttk.Label(f_macro, text="Kcal (100g):").grid(row=0, column=2, sticky="w", pady=4)
        ttk.Entry(f_macro, textvariable=self.v_kcal, width=20).grid(row=0, column=3, sticky="w", padx=(6, 0), pady=4)

        macro_rows = [
            ("Protein (g):", self.v_protein, "Carbs (g):", self.v_carbs),
            ("Fat (g):", self.v_fat, "Sugars (g):", self.v_sugars),
            ("Saturated Fat (g):", self.v_saturated_fat, "Fiber (g):", self.v_fiber),
            ("Salt (g):", self.v_salt, "Grams / piece:", self.v_grams_per_piece),
            ("Density (kg/L):", self.v_density, "", None),
        ]
        for r_idx, (l1, v1, l2, v2) in enumerate(macro_rows, start=1):
            ttk.Label(f_macro, text=l1).grid(row=r_idx, column=0, sticky="w", pady=4)
            ttk.Entry(f_macro, textvariable=v1, width=20).grid(row=r_idx, column=1, sticky="w", padx=(6, 16), pady=4)
            if l2 and v2 is not None:
                ttk.Label(f_macro, text=l2).grid(row=r_idx, column=2, sticky="w", pady=4)
                ttk.Entry(f_macro, textvariable=v2, width=20).grid(row=r_idx, column=3, sticky="w", padx=(6, 0), pady=4)

        btn_bar = ttk.Frame(main, padding=(0, 6, 0, 0))
        btn_bar.pack(fill="x")
        ttk.Button(btn_bar, text="Save Changes", command=self._on_save).pack(side="right")
        ttk.Button(btn_bar, text="Cancel", command=self._on_cancel).pack(side="right", padx=(0, 8))

    def _validate(self) -> tuple[dict, dict]:
        errors = []
        name = self.v_name.get().strip()
        if not name: errors.append("Name cannot be empty.")
        location = self.v_location.get().strip()
        if not location: errors.append("Location cannot be empty.")

        exp = self.v_expiration.get().strip()
        if exp:
            try:
                date.fromisoformat(exp)
                if not re.match(r"^\d{4}-[0-1]\d-[0-3]\d$", exp): raise ValueError
            except Exception: errors.append("Expiration Date must be in YYYY-MM-DD format.")
        else: exp = None

        try:
            unit_weight = float(self.v_unit_weight.get().strip())
            if not math.isfinite(unit_weight) or unit_weight <= 0:
                errors.append("Package Unit Weight must be greater than 0.")
        except Exception:
            errors.append("Package Unit Weight must be a valid positive number.")
            unit_weight = 1.0

        try:
            intact = int(self.v_intact.get().strip())
            if intact < 0: errors.append("Intact Packages cannot be negative.")
        except Exception:
            errors.append("Intact Packages must be a valid non-negative integer.")
            intact = 0

        try:
            remainder = float(self.v_remainder.get().strip())
            if not math.isfinite(remainder) or remainder < 0:
                errors.append("Open Remainder must be a valid non-negative number.")
        except Exception:
            errors.append("Open Remainder must be a valid non-negative number.")
            remainder = 0.0

        if intact == 0 and remainder <= 0:
            errors.append("Total quantity cannot be 0. To delete this item, use the delete button.")

        unit = self.v_unit.get().strip()
        if unit not in ("kg", "L", "pcs"): errors.append("Unit must be 'kg', 'L', or 'pcs'.")

        def _parse_num(val_str, field_name, required=False, allow_zero=True):
            s = val_str.strip()
            if not s:
                if required: errors.append(f"{field_name} is required.")
                return None
            try:
                n = float(s)
                if not math.isfinite(n) or (n < 0 if allow_zero else n <= 0):
                    errors.append(f"{field_name} must be {'>=' if allow_zero else '>'} 0.")
                    return None
                return n
            except Exception:
                errors.append(f"{field_name} must be a valid number.")
                return None

        kcal = _parse_num(self.v_kcal.get(), "Kcal", required=True)
        protein = _parse_num(self.v_protein.get(), "Protein", required=True)
        carbs = _parse_num(self.v_carbs.get(), "Carbs", required=True)
        fat = _parse_num(self.v_fat.get(), "Fat", required=True)
        sugars = _parse_num(self.v_sugars.get(), "Sugars")
        sat_fat = _parse_num(self.v_saturated_fat.get(), "Saturated Fat")
        fiber = _parse_num(self.v_fiber.get(), "Fiber")
        salt = _parse_num(self.v_salt.get(), "Salt")
        gpp = _parse_num(self.v_grams_per_piece.get(), "Grams per piece", allow_zero=False)
        den = _parse_num(self.v_density.get(), "Density kg/L", allow_zero=False)

        if errors:
            raise ValueError("\n".join(errors))

        return {
            "Name": name, "Location": location, "Expiration_Date": exp, "Unit_Weight": unit_weight,
            "Intact_Items": intact, "Open_Remainder": remainder, "Unit": unit,
            "Grams_per_piece": gpp, "Density_kg_per_L": den,
        }, {
            "Kcal_100g": kcal, "Protein_100g": protein, "Carbs_100g": carbs, "Fat_100g": fat,
            "Sugars_100g": sugars, "Saturated_fat_100g": sat_fat, "Fiber_100g": fiber, "Salt_100g": salt,
        }

    def _on_save(self) -> bool:
        try:
            item_fields, macro_fields = self._validate()
        except ValueError as e:
            self.last_error = str(e)
            messagebox.showerror("Validation Error", str(e), parent=self)
            return False

        d = self.item_data
        macros_changed = any(v is not None and (d.get(k) is None or abs(d[k] - v) > 1e-5) or (v is None and d.get(k) is not None)
                             for k, v in macro_fields.items()) or (item_fields["Unit"] != d.get("Unit"))
        propagate = False
        if macros_changed:
            pid = d["ID"]
            if is_product_macro_linked(pid, db_path=self.db_path):
                prompt = MacroPromptDialog(self, product_name=d.get("Name", f"Product #{pid}"), product_id=pid)
                if prompt.result is None:
                    return False
                propagate = (prompt.result == "propagate")
            else:
                propagate = True

        try:
            res = update_inventory_item(row_id=self.row_id, item_fields=item_fields, macro_fields=macro_fields,
                                        propagate=propagate, db_path=self.db_path)
            self.result = res
            self.destroy()
            return True
        except Exception as e:
            self.last_error = str(e)
            messagebox.showerror("Save Error", str(e), parent=self)
            return False


class ProfileSetupDialog(BaseDialog):
    """Modal dialog allowing initial configuration of user profile from the empty-state overlay."""

    def __init__(self, parent, db_path: Optional[str] = None, wait: bool = True):
        super().__init__(parent, "Set Up Profile", wait=False, resizable=(False, False))
        self.db_path = db_path
        c = ttk.Frame(self, padding=16)
        c.pack(fill="both", expand=True)
        ttk.Label(c, text="Enter your details to get started:", font=("TkDefaultFont", 11, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))

        ttk.Label(c, text="Sex:").grid(row=1, column=0, sticky="w", pady=4)
        self.v_sex = tk.StringVar(value="M")
        ttk.Combobox(c, textvariable=self.v_sex, values=["M", "F"], state="readonly", width=14).grid(row=1, column=1, sticky="w", padx=(10, 0), pady=4)

        ttk.Label(c, text="Height (cm):").grid(row=2, column=0, sticky="w", pady=4)
        self.v_height = tk.StringVar(value="175")
        ttk.Entry(c, textvariable=self.v_height, width=16).grid(row=2, column=1, sticky="w", padx=(10, 0), pady=4)

        ttk.Label(c, text="Current weight (kg):").grid(row=3, column=0, sticky="w", pady=4)
        self.v_weight = tk.StringVar(value="70")
        ttk.Entry(c, textvariable=self.v_weight, width=16).grid(row=3, column=1, sticky="w", padx=(10, 0), pady=4)

        ttk.Label(c, text="Age (years):").grid(row=4, column=0, sticky="w", pady=4)
        self.v_age = tk.StringVar(value="25")
        ttk.Entry(c, textvariable=self.v_age, width=16).grid(row=4, column=1, sticky="w", padx=(10, 0), pady=4)

        ttk.Label(c, text="Activity level:").grid(row=5, column=0, sticky="w", pady=4)
        self.v_activity = tk.StringVar(value="moderate")
        from meal_planner.engine import PAL
        ttk.Combobox(c, textvariable=self.v_activity, values=sorted(PAL.keys()), state="readonly", width=14).grid(row=5, column=1, sticky="w", padx=(10, 0), pady=4)

        ttk.Label(c, text="Goal:").grid(row=6, column=0, sticky="w", pady=4)
        self.v_goal = tk.StringVar(value="maintenance")
        ttk.Combobox(c, textvariable=self.v_goal, values=["maintenance", "weight_loss", "weight_gain"], state="readonly", width=14).grid(row=6, column=1, sticky="w", padx=(10, 0), pady=4)

        ttk.Label(c, text="Target weight (kg):").grid(row=7, column=0, sticky="w", pady=4)
        self.v_target_weight = tk.StringVar(value="70")
        ttk.Entry(c, textvariable=self.v_target_weight, width=16).grid(row=7, column=1, sticky="w", padx=(10, 0), pady=4)

        ttk.Label(c, text="Weekly rate (kg/week):").grid(row=8, column=0, sticky="w", pady=4)
        self.v_weekly_rate = tk.StringVar(value="0.5")
        ttk.Entry(c, textvariable=self.v_weekly_rate, width=16).grid(row=8, column=1, sticky="w", padx=(10, 0), pady=4)

        btn_row = ttk.Frame(c)
        btn_row.grid(row=9, column=0, columnspan=2, sticky="e", pady=(16, 0))
        ttk.Button(btn_row, text="Cancel", command=self.cancel).pack(side="right", padx=(6, 0))
        ttk.Button(btn_row, text="Save Profile", command=self.save).pack(side="right")
        self._finish_init(parent, wait=wait)

    def save(self) -> bool:
        sex = self.v_sex.get().strip()
        if sex not in ("M", "F"):
            messagebox.showerror("Error", "Please select 'M' or 'F' for sex.", parent=self)
            return False
        try:
            height, weight, age = float(self.v_height.get().strip()), float(self.v_weight.get().strip()), int(self.v_age.get().strip())
            if height <= 0 or weight <= 0 or age <= 0: raise ValueError()
        except ValueError:
            messagebox.showerror("Error", "Height, weight, and age must be positive numerical values.", parent=self)
            return False

        activity = self.v_activity.get().strip()
        goal = self.v_goal.get().strip()
        try: target_w = float(self.v_target_weight.get().strip())
        except ValueError: target_w = weight
        try: weekly_rate = float(self.v_weekly_rate.get().strip())
        except ValueError: weekly_rate = 0.5

        data = {
            "Weight_kg": weight, "Height_cm": height, "Age_years": age, "Sex": sex,
            "Activity_Level": activity, "Goal": goal, "Target_weight_kg": target_w,
            "Weekly_rate_kg": weekly_rate if goal != "maintenance" else None,
            "Kcal_tolerance_pct": 5.0, "Open_extra_pct": 5.0, "No_repeat_days": 3,
            "Default_servings": 1, "Use_manual_maintenance": False, "Use_manual_kcal": False,
            "Maintenance_manual": None, "Kcal_manual": None,
        }
        try:
            from meal_planner.engine import update_profile
            update_profile(conn=None, values=data, db_path=self.db_path)
            self.result = data
            self.destroy()
            return True
        except Exception as e:
            messagebox.showerror("Save Error", str(e), parent=self)
            return False

    on_save = save
    _on_save = save


class _MealDict:
    """Case-insensitive dictionary supporting bilingual Italian/English meal aliases."""
    _ALIASES = {
        "colazione": "breakfast", "breakfast": "colazione",
        "pranzo": "lunch", "lunch": "pranzo",
        "cena": "dinner", "dinner": "cena",
        "spuntino": "snack", "snack": "spuntino",
        "merenda": "snack",
    }

    def __init__(self, canonical_items=None):
        self._canonical = {}
        self._lower_map = {}
        if canonical_items:
            for name, var in canonical_items:
                self.add_canonical(name, var)

    def add_canonical(self, name: str, var: tk.BooleanVar):
        self._canonical[name] = var
        self._lower_map[name.strip().lower()] = name

    def _resolve(self, key: str):
        if not isinstance(key, str):
            return key
        k = key.strip().lower()
        if k in self._lower_map:
            return self._lower_map[k]
        alias = self._ALIASES.get(k)
        if alias and alias.lower() in self._lower_map:
            return self._lower_map[alias.lower()]
        return None

    def __getitem__(self, key: str) -> tk.BooleanVar:
        canon = self._resolve(key)
        if canon:
            return self._canonical[canon]
        var = tk.BooleanVar(value=False)
        self.add_canonical(key, var)
        return var

    def __setitem__(self, key: str, value: tk.BooleanVar):
        canon = self._resolve(key)
        if canon: self._canonical[canon] = value
        else: self.add_canonical(key, value)

    def __contains__(self, key: str) -> bool:
        return self._resolve(key) is not None

    def get(self, key: str, default=None):
        canon = self._resolve(key)
        return self._canonical[canon] if canon else default

    def items(self): return self._canonical.items()
    def keys(self): return self._canonical.keys()
    def values(self): return self._canonical.values()
    def __iter__(self): return iter(self._canonical)
    def __len__(self): return len(self._canonical)


class QuickProductDialog(BaseDialog):
    """Dialog to register an unknown product on the fly."""

    def __init__(self, master=None, name: str = "", suggested_unit: str = "g", wait: bool = True, db_path: Optional[str] = None):
        super().__init__(master, "New Product", wait=False, resizable=(False, False))
        self.db_path = db_path
        c = ttk.Frame(self, padding=14)
        c.pack(fill="both", expand=True)
        ttk.Label(c, text=f"Product not found: '{name}'\nEnter details to add it to catalog:", font=("TkDefaultFont", 10, "bold"), wraplength=340).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        ttk.Label(c, text="Name:").grid(row=1, column=0, sticky="w", pady=3)
        self.v_name = tk.StringVar(value=name)
        self.e_name = ttk.Entry(c, textvariable=self.v_name, width=24)
        self.e_name.grid(row=1, column=1, sticky="w", padx=(8, 0), pady=3)

        ttk.Label(c, text="Unit of measure:").grid(row=2, column=0, sticky="w", pady=3)
        self.v_unit = tk.StringVar(value=suggested_unit if suggested_unit in ("g", "kg", "ml", "L", "pcs", "pz") else "g")
        self.cb_unit = ttk.Combobox(c, textvariable=self.v_unit, values=["g", "kg", "ml", "L", "pcs"], state="readonly", width=10)
        self.cb_unit.grid(row=2, column=1, sticky="w", padx=(8, 0), pady=3)

        ttk.Label(c, text="Values per 100g:", font=("TkDefaultFont", 9, "bold")).grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 4))

        fields = [("kcal", "Kcal *:", "v_kcal", "0"), ("prot", "Protein (g) *:", "v_prot", "0"),
                  ("carb", "Carbs (g) *:", "v_carb", "0"), ("fat", "Fat (g) *:", "v_fat", "0"),
                  ("fiber", "Fiber (g):", "v_fiber", "")]
        self.macro_vars = {}
        for idx, (k, lbl, var_n, def_v) in enumerate(fields, start=4):
            ttk.Label(c, text=lbl).grid(row=idx, column=0, sticky="w", pady=2)
            var = tk.StringVar(value=def_v)
            self.macro_vars[k] = var
            setattr(self, var_n, var)
            e = ttk.Entry(c, textvariable=var, width=12)
            e.grid(row=idx, column=1, sticky="w", padx=(8, 0), pady=2)
            setattr(self, f"e_{k}", e)

        btn_bar = ttk.Frame(c)
        btn_bar.grid(row=9, column=0, columnspan=2, sticky="e", pady=(12, 0))
        self.btn_cancel = ttk.Button(btn_bar, text="Cancel", command=self.cancel)
        self.btn_cancel.pack(side="right", padx=(6, 0))
        self.btn_save = ttk.Button(btn_bar, text="Add", command=self.save)
        self.btn_save.pack(side="right")
        self._finish_init(master, wait=wait)

    def save(self) -> bool:
        name = self.v_name.get().strip()
        if not name:
            messagebox.showerror("Error", "Product name is required.", parent=self)
            return False
        unit = self.v_unit.get().strip()
        if not unit:
            messagebox.showerror("Error", "Unit of measure is required.", parent=self)
            return False

        try:
            kcal = float(self.v_kcal.get().strip().replace(",", "."))
            prot = float(self.v_prot.get().strip().replace(",", "."))
            carb = float(self.v_carb.get().strip().replace(",", "."))
            fat = float(self.v_fat.get().strip().replace(",", "."))
            if kcal < 0 or prot < 0 or carb < 0 or fat < 0: raise ValueError()
        except (ValueError, TypeError):
            messagebox.showerror("Error", "Kcal, Protein, Carbs, and Fat must be non-negative numbers.", parent=self)
            return False

        fiber = None
        if self.v_fiber.get().strip():
            try:
                fiber_val = float(self.v_fiber.get().strip().replace(",", "."))
                if fiber_val < 0: raise ValueError()
                fiber = fiber_val
            except (ValueError, TypeError):
                messagebox.showerror("Error", "Fiber must be a non-negative number.", parent=self)
                return False

        macros = {"Kcal_100g": kcal, "Protein_100g": prot, "Carbs_100g": carb, "Fat_100g": fat}
        if fiber is not None: macros["Fiber_100g"] = fiber
        self.result = {"name": name, "unit": unit, "macros": macros}
        self.destroy()
        return True

    on_save = save
    _on_save = save


class AddRecipeDialog(BaseDialog):
    """Modal dialog allowing direct GUI creation of recipes."""

    def __init__(self, master=None, db_path: Optional[str] = None, wait: bool = True, on_save_callback=None, on_unknown_product_callback=None):
        super().__init__(master, "New Recipe", wait=False, minsize=(680, 620))
        self.db_path = db_path
        self._wait = wait
        self.on_save_callback = on_save_callback
        self.on_unknown_product_callback = on_unknown_product_callback
        self.ingredients = []
        self._focus_timer = None

        self._init_variables()
        self._init_ui()
        self._finish_init(master, wait=wait)

    def _init_variables(self):
        self.v_name = tk.StringVar(value="")
        self.v_servings = tk.StringVar(value="1")
        self.v_notes = tk.StringVar(value="")
        self.v_ingredient_name = tk.StringVar(value="")
        self.v_quantity = tk.StringVar(value="")
        self.v_unit = tk.StringVar(value="g")
        self.v_to_taste = tk.BooleanVar(value=False)

        try:
            with self._get_conn() as conn:
                self.v_meals = self._load_meals(conn=conn)
                self._known_ingredients = self._load_known_ingredients(conn=conn)
        except Exception:
            self.v_meals = self._load_meals()
            self._known_ingredients = self._load_known_ingredients()

    def _load_meals(self, conn=None) -> _MealDict:
        meals = []
        try:
            with (contextlib.nullcontext(conn) if conn is not None else self._get_conn()) as c:
                cur = c.cursor()
                cur.execute("SELECT Name FROM MEALS ORDER BY Sort_Order")
                for (m_name,) in cur.fetchall():
                    meals.append((m_name, tk.BooleanVar(value=False)))
        except Exception:
            pass
        if not meals:
            for m_name in ("breakfast", "lunch", "dinner"):
                meals.append((m_name, tk.BooleanVar(value=False)))
        return _MealDict(meals)

    def _load_known_ingredients(self, conn=None) -> list:
        try:
            with (contextlib.nullcontext(conn) if conn is not None else self._get_conn()) as c:
                cur = c.cursor()
                cur.execute("SELECT Name FROM ALIASES UNION SELECT Name FROM SUBSTITUTE_GROUPS ORDER BY Name")
                return [r[0] for r in cur.fetchall()]
        except Exception:
            return []

    def _init_ui(self):
        main = ttk.Frame(self, padding=12)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)

        meta = ttk.LabelFrame(main, text="Recipe Details", padding=10)
        meta.pack(fill="x", pady=(0, 8))
        meta.columnconfigure(1, weight=1)
        ttk.Label(meta, text="Recipe Name:").grid(row=0, column=0, sticky="w", pady=4)
        self.e_name = ttk.Entry(meta, textvariable=self.v_name)
        self.e_name.grid(row=0, column=1, sticky="ew", padx=(8, 16), pady=4)
        ttk.Label(meta, text="Servings:").grid(row=0, column=2, sticky="w", pady=4)
        self.e_servings = ttk.Entry(meta, textvariable=self.v_servings, width=6)
        self.e_servings.grid(row=0, column=3, sticky="w", padx=(8, 0), pady=4)
        self.v_servings.trace_add("write", lambda *args: self._update_macro_summary())

        m_frame = ttk.Frame(meta)
        m_frame.grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 2))
        ttk.Label(m_frame, text="Meals:").pack(side="left", padx=(0, 8))
        for m_name, var in self.v_meals.items():
            ttk.Checkbutton(m_frame, text=m_name.capitalize(), variable=var).pack(side="left", padx=(0, 10))

        n_frame = ttk.LabelFrame(main, text="Preparation Notes", padding=8)
        n_frame.pack(fill="x", pady=(0, 8))
        self.t_notes = tk.Text(n_frame, height=3, wrap="word")
        self.t_notes.pack(fill="x", expand=True)
        self.t_notes.bind("<FocusOut>", lambda e: self.v_notes.set(self.notes))

        ing_box = ttk.LabelFrame(main, text="Add Ingredient", padding=10)
        ing_box.pack(fill="x", pady=(0, 8))
        ing_box.columnconfigure(1, weight=1)
        ttk.Label(ing_box, text="Ingredient:").grid(row=0, column=0, sticky="w", pady=2)
        self.cb_ingredient = ttk.Combobox(ing_box, textvariable=self.v_ingredient_name,
                                          values=getattr(self, "_known_ingredients", None) or self._load_known_ingredients(), width=22)
        self.cb_ingredient.grid(row=0, column=1, sticky="ew", padx=(6, 12), pady=2)
        ttk.Label(ing_box, text="Quantity:").grid(row=0, column=2, sticky="w", pady=2)
        self.e_quantity = ttk.Entry(ing_box, textvariable=self.v_quantity, width=8)
        self.e_quantity.grid(row=0, column=3, sticky="w", padx=(6, 12), pady=2)
        ttk.Label(ing_box, text="Unit:").grid(row=0, column=4, sticky="w", pady=2)
        self.cb_unit = ttk.Combobox(ing_box, textvariable=self.v_unit, values=["g", "kg", "ml", "L", "pcs", "tbsp", "tsp"], state="readonly", width=8)
        self.cb_unit.grid(row=0, column=5, sticky="w", padx=(6, 12), pady=2)
        self.chk_to_taste = ttk.Checkbutton(ing_box, text="To taste (q.b.)", variable=self.v_to_taste)
        self.chk_to_taste.grid(row=0, column=6, sticky="w", padx=(0, 12), pady=2)
        self.btn_add_ingredient = ttk.Button(ing_box, text="Add", command=self.add_ingredient)
        self.btn_add_ingredient.grid(row=0, column=7, sticky="w", pady=2)
        ttk.Label(ing_box, text="ℹ️ 'To taste' (q.b. / quanto basta): for discretionary ingredients added to taste without fixed nutritional weight (e.g. salt, pepper, spices, herbs).",
                  font=("TkDefaultFont", 8, "italic")).grid(row=1, column=0, columnspan=8, sticky="w", pady=(2, 0))

        t_frame = ttk.LabelFrame(main, text="Added Ingredients (Recipe Total)", padding=8)
        t_frame.pack(fill="both", expand=True, pady=(0, 8))
        t_frame.columnconfigure(0, weight=1)
        t_frame.rowconfigure(0, weight=1)

        cols = ("product", "quantity", "unit", "to_taste", "kcal", "prot", "carb", "fat")
        self.t_ingredients = ttk.Treeview(t_frame, columns=cols, show="headings", height=5)
        for col, (head, w, a) in zip(cols, (("Ingredient", 180, "w"), ("Quantity", 70, "e"), ("Unit", 50, "center"),
                                            ("To taste", 50, "center"), ("Kcal", 60, "e"), ("Prot (g)", 60, "e"),
                                            ("Carbs (g)", 60, "e"), ("Fat (g)", 60, "e"))):
            self.t_ingredients.heading(col, text=head)
            self.t_ingredients.column(col, width=w, anchor=a)
        sb = ttk.Scrollbar(t_frame, orient="vertical", command=self.t_ingredients.yview)
        self.t_ingredients.configure(yscrollcommand=sb.set)
        self.t_ingredients.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")

        sum_row = ttk.Frame(t_frame)
        sum_row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        sum_row.columnconfigure(0, weight=1)
        self.lbl_macro_summary = ttk.Label(sum_row, text="Total per serving (1 of 1): 0.0 kcal | Prot: 0.0g | Carbs: 0.0g | Fat: 0.0g", font=("TkDefaultFont", 9, "bold"))
        self.lbl_macro_summary.grid(row=0, column=0, sticky="w")
        self.btn_remove_ingredient = ttk.Button(sum_row, text="Remove Ingredient", command=self.remove_ingredient)
        self.btn_remove_ingredient.grid(row=0, column=1, sticky="e")

        action_bar = ttk.Frame(main)
        action_bar.pack(fill="x", pady=(6, 0))
        self.btn_cancel = ttk.Button(action_bar, text="Cancel", command=self._on_cancel)
        self.btn_cancel.pack(side="right", padx=(8, 0))
        self.btn_save = ttk.Button(action_bar, text="Save Recipe", command=self._on_save)
        self.btn_save.pack(side="right")

        self.bind("<Control-Return>", lambda e: self._on_save())
        self.bind("<Control-s>", lambda e: self._on_save())
        self.bind("<Control-S>", lambda e: self._on_save())
        self.e_name.bind("<Return>", lambda e: self.cb_ingredient.focus_set())
        self.e_quantity.bind("<Return>", lambda e: self.add_ingredient())
        self.cb_ingredient.bind("<Return>", lambda e: self.e_quantity.focus_set() if not self.v_quantity.get().strip() else self.add_ingredient())
        self.t_ingredients.bind("<Delete>", lambda e: self.remove_ingredient())
        self.t_ingredients.bind("<BackSpace>", lambda e: self.remove_ingredient())
        self._focus_timer = self.after(100, self._set_initial_focus)

    @property
    def notes(self) -> str:
        if hasattr(self, "t_notes"):
            try:
                txt = self.t_notes.get("1.0", "end-1c").strip()
                if txt: return txt
            except Exception: pass
        return self.v_notes.get().strip()

    @notes.setter
    def notes(self, value: str):
        val = str(value or "").strip()
        self.v_notes.set(val)
        if hasattr(self, "t_notes"):
            try:
                self.t_notes.delete("1.0", "end")
                if val: self.t_notes.insert("1.0", val)
            except Exception: pass

    def _calculate_ingredient_macros(self, name: str, quantity: float, unit: str, conn=None) -> dict:
        macros = {"kcal": 0.0, "prot": 0.0, "carb": 0.0, "fat": 0.0}
        def _calc(c):
            cur = c.cursor()
            cur.execute("""
                SELECT p.Unit, p.Kcal_100g, p.Protein_100g, p.Carbs_100g, p.Fat_100g,
                       p.Grams_per_piece, p.Density_kg_per_L
                FROM ALIASES a JOIN PRODUCTS p ON a.ID = p.ID WHERE a.Name = ? COLLATE NOCASE
            """, (name,))
            row = cur.fetchone()
            if not row: return macros
            _, k100, p100, c100, f100, g_pc, dens = row
            g = None
            if unit == "g": g = quantity
            elif unit == "kg": g = quantity * 1000.0
            elif unit == "pcs": g = quantity * g_pc if g_pc else None
            elif unit in ("ml", "L", "tbsp", "tsp"):
                factors = {"ml": 1.0, "L": 1000.0, "tbsp": 15.0, "tsp": 5.0}
                g = quantity * factors.get(unit, 1.0) * (dens or 1.0)
            if g is not None:
                macros["kcal"] = round(g / 100.0 * k100, 2)
                macros["prot"] = round(g / 100.0 * p100, 2)
                macros["carb"] = round(g / 100.0 * c100, 2)
                macros["fat"] = round(g / 100.0 * f100, 2)
            return macros

        try:
            if conn is not None: return _calc(conn)
            with self._get_conn() as c: return _calc(c)
        except Exception: return macros

    def add_ingredient(self) -> bool:
        name = (self.v_ingredient_name.get() or self.cb_ingredient.get()).strip()
        if not name:
            messagebox.showerror("Error", "Please select or enter an ingredient.", parent=self)
            return False
        unit = (self.v_unit.get() or self.cb_unit.get()).strip() or "g"
        to_taste = 1 if self.v_to_taste.get() else 0
        raw_qty = (self.v_quantity.get() or self.e_quantity.get()).strip()
        if not raw_qty and to_taste == 1:
            qty = 1.0
        else:
            try:
                qty = float(raw_qty.replace(",", "."))
                if qty <= 0: raise ValueError()
            except (ValueError, TypeError):
                messagebox.showerror("Error", "Quantity must be a positive number (> 0).", parent=self)
                return False

        from meal_planner.recipes import is_ingredient_known, register_product_on_the_fly
        macros = None
        with self._get_conn() as conn:
            known = is_ingredient_known(conn, name)
            if known:
                macros = self._calculate_ingredient_macros(name, qty, unit, conn=conn)

        if not known:
            dlg_res = self.on_unknown_product_callback(name, unit) if self.on_unknown_product_callback else QuickProductDialog(self, name=name, suggested_unit=unit, wait=self._wait, db_path=self.db_path).result
            if not dlg_res:
                return False
            with self._get_conn() as conn:
                register_product_on_the_fly(conn, name=dlg_res["name"], unit=dlg_res["unit"], macros=dlg_res["macros"])
                name = dlg_res["name"]
                self.cb_ingredient["values"] = self._load_known_ingredients(conn=conn)
                macros = self._calculate_ingredient_macros(name, qty, unit, conn=conn)

        if macros is None:
            macros = self._calculate_ingredient_macros(name, qty, unit)
        self.ingredients.append({"product": name, "quantity": qty, "unit": unit, "to_taste": to_taste, **macros})
        self._refresh_ingredients_table()
        self._update_macro_summary()
        self.v_ingredient_name.set("")
        self.cb_ingredient.set("")
        self.v_quantity.set("")
        self.e_quantity.delete(0, "end")
        self.v_to_taste.set(False)
        try: self.cb_ingredient.focus_set()
        except Exception: pass
        return True

    def remove_ingredient(self, index: Optional[int] = None) -> bool:
        if index is not None and 0 <= index < len(self.ingredients):
            del self.ingredients[index]
        else:
            sel = self.t_ingredients.selection()
            if sel:
                idx = self.t_ingredients.index(sel[0])
                if 0 <= idx < len(self.ingredients): del self.ingredients[idx]
            elif self.ingredients:
                self.ingredients.pop()
            else:
                messagebox.showwarning("Warning", "Please select an ingredient to remove.", parent=self)
                return False
        self._refresh_ingredients_table()
        self._update_macro_summary()
        return True

    def _refresh_ingredients_table(self):
        for item in self.t_ingredients.get_children():
            self.t_ingredients.delete(item)
        for ing in self.ingredients:
            self.t_ingredients.insert("", "end", values=(
                ing["product"], f"{ing['quantity']:g}", ing["unit"], "Yes" if ing["to_taste"] else "No",
                f"{ing.get('kcal', 0.0):.1f}", f"{ing.get('prot', 0.0):.1f}", f"{ing.get('carb', 0.0):.1f}", f"{ing.get('fat', 0.0):.1f}",
            ))

    def _update_macro_summary(self):
        if not hasattr(self, "lbl_macro_summary"): return
        tot_kcal = sum(ing.get("kcal", 0.0) for ing in self.ingredients)
        tot_prot = sum(ing.get("prot", 0.0) for ing in self.ingredients)
        tot_carb = sum(ing.get("carb", 0.0) for ing in self.ingredients)
        tot_fat = sum(ing.get("fat", 0.0) for ing in self.ingredients)
        try:
            serv = float(self.v_servings.get().strip().replace(",", ".") or "1")
            if not math.isfinite(serv) or serv <= 0: serv = 1.0
        except Exception: serv = 1.0
        self.lbl_macro_summary.configure(
            text=f"Total per serving (1 of {serv:g}): {tot_kcal/serv:.1f} kcal | Prot: {tot_prot/serv:.1f}g | Carbs: {tot_carb/serv:.1f}g | Fat: {tot_fat/serv:.1f}g"
        )

    def _on_save(self) -> bool:
        name = self.v_name.get().strip()
        if not name:
            messagebox.showerror("Error", "Recipe name is required.", parent=self)
            return False
        meals = [m for m, var in self.v_meals.items() if var.get()]
        if not meals:
            messagebox.showerror("Error", "Select at least one meal for the recipe.", parent=self)
            return False
        if not self.ingredients:
            messagebox.showerror("Error", "Add at least one ingredient to the recipe.", parent=self)
            return False
        try:
            serv = float(self.v_servings.get().strip().replace(",", ".") or "1")
            if not math.isfinite(serv) or serv <= 0: raise ValueError()
        except Exception:
            messagebox.showerror("Error", "Number of servings must be a positive number (> 0).", parent=self)
            return False

        scaled = [{"product": ing["product"], "quantity": round(ing["quantity"] / serv, 6),
                   "unit": ing["unit"], "to_taste": ing["to_taste"]} for ing in self.ingredients]
        try:
            with self._get_conn() as conn:
                from meal_planner.recipes import create_recipe
                outcome = create_recipe(conn, name, meals, scaled, notes=self.notes or None)
            self.result = {"Recipe_ID": outcome["Recipe_ID"], "Name": name, "warnings": outcome.get("warnings", [])}
            if self.on_save_callback:
                try: self.on_save_callback(self.result)
                except Exception: pass
            self.destroy()
            return True
        except Exception as e:
            self.last_error = str(e)
            messagebox.showerror("Save Error", str(e), parent=self)
            return False

    def _set_initial_focus(self):
        self._focus_timer = None
        if self.winfo_exists() and hasattr(self, "e_name"):
            try:
                self.e_name.focus_set()
            except Exception:
                pass

    def destroy(self):
        if hasattr(self, "_focus_timer") and self._focus_timer:
            try: self.after_cancel(self._focus_timer)
            except Exception: pass
            self._focus_timer = None
        super().destroy()

    def _on_cancel(self):
        if hasattr(self, "_focus_timer") and self._focus_timer:
            try: self.after_cancel(self._focus_timer)
            except Exception: pass
            self._focus_timer = None
        super()._on_cancel()


class RecipeDetailsDialog(BaseDialog):
    """Modal dialog displaying detailed view of an existing recipe."""

    def __init__(self, master=None, recipe_name: str = "", db_path: Optional[str] = None, wait: bool = True):
        super().__init__(master, f"Recipe Details — {recipe_name}", wait=False, minsize=(640, 480))
        self.db_path = db_path
        self._wait = wait
        self.recipe_name = str(recipe_name).strip()
        self._init_ui()
        self._finish_init(master, wait=wait)

    def _init_ui(self):
        main_frame = ttk.Frame(self, padding=16)
        main_frame.pack(fill="both", expand=True)
        main_frame.columnconfigure(0, weight=1)
        main_frame.rowconfigure(2, weight=1)

        header = ttk.Frame(main_frame)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(header, text=self.recipe_name, font=("TkDefaultFont", 13, "bold")).pack(anchor="w")

        notes = ""
        meals = []
        macros = (0, 0, 0, 0, 0)
        ingredients = []
        try:
            with self._get_conn() as conn:
                cur = conn.cursor()
                r = cur.execute("SELECT Recipe_ID, Notes FROM RECIPES WHERE Name = ?", (self.recipe_name,)).fetchone()
                if r:
                    rid, notes = r
                    meals = [m[0].capitalize() for m in cur.execute("SELECT Meal FROM RECIPE_MEALS WHERE Recipe_ID = ? ORDER BY Meal", (rid,)).fetchall()]
                    m_row = cur.execute("SELECT Kcal, Protein, Carbs, Fat, Fiber FROM RECIPE_MACROS_VIEW WHERE Recipe_ID = ?", (rid,)).fetchone()
                    if m_row: macros = m_row
                    ingredients = cur.execute('''
                        SELECT 
                            COALESCE(g.Name, a.Name, 'Product ' || ri.ID) AS IngName,
                            ri.Quantity, ri.Unit, ri.To_Taste
                        FROM RECIPE_INGREDIENTS ri
                        LEFT JOIN SUBSTITUTE_GROUPS g ON ri.Group_ID = g.Group_ID
                        LEFT JOIN (SELECT ID, MIN(Name) as Name FROM ALIASES GROUP BY ID) a ON ri.ID = a.ID
                        WHERE ri.Recipe_ID = ?
                        ORDER BY ri.Row_ID
                    ''', (rid,)).fetchall()
        except Exception:
            pass

        ttk.Label(header, text=f"Associated meals: {', '.join(meals) if meals else 'All meals'}", font=("TkDefaultFont", 9)).pack(anchor="w", pady=(2, 0))
        if notes:
            n_frame = ttk.LabelFrame(main_frame, text="Preparation Notes", padding=8)
            n_frame.grid(row=1, column=0, sticky="ew", pady=(0, 10))
            ttk.Label(n_frame, text=notes, wraplength=580, justify="left").pack(anchor="w")

        t_frame = ttk.LabelFrame(main_frame, text="Ingredients for 1 serving", padding=8)
        t_frame.grid(row=2, column=0, sticky="nsew", pady=(0, 10))
        t_frame.columnconfigure(0, weight=1)
        t_frame.rowconfigure(0, weight=1)

        cols = ("name", "quantity", "unit", "to_taste")
        tv = ttk.Treeview(t_frame, columns=cols, show="headings", height=6)
        tv.heading("name", text="Ingredient")
        tv.heading("quantity", text="Quantity")
        tv.heading("unit", text="Unit")
        tv.heading("to_taste", text="To taste (q.b.)")
        tv.column("name", width=280, anchor="w")
        tv.column("quantity", width=90, anchor="e")
        tv.column("unit", width=80, anchor="center")
        tv.column("to_taste", width=80, anchor="center")

        sb = ttk.Scrollbar(t_frame, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")

        for ing in ingredients:
            ing_name, qty, unit, to_taste = ing
            tv.insert("", "end", values=(ing_name, f"{qty:g}", unit, "Yes" if to_taste else "No"))

        ttk.Label(
            t_frame,
            text="ℹ️ 'To taste' (q.b. / quanto basta): ingredients added as needed to taste without fixed nutritional weight (e.g. salt, pepper, spices).",
            font=("TkDefaultFont", 8, "italic"),
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        kcal, prot, carb, fat, fiber = macros
        summary_frame = ttk.Frame(main_frame)
        summary_frame.grid(row=3, column=0, sticky="ew", pady=(0, 12))
        ttk.Label(summary_frame, text=f"Per-serving values: {kcal:.0f} kcal  |  Prot: {prot:.1f}g  |  Carbs: {carb:.1f}g  |  Fat: {fat:.1f}g  |  Fiber: {fiber:.1f}g",
                  font=("TkDefaultFont", 9, "bold")).pack(side="left")

        btn_bar = ttk.Frame(main_frame)
        btn_bar.grid(row=4, column=0, sticky="ew")
        ttk.Button(btn_bar, text="Close", command=self.destroy).pack(side="right")


class RecipeGeneratorDialog(BaseDialog):
    """Interactive modal dialog for the Hybrid Recipe Generation Pipeline."""

    ARCHETYPE_OPTIONS = [
        "Auto (Default)",
        "Grain / Pasta Dish",
        "Main & Side Plate",
        "One-Pot Balanced Bowl",
        "Breakfast / Snack Bowl",
    ]
    MEAL_OPTIONS = ["lunch", "dinner", "breakfast"]

    def __init__(self, master=None, db_path: Optional[str] = None, wait: bool = True, on_save_callback=None, auto_generate: bool = True):
        super().__init__(master, "Generate Recipe (Slot + Pairing + LP Solver)", wait=False, minsize=(720, 580))
        self.db_path = db_path
        self._wait = wait
        self.on_save_callback = on_save_callback
        self._variation_index = 0
        self.current_candidate = None
        self.saved_outcome: Optional[Dict[str, Any]] = None

        self.v_meal = tk.StringVar(value="lunch")
        self.v_archetype = tk.StringVar(value=self.ARCHETYPE_OPTIONS[0])
        self.v_target_kcal = tk.StringVar(value=str(int(self._fetch_default_kcal("lunch"))))
        self.v_pantry_only = tk.BooleanVar(value=True)
        self.v_title = tk.StringVar(value="")
        self.v_meta = tk.StringVar(value="Select parameters and click Generate.")
        self.v_macros = tk.StringVar(value="Per-serving macros: -")

        self._init_ui()
        self.bind("<Control-Return>", lambda e: self.save())
        self.bind("<Control-s>", lambda e: self.save())
        self.bind("<Control-S>", lambda e: self.save())

        if auto_generate:
            self.generate_proposal(show_errors=False)
        self._finish_init(master, wait=wait)

    def _fetch_default_kcal(self, meal_type: str) -> float:
        from meal_planner.generator import get_default_target_kcal
        try:
            with self._get_conn() as conn:
                return get_default_target_kcal(conn, meal_type)
        except Exception:
            return 700.0

    def _on_meal_changed(self, _event=None):
        meal = self.v_meal.get().strip().lower()
        self.v_target_kcal.set(str(int(self._fetch_default_kcal(meal))))
        self._variation_index = 0

    def _init_ui(self):
        main = ttk.Frame(self, padding=14)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(2, weight=1)

        params = ttk.LabelFrame(main, text="Generation Parameters", padding=10)
        params.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        for col in range(5):
            params.columnconfigure(col, weight=1 if col in (1, 3) else 0)

        ttk.Label(params, text="Meal:").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=4)
        self.cb_meal = ttk.Combobox(params, textvariable=self.v_meal, values=self.MEAL_OPTIONS, state="readonly", width=14)
        self.cb_meal.grid(row=0, column=1, sticky="ew", padx=(0, 12), pady=4)
        self.cb_meal.bind("<<ComboboxSelected>>", self._on_meal_changed)

        ttk.Label(params, text="Archetype:").grid(row=0, column=2, sticky="w", padx=(0, 6), pady=4)
        self.cb_archetype = ttk.Combobox(params, textvariable=self.v_archetype, values=self.ARCHETYPE_OPTIONS, state="readonly", width=24)
        self.cb_archetype.grid(row=0, column=3, sticky="ew", padx=(0, 12), pady=4)
        self.cb_archetype.bind("<<ComboboxSelected>>", lambda e: setattr(self, "_variation_index", 0))

        ttk.Label(params, text="Target kcal:").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=4)
        self.e_target_kcal = ttk.Entry(params, textvariable=self.v_target_kcal, width=10)
        self.e_target_kcal.grid(row=1, column=1, sticky="w", padx=(0, 12), pady=4)

        self.chk_pantry_only = ttk.Checkbutton(params, text="Pantry Only (prioritize opened & near-expiry FIFO)", variable=self.v_pantry_only)
        self.chk_pantry_only.grid(row=1, column=2, columnspan=2, sticky="w", pady=4)

        self.btn_generate = ttk.Button(params, text="✨ Generate", command=self.generate_proposal)
        self.btn_generate.grid(row=0, column=4, rowspan=2, sticky="ns", padx=(8, 0), pady=2)

        t_frame = ttk.LabelFrame(main, text="Proposed Recipe", padding=10)
        t_frame.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        t_frame.columnconfigure(1, weight=1)
        ttk.Label(t_frame, text="Recipe Name:").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.e_title = ttk.Entry(t_frame, textvariable=self.v_title, font=("TkDefaultFont", 10, "bold"))
        self.e_title.grid(row=0, column=1, sticky="ew")
        ttk.Label(t_frame, textvariable=self.v_meta, font=("TkDefaultFont", 8)).grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

        center = ttk.Frame(main)
        center.grid(row=2, column=0, sticky="nsew", pady=(0, 10))
        center.columnconfigure(0, weight=1)
        center.rowconfigure(0, weight=3)
        center.rowconfigure(1, weight=2)

        ing_frame = ttk.LabelFrame(center, text="Optimized Ingredients (1 serving)", padding=8)
        ing_frame.grid(row=0, column=0, sticky="nsew", pady=(0, 8))
        ing_frame.columnconfigure(0, weight=1)
        ing_frame.rowconfigure(0, weight=1)

        cols = ("name", "role", "quantity", "unit", "to_taste", "status")
        self.t_ingredients = ttk.Treeview(ing_frame, columns=cols, show="headings", height=6)
        for col, (head, w, a) in zip(cols, (("Ingredient", 190, "w"), ("Slot / Role", 120, "w"), ("Quantity", 80, "e"),
                                            ("Unit", 60, "center"), ("To taste (q.b.)", 95, "center"), ("Pantry Status", 120, "center"))):
            self.t_ingredients.heading(col, text=head)
            self.t_ingredients.column(col, width=w, anchor=a)

        sb = ttk.Scrollbar(ing_frame, orient="vertical", command=self.t_ingredients.yview)
        self.t_ingredients.configure(yscrollcommand=sb.set)
        self.t_ingredients.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")

        ttk.Label(ing_frame, text="ℹ️ 'To taste' (q.b. / quanto basta): discretionary seasoning added to taste (e.g. herbs, spices, grated cheese).",
                  font=("TkDefaultFont", 8, "italic")).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        notes_frame = ttk.LabelFrame(center, text="Synthesized Preparation Steps (Editable)", padding=8)
        notes_frame.grid(row=1, column=0, sticky="nsew")
        notes_frame.columnconfigure(0, weight=1)
        notes_frame.rowconfigure(0, weight=1)
        self.txt_notes = tk.Text(notes_frame, height=4, wrap="word", font=("TkDefaultFont", 9))
        notes_sb = ttk.Scrollbar(notes_frame, orient="vertical", command=self.txt_notes.yview)
        self.txt_notes.configure(yscrollcommand=notes_sb.set)
        self.txt_notes.grid(row=0, column=0, sticky="nsew")
        notes_sb.grid(row=0, column=1, sticky="ns")

        macro_frame = ttk.Frame(main)
        macro_frame.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(macro_frame, textvariable=self.v_macros, font=("TkDefaultFont", 9, "bold")).pack(side="left")

        btn_bar = ttk.Frame(main)
        btn_bar.grid(row=4, column=0, sticky="ew")
        self.btn_regenerate = ttk.Button(btn_bar, text="🎲 Regenerate / Try Another", command=self.regenerate_proposal)
        self.btn_regenerate.pack(side="left")
        ttk.Button(btn_bar, text="Cancel", command=self.cancel).pack(side="right", padx=(6, 0))
        self.btn_save = ttk.Button(btn_bar, text="💾 Save to Recipes", command=self.save)
        self.btn_save.pack(side="right")

    def generate_proposal(self, show_errors: bool = True) -> bool:
        from meal_planner.generator import RecipeGenerator
        meal_type = (self.v_meal.get() or "lunch").strip().lower()
        arch_sel = (self.v_archetype.get() or "").strip()
        archetype_name = None if (not arch_sel or arch_sel.startswith("Auto")) else arch_sel
        pantry_only = bool(self.v_pantry_only.get())

        try:
            raw_kcal = self.v_target_kcal.get().strip()
            target_kcal = float(raw_kcal.replace(",", ".")) if raw_kcal else None
            if target_kcal is not None and (not math.isfinite(target_kcal) or target_kcal <= 0):
                raise ValueError("Target kcal must be a positive number.")
        except ValueError as exc:
            self.last_error = str(exc)
            if show_errors and self._wait:
                messagebox.showerror("Invalid Input", str(exc), parent=self)
            return False

        try:
            gen = RecipeGenerator(db_path=self.db_path)
            try:
                candidate = gen.generate_recipe(target_kcal=target_kcal, meal_type=meal_type, archetype_name=archetype_name,
                                                pantry_only=pantry_only, db_path=self.db_path, variation_index=self._variation_index)
            except ValueError:
                if pantry_only:
                    candidate = gen.generate_recipe(target_kcal=target_kcal, meal_type=meal_type, archetype_name=archetype_name,
                                                    pantry_only=False, db_path=self.db_path, variation_index=self._variation_index)
                    candidate.warnings.append("Some required slots were missing in pantry; included catalog suggestions.")
                else:
                    raise
        except Exception as exc:
            self.last_error = str(exc)
            if show_errors and self._wait:
                messagebox.showerror("Generation Error", str(exc), parent=self)
            return False

        self.last_error = None
        self.current_candidate = candidate
        self._populate_candidate(candidate)
        return True

    def regenerate_proposal(self) -> bool:
        self._variation_index += 1
        return self.generate_proposal(show_errors=True)

    def _populate_candidate(self, candidate):
        self.v_title.set(candidate.title)
        warn_str = f"  |  ⚠️ {candidate.warnings[0]}" if candidate.warnings else ""
        self.v_meta.set(f"Archetype: {candidate.archetype_name}  |  Meal: {candidate.meal_type.capitalize()}  |  "
                        f"Solver: {candidate.solver_status}  |  Caloric delta: {candidate.caloric_delta_pct:.1f}%{warn_str}")

        for iid in self.t_ingredients.get_children():
            self.t_ingredients.delete(iid)
        for ing in candidate.ingredients:
            qty, unit = ing.calculate_display_quantity()
            role_label = ing.role.value if hasattr(ing.role, "value") else str(ing.role)
            status_str = "Missing (To buy)" if ing.is_missing else ("Opened (FIFO)" if ing.is_open else "In pantry")
            self.t_ingredients.insert("", "end", values=(ing.name, role_label, f"{qty:g}", unit, "Yes" if ing.to_taste else "No", status_str))

        self.txt_notes.delete("1.0", "end")
        self.txt_notes.insert("1.0", candidate.notes or "")
        self.v_macros.set(f"Per-serving values: {candidate.total_kcal:.0f} kcal (Target: {candidate.target_kcal:.0f} kcal)  |  "
                          f"Prot: {candidate.total_protein:.1f}g  |  Carbs: {candidate.total_carbs:.1f}g  |  "
                          f"Fat: {candidate.total_fat:.1f}g  |  Fiber: {candidate.total_fiber:.1f}g")

    def save(self) -> bool:
        from meal_planner import recipes as rc
        if self.current_candidate is None and not self.generate_proposal(show_errors=True):
            return False
        title = self.v_title.get().strip()
        if not title:
            messagebox.showerror("Missing Recipe Name", "Please enter a name for the recipe.", parent=self)
            return False
        notes = self.txt_notes.get("1.0", "end-1c").strip()
        meal_type = (self.v_meal.get() or self.current_candidate.meal_type or "lunch").strip().lower()
        kwargs = self.current_candidate.to_create_recipe_kwargs()
        kwargs["name"] = title
        kwargs["meals"] = [meal_type]
        kwargs["notes"] = notes
        try:
            with self._get_conn() as conn:
                outcome = rc.create_recipe(conn, **kwargs)
        except Exception as exc:
            self.last_error = str(exc)
            if self._wait: messagebox.showerror("Save Error", str(exc), parent=self)
            return False
        outcome["Name"] = title
        self.saved_outcome = outcome
        if callable(self.on_save_callback):
            self.on_save_callback(outcome)
        self.destroy()
        return True

    def _on_cancel(self):
        self.saved_outcome = None
        super()._on_cancel()

    def cancel(self):
        self._on_cancel()
