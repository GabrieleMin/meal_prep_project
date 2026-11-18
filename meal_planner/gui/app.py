"""Main window."""

from datetime import date, datetime
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
import contextlib
import io
import os
import threading
import tkinter as tk
from typing import Optional

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    BaseTk, HAS_DND = TkinterDnD.Tk, True
except (ImportError, Exception):
    BaseTk, HAS_DND = tk.Tk, False

_RECEIPT_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}

from meal_planner import config, engine as m, planner as pf, recipes as rc
from meal_planner.db.schema import migrate
from meal_planner.engine import bmi, estimates, reset_profile, set_meal_shares, update_profile
from meal_planner.gui import theme
from meal_planner.gui.dialogs import (
    AddRecipeDialog, AliasMapDialog, ChoiceDialog, InventoryItemEditDialog,
    MealPrepDialog, ProductDialog, ProfileSetupDialog, ReceiptReconciliationDialog,
    RecipeDetailsDialog, RecipeGeneratorDialog,
)
from meal_planner.gui.support import GREEN, RED, db, number, safe
from meal_planner.gui.widgets import Form, LineChart, Table
from meal_planner.inventory import (
    ProductNotFound, add_item, add_location, clear_inventory, consume_quantity,
    delete_inventory_item, delete_location, get_available_inventory, get_exclusion_list,
    get_inventory_item, get_inventory_rows, get_locations, get_receipt_logs,
    log_receipt, rename_location,
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
            migrate(c)
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
                             ("Profile", self.tab_profile), ("Settings", self.tab_settings)):
            tab = ttk.Frame(self.nb, padding=10)
            self.nb.add(tab, text=title)
            build(tab)
        self.nb.select(0)
        self.bar = ttk.Label(self, textvariable=self.status, anchor="w", padding=6)
        self.bar.pack(fill="x")
        self.nb.bind("<<NotebookTabChanged>>", lambda e: self.on_tab_changed())
        with db() as c:
            self.load_profile_form(m.load_profile(c))
        self.refresh()
        self.set_theme(self.theme)
        self.nb.select(0)
        self._startup_alert_timer = self.after(200, self._startup_expiration_alert)

    def _resolve_db_path(self) -> Optional[str]:
        return getattr(self, "db_path", None) or os.environ.get("MEAL_PLANNER_DB")

    def _select_tab(self, *names) -> bool:
        if not hasattr(self, "nb"):
            return False
        for i in range(self.nb.index("end")):
            if self.nb.tab(i, "text") in names:
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
                return True
        return False

    def _set_static_vars(self, sex: str = "", height: str = ""):
        if hasattr(self, "v_static_sex"):
            self.v_static_sex.set(sex)
        if hasattr(self, "v_static_height"):
            self.v_static_height.set(height)

    def destroy(self):
        if getattr(self, "_startup_alert_timer", None):
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
            lines = "\n".join(
                f"  • {it['Name']} — expired {it['Expiration_Date']} ({it['Location']})" for it in expired
            )
            messagebox.showwarning(
                "Expired Items",
                f"⚠️  {len(expired)} expired item(s) found in your pantry:\n\n{lines}\n\n"
                "These items are excluded from meal planning.\nPlease discard or consume them.",
                parent=self,
            )
        except Exception:
            pass

    def set_theme(self, name):
        self.theme = name
        theme.apply(name)
        for w in (self.text, getattr(self, "_receipt_log", None), getattr(self, "lbl_full_overview_text", None)):
            if isinstance(w, tk.Text):
                theme.style_text(w, name)
        if hasattr(self, "_drop_zone"):
            bg, fg = theme.palette(name)
            self._drop_zone.configure(background=bg)
            if hasattr(self, "_drop_label_id"):
                self._drop_zone.itemconfig(self._drop_label_id, fill=fg)
        if hasattr(self, "t_disp") and hasattr(self.t_disp, "update_delete_icon"):
            self.t_disp.update_delete_icon(is_dark=(name == "dark"))
        if hasattr(self, "profile_overlay") and self.profile_overlay.winfo_ismapped():
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
            18, show_delete=True, on_delete=self._on_delete_inventory_row,
            on_select=self._on_edit_inventory_row, on_double_click=self._on_edit_inventory_row,
            is_dark=(self.theme == "dark"),
        )
        self.t_disp.grid(row=0, column=1, sticky="nsew")
        ttk.Button(f, text="Export to .txt", command=self.export_inventory).grid(row=1, column=1, sticky="e", pady=(8, 0))
        side = ttk.Frame(f)
        side.grid(row=0, column=0, sticky="n", padx=(0, 12))

        sel_frame = ttk.LabelFrame(side, text="Pantry actions", padding=6)
        sel_frame.pack(fill="x", pady=(0, 6))
        ttk.Label(sel_frame, text="Select an action to show its form:", font=("TkDefaultFont", 8, "italic"), anchor="w").pack(fill="x", pady=(0, 4))

        btn_grid = ttk.Frame(sel_frame)
        btn_grid.pack(fill="x")
        self.v_pantry_action = tk.StringVar(value="Add to inventory")
        for col, (attr, label, action, pad) in enumerate((
            ("btn_act_add", "Add", "Add to inventory", (0, 2)),
            ("btn_act_use", "Consume", "Consume", 2),
            ("btn_act_loc", "Locations", "Locations", (2, 0)),
        )):
            btn_grid.columnconfigure(col, weight=1, uniform="act_btn")
            btn = ttk.Button(btn_grid, text=label, command=lambda a=action: self.switch_pantry_action(a))
            btn.grid(row=0, column=col, sticky="ew", padx=pad)
            setattr(self, attr, btn)

        action_container = ttk.Frame(side)
        action_container.pack(fill="x", pady=(0, 6))
        self.f_add = Form(action_container, "Add to inventory",
                          [("name", "Product", []), ("weight", "Package weight/volume", None),
                           ("num", "No. of packages", None), ("location", "Location", []),
                           ("exp", "Expiry date (YYYY-MM-DD)", None)], "Add", self.add)
        self.f_use = Form(action_container, "Consume", [("name", "Product", []), ("q", "Quantity", None)],
                          "Consume", self.consume)

        self.loc_mgr = ttk.LabelFrame(action_container, text="Locations", padding=6)
        self.v_loc_sel = tk.StringVar()
        self.cb_loc = ttk.Combobox(self.loc_mgr, textvariable=self.v_loc_sel, width=22, state="readonly")
        self.cb_loc.pack(fill="x")
        btn_row = ttk.Frame(self.loc_mgr)
        btn_row.pack(fill="x", pady=(4, 0))
        for text, cmd, px in (("Add", self.loc_add, 0), ("Rename", self.loc_rename, 4), ("Delete", self.loc_delete, 0)):
            ttk.Button(btn_row, text=text, command=cmd).pack(side="left", padx=px)

        self._pantry_forms = {"Add to inventory": self.f_add, "Consume": self.f_use, "Locations": self.loc_mgr}
        self.switch_pantry_action("Add to inventory")

        rec_mgr = ttk.LabelFrame(side, text="Receipts", padding=6)
        rec_mgr.pack(fill="x")
        drop_h = 70
        bg, fg = theme.palette(self.theme)
        self._drop_zone = tk.Canvas(
            rec_mgr, height=drop_h, highlightthickness=2,
            highlightbackground="#3b82f6" if HAS_DND else "#888888", background=bg, cursor="hand2",
        )
        self._drop_zone.pack(fill="x", pady=(0, 4))
        self._drop_label_id = self._drop_zone.create_text(
            120, drop_h // 2 - 10, text="⬇  Drag receipts here  ⬇", font=("TkDefaultFont", 10, "bold"), anchor="center", fill=fg,
        )
        self._drop_hint_id = self._drop_zone.create_text(
            120, drop_h // 2 + 12, text="or click to browse" if HAS_DND else "(click to browse image)",
            font=("TkDefaultFont", 8), anchor="center", fill="#888888",
        )
        if HAS_DND:
            try:
                self._drop_zone.drop_target_register(DND_FILES)
                self._drop_zone.dnd_bind("<<Drop>>", self._on_receipt_drop)
            except Exception:
                pass

        def _recenter(e):
            w = e.width if e.width > 1 else 160
            self._drop_zone.coords(self._drop_label_id, w // 2, drop_h // 2 - 10)
            if hasattr(self, "_drop_hint_id"):
                self._drop_zone.coords(self._drop_hint_id, w // 2, drop_h // 2 + 12)

        self._drop_zone.bind("<Configure>", _recenter)
        self._drop_zone.bind("<Button-1>", lambda e: self.receipt_browse())

        browse_bar = ttk.Frame(rec_mgr)
        browse_bar.pack(fill="x", pady=(0, 4))
        ttk.Button(browse_bar, text="Browse files...", command=self.receipt_browse).pack(side="left")
        self._receipt_status = tk.StringVar(value="")
        ttk.Label(browse_bar, textvariable=self._receipt_status, font=("TkDefaultFont", 8), anchor="w").pack(side="left", padx=(6, 0))

        self._last_receipt_var = tk.StringVar(value="Last receipt loaded: None")
        self._receipt_log_text = ""
        self.lbl_last_receipt = ttk.Label(
            rec_mgr, textvariable=self._last_receipt_var, font=("TkDefaultFont", 8, "italic"), anchor="w", padding=(2, 4),
        )
        self.lbl_last_receipt.pack(fill="x", pady=(2, 0))

        class _ReceiptLogProxy:
            def __init__(self, outer):
                self._outer = outer
            def insert(self, *args): pass
            def see(self, *args): pass
            def configure(self, **kwargs): pass
            def get(self, *args): return self._outer._receipt_log_text

        self._receipt_log = _ReceiptLogProxy(self)

    def tab_shopping(self, f):
        ttk.Label(f, text="What is missing: consumption beyond availability and plan ingredients.").pack(anchor="w")
        self.t_shopping = Table(f, [("n", "Product", 320), ("q", "Missing quantity", 140), ("u", "Unit", 80)], 20)
        self.t_shopping.pack(fill="both", expand=True, pady=8)

    def tab_recipes(self, f):
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)
        header_bar = ttk.Frame(f)
        header_bar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(
            header_bar,
            text="Macronutrients per serving and feasibility with current inventory "
                 "('Open' = share from open packages). Double-click for details.",
            font=("TkDefaultFont", 9),
        ).pack(side="left")

        self.t_ric = Table(
            f,
            [("r", "Recipe", 240), ("k", "kcal", 65), ("p", "Prot", 65), ("c", "Carb", 65),
             ("g", "Fat", 65), ("fi", "Fiber", 65), ("a", "Open", 65), ("e", "Feasibility / Outcome", 320)],
            18, on_double_click=self._on_recipe_row_double_click,
        )
        self.t_ric.grid(row=1, column=0, sticky="nsew", pady=(0, 8))

        bar = ttk.Frame(f)
        bar.grid(row=2, column=0, sticky="ew")
        for idx, (attr, text, cmd) in enumerate((
            ("btn_add_recipe", "➕ New Recipe...", self.open_add_recipe_dialog),
            ("btn_generate_recipe", "✨ Generate Recipe...", self.open_recipe_generator_dialog),
            ("btn_details_recipe", "👁️ Recipe Details...", self.open_recipe_details),
            ("btn_import_recipes", "📂 Import from file (.json / .csv)...", self.import_recipes),
            ("btn_delete_recipe", "🗑️ Delete Selected Recipe", self.delete_recipe),
        )):
            btn = ttk.Button(bar, text=text, command=cmd)
            btn.pack(side="left", padx=(0, 6) if idx == 0 else 6)
            setattr(self, attr, btn)

        self.v_rnome = tk.StringVar()
        self.v_meals = {}

    def _on_recipe_row_double_click(self, row_id):
        """Handles double click on recipe table row by selecting and opening details."""
        if not row_id:
            return None
        self.t_ric.tv.selection_set(row_id)
        vals = self.t_ric.tv.item(row_id, "values")
        return self.open_recipe_details(recipe_name=vals[0], wait=False) if vals else None

    def tab_plan(self, f):
        top = ttk.Frame(f)
        top.pack(fill="x")
        self.v_date, self.v_days, self.v_kcal = tk.StringVar(value=date.today().isoformat()), tk.StringVar(value="7"), tk.StringVar()
        for label, var, width in (("Start date", self.v_date, 12), ("Days", self.v_days, 4), ("Kcal (empty = profile)", self.v_kcal, 8)):
            ttk.Label(top, text=label).pack(side="left", padx=(0, 4))
            ttk.Entry(top, textvariable=var, width=width).pack(side="left", padx=(0, 12))
        for text, command in (("Propose plan", self.propose), ("Save plan", self.save), ("Mark as cooked", self.cooked)):
            ttk.Button(top, text=text, command=command).pack(side="left", padx=3)
        middle = ttk.Frame(f)
        middle.pack(fill="x", pady=(6, 0))
        ttk.Label(middle, text="On the selected meal:").pack(side="left", padx=(0, 6))
        for text, command in (("Lock recipe...", self.lock), ("Skip meal", self.skip), ("Unlock all", self.unlock)):
            ttk.Button(middle, text=text, command=command).pack(side="left", padx=3)
        ttk.Button(middle, text="Meal Prep...", command=self.open_meal_prep_dialog).pack(side="left", padx=(12, 3))
        self.t_plan = Table(f, [("d", "Date", 95), ("p", "Meal", 90), ("r", "Recipe", 290), ("k", "kcal", 60), ("s", "Status", 360)], 9)
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
            self.pm = {n: tk.StringVar(value=f"{s:g}") for n, s in c.execute("SELECT Name, Energy_Share_pct FROM MEALS ORDER BY Sort_Order")}

        def box(title, col, row):
            b = ttk.LabelFrame(f, text=title, padding=10)
            b.grid(row=row, column=col, sticky="nw", padx=(0, 12), pady=(0, 10))
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

        body = box("Body", 0, 0)
        for label, key in (("Weight (kg)", "Weight_kg"), ("Age (years)", "Age_years"), ("Body fat (%)", "Body_fat_pct"), ("Waist (cm)", "Waist_cm")):
            field(body, label, self.pv[key])
        self.bmi_label = ttk.Label(body)
        self.bmi_label.grid(row=body.grid_size()[1], column=0, columnspan=2, sticky="w", pady=(6, 0))

        settings = box("Planner settings", 0, 1)
        for label, key in (("Kcal tolerance (%)", "Kcal_tolerance_pct"), ("Open-package extra (%)", "Open_extra_pct"),
                           ("No-repeat days", "No_repeat_days"), ("Default servings per cooking", "Default_servings")):
            field(settings, label, self.pv[key])

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

        ttk.Button(f, text="Save profile", command=self.save_profile).grid(row=2, column=0, columnspan=2, sticky="ew", padx=(0, 12))

        progress = ttk.LabelFrame(f, text="Progress", padding=10)
        progress.grid(row=0, column=2, rowspan=3, sticky="nsew")
        f.columnconfigure(2, weight=1)
        f.rowconfigure(0, weight=1)
        self.t_hist = Table(progress, [("d", "Date", 90), ("w", "Weight", 70), ("b", "Body fat %", 80), ("c", "Waist", 70)], 6)
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
        """Draws the rounded card and empty-state prompt on the profile overlay canvas."""
        c = self.profile_overlay
        c.delete("all")
        for child in c.winfo_children():
            child.destroy()
        w, h = c.winfo_width(), c.winfo_height()
        if w <= 1 or h <= 1:
            ref = getattr(self, "tab_profile_frame", c)
            w, h = max(ref.winfo_width(), 700), max(ref.winfo_height(), 500)

        bg, fill, outline, fg, sub_fg = (
            ("#1e222b", "#282c37", "#3f4656", "#f9fafb", "#9ca3af")
            if getattr(self, "theme", "dark") == "dark"
            else ("#f3f4f6", "#ffffff", "#cbd5e1", "#111827", "#6b7280")
        )
        c.configure(bg=bg)

        cw, ch, r = min(560, max(360, w - 80)), min(280, max(200, h - 80)), 22
        x1, y1 = (w - cw) / 2, (h - ch) / 2
        x2, y2 = x1 + cw, y1 + ch
        points = [
            x1 + r, y1, x1 + r, y1, x2 - r, y1, x2 - r, y1, x2, y1,
            x2, y1 + r, x2, y1 + r, x2, y2 - r, x2, y2 - r, x2, y2,
            x2 - r, y2, x2 - r, y2, x1 + r, y2, x1 + r, y2, x1, y2,
            x1, y2 - r, x1, y2 - r, x1, y1 + r, x1, y1 + r, x1, y1,
        ]
        c.create_polygon(points, fill=fill, outline=outline, width=2, smooth=True, tags=("overlay_card",))
        c.create_text((x1 + x2) / 2, y1 + ch * 0.35, text="Hello, add your profile to get started",
                      fill=fg, font=("TkDefaultFont", 15, "bold"), justify="center", tags=("overlay_text", "overlay_card"))
        c.create_text((x1 + x2) / 2, y1 + ch * 0.54,
                      text="Set up your profile to calculate caloric needs and start planning meals.",
                      fill=sub_fg, font=("TkDefaultFont", 10), justify="center", width=cw - 60, tags=("overlay_card",))
        btn = ttk.Button(c, text="➕ Add Profile", command=self.open_profile_setup_dialog)
        c.create_window((x1 + x2) / 2, y1 + ch * 0.76, window=btn, tags=("overlay_btn",))
        c.tag_bind("overlay_card", "<Button-1>", lambda e: self.open_profile_setup_dialog())
        c.tag_bind("overlay_card", "<Enter>", lambda e: c.config(cursor="hand2"))
        c.tag_bind("overlay_card", "<Leave>", lambda e: c.config(cursor=""))

    def open_profile_setup_dialog(self):
        """Interactive modal dialog allowing user to configure their profile when overlay is shown."""
        dialog = ProfileSetupDialog(self, db_path=self._resolve_db_path())
        if dialog.result:
            with db() as c:
                self.load_profile_form(m.load_profile(c))
            self.refresh(active_tab="Profile")

    # ---------------- Setting tab ----------------

    def tab_settings(self, f):
        """Settings tab with non-monolithic section selection controller."""
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)

        nav_frame = ttk.LabelFrame(f, text="Settings Sections", padding=6)
        nav_frame.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.v_settings_section = tk.StringVar(value="style_and_config")
        self.settings_container = ttk.Frame(f)
        self.settings_container.grid(row=1, column=0, sticky="nsew")
        self._sec_frames = {}

        specs = (
            ("style", "style_and_config", "Style & Config", (0, 2), self._build_sec_style_and_config),
            ("profile", "full_profile", "Full Profile", 2, self._build_sec_full_profile),
            ("receipts", "receipt_logs", "Receipt Logs", 2, self._build_sec_receipt_logs),
            ("destructive", "destructive_actions", "Destructive Actions", (2, 0), self._build_sec_destructive_actions),
        )
        for col, (short, key, label, pad, builder) in enumerate(specs):
            nav_frame.columnconfigure(col, weight=1, uniform="sec_btn")
            btn = ttk.Button(nav_frame, text=label, command=lambda s=key: self.switch_settings_section(s))
            btn.grid(row=0, column=col, sticky="ew", padx=pad)
            setattr(self, f"btn_sec_{short}", btn)
            sec_frame = ttk.Frame(self.settings_container)
            setattr(self, f"f_sec_{short}", sec_frame)
            self._sec_frames[key] = sec_frame
            builder(sec_frame)

        self.switch_settings_section("style_and_config", select_tab=False)

    def switch_settings_section(self, section: str, select_tab: bool = True):
        """Switches active section in Settings tab."""
        if select_tab:
            self._select_tab("Setting", "Settings")
        self.v_settings_section.set(section)
        for fr in self._sec_frames.values():
            fr.pack_forget()
        target = self._sec_frames.get(section)
        if target is not None:
            target.pack(fill="both", expand=True)
        if section == "full_profile":
            self._refresh_full_profile_view()
        elif section == "receipt_logs":
            self._refresh_receipt_logs_table()

    def _build_sec_style_and_config(self, parent):
        box = ttk.LabelFrame(parent, text="Style & Configuration Settings", padding=14)
        box.pack(fill="x", padx=4, pady=4)

        ttk.Label(box, text="Default Startup Theme:").grid(row=0, column=0, sticky="w", pady=6)
        self.v_cfg_theme = tk.StringVar(value=config.get("default_theme", "dark"))
        ttk.Combobox(box, textvariable=self.v_cfg_theme, values=["dark", "light"], state="readonly", width=12).grid(
            row=0, column=1, sticky="w", padx=(10, 0), pady=6)

        ttk.Label(box, text="Expiration warning alert (days):").grid(row=1, column=0, sticky="w", pady=6)
        self.v_cfg_warn_days = tk.StringVar(value=str(config.get("expiration_warning_days", 7)))
        ttk.Entry(box, textvariable=self.v_cfg_warn_days, width=12).grid(row=1, column=1, sticky="w", padx=(10, 0), pady=6)

        ttk.Label(box, text="Receipts output directory:").grid(row=2, column=0, sticky="w", pady=6)
        self.v_cfg_receipts_dir = tk.StringVar(value=str(config.get("receipts_output_dir", "receipts_output")))
        dir_row = ttk.Frame(box)
        dir_row.grid(row=2, column=1, sticky="w", padx=(10, 0), pady=6)
        ttk.Entry(dir_row, textvariable=self.v_cfg_receipts_dir, width=28).pack(side="left")
        ttk.Button(dir_row, text="Browse...", command=self._browse_receipts_dir).pack(side="left", padx=(6, 0))

        ttk.Button(box, text="Save Configuration", command=self.save_settings_config).grid(row=3, column=0, columnspan=2, sticky="w", pady=(12, 0))

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
            "default_theme": theme_val, "theme": theme_val,
            "expiration_warning_days": warn_days, "receipts_output_dir": out_dir,
        })
        if theme_val != self.theme:
            self.set_theme(theme_val)
        self.refresh(active_tab="Pantry")
        self.msg("Configuration saved successfully.")

    def _build_sec_full_profile(self, parent):
        static_box = ttk.LabelFrame(parent, text="Static Profile Information (Never Changes)", padding=12)
        static_box.pack(fill="x", padx=4, pady=4)

        ttk.Label(static_box, text="Sex:").grid(row=0, column=0, sticky="w", pady=4)
        self.v_static_sex = tk.StringVar(value=self.pv["Sex"].get() if hasattr(self, "pv") and "Sex" in self.pv else "")
        self.cb_static_sex = ttk.Combobox(static_box, textvariable=self.v_static_sex, values=["M", "F"], state="readonly", width=8)
        self.cb_static_sex.grid(row=0, column=1, sticky="w", padx=(10, 0), pady=4)

        ttk.Label(static_box, text="Height (cm):").grid(row=1, column=0, sticky="w", pady=4)
        self.v_static_height = tk.StringVar(value=self.pv["Height_cm"].get() if hasattr(self, "pv") and "Height_cm" in self.pv else "")
        ttk.Entry(static_box, textvariable=self.v_static_height, width=10).grid(row=1, column=1, sticky="w", padx=(10, 0), pady=4)

        ttk.Button(static_box, text="Save Static Profile", command=self.save_static_profile).grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))

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
            if cur.execute("SELECT COUNT(*) FROM PROFILE WHERE ID = 1").fetchone()[0] > 0:
                cur.execute("UPDATE PROFILE SET Sex = ?, Height_cm = ? WHERE ID = 1", (sex, height))
            else:
                cur.execute("INSERT INTO PROFILE (ID, Height_cm, Sex) VALUES (1, ?, ?)", (height, sex))
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
                self._set_static_vars("", "")
                self.lbl_full_overview_text.insert(
                    "end",
                    "No user profile is currently configured.\n"
                    "No profile metrics to display. Please configure your profile in the Profile tab.\n",
                )
                self.lbl_full_overview_text.configure(state="disabled")
                return

            calc_m, _, target = estimates(p)
            bmi_str = "N/A"
            if p.get("Weight_kg") and p.get("Height_cm"):
                bmi_val, bmi_cat = bmi(p["Weight_kg"], p["Height_cm"])
                bmi_str = f"BMI {bmi_val:.1f} ({bmi_cat})"
            meals = c.execute("SELECT Name, Energy_Share_pct FROM MEALS ORDER BY Sort_Order").fetchall()

        if hasattr(self, "v_static_sex") and p.get("Sex"):
            self.v_static_sex.set(p["Sex"])
        if hasattr(self, "v_static_height") and p.get("Height_cm"):
            self.v_static_height.set(f"{p['Height_cm']:g}")

        lines = [
            "--- STATIC METRICS ---",
            f"  • Sex:                 {p.get('Sex') or 'Not set'}",
            f"  • Height:              {f'{p['Height_cm']:g} cm' if p.get('Height_cm') else 'Not set'}",
            "",
            "--- DYNAMIC BODY METRICS ---",
            f"  • Weight:              {f'{p['Weight_kg']:g} kg' if p.get('Weight_kg') else 'Not set'}",
            f"  • Age:                 {f'{p['Age_years']} years' if p.get('Age_years') else 'Not set'}",
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
            *(f"  • {name.capitalize()}: {share:g}%" for name, share in meals),
        ]
        self.lbl_full_overview_text.insert("end", "\n".join(lines))
        self.lbl_full_overview_text.configure(state="disabled")

    def _build_sec_receipt_logs(self, parent):
        top_bar = ttk.Frame(parent)
        top_bar.pack(fill="x", padx=4, pady=4)
        ttk.Label(top_bar, text="History of Imported Receipts:", font=("TkDefaultFont", 9, "bold")).pack(side="left")
        ttk.Button(top_bar, text="Refresh Logs", command=self._refresh_receipt_logs_table).pack(side="right")
        self.t_receipt_logs = Table(parent, [("t", "Time", 120), ("d", "Day", 120), ("f", "File Name", 360)], 20)
        self.t_receipt_logs.pack(fill="both", expand=True, padx=4, pady=(0, 4))

    def _refresh_receipt_logs_table(self):
        if hasattr(self, "t_receipt_logs"):
            with db() as c:
                logs = get_receipt_logs(c)
            self.t_receipt_logs.fill([((t, d, f), None) for t, d, f in logs])

    def _build_sec_destructive_actions(self, parent):
        ttk.Label(parent, text="⚠️ DANGER ZONE: Destructive Data Management", font=("TkDefaultFont", 11, "bold"), foreground=RED).pack(anchor="w", padx=4, pady=(8, 4))
        ttk.Label(
            parent,
            text="Actions in this section permanently delete data from your local database.\nEach operation requires explicit confirmation before execution.",
            wraplength=600,
        ).pack(anchor="w", padx=4, pady=(0, 12))

        for title, desc, btn_text, cmd in (
            ("Hard-Delete Entire Inventory",
             "Permanently delete all food stock, quantities, and locations from inventory.\nCatalog products and recipes are preserved.",
             "Delete Entire Inventory", self.on_clear_inventory),
            ("Reset User Profile",
             "Permanently delete the user profile and all historical weight logs.\nThe application returns to the unconfigured empty-profile state.",
             "Reset User Profile", self.on_reset_profile),
        ):
            box = ttk.LabelFrame(parent, text=title, padding=12)
            box.pack(fill="x", padx=4, pady=6)
            ttk.Label(box, text=desc).pack(anchor="w", pady=(0, 8))
            ttk.Button(box, text=btn_text, command=cmd).pack(anchor="w")

    def on_clear_inventory(self):
        if not messagebox.askyesno(
            "Confirm Hard-Delete Inventory",
            "Are you sure you want to permanently delete ALL items from your inventory?\n\nThis action CANNOT be undone.",
            parent=self, icon="warning",
        ):
            self.msg("Inventory deletion cancelled.")
            return
        with db() as c:
            clear_inventory(c)
        self.refresh(active_tab="Pantry")
        self.msg("Entire inventory has been permanently deleted.", error=True)

    def on_reset_profile(self):
        if not messagebox.askyesno(
            "Confirm Profile Reset",
            "Are you sure you want to permanently RESET your user profile?\n\n"
            "All physical metrics, targets, and weight history will be wiped.\nThis action CANNOT be undone.",
            parent=self, icon="warning",
        ):
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
        for k, val in (("Kcal_tolerance_pct", "5"), ("Open_extra_pct", "5"), ("No_repeat_days", "3"), ("Default_servings", "1")):
            self.pv[k].set(val)
        self._set_static_vars("", "")
        self.refresh()
        self._refresh_full_profile_view()
        self.msg("User profile has been reset.", error=True)

    def load_profile_form(self, p):
        if not p:
            return
        for k, var in self.pv.items():
            val = p.get(k)
            var.set("" if val is None else f"{val:g}" if isinstance(val, float) else str(val))
        for k, var in self.pu.items():
            var.set(bool(p.get(k)))
        if hasattr(self, "v_static_sex") and p.get("Sex"):
            self.v_static_sex.set(p["Sex"])
        if hasattr(self, "v_static_height") and p.get("Height_cm"):
            self.v_static_height.set(f"{p['Height_cm']:g}")

    def on_tab_changed(self):
        try:
            current_tab = self.nb.tab(self.nb.select(), "text")
        except Exception:
            current_tab = None
        self.refresh(active_tab=current_tab)

    # ---------------- refresh ----------------

    @staticmethod
    def _exp_tag(exp: Optional[str], today: date, warn_days: int) -> Optional[str]:
        if not exp:
            return None
        try:
            days_left = (date.fromisoformat(exp) - today).days
            return "expired" if days_left < 0 else ("expiring" if days_left <= warn_days else None)
        except ValueError:
            return None

    def refresh(self, active_tab=None):
        current_tab = None
        if hasattr(self, "nb"):
            try:
                current_tab = self.nb.tab(self.nb.select(), "text")
            except Exception:
                pass
        if active_tab is not None and hasattr(self, "nb") and not getattr(self, "_syncing_tab", False):
            target_title = "Profile" if active_tab in ("Profile", "Profilo") else ("Settings" if active_tab in ("Setting", "Settings") else active_tab)
            aliases = ("Profile", "Profilo") if target_title == "Profile" else (("Settings", "Setting") if target_title == "Settings" else (target_title,))
            self._select_tab(*aliases)

        with db() as c:
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
                if getattr(self, "f_ing", None):
                    self.f_ing.w["p"]["values"] = names + groups

            if active_tab in (None, "Pantry"):
                today = date.today()
                warn_days = int(config.get("expiration_warning_days", 7))
                self.t_disp.fill([
                    ((r["Name"], f"{r['Total_Available_Weight']:g}", r["Unit"], r["Location"], r.get("Expiration_Date") or ""),
                     self._exp_tag(r.get("Expiration_Date"), today, warn_days), r["Row_ID"])
                    for r in get_inventory_rows(self.db_path)
                ])

            if active_tab in (None, "Shopping"):
                shopping = c.execute("SELECT Name, Missing_Quantity, Unit FROM SHOPPING_LIST_VIEW ORDER BY Name").fetchall()
                self.t_shopping.fill([((n, f"{q:.2f}", u), None) for n, q, u in shopping])

            target_tab = active_tab if active_tab is not None else current_tab
            if target_tab == "Recipes" or (active_tab is None and not self.t_ric.tv.get_children() and self._cached_recipe_rows is None):
                if target_tab != "Recipes" or getattr(self, "_recipes_dirty", False) or self._cached_recipe_rows is None:
                    self._cached_recipe_rows = self.recipe_rows(c)
                    self._recipes_dirty = False
                self.t_ric.fill(self._cached_recipe_rows)
            else:
                self._recipes_dirty = True

            if active_tab in (None, "Profile", "Profilo"):
                p = m.load_profile(c)
                if not (p and p.get("Weight_kg") is not None and p.get("Sex") is not None):
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
                    history = c.execute("SELECT Date, Weight_kg, Body_fat_pct, Waist_cm FROM WEIGHT_LOG ORDER BY Date DESC LIMIT 90").fetchall()
                    calc_m, calc_k, _ = estimates(p)
                    self.calc_m.set(f"calculated: {calc_m or '-'}")
                    self.calc_k.set(f"calculated: {calc_k or '-'}")
                    self.bmi_label.configure(text=f"BMI {bmi(p['Weight_kg'], p['Height_cm'])[0]:.1f} ({bmi(p['Weight_kg'], p['Height_cm'])[1]})"
                                             if p.get("Weight_kg") and p.get("Height_cm") else "")
                    self.profile_label.configure(text=f"Target in use: {p['Kcal_target']:.0f} kcal/day"
                                                 if p.get("Kcal_target") else "Target not defined (save the profile)")
                    self.t_hist.fill([((d, f"{w:g}", "" if b is None else f"{b:g}", "" if x is None else f"{x:g}"), None)
                                      for d, w, b, x in history])
                    self.chart.plot([(d, w) for d, w, _, _ in reversed(history)], *theme.palette(self.theme))

            if active_tab in (None, "Setting", "Settings") and hasattr(self, "v_settings_section"):
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
        for widget in self._pantry_forms.values():
            widget.pack_forget()
        if choice in self._pantry_forms:
            self._pantry_forms[choice].pack(fill="x")

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
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        rows = get_available_inventory()
        with open(path, "w", encoding="utf-8") as out:
            out.write("# Product\tQuantity\tUnit\tLocation\n")
            out.writelines(f"{r['Name']}\t{r['Total_Available_Weight']:g}\t{r['Unit']}\t{r['Location']}\n" for r in rows)
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
        if not messagebox.askyesno("Delete Inventory Item", f"Are you sure you want to delete '{item_name}' (ID #{row_id}) from inventory?", parent=self):
            return
        try:
            if delete_inventory_item(row_id=row_id, db_path=self.db_path):
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
        if InventoryItemEditDialog(self, row_id=row_id, db_path=self.db_path).result:
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
                    status = "proposed: " + ("uses a leftover" if v["leftover"] else m.outcome(v["sim"], products, names))
                    rows.append(((p["date_str"], v["meal"], v["recipe"], f"{v['macro']['Kcal']:.0f}", status), None if v["sim"]["feasible"] else "ko"))
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
                "ORDER BY p.Date, (SELECT Sort_Order FROM MEALS WHERE Name = p.Meal)",
                (date_str, date_str, days),
            ).fetchall()
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
            parent=self, minvalue=1, maxvalue=20, initialvalue=servings,
        )
        if n is None:
            return
        with db() as c:
            pf.mark_cooked(c, date_str, meal, n)
        self.msg(f"Done: {meal} on {date_str} cooked, inventory updated" + (f" ({n - 1} leftovers available)." if n > 1 else "."))
        self.show_saved()
        self.refresh()

    @safe
    def save_profile(self):
        v = {k: s.get().strip() for k, s in self.pv.items()}
        with db() as c:
            existing_p = m.load_profile(c) or {}
        for k, static_attr in (("Sex", "v_static_sex"), ("Height_cm", "v_static_height")):
            if not v.get(k):
                static_val = getattr(self, static_attr).get() if hasattr(self, static_attr) else ""
                v[k] = static_val or (str(existing_p[k]) if existing_p.get(k) else "")
                if v[k]:
                    self.pv[k].set(v[k])

        if not (v.get("Sex") and v.get("Height_cm")):
            raise ValueError("Configure static profile metrics (Sex and Height) in Settings -> Full Profile first.")
        if not (v.get("Activity_Level") and v.get("Goal")):
            raise ValueError("Fill in activity level and goal.")

        int_keys = ("Age_years", "No_repeat_days", "Default_servings")
        req_keys = ("Weight_kg", "Height_cm", "Age_years", "Kcal_tolerance_pct", "Open_extra_pct", *int_keys[1:])
        opt_keys = ("Body_fat_pct", "Waist_cm", "Target_weight_kg", "Weekly_rate_kg", "Maintenance_manual", "Kcal_manual")
        data = {k: v[k] for k in ("Sex", "Activity_Level", "Goal")}
        for k in req_keys:
            val = number(v[k], k.replace("_", " "))
            data[k] = int(val) if k in int_keys else val
        for k in opt_keys:
            data[k] = number(v[k], k.replace("_", " ")) if v[k] else None
        data.update({k: int(var.get()) for k, var in self.pu.items()})

        with db() as c:
            set_meal_shares(c, {n: number(var.get(), n) for n, var in self.pm.items()})
            kcal = update_profile(c, data)
            c.commit()
        warning = data["Goal"] != "maintenance" and (data["Weekly_rate_kg"] or 0) > 1
        self.msg(f"Profile saved: target {kcal:.0f} kcal/day." + (" Warning: a rate above 1 kg/week is aggressive." if warning else ""), warning)
        self.refresh()

    @safe
    def add_ingredient(self):
        if not getattr(self, "f_ing", None):
            return
        v = self.f_ing.read()
        if not v["p"] or not v["u"]:
            raise ValueError("Specify product (or group), quantity and unit.")
        self.draft.append((v["p"], number(v["q"], "quantity"), v["u"]))
        self.f_ing.clear()
        self.show_draft()

    def show_draft(self):
        if getattr(self, "t_draft", None):
            self.t_draft.fill([((p, f"{q:g}", u), None) for p, q, u in self.draft])

    @safe
    def remove_ingredient(self):
        if getattr(self, "t_draft", None) and self.t_draft.tv.selection():
            del self.draft[self.t_draft.tv.index(self.t_draft.tv.selection()[0])]
            self.show_draft()

    @safe
    def save_recipe(self):
        name = self.v_rnome.get().strip() if hasattr(self, "v_rnome") else ""
        if not name:
            return
        with db() as c:
            outcome = rc.create_recipe(c, name, [n for n, var in self.v_meals.items() if var.get()], self.draft)
        self.draft = []
        self.v_rnome.set("")
        for var in self.v_meals.values():
            var.set(False)
        self.show_draft()
        warning = " Warning: " + outcome["warnings"][0] if outcome["warnings"] else ""
        self.msg(f"Recipe '{name}' saved." + warning, bool(warning))
        self._cached_recipe_rows = None
        self._recipes_dirty = True
        self.refresh()

    @safe
    def open_add_recipe_dialog(self, wait: bool = True):
        """Opens AddRecipeDialog modal for direct recipe creation."""
        return AddRecipeDialog(self, db_path=self._resolve_db_path(), wait=wait, on_save_callback=self._on_recipe_dialog_saved)

    def add_recipe(self, wait: bool = True):
        """Alias for open_add_recipe_dialog."""
        return self.open_add_recipe_dialog(wait=wait)

    @safe
    def open_recipe_generator_dialog(self, wait: bool = True):
        """Opens RecipeGeneratorDialog modal for hybrid slot + pairing + LP recipe generation."""
        return RecipeGeneratorDialog(self, db_path=self._resolve_db_path(), wait=wait, on_save_callback=self._on_recipe_dialog_saved)

    def _ensure_recipes_loaded(self):
        with db() as c:
            self._cached_recipe_rows = self.recipe_rows(c)
            self._recipes_dirty = False
            self.t_ric.fill(self._cached_recipe_rows)

    @safe
    def open_recipe_details(self, recipe_name: Optional[str] = None, wait: bool = True):
        """Opens RecipeDetailsDialog to inspect ingredients, macros, and notes."""
        if not recipe_name:
            selection = self.t_ric.tv.selection()
            if not selection:
                children = self.t_ric.tv.get_children()
                if not children:
                    messagebox.showinfo("Recipe Details", "No recipes available in the table.", parent=self)
                    return None
                self.t_ric.tv.selection_set(children[0])
                selection = (children[0],)
            recipe_name = self.t_ric.tv.item(selection[0], "values")[0]
        return RecipeDetailsDialog(self, recipe_name=recipe_name, db_path=self._resolve_db_path(), wait=wait)

    def _on_recipe_dialog_saved(self, outcome):
        """Callback invoked when a new recipe is saved through AddRecipeDialog."""
        name = outcome.get("Name", "Recipe")
        warning = (" Warning: " + outcome["warnings"][0]) if outcome.get("warnings") else ""
        self.msg(f"Recipe '{name}' saved." + warning, bool(warning))
        self._cached_recipe_rows = None
        self._recipes_dirty = True
        self.refresh(active_tab="Recipes")

    @safe
    def import_recipes(self):
        path = filedialog.askopenfilename(title="Import recipes", filetypes=[("Recipes", "*.json *.csv"), ("All files", "*.*")])
        if not path:
            return
        with db() as c:
            created, errors = rc.import_file(c, path)
        self.msg(f"Imported {created} recipes" + (f", {len(errors)} skipped." if errors else "."), bool(errors))
        if errors:
            messagebox.showwarning("Skipped recipes", "\n".join(errors[:15]))
        self._cached_recipe_rows = None
        self._recipes_dirty = True
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
        self._cached_recipe_rows = None
        self._recipes_dirty = True
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
            names = [r[0] for r in c.execute(
                "SELECT r.Name FROM RECIPES r JOIN RECIPE_MEALS p USING (Recipe_ID) WHERE p.Meal = ? ORDER BY r.Name", (meal,))]
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
            self.schedule_prep(**dlg.result)

    @safe
    def schedule_prep(self, recipe_id: int, cook_date: str, target_dates: list):
        """Schedules a meal prep cooking for the given recipe and dates."""
        with db() as c:
            cooking_id = pf.schedule_meal_prep(c, recipe_id, cook_date, target_dates)
        messagebox.showinfo(
            "Meal Prep Scheduled",
            f"Meal prep successfully scheduled (Cooking ID: {cooking_id})!\n"
            f"Cook date: {cook_date}\nMeals planned on: {', '.join(target_dates)}",
            parent=self,
        )
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

    @property
    def _receipt_log_text(self) -> str:
        if not hasattr(self, "_receipt_log_buffer"):
            self._receipt_log_buffer = []
        return "".join(self._receipt_log_buffer)

    @_receipt_log_text.setter
    def _receipt_log_text(self, val: str):
        self._receipt_log_buffer = [val] if val else []

    def _receipt_log_write(self, text: str):
        if not hasattr(self, "_receipt_log_buffer"):
            self._receipt_log_buffer = []
        self._receipt_log_buffer.append(text)

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
        path = filedialog.askopenfilename(title="Select receipt image", filetypes=[("Receipt images", ext_str), ("All files", "*.*")])
        if path:
            self._process_receipt(path)

    def _process_receipt(self, path: str):
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if hasattr(self, "_last_receipt_var"):
            self._last_receipt_var.set(f"Last receipt loaded: {now_str}")
        self._receipt_status.set("Reading receipt (OCR running)...")
        self.msg(f"Processing receipt: {Path(path).name}...")
        self._receipt_log_write(f"[{now_str}] File: {Path(path).name}\n  -> Running OCR analysis...\n")

        def _worker():
            try:
                from meal_planner import receipts as rp
                voci = rp.leggi_voci(path)
                self.after(0, lambda: self._on_receipt_read_completed(path, voci, None))
            except Exception as exc:
                self.after(0, lambda: self._on_receipt_read_completed(path, [], exc))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_receipt_read_completed(self, path: str, voci, error):
        self._receipt_status.set("")
        if error:
            err_str = str(error)
            display_err = "Unable to load image" if "impossibile caricare l'immagine" in err_str.lower() else err_str
            self.msg(f"OCR error: {error}", error=True)
            self._receipt_log_write(f"  [ERROR] {error}\n\n")
            messagebox.showerror("Receipt Error", display_err)
            return
        self.msg(f"Receipt parsed: {len(voci)} items found.")
        self._receipt_log_write(f"  -> Successfully recognized {len(voci)} item(s).\n  -> Opening review modal...\n\n")
        self._open_receipt_review(path, voci)

    def _open_receipt_review(self, path: str, voci):
        from meal_planner import receipts as rp
        out_dir = Path(config.get("receipts_output_dir", "receipts_output"))
        if not out_dir.is_absolute():
            out_dir = Path(__file__).resolve().parents[2] / out_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"receipt_{datetime.now():%Y%m%d_%H%M%S}.txt"
        top = tk.Toplevel(self)
        top.transient(self)
        top.title(f"receipt - {Path(path).name}")
        rp.FinestraModifica(
            top, voci, out_path, f"receipt - {Path(path).name}",
            on_save=lambda items: self.after(50, lambda: self._on_receipt_review_saved(items, out_path)),
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

        imported_count = new_aliases = new_products = 0
        opt_keys = ("fallback_id", "new_product_unit", "new_product_macros")
        for it in dlg.result:
            kwargs = {
                "name": it["name"], "unit_weight": it["unit_weight"], "intact_items": it["intact_items"],
                "location": it["location"], "expiration_date": it.get("expiration_date"),
                **({"db_path": db_path} if db_path else {}),
                **{k: it[k] for k in opt_keys if it.get(k) is not None},
            }
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

        now = datetime.now()
        file_name = Path(out_path).name if out_path else f"receipt_{now:%Y%m%d_%H%M%S}.txt"
        try:
            with db() as c:
                log_receipt(now.strftime("%H:%M:%S"), now.strftime("%Y-%m-%d"), file_name, conn=c)
        except Exception:
            pass
        if hasattr(self, "_last_receipt_var"):
            self._last_receipt_var.set(f"Last receipt loaded: {now:%Y-%m-%d %H:%M:%S}")
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
                raw_names = [p.strip() for line in Path(out_path).read_text(encoding="utf-8").splitlines()
                             if (p := line.strip().split(";")[0])]
            except Exception:
                pass
        if not raw_names and voci:
            raw_names = [
                n.strip() for v in voci
                if (n := v.nome if hasattr(v, "nome") else (v.get("nome") if isinstance(v, dict) else str(v))) and n.strip()
            ]
        with db() as c:
            alias_rows = c.execute("SELECT Name, ID FROM ALIASES").fetchall()
        known_lower = {name.lower() for name, _ in alias_rows}
        known_names = sorted({name for name, _ in alias_rows})
        unrecognized = list(dict.fromkeys(n for n in raw_names if n.lower() not in known_lower))
        if not unrecognized:
            return
        try:
            dlg = AliasMapDialog(self, unrecognized, known_names)
        except Exception:
            return
        if not dlg.result:
            return

        canonical_to_id = dict(alias_rows)
        mapped = 0
        with db() as c:
            for label, canonical in dlg.result.items():
                if canonical in canonical_to_id:
                    tid = canonical_to_id[canonical]
                    c.execute("INSERT OR IGNORE INTO ALIASES (Name, ID) VALUES (?, ?)", (label, tid))
                    mapped += 1
                    self._receipt_log_write(f"  -> Mapped alias '{label}' -> '{canonical}' (ID {tid})\n")
            c.commit()
        if mapped > 0:
            self.refresh()
            self.msg(f"Mapped {mapped} receipt alias(es) to catalog products.")


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
