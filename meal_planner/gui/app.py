"""Main window."""

from datetime import date
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
import contextlib
import io
import subprocess
import sys
import threading
import tkinter as tk

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    BaseTk = TkinterDnD.Tk
    HAS_DND = True
except (ImportError, Exception):
    BaseTk = tk.Tk
    HAS_DND = False

_RECEIPTS_DIR = Path(__file__).resolve().parents[1] / "receipts"
_RECEIPT_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}

from meal_planner import config
from meal_planner import engine as m
from meal_planner import planner as pf
from meal_planner import recipes as rc
from meal_planner.db.schema import migrate
from meal_planner.gui.dialogs import (
    ProductDialog,
    ChoiceDialog,
    AliasMapDialog,
    MealPrepDialog,
    ReceiptReconciliationDialog,
    InventoryItemEditDialog,
    MacroPromptDialog,
    ProfileSetupDialog,
    AddRecipeDialog,
    QuickProductDialog,
)
from meal_planner.planner.storage import schedule_meal_prep
from meal_planner.engine.profile import bmi, estimates, reset_profile, set_meal_shares, update_profile
from meal_planner.gui import theme
from meal_planner.gui.support import RED, GREEN, db, number, safe
from meal_planner.gui.widgets import Form, LineChart, Table
from meal_planner.inventory import (
    add_item,
    consume_quantity,
    get_available_inventory,
    get_inventory_rows,
    get_inventory_item,
    delete_inventory_item,
    update_inventory_item,
    is_product_macro_linked,
    get_locations,
    add_location,
    rename_location,
    delete_location,
    ProductNotFound,
    InvalidInput,
    get_exclusion_list,
    clear_inventory,
    get_receipt_logs,
    log_receipt,
)


