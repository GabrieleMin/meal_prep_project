"""
Comprehensive automated tests for Milestone M1 Acceptance Criteria:
- R1: Startup DB integrity & FK verification, schema fast-path (zero DDL on initialized v1 DB),
      corrupted/FK-violating DB handling, FD leak prevention, clean main() exit.
- R2: System info & HiDPI scaling adaptation with safe fallbacks, zero malformed/TkDefaultFont tuples,
      single sv_ttk.set_theme at startup preserving Treeview rowheight across toggles,
      RoundedEntry >=4x LANCZOS supersampling, crisp PIL overlay card (no polygon), sv_ttk palette alignment.
- R3: Dispensa single-click vs double-click, single startup refresh & theme apply,
      tab-targeted refresh + SQLite conn=c reuse, constant-query batch recipe_rows (no N+1),
      debounced <Configure> on profile_overlay preserving ttk.Button instance identity.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image

from meal_planner import config as cfg
from meal_planner import engine as m
from meal_planner.db import connection as db_conn
from meal_planner.db.connection import get_connection
from meal_planner.db.schema import (
    DatabaseIntegrityError,
    SCHEMA_VERSION,
    create_schema,
    schema_exists,
    verify_integrity,
)
from meal_planner.gui import system_info, theme
from meal_planner.gui.app import App, main as gui_main
from meal_planner.gui.dialogs import (
    _RECONCILIATION_FLASH_STYLES,
    AddRecipeDialog,
    ReceiptReconciliationDialog,
)
from meal_planner.gui import widgets as gui_widgets
from meal_planner.gui.widgets import LineChart, Table


class _IsolatedDbGuiTestCase(unittest.TestCase):
    """Base test case using an isolated temporary DB without ever touching data/inventory.db."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp.name
        self.tmp.close()

        self._orig_db_env = os.environ.get("MEAL_PLANNER_DB")
        os.environ["MEAL_PLANNER_DB"] = self.db_path
        cfg._invalidate_cache()

        self._roots_to_destroy = []

    def tearDown(self):
        for root in reversed(self._roots_to_destroy):
            try:
                if root.winfo_exists():
                    root.destroy()
            except Exception:
                pass
        self._roots_to_destroy.clear()

        cfg._invalidate_cache()
        if self._orig_db_env is not None:
            os.environ["MEAL_PLANNER_DB"] = self._orig_db_env
        else:
            os.environ.pop("MEAL_PLANNER_DB", None)
        cfg._invalidate_cache()

        for suffix in ("", "-wal", "-shm"):
            p = self.db_path + suffix
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass

    def _init_healthy_db(self):
        conn = get_connection(self.db_path)
        create_schema(conn)
        conn.commit()
        conn.close()


