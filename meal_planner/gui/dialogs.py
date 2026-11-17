"""Modal dialogs."""

import contextlib
from datetime import date
import math
import re
from tkinter import messagebox, ttk
import tkinter as tk
from typing import Any, Dict, List, Optional, Tuple

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
            mx = master.winfo_rootx()
            my = master.winfo_rooty()
            mw = master.winfo_width()
            mh = master.winfo_height()
            dw = dialog.winfo_reqwidth()
            dh = dialog.winfo_reqheight()
            x = max(0, mx + (mw - dw) // 2)
            y = max(0, my + (mh - dh) // 2)
            dialog.geometry(f"+{x}+{y}")
        else:
            sw = dialog.winfo_screenwidth()
            sh = dialog.winfo_screenheight()
            dw = dialog.winfo_reqwidth()
            dh = dialog.winfo_reqheight()
            x = max(0, (sw - dw) // 2)
            y = max(0, (sh - dh) // 2)
            dialog.geometry(f"+{x}+{y}")
    except Exception:
        pass
    try:
        dialog.lift()
        dialog.focus_force()
    except Exception:
        pass


class ProductDialog(tk.Toplevel):
    """Asks for the data of a product not yet in the archive."""

    def __init__(self, master, name, suggested_id):
        super().__init__(master)
        self.title("New product")
        self.transient(master)
        self.result = None
        ttk.Label(self, wraplength=380, padding=10, text=(
            f"'{name}' is not in the archive yet.\nEnter the ID: if it already exists the name becomes an alias "
            "of that product; if it is new, also fill in unit and values per 100 g.")).pack()
        self.form = Form(self, "", [("id", "Product ID", None), ("unit", "Unit", ["kg", "L", "pcs"]),
                                    ("kcal", "Kcal", None), ("prot", "Protein (g)", None),
                                    ("carb", "Carbs (g)", None), ("fat", "Fat (g)", None)],
                         "Confirm", self.confirm)
        self.form.pack(padx=10, pady=(0, 10))
        self.form.v["id"].set(suggested_id)
        self.grab_set()
        self.wait_window()

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


class ChoiceDialog(tk.Toplevel):
    """Asks the user to pick a value from a list."""

    def __init__(self, master, title, values):
        super().__init__(master)
        self.title(title)
        self.transient(master)
        self.result = None
        self.v = tk.StringVar()
        ttk.Label(self, text=title, padding=10).pack()
        ttk.Combobox(self, textvariable=self.v, values=values, state="readonly", width=34).pack(padx=10)
        ttk.Button(self, text="Confirm", command=self.confirm).pack(pady=10)
        self.grab_set()
        self.wait_window()

    def confirm(self):
        self.result = self.v.get() or None
        self.destroy()


class AliasMapDialog(tk.Toplevel):
    """
    Modal dialog shown after receipt item selection.
    Lets the user map unrecognized receipt labels (aliases) to existing DB products.
    `items`: list of str — unrecognized labels from the receipt.
    After OK, self.result is a dict {label: canonical_name_or_None}.
    """
    def __init__(self, parent, items: list, known_names: list):
        super().__init__(parent)
        self.title("Map Receipt Items to Database Products")
        self.transient(parent)
        self.resizable(True, True)
        self.result = {}
        self.grab_set()

        ttk.Label(self, text="Map unrecognized receipt items to known products:", 
                  padding=(10, 8)).pack(anchor="w")

        frame = ttk.Frame(self, padding=10)
        frame.pack(fill="both", expand=True)

        self._vars = {}
        for i, item in enumerate(items):
            ttk.Label(frame, text=item, width=25, anchor="w").grid(row=i, column=0, sticky="w", pady=2)
            var = tk.StringVar()
            cb = ttk.Combobox(frame, textvariable=var, values=["(skip)"] + known_names, width=30)
            cb.set("(skip)")
            cb.grid(row=i, column=1, sticky="ew", padx=(8, 0), pady=2)
            self._vars[item] = var
        frame.columnconfigure(1, weight=1)

        btn_frame = ttk.Frame(self, padding=(10, 0, 10, 10))
        btn_frame.pack(fill="x")
        ttk.Button(btn_frame, text="OK", command=self._ok).pack(side="right")
        ttk.Button(btn_frame, text="Cancel", command=self.destroy).pack(side="right", padx=(0, 6))

        self.wait_window(self)

    def _ok(self):
        self.result = {
            label: (var.get() if var.get() != "(skip)" else None)
            for label, var in self._vars.items()
        }
        self.destroy()


class MealPrepDialog(tk.Toplevel):
    """
    Modal dialog to schedule an advance meal prep.
    User selects a recipe, cook date, and target meal dates.
    """
    def __init__(self, parent, recipes: list, default_cook_date: str = None):
        super().__init__(parent)
        self.title("Advance Meal Prep")
        self.transient(parent)
        self.resizable(True, True)
        self.result = None
        self.recipes = recipes  # list of (recipe_id, name)
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
        ttk.Button(btn_frame, text="Cancel", command=self.destroy).pack(side="right", padx=(0, 6))

        self.grab_set()
        self.wait_window(self)

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

        self.result = {
            "recipe_id": recipe_id,
            "cook_date": cook_date,
            "target_dates": target_dates
        }
        self.destroy()


# ----------------------------------------------------------------------
# RECEIPT RECONCILIATION DIALOG (Milestone 3)
# ----------------------------------------------------------------------

COLOR_RECOGNIZED = "#30a46c"    # Green
COLOR_UNRECOGNIZED = "#f5a524"  # Orange

# Cache for pulse-flash ttk PhotoImages & styles per Tk session/theme
_RECONCILIATION_FLASH_STYLES = {}


class RoundedEntry(ttk.Entry):
    """
    Fluent / sv_ttk rounded entry matching the modern application theme,
    with full support for dynamic color animations (smooth red pulse to yellow)
    and backward-compatible bg / cget('bg') queries.
    """
    def __init__(self, master=None, dialog=None, **kwargs):
        self._dialog = dialog
        self._bg = kwargs.pop("bg", kwargs.pop("background", getattr(dialog, "color_std", "#2b2b2b")))
        self._fg = kwargs.pop("fg", kwargs.pop("foreground", None))
        kwargs.pop("insertbackground", None)
        kwargs.pop("relief", None)
        kwargs.pop("bd", None)
        kwargs.pop("highlightthickness", None)
        kwargs.pop("highlightbackground", None)
        kwargs.pop("highlightcolor", None)
        kwargs.pop("font", None)
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


class ReceiptReconciliationDialog(tk.Toplevel):
    """
    Modal dialog to reconcile receipt items with the product catalog and inventory.
    Allows mapping receipt labels to existing products or creating new products
    with nutritional macros, setting unit weight, packages, location, and expiry date.
    """

    def __init__(
        self,
        master,
        items: List[Any],
        db_path: Optional[str] = None,
        wait: bool = True,
    ):
        super().__init__(master)
        self.title("Receipt Reconciliation - Review & Inventory Import")
        self.transient(master)
        self.resizable(True, True)
        self.geometry("1180x620")
        self.minsize(860, 420)
        self.db_path = db_path
        self.result = None
        self.last_error = None

        from meal_planner.gui import theme
        current_theme = getattr(master, "theme", None) or theme.load()
        self.is_dark = (current_theme == "dark")
        bg_std, fg_std = theme.palette(current_theme)
        if self.is_dark:
            self.color_std = bg_std
            self.color_fg = fg_std
            self.color_red = "#991b1b"
            self.color_yellow = "#524314"
        else:
            self.color_std = bg_std
            self.color_fg = fg_std
            self.color_red = "#ef4444"
            self.color_yellow = "#fef9c3"

        self._active_error_widgets = set()
        self._anim_timer_id = None
        self._init_rounded_styles()

        self._convert_unit = convert_receipt_quantity_unit

        try:
            self.catalog = get_catalog_for_reconciliation(db_path=self.db_path)
        except Exception:
            self.catalog = {"products": [], "alias_map": {}}

        self.product_by_name = {
            p["canonical_name"]: p for p in self.catalog.get("products", [])
        }
        self.alias_map = self.catalog.get("alias_map", {})

        try:
            self.locations = get_locations(db_path=self.db_path)
        except Exception:
            self.locations = []
        if not self.locations:
            self.locations = ["freezer", "fridge", "pantry"]

        try:
            self.next_product_id = get_next_product_id(db_path=self.db_path)
        except Exception:
            self.next_product_id = 1

        self.rows = []
        self._rows = self.rows

        self._setup_ui(items)

        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.bind("<Escape>", lambda e: self._on_cancel())

        try:
            self.grab_set()
        except Exception:
            pass

        try:
            if wait:
                self.wait_window(self)
        finally:
            try:
                self.grab_release()
            except Exception:
                pass

    def destroy(self):
        if hasattr(self, "_anim_timer_id") and self._anim_timer_id is not None:
            try:
                self.after_cancel(self._anim_timer_id)
            except Exception:
                pass
            self._anim_timer_id = None
        try:
            self.grab_release()
        except Exception:
            pass
        if hasattr(self, "_style_photos"):
            self._style_photos = []
        super().destroy()

    def _init_rounded_styles(self):
        cache_key = (id(self.tk), bool(self.is_dark))
        if cache_key in _RECONCILIATION_FLASH_STYLES:
            self._style_photos = _RECONCILIATION_FLASH_STYLES[cache_key]
            try:
                style = ttk.Style(self)
                theme_tag = "dark" if self.is_dark else "light"
                for i in range(26):
                    elem_name = f"RecFlash_{theme_tag}_{i}.field"
                    style_name = f"RecFlash_{i}.TEntry"
                    style.layout(style_name, [(elem_name, {"sticky": "nswe", "children": [("Entry.padding", {"sticky": "nswe", "children": [("Entry.textarea", {"sticky": "nswe"})]})]})])
            except Exception:
                pass
            return

        self._style_photos = []
        try:
            from PIL import Image, ImageDraw, ImageTk
            style = ttk.Style(self)
            total_steps = 25
            theme_tag = "dark" if self.is_dark else "light"
            for i in range(total_steps + 1):
                if i == 0:
                    fill = self.color_std
                    border = "#444444" if self.is_dark else "#cccccc"
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
        ent = RoundedEntry(
            self.table_frame,
            dialog=self,
            textvariable=textvariable,
            width=width,
            justify=justify,
            bg=self.color_std,
            fg=self.color_fg,
        )
        return ent

    @staticmethod
    def _interpolate_hex(c1: str, c2: str, t: float) -> str:
        t = max(0.0, min(1.0, t))
        try:
            r1, g1, b1 = int(c1[1:3], 16), int(c1[3:5], 16), int(c1[5:7], 16)
            r2, g2, b2 = int(c2[1:3], 16), int(c2[3:5], 16), int(c2[5:7], 16)
            r = int(r1 + (r2 - r1) * t)
            g = int(g1 + (g2 - g1) * t)
            b = int(b1 + (b2 - b1) * t)
            return f"#{r:02x}{g:02x}{b:02x}"
        except Exception:
            return c2

    @staticmethod
    def _set_widget_bg(widget, color: str):
        try:
            widget.configure(bg=color)
        except Exception:
            try:
                widget.configure(background=color)
            except Exception:
                pass

    def _flash_error_widgets(self, widgets):
        """
        Gradually animates error widgets through a smooth red pulse (0 to 500ms)
        and transitions into a delicate light yellow (500 to 1000ms), where they
        persist to remind the user of remaining invalid fields.
        """
        if self._anim_timer_id is not None:
            try:
                self.after_cancel(self._anim_timer_id)
            except Exception:
                pass
            self._anim_timer_id = None

        if not widgets:
            return

        total_steps = 25  # 25 steps * 40ms = 1000ms (1 second)
        step_interval_ms = 40

        def _step(current_step):
            if not self.winfo_exists():
                return

            if current_step <= 12:
                # 0ms to 480ms: smooth fade from standard to red
                t = current_step / 12.0
                hex_col = self._interpolate_hex(self.color_std, self.color_red, t)
            else:
                # 480ms to 1000ms: smooth fade from red to transparent-like yellow
                t = (current_step - 12) / 13.0
                hex_col = self._interpolate_hex(self.color_red, self.color_yellow, t)

            for w in widgets:
                if hasattr(w, "winfo_exists"):
                    try:
                        if w.winfo_exists():
                            self._set_widget_bg(w, hex_col)
                            if isinstance(w, RoundedEntry):
                                w.configure(style=f"RecFlash_{current_step}.TEntry")
                    except Exception:
                        pass

            if current_step < total_steps:
                self._anim_timer_id = self.after(step_interval_ms, lambda: _step(current_step + 1))
            else:
                self._anim_timer_id = None
                for w in widgets:
                    if hasattr(w, "winfo_exists"):
                        try:
                            if w.winfo_exists():
                                self._set_widget_bg(w, self.color_yellow)
                                if isinstance(w, RoundedEntry):
                                    w.configure(style="RecFlash_25.TEntry")
                        except Exception:
                            pass

        _step(0)

    def _setup_ui(self, items: list):
        # Header Frame
        header = ttk.Frame(self, padding=(14, 10, 14, 4))
        header.pack(fill="x")
        ttk.Label(
            header,
            text="Receipt Items Reconciliation",
            font=("TkDefaultFont", 11, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            header,
            text="Review parsed receipt items below. Map items to existing products or define new products with nutritional values.",
        ).pack(anchor="w", pady=(2, 6))

        # Legend Frame
        legend = ttk.Frame(header)
        legend.pack(anchor="w", pady=(0, 4))

        c_rec = tk.Canvas(legend, width=16, height=16, highlightthickness=0, bg=self.color_std)
        c_rec.create_oval(2, 2, 14, 14, fill=COLOR_RECOGNIZED, outline=COLOR_RECOGNIZED)
        c_rec.pack(side="left")
        ttk.Label(legend, text=" Recognized / Catalog product    ").pack(side="left")

        c_unrec = tk.Canvas(legend, width=16, height=16, highlightthickness=0, bg=self.color_std)
        c_unrec.create_oval(2, 2, 14, 14, fill=COLOR_UNRECOGNIZED, outline=COLOR_UNRECOGNIZED)
        c_unrec.pack(side="left")
        ttk.Label(legend, text=" Unrecognized / New product (requires macros)").pack(side="left")

        # Scrollable Area Container
        container = ttk.Frame(self, style="Card.TFrame", padding=6)
        container.pack(fill="both", expand=True, padx=12, pady=4)

        try:
            bg = ttk.Style(self).lookup("TFrame", "background")
        except Exception:
            bg = None
        if not bg:
            bg = self.color_std

        self.canvas = tk.Canvas(container, highlightthickness=0, background=bg)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)

        scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.table_frame = ttk.Frame(self.canvas)
        self.canvas_win = self.canvas.create_window((0, 0), window=self.table_frame, anchor="nw")

        self.table_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        def _on_canvas_configure(e):
            try:
                if self.canvas.winfo_exists() and self.table_frame.winfo_exists():
                    min_w = self.table_frame.winfo_reqwidth()
                    target_w = max(e.width, min_w)
                    self.canvas.itemconfig(self.canvas_win, width=target_w)
            except Exception:
                pass
        self.canvas.bind("<Configure>", _on_canvas_configure)

        self._bind_mousewheel(self)
        self._bind_mousewheel(container)
        self._bind_mousewheel(self.canvas)
        self._bind_mousewheel(self.table_frame)

        # Headers
        headers = [
            ("", 2),
            ("Receipt Name", 18),
            ("Catalog Product", 22),
            ("Inventory Name", 18),
            ("Unit", 5),
            ("Weight/Vol", 8),
            ("Packs", 5),
            ("Kcal", 6),
            ("Prot (g)", 6),
            ("Carb (g)", 6),
            ("Fat (g)", 6),
            ("Location", 10),
            ("Expiry (YYYY-MM-DD)", 12),
        ]
        for col_idx, (text, width) in enumerate(headers):
            lbl = ttk.Label(self.table_frame, text=text, font=("TkDefaultFont", 9, "bold"))
            lbl.grid(row=0, column=col_idx, padx=3, pady=(2, 6), sticky="w")

        catalog_names = sorted(list(self.product_by_name.keys()), key=lambda s: s.lower())
        self.product_options = ["[New Product]"] + catalog_names

        for row_idx, item in enumerate(items, start=1):
            self._create_row(row_idx, item)

        # Bottom Button Frame
        btn_frame = ttk.Frame(self, padding=(14, 10))
        btn_frame.pack(fill="x", side="bottom")
        self.btn_cancel = ttk.Button(btn_frame, text="Cancel", command=self._on_cancel)
        self.btn_cancel.pack(side="right", padx=(6, 0))
        self.btn_save = ttk.Button(btn_frame, text="Save to Database", style="Accent.TButton", command=self._on_save)
        self.btn_save.pack(side="right")

        # Dynamically size window width to match all item controls up to max screen width
        try:
            self.update_idletasks()
            screen_w = self.winfo_screenwidth()
            screen_h = self.winfo_screenheight()
            req_w = self.table_frame.winfo_reqwidth()
            needed_w = max(req_w + 60, 1260)
            final_w = min(needed_w, max(400, screen_w - 40))
            final_h = min(680, max(360, screen_h - 80))
            pos_x = max(0, (screen_w - final_w) // 2)
            pos_y = max(0, (screen_h - final_h) // 2)
            self.geometry(f"{final_w}x{final_h}+{pos_x}+{pos_y}")
            self.minsize(min(600, max(300, screen_w - 20)), min(360, max(240, screen_h - 40)))
        except Exception:
            pass

    def _bind_mousewheel(self, widget):
        def _on_mousewheel(e, delta=None):
            if not self.winfo_exists():
                return
            try:
                d = delta if delta is not None else (-1 if e.delta > 0 else 1)
                self.canvas.yview_scroll(d, "units")
            except Exception:
                pass

        for ev, d in (("<Button-4>", -1), ("<Button-5>", 1)):
            widget.bind(ev, lambda e, delta=d: _on_mousewheel(e, delta), add="+")
        widget.bind("<MouseWheel>", lambda e: _on_mousewheel(e), add="+")

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

        try:
            q_val = float(str(raw_qty).replace(",", "."))
        except Exception:
            q_val = 1.0

        try:
            packs_val = int(raw_packs)
        except Exception:
            packs_val = 1

        matched_prod = self.alias_map.get(raw_name.strip().lower())
        target_u = matched_prod.get("unit") if matched_prod else None
        g_piece = matched_prod.get("grams_per_piece") if matched_prod else None
        dens = matched_prod.get("density_kg_per_l") if matched_prod else None

        try:
            norm_qty, norm_unit = self._convert_unit(
                q_val,
                str(raw_unit),
                target_unit=target_u,
                grams_per_piece=g_piece,
                density_kg_per_l=dens,
            )
        except Exception:
            try:
                norm_qty, norm_unit = self._convert_unit(q_val, str(raw_unit))
            except Exception:
                norm_qty = q_val
                u_str = str(raw_unit).strip().lower()
                if u_str in ("kg", "kgs", "g", "gr"):
                    norm_unit = "kg"
                elif u_str in ("l", "lt", "liter", "litro", "ml", "cl"):
                    norm_unit = "L"
                else:
                    norm_unit = "pcs"

        is_recognized = (matched_prod is not None)

        status_canvas = tk.Canvas(self.table_frame, width=16, height=16, highlightthickness=0, bg=self.color_std)
        rect_id = status_canvas.create_oval(
            2, 2, 14, 14,
            fill=COLOR_RECOGNIZED if is_recognized else COLOR_UNRECOGNIZED,
            outline=COLOR_RECOGNIZED if is_recognized else COLOR_UNRECOGNIZED,
        )
        status_canvas.grid(row=row_idx, column=0, padx=2, pady=3)

        lbl_raw = ttk.Label(self.table_frame, text=raw_name, width=18, anchor="w")
        lbl_raw.grid(row=row_idx, column=1, padx=3, pady=3, sticky="w")

        initial_prod_choice = matched_prod["canonical_name"] if is_recognized else "[New Product]"
        product_var = tk.StringVar(value=initial_prod_choice)
        cb_prod = ttk.Combobox(
            self.table_frame,
            textvariable=product_var,
            values=self.product_options,
            state="readonly",
            width=22,
        )
        cb_prod.grid(row=row_idx, column=2, padx=3, pady=3, sticky="ew")

        name_var = tk.StringVar(value=raw_name.strip())
        ent_name = self._create_entry(name_var, width=18)
        ent_name.grid(row=row_idx, column=3, padx=3, pady=3, sticky="ew")

        frame_unit = tk.Frame(self.table_frame, bg=self.color_std, padx=1, pady=1)
        frame_unit.grid(row=row_idx, column=4, padx=3, pady=3, sticky="w")
        unit_var = tk.StringVar(value=norm_unit)
        cb_unit = ttk.Combobox(
            frame_unit,
            textvariable=unit_var,
            values=["kg", "L", "pcs"],
            state="readonly",
            width=5,
        )
        cb_unit.pack()

        weight_var = tk.StringVar(value=f"{norm_qty:g}")
        ent_weight = self._create_entry(weight_var, width=8, justify="right")
        ent_weight.grid(row=row_idx, column=5, padx=3, pady=3, sticky="ew")

        packs_var = tk.StringVar(value=str(packs_val))
        ent_packs = self._create_entry(packs_var, width=5, justify="right")
        ent_packs.grid(row=row_idx, column=6, padx=3, pady=3, sticky="ew")

        kcal_val = f"{matched_prod['kcal']:g}" if is_recognized and matched_prod.get("kcal") is not None else ""
        prot_val = f"{matched_prod['protein']:g}" if is_recognized and matched_prod.get("protein") is not None else ""
        carb_val = f"{matched_prod['carbs']:g}" if is_recognized and matched_prod.get("carbs") is not None else ""
        fat_val = f"{matched_prod['fat']:g}" if is_recognized and matched_prod.get("fat") is not None else ""

        kcal_var = tk.StringVar(value=kcal_val)
        ent_kcal = self._create_entry(kcal_var, width=6, justify="right")
        ent_kcal.grid(row=row_idx, column=7, padx=3, pady=3, sticky="ew")

        prot_var = tk.StringVar(value=prot_val)
        ent_prot = self._create_entry(prot_var, width=6, justify="right")
        ent_prot.grid(row=row_idx, column=8, padx=3, pady=3, sticky="ew")

        carb_var = tk.StringVar(value=carb_val)
        ent_carb = self._create_entry(carb_var, width=6, justify="right")
        ent_carb.grid(row=row_idx, column=9, padx=3, pady=3, sticky="ew")

        fat_var = tk.StringVar(value=fat_val)
        ent_fat = self._create_entry(fat_var, width=6, justify="right")
        ent_fat.grid(row=row_idx, column=10, padx=3, pady=3, sticky="ew")

        def_loc = "pantry" if "pantry" in self.locations else (self.locations[0] if self.locations else "pantry")
        loc_var = tk.StringVar(value=def_loc)
        cb_loc = ttk.Combobox(
            self.table_frame,
            textvariable=loc_var,
            values=self.locations,
            state="readonly",
            width=10,
        )
        cb_loc.grid(row=row_idx, column=11, padx=3, pady=3, sticky="ew")

        exp_var = tk.StringVar(value="")
        ent_exp = self._create_entry(exp_var, width=12)
        ent_exp.grid(row=row_idx, column=12, padx=3, pady=3, sticky="ew")

        row_record = {
            "status_canvas": status_canvas,
            "canvas": status_canvas,
            "rect_id": rect_id,
            "lbl_raw": lbl_raw,
            "product_var": product_var,
            "cb_prod": cb_prod,
            "name_var": name_var,
            "ent_name": ent_name,
            "unit_var": unit_var,
            "frame_unit": frame_unit,
            "cb_unit": cb_unit,
            "weight_var": weight_var,
            "ent_weight": ent_weight,
            "packs_var": packs_var,
            "ent_packs": ent_packs,
            "kcal_var": kcal_var,
            "ent_kcal": ent_kcal,
            "prot_var": prot_var,
            "ent_prot": ent_prot,
            "carb_var": carb_var,
            "ent_carb": ent_carb,
            "fat_var": fat_var,
            "ent_fat": ent_fat,
            "loc_var": loc_var,
            "cb_loc": cb_loc,
            "exp_var": exp_var,
            "ent_exp": ent_exp,
            "raw_name": raw_name,
            "raw_qty": raw_qty,
            "raw_unit": raw_unit,
            "raw_packs": raw_packs,
            "status": COLOR_RECOGNIZED if is_recognized else COLOR_UNRECOGNIZED,
            "_last_choice": initial_prod_choice,
        }
        self.rows.append(row_record)

        cb_prod.bind(
            "<<ComboboxSelected>>",
            lambda e, r=row_record: self._on_product_change(r),
        )
        product_var.trace_add(
            "write",
            lambda *args, r=row_record: self._on_product_change(r),
        )

        def _on_macro_write(*args, r=row_record):
            self._update_row_status(r)

        for v in (kcal_var, prot_var, carb_var, fat_var):
            v.trace_add("write", _on_macro_write)

    def _update_row_status(self, row: dict):
        choice = row["product_var"].get().strip()
        matched = (
            self.product_by_name.get(choice)
            or self.alias_map.get(choice.lower())
        )
        if choice != "[New Product]" and matched is not None:
            color = COLOR_RECOGNIZED
        else:
            k = row["kcal_var"].get().strip().replace(",", ".")
            p = row["prot_var"].get().strip().replace(",", ".")
            c = row["carb_var"].get().strip().replace(",", ".")
            f = row["fat_var"].get().strip().replace(",", ".")
            if k and p and c and f:
                try:
                    kv, pv, cv, fv = float(k), float(p), float(c), float(f)
                    if (
                        math.isfinite(kv)
                        and math.isfinite(pv)
                        and math.isfinite(cv)
                        and math.isfinite(fv)
                        and kv >= 0
                        and pv >= 0
                        and cv >= 0
                        and fv >= 0
                    ):
                        color = COLOR_RECOGNIZED
                    else:
                        color = COLOR_UNRECOGNIZED
                except ValueError:
                    color = COLOR_UNRECOGNIZED
            else:
                color = COLOR_UNRECOGNIZED

        row["status_canvas"].itemconfig(
            row["rect_id"], fill=color, outline=color
        )
        row["status"] = color

    def _on_product_change(self, row: dict):
        last_choice = row.get("_last_choice")
        choice = row["product_var"].get().strip()
        row["_last_choice"] = choice

        prod = (
            self.product_by_name.get(choice)
            or self.alias_map.get(choice.lower())
        )
        if choice != "[New Product]" and prod is not None:
            target_u = prod.get("unit")
            g_piece = prod.get("grams_per_piece")
            dens = prod.get("density_kg_per_l")

            if target_u:
                row["unit_var"].set(target_u)

            # Re-convert raw receipt quantity to target product unit if possible
            try:
                raw_q = float(str(row.get("raw_qty", 1.0)).replace(",", "."))
                raw_u = str(row.get("raw_unit", "pcs"))
                c_qty, c_unit = self._convert_unit(
                    raw_q,
                    raw_u,
                    target_unit=target_u,
                    grams_per_piece=g_piece,
                    density_kg_per_l=dens,
                )
                row["weight_var"].set(f"{c_qty:g}")
                if c_unit:
                    row["unit_var"].set(c_unit)
            except Exception:
                pass

            row["kcal_var"].set(f"{prod['kcal']:g}" if prod.get("kcal") is not None else "")
            row["prot_var"].set(f"{prod['protein']:g}" if prod.get("protein") is not None else "")
            row["carb_var"].set(f"{prod['carbs']:g}" if prod.get("carbs") is not None else "")
            row["fat_var"].set(f"{prod['fat']:g}" if prod.get("fat") is not None else "")
        elif choice == "[New Product]" and last_choice is not None and last_choice != "[New Product]":
            row["kcal_var"].set("")
            row["prot_var"].set("")
            row["carb_var"].set("")
            row["fat_var"].set("")
            try:
                raw_q = float(str(row.get("raw_qty", 1.0)).replace(",", "."))
                raw_u = str(row.get("raw_unit", "pcs"))
                orig_qty, orig_unit = self._convert_unit(raw_q, raw_u)
                row["weight_var"].set(f"{orig_qty:g}")
                row["unit_var"].set(orig_unit)
            except Exception:
                pass

        self._update_row_status(row)

    def _on_save(self) -> bool:
        errors = []
        result = []
        assigned_new_ids = {}
        current_error_widgets = set()

        try:
            base_id = get_next_product_id(db_path=self.db_path)
        except Exception:
            base_id = getattr(self, "next_product_id", 1) or 1

        for i, row in enumerate(self.rows, start=1):
            row_has_error = False

            inv_name = row["name_var"].get().strip()
            if not inv_name:
                errors.append(f"Row {i}: Name cannot be empty.")
                current_error_widgets.add(row["ent_name"])
                row_has_error = True

            unit = row["unit_var"].get().strip()
            if unit not in ("kg", "L", "pcs"):
                errors.append(f"Row {i} ({inv_name or 'item'}): Unit must be 'kg', 'L', or 'pcs'.")
                current_error_widgets.add(row.get("frame_unit", row.get("cb_unit")))
                row_has_error = True

            weight_str = row["weight_var"].get().strip().replace(",", ".")
            try:
                weight_val = float(weight_str)
                if not math.isfinite(weight_val) or weight_val <= 0:
                    raise ValueError
            except ValueError:
                errors.append(f"Row {i} ({inv_name or 'item'}): Weight/Volume must be a positive number.")
                current_error_widgets.add(row["ent_weight"])
                row_has_error = True
                weight_val = 1.0

            packs_str = row["packs_var"].get().strip().replace(",", ".")
            try:
                p_f = float(packs_str)
                if not math.isfinite(p_f) or not p_f.is_integer() or int(p_f) < 1:
                    raise ValueError
                packs_val = int(p_f)
            except ValueError:
                errors.append(f"Row {i} ({inv_name or 'item'}): Packages must be an integer >= 1.")
                current_error_widgets.add(row["ent_packs"])
                row_has_error = True
                packs_val = 1

            exp_str = row["exp_var"].get().strip()
            exp_date = None
            if exp_str:
                if not re.match(r"^\d{4}-\d{2}-\d{2}$", exp_str):
                    errors.append(f"Row {i} ({inv_name or 'item'}): Expiration date must be YYYY-MM-DD (e.g. 2026-12-31).")
                    current_error_widgets.add(row["ent_exp"])
                    row_has_error = True
                else:
                    try:
                        date.fromisoformat(exp_str)
                        exp_date = exp_str
                    except ValueError:
                        errors.append(f"Row {i} ({inv_name or 'item'}): Invalid date '{exp_str}'.")
                        current_error_widgets.add(row["ent_exp"])
                        row_has_error = True

            loc = row["loc_var"].get().strip()
            if not loc:
                loc = "pantry"

            prod_choice = row["product_var"].get().strip()
            matched_prod = (
                self.product_by_name.get(prod_choice)
                or self.alias_map.get(prod_choice.lower())
            )
            if prod_choice != "[New Product]" and matched_prod is not None:
                is_new = False
                prod = matched_prod
                canonical_name = prod["canonical_name"]
                fallback_id = prod["id"]
                new_unit = None
                new_macros = None
            else:
                is_new = True
                canonical_name = inv_name
                macro_err = False
                for field_name, var_key, ent_key in (
                    ("Kcal", "kcal_var", "ent_kcal"),
                    ("Protein", "prot_var", "ent_prot"),
                    ("Carbs", "carb_var", "ent_carb"),
                    ("Fat", "fat_var", "ent_fat"),
                ):
                    val_str = row[var_key].get().strip().replace(",", ".")
                    try:
                        if not val_str:
                            raise ValueError
                        fval = float(val_str)
                        if not math.isfinite(fval) or fval < 0:
                            raise ValueError
                    except ValueError:
                        current_error_widgets.add(row[ent_key])
                        macro_err = True

                if macro_err:
                    errors.append(
                        f"Row {i} ({inv_name or 'item'}): For new products, nutritional macros (Kcal, Protein, Carbs, Fat) per 100g must be valid numbers >= 0."
                    )
                    row_has_error = True
                else:
                    kcal_val = float(row["kcal_var"].get().strip().replace(",", "."))
                    prot_val = float(row["prot_var"].get().strip().replace(",", "."))
                    carb_val = float(row["carb_var"].get().strip().replace(",", "."))
                    fat_val = float(row["fat_var"].get().strip().replace(",", "."))
                    new_unit = unit
                    new_macros = {
                        "Kcal_100g": kcal_val,
                        "Protein_100g": prot_val,
                        "Carbs_100g": carb_val,
                        "Fat_100g": fat_val,
                    }
                    norm_key = inv_name.lower()
                    if norm_key in assigned_new_ids:
                        fallback_id = assigned_new_ids[norm_key]
                    else:
                        fallback_id = base_id + len(assigned_new_ids)
                        assigned_new_ids[norm_key] = fallback_id

            if not row_has_error:
                if unit == "pcs":
                    unit_weight = 1.0
                    intact_items = max(1, int(round(weight_val * packs_val)))
                else:
                    unit_weight = weight_val
                    intact_items = packs_val

                result.append({
                    "name": inv_name,
                    "canonical_name": canonical_name,
                    "unit_weight": unit_weight,
                    "intact_items": intact_items,
                    "location": loc,
                    "expiration_date": exp_date,
                    "fallback_id": fallback_id,
                    "new_product_unit": new_unit,
                    "new_product_macros": new_macros,
                    "is_new_product": is_new,
                })

        # Dynamically restore widgets that are now valid
        previously_highlighted = getattr(self, "_active_error_widgets", set())
        recovered_widgets = previously_highlighted - current_error_widgets
        for w in recovered_widgets:
            if hasattr(w, "winfo_exists"):
                try:
                    if w.winfo_exists():
                        self._set_widget_bg(w, self.color_std)
                        if isinstance(w, RoundedEntry):
                            w.configure(style="TEntry")
                except Exception:
                    pass

        if errors or current_error_widgets:
            self.last_error = "\n".join(errors)
            self._active_error_widgets = current_error_widgets
            self._flash_error_widgets(current_error_widgets)
            return False

        # No errors: clean up highlights and commit result
        for w in self._active_error_widgets:
            if hasattr(w, "winfo_exists"):
                try:
                    if w.winfo_exists():
                        self._set_widget_bg(w, self.color_std)
                        if isinstance(w, RoundedEntry):
                            w.configure(style="TEntry")
                except Exception:
                    pass
        self._active_error_widgets.clear()
        self.last_error = None
        self.result = result
        self.destroy()
        return True

    def save(self) -> bool:
        """Alias for _on_save to support automated headless test calls."""
        return self._on_save()

    def _on_cancel(self):
        self.result = None
        self.destroy()

    def cancel(self):
        """Alias for _on_cancel to support automated headless test calls."""
        self._on_cancel()


class MacroPromptDialog(tk.Toplevel):
    """
    Prompt dialog shown when modifying macronutrients of a product whose macros
    are linked across multiple instances in the database (via aliases, multiple
    inventory rows, or recipes).

    Offers two explicit options:
      Option 1: "Modify only that specific item" (result = "single")
      Option 2: "Propagate the modification backwards across all linked instances" (result = "propagate")
    Or Cancel (result = None).
    """
    def __init__(self, parent, product_name: str, product_id: int = None, wait: bool = True):
        super().__init__(parent)
        self.title("Macronutrient Modification")
        self.transient(parent)
        self.resizable(False, False)
        self.result = None

        container = ttk.Frame(self, padding=16)
        container.pack(fill="both", expand=True)

        msg_title = ttk.Label(
            container,
            text=f"Modify Macronutrients for '{product_name}'",
            font=("TkDefaultFont", 11, "bold"),
            wraplength=440,
        )
        msg_title.pack(anchor="w", pady=(0, 8))

        explanation = ttk.Label(
            container,
            text=(
                "This product's macronutrients are linked across multiple items in the database "
                "(other inventory rows, aliases, or recipes).\n\n"
                "Please choose how to apply your changes:"
            ),
            wraplength=440,
        )
        explanation.pack(anchor="w", pady=(0, 14))

        # Option 1
        f1 = ttk.LabelFrame(container, text="Option 1: Modify only that specific item", padding=10)
        f1.pack(fill="x", pady=4)
        btn1 = ttk.Button(
            f1,
            text="Modify only that specific item",
            command=self._choose_single,
        )
        btn1.pack(anchor="w", pady=(0, 4))
        desc1 = ttk.Label(
            f1,
            text="Allocates a new product ID. Updates only this inventory item while leaving all other linked instances and recipes untouched.",
            font=("TkDefaultFont", 8, "italic"),
            wraplength=420,
        )
        desc1.pack(anchor="w")

        # Option 2
        f2 = ttk.LabelFrame(container, text="Option 2: Propagate modification backwards across all linked instances", padding=10)
        f2.pack(fill="x", pady=6)
        btn2 = ttk.Button(
            f2,
            text="Propagate the modification backwards across all linked instances",
            command=self._choose_propagate,
        )
        btn2.pack(anchor="w", pady=(0, 4))
        desc2 = ttk.Label(
            f2,
            text="Updates the product record in place, preserving its product ID so all linked rows, aliases, and recipes reflect the changes.",
            font=("TkDefaultFont", 8, "italic"),
            wraplength=420,
        )
        desc2.pack(anchor="w")

        # Buttons
        b_bar = ttk.Frame(container, padding=(0, 10, 0, 0))
        b_bar.pack(fill="x")
        ttk.Button(b_bar, text="Cancel", command=self._cancel).pack(side="right")

        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Escape>", lambda e: self._cancel())

        try:
            self.grab_set()
        except Exception:
            pass

        if wait:
            self.wait_window(self)

    def _choose_single(self):
        self.result = "single"
        self.destroy()

    def _choose_propagate(self):
        self.result = "propagate"
        self.destroy()

    def _cancel(self):
        self.result = None
        self.destroy()

    def choose_single(self):
        self._choose_single()

    def choose_propagate(self):
        self._choose_propagate()

    def cancel(self):
        self._cancel()


class InventoryItemEditDialog(tk.Toplevel):
    """
    Modal dialog allowing the user to view and edit all characteristics of an inventory item:
      - Inventory & identity characteristics: Name, Unit_Weight, Intact_Items, Open_Remainder, Location, Expiration_Date
      - Product characteristics: Unit, Grams_per_piece, Density_kg_per_L
      - Nutritional macronutrients: Kcal, Protein, Carbs, Fat, Sugars, Saturated_fat, Fiber, Salt

    Pre-filled with all current characteristics from INVENTORY, PRODUCTS, and ALIASES.
    Performs range validations and handles macro linkage prompts.
    """
    def __init__(self, parent, row_id: int, db_path: str = None, wait: bool = True):
        super().__init__(parent)
        self.title("Edit Inventory Item")
        self.transient(parent)
        self.resizable(True, True)
        self.row_id = row_id
        self.db_path = db_path
        self.result = None
        self.last_error = None

        self.item_data = get_inventory_item(self.row_id, db_path=self.db_path)
        if not self.item_data:
            messagebox.showerror("Item Not Found", f"Inventory row #{row_id} does not exist.", parent=self)
            self.destroy()
            return

        self._init_variables()
        self._init_ui()

        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.bind("<Escape>", lambda e: self._on_cancel())

        try:
            self.grab_set()
        except Exception:
            pass

        if wait:
            self.wait_window(self)

    def _init_variables(self):
        d = self.item_data
        self.v_name = tk.StringVar(value=d.get("Name") or "")
        self.v_location = tk.StringVar(value=d.get("Location") or "")
        self.v_expiration = tk.StringVar(value=d.get("Expiration_Date") or "")
        self.v_unit_weight = tk.StringVar(value=f"{d['Unit_Weight']:g}" if d.get("Unit_Weight") is not None else "")
        self.v_intact = tk.StringVar(value=str(d.get("Intact_Items", 0)))
        self.v_remainder = tk.StringVar(value=f"{d['Open_Remainder']:g}" if d.get("Open_Remainder") is not None else "0")

        self.v_unit = tk.StringVar(value=d.get("Unit") or "kg")
        self.v_kcal = tk.StringVar(value=f"{d['Kcal_100g']:g}" if d.get("Kcal_100g") is not None else "")
        self.v_protein = tk.StringVar(value=f"{d['Protein_100g']:g}" if d.get("Protein_100g") is not None else "")
        self.v_carbs = tk.StringVar(value=f"{d['Carbs_100g']:g}" if d.get("Carbs_100g") is not None else "")
        self.v_fat = tk.StringVar(value=f"{d['Fat_100g']:g}" if d.get("Fat_100g") is not None else "")
        self.v_sugars = tk.StringVar(value=f"{d['Sugars_100g']:g}" if d.get("Sugars_100g") is not None else "")
        self.v_saturated_fat = tk.StringVar(value=f"{d['Saturated_fat_100g']:g}" if d.get("Saturated_fat_100g") is not None else "")
        self.v_fiber = tk.StringVar(value=f"{d['Fiber_100g']:g}" if d.get("Fiber_100g") is not None else "")
        self.v_salt = tk.StringVar(value=f"{d['Salt_100g']:g}" if d.get("Salt_100g") is not None else "")
        self.v_grams_per_piece = tk.StringVar(value=f"{d['Grams_per_piece']:g}" if d.get("Grams_per_piece") is not None else "")
        self.v_density = tk.StringVar(value=f"{d['Density_kg_per_L']:g}" if d.get("Density_kg_per_L") is not None else "")

    def _init_ui(self):
        main_frame = ttk.Frame(self, padding=14)
        main_frame.pack(fill="both", expand=True)

        header = ttk.Label(
            main_frame,
            text=f"Editing Inventory Item #{self.row_id} — {self.item_data.get('Name')}",
            font=("TkDefaultFont", 11, "bold"),
        )
        header.pack(anchor="w", pady=(0, 12))

        # Section 1: Item & Inventory Characteristics
        f_inv = ttk.LabelFrame(main_frame, text="Inventory Characteristics", padding=10)
        f_inv.pack(fill="x", pady=(0, 10))

        # Row 0: Name, Location
        ttk.Label(f_inv, text="Name:").grid(row=0, column=0, sticky="w", pady=4)
        e_name = ttk.Entry(f_inv, textvariable=self.v_name, width=28)
        e_name.grid(row=0, column=1, sticky="w", padx=(6, 16), pady=4)

        ttk.Label(f_inv, text="Location:").grid(row=0, column=2, sticky="w", pady=4)
        try:
            locs = get_locations(db_path=self.db_path)
        except Exception:
            locs = ["freezer", "fridge", "pantry"]
        cb_loc = ttk.Combobox(f_inv, textvariable=self.v_location, values=locs, width=20)
        cb_loc.grid(row=0, column=3, sticky="w", padx=(6, 0), pady=4)

        # Row 1: Expiration Date, Unit Weight
        ttk.Label(f_inv, text="Expiration (YYYY-MM-DD):").grid(row=1, column=0, sticky="w", pady=4)
        e_exp = ttk.Entry(f_inv, textvariable=self.v_expiration, width=28)
        e_exp.grid(row=1, column=1, sticky="w", padx=(6, 16), pady=4)

        ttk.Label(f_inv, text="Package Unit Weight:").grid(row=1, column=2, sticky="w", pady=4)
        e_uw = ttk.Entry(f_inv, textvariable=self.v_unit_weight, width=20)
        e_uw.grid(row=1, column=3, sticky="w", padx=(6, 0), pady=4)

        # Row 2: Intact Items, Open Remainder
        ttk.Label(f_inv, text="Intact Packages:").grid(row=2, column=0, sticky="w", pady=4)
        e_intact = ttk.Entry(f_inv, textvariable=self.v_intact, width=28)
        e_intact.grid(row=2, column=1, sticky="w", padx=(6, 16), pady=4)

        ttk.Label(f_inv, text="Open Remainder:").grid(row=2, column=2, sticky="w", pady=4)
        e_rem = ttk.Entry(f_inv, textvariable=self.v_remainder, width=20)
        e_rem.grid(row=2, column=3, sticky="w", padx=(6, 0), pady=4)

        # Section 2: Product & Macronutrients
        f_macro = ttk.LabelFrame(main_frame, text="Product & Macronutrients (per 100g / package unit)", padding=10)
        f_macro.pack(fill="x", pady=(0, 10))

        # Row 0: Unit, Kcal
        ttk.Label(f_macro, text="Unit:").grid(row=0, column=0, sticky="w", pady=4)
        cb_unit = ttk.Combobox(f_macro, textvariable=self.v_unit, values=["kg", "L", "pcs"], width=20, state="readonly")
        cb_unit.grid(row=0, column=1, sticky="w", padx=(6, 16), pady=4)

        ttk.Label(f_macro, text="Kcal (100g):").grid(row=0, column=2, sticky="w", pady=4)
        e_kcal = ttk.Entry(f_macro, textvariable=self.v_kcal, width=20)
        e_kcal.grid(row=0, column=3, sticky="w", padx=(6, 0), pady=4)

        # Row 1: Protein, Carbs
        ttk.Label(f_macro, text="Protein (g):").grid(row=1, column=0, sticky="w", pady=4)
        e_prot = ttk.Entry(f_macro, textvariable=self.v_protein, width=20)
        e_prot.grid(row=1, column=1, sticky="w", padx=(6, 16), pady=4)

        ttk.Label(f_macro, text="Carbs (g):").grid(row=1, column=2, sticky="w", pady=4)
        e_carb = ttk.Entry(f_macro, textvariable=self.v_carbs, width=20)
        e_carb.grid(row=1, column=3, sticky="w", padx=(6, 0), pady=4)

        # Row 2: Fat, Sugars
        ttk.Label(f_macro, text="Fat (g):").grid(row=2, column=0, sticky="w", pady=4)
        e_fat = ttk.Entry(f_macro, textvariable=self.v_fat, width=20)
        e_fat.grid(row=2, column=1, sticky="w", padx=(6, 16), pady=4)

        ttk.Label(f_macro, text="Sugars (g):").grid(row=2, column=2, sticky="w", pady=4)
        e_sug = ttk.Entry(f_macro, textvariable=self.v_sugars, width=20)
        e_sug.grid(row=2, column=3, sticky="w", padx=(6, 0), pady=4)

        # Row 3: Saturated Fat, Fiber
        ttk.Label(f_macro, text="Saturated Fat (g):").grid(row=3, column=0, sticky="w", pady=4)
        e_sat = ttk.Entry(f_macro, textvariable=self.v_saturated_fat, width=20)
        e_sat.grid(row=3, column=1, sticky="w", padx=(6, 16), pady=4)

        ttk.Label(f_macro, text="Fiber (g):").grid(row=3, column=2, sticky="w", pady=4)
        e_fib = ttk.Entry(f_macro, textvariable=self.v_fiber, width=20)
        e_fib.grid(row=3, column=3, sticky="w", padx=(6, 0), pady=4)

        # Row 4: Salt, Grams per piece
        ttk.Label(f_macro, text="Salt (g):").grid(row=4, column=0, sticky="w", pady=4)
        e_salt = ttk.Entry(f_macro, textvariable=self.v_salt, width=20)
        e_salt.grid(row=4, column=1, sticky="w", padx=(6, 16), pady=4)

        ttk.Label(f_macro, text="Grams / piece:").grid(row=4, column=2, sticky="w", pady=4)
        e_gpp = ttk.Entry(f_macro, textvariable=self.v_grams_per_piece, width=20)
        e_gpp.grid(row=4, column=3, sticky="w", padx=(6, 0), pady=4)

        # Row 5: Density kg/L
        ttk.Label(f_macro, text="Density (kg/L):").grid(row=5, column=0, sticky="w", pady=4)
        e_den = ttk.Entry(f_macro, textvariable=self.v_density, width=20)
        e_den.grid(row=5, column=1, sticky="w", padx=(6, 16), pady=4)

        # Action Buttons
        btn_bar = ttk.Frame(main_frame, padding=(0, 6, 0, 0))
        btn_bar.pack(fill="x")
        ttk.Button(btn_bar, text="Save Changes", command=self._on_save).pack(side="right")
        ttk.Button(btn_bar, text="Cancel", command=self._on_cancel).pack(side="right", padx=(0, 8))

    def _validate(self) -> tuple[dict, dict]:
        errors = []
        name = self.v_name.get().strip()
        if not name:
            errors.append("Name cannot be empty.")

        location = self.v_location.get().strip()
        if not location:
            errors.append("Location cannot be empty.")

        exp = self.v_expiration.get().strip()
        if exp:
            from datetime import date as _dt
            try:
                _dt.fromisoformat(exp)
                if not re.match(r"^\d{4}-[0-1]\d-[0-3]\d$", exp):
                    raise ValueError
            except Exception:
                errors.append("Expiration Date must be in YYYY-MM-DD format.")
        else:
            exp = None

        try:
            unit_weight = float(self.v_unit_weight.get().strip())
            if not math.isfinite(unit_weight):
                errors.append("Package Unit Weight must be a valid positive number.")
            elif unit_weight <= 0:
                errors.append("Package Unit Weight must be greater than 0.")
        except Exception:
            errors.append("Package Unit Weight must be a valid positive number.")
            unit_weight = 1.0

        try:
            intact = int(self.v_intact.get().strip())
            if intact < 0:
                errors.append("Intact Packages cannot be negative.")
        except Exception:
            errors.append("Intact Packages must be a valid non-negative integer.")
            intact = 0

        try:
            remainder = float(self.v_remainder.get().strip())
            if not math.isfinite(remainder):
                errors.append("Open Remainder must be a valid non-negative number.")
            elif remainder < 0:
                errors.append("Open Remainder cannot be negative.")
        except Exception:
            errors.append("Open Remainder must be a valid non-negative number.")
            remainder = 0.0

        if intact == 0 and remainder <= 0:
            errors.append("Total quantity cannot be 0. To delete this item, use the delete button.")

        unit = self.v_unit.get().strip()
        if unit not in ("kg", "L", "pcs"):
            errors.append("Unit must be 'kg', 'L', or 'pcs'.")

        def _parse_num(val_str, field_name, required=False, allow_zero=True):
            s = val_str.strip()
            if not s:
                if required:
                    errors.append(f"{field_name} is required.")
                return None
            try:
                n = float(s)
                if not math.isfinite(n):
                    errors.append(f"{field_name} must be a valid number.")
                    return None
                if allow_zero and n < 0:
                    errors.append(f"{field_name} must be >= 0.")
                elif not allow_zero and n <= 0:
                    errors.append(f"{field_name} must be > 0.")
                return n
            except Exception:
                errors.append(f"{field_name} must be a valid number.")
                return None

        kcal = _parse_num(self.v_kcal.get(), "Kcal", required=True)
        protein = _parse_num(self.v_protein.get(), "Protein", required=True)
        carbs = _parse_num(self.v_carbs.get(), "Carbs", required=True)
        fat = _parse_num(self.v_fat.get(), "Fat", required=True)
        sugars = _parse_num(self.v_sugars.get(), "Sugars", required=False)
        sat_fat = _parse_num(self.v_saturated_fat.get(), "Saturated Fat", required=False)
        fiber = _parse_num(self.v_fiber.get(), "Fiber", required=False)
        salt = _parse_num(self.v_salt.get(), "Salt", required=False)

        gpp = _parse_num(self.v_grams_per_piece.get(), "Grams per piece", required=False, allow_zero=False)
        den = _parse_num(self.v_density.get(), "Density kg/L", required=False, allow_zero=False)

        if errors:
            raise ValueError("\n".join(errors))

        item_fields = {
            "Name": name,
            "Location": location,
            "Expiration_Date": exp,
            "Unit_Weight": unit_weight,
            "Intact_Items": intact,
            "Open_Remainder": remainder,
            "Unit": unit,
            "Grams_per_piece": gpp,
            "Density_kg_per_L": den,
        }
        macro_fields = {
            "Kcal_100g": kcal,
            "Protein_100g": protein,
            "Carbs_100g": carbs,
            "Fat_100g": fat,
            "Sugars_100g": sugars,
            "Saturated_fat_100g": sat_fat,
            "Fiber_100g": fiber,
            "Salt_100g": salt,
        }
        return item_fields, macro_fields

    def _on_save(self) -> bool:
        try:
            item_fields, macro_fields = self._validate()
        except ValueError as e:
            self.last_error = str(e)
            messagebox.showerror("Validation Error", str(e), parent=self)
            return False

        # Detect macro changes
        d = self.item_data
        macros_changed = False
        for k, v in macro_fields.items():
            orig_v = d.get(k)
            if v is not None and (orig_v is None or abs(orig_v - v) > 1e-5):
                macros_changed = True
                break
            elif v is None and orig_v is not None:
                macros_changed = True
                break

        if item_fields["Unit"] != d.get("Unit"):
            macros_changed = True

        propagate = False
        if macros_changed:
            product_id = d["ID"]
            is_linked = is_product_macro_linked(product_id, db_path=self.db_path)
            if is_linked:
                prompt_dlg = MacroPromptDialog(self, product_name=d.get("Name", f"Product #{product_id}"), product_id=product_id)
                if prompt_dlg.result is None:
                    # User cancelled the macro prompt
                    return False
                propagate = (prompt_dlg.result == "propagate")
            else:
                propagate = True

        try:
            res = update_inventory_item(
                row_id=self.row_id,
                item_fields=item_fields,
                macro_fields=macro_fields,
                propagate=propagate,
                db_path=self.db_path,
            )
            self.result = res
            self.destroy()
            return True
        except Exception as e:
            self.last_error = str(e)
            messagebox.showerror("Save Error", str(e), parent=self)
            return False

    def save(self) -> bool:
        """Alias for _on_save to support automated testing."""
        return self._on_save()

    def _on_cancel(self):
        self.result = None
        self.destroy()

    def cancel(self):
        """Alias for _on_cancel to support automated testing."""
        self._on_cancel()


class ProfileSetupDialog(tk.Toplevel):
    """
    Modal dialog allowing initial configuration of user profile from the empty-state overlay.
    """
    def __init__(self, parent, db_path: Optional[str] = None, wait: bool = True):
        super().__init__(parent)
        self.title("Set Up Profile")
        if parent:
            self.transient(parent)
        self.resizable(False, False)
        self.result = None
        self.db_path = db_path

        container = ttk.Frame(self, padding=16)
        container.pack(fill="both", expand=True)

        ttk.Label(
            container,
            text="Enter your details to get started:",
            font=("TkDefaultFont", 11, "bold")
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))

        # Static Fields
        ttk.Label(container, text="Sex:").grid(row=1, column=0, sticky="w", pady=4)
        self.v_sex = tk.StringVar(value="M")
        cb_sex = ttk.Combobox(container, textvariable=self.v_sex, values=["M", "F"], state="readonly", width=14)
        cb_sex.grid(row=1, column=1, sticky="w", padx=(10, 0), pady=4)

        ttk.Label(container, text="Height (cm):").grid(row=2, column=0, sticky="w", pady=4)
        self.v_height = tk.StringVar(value="175")
        ttk.Entry(container, textvariable=self.v_height, width=16).grid(row=2, column=1, sticky="w", padx=(10, 0), pady=4)

        # Dynamic Fields
        ttk.Label(container, text="Current weight (kg):").grid(row=3, column=0, sticky="w", pady=4)
        self.v_weight = tk.StringVar(value="70")
        ttk.Entry(container, textvariable=self.v_weight, width=16).grid(row=3, column=1, sticky="w", padx=(10, 0), pady=4)

        ttk.Label(container, text="Age (years):").grid(row=4, column=0, sticky="w", pady=4)
        self.v_age = tk.StringVar(value="25")
        ttk.Entry(container, textvariable=self.v_age, width=16).grid(row=4, column=1, sticky="w", padx=(10, 0), pady=4)

        ttk.Label(container, text="Activity level:").grid(row=5, column=0, sticky="w", pady=4)
        self.v_activity = tk.StringVar(value="moderate")
        from meal_planner.engine.constants import PAL
        cb_act = ttk.Combobox(container, textvariable=self.v_activity, values=sorted(PAL.keys()), state="readonly", width=14)
        cb_act.grid(row=5, column=1, sticky="w", padx=(10, 0), pady=4)

        ttk.Label(container, text="Goal:").grid(row=6, column=0, sticky="w", pady=4)
        self.v_goal = tk.StringVar(value="maintenance")
        cb_goal = ttk.Combobox(container, textvariable=self.v_goal, values=["maintenance", "weight_loss", "weight_gain"], state="readonly", width=14)
        cb_goal.grid(row=6, column=1, sticky="w", padx=(10, 0), pady=4)

        ttk.Label(container, text="Target weight (kg):").grid(row=7, column=0, sticky="w", pady=4)
        self.v_target_weight = tk.StringVar(value="70")
        ttk.Entry(container, textvariable=self.v_target_weight, width=16).grid(row=7, column=1, sticky="w", padx=(10, 0), pady=4)

        ttk.Label(container, text="Weekly rate (kg/week):").grid(row=8, column=0, sticky="w", pady=4)
        self.v_weekly_rate = tk.StringVar(value="0.5")
        ttk.Entry(container, textvariable=self.v_weekly_rate, width=16).grid(row=8, column=1, sticky="w", padx=(10, 0), pady=4)

        btn_row = ttk.Frame(container)
        btn_row.grid(row=9, column=0, columnspan=2, sticky="e", pady=(16, 0))
        ttk.Button(btn_row, text="Cancel", command=self.cancel).pack(side="right", padx=(6, 0))
        ttk.Button(btn_row, text="Save Profile", command=self.save).pack(side="right")

        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.bind("<Escape>", lambda e: self.cancel())

        _center_and_lift(self, parent)

        try:
            self.grab_set()
        except Exception:
            pass

        if wait:
            self.wait_window(self)

    def save(self) -> bool:
        sex = self.v_sex.get().strip()
        if sex not in ("M", "F"):
            messagebox.showerror("Error", "Please select 'M' or 'F' for sex.", parent=self)
            return False
        try:
            height = float(self.v_height.get().strip())
            weight = float(self.v_weight.get().strip())
            age = int(self.v_age.get().strip())
            if height <= 0 or weight <= 0 or age <= 0:
                raise ValueError()
        except ValueError:
            messagebox.showerror("Error", "Height, weight, and age must be positive numerical values.", parent=self)
            return False

        activity = self.v_activity.get().strip()
        goal = self.v_goal.get().strip()

        target_w = weight
        if self.v_target_weight.get().strip():
            try:
                target_w = float(self.v_target_weight.get().strip())
            except ValueError:
                target_w = weight

        weekly_rate = 0.5
        if self.v_weekly_rate.get().strip():
            try:
                weekly_rate = float(self.v_weekly_rate.get().strip())
            except ValueError:
                weekly_rate = 0.5

        data = {
            "Weight_kg": weight,
            "Height_cm": height,
            "Age_years": age,
            "Sex": sex,
            "Activity_Level": activity,
            "Goal": goal,
            "Target_weight_kg": target_w,
            "Weekly_rate_kg": weekly_rate if goal != "maintenance" else None,
            "Kcal_tolerance_pct": 5.0,
            "Open_extra_pct": 5.0,
            "No_repeat_days": 3,
            "Default_servings": 1,
            "Use_manual_maintenance": False,
            "Use_manual_kcal": False,
            "Maintenance_manual": None,
            "Kcal_manual": None,
        }

        try:
            from meal_planner.engine.profile import update_profile
            update_profile(conn=None, values=data, db_path=self.db_path)
            self.result = data
            self.destroy()
            return True
        except Exception as e:
            messagebox.showerror("Save Error", str(e), parent=self)
            return False

    def cancel(self):
        self.result = None
        self.destroy()

    # Aliases for automated testing compatibility
    on_save = save
    _on_save = save
    on_cancel = cancel
    _on_cancel = cancel


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
        if canon:
            self._canonical[canon] = value
        else:
            self.add_canonical(key, value)

    def __contains__(self, key: str) -> bool:
        return self._resolve(key) is not None

    def get(self, key: str, default=None):
        canon = self._resolve(key)
        if canon:
            return self._canonical[canon]
        return default

    def items(self):
        return self._canonical.items()

    def keys(self):
        return self._canonical.keys()

    def values(self):
        return self._canonical.values()

    def __iter__(self):
        return iter(self._canonical)

    def __len__(self):
        return len(self._canonical)


class QuickProductDialog(tk.Toplevel):
    """
    Dialog to register an unknown product on the fly.
    Prompts for Name, Unit, and Macros (Kcal, Protein, Carbs, Fat, Fiber).
    Supports wait: bool = True and safe grab_set().
    """
    def __init__(self, master=None, name: str = "", suggested_unit: str = "g", wait: bool = True, db_path: Optional[str] = None):
        super().__init__(master)
        self.title("New Product")
        if master:
            self.transient(master)
        self.resizable(False, False)
        self.result = None
        self.db_path = db_path

        container = ttk.Frame(self, padding=14)
        container.pack(fill="both", expand=True)

        ttk.Label(container, text=f"Product not found: '{name}'\nEnter details to add it to catalog:",
                  font=("TkDefaultFont", 10, "bold"), wraplength=340).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))

        ttk.Label(container, text="Name:").grid(row=1, column=0, sticky="w", pady=3)
        self.v_name = tk.StringVar(value=name)
        self.e_name = ttk.Entry(container, textvariable=self.v_name, width=24)
        self.e_name.grid(row=1, column=1, sticky="w", padx=(8, 0), pady=3)

        ttk.Label(container, text="Unit of measure:").grid(row=2, column=0, sticky="w", pady=3)
        self.v_unit = tk.StringVar(value=suggested_unit if suggested_unit in ("g", "kg", "ml", "L", "pcs", "pz") else "g")
        self.cb_unit = ttk.Combobox(container, textvariable=self.v_unit, values=["g", "kg", "ml", "L", "pcs"], state="readonly", width=10)
        self.cb_unit.grid(row=2, column=1, sticky="w", padx=(8, 0), pady=3)

        ttk.Label(container, text="Values per 100g:", font=("TkDefaultFont", 9, "bold")).grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 4))

        fields = [
            ("kcal", "Kcal *:", "v_kcal", "0"),
            ("prot", "Protein (g) *:", "v_prot", "0"),
            ("carb", "Carbs (g) *:", "v_carb", "0"),
            ("fat", "Fat (g) *:", "v_fat", "0"),
            ("fiber", "Fiber (g):", "v_fiber", ""),
        ]
        self.macro_vars = {}
        for idx, (key, label, var_name, default_val) in enumerate(fields, start=4):
            ttk.Label(container, text=label).grid(row=idx, column=0, sticky="w", pady=2)
            var = tk.StringVar(value=default_val)
            self.macro_vars[key] = var
            setattr(self, var_name, var)
            e = ttk.Entry(container, textvariable=var, width=12)
            e.grid(row=idx, column=1, sticky="w", padx=(8, 0), pady=2)
            setattr(self, f"e_{key}", e)

        btn_bar = ttk.Frame(container)
        btn_bar.grid(row=9, column=0, columnspan=2, sticky="e", pady=(12, 0))
        self.btn_cancel = ttk.Button(btn_bar, text="Cancel", command=self.cancel)
        self.btn_cancel.pack(side="right", padx=(6, 0))
        self.btn_save = ttk.Button(btn_bar, text="Add", command=self.save)
        self.btn_save.pack(side="right")

        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.bind("<Escape>", lambda e: self.cancel())

        _center_and_lift(self, master)

        try:
            self.grab_set()
        except Exception:
            pass

        if wait:
            self.wait_window(self)

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
            if kcal < 0 or prot < 0 or carb < 0 or fat < 0:
                raise ValueError()
        except (ValueError, TypeError):
            messagebox.showerror("Error", "Kcal, Protein, Carbs, and Fat must be non-negative numbers.", parent=self)
            return False

        fiber = None
        if self.v_fiber.get().strip():
            try:
                fiber_val = float(self.v_fiber.get().strip().replace(",", "."))
                if fiber_val < 0:
                    raise ValueError()
                fiber = fiber_val
            except (ValueError, TypeError):
                messagebox.showerror("Error", "Fiber must be a non-negative number.", parent=self)
                return False

        macros = {
            "Kcal_100g": kcal,
            "Protein_100g": prot,
            "Carbs_100g": carb,
            "Fat_100g": fat,
        }
        if fiber is not None:
            macros["Fiber_100g"] = fiber

        self.result = {
            "name": name,
            "unit": unit,
            "macros": macros,
        }
        self.destroy()
        return True

    def cancel(self):
        self.result = None
        self.destroy()

    on_save = save
    _on_save = save
    on_cancel = cancel
    _on_cancel = cancel


class AddRecipeDialog(tk.Toplevel):
    """
    Modal dialog allowing direct GUI creation of recipes with:
    - Recipe Name entry (v_name, e_name)
    - Meal selection supporting bilingual lookup (v_meals)
    - Servings entry (v_servings, default "1", scales ingredient quantities when saving)
    - Notes field (v_notes, t_notes, and @property def notes(self))
    - Ingredients builder:
        - Autocomplete/combobox (cb_ingredient)
        - Quantity entry (e_quantity)
        - Unit combobox (cb_unit)
        - 'To Taste' ('Q.b.') toggle (chk_to_taste)
        - Add / Remove ingredient buttons
        - Unknown ingredient creation on the fly via QuickProductDialog
        - Visual list/treeview (t_ingredients) with live macronutrient calculations
        - Macro summary label (lbl_macro_summary)
    - Save and Cancel buttons with test aliases
    - Non-blocking headless execution support (wait: bool = True)
    """
    def __init__(
        self,
        master=None,
        db_path: Optional[str] = None,
        wait: bool = True,
        on_save_callback=None,
        on_unknown_product_callback=None,
    ):
        super().__init__(master)
        self.title("New Recipe")
        if master:
            self.transient(master)
        self.resizable(True, True)
        self.minsize(680, 620)
        self.db_path = db_path
        self._wait = wait
        self.on_save_callback = on_save_callback
        self.on_unknown_product_callback = on_unknown_product_callback
        self.result = None
        self.last_error = None
        self.ingredients = []
        self._focus_timer = None

        self._init_variables()
        self._init_ui()

        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.bind("<Escape>", lambda e: self._on_cancel())

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
        conn = get_connection(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def _init_variables(self):
        self.v_name = tk.StringVar(value="")
        self.v_servings = tk.StringVar(value="1")
        self.v_notes = tk.StringVar(value="")
        self.v_ingredient_name = tk.StringVar(value="")
        self.v_quantity = tk.StringVar(value="")
        self.v_unit = tk.StringVar(value="g")
        self.v_to_taste = tk.BooleanVar(value=False)

        # Consolidated database load during initialization
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
            if conn is not None:
                cur = conn.cursor()
                cur.execute("SELECT Name FROM MEALS ORDER BY Sort_Order")
                for (m_name,) in cur.fetchall():
                    meals.append((m_name, tk.BooleanVar(value=False)))
            else:
                with self._get_conn() as c:
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
            if conn is not None:
                cur = conn.cursor()
                cur.execute("SELECT Name FROM ALIASES UNION SELECT Name FROM SUBSTITUTE_GROUPS ORDER BY Name")
                return [r[0] for r in cur.fetchall()]
            else:
                with self._get_conn() as c:
                    cur = c.cursor()
                    cur.execute("SELECT Name FROM ALIASES UNION SELECT Name FROM SUBSTITUTE_GROUPS ORDER BY Name")
                    return [r[0] for r in cur.fetchall()]
        except Exception:
            return []

    def _init_ui(self):
        main_frame = ttk.Frame(self, padding=12)
        main_frame.pack(fill="both", expand=True)
        main_frame.columnconfigure(0, weight=1)

        # 1. Top Section: Name, Servings, Meals
        meta_frame = ttk.LabelFrame(main_frame, text="Recipe Details", padding=10)
        meta_frame.pack(fill="x", pady=(0, 8))
        meta_frame.columnconfigure(1, weight=1)

        ttk.Label(meta_frame, text="Recipe Name:").grid(row=0, column=0, sticky="w", pady=4)
        self.e_name = ttk.Entry(meta_frame, textvariable=self.v_name)
        self.e_name.grid(row=0, column=1, sticky="ew", padx=(8, 16), pady=4)

        ttk.Label(meta_frame, text="Servings:").grid(row=0, column=2, sticky="w", pady=4)
        self.e_servings = ttk.Entry(meta_frame, textvariable=self.v_servings, width=6)
        self.e_servings.grid(row=0, column=3, sticky="w", padx=(8, 0), pady=4)
        self.v_servings.trace_add("write", lambda *args: self._update_macro_summary())

        # Meals checkboxes
        meal_frame = ttk.Frame(meta_frame)
        meal_frame.grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 2))
        ttk.Label(meal_frame, text="Meals:").pack(side="left", padx=(0, 8))
        for m_name, var in self.v_meals.items():
            ttk.Checkbutton(meal_frame, text=m_name.capitalize(), variable=var).pack(side="left", padx=(0, 10))

        # Notes
        notes_frame = ttk.LabelFrame(main_frame, text="Preparation Notes", padding=8)
        notes_frame.pack(fill="x", pady=(0, 8))
        self.t_notes = tk.Text(notes_frame, height=3, wrap="word")
        self.t_notes.pack(fill="x", expand=True)
        self.t_notes.bind("<FocusOut>", lambda e: self.v_notes.set(self.notes))

        # 2. Ingredient Builder Section
        ing_box = ttk.LabelFrame(main_frame, text="Add Ingredient", padding=10)
        ing_box.pack(fill="x", pady=(0, 8))
        ing_box.columnconfigure(1, weight=1)

        ttk.Label(ing_box, text="Ingredient:").grid(row=0, column=0, sticky="w", pady=2)
        known = getattr(self, "_known_ingredients", None) or self._load_known_ingredients()
        self.cb_ingredient = ttk.Combobox(ing_box, textvariable=self.v_ingredient_name, values=known, width=22)
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

        ttk.Label(
            ing_box,
            text="ℹ️ 'To taste' (q.b. / quanto basta): for discretionary ingredients added to taste without fixed nutritional weight (e.g. salt, pepper, spices, herbs).",
            font=("TkDefaultFont", 8, "italic"),
        ).grid(row=1, column=0, columnspan=8, sticky="w", pady=(2, 0))

        # 3. Ingredients Visual Table
        table_frame = ttk.LabelFrame(main_frame, text="Added Ingredients (Recipe Total)", padding=8)
        table_frame.pack(fill="both", expand=True, pady=(0, 8))
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)

        cols = ("product", "quantity", "unit", "to_taste", "kcal", "prot", "carb", "fat")
        self.t_ingredients = ttk.Treeview(table_frame, columns=cols, show="headings", height=5)
        self.t_ingredients.heading("product", text="Ingredient")
        self.t_ingredients.heading("quantity", text="Quantity")
        self.t_ingredients.heading("unit", text="Unit")
        self.t_ingredients.heading("to_taste", text="To taste")
        self.t_ingredients.heading("kcal", text="Kcal")
        self.t_ingredients.heading("prot", text="Prot (g)")
        self.t_ingredients.heading("carb", text="Carbs (g)")
        self.t_ingredients.heading("fat", text="Fat (g)")

        self.t_ingredients.column("product", width=180, anchor="w")
        self.t_ingredients.column("quantity", width=70, anchor="e")
        self.t_ingredients.column("unit", width=50, anchor="center")
        self.t_ingredients.column("to_taste", width=50, anchor="center")
        self.t_ingredients.column("kcal", width=60, anchor="e")
        self.t_ingredients.column("prot", width=60, anchor="e")
        self.t_ingredients.column("carb", width=60, anchor="e")
        self.t_ingredients.column("fat", width=60, anchor="e")

        sb = ttk.Scrollbar(table_frame, orient="vertical", command=self.t_ingredients.yview)
        self.t_ingredients.configure(yscrollcommand=sb.set)
        self.t_ingredients.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")

        # Table buttons & macro summary
        summary_row = ttk.Frame(table_frame)
        summary_row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        summary_row.columnconfigure(0, weight=1)

        self.lbl_macro_summary = ttk.Label(
            summary_row,
            text="Total per serving (1 of 1): 0.0 kcal | Prot: 0.0g | Carbs: 0.0g | Fat: 0.0g",
            font=("TkDefaultFont", 9, "bold"),
        )
        self.lbl_macro_summary.grid(row=0, column=0, sticky="w")

        self.btn_remove_ingredient = ttk.Button(
            summary_row,
            text="Remove Ingredient",
            command=self.remove_ingredient,
        )
        self.btn_remove_ingredient.grid(row=0, column=1, sticky="e")

        # 4. Action Buttons Bar
        action_bar = ttk.Frame(main_frame)
        action_bar.pack(fill="x", pady=(6, 0))
        self.btn_cancel = ttk.Button(action_bar, text="Cancel", command=self._on_cancel)
        self.btn_cancel.pack(side="right", padx=(8, 0))
        self.btn_save = ttk.Button(action_bar, text="Save Recipe", command=self._on_save)
        self.btn_save.pack(side="right")

        # Keyboard ergonomics & quick navigation
        self.bind("<Control-Return>", lambda e: self._on_save())
        self.bind("<Control-s>", lambda e: self._on_save())
        self.bind("<Control-S>", lambda e: self._on_save())
        self.e_name.bind("<Return>", lambda e: self.cb_ingredient.focus_set())
        self.e_quantity.bind("<Return>", lambda e: self.add_ingredient())
        self.cb_ingredient.bind("<Return>", lambda e: self.e_quantity.focus_set() if not self.v_quantity.get().strip() else self.add_ingredient())
        self.t_ingredients.bind("<Delete>", lambda e: self.remove_ingredient())
        self.t_ingredients.bind("<BackSpace>", lambda e: self.remove_ingredient())

        # Auto-focus on recipe name
        self._focus_timer = self.after(100, self._set_initial_focus)

    def _set_initial_focus(self):
        try:
            self.e_name.focus_set()
        except Exception:
            pass

    @property
    def notes(self) -> str:
        if hasattr(self, "t_notes"):
            try:
                txt = self.t_notes.get("1.0", "end-1c").strip()
                if txt:
                    return txt
            except Exception:
                pass
        return self.v_notes.get().strip()

    @notes.setter
    def notes(self, value: str):
        val = str(value or "").strip()
        self.v_notes.set(val)
        if hasattr(self, "t_notes"):
            try:
                self.t_notes.delete("1.0", "end")
                if val:
                    self.t_notes.insert("1.0", val)
            except Exception:
                pass

    def _calculate_ingredient_macros(self, name: str, quantity: float, unit: str, conn=None) -> dict:
        macros = {"kcal": 0.0, "prot": 0.0, "carb": 0.0, "fat": 0.0}
        def _calc_with_conn(c):
            cur = c.cursor()
            cur.execute("""
                SELECT p.Unit, p.Kcal_100g, p.Protein_100g, p.Carbs_100g, p.Fat_100g,
                       p.Grams_per_piece, p.Density_kg_per_L
                FROM ALIASES a
                JOIN PRODUCTS p ON a.ID = p.ID
                WHERE a.Name = ? COLLATE NOCASE
            """, (name,))
            row = cur.fetchone()
            if not row:
                return macros
            p_unit, kcal_100, prot_100, carb_100, fat_100, g_pc, dens = row
            g = None
            if unit == "g":
                g = quantity
            elif unit == "kg":
                g = quantity * 1000.0
            elif unit == "pcs":
                g = quantity * g_pc if g_pc else None
            elif unit in ("ml", "L", "tbsp", "tsp"):
                d = dens or 1.0
                factors = {"ml": 1.0, "L": 1000.0, "tbsp": 15.0, "tsp": 5.0}
                g = quantity * factors.get(unit, 1.0) * d
            if g is not None:
                macros["kcal"] = round(g / 100.0 * kcal_100, 2)
                macros["prot"] = round(g / 100.0 * prot_100, 2)
                macros["carb"] = round(g / 100.0 * carb_100, 2)
                macros["fat"] = round(g / 100.0 * fat_100, 2)
            return macros

        try:
            if conn is not None:
                return _calc_with_conn(conn)
            else:
                with self._get_conn() as c:
                    return _calc_with_conn(c)
        except Exception:
            return macros

    def add_ingredient(self) -> bool:
        name = (self.v_ingredient_name.get() or self.cb_ingredient.get()).strip()
        if not name:
            messagebox.showerror("Error", "Please select or enter an ingredient.", parent=self)
            return False

        unit = (self.v_unit.get() or self.cb_unit.get()).strip()
        if unit not in ("g", "kg", "ml", "L", "pcs", "tbsp", "tsp"):
            unit = "g"

        to_taste = 1 if self.v_to_taste.get() else 0
        raw_qty = (self.v_quantity.get() or self.e_quantity.get()).strip()

        if not raw_qty and to_taste == 1:
            qty = 1.0
        else:
            try:
                qty = float(raw_qty.replace(",", "."))
                if qty <= 0:
                    raise ValueError()
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
            dlg_result = None
            if self.on_unknown_product_callback:
                dlg_result = self.on_unknown_product_callback(name, unit)
            else:
                dlg = QuickProductDialog(self, name=name, suggested_unit=unit, wait=self._wait, db_path=self.db_path)
                dlg_result = dlg.result

            if not dlg_result:
                return False

            with self._get_conn() as conn:
                register_product_on_the_fly(
                    conn,
                    name=dlg_result["name"],
                    unit=dlg_result["unit"],
                    macros=dlg_result["macros"],
                )
                name = dlg_result["name"]
                self.cb_ingredient["values"] = self._load_known_ingredients(conn=conn)
                macros = self._calculate_ingredient_macros(name, qty, unit, conn=conn)

        if macros is None:
            macros = self._calculate_ingredient_macros(name, qty, unit)
        item = {
            "product": name,
            "quantity": qty,
            "unit": unit,
            "to_taste": to_taste,
            **macros,
        }
        self.ingredients.append(item)
        self._refresh_ingredients_table()
        self._update_macro_summary()

        self.v_ingredient_name.set("")
        self.cb_ingredient.set("")
        self.v_quantity.set("")
        self.e_quantity.delete(0, "end")
        self.v_to_taste.set(False)
        try:
            self.cb_ingredient.focus_set()
        except Exception:
            pass
        return True

    def remove_ingredient(self, index: Optional[int] = None) -> bool:
        if index is not None and 0 <= index < len(self.ingredients):
            del self.ingredients[index]
            self._refresh_ingredients_table()
            self._update_macro_summary()
            return True
        selected = self.t_ingredients.selection()
        if selected:
            idx = self.t_ingredients.index(selected[0])
            if 0 <= idx < len(self.ingredients):
                del self.ingredients[idx]
                self._refresh_ingredients_table()
                self._update_macro_summary()
                return True
        elif self.ingredients:
            self.ingredients.pop()
            self._refresh_ingredients_table()
            self._update_macro_summary()
            return True
        messagebox.showwarning("Warning", "Please select an ingredient to remove.", parent=self)
        return False

    def _refresh_ingredients_table(self):
        for item in self.t_ingredients.get_children():
            self.t_ingredients.delete(item)
        for ing in self.ingredients:
            self.t_ingredients.insert(
                "",
                "end",
                values=(
                    ing["product"],
                    f"{ing['quantity']:g}",
                    ing["unit"],
                    "Yes" if ing["to_taste"] else "No",
                    f"{ing.get('kcal', 0.0):.1f}",
                    f"{ing.get('prot', 0.0):.1f}",
                    f"{ing.get('carb', 0.0):.1f}",
                    f"{ing.get('fat', 0.0):.1f}",
                ),
            )

    def _update_macro_summary(self):
        if not hasattr(self, "lbl_macro_summary"):
            return
        tot_kcal = sum(ing.get("kcal", 0.0) for ing in self.ingredients)
        tot_prot = sum(ing.get("prot", 0.0) for ing in self.ingredients)
        tot_carb = sum(ing.get("carb", 0.0) for ing in self.ingredients)
        tot_fat = sum(ing.get("fat", 0.0) for ing in self.ingredients)
        try:
            servings = float(self.v_servings.get().strip().replace(",", ".") or "1")
            if not math.isfinite(servings) or servings <= 0:
                servings = 1.0
        except (ValueError, TypeError):
            servings = 1.0

        per_kcal = tot_kcal / servings
        per_prot = tot_prot / servings
        per_carb = tot_carb / servings
        per_fat = tot_fat / servings
        self.lbl_macro_summary.configure(
            text=f"Total per serving (1 of {servings:g}): {per_kcal:.1f} kcal | Prot: {per_prot:.1f}g | Carbs: {per_carb:.1f}g | Fat: {per_fat:.1f}g"
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
            servings = float(self.v_servings.get().strip().replace(",", ".") or "1")
            if not math.isfinite(servings) or servings <= 0:
                raise ValueError()
        except (ValueError, TypeError):
            messagebox.showerror("Error", "Number of servings must be a positive number (> 0).", parent=self)
            return False

        scaled_ingredients = []
        for ing in self.ingredients:
            scaled_qty = ing["quantity"] / servings
            scaled_ingredients.append({
                "product": ing["product"],
                "quantity": round(scaled_qty, 6),
                "unit": ing["unit"],
                "to_taste": ing["to_taste"],
            })

        notes_val = self.notes or None

        try:
            with self._get_conn() as conn:
                from meal_planner.recipes import create_recipe
                outcome = create_recipe(conn, name, meals, scaled_ingredients, notes=notes_val)
            self.result = {
                "Recipe_ID": outcome["Recipe_ID"],
                "Name": name,
                "warnings": outcome.get("warnings", []),
            }
            if self.on_save_callback:
                try:
                    self.on_save_callback(self.result)
                except Exception:
                    pass
            self.destroy()
            return True
        except Exception as e:
            self.last_error = str(e)
            messagebox.showerror("Save Error", str(e), parent=self)
            return False

    def destroy(self):
        if hasattr(self, "_focus_timer") and self._focus_timer:
            try:
                self.after_cancel(self._focus_timer)
            except Exception:
                pass
            self._focus_timer = None
        super().destroy()

    def _on_cancel(self):
        if hasattr(self, "_focus_timer") and self._focus_timer:
            try:
                self.after_cancel(self._focus_timer)
            except Exception:
                pass
            self._focus_timer = None
        self.result = None
        self.destroy()

    save = _on_save
    on_save = _on_save
    cancel = _on_cancel
    on_cancel = _on_cancel


class RecipeDetailsDialog(tk.Toplevel):
    """
    Modal dialog displaying detailed view of an existing recipe:
    - Recipe Name & Associated Meals
    - Preparation Notes
    - Full ingredients list with quantities, units, and Q.b. flags
    - Live macronutrient summary from RECIPE_MACROS_VIEW
    """
    def __init__(self, master=None, recipe_name: str = "", db_path: Optional[str] = None, wait: bool = True):
        super().__init__(master)
        self.title(f"Recipe Details — {recipe_name}")
        if master:
            self.transient(master)
        self.resizable(True, True)
        self.minsize(640, 480)
        self.db_path = db_path
        self._wait = wait
        self.recipe_name = str(recipe_name).strip()

        self._init_ui()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Escape>", lambda e: self.destroy())

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
        conn = get_connection(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def _init_ui(self):
        main_frame = ttk.Frame(self, padding=16)
        main_frame.pack(fill="both", expand=True)
        main_frame.columnconfigure(0, weight=1)
        main_frame.rowconfigure(2, weight=1)

        # 1. Header Frame
        header = ttk.Frame(main_frame)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(header, text=self.recipe_name, font=("TkDefaultFont", 13, "bold")).pack(anchor="w")

        # Load recipe details from DB
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
                    meals = [m[0].capitalize() for m in cur.execute(
                        "SELECT Meal FROM RECIPE_MEALS WHERE Recipe_ID = ? ORDER BY Meal", (rid,)).fetchall()]
                    m_row = cur.execute(
                        "SELECT Kcal, Protein, Carbs, Fat, Fiber FROM RECIPE_MACROS_VIEW WHERE Recipe_ID = ?", (rid,)).fetchone()
                    if m_row:
                        macros = m_row
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

        # Meta tags / meals info
        meals_text = f"Associated meals: {', '.join(meals) if meals else 'All meals'}"
        ttk.Label(header, text=meals_text, font=("TkDefaultFont", 9)).pack(anchor="w", pady=(2, 0))

        # Notes section if present
        if notes:
            n_frame = ttk.LabelFrame(main_frame, text="Preparation Notes", padding=8)
            n_frame.grid(row=1, column=0, sticky="ew", pady=(0, 10))
            ttk.Label(n_frame, text=notes, wraplength=580, justify="left").pack(anchor="w")

        # 2. Ingredients table
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
            qb_str = "Yes" if to_taste else "No"
            tv.insert("", "end", values=(ing_name, f"{qty:g}", unit, qb_str))

        ttk.Label(
            t_frame,
            text="ℹ️ 'To taste' (q.b. / quanto basta): ingredients added as needed to taste without fixed nutritional weight (e.g. salt, pepper, spices).",
            font=("TkDefaultFont", 8, "italic"),
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        # 3. Macro summary bar
        kcal, prot, carb, fat, fiber = macros
        summary_text = f"Per-serving values: {kcal:.0f} kcal  |  Prot: {prot:.1f}g  |  Carbs: {carb:.1f}g  |  Fat: {fat:.1f}g  |  Fiber: {fiber:.1f}g"
        summary_frame = ttk.Frame(main_frame)
        summary_frame.grid(row=3, column=0, sticky="ew", pady=(0, 12))
        ttk.Label(summary_frame, text=summary_text, font=("TkDefaultFont", 9, "bold")).pack(side="left")

        # 4. Close button
        btn_bar = ttk.Frame(main_frame)
        btn_bar.grid(row=4, column=0, sticky="ew")
        ttk.Button(btn_bar, text="Close", command=self.destroy).pack(side="right")


class RecipeGeneratorDialog(tk.Toplevel):
    """
    Interactive modal dialog for the Hybrid Recipe Generation Pipeline:
    - Slot Architecture (Culinary Archetypes)
    - Food Pairing Graph (Harmony & Hard Exclusions)
    - ILP/CSP Optimizer (Exact dosages, macro targets, and FIFO pantry priority)
    Supports headless non-blocking mode (wait=False) for automated testing.
    """

    ARCHETYPE_OPTIONS = [
        "Auto (Default)",
        "Grain / Pasta Dish",
        "Main & Side Plate",
        "One-Pot Balanced Bowl",
        "Breakfast / Snack Bowl",
    ]

    MEAL_OPTIONS = ["lunch", "dinner", "breakfast"]

    def __init__(
        self,
        master=None,
        db_path: Optional[str] = None,
        wait: bool = True,
        on_save_callback=None,
        auto_generate: bool = True,
    ):
        super().__init__(master)
        self.title("Generate Recipe (Slot + Pairing + LP Solver)")
        if master:
            self.transient(master)
        self.resizable(True, True)
        self.minsize(720, 580)
        self.db_path = db_path
        self._wait = wait
        self.on_save_callback = on_save_callback
        self._variation_index = 0
        self.current_candidate = None
        self.saved_outcome: Optional[Dict[str, Any]] = None

        # State variables
        self.v_meal = tk.StringVar(value="lunch")
        self.v_archetype = tk.StringVar(value=self.ARCHETYPE_OPTIONS[0])
        self.v_target_kcal = tk.StringVar(value=str(int(self._fetch_default_kcal("lunch"))))
        self.v_pantry_only = tk.BooleanVar(value=True)
        self.v_title = tk.StringVar(value="")
        self.v_meta = tk.StringVar(value="Select parameters and click Generate.")
        self.v_macros = tk.StringVar(value="Per-serving macros: -")

        self._init_ui()
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.bind("<Escape>", lambda e: self.cancel())
        self.bind("<Control-Return>", lambda e: self.save())
        self.bind("<Control-s>", lambda e: self.save())
        self.bind("<Control-S>", lambda e: self.save())

        if auto_generate:
            self.generate_proposal(show_errors=False)

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
        conn = get_connection(self.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def _fetch_default_kcal(self, meal_type: str) -> float:
        from meal_planner.generator.pipeline import get_default_target_kcal
        try:
            with self._get_conn() as conn:
                return get_default_target_kcal(conn, meal_type)
        except Exception:
            return 700.0

    def _on_meal_changed(self, _event=None):
        meal = self.v_meal.get().strip().lower()
        default_kcal = self._fetch_default_kcal(meal)
        self.v_target_kcal.set(str(int(default_kcal)))
        self._variation_index = 0

    def _init_ui(self):
        main_frame = ttk.Frame(self, padding=14)
        main_frame.pack(fill="both", expand=True)
        main_frame.columnconfigure(0, weight=1)
        main_frame.rowconfigure(2, weight=1)

        # 1. Top Parameters Frame
        params_frame = ttk.LabelFrame(main_frame, text="Generation Parameters", padding=10)
        params_frame.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        for col in range(5):
            params_frame.columnconfigure(col, weight=1 if col in (1, 3) else 0)

        ttk.Label(params_frame, text="Meal:").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=4)
        self.cb_meal = ttk.Combobox(
            params_frame,
            textvariable=self.v_meal,
            values=self.MEAL_OPTIONS,
            state="readonly",
            width=14,
        )
        self.cb_meal.grid(row=0, column=1, sticky="ew", padx=(0, 12), pady=4)
        self.cb_meal.bind("<<ComboboxSelected>>", self._on_meal_changed)

        ttk.Label(params_frame, text="Archetype:").grid(row=0, column=2, sticky="w", padx=(0, 6), pady=4)
        self.cb_archetype = ttk.Combobox(
            params_frame,
            textvariable=self.v_archetype,
            values=self.ARCHETYPE_OPTIONS,
            state="readonly",
            width=24,
        )
        self.cb_archetype.grid(row=0, column=3, sticky="ew", padx=(0, 12), pady=4)
        self.cb_archetype.bind("<<ComboboxSelected>>", lambda e: setattr(self, "_variation_index", 0))

        ttk.Label(params_frame, text="Target kcal:").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=4)
        self.e_target_kcal = ttk.Entry(params_frame, textvariable=self.v_target_kcal, width=10)
        self.e_target_kcal.grid(row=1, column=1, sticky="w", padx=(0, 12), pady=4)

        self.chk_pantry_only = ttk.Checkbutton(
            params_frame,
            text="Pantry Only (prioritize opened & near-expiry FIFO)",
            variable=self.v_pantry_only,
        )
        self.chk_pantry_only.grid(row=1, column=2, columnspan=2, sticky="w", pady=4)

        self.btn_generate = ttk.Button(
            params_frame,
            text="✨ Generate",
            command=self.generate_proposal,
        )
        self.btn_generate.grid(row=0, column=4, rowspan=2, sticky="ns", padx=(8, 0), pady=2)

        # 2. Recipe Title & Metadata Frame
        title_frame = ttk.LabelFrame(main_frame, text="Proposed Recipe", padding=10)
        title_frame.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        title_frame.columnconfigure(1, weight=1)

        ttk.Label(title_frame, text="Recipe Name:").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.e_title = ttk.Entry(title_frame, textvariable=self.v_title, font=("TkDefaultFont", 10, "bold"))
        self.e_title.grid(row=0, column=1, sticky="ew")

        ttk.Label(
            title_frame,
            textvariable=self.v_meta,
            font=("TkDefaultFont", 8),
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

        # 3. Ingredients & Preparation Notes Split
        center_frame = ttk.Frame(main_frame)
        center_frame.grid(row=2, column=0, sticky="nsew", pady=(0, 10))
        center_frame.columnconfigure(0, weight=1)
        center_frame.rowconfigure(0, weight=3)
        center_frame.rowconfigure(1, weight=2)

        ing_frame = ttk.LabelFrame(center_frame, text="Optimized Ingredients (1 serving)", padding=8)
        ing_frame.grid(row=0, column=0, sticky="nsew", pady=(0, 8))
        ing_frame.columnconfigure(0, weight=1)
        ing_frame.rowconfigure(0, weight=1)

        cols = ("name", "role", "quantity", "unit", "to_taste", "status")
        self.t_ingredients = ttk.Treeview(ing_frame, columns=cols, show="headings", height=6)
        self.t_ingredients.heading("name", text="Ingredient")
        self.t_ingredients.heading("role", text="Slot / Role")
        self.t_ingredients.heading("quantity", text="Quantity")
        self.t_ingredients.heading("unit", text="Unit")
        self.t_ingredients.heading("to_taste", text="To taste (q.b.)")
        self.t_ingredients.heading("status", text="Pantry Status")

        self.t_ingredients.column("name", width=190, anchor="w")
        self.t_ingredients.column("role", width=120, anchor="w")
        self.t_ingredients.column("quantity", width=80, anchor="e")
        self.t_ingredients.column("unit", width=60, anchor="center")
        self.t_ingredients.column("to_taste", width=95, anchor="center")
        self.t_ingredients.column("status", width=120, anchor="center")

        sb = ttk.Scrollbar(ing_frame, orient="vertical", command=self.t_ingredients.yview)
        self.t_ingredients.configure(yscrollcommand=sb.set)
        self.t_ingredients.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")

        ttk.Label(
            ing_frame,
            text="ℹ️ 'To taste' (q.b. / quanto basta): discretionary seasoning added to taste (e.g. herbs, spices, grated cheese).",
            font=("TkDefaultFont", 8, "italic"),
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        # Preparation Steps
        notes_frame = ttk.LabelFrame(center_frame, text="Synthesized Preparation Steps (Editable)", padding=8)
        notes_frame.grid(row=1, column=0, sticky="nsew")
        notes_frame.columnconfigure(0, weight=1)
        notes_frame.rowconfigure(0, weight=1)

        self.txt_notes = tk.Text(notes_frame, height=4, wrap="word", font=("TkDefaultFont", 9))
        notes_sb = ttk.Scrollbar(notes_frame, orient="vertical", command=self.txt_notes.yview)
        self.txt_notes.configure(yscrollcommand=notes_sb.set)
        self.txt_notes.grid(row=0, column=0, sticky="nsew")
        notes_sb.grid(row=0, column=1, sticky="ns")

        # 4. Macro Summary Bar
        macro_frame = ttk.Frame(main_frame)
        macro_frame.grid(row=3, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(macro_frame, textvariable=self.v_macros, font=("TkDefaultFont", 9, "bold")).pack(side="left")

        # 5. Bottom Action Bar
        btn_bar = ttk.Frame(main_frame)
        btn_bar.grid(row=4, column=0, sticky="ew")
        self.btn_regenerate = ttk.Button(
            btn_bar,
            text="🎲 Regenerate / Try Another",
            command=self.regenerate_proposal,
        )
        self.btn_regenerate.pack(side="left")

        ttk.Button(btn_bar, text="Cancel", command=self.cancel).pack(side="right", padx=(6, 0))
        self.btn_save = ttk.Button(btn_bar, text="💾 Save to Recipes", command=self.save)
        self.btn_save.pack(side="right")

    def generate_proposal(self, show_errors: bool = True) -> bool:
        """Runs the hybrid generator pipeline and populates the preview UI."""
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
                candidate = gen.generate_recipe(
                    target_kcal=target_kcal,
                    meal_type=meal_type,
                    archetype_name=archetype_name,
                    pantry_only=pantry_only,
                    db_path=self.db_path,
                    variation_index=self._variation_index,
                )
            except ValueError:
                if pantry_only:
                    # Automatic fallback to catalog suggestions if pantry lacks required slot
                    candidate = gen.generate_recipe(
                        target_kcal=target_kcal,
                        meal_type=meal_type,
                        archetype_name=archetype_name,
                        pantry_only=False,
                        db_path=self.db_path,
                        variation_index=self._variation_index,
                    )
                    candidate.warnings.append(
                        "Some required slots were missing in pantry; included catalog suggestions."
                    )
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
        """Advances the variation index to propose an alternative combination."""
        self._variation_index += 1
        return self.generate_proposal(show_errors=True)

    def _populate_candidate(self, candidate):
        self.v_title.set(candidate.title)
        warn_str = f"  |  ⚠️ {candidate.warnings[0]}" if candidate.warnings else ""
        self.v_meta.set(
            f"Archetype: {candidate.archetype_name}  |  Meal: {candidate.meal_type.capitalize()}  |  "
            f"Solver: {candidate.solver_status}  |  Caloric delta: {candidate.caloric_delta_pct:.1f}%{warn_str}"
        )

        for iid in self.t_ingredients.get_children():
            self.t_ingredients.delete(iid)

        for ing in candidate.ingredients:
            qty, unit = ing.calculate_display_quantity()
            role_label = ing.role.value if hasattr(ing.role, "value") else str(ing.role)
            qb_str = "Yes" if ing.to_taste else "No"
            if ing.is_missing:
                status_str = "Missing (To buy)"
            elif ing.is_open:
                status_str = "Opened (FIFO)"
            else:
                status_str = "In pantry"
            self.t_ingredients.insert(
                "",
                "end",
                values=(ing.name, role_label, f"{qty:g}", unit, qb_str, status_str),
            )

        self.txt_notes.delete("1.0", "end")
        self.txt_notes.insert("1.0", candidate.notes or "")

        self.v_macros.set(
            f"Per-serving values: {candidate.total_kcal:.0f} kcal (Target: {candidate.target_kcal:.0f} kcal)  |  "
            f"Prot: {candidate.total_protein:.1f}g  |  Carbs: {candidate.total_carbs:.1f}g  |  "
            f"Fat: {candidate.total_fat:.1f}g  |  Fiber: {candidate.total_fiber:.1f}g"
        )

    def save(self) -> bool:
        """Persists the generated recipe to SQLite RECIPES via create_recipe."""
        from meal_planner import recipes as rc

        if self.current_candidate is None:
            if not self.generate_proposal(show_errors=True):
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
            if self._wait:
                messagebox.showerror("Save Error", str(exc), parent=self)
            return False

        outcome["Name"] = title
        self.saved_outcome = outcome
        if callable(self.on_save_callback):
            self.on_save_callback(outcome)
        self.destroy()
        return True

    def cancel(self):
        self.saved_outcome = None
        self.destroy()