class App(BaseTk):
    def __init__(self):
        super().__init__()
        self.title("Meal Planner")
        self.geometry("1140x700")
        self.minsize(920, 580)
        self.theme = theme.load()
        theme.apply(self.theme)
        ttk.Style(self).configure("Treeview", rowheight=28)
        with db() as c:
            migrate(c)                                     # makes sure the schema exists
        self.db_path = None
        self.plans, self.draft, self.fixed = [], [], {}
        self._cached_recipe_rows = None
        self.status = tk.StringVar(value="Ready.")
        header = ttk.Frame(self, padding=(8, 8, 8, 0))
        header.pack(fill="x")
        self.toggle = ttk.Button(header, command=self.toggle_theme)
        self.toggle.pack(side="right")
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=8, pady=(8, 0))
        for title, build in (("Pantry", self.tab_pantry), ("Shopping", self.tab_shopping),
                             ("Recipes", self.tab_recipes), ("Plan", self.tab_plan),
                             ("Profilo", self.tab_profile), ("Setting", self.tab_settings)):
            tab = ttk.Frame(self.nb, padding=10)
            self.nb.add(tab, text=title)
            build(tab)
        self.bar = ttk.Label(self, textvariable=self.status, anchor="w", padding=6)
        self.bar.pack(fill="x")
        self.nb.bind("<<NotebookTabChanged>>", lambda e: self.on_tab_changed())
        with db() as c:
            self.load_profile_form(m.load_profile(c))
        self.refresh()
        self.set_theme(self.theme)
        self._startup_alert_timer = self.after(200, self._startup_expiration_alert)

    def destroy(self):
        if hasattr(self, "_startup_alert_timer") and self._startup_alert_timer:
            try:
                self.after_cancel(self._startup_alert_timer)
            except Exception:
                pass
            self._startup_alert_timer = None
        super().destroy()

    def _startup_expiration_alert(self):
        """Shows a warning popup at startup if there are expired items in the inventory."""
        self._startup_alert_timer = None
        try:
            if not self.winfo_exists():
                return
            expired = get_exclusion_list()
            if not expired:
                return
            lines = []
            for item in expired:
                lines.append(f"  • {item['Name']} — expired {item['Expiration_Date']} ({item['Location']})")
            msg = (
                f"⚠️  {len(expired)} expired item(s) found in your pantry:\n\n"
                + "\n".join(lines)
                + "\n\nThese items are excluded from meal planning.\n"
                "Please discard or consume them."
            )
            messagebox.showwarning("Expired Items", msg, parent=self)
        except Exception:
            pass  # Never crash on startup alert

    def set_theme(self, name):
        self.theme = name
        theme.apply(name)
        theme.style_text(self.text, name)
        if hasattr(self, "_receipt_log") and isinstance(self._receipt_log, tk.Text):
            theme.style_text(self._receipt_log, name)
        if hasattr(self, "_drop_zone"):
            bg, fg = theme.palette(name)
            self._drop_zone.configure(background=bg)
            if hasattr(self, "_drop_label_id"):
                self._drop_zone.itemconfig(self._drop_label_id, fill=fg)
        if hasattr(self, "t_disp") and hasattr(self.t_disp, "update_delete_icon"):
            self.t_disp.update_delete_icon(is_dark=(name == "dark"))
        if hasattr(self, "lbl_full_overview_text") and isinstance(self.lbl_full_overview_text, tk.Text):
            theme.style_text(self.lbl_full_overview_text, name)
        if hasattr(self, "profile_overlay") and hasattr(self, "_draw_profile_overlay"):
            if self.profile_overlay.winfo_ismapped():
                self._draw_profile_overlay()
        self.chart.plot(self.chart.points, *theme.palette(name))
        self.toggle.configure(text="Light mode" if name == "dark" else "Dark mode")

    def toggle_theme(self):
        """Toggles theme in real time for the current session without modifying default startup theme."""
        self.set_theme("light" if self.theme == "dark" else "dark")

    def msg(self, text, error=False):
        self.status.set(text)
        self.bar.configure(foreground=RED if error else GREEN)

    # ---------------- tabs ----------------

    def tab_pantry(self, f):
        f.columnconfigure(1, weight=1)
        f.rowconfigure(0, weight=1)
        self.t_disp = Table(
            f,
            [("n", "Product", 200), ("q", "Available", 90), ("u", "Unit", 55),
             ("l", "Location", 110), ("e", "Expires", 95)],
            18,
            show_delete=True,
            on_delete=self._on_delete_inventory_row,
            on_select=self._on_edit_inventory_row,
            on_double_click=self._on_edit_inventory_row,
        )
        self.t_disp.grid(row=0, column=1, sticky="nsew")
        ttk.Button(f, text="Export to .txt", command=self.export_inventory).grid(
            row=1, column=1, sticky="e", pady=(8, 0))
        side = ttk.Frame(f)
        side.grid(row=0, column=0, sticky="n", padx=(0, 12))

        # --- Feature selector (3 pulsanti per le azioni della dispensa) ---
        sel_frame = ttk.LabelFrame(side, text="Pantry actions", padding=6)
        sel_frame.pack(fill="x", pady=(0, 6))

        hint_lbl = ttk.Label(
            sel_frame,
            text="Select an action to show its form:",
            font=("TkDefaultFont", 8, "italic"),
            anchor="w"
        )
        hint_lbl.pack(fill="x", pady=(0, 4))

        btn_grid = ttk.Frame(sel_frame)
        btn_grid.pack(fill="x")
        btn_grid.columnconfigure(0, weight=1, uniform="act_btn")
        btn_grid.columnconfigure(1, weight=1, uniform="act_btn")
        btn_grid.columnconfigure(2, weight=1, uniform="act_btn")

        self.v_pantry_action = tk.StringVar(value="Add to inventory")
        self.btn_act_add = ttk.Button(
            btn_grid, text="Add",
            command=lambda: self.switch_pantry_action("Add to inventory")
        )
        self.btn_act_use = ttk.Button(
            btn_grid, text="Consume",
            command=lambda: self.switch_pantry_action("Consume")
        )
        self.btn_act_loc = ttk.Button(
            btn_grid, text="Locations",
            command=lambda: self.switch_pantry_action("Locations")
        )

        self.btn_act_add.grid(row=0, column=0, sticky="ew", padx=(0, 2))
        self.btn_act_use.grid(row=0, column=1, sticky="ew", padx=2)
        self.btn_act_loc.grid(row=0, column=2, sticky="ew", padx=(2, 0))

        # Container for the selected action form
        action_container = ttk.Frame(side)
        action_container.pack(fill="x", pady=(0, 6))

        self.f_add = Form(action_container, "Add to inventory",
                          [("name", "Product", []), ("weight", "Package weight/volume", None),
                           ("num", "No. of packages", None), ("location", "Location", []),
                           ("exp", "Expiry date (YYYY-MM-DD)", None)], "Add", self.add)
        self.f_use = Form(action_container, "Consume", [("name", "Product", []), ("q", "Quantity", None)],
                          "Consume", self.consume)

        # --- Location manager ---
        self.loc_mgr = ttk.LabelFrame(action_container, text="Locations", padding=6)
        self.v_loc_sel = tk.StringVar()
        self.cb_loc = ttk.Combobox(self.loc_mgr, textvariable=self.v_loc_sel, width=22, state="readonly")
        self.cb_loc.pack(fill="x")
        btn_row = ttk.Frame(self.loc_mgr)
        btn_row.pack(fill="x", pady=(4, 0))
        ttk.Button(btn_row, text="Add",    command=self.loc_add).pack(side="left")
        ttk.Button(btn_row, text="Rename", command=self.loc_rename).pack(side="left", padx=4)
        ttk.Button(btn_row, text="Delete", command=self.loc_delete).pack(side="left")

        self.switch_pantry_action("Add to inventory")

        # --- Receipt import (drag & drop and log - always visible) ---
        rec_mgr = ttk.LabelFrame(side, text="Receipts", padding=6)
        rec_mgr.pack(fill="x")

        DROP_H = 70
        bg, fg = theme.palette(self.theme)
        self._drop_zone = tk.Canvas(
            rec_mgr, height=DROP_H, highlightthickness=2,
            highlightbackground="#3b82f6" if HAS_DND else "#888888",
            background=bg, cursor="hand2"
        )
        self._drop_zone.pack(fill="x", pady=(0, 4))
        self._drop_label_id = self._drop_zone.create_text(
            120, DROP_H // 2 - 10,
            text="⬇  Drag receipts here  ⬇",
            font=("TkDefaultFont", 10, "bold"),
            anchor="center",
            fill=fg
        )
        hint_text = "or click to browse" if HAS_DND else "(click to browse image)"
        self._drop_hint_id = self._drop_zone.create_text(
            120, DROP_H // 2 + 12,
            text=hint_text,
            font=("TkDefaultFont", 8),
            anchor="center",
            fill="#888888"
        )

        if HAS_DND:
            try:
                self._drop_zone.drop_target_register(DND_FILES)
                self._drop_zone.dnd_bind("<<Drop>>", self._on_receipt_drop)
            except Exception:
                pass

        def _recenter(e):
            w = e.width if e.width > 1 else 160
            self._drop_zone.coords(self._drop_label_id, w // 2, DROP_H // 2 - 10)
            if hasattr(self, "_drop_hint_id"):
                self._drop_zone.coords(self._drop_hint_id, w // 2, DROP_H // 2 + 12)

        self._drop_zone.bind("<Configure>", _recenter)
        self._drop_zone.bind("<Button-1>", lambda e: self.receipt_browse())

        browse_bar = ttk.Frame(rec_mgr)
        browse_bar.pack(fill="x", pady=(0, 4))
        ttk.Button(browse_bar, text="Browse files...", command=self.receipt_browse).pack(side="left")
        self._receipt_status = tk.StringVar(value="")
        ttk.Label(browse_bar, textvariable=self._receipt_status, font=("TkDefaultFont", 8), anchor="w").pack(
            side="left", padx=(6, 0)
        )

        self._last_receipt_var = tk.StringVar(value="Last receipt loaded: None")
        self._receipt_log_text = ""
        self.lbl_last_receipt = ttk.Label(
            rec_mgr,
            textvariable=self._last_receipt_var,
            font=("TkDefaultFont", 8, "italic"),
            anchor="w",
            padding=(2, 4),
        )
        self.lbl_last_receipt.pack(fill="x", pady=(2, 0))

        class _ReceiptLogProxy:
            def __init__(self, outer):
                self._outer = outer
            def insert(self, *args):
                pass
            def see(self, *args):
                pass
            def configure(self, **kwargs):
                pass
            def get(self, *args):
                return self._outer._receipt_log_text
        self._receipt_log = _ReceiptLogProxy(self)

    def tab_shopping(self, f):
        ttk.Label(f, text="What is missing: consumption beyond availability and plan ingredients.").pack(anchor="w")
        self.t_shopping = Table(f, [("n", "Product", 320), ("q", "Missing quantity", 140), ("u", "Unit", 80)], 20)
        self.t_shopping.pack(fill="both", expand=True, pady=8)

    def tab_recipes(self, f):
        f.columnconfigure(1, weight=1)
        f.rowconfigure(1, weight=1)
        ttk.Label(f, text="Macros per serving and feasibility with the current inventory "
                          "('Open' = share taken from packages already open).").grid(row=0, column=1, sticky="w")
        self.t_ric = Table(f, [("r", "Recipe", 200), ("k", "kcal", 50), ("p", "Prot", 50), ("c", "Carb", 50),
                                 ("g", "Fat", 50), ("fi", "Fiber", 50), ("a", "Open", 55),
                                 ("e", "Outcome", 220)], 18)
        self.t_ric.grid(row=1, column=1, sticky="nsew", pady=8)
        bar = ttk.Frame(f)
        bar.grid(row=2, column=1, sticky="w")
        self.btn_add_recipe = ttk.Button(bar, text="Nuova ricetta...", command=self.open_add_recipe_dialog)
        self.btn_add_recipe.pack(side="left", padx=(0, 6))
        ttk.Button(bar, text="Import from file (.json / .csv)...", command=self.import_recipes).pack(side="left")
        ttk.Button(bar, text="Delete selected recipe", command=self.delete_recipe).pack(side="left", padx=6)

        new = ttk.LabelFrame(f, text="New recipe (quantities for ONE serving)", padding=10)
        new.grid(row=0, column=0, rowspan=3, sticky="n", padx=(0, 12))
        self.v_rnome = tk.StringVar()
        ttk.Label(new, text="Name").grid(row=0, column=0, sticky="w")
        ttk.Entry(new, textvariable=self.v_rnome, width=30).grid(row=0, column=1, sticky="ew", padx=(8, 0))
        with db() as c:
            self.v_meals = {n: tk.BooleanVar() for (n,) in c.execute("SELECT Name FROM MEALS ORDER BY Sort_Order")}
        checkboxes = ttk.Frame(new)
        checkboxes.grid(row=1, column=0, columnspan=2, sticky="w", pady=4)
        ttk.Label(checkboxes, text="Meals:").pack(side="left", padx=(0, 6))
        for name, var in self.v_meals.items():
            ttk.Checkbutton(checkboxes, text=name, variable=var).pack(side="left", padx=(0, 6))
        self.f_ing = Form(new, "Ingredient", [("p", "Product or group", []), ("q", "Quantity", None),
                                                 ("u", "Unit", list(rc.UNITS))],
                          "Add ingredient", self.add_ingredient)
        self.f_ing.grid(row=2, column=0, columnspan=2, sticky="ew")
        self.t_draft = Table(new, [("p", "Recipe ingredients", 160), ("q", "Quantity", 70),
                                       ("u", "Unit", 70)], 5)
        self.t_draft.grid(row=3, column=0, columnspan=2, pady=6)
        ttk.Button(new, text="Remove selected ingredient",
                   command=self.remove_ingredient).grid(row=4, column=0, columnspan=2, sticky="ew")
        ttk.Button(new, text="Save recipe", command=self.save_recipe).grid(
            row=5, column=0, columnspan=2, sticky="ew", pady=(6, 0))

    def tab_plan(self, f):
        top = ttk.Frame(f)
        top.pack(fill="x")
        self.v_date, self.v_days, self.v_kcal = (tk.StringVar(value=date.today().isoformat()),
                                                   tk.StringVar(value="7"), tk.StringVar())
        for label, var, width in (("Start date", self.v_date, 12), ("Days", self.v_days, 4),
                                          ("Kcal (empty = profile)", self.v_kcal, 8)):
            ttk.Label(top, text=label).pack(side="left", padx=(0, 4))
            ttk.Entry(top, textvariable=var, width=width).pack(side="left", padx=(0, 12))
        for text, command in (("Propose plan", self.propose), ("Save plan", self.save),
                               ("Mark as cooked", self.cooked)):
            ttk.Button(top, text=text, command=command).pack(side="left", padx=3)
        middle = ttk.Frame(f)
        middle.pack(fill="x", pady=(6, 0))
        ttk.Label(middle, text="On the selected meal:").pack(side="left", padx=(0, 6))
        for text, command in (("Lock recipe...", self.lock), ("Skip meal", self.skip),
                               ("Unlock all", self.unlock)):
            ttk.Button(middle, text=text, command=command).pack(side="left", padx=3)
        ttk.Button(middle, text="Meal Prep...", command=self.open_meal_prep_dialog).pack(side="left", padx=(12, 3))
        self.t_plan = Table(f, [("d", "Date", 95), ("p", "Meal", 90), ("r", "Recipe", 290),
                                   ("k", "kcal", 60), ("s", "Status", 360)], 9)
        self.t_plan.pack(fill="both", expand=True, pady=8)
        self.text = tk.Text(f, height=9, wrap="word", state="disabled", relief="flat")
        self.text.pack(fill="x")

    def tab_profile(self, f):
        keys = ("Weight_kg", "Height_cm", "Age_years", "Body_fat_pct", "Waist_cm", "Sex", "Activity_Level", "Goal",
                "Target_weight_kg", "Weekly_rate_kg", "Maintenance_manual", "Kcal_manual",
                "Kcal_tolerance_pct", "Open_extra_pct", "No_repeat_days", "Default_servings")
        self.pv = {k: tk.StringVar() for k in keys}
        self.pu = {k: tk.BooleanVar() for k in ("Use_manual_maintenance", "Use_manual_kcal")}
        self.calc_m, self.calc_k = tk.StringVar(), tk.StringVar()
        with db() as c:
            self.pm = {n: tk.StringVar(value=f"{s:g}") for n, s in
                       c.execute("SELECT Name, Energy_Share_pct FROM MEALS ORDER BY Sort_Order")}

        def box(title, column, row):
            b = ttk.LabelFrame(f, text=title, padding=10)
            b.grid(row=row, column=column, sticky="nw", padx=(0, 12), pady=(0, 10))
            return b

        def field(b, label, var, values=None):
            r = b.grid_size()[1]
            ttk.Label(b, text=label).grid(row=r, column=0, sticky="w", pady=3)
            cls, kw = (ttk.Combobox, {"values": values, "state": "readonly"}) if values else (ttk.Entry, {})
            cls(b, textvariable=var, width=10, **kw).grid(row=r, column=1, padx=(8, 0), pady=3, sticky="w")

        def estimate(b, label, calc, key, use):
            r = b.grid_size()[1]
            ttk.Label(b, text=label).grid(row=r, column=0, sticky="w", pady=3)
            ttk.Label(b, textvariable=calc, width=16).grid(row=r, column=1, padx=(8, 0))
            ttk.Entry(b, textvariable=self.pv[key], width=8).grid(row=r, column=2, padx=(8, 0))
            ttk.Checkbutton(b, text="Use manual", variable=self.pu[use]).grid(row=r, column=3, padx=(8, 0))

        # Body box: dynamic metrics only (Height_cm moved to Settings)
        body = box("Body", 0, 0)
        for label, key in (("Weight (kg)", "Weight_kg"), ("Age (years)", "Age_years"),
                           ("Body fat (%)", "Body_fat_pct"), ("Waist (cm)", "Waist_cm")):
            field(body, label, self.pv[key])
        self.bmi_label = ttk.Label(body)
        self.bmi_label.grid(row=body.grid_size()[1], column=0, columnspan=2, sticky="w", pady=(6, 0))

        settings = box("Planner settings", 0, 1)
        for label, key in (("Kcal tolerance (%)", "Kcal_tolerance_pct"), ("Open-package extra (%)", "Open_extra_pct"),
                           ("No-repeat days", "No_repeat_days"), ("Default servings per cooking", "Default_servings")):
            field(settings, label, self.pv[key])

        # Goal box: dynamic metrics only (Sex moved to Settings)
        goal = box("Goal", 1, 0)
        field(goal, "Activity", self.pv["Activity_Level"], sorted(m.PAL))
        field(goal, "Goal", self.pv["Goal"], ["maintenance", "weight_loss", "weight_gain"])
        field(goal, "Target weight (kg)", self.pv["Target_weight_kg"])
        field(goal, "Weekly rate (kg/week)", self.pv["Weekly_rate_kg"])
        estimate(goal, "Maintenance kcal", self.calc_m, "Maintenance_manual", "Use_manual_maintenance")
        estimate(goal, "Target kcal/day", self.calc_k, "Kcal_manual", "Use_manual_kcal")
        self.profile_label = ttk.Label(goal)
        self.profile_label.grid(row=goal.grid_size()[1], column=0, columnspan=4, sticky="w", pady=(6, 0))

        meals = box("Energy share per meal (%)", 1, 1)
        for name, var in self.pm.items():
            field(meals, name, var)

        ttk.Button(f, text="Save profile", command=self.save_profile).grid(
            row=2, column=0, columnspan=2, sticky="ew", padx=(0, 12))

        progress = ttk.LabelFrame(f, text="Progress", padding=10)
        progress.grid(row=0, column=2, rowspan=3, sticky="nsew")
        f.columnconfigure(2, weight=1)
        f.rowconfigure(0, weight=1)
        self.t_hist = Table(progress, [("d", "Date", 90), ("w", "Weight", 70), ("b", "Body fat %", 80),
                                       ("c", "Waist", 70)], 6)
        self.t_hist.pack(fill="x")
        self.chart = LineChart(progress)
        self.chart.pack(fill="both", expand=True, pady=(10, 0))

        self.tab_profile_frame = f
        self._init_profile_overlay(f)

    def _init_profile_overlay(self, parent):
        """Initializes empty-state overlay covering the profile tab."""
        self.profile_overlay = tk.Canvas(parent, highlightthickness=0, borderwidth=0)
        self.profile_overlay.bind("<Configure>", lambda e: self._draw_profile_overlay())

    def _draw_profile_overlay(self):
        """Draws the large rounded rectangle and verbatim Italian message on overlay canvas."""
        c = self.profile_overlay
        c.delete("all")
        w = c.winfo_width()
        h = c.winfo_height()
        if w <= 1 or h <= 1:
            frame_ref = getattr(self, "tab_profile_frame", c)
            w = max(frame_ref.winfo_width(), 700)
            h = max(frame_ref.winfo_height(), 500)

        is_dark = (getattr(self, "theme", "dark") == "dark")
        bg_color = "#1e222b" if is_dark else "#f3f4f6"
        card_fill = "#282c37" if is_dark else "#ffffff"
        card_outline = "#3f4656" if is_dark else "#cbd5e1"
        text_color = "#f9fafb" if is_dark else "#111827"
        subtext_color = "#9ca3af" if is_dark else "#6b7280"

        c.configure(bg=bg_color)

        card_w = min(560, max(360, w - 80))
        card_h = min(280, max(200, h - 80))
        x1 = (w - card_w) / 2
        y1 = (h - card_h) / 2
        x2 = x1 + card_w
        y2 = y1 + card_h
        r = 22

        points = [
            x1 + r, y1,
            x1 + r, y1,
            x2 - r, y1,
            x2 - r, y1,
            x2, y1,
            x2, y1 + r,
            x2, y1 + r,
            x2, y2 - r,
            x2, y2 - r,
            x2, y2,
            x2 - r, y2,
            x2 - r, y2,
            x1 + r, y2,
            x1 + r, y2,
            x1, y2,
            x1, y2 - r,
            x1, y2 - r,
            x1, y1 + r,
            x1, y1 + r,
            x1, y1,
        ]
        c.create_polygon(points, fill=card_fill, outline=card_outline, width=2, smooth=True, tags=("overlay_card",))

        # Exact verbatim Italian text required by Requirement R3
        exact_text = "Ciao, aggiunti il tuo profilo per iniziare"
        c.create_text(
            (x1 + x2) / 2,
            y1 + card_h * 0.35,
            text=exact_text,
            fill=text_color,
            font=("TkDefaultFont", 15, "bold"),
            justify="center",
            tags=("overlay_text", "overlay_card")
        )

        sub_text = "Configura il tuo profilo per calcolare il fabbisogno calorico e iniziare a pianificare i pasti."
        c.create_text(
            (x1 + x2) / 2,
            y1 + card_h * 0.54,
            text=sub_text,
            fill=subtext_color,
            font=("TkDefaultFont", 10),
            justify="center",
            width=card_w - 60,
            tags=("overlay_card",)
        )

        btn = ttk.Button(c, text="➕ Aggiungi profilo", command=self.open_profile_setup_dialog)
        c.create_window((x1 + x2) / 2, y1 + card_h * 0.76, window=btn, tags=("overlay_btn",))

        c.tag_bind("overlay_card", "<Button-1>", lambda e: self.open_profile_setup_dialog())
        c.tag_bind("overlay_card", "<Enter>", lambda e: c.config(cursor="hand2"))
        c.tag_bind("overlay_card", "<Leave>", lambda e: c.config(cursor=""))

    def open_profile_setup_dialog(self):
        """Interactive modal dialog allowing user to configure their profile when overlay is shown."""
        db_path = None
        if hasattr(self, "db_path"):
            db_path = self.db_path
        elif "MEAL_PLANNER_DB" in os.environ:
            db_path = os.environ["MEAL_PLANNER_DB"]
        dialog = ProfileSetupDialog(self, db_path=db_path)
        if dialog.result:
            with db() as c:
                self.load_profile_form(m.load_profile(c))
            self.refresh(active_tab="Profilo")

    # ---------------- Setting tab (Requirement R2) ----------------

    def tab_settings(self, f):
        """Settings tab with non-monolithic section selection controller."""
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)

        # 1. Non-monolithic section controller (button bar)
        nav_frame = ttk.LabelFrame(f, text="Settings Sections", padding=6)
        nav_frame.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        for i in range(4):
            nav_frame.columnconfigure(i, weight=1, uniform="sec_btn")

        self.v_settings_section = tk.StringVar(value="style_and_config")

        self.btn_sec_style = ttk.Button(
            nav_frame, text="Style & Config",
            command=lambda: self.switch_settings_section("style_and_config")
        )
        self.btn_sec_profile = ttk.Button(
            nav_frame, text="Full Profile",
            command=lambda: self.switch_settings_section("full_profile")
        )
        self.btn_sec_receipts = ttk.Button(
            nav_frame, text="Receipt Logs",
            command=lambda: self.switch_settings_section("receipt_logs")
        )
        self.btn_sec_destructive = ttk.Button(
            nav_frame, text="Destructive Actions",
            command=lambda: self.switch_settings_section("destructive_actions")
        )

        self.btn_sec_style.grid(row=0, column=0, sticky="ew", padx=(0, 2))
        self.btn_sec_profile.grid(row=0, column=1, sticky="ew", padx=2)
        self.btn_sec_receipts.grid(row=0, column=2, sticky="ew", padx=2)
        self.btn_sec_destructive.grid(row=0, column=3, sticky="ew", padx=(2, 0))

        # 2. Main content container
        self.settings_container = ttk.Frame(f)
        self.settings_container.grid(row=1, column=0, sticky="nsew")

        # Build the 4 section subframes
        self.f_sec_style = ttk.Frame(self.settings_container)
        self.f_sec_profile = ttk.Frame(self.settings_container)
        self.f_sec_receipts = ttk.Frame(self.settings_container)
        self.f_sec_destructive = ttk.Frame(self.settings_container)

        self._build_sec_style_and_config(self.f_sec_style)
        self._build_sec_full_profile(self.f_sec_profile)
        self._build_sec_receipt_logs(self.f_sec_receipts)
        self._build_sec_destructive_actions(self.f_sec_destructive)

        self.switch_settings_section("style_and_config")

    def switch_settings_section(self, section: str):
        """Switches active section in Settings tab."""
        if hasattr(self, "nb"):
            for i in range(self.nb.index("end")):
                if self.nb.tab(i, "text") in ("Setting", "Settings"):
                    try:
                        cur_idx = self.nb.index(self.nb.select())
                    except Exception:
                        cur_idx = -1
                    if cur_idx != i:
                        self.nb.select(i)
                    break

        self.v_settings_section.set(section)
        self.f_sec_style.pack_forget()
        self.f_sec_profile.pack_forget()
        self.f_sec_receipts.pack_forget()
        self.f_sec_destructive.pack_forget()

        if section == "style_and_config":
            self.f_sec_style.pack(fill="both", expand=True)
        elif section == "full_profile":
            self.f_sec_profile.pack(fill="both", expand=True)
            self._refresh_full_profile_view()
        elif section == "receipt_logs":
            self.f_sec_receipts.pack(fill="both", expand=True)
            self._refresh_receipt_logs_table()
        elif section == "destructive_actions":
            self.f_sec_destructive.pack(fill="both", expand=True)

    def _build_sec_style_and_config(self, parent):
        box = ttk.LabelFrame(parent, text="Style & Configuration Settings", padding=14)
        box.pack(fill="x", padx=4, pady=4)

        # Theme selection
        ttk.Label(box, text="Default Startup Theme:").grid(row=0, column=0, sticky="w", pady=6)
        self.v_cfg_theme = tk.StringVar(value=config.get("default_theme", "dark"))
        cb_theme = ttk.Combobox(box, textvariable=self.v_cfg_theme, values=["dark", "light"], state="readonly", width=12)
        cb_theme.grid(row=0, column=1, sticky="w", padx=(10, 0), pady=6)

        # Expiration warning days
        ttk.Label(box, text="Expiration warning alert (days):").grid(row=1, column=0, sticky="w", pady=6)
        self.v_cfg_warn_days = tk.StringVar(value=str(config.get("expiration_warning_days", 7)))
        ttk.Entry(box, textvariable=self.v_cfg_warn_days, width=12).grid(row=1, column=1, sticky="w", padx=(10, 0), pady=6)

        # Receipts output directory
        ttk.Label(box, text="Receipts output directory:").grid(row=2, column=0, sticky="w", pady=6)
        self.v_cfg_receipts_dir = tk.StringVar(value=str(config.get("receipts_output_dir", "receipts_output")))
        dir_row = ttk.Frame(box)
        dir_row.grid(row=2, column=1, sticky="w", padx=(10, 0), pady=6)
        ttk.Entry(dir_row, textvariable=self.v_cfg_receipts_dir, width=28).pack(side="left")
        ttk.Button(dir_row, text="Browse...", command=self._browse_receipts_dir).pack(side="left", padx=(6, 0))

        # Save button
        ttk.Button(box, text="Save Configuration", command=self.save_settings_config).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(12, 0)
        )

    def _browse_receipts_dir(self):
        chosen = filedialog.askdirectory(title="Select Receipts Output Directory", parent=self)
        if chosen:
            self.v_cfg_receipts_dir.set(chosen)

    def save_settings_config(self):
        theme_val = self.v_cfg_theme.get().strip().lower()
        if theme_val not in ("dark", "light"):
            theme_val = "dark"
        try:
            warn_days = int(self.v_cfg_warn_days.get().strip())
            if warn_days < 0:
                raise ValueError()
        except ValueError:
            messagebox.showerror("Invalid Input", "Expiration warning days must be a non-negative integer.", parent=self)
            return

        out_dir = self.v_cfg_receipts_dir.get().strip() or "receipts_output"
        config.save_config({
            "default_theme": theme_val,
            "theme": theme_val,
            "expiration_warning_days": warn_days,
            "receipts_output_dir": out_dir,
        })

        if theme_val != self.theme:
            self.set_theme(theme_val)
        self.refresh(active_tab="Pantry")
        self.msg("Configuration saved successfully.")

    def _build_sec_full_profile(self, parent):
        static_box = ttk.LabelFrame(parent, text="Static Profile Information (Never Changes)", padding=12)
        static_box.pack(fill="x", padx=4, pady=4)

        ttk.Label(static_box, text="Sex:").grid(row=0, column=0, sticky="w", pady=4)
        init_sex = self.pv["Sex"].get() if hasattr(self, "pv") and "Sex" in self.pv else ""
        self.v_static_sex = tk.StringVar(value=init_sex)
        self.cb_static_sex = ttk.Combobox(static_box, textvariable=self.v_static_sex, values=["M", "F"], state="readonly", width=8)
        self.cb_static_sex.grid(row=0, column=1, sticky="w", padx=(10, 0), pady=4)

        ttk.Label(static_box, text="Height (cm):").grid(row=1, column=0, sticky="w", pady=4)
        init_height = self.pv["Height_cm"].get() if hasattr(self, "pv") and "Height_cm" in self.pv else ""
        self.v_static_height = tk.StringVar(value=init_height)
        ttk.Entry(static_box, textvariable=self.v_static_height, width=10).grid(row=1, column=1, sticky="w", padx=(10, 0), pady=4)

        ttk.Button(static_box, text="Save Static Profile", command=self.save_static_profile).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(10, 0)
        )

        self.f_full_overview = ttk.LabelFrame(parent, text="Complete Profile & Planning Overview", padding=12)
        self.f_full_overview.pack(fill="both", expand=True, padx=4, pady=4)
        self.lbl_full_overview_text = tk.Text(self.f_full_overview, wrap="word", state="disabled", height=14)
        self.lbl_full_overview_text.pack(fill="both", expand=True)

    def save_static_profile(self):
        sex = self.v_static_sex.get().strip()
        if sex not in ("M", "F"):
            messagebox.showerror("Invalid Sex", "Please select 'M' or 'F' for Sex.", parent=self)
            return
        try:
            height = float(self.v_static_height.get().strip())
            if height <= 0:
                raise ValueError()
        except ValueError:
            messagebox.showerror("Invalid Height", "Height must be a positive number in cm.", parent=self)
            return

        with db() as c:
            cur = c.cursor()
            cur.execute("SELECT COUNT(*) FROM PROFILE WHERE ID = 1")
            if cur.fetchone()[0] > 0:
                cur.execute("UPDATE PROFILE SET Sex = ?, Height_cm = ? WHERE ID = 1", (sex, height))
            else:
                cur.execute(
                    "INSERT INTO PROFILE (ID, Height_cm, Sex) VALUES (1, ?, ?)",
                    (height, sex)
                )
            c.commit()
            if hasattr(self, "pv"):
                if "Sex" in self.pv:
                    self.pv["Sex"].set(sex)
                if "Height_cm" in self.pv:
                    self.pv["Height_cm"].set(f"{height:g}")
            p = m.load_profile(c)
            if p and p.get("Weight_kg") and p.get("Activity_Level"):
                _, _, target = estimates(p)
                if target:
                    cur.execute("UPDATE PROFILE SET Kcal_target = ? WHERE ID = 1", (target,))
                    c.commit()
            self.msg("Static profile updated.")
            self.refresh()
            self._refresh_full_profile_view()

    def _refresh_full_profile_view(self):
        if not hasattr(self, "lbl_full_overview_text"):
            return
        self.lbl_full_overview_text.configure(state="normal")
        self.lbl_full_overview_text.delete("1.0", "end")

        with db() as c:
            p = m.load_profile(c)
            if not p or (not p.get("Sex") and not p.get("Height_cm") and not p.get("Weight_kg")):
                if hasattr(self, "v_static_sex"):
                    self.v_static_sex.set("")
                if hasattr(self, "v_static_height"):
                    self.v_static_height.set("")
                self.lbl_full_overview_text.insert(
                    "end",
                    "No user profile is currently configured.\n"
                    "No profile metrics to display. Please configure your profile in the Profilo tab.\n"
                )
                self.lbl_full_overview_text.configure(state="disabled")
                return

            calc_m, calc_k, target = estimates(p)
            bmi_val = None
            bmi_cat = "N/A"
            if p.get("Weight_kg") and p.get("Height_cm"):
                bmi_val, bmi_cat = bmi(p["Weight_kg"], p["Height_cm"])
            meals = c.execute("SELECT Name, Energy_Share_pct FROM MEALS ORDER BY Sort_Order").fetchall()

        if hasattr(self, "v_static_sex") and p.get("Sex"):
            self.v_static_sex.set(p["Sex"])
        if hasattr(self, "v_static_height") and p.get("Height_cm"):
            self.v_static_height.set(f"{p['Height_cm']:g}")

        h_str = f"{p['Height_cm']:g} cm" if p.get("Height_cm") else "Not set"
        w_str = f"{p['Weight_kg']:g} kg" if p.get("Weight_kg") else "Not set"
        age_str = f"{p['Age_years']} years" if p.get("Age_years") else "Not set"
        bmi_str = f"BMI {bmi_val:.1f} ({bmi_cat})" if bmi_val else "N/A"

        lines = [
            "--- STATIC METRICS ---",
            f"  • Sex:                 {p.get('Sex') or 'Not set'}",
            f"  • Height:              {h_str}",
            "",
            "--- DYNAMIC BODY METRICS ---",
            f"  • Weight:              {w_str}",
            f"  • Age:                 {age_str}",
            f"  • Body Mass Index:     {bmi_str}",
            "",
            "--- GOAL & CALORIC TARGETS ---",
            f"  • Goal:                {p.get('Goal') or 'Not set'}",
            f"  • Activity Level:      {p.get('Activity_Level') or 'Not set'}",
            f"  • Maintenance Kcal:    {calc_m or 'N/A'} kcal/day",
            f"  • Caloric Target:      {p.get('Kcal_target') or target or 'N/A'} kcal/day",
            "",
            "--- PLANNING PARAMETERS ---",
            f"  • Kcal Tolerance:      ±{p.get('Kcal_tolerance_pct', 5):g}%",
            f"  • Open Package Extra:  +{p.get('Open_extra_pct', 5):g}%",
            f"  • No-Repeat Window:    {p.get('No_repeat_days', 3)} days",
            f"  • Default Servings:    {p.get('Default_servings', 1)} serving(s)",
            "",
            "--- MEAL ENERGY SHARES ---",
        ]
        for name, share in meals:
            lines.append(f"  • {name.capitalize()}: {share:g}%")

        self.lbl_full_overview_text.insert("end", "\n".join(lines))
        self.lbl_full_overview_text.configure(state="disabled")

    def _build_sec_receipt_logs(self, parent):
        top_bar = ttk.Frame(parent)
        top_bar.pack(fill="x", padx=4, pady=4)
        ttk.Label(top_bar, text="History of Imported Receipts:", font=("TkDefaultFont", 9, "bold")).pack(side="left")
        ttk.Button(top_bar, text="Refresh Logs", command=self._refresh_receipt_logs_table).pack(side="right")

        self.t_receipt_logs = Table(
            parent,
            [("t", "Time", 120), ("d", "Day", 120), ("f", "File Name", 360)],
            20
        )
        self.t_receipt_logs.pack(fill="both", expand=True, padx=4, pady=(0, 4))

    def _refresh_receipt_logs_table(self):
        if not hasattr(self, "t_receipt_logs"):
            return
        with db() as c:
            logs = get_receipt_logs(c)
        self.t_receipt_logs.fill([((t, d, f), None) for t, d, f in logs])

    def _build_sec_destructive_actions(self, parent):
        warn_lbl = ttk.Label(
            parent,
            text="⚠️ DANGER ZONE: Destructive Data Management",
            font=("TkDefaultFont", 11, "bold"),
            foreground=RED
        )
        warn_lbl.pack(anchor="w", padx=4, pady=(8, 4))

        desc_lbl = ttk.Label(
            parent,
            text="Actions in this section permanently delete data from your local database.\n"
                 "Each operation requires explicit confirmation before execution.",
            wraplength=600
        )
        desc_lbl.pack(anchor="w", padx=4, pady=(0, 12))

        # Action 1: Hard-Delete Inventory
        inv_box = ttk.LabelFrame(parent, text="Hard-Delete Entire Inventory", padding=12)
        inv_box.pack(fill="x", padx=4, pady=6)
        ttk.Label(
            inv_box,
            text="Permanently delete all food stock, quantities, and locations from inventory.\n"
                 "Catalog products and recipes are preserved."
        ).pack(anchor="w", pady=(0, 8))
        ttk.Button(inv_box, text="Delete Entire Inventory", command=self.on_clear_inventory).pack(anchor="w")

        # Action 2: Reset User Profile
        prof_box = ttk.LabelFrame(parent, text="Reset User Profile", padding=12)
        prof_box.pack(fill="x", padx=4, pady=6)
        ttk.Label(
            prof_box,
            text="Permanently delete the user profile and all historical weight logs.\n"
                 "The application returns to the unconfigured empty-profile state."
        ).pack(anchor="w", pady=(0, 8))
        ttk.Button(prof_box, text="Reset User Profile", command=self.on_reset_profile).pack(anchor="w")

    def on_clear_inventory(self):
        confirm = messagebox.askyesno(
            "Confirm Hard-Delete Inventory",
            "Are you sure you want to permanently delete ALL items from your inventory?\n\n"
            "This action CANNOT be undone.",
            parent=self,
            icon="warning"
        )
        if not confirm:
            self.msg("Inventory deletion cancelled.")
            return

        with db() as c:
            clear_inventory(c)
        self.refresh(active_tab="Pantry")
        self.msg("Entire inventory has been permanently deleted.", error=True)

    def on_reset_profile(self):
        confirm = messagebox.askyesno(
            "Confirm Profile Reset",
            "Are you sure you want to permanently RESET your user profile?\n\n"
            "All physical metrics, targets, and weight history will be wiped.\n"
            "This action CANNOT be undone.",
            parent=self,
            icon="warning"
        )
        if not confirm:
            self.msg("Profile reset cancelled.")
            return

        with db() as c:
            reset_profile(c)

        for var in self.pv.values():
            var.set("")
        for var in self.pu.values():
            var.set(False)
        self.calc_m.set("")
        self.calc_k.set("")

        # Restore schema planner parameter defaults
        self.pv["Kcal_tolerance_pct"].set("5")
        self.pv["Open_extra_pct"].set("5")
        self.pv["No_repeat_days"].set("3")
        self.pv["Default_servings"].set("1")

        if hasattr(self, "v_static_sex"):
            self.v_static_sex.set("")
        if hasattr(self, "v_static_height"):
            self.v_static_height.set("")

        self.refresh()
        self._refresh_full_profile_view()
        self.msg("User profile has been reset.", error=True)

    def load_profile_form(self, p):
        if not p:
            return
        for k, var in self.pv.items():
            value = p.get(k)
            var.set("" if value is None else f"{value:g}" if isinstance(value, float) else str(value))
        for k, var in self.pu.items():
            var.set(bool(p.get(k)))
        if "Sex" in p and hasattr(self, "v_static_sex") and p.get("Sex"):
            self.v_static_sex.set(p["Sex"])
        if "Height_cm" in p and hasattr(self, "v_static_height") and p.get("Height_cm"):
            self.v_static_height.set(f"{p['Height_cm']:g}")

    def on_tab_changed(self):
        try:
            current_tab = self.nb.tab(self.nb.select(), "text")
        except Exception:
            current_tab = None
        self.refresh(active_tab=current_tab)

    # ---------------- refresh ----------------

    def refresh(self, active_tab=None):
        if active_tab is None:
            self._cached_recipe_rows = None

        if active_tab is not None and hasattr(self, "nb") and not getattr(self, "_syncing_tab", False):
            target_title = "Profilo" if active_tab in ("Profile", "Profilo") else ("Setting" if active_tab in ("Setting", "Settings") else active_tab)
            for i in range(self.nb.index("end")):
                if self.nb.tab(i, "text") == target_title:
                    try:
                        cur_idx = self.nb.index(self.nb.select())
                    except Exception:
                        cur_idx = -1
                    if cur_idx != i:
                        self._syncing_tab = True
                        try:
                            self.nb.select(i)
                        finally:
                            self._syncing_tab = False
                    break

        with db() as c:
            # Common names for forms (only if pantry or recipes might need them)
            if active_tab in (None, "Pantry", "Recipes"):
                names = [r[0] for r in c.execute("SELECT Name FROM ALIASES ORDER BY Name")]
                for form in (self.f_add, self.f_use):
                    form.w["name"]["values"] = names
                locations = get_locations()
                self.cb_loc["values"] = locations
                self.f_add.w["location"]["values"] = locations

            if active_tab in (None, "Recipes"):
                groups = [r[0] for r in c.execute("SELECT Name FROM SUBSTITUTE_GROUPS ORDER BY Name")]
                if active_tab == "Recipes":
                    names = [r[0] for r in c.execute("SELECT Name FROM ALIASES ORDER BY Name")]
                self.f_ing.w["p"]["values"] = names + groups

            # Pantry tab
            if active_tab in (None, "Pantry"):
                today = date.today()
                _WARN_DAYS = int(config.get("expiration_warning_days", 7))
                pantry_rows = []
                for r in get_inventory_rows(self.db_path):
                    exp = r.get("Expiration_Date")
                    tag = None
                    if exp:
                        try:
                            exp_date = date.fromisoformat(exp)
                            days_left = (exp_date - today).days
                            if days_left < 0:
                                tag = "expired"
                            elif days_left <= _WARN_DAYS:
                                tag = "expiring"
                        except ValueError:
                            pass
                    pantry_rows.append((
                        (r["Name"], f"{r['Total_Available_Weight']:g}", r["Unit"], r["Location"], exp or ""),
                        tag,
                        r["Row_ID"],
                    ))
                self.t_disp.fill(pantry_rows)

            # Shopping tab
            if active_tab in (None, "Shopping"):
                shopping = c.execute("SELECT Name, Missing_Quantity, Unit FROM SHOPPING_LIST_VIEW "
                                     "ORDER BY Name").fetchall()
                self.t_shopping.fill([((n, f"{q:.2f}", u), None) for n, q, u in shopping])

            # Recipes tab (cached unless invalidated)
            if active_tab in (None, "Recipes"):
                if self._cached_recipe_rows is None:
                    self._cached_recipe_rows = self.recipe_rows(c)
                self.t_ric.fill(self._cached_recipe_rows)

            # Profile tab
            if active_tab in (None, "Profile", "Profilo"):
                p = m.load_profile(c)
                has_profile = bool(p and p.get("Weight_kg") is not None and p.get("Sex") is not None)
                if not has_profile:
                    if hasattr(self, "profile_overlay"):
                        self.profile_overlay.place(relx=0, rely=0, relwidth=1.0, relheight=1.0)
                        tk.Misc.lift(self.profile_overlay)
                        self._draw_profile_overlay()
                    self.calc_m.set("calculated: -")
                    self.calc_k.set("calculated: -")
                    if hasattr(self, "bmi_label"):
                        self.bmi_label.configure(text="")
                    if hasattr(self, "profile_label"):
                        self.profile_label.configure(text="Target not defined")
                    if hasattr(self, "t_hist"):
                        self.t_hist.fill([])
                    if hasattr(self, "chart"):
                        self.chart.plot([], *theme.palette(self.theme))
                else:
                    if hasattr(self, "profile_overlay"):
                        self.profile_overlay.place_forget()
                    history = c.execute("SELECT Date, Weight_kg, Body_fat_pct, Waist_cm FROM WEIGHT_LOG "
                                        "ORDER BY Date DESC LIMIT 90").fetchall()
                    calc_m, calc_k, _ = estimates(p)
                    self.calc_m.set(f"calculated: {calc_m or '-'}")
                    self.calc_k.set(f"calculated: {calc_k or '-'}")
                    if p.get("Weight_kg") and p.get("Height_cm"):
                        value, category = bmi(p["Weight_kg"], p["Height_cm"])
                        self.bmi_label.configure(text=f"BMI {value:.1f} ({category})")
                    else:
                        self.bmi_label.configure(text="")
                    self.profile_label.configure(text=(f"Target in use: {p['Kcal_target']:.0f} kcal/day"
                                                       if p.get("Kcal_target") else "Target not defined (save the profile)"))
                    self.t_hist.fill([((d, f"{w:g}", "" if b is None else f"{b:g}", "" if x is None else f"{x:g}"), None)
                                      for d, w, b, x in history])
                    self.chart.plot([(d, w) for d, w, _, _ in reversed(history)], *theme.palette(self.theme))

            # Settings tab
            if active_tab in (None, "Setting", "Settings"):
                if hasattr(self, "v_settings_section"):
                    sec = self.v_settings_section.get()
                    if sec == "full_profile" and hasattr(self, "_refresh_full_profile_view"):
                        self._refresh_full_profile_view()
                    elif sec == "receipt_logs" and hasattr(self, "_refresh_receipt_logs_table"):
                        self._refresh_receipt_logs_table()

    @staticmethod
    def recipe_rows(c):
        products, names, inv = m.load_products(c), m.product_names(c), m.VirtualInventory.from_db(c)
        rows = []
        for recipe_id, name in c.execute("SELECT Recipe_ID, Name FROM RECIPES ORDER BY Name").fetchall():
            sim = m.simulate_recipe(c, inv.copy(), recipe_id, products)
            mac = m.recipe_macros(c, recipe_id, products, sim["group_choices"])
            rows.append(((name, f"{mac['Kcal']:.0f}", f"{mac['Protein']:.1f}", f"{mac['Carbs']:.1f}",
                           f"{mac['Fat']:.1f}", f"{mac['Fiber']:.1f}", f"{sim['open_coverage'] * 100:.0f}%",
                           m.outcome(sim, products, names)), None if sim["feasible"] else "ko"))
        return rows

    # ---------------- actions ----------------

    def switch_pantry_action(self, choice: str):
        self.v_pantry_action.set(choice)
        self.f_add.pack_forget()
        self.f_use.pack_forget()
        self.loc_mgr.pack_forget()
        if choice == "Add to inventory":
            self.f_add.pack(fill="x")
        elif choice == "Consume":
            self.f_use.pack(fill="x")
        elif choice == "Locations":
            self.loc_mgr.pack(fill="x")

    @safe
    def add(self):
        v = self.f_add.read()
        weight, num = number(v["weight"], "weight"), int(number(v["num"], "packages"))
        exp = v.get("exp", "").strip() or None
        try:
            add_item(v["name"], weight, num, v["location"], expiration_date=exp)
        except ProductNotFound:
            with db() as c:
                suggested = c.execute("SELECT COALESCE(MAX(ID), 0) + 1 FROM PRODUCTS").fetchone()[0]
            extra = ProductDialog(self, v["name"], suggested).result
            if extra is None:
                return
            add_item(v["name"], weight, num, v["location"], expiration_date=exp, **extra)
        self.msg(f"Added: {v['name']} ({num} x {weight:g}) in {v['location']}.")
        self.f_add.clear()
        self.refresh()

    @safe
    def consume(self):
        v = self.f_use.read()
        r = consume_quantity(v["name"], number(v["q"], "quantity"))
        text = f"Consumed {r['consumed']:g} of {v['name']}."
        if r["insufficient_availability"]:
            text += f" Insufficient availability: {r['shortfall']:g} missing, added to the shopping list."
        self.msg(text, r["insufficient_availability"])
        self.f_use.clear()
        self.refresh()

    @safe
    def export_inventory(self):
        path = filedialog.asksaveasfilename(
            title="Export inventory", defaultextension=".txt", initialfile=f"pantry_{date.today()}.txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if not path:
            return
        rows = get_available_inventory()
        with open(path, "w", encoding="utf-8") as out:
            out.write("# Product\tQuantity\tUnit\tLocation\n")
            out.writelines(f"{r['Name']}\t{r['Total_Available_Weight']:g}\t{r['Unit']}\t{r['Location']}\n"
                           for r in rows)
        self.msg(f"Exported {len(rows)} lines to {path}.")

    def _on_delete_inventory_row(self, row_iid: str):
        try:
            row_id = int(row_iid)
        except (ValueError, TypeError):
            return
        item = get_inventory_item(row_id, db_path=self.db_path)
        if not item:
            return
        item_name = item.get("Name", f"Item #{row_id}")
        confirm = messagebox.askyesno(
            "Delete Inventory Item",
            f"Are you sure you want to delete '{item_name}' (ID #{row_id}) from inventory?",
            parent=self,
        )
        if confirm:
            try:
                deleted = delete_inventory_item(row_id=row_id, db_path=self.db_path)
                if deleted:
                    self.msg(f"Deleted '{item_name}' from inventory.")
                    self.refresh()
                else:
                    self.msg(f"Could not delete '{item_name}': item not found.", error=True)
            except Exception as e:
                self.msg(f"Error deleting item: {e}", error=True)

    def _on_edit_inventory_row(self, row_iid: str):
        try:
            row_id = int(row_iid)
        except (ValueError, TypeError):
            return
        dlg = InventoryItemEditDialog(self, row_id=row_id, db_path=self.db_path)
        if dlg.result:
            self.msg("Inventory item updated successfully.")
            self.refresh()


    def plan_params(self):
        date_str = self.v_date.get().strip()
        try:
            date.fromisoformat(date_str)
        except ValueError:
            raise ValueError("Invalid date: use the YYYY-MM-DD format.")
        days = int(number(self.v_days.get(), "days"))
        if not 1 <= days <= 31:
            raise ValueError("Days must be between 1 and 31.")
        kcal = number(self.v_kcal.get(), "kcal") if self.v_kcal.get().strip() else None
        return date_str, days, kcal

    def write_text(self, text):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("end", text)
        self.text.configure(state="disabled")

    @safe
    def propose(self):
        date_str, days, kcal = self.plan_params()
        with db() as c:
            self.plans = pf.plan_week(c, date_str, days, kcal, self.fixed or None)
            products, names = m.load_products(c), m.product_names(c)
            summary = io.StringIO()
            with contextlib.redirect_stdout(summary):
                pf.print_week(c, self.plans)
        rows = []
        for p in self.plans:
            for v in p["meals"]:
                if v["Recipe_ID"] is None:
                    rows.append(((p["date_str"], v["meal"], "(skipped)", "", "skipped"), None))
                else:
                    ok = v["sim"]["feasible"]
                    rows.append(((p["date_str"], v["meal"], v["recipe"], f"{v['macro']['Kcal']:.0f}",
                                   "proposta: " + ("uses a leftover" if v["leftover"] else m.outcome(v["sim"], products, names))), None if ok else "ko"))
        self.t_plan.fill(rows)
        self.write_text(summary.getvalue())
        self.msg("Plan proposed (not saved yet).")

    @safe
    def save(self):
        if not self.plans:
            raise ValueError("Propose a plan first.")
        with db() as c:
            pf.save_week(c, self.plans)
        self.msg("Plan saved. Shopping list updated.")
        self.show_saved()
        self.refresh()

    def show_saved(self):
        date_str, days, _ = self.plan_params()
        with db() as c:
            rows = c.execute(
                "SELECT p.Date, p.Meal, COALESCE(r.Name, '(skipped)'), COALESCE(CAST(ROUND(v.Kcal) AS INTEGER), ''), "
                "p.Status FROM PLAN p LEFT JOIN COOKINGS k USING (Cooking_ID) "
                "LEFT JOIN RECIPES r ON r.Recipe_ID = k.Recipe_ID "
                "LEFT JOIN RECIPE_MACROS_VIEW v ON v.Recipe_ID = r.Recipe_ID "
                "WHERE p.Date >= ? AND p.Date < date(?, '+' || ? || ' day') "
                "ORDER BY p.Date, (SELECT Sort_Order FROM MEALS WHERE Name = p.Meal)", (date_str, date_str, days)).fetchall()
        self.t_plan.fill([(r, None) for r in rows])

    @safe
    def cooked(self):
        selection = self.t_plan.tv.selection()
        if not selection:
            raise ValueError("Select a meal in the table (after saving the plan).")
        date_str, meal = self.t_plan.tv.item(selection[0], "values")[:2]
        with db() as c:
            servings = m.load_profile(c)["Default_servings"]
        n = simpledialog.askinteger(
            "Cooked", f"How many servings are you cooking now for '{meal}' on {date_str}?\n"
                        "1 = this meal only; more = the others become leftovers for later meals.\n"
                        "(If the meal uses a ready leftover, keep 1.)",
            parent=self, minvalue=1, maxvalue=20, initialvalue=servings)
        if n is None:
            return
        with db() as c:
            pf.mark_cooked(c, date_str, meal, n)
        self.msg(f"Done: {meal} on {date_str} cooked, inventory updated"
                 + (f" ({n - 1} leftovers available)." if n > 1 else "."))
        self.show_saved()
        self.refresh()

    @safe
    def save_profile(self):
        v = {k: s.get().strip() for k, s in self.pv.items()}
        with db() as c:
            existing_p = m.load_profile(c)
        if not v.get("Sex"):
            if hasattr(self, "v_static_sex") and self.v_static_sex.get():
                v["Sex"] = self.v_static_sex.get()
            elif existing_p and existing_p.get("Sex"):
                v["Sex"] = existing_p["Sex"]
            if v.get("Sex"):
                self.pv["Sex"].set(v["Sex"])
        if not v.get("Height_cm"):
            if hasattr(self, "v_static_height") and self.v_static_height.get():
                v["Height_cm"] = self.v_static_height.get()
            elif existing_p and existing_p.get("Height_cm"):
                v["Height_cm"] = str(existing_p["Height_cm"])
            if v.get("Height_cm"):
                self.pv["Height_cm"].set(v["Height_cm"])

        if not (v.get("Sex") and v.get("Height_cm")):
            raise ValueError("Configure static profile metrics (Sex and Height) in Settings -> Full Profile first.")
        if not (v.get("Activity_Level") and v.get("Goal")):
            raise ValueError("Fill in activity level and goal.")
        data = {k: v[k] for k in ("Sex", "Activity_Level", "Goal")}
        for k in ("Weight_kg", "Height_cm", "Age_years", "Kcal_tolerance_pct", "Open_extra_pct",
                  "No_repeat_days", "Default_servings"):
            data[k] = number(v[k], k.replace("_", " "))
        for k in ("Age_years", "No_repeat_days", "Default_servings"):
            data[k] = int(data[k])
        for k in ("Body_fat_pct", "Waist_cm", "Target_weight_kg", "Weekly_rate_kg", "Maintenance_manual",
                  "Kcal_manual"):
            data[k] = number(v[k], k.replace("_", " ")) if v[k] else None
        data.update({k: int(var.get()) for k, var in self.pu.items()})
        with db() as c:
            set_meal_shares(c, {n: number(var.get(), n) for n, var in self.pm.items()})
            kcal = update_profile(c, data)
            c.commit()
        warning = (data["Goal"] != "maintenance" and (data["Weekly_rate_kg"] or 0) > 1)
        self.msg(f"Profile saved: target {kcal:.0f} kcal/day."
                 + (" Warning: a rate above 1 kg/week is aggressive." if warning else ""), warning)
        self.refresh()

    @safe
    def add_ingredient(self):
        v = self.f_ing.read()
        if not v["p"] or not v["u"]:
            raise ValueError("Specify product (or group), quantity and unit.")
        self.draft.append((v["p"], number(v["q"], "quantity"), v["u"]))
        self.f_ing.clear()
        self.show_draft()

    def show_draft(self):
        self.t_draft.fill([((p, f"{q:g}", u), None) for p, q, u in self.draft])

    @safe
    def remove_ingredient(self):
        selection = self.t_draft.tv.selection()
        if selection:
            del self.draft[self.t_draft.tv.index(selection[0])]
            self.show_draft()

    @safe
    def save_recipe(self):
        name = self.v_rnome.get().strip()
        meals = [n for n, var in self.v_meals.items() if var.get()]
        with db() as c:
            outcome = rc.create_recipe(c, name, meals, self.draft)
        self.draft = []
        self.v_rnome.set("")
        for var in self.v_meals.values():
            var.set(False)
        self.show_draft()
        warning = " Warning: " + outcome["warnings"][0] if outcome["warnings"] else ""
        self.msg(f"Recipe '{name}' saved." + warning, bool(warning))
        self.refresh()

    def open_add_recipe_dialog(self, wait: bool = True):
        """Opens AddRecipeDialog modal for direct recipe creation."""
        db_path = getattr(self, "db_path", None)
        if not db_path and "MEAL_PLANNER_DB" in os.environ:
            db_path = os.environ["MEAL_PLANNER_DB"]
        dlg = AddRecipeDialog(
            self,
            db_path=db_path,
            wait=wait,
            on_save_callback=self._on_recipe_dialog_saved,
        )
        return dlg

    def add_recipe(self, wait: bool = True):
        """Alias for open_add_recipe_dialog."""
        return self.open_add_recipe_dialog(wait=wait)

    def _on_recipe_dialog_saved(self, outcome):
        """Callback invoked when a new recipe is saved through AddRecipeDialog."""
        name = outcome.get("Name", "Recipe")
        warning = (" Warning: " + outcome["warnings"][0]) if outcome.get("warnings") else ""
        self.msg(f"Recipe '{name}' saved." + warning, bool(warning))
        self._cached_recipe_rows = None
        self.refresh(active_tab="Recipes")

    @safe
    def import_recipes(self):
        path = filedialog.askopenfilename(title="Import recipes",
                                              filetypes=[("Recipes", "*.json *.csv"), ("All files", "*.*")])
        if not path:
            return
        with db() as c:
            created, errors = rc.import_file(c, path)
        self.msg(f"Imported {created} recipes" + (f", {len(errors)} skipped." if errors else "."), bool(errors))
        if errors:
            messagebox.showwarning("Skipped recipes", "\n".join(errors[:15]))
        self.refresh()

    @safe
    def delete_recipe(self):
        selection = self.t_ric.tv.selection()
        if not selection:
            raise ValueError("Select a recipe in the table.")
        name = self.t_ric.tv.item(selection[0], "values")[0]
        if not messagebox.askyesno("Delete", f"Delete the recipe '{name}'?"):
            return
        with db() as c:
            rc.delete_recipe(c, name)
        self.msg(f"Recipe '{name}' deleted.")
        self.refresh()

    def selected_meal(self):
        selection = self.t_plan.tv.selection()
        if not selection:
            raise ValueError("Select a meal in the table.")
        return self.t_plan.tv.item(selection[0], "values")[:2]

    @safe
    def lock(self):
        date_str, meal = self.selected_meal()
        with db() as c:
            names = [r[0] for r in c.execute("SELECT r.Name FROM RECIPES r JOIN RECIPE_MEALS p USING (Recipe_ID) "
                                            "WHERE p.Meal = ? ORDER BY r.Name", (meal,))]
        if not names:
            raise ValueError(f"No recipe allowed for {meal}.")
        choice = ChoiceDialog(self, f"Recipe for {meal} on {date_str}", names).result
        if choice is None:
            return
        with db() as c:
            self.fixed.setdefault(date_str, {})[meal] = m.recipe_id_from_name(c, choice)
        self.propose()
        self.msg(f"{meal} on {date_str} fixed to '{choice}': save the plan to confirm it.")

    @safe
    def skip(self):
        date_str, meal = self.selected_meal()
        self.fixed.setdefault(date_str, {})[meal] = None
        self.propose()
        self.msg(f"{meal} on {date_str} skipped: save the plan to record it.")

    @safe
    def unlock(self):
        self.fixed = {}
        self.propose()

    @safe
    def open_meal_prep_dialog(self):
        with db() as c:
            recipes = c.execute("SELECT Recipe_ID, Name FROM RECIPES ORDER BY Name").fetchall()
        if not recipes:
            messagebox.showinfo("No Recipes", "No recipes found in database to schedule.", parent=self)
            return
        dlg = MealPrepDialog(self, recipes, default_cook_date=self.v_date.get() or date.today().isoformat())
        if dlg.result:
            self.schedule_prep(
                recipe_id=dlg.result["recipe_id"],
                cook_date=dlg.result["cook_date"],
                target_dates=dlg.result["target_dates"]
            )

    @safe
    def schedule_prep(self, recipe_id: int, cook_date: str, target_dates: list):
        """Schedules a meal prep cooking for the given recipe and dates."""
        with db() as c:
            cooking_id = schedule_meal_prep(c, recipe_id, cook_date, target_dates)
        messagebox.showinfo("Meal Prep Scheduled",
                            f"Meal prep successfully scheduled (Cooking ID: {cooking_id})!\n"
                            f"Cook date: {cook_date}\n"
                            f"Meals planned on: {', '.join(target_dates)}",
                            parent=self)
        self.refresh()

    # ---------------- location manager ----------------

    @safe
    def loc_add(self):
        name = simpledialog.askstring("New location", "Location name:", parent=self)
        if name and name.strip():
            add_location(name.strip())
            self.refresh()
            self.msg(f"Location '{name.strip()}' added.")

    @safe
    def loc_rename(self):
        old = self.v_loc_sel.get()
        if not old:
            raise ValueError("Select a location from the list first.")
        new = simpledialog.askstring("Rename location", f"New name for '{old}':", parent=self)
        if new and new.strip():
            rename_location(old, new.strip())
            self.refresh()
            self.msg(f"Location '{old}' renamed to '{new.strip()}'.")

    @safe
    def loc_delete(self):
        name = self.v_loc_sel.get()
        if not name:
            raise ValueError("Select a location from the list first.")
        if messagebox.askyesno("Delete location", f"Delete location '{name}'?"):
            delete_location(name)
            self.v_loc_sel.set("")
            self.refresh()
            self.msg(f"Location '{name}' deleted.")

    # ---------------- receipt actions ----------------

    def _receipt_log_write(self, text: str):
        if not hasattr(self, "_receipt_log_text"):
            self._receipt_log_text = ""
        self._receipt_log_text += text

    def _on_receipt_drop(self, event):
        raw = (event.data or "").strip()
        if not raw:
            return
        if raw.startswith("{") and raw.endswith("}") and raw.count("{") == 1:
            raw = raw[1:-1]
        elif "{" in raw:
            raw = raw.split("}")[0].replace("{", "")
        else:
            raw = raw.split()[0]
        path = raw.strip()
        if Path(path).suffix.lower() in _RECEIPT_EXTS:
            self._process_receipt(path)
        else:
            self._receipt_log_write(f"[SKIPPED] Unsupported file type: {Path(path).name}\n")

    @safe
    def receipt_browse(self):
        ext_str = " ".join(f"*{e}" for e in sorted(_RECEIPT_EXTS))
        path = filedialog.askopenfilename(
            title="Select receipt image",
            filetypes=[("Receipt images", ext_str), ("All files", "*.*")]
        )
        if path:
            self._process_receipt(path)

    def _process_receipt(self, path: str):
        from datetime import datetime
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if hasattr(self, "_last_receipt_var"):
            self._last_receipt_var.set(f"Last receipt loaded: {now_str}")
        self._receipt_status.set("Reading receipt (OCR running)...")
        self.msg(f"Processing receipt: {Path(path).name}...")
        self._receipt_log_write(f"[{now_str}] File: {Path(path).name}\n")
        self._receipt_log_write("  -> Running OCR analysis...\n")

        def _worker():
            try:
                from meal_planner import receipts as rp
                voci = rp.leggi_voci(path)
                self.after(0, lambda: _done(voci, None))
            except Exception as exc:
                self.after(0, lambda: _done([], exc))

        def _done(voci, error):
            self._receipt_status.set("")
            if error:
                self.msg(f"OCR error: {error}", error=True)
                self._receipt_log_write(f"  [ERROR] {error}\n\n")
                messagebox.showerror("Receipt Error", str(error))
                return
            self.msg(f"Receipt parsed: {len(voci)} items found.")
            self._receipt_log_write(f"  -> Successfully recognized {len(voci)} item(s).\n")
            self._receipt_log_write("  -> Opening review modal...\n\n")
            self._open_receipt_review(path, voci)

        threading.Thread(target=_worker, daemon=True).start()

    def _open_receipt_review(self, path: str, voci):
        from datetime import datetime
        from meal_planner import receipts as rp

        out_dir_cfg = config.get("receipts_output_dir", "receipts_output")
        out_dir = Path(out_dir_cfg)
        if not out_dir.is_absolute():
            out_dir = Path(__file__).resolve().parents[2] / out_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"receipt_{datetime.now():%Y%m%d_%H%M%S}.txt"

        top = tk.Toplevel(self)
        top.transient(self)
        top.title(f"receipt - {Path(path).name}")
        rp.FinestraModifica(
            top, voci, out_path, f"receipt - {Path(path).name}",
            on_save=lambda items: self.after(50, lambda: self._on_receipt_review_saved(items, out_path))
        )

    def _on_receipt_review_saved(self, items, out_path=None):
        if not items:
            return
        db_path = getattr(self, "db_path", None)
        self._receipt_log_write(f"  -> Raw items confirmed. Opening reconciliation dialog for {len(items)} item(s)...\n")
        try:
            dlg = ReceiptReconciliationDialog(self, items, db_path=db_path)
        except Exception as exc:
            self.msg(f"Error opening reconciliation: {exc}", error=True)
            self._receipt_log_write(f"  [ERROR] {exc}\n")
            return

        if not dlg.result:
            self._receipt_log_write("  -> Reconciliation cancelled by user. No items imported.\n\n")
            return

        imported_count = 0
        new_aliases = 0
        new_products = 0

        for it in dlg.result:
            kwargs = {
                "name": it["name"],
                "unit_weight": it["unit_weight"],
                "intact_items": it["intact_items"],
                "location": it["location"],
                "expiration_date": it.get("expiration_date"),
            }
            if db_path:
                kwargs["db_path"] = db_path
            if it.get("fallback_id") is not None:
                kwargs["fallback_id"] = it["fallback_id"]
            if it.get("new_product_unit") is not None:
                kwargs["new_product_unit"] = it["new_product_unit"]
            if it.get("new_product_macros") is not None:
                kwargs["new_product_macros"] = it["new_product_macros"]

            try:
                outcome = add_item(**kwargs)
                imported_count += 1
                case = outcome.get("case") if isinstance(outcome, dict) else None
                if case == "B2" or it.get("is_new_product"):
                    new_products += 1
                elif case == "B1":
                    new_aliases += 1
            except Exception as e:
                self._receipt_log_write(f"  [ERROR] Failed to add '{it['name']}': {e}\n")

        from datetime import datetime
        now = datetime.now()
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
        time_str = now.strftime("%H:%M:%S")
        day_str = now.strftime("%Y-%m-%d")
        file_name = Path(out_path).name if out_path else f"receipt_{now.strftime('%Y%m%d_%H%M%S')}.txt"
        try:
            with db() as c:
                log_receipt(time_str, day_str, file_name, conn=c)
        except Exception:
            pass

        if hasattr(self, "_last_receipt_var"):
            self._last_receipt_var.set(f"Last receipt loaded: {now_str}")

        self.refresh()
        self._receipt_log_write(
            f"  -> Successfully imported {imported_count} item(s) into inventory "
            f"({new_products} new product(s), {new_aliases} new alias(es)).\n\n"
        )
        self.msg(f"Imported {imported_count} receipt item(s) into inventory.")

    def _check_receipt_aliases(self, voci, out_path=None):
        """Checks for receipt items not present in ALIASES and opens AliasMapDialog."""
        raw_names = []
        if out_path and Path(out_path).exists():
            try:
                content = Path(out_path).read_text(encoding="utf-8")
                for line in content.splitlines():
                    parts = line.strip().split(";")
                    if parts and parts[0].strip():
                        raw_names.append(parts[0].strip())
            except Exception:
                pass

        if not raw_names and voci:
            for v in voci:
                name = v.nome if hasattr(v, "nome") else (v.get("nome") if isinstance(v, dict) else str(v))
                if name and name.strip():
                    raw_names.append(name.strip())

        with db() as c:
            alias_rows = c.execute("SELECT Name, ID FROM ALIASES").fetchall()
        known_map = {name.lower(): (name, pid) for name, pid in alias_rows}
        known_names = sorted(list({name for name, _ in alias_rows}))

        unrecognized = []
        for name in raw_names:
            if name.lower() not in known_map and name not in unrecognized:
                unrecognized.append(name)

        if not unrecognized:
            return

        try:
            dlg = AliasMapDialog(self, unrecognized, known_names)
        except Exception:
            return

        if not dlg.result:
            return

        mapped_count = 0
        canonical_to_id = {name: pid for name, pid in alias_rows}
        with db() as c:
            for label, canonical in dlg.result.items():
                if canonical and canonical in canonical_to_id:
                    target_id = canonical_to_id[canonical]
                    c.execute("INSERT OR IGNORE INTO ALIASES (Name, ID) VALUES (?, ?)", (label, target_id))
                    mapped_count += 1
                    self._receipt_log_write(f"  -> Mapped alias '{label}' -> '{canonical}' (ID {target_id})\n")
            c.commit()

        if mapped_count > 0:
            self.refresh()
            self.msg(f"Mapped {mapped_count} receipt alias(es) to catalog products.")


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