class TestR1StartupIntegrityAndSchemaFastPath(_IsolatedDbGuiTestCase):
    """R1 Acceptance Criteria tests."""

    def test_startup_runs_integrity_and_fk_checks_with_zero_ddl_on_v1_db(self):
        self._init_healthy_db()
        # Prime config table too via save_config so APP_SETTINGS exists
        cfg.save_config({"default_theme": "dark", "theme": "dark"})
        cfg._invalidate_cache()

        traced_sql: list[str] = []
        orig_get_conn = db_conn.get_connection

        def tracing_get_conn(*args, **kwargs):
            c = orig_get_conn(*args, **kwargs)
            c.set_trace_callback(traced_sql.append)
            return c

        with patch("meal_planner.db.connection.get_connection", side_effect=tracing_get_conn), \
             patch("meal_planner.gui.support.get_connection", side_effect=tracing_get_conn):
            app = App()
            self._roots_to_destroy.append(app)
            app.withdraw()
            app.update_idletasks()

        normalized = [s.strip().upper() for s in traced_sql if s and s.strip()]
        self.assertTrue(any("PRAGMA INTEGRITY_CHECK" in s for s in normalized), f"Missing PRAGMA integrity_check in {normalized}")
        self.assertTrue(any("PRAGMA FOREIGN_KEY_CHECK" in s for s in normalized), f"Missing PRAGMA foreign_key_check in {normalized}")

        ddl_prefixes = ("CREATE TABLE", "CREATE INDEX", "CREATE VIEW", "CREATE TRIGGER", "ALTER TABLE")
        ddl_executed = [s for s in normalized if s.startswith(ddl_prefixes)]
        self.assertEqual(ddl_executed, [], f"Initialized v1 DB startup executed unexpected DDL: {ddl_executed}")

        seed_dml = [s for s in normalized if s.startswith("INSERT OR IGNORE INTO")]
        self.assertEqual(seed_dml, [], f"Initialized v1 DB startup executed unexpected seed DML: {seed_dml}")

    def test_create_schema_fast_path_v0_upgrade_and_partial_repair(self):
        # 1. Fresh DB -> creates schema and sets user_version == SCHEMA_VERSION
        conn = get_connection(self.db_path)
        try:
            create_schema(conn)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertTrue(schema_exists(conn))
            verify_integrity(conn)

            # 2. Second call -> fast-path executes only read-only checks
            sql_second: list[str] = []
            conn.set_trace_callback(sql_second.append)
            create_schema(conn)
            conn.set_trace_callback(None)
            self.assertEqual(len(sql_second), 2)
            self.assertIn("PRAGMA user_version", sql_second[0])
            self.assertIn("sqlite_master", sql_second[1])

            # 3. Legacy v0 DB -> user_version = 0 triggers full idempotent check and sets user_version = 1
            conn.execute("PRAGMA user_version = 0")
            conn.commit()
            sql_v0: list[str] = []
            conn.set_trace_callback(sql_v0.append)
            create_schema(conn)
            conn.set_trace_callback(None)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)
            self.assertTrue(any("CREATE TABLE IF NOT EXISTS" in s for s in sql_v0))

            # 4. Partial schema repair even when user_version == 1
            conn.execute("DROP TABLE RECEIPT_LOGS")
            conn.commit()
            self.assertFalse(schema_exists(conn))
            create_schema(conn)
            self.assertTrue(schema_exists(conn))
        finally:
            conn.close()

    def test_startup_corrupted_file_raises_and_notifies_without_continuing(self):
        with open(self.db_path, "wb") as fh:
            fh.write(b"NOT_A_VALID_SQLITE_DATABASE_HEADER_0123456789" * 4)

        with patch("tkinter.messagebox.showerror") as mock_err, \
             patch.object(App, "refresh") as mock_refresh:
            with self.assertRaises(DatabaseIntegrityError):
                App()
            self.assertTrue(mock_err.called, "User must be alerted via messagebox.showerror on corrupted DB")
            self.assertEqual(mock_refresh.call_count, 0, "GUI must not continue to refresh() on corrupted DB")

    def test_startup_integrity_check_failure_raises_and_notifies(self):
        self._init_healthy_db()
        # Simulate PRAGMA integrity_check returning a corruption message on a valid SQLite file
        orig_get_conn = db_conn.get_connection

        class _CorruptPragmaConn:
            def __init__(self, inner):
                self._inner = inner

            def execute(self, sql, *args, **kwargs):
                if "integrity_check" in sql.lower():
                    cur = MagicMock()
                    cur.fetchall.return_value = [("*** in database main *** Page 2: btreeInitPage() returns error code 11",)]
                    return cur
                return self._inner.execute(sql, *args, **kwargs)

            def __getattr__(self, item):
                return getattr(self._inner, item)

        def wrapped_conn(*args, **kwargs):
            return _CorruptPragmaConn(orig_get_conn(*args, **kwargs))

        with patch("meal_planner.gui.support.get_connection", side_effect=wrapped_conn), \
             patch("tkinter.messagebox.showerror") as mock_err, \
             patch.object(App, "refresh") as mock_refresh:
            with self.assertRaises(DatabaseIntegrityError) as ctx:
                App()
            self.assertIn("btreeInitPage", str(ctx.exception))
            self.assertTrue(mock_err.called)
            self.assertEqual(mock_refresh.call_count, 0)

    def test_startup_fk_violation_raises_and_notifies(self):
        self._init_healthy_db()
        conn = get_connection(self.db_path)
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute(
                "INSERT INTO INVENTORY (ID, Unit_Weight, Intact_Items, Open_Remainder, Location) "
                "VALUES (99999, 1.0, 1, 0.0, 'pantry')"
            )
            conn.execute("PRAGMA foreign_keys = ON")
            conn.commit()
        finally:
            conn.close()

        with patch("tkinter.messagebox.showerror") as mock_err, \
             patch.object(App, "refresh") as mock_refresh:
            with self.assertRaises(DatabaseIntegrityError) as ctx:
                App()
            self.assertIn("INVENTORY", str(ctx.exception))
            self.assertTrue(mock_err.called)
            self.assertEqual(mock_refresh.call_count, 0)

    def test_main_returns_nonzero_cleanly_on_database_integrity_error(self):
        with open(self.db_path, "wb") as fh:
            fh.write(b"CORRUPTED_BYTES")
        with patch("tkinter.messagebox.showerror"):
            rc = gui_main()
        self.assertEqual(rc, 1)


class TestR2SharpnessAndSystemAdaptation(_IsolatedDbGuiTestCase):
    """R2 Acceptance Criteria tests."""

    def test_system_info_hidpi_env_overrides_and_safe_fallbacks(self):
        # 1. Zero-mm virtual monitor without fpixels -> safe fallback (no ZeroDivisionError)
        info_fallback = system_info.collect_system_info(
            root=None,
            env={},
            screen_metrics={"width_px": 1280, "height_px": 800, "width_mm": 0, "height_mm": 0, "fpixels_1i": None},
        )
        self.assertTrue(info_fallback.used_fallback)
        self.assertEqual(info_fallback.dpi, 96.0)
        self.assertEqual(info_fallback.scale_factor, 1.0)
        self.assertAlmostEqual(info_fallback.tk_scaling, 96.0 / 72.0, places=4)

        # 2. Simulated 192 DPI (2x HiDPI)
        info_hidpi = system_info.collect_system_info(
            root=None,
            env={},
            screen_metrics={"width_px": 3840, "height_px": 2160, "width_mm": 0, "height_mm": 0, "fpixels_1i": 192.0},
        )
        self.assertFalse(info_hidpi.used_fallback)
        self.assertEqual(info_hidpi.dpi, 192.0)
        self.assertEqual(info_hidpi.scale_factor, 2.0)
        self.assertAlmostEqual(info_hidpi.tk_scaling, 192.0 / 72.0, places=4)

        # 3. Environment scaling overrides (GDK_SCALE * GDK_DPI_SCALE, QT_SCALE_FACTOR, MEAL_PLANNER_SCALE)
        info_gdk = system_info.collect_system_info(
            root=None,
            env={"GDK_SCALE": "2", "GDK_DPI_SCALE": "0.75"},
            screen_metrics={"width_px": 1920, "height_px": 1080, "width_mm": 500, "height_mm": 280, "fpixels_1i": 96.0},
        )
        self.assertAlmostEqual(info_gdk.scale_factor, 1.5, places=3)

        info_qt = system_info.collect_system_info(
            root=None,
            env={"QT_SCALE_FACTOR": "1.25"},
            screen_metrics={"width_px": 1920, "height_px": 1080, "width_mm": 500, "height_mm": 280, "fpixels_1i": 96.0},
        )
        self.assertAlmostEqual(info_qt.scale_factor, 1.25, places=3)

        # 4. Invalid env scale strings ignored safely
        info_bad = system_info.collect_system_info(
            root=None,
            env={"MEAL_PLANNER_SCALE": "invalid", "GDK_SCALE": "-2", "QT_SCALE_FACTOR": "nan"},
            screen_metrics={"width_px": 1280, "height_px": 800, "width_mm": 0, "height_mm": 0, "fpixels_1i": 96.0},
        )
        self.assertEqual(info_bad.scale_factor, 1.0)

        # 5. Screen clamping for small screens (800x600)
        w, h, mw, mh = system_info.clamp_geometry(1140, 700, 800, 600, min_w=920, min_h=580)
        self.assertLessEqual(w, int(800 * 0.95))
        self.assertLessEqual(h, int(600 * 0.92))
        self.assertLessEqual(mw, w)
        self.assertLessEqual(mh, h)

        # 6. Apply scaling + SunValley* font scaling on real Tk root
        root = tk.Tk()
        root.withdraw()
        self._roots_to_destroy.append(root)
        system_info.apply_tk_scaling(root, info_hidpi)
        applied_scaling = float(root.tk.call("tk", "scaling"))
        self.assertAlmostEqual(applied_scaling, info_hidpi.tk_scaling, places=1)
        self.assertGreater(applied_scaling, 2.5)
        theme.apply("dark", root=root, scale_factor=2.0)
        body_font = tkfont.nametofont("SunValleyBodyFont", root=root)
        self.assertEqual(body_font.cget("size"), -28)
        self.assertGreaterEqual(int(ttk.Style(root).lookup("Treeview", "rowheight")), 56)

    def test_no_malformed_or_tkdefaultfont_tuples_in_gui_modules(self):
        project_root = Path(__file__).resolve().parents[1]
        targets = list((project_root / "meal_planner" / "gui").glob("*.py")) + [
            project_root / "meal_planner" / "receipts" / "pipeline.py"
        ]
        violations = []
        for path in targets:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, (ast.Tuple, ast.List)) and node.elts:
                    first = node.elts[0]
                    if isinstance(first, ast.Constant):
                        if first.value == "TkDefaultFont":
                            violations.append(f"{path.name}:{node.lineno}: hardcoded TkDefaultFont tuple")
                        if first.value == "" and len(node.elts) >= 2 and isinstance(node.elts[1], ast.Constant) and node.elts[1].value == 0:
                            violations.append(f"{path.name}:{node.lineno}: malformed zero-size font tuple")
        self.assertEqual(violations, [], f"Font policy violations found: {violations}")

    def test_runtime_table_expired_tag_and_linechart_fonts(self):
        self._init_healthy_db()
        app = App()
        self._roots_to_destroy.append(app)
        app.withdraw()
        app.update_idletasks()

        exp_font_name = app.t_disp.tv.tag_configure("expired", "font")
        self.assertTrue(exp_font_name)
        exp_font = tkfont.nametofont(str(exp_font_name), root=app)
        sv_body = tkfont.nametofont("SunValleyBodyFont", root=app)
        self.assertEqual(exp_font.actual("weight"), "bold")
        self.assertEqual(exp_font.actual("family"), sv_body.actual("family"))
        self.assertEqual(int(exp_font.cget("size")), int(sv_body.cget("size")))

        app.chart.plot([("2026-01-01", 70.0), ("2026-01-02", 69.5)], *theme.palette(app.theme))
        app.update_idletasks()
        chart_texts = [it for it in app.chart.find_all() if app.chart.type(it) == "text"]
        self.assertGreaterEqual(len(chart_texts), 4)
        cap_font = theme.font("caption", app)
        for tid in chart_texts:
            self.assertEqual(app.chart.itemcget(tid, "font"), str(cap_font))

    def test_single_sv_ttk_apply_at_startup_and_treeview_rowheight_preserved(self):
        self._init_healthy_db()
        import sv_ttk
        with patch("sv_ttk.set_theme", wraps=sv_ttk.set_theme) as mock_set_theme:
            app = App()
            self._roots_to_destroy.append(app)
            app.withdraw()
            app.update_idletasks()
            self.assertEqual(
                mock_set_theme.call_count,
                1,
                f"sv_ttk.set_theme must be called once at startup, got {mock_set_theme.call_count}",
            )

        style = ttk.Style(app)
        rh_dark = int(style.lookup("Treeview", "rowheight"))
        self.assertGreaterEqual(rh_dark, 28)

        app.toggle_theme()
        app.update_idletasks()
        rh_light = int(style.lookup("Treeview", "rowheight"))
        self.assertGreaterEqual(rh_light, 28, "Treeview rowheight must survive dark->light theme switch")

        app.toggle_theme()
        app.update_idletasks()
        self.assertGreaterEqual(int(style.lookup("Treeview", "rowheight")), 28)

    def test_rounded_entry_supersampling_and_overlay_card_and_sv_ttk_palette(self):
        self._init_healthy_db()
        self.assertEqual(theme.palette("dark"), ("#1c1c1c", "#fafafa"))
        self.assertEqual(theme.palette("light"), ("#fafafa", "#1c1c1c"))

        app = App()
        self._roots_to_destroy.append(app)
        app.withdraw()
        app.refresh(active_tab="Profile")
        app.update()
        deadline_init = time.monotonic() + 0.5
        while app._overlay_draw_job is not None and time.monotonic() < deadline_init:
            app.update()
            time.sleep(0.01)
        app.update_idletasks()

        # Profile overlay must have zero polygons, crisp image card, and sv_ttk background
        c = app.profile_overlay
        self.assertEqual([it for it in c.find_all() if c.type(it) == "polygon"], [])
        img_items = [it for it in c.find_all() if c.type(it) == "image"]
        self.assertEqual(len(img_items), 1)
        self.assertIn("overlay_card", c.gettags(img_items[0]))
        expected_bg = "#1c1c1c" if app.theme == "dark" else "#fafafa"
        self.assertEqual(c.cget("background"), expected_bg)
        self.assertEqual(app._drop_zone.cget("background"), expected_bg)
        self.assertEqual(app.text.cget("background"), expected_bg)
        self.assertEqual(app.lbl_full_overview_text.cget("background"), expected_bg)

        # RoundedEntry 4x+ LANCZOS supersampling verification
        cache_key = (id(app.tk), app.theme == "dark")
        _RECONCILIATION_FLASH_STYLES.pop(cache_key, None)

        new_sizes = []
        resize_methods = []
        orig_new = Image.new
        orig_resize = Image.Image.resize

        def spy_new(mode, size, color=0):
            new_sizes.append(size)
            return orig_new(mode, size, color)

        def spy_resize(img_self, size, *args, **kwargs):
            resample = args[0] if args else kwargs.get("resample")
            resize_methods.append((img_self.size, size, resample))
            return orig_resize(img_self, size, *args, **kwargs)

        with patch("PIL.Image.new", side_effect=spy_new), \
             patch.object(Image.Image, "resize", spy_resize):
            dlg = ReceiptReconciliationDialog(app, [("Test Item", 1, "kg", 1)], db_path=self.db_path, wait=False)
            dlg.destroy()

        self.assertEqual(len(new_sizes), 26)
        self.assertTrue(all(w >= 144 and h >= 104 for w, h in new_sizes), f"Expected >=4x supersampling, got {new_sizes[:3]}")
        lanczos_val = getattr(Image, "Resampling", Image).LANCZOS
        self.assertTrue(all(r == lanczos_val for _, _, r in resize_methods))

        # Notes tk.Text widget in AddRecipeDialog matches theme palette
        add_dlg = AddRecipeDialog(app, db_path=self.db_path, wait=False)
        self.assertEqual(add_dlg.t_notes.cget("background"), expected_bg)
        add_dlg.destroy()


class TestR3ResponsivenessAndQueryOptimization(_IsolatedDbGuiTestCase):
    """R3 Acceptance Criteria tests."""

    def _seed_sample_data(self):
        conn = get_connection(self.db_path)
        create_schema(conn)
        conn.execute(
            "INSERT OR REPLACE INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g) "
            "VALUES (1, 'kg', 350, 12, 70, 2)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g) "
            "VALUES (2, 'kg', 165, 31, 0, 3.6)"
        )
        conn.execute("INSERT OR REPLACE INTO ALIASES (Name, ID) VALUES ('Pasta', 1)")
        conn.execute("INSERT OR REPLACE INTO ALIASES (Name, ID) VALUES ('Chicken', 2)")
        conn.execute(
            "INSERT INTO INVENTORY (Row_ID, ID, Unit_Weight, Intact_Items, Open_Remainder, Location) "
            "VALUES (10, 1, 0.5, 2, 0.0, 'pantry')"
        )
        conn.commit()
        conn.close()

    def test_dispensa_single_click_selects_only_double_click_opens_edit_once(self):
        self._seed_sample_data()
        app = App()
        self._roots_to_destroy.append(app)
        app.withdraw()
        app.update_idletasks()

        self.assertIsNone(app.t_disp.on_select, "Dispensa table must not open edit dialog on single click")
        self.assertIsNotNone(app.t_disp.on_double_click)

        rows = app.t_disp.tv.get_children()
        self.assertGreaterEqual(len(rows), 1)
        target_iid = rows[0]

        class _FakeEv:
            x, y = 120, 20

        with patch("meal_planner.gui.app.InventoryItemEditDialog") as mock_edit_dlg, \
             patch.object(app.t_disp.tv, "identify", return_value="cell"), \
             patch.object(app.t_disp.tv, "identify_row", return_value=target_iid), \
             patch.object(app.t_disp.tv, "identify_column", return_value="#1"):
            mock_edit_dlg.return_value.result = None
            app.t_disp._handle_click(_FakeEv())
            self.assertEqual(mock_edit_dlg.call_count, 0, "Single click must NOT open InventoryItemEditDialog")

            app.t_disp._handle_double_click(_FakeEv())
            self.assertEqual(mock_edit_dlg.call_count, 1, "Double click must open InventoryItemEditDialog once")
            self.assertIn(target_iid, app.t_disp.tv.selection())

    def test_single_startup_refresh_and_single_theme_apply(self):
        self._seed_sample_data()
        refresh_calls = []
        orig_refresh = App.refresh

        def spy_refresh(app_self, *args, **kwargs):
            refresh_calls.append((args, kwargs))
            return orig_refresh(app_self, *args, **kwargs)

        with patch("meal_planner.gui.theme.apply", wraps=theme.apply) as mock_theme_apply, \
             patch.object(App, "refresh", spy_refresh):
            app = App()
            self._roots_to_destroy.append(app)
            app.withdraw()
            app.update()
            self.assertEqual(mock_theme_apply.call_count, 1, f"Expected 1 startup theme.apply, got {mock_theme_apply.call_count}")
            self.assertEqual(len(refresh_calls), 1, f"Expected 1 startup App.refresh, got {len(refresh_calls)}")

    def test_targeted_refresh_and_connection_reuse(self):
        self._seed_sample_data()
        app = App()
        self._roots_to_destroy.append(app)
        app.withdraw()
        app.update_idletasks()

        # 1. Verify bare app.refresh() reuses a single SQLite connection (via conn=c)
        orig_get_conn = db_conn.get_connection
        conn_count = 0

        def counting_conn(*args, **kwargs):
            nonlocal conn_count
            conn_count += 1
            return orig_get_conn(*args, **kwargs)

        with patch("meal_planner.gui.support.get_connection", side_effect=counting_conn), \
             patch("meal_planner.db.connection.get_connection", side_effect=counting_conn):
            app.refresh()
        self.assertEqual(conn_count, 1, f"app.refresh() should open exactly 1 connection, opened {conn_count}")

        # 2. Verify single-tab action (add / save_profile / loc_add) does NOT trigger unfiltered 6-tab fill
        with patch.object(app.t_hist, "fill") as mock_hist_fill, \
             patch.object(app.t_shopping, "fill") as mock_shop_fill, \
             patch.object(app, "recipe_rows") as mock_recipe_rows:
            app.f_add.v["name"].set("Pasta")
            app.f_add.v["weight"].set("0.5")
            app.f_add.v["num"].set("1")
            app.f_add.v["location"].set("pantry")
            app.add()
            self.assertEqual(mock_hist_fill.call_count, 0, "Pantry add() must not refresh Profile history")
            self.assertEqual(mock_shop_fill.call_count, 0, "Pantry add() must not refresh Shopping list")
            self.assertEqual(mock_recipe_rows.call_count, 0, "Pantry add() must defer Recipes simulation")

        with patch.object(app.t_disp, "fill") as mock_disp_fill, \
             patch.object(app.t_shopping, "fill") as mock_shop_fill:
            app.v_static_sex.set("M")
            app.v_static_height.set("180")
            app.pv["Weight_kg"].set("75")
            app.pv["Age_years"].set("30")
            app.pv["Activity_Level"].set("moderate")
            app.pv["Goal"].set("maintenance")
            app.pv["Kcal_tolerance_pct"].set("5")
            app.pv["Open_extra_pct"].set("5")
            app.pv["No_repeat_days"].set("3")
            app.pv["Default_servings"].set("1")
            app.save_profile()
            self.assertEqual(mock_disp_fill.call_count, 0, "save_profile() must not refresh Pantry table")
            self.assertEqual(mock_shop_fill.call_count, 0, "save_profile() must not refresh Shopping table")

    def test_recipe_rows_constant_query_count_no_n_plus_1(self):
        self._seed_sample_data()
        conn = get_connection(self.db_path)
        try:
            conn.execute("INSERT INTO SUBSTITUTE_GROUPS (Group_ID, Name) VALUES (1, 'Proteins')")
            conn.execute("INSERT INTO GROUP_MEMBERS (Group_ID, ID) VALUES (1, 2)")

            def _add_recipes(start_idx, end_idx):
                for i in range(start_idx, end_idx + 1):
                    conn.execute("INSERT INTO RECIPES (Recipe_ID, Name) VALUES (?, ?)", (i, f"Recipe {i:02d}"))
                    conn.execute("INSERT INTO RECIPE_MEALS (Recipe_ID, Meal) VALUES (?, 'lunch')", (i,))
                    conn.execute(
                        "INSERT INTO RECIPE_INGREDIENTS (Recipe_ID, ID, Group_ID, Quantity, Unit, To_Taste) "
                        "VALUES (?, 1, NULL, 100, 'g', 0)",
                        (i,),
                    )
                    conn.execute(
                        "INSERT INTO RECIPE_INGREDIENTS (Recipe_ID, ID, Group_ID, Quantity, Unit, To_Taste) "
                        "VALUES (?, NULL, 1, 150, 'g', 0)",
                        (i,),
                    )
                conn.commit()

            _add_recipes(1, 2)
            sql_n2: list[str] = []
            conn.set_trace_callback(sql_n2.append)
            rows_n2 = App.recipe_rows(conn)
            conn.set_trace_callback(None)

            _add_recipes(3, 6)
            sql_n6: list[str] = []
            conn.set_trace_callback(sql_n6.append)
            rows_n6 = App.recipe_rows(conn)
            conn.set_trace_callback(None)

            self.assertEqual(len(rows_n2), 2)
            self.assertEqual(len(rows_n6), 6)
            self.assertEqual(
                len(sql_n2),
                len(sql_n6),
                f"Query count grew with recipe count N! N=2: {len(sql_n2)}, N=6: {len(sql_n6)}",
            )
            ri_queries = [s for s in sql_n6 if "RECIPE_INGREDIENTS" in s]
            gm_queries = [s for s in sql_n6 if "GROUP_MEMBERS" in s]
            self.assertEqual(len(ri_queries), 1)
            self.assertEqual(len(gm_queries), 1)

            # Verify exact macro & feasibility equivalence vs per-recipe simulate_recipe / recipe_macros
            products = m.load_products(conn)
            names = m.product_names(conn)
            inv = m.VirtualInventory.from_db(conn)
            for idx, (rid, name) in enumerate(conn.execute("SELECT Recipe_ID, Name FROM RECIPES ORDER BY Name")):
                sim = m.simulate_recipe(conn, inv.copy(), rid, products)
                mac = m.recipe_macros(conn, rid, products, sim["group_choices"])
                expected_vals = (
                    name,
                    f"{mac['Kcal']:.0f}",
                    f"{mac['Protein']:.1f}",
                    f"{mac['Carbs']:.1f}",
                    f"{mac['Fat']:.1f}",
                    f"{mac['Fiber']:.1f}",
                    f"{sim['open_coverage'] * 100:.0f}%",
                    m.outcome(sim, products, names),
                )
                self.assertEqual(rows_n6[idx][0], expected_vals)
        finally:
            conn.close()

    def test_profile_overlay_configure_debounce_and_button_reuse(self):
        self._init_healthy_db()
        app = App()
        self._roots_to_destroy.append(app)
        app.withdraw()
        app.refresh(active_tab="Profile")
        app.update()
        deadline_init = time.monotonic() + 0.5
        while app._overlay_draw_job is not None and time.monotonic() < deadline_init:
            app.update()
            time.sleep(0.01)
        app.update_idletasks()

        c = app.profile_overlay
        btn_items = c.find_withtag("overlay_btn")
        self.assertEqual(len(btn_items), 1)
        btn_before = c.nametowidget(c.itemcget(btn_items[0], "window"))

        class _CfgEv:
            def __init__(self, width, height):
                self.width = width
                self.height = height

        # 1. Move-only events (same size as _overlay_last_size) do not schedule redraw
        cur_w, cur_h = app._overlay_last_size
        with patch.object(app, "_draw_profile_overlay", wraps=app._draw_profile_overlay) as mock_draw:
            for _ in range(10):
                app._on_profile_overlay_configure(_CfgEv(cur_w, cur_h))
            self.assertIsNone(app._overlay_draw_job)

            # 2. Rapid burst of 15 resize events schedules a single debounced redraw
            for step in range(15):
                app._on_profile_overlay_configure(_CfgEv(600 + step * 10, 420 + step * 5))
            self.assertEqual(mock_draw.call_count, 0, "Resize burst must be debounced, not synchronous")

            deadline = time.monotonic() + 0.5
            while app._overlay_draw_job is not None and time.monotonic() < deadline:
                app.update()
                time.sleep(0.01)
            app.update_idletasks()
            self.assertEqual(mock_draw.call_count, 1, "Debounced burst should draw at most once after settle")

        btn_items_after = c.find_withtag("overlay_btn")
        self.assertEqual(len(btn_items_after), 1)
        btn_after = c.nametowidget(c.itemcget(btn_items_after[0], "window"))
        self.assertIs(btn_after, btn_before, "ttk.Button on overlay must be reused, never destroyed/recreated")

    def test_table_delete_icon_caching_and_invalidation(self):
        root = tk.Tk()
        root.withdraw()
        self._roots_to_destroy.append(root)

        size_px = theme.scale_px(20, root)
        rows = [((f"Item {i}",), "", str(i)) for i in range(10)]

        with patch("meal_planner.gui.widgets.create_delete_icon", wraps=gui_widgets.create_delete_icon) as mock_create:
            table = Table(root, [("name", "Product", 120)], height=5, show_delete=True, is_dark=False)
            table.fill(rows)

            t0 = time.perf_counter()
            for i in range(100):
                table.update_delete_icon(is_dark=(i % 2 == 0))
            elapsed = time.perf_counter() - t0

            self.assertLess(elapsed, 1.0, f"100 cached icon toggles took {elapsed:.3f}s (expected < 1.0s)")
            self.assertLessEqual(
                mock_create.call_count,
                2,
                f"Expected at most 2 create_delete_icon calls (light+dark), got {mock_create.call_count}",
            )
            self.assertEqual(
                set(table._delete_icons_cache.keys()),
                {(False, size_px), (True, size_px)},
            )
            active_token = str(table._delete_icon)
            for child in table.tv.get_children(""):
                img = table.tv.item(child, "image")
                assigned = img[0] if isinstance(img, (tuple, list)) else img
                self.assertEqual(assigned, active_token)

            # Distinct size_px uses a distinct cache key
            with patch("meal_planner.gui.theme.scale_px", return_value=size_px + 8):
                table.update_delete_icon(is_dark=True)
            self.assertIn((True, size_px + 8), table._delete_icons_cache)

            # Setting _delete_icon = None invalidates the cache and forces fresh generation
            calls_before_invalidation = mock_create.call_count
            table._delete_icon = None
            table.update_delete_icon(is_dark=False)
            self.assertEqual(mock_create.call_count, calls_before_invalidation + 1)
            self.assertIsNotNone(table._delete_icon)
            self.assertIs(table._delete_icon_ref, table._delete_icon)
            self.assertEqual(set(table._delete_icons_cache.keys()), {(False, size_px)})

            # External Tcl image deletion of a cached inactive icon triggers clean regeneration
            table.update_delete_icon(is_dark=True)
            cached_light = table._delete_icons_cache[(False, size_px)]
            table.tv.tk.call("image", "delete", str(cached_light))
            calls_before_tcl_recovery = mock_create.call_count
            table.update_delete_icon(is_dark=False)
            self.assertEqual(mock_create.call_count, calls_before_tcl_recovery + 1)
            self.assertEqual(table.tv.tk.call("image", "type", str(table._delete_icon)), "photo")

        # Cache invalidation + PIL failure leaves _delete_icon None and rows clean
        table._delete_icon = None
        with patch("PIL.Image.new", side_effect=RuntimeError("Simulated PIL failure")):
            table.update_delete_icon(is_dark=True)
        self.assertIsNone(table._delete_icon)
        self.assertIsNone(table._delete_icon_ref)
        for child in table.tv.get_children(""):
            img = table.tv.item(child, "image")
            assigned = img[0] if isinstance(img, (tuple, list)) else img
            self.assertEqual(assigned, "")


if __name__ == "__main__":
    unittest.main()
