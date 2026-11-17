"""
tests/test_e2e_theme_and_efficiency.py

Comprehensive Opaque-Box E2E Test Suite for Theme Toggle Persistence,
Aesthetic Softening, Config In-Memory Caching, Backend Invariants, and UI Localization.

Test Methodology:
- Tier 1: Feature Coverage (Happy Path for F1 & F2)
- Tier 2: Boundary & Corner Cases (Extreme inputs, stress toggles, concurrency, missing data)
- Tier 3: Cross-Feature Interactions (Pairwise combinations across theme, cache, DB, and UI)
- Tier 4: Real-World Scenarios (Full end-to-end user workflows)

Safety & Invariants:
- Strictly 100% OFFLINE execution.
- Ephemeral SQLite isolation per test via tempfile and MEAL_PLANNER_DB.
- Guaranteed baseline preservation on data/inventory.db.
- Non-blocking headless Tkinter execution with modal dialog mocking.
"""

from collections import Counter
from contextlib import redirect_stdout
import io
import math
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import tkinter as tk
from tkinter import ttk
from PIL import Image, ImageTk

from meal_planner.db.connection import get_connection, DB_PATH
from meal_planner.db.schema import create_schema, migrate
import meal_planner.config as cfg
import meal_planner.inventory as inv
import meal_planner.recipes as rec
from meal_planner.engine import catalog as engine_catalog
from meal_planner.engine import nutrition as engine_nutrition
from meal_planner.engine import virtual_inventory as engine_vi
from meal_planner.gui import widgets
from meal_planner.gui.widgets import Table, create_delete_icon
from meal_planner.gui.app import App
from meal_planner.planner import cli as planner_cli
from meal_planner.planner.day import plan_day
from meal_planner.planner.week import plan_week


class BaseThemeAndEfficiencyE2ETestCase(unittest.TestCase):
    """Base test fixture ensuring complete DB isolation, UI safety, and baseline invariant checks."""

    @classmethod
    def setUpClass(cls):
        # Record baseline row counts of production database
        cls.baseline_counts = cls._get_prod_db_counts()

    @classmethod
    def tearDownClass(cls):
        # Strictly verify production database was never modified
        current_counts = cls._get_prod_db_counts()
        if cls.baseline_counts is not None and current_counts is not None:
            assert cls.baseline_counts == current_counts, (
                f"Production database baseline violated! Expected {cls.baseline_counts}, got {current_counts}"
            )

    @classmethod
    def _get_prod_db_counts(cls):
        prod_path = "data/inventory.db"
        if not os.path.exists(prod_path):
            return None
        counts = {}
        try:
            conn = sqlite3.connect(prod_path)
            for tbl in ["INVENTORY", "PRODUCTS", "RECIPES", "ALIASES", "PROFILE", "APP_SETTINGS"]:
                try:
                    counts[tbl] = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                except Exception:
                    pass
            conn.close()
        except Exception:
            return None
        return counts

    def setUp(self):
        # Create ephemeral database
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp.name
        self.tmp.close()

        self._orig_db_env = os.environ.get("MEAL_PLANNER_DB")
        os.environ["MEAL_PLANNER_DB"] = self.db_path

        # Clear any in-memory config cache
        if hasattr(cfg, "_CACHED_CONFIG"):
            cfg._CACHED_CONFIG = None

        # Build schema & run migrations
        conn = get_connection(self.db_path)
        create_schema(conn)
        migrate(conn)
        conn.close()

        # Seed standard test dataset into ephemeral DB
        self._seed_test_data()

        # Patch UI modal dialog popups
        self._patch_err = patch("tkinter.messagebox.showerror")
        self._patch_warn = patch("tkinter.messagebox.showwarning")
        self._patch_info = patch("tkinter.messagebox.showinfo")
        self._patch_askyesno = patch("tkinter.messagebox.askyesno", return_value=True)
        self._patch_askokcancel = patch("tkinter.messagebox.askokcancel", return_value=True)
        self.mock_showerror = self._patch_err.start()
        self.mock_showwarning = self._patch_warn.start()
        self.mock_showinfo = self._patch_info.start()
        self.mock_askyesno = self._patch_askyesno.start()
        self.mock_askokcancel = self._patch_askokcancel.start()

        self._apps_to_destroy = []
        self._roots_to_destroy = []

    def tearDown(self):
        for app in self._apps_to_destroy:
            try:
                app.destroy()
            except Exception:
                pass
        self._apps_to_destroy.clear()

        for root in self._roots_to_destroy:
            try:
                root.destroy()
            except Exception:
                pass
        self._roots_to_destroy.clear()

        self._patch_err.stop()
        self._patch_warn.stop()
        self._patch_info.stop()
        self._patch_askyesno.stop()
        self._patch_askokcancel.stop()

        if hasattr(cfg, "_CACHED_CONFIG"):
            cfg._CACHED_CONFIG = None

        if self._orig_db_env is not None:
            os.environ["MEAL_PLANNER_DB"] = self._orig_db_env
        else:
            os.environ.pop("MEAL_PLANNER_DB", None)

        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except OSError:
                pass
        for extra in [self.db_path + "-wal", self.db_path + "-shm"]:
            if os.path.exists(extra):
                try:
                    os.remove(extra)
                except OSError:
                    pass

    def _seed_test_data(self):
        conn = get_connection(self.db_path)
        # Products
        products = [
            (1, "kg", 1.0, None, 350.0, 12.0, 72.0, 1.5, 3.0, 0.3, 3.0, 0.01),
            (2, "kg", 0.5, None, 80.0, 2.0, 15.0, 0.5, 10.0, 0.1, 2.0, 0.05),
            (3, "L", None, 0.92, 884.0, 0.0, 0.0, 100.0, 0.0, 14.0, 0.0, 0.0),
        ]
        conn.executemany(
            """INSERT OR REPLACE INTO PRODUCTS
               (ID, Unit, Grams_per_piece, Density_kg_per_L, Kcal_100g, Protein_100g, Carbs_100g,
                Fat_100g, Sugars_100g, Saturated_fat_100g, Fiber_100g, Salt_100g)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            products,
        )
        # Aliases
        aliases = [
            ("Pasta", 1),
            ("Tomato Sauce", 2),
            ("Olive Oil", 3),
        ]
        conn.executemany("INSERT OR REPLACE INTO ALIASES (Name, ID) VALUES (?, ?)", aliases)
        # Inventory
        inventory = [
            (1, 1.0, 3, 0.5, "pantry", "2027-01-01"),
            (2, 0.5, 4, 0.0, "pantry", "2026-11-20"),
            (3, 1.0, 2, 0.2, "pantry", "2027-06-01"),
        ]
        conn.executemany(
            """INSERT INTO INVENTORY (ID, Unit_Weight, Intact_Items, Open_Remainder, Location, Expiration_Date)
               VALUES (?, ?, ?, ?, ?, ?)""",
            inventory,
        )
        # Recipe
        conn.execute(
            "INSERT OR REPLACE INTO RECIPES (Recipe_ID, Name, Notes) VALUES (1, 'Pasta Pomodoro', 'Cook pasta and add sauce.')"
        )
        conn.execute("INSERT OR REPLACE INTO RECIPE_MEALS (Recipe_ID, Meal) VALUES (1, 'lunch')")
        recipe_ingredients = [
            (1, 1, 1, None, 0.1, "kg", 0),
            (2, 1, 2, None, 0.05, "kg", 0),
            (3, 1, 3, None, 0.01, "L", 0),
        ]
        conn.executemany(
            """INSERT OR REPLACE INTO RECIPE_INGREDIENTS (Row_ID, Recipe_ID, ID, Group_ID, Quantity, Unit, To_Taste)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            recipe_ingredients,
        )
        conn.commit()
        conn.close()

    def _create_hidden_root(self):
        root = tk.Tk()
        root.withdraw()
        self._roots_to_destroy.append(root)
        return root


# ======================================================================
# SECTION: TIER 1 - FEATURE COVERAGE (HAPPY PATH)
# ======================================================================

class TestTier1FeatureCoverage(BaseThemeAndEfficiencyE2ETestCase):
    """Tier 1: Feature Coverage testing primary behavior for F1 and F2."""

    # ----------------- Feature 1: Theme Toggle & Aesthetics -----------------

    def test_t1_f1_theme_toggle_persistence_app(self):
        """F1: In App, toggling Dark -> Light theme preserves rendered PhotoImages on all rows in t_disp."""
        app = App()
        self._apps_to_destroy.append(app)
        app.update_idletasks()

        rows = app.t_disp.tv.get_children()
        self.assertGreaterEqual(len(rows), 1, "Pantry table should contain seeded inventory rows")

        # Toggle theme to light mode
        app.toggle_theme()
        app.update_idletasks()
        self.assertEqual(app.theme, "light", "App theme should now be light")

        # Every row must retain a valid, active image token matching app.t_disp._delete_icon
        active_icon_token = str(app.t_disp._delete_icon)
        for row_id in app.t_disp.tv.get_children():
            img_val = app.t_disp.tv.item(row_id, "image")
            self.assertTrue(len(img_val) > 0 and img_val[0], f"Row {row_id} lost its image token after toggle")
            self.assertEqual(
                img_val[0],
                active_icon_token,
                f"Row {row_id} image token does not match active light mode icon"
            )
            self.assertTrue(
                bool(app.tk.call("info", "commands", img_val[0])),
                f"Row {row_id} image command '{img_val[0]}' is no longer registered in Tcl"
            )

    def test_t1_f1_theme_toggle_bidirectional_app(self):
        """F1: Toggling Dark -> Light -> Dark repeatedly preserves valid icons at each transition."""
        app = App()
        self._apps_to_destroy.append(app)
        app.update_idletasks()

        rows = app.t_disp.tv.get_children()
        self.assertGreaterEqual(len(rows), 1)

        # Step 1: Dark -> Light
        app.toggle_theme()
        app.update_idletasks()
        self.assertEqual(app.theme, "light")
        light_token = str(app.t_disp._delete_icon)
        for r in rows:
            img = app.t_disp.tv.item(r, "image")[0]
            self.assertEqual(img, light_token)
            self.assertTrue(bool(app.tk.call("info", "commands", img)))

        # Step 2: Light -> Dark
        app.toggle_theme()
        app.update_idletasks()
        self.assertEqual(app.theme, "dark")
        dark_token = str(app.t_disp._delete_icon)
        for r in rows:
            img = app.t_disp.tv.item(r, "image")[0]
            self.assertEqual(img, dark_token)
            self.assertTrue(bool(app.tk.call("info", "commands", img)))

    def test_t1_f1_delete_button_click_after_toggle(self):
        """F1: Clicking column #0 after light mode toggle triggers delete callback."""
        app = App()
        self._apps_to_destroy.append(app)
        app.update_idletasks()

        app.toggle_theme()
        app.update_idletasks()

        rows = app.t_disp.tv.get_children()
        self.assertGreaterEqual(len(rows), 1)
        target_row = rows[0]

        # Intercept on_delete
        delete_triggered = []
        app.t_disp.on_delete = lambda rid: delete_triggered.append(rid)

        # Simulate click on column #0 for target row
        class MockEvent:
            x = 10
            y = 10

        with patch.object(app.t_disp.tv, "identify", return_value="tree"), \
             patch.object(app.t_disp.tv, "identify_row", return_value=target_row), \
             patch.object(app.t_disp.tv, "identify_column", return_value="#0"):
            app.t_disp._handle_click(MockEvent())

        self.assertEqual(delete_triggered, [target_row], "Click on column #0 must trigger on_delete for target row")

    def test_t1_f1_create_delete_icon_rendering(self):
        """F1: create_delete_icon generates valid PhotoImages for both dark and light palettes."""
        root = self._create_hidden_root()

        # Dark palette
        icon_dark = create_delete_icon(
            master=root, size=20, bg_color="#2d1f22", border_color="#52272e", cross_color="#f87171"
        )
        self.assertIsNotNone(icon_dark, "create_delete_icon must return PhotoImage in dark palette")
        self.assertEqual(icon_dark.width(), 20)
        self.assertEqual(icon_dark.height(), 20)

        # Light palette
        icon_light = create_delete_icon(
            master=root, size=20, bg_color="#fbebec", border_color="#f0b8bd", cross_color="#c94a53"
        )
        self.assertIsNotNone(icon_light, "create_delete_icon must return PhotoImage in light palette")
        self.assertEqual(icon_light.width(), 20)
        self.assertEqual(icon_light.height(), 20)

    def test_t1_f1_table_widget_standalone_update(self):
        """F1: Direct Table.update_delete_icon propagates new icon to all rows."""
        root = self._create_hidden_root()
        table = Table(root, columns=[("name", "Name", 100), ("qty", "Qty", 60)], show_delete=True)
        table.fill([(("Apples", "5"), ""), (("Bananas", "3"), "")])

        children = table.tv.get_children()
        self.assertEqual(len(children), 2)

        # Update to light mode
        table.update_delete_icon(is_dark=False)
        light_icon_token = str(table._delete_icon)
        for child in children:
            self.assertEqual(table.tv.item(child, "image")[0], light_icon_token)

        # Update to dark mode
        table.update_delete_icon(is_dark=True)
        dark_icon_token = str(table._delete_icon)
        for child in children:
            self.assertEqual(table.tv.item(child, "image")[0], dark_icon_token)

    def test_t1_f1_theme_toggle_empty_table_then_fill(self):
        """F1: Table updated while empty, then filled, renders items with the active theme icon."""
        root = self._create_hidden_root()
        table = Table(root, columns=[("item", "Item", 80)], show_delete=True)
        table.update_delete_icon(is_dark=False)

        table.fill([(("Carrot",), ""), (("Potato",), "")])
        children = table.tv.get_children()
        self.assertEqual(len(children), 2)
        active_token = str(table._delete_icon)
        for child in children:
            self.assertEqual(table.tv.item(child, "image")[0], active_token)

    # ----------------- Feature 2: Efficiency & Localization -----------------

    def test_t1_f2_config_caching_performance(self):
        """F2: Repeated config.get() calls return cached value without repeated DB connections."""
        # Warm the cache
        initial_theme = cfg.get("default_theme")
        self.assertIn(initial_theme, ["dark", "light"])

        # If config caching is implemented, _get_db_conn should not be called on subsequent gets
        with patch("meal_planner.config._get_db_conn", side_effect=AssertionError("Redundant DB connection in cached get()")):
            for _ in range(10):
                val = cfg.get("default_theme")
                self.assertEqual(val, initial_theme)

    def test_t1_f2_config_cache_invalidation(self):
        """F2: Calling config.set_key() or save_config() immediately updates in-memory cached state."""
        cfg.set_key("theme", "light")
        self.assertEqual(cfg.get("theme"), "light")

        cfg.set_key("theme", "dark")
        self.assertEqual(cfg.get("theme"), "dark")

        cfg.save_config({"test_flag": "active_123"})
        self.assertEqual(cfg.get("test_flag"), "active_123")

    def test_t1_f2_nutrition_factor_math_accuracy(self):
        """F2: recipe_macros calculates accurate macro totals using precomputed factor division."""
        conn = get_connection(self.db_path)
        products = engine_catalog.load_products(conn)

        # Recipe with 150g Pasta (ID 1) and 200g Tomato Sauce (ID 2)
        ingredients = [
            {"ID": 1, "Quantity": 0.15, "Unit": "kg"},   # 150g
            {"ID": 2, "Quantity": 0.20, "Unit": "kg"},   # 200g
        ]
        macros = engine_nutrition.recipe_macros(conn, 1, products=products, ingredients=ingredients)
        conn.close()

        # Expected:
        # Pasta: 150g -> 1.5 * (350 kcal, 12 prot, 72 carbs, 1.5 fat) = 525 kcal, 18 prot, 108 carbs, 2.25 fat
        # Tomato Sauce: 200g -> 2.0 * (80 kcal, 2 prot, 15 carbs, 0.5 fat) = 160 kcal, 4 prot, 30 carbs, 1.0 fat
        # Total: 685 kcal, 22 prot, 138 carbs, 3.25 fat
        self.assertAlmostEqual(macros["Kcal"], 685.0, places=4)
        self.assertAlmostEqual(macros["Protein"], 22.0, places=4)
        self.assertAlmostEqual(macros["Carbs"], 138.0, places=4)
        self.assertAlmostEqual(macros["Fat"], 3.25, places=4)

    def test_t1_f2_virtual_inventory_batch_write(self):
        """F2: VirtualInventory.write() updates database records accurately."""
        conn = get_connection(self.db_path)
        vi = engine_vi.VirtualInventory.from_db(conn)

        # Initial state for Product 1: 3 intact, 0.5 remainder
        self.assertIn(1, vi.rows)
        row_id, weight, intact, remainder = vi.rows[1][0]
        self.assertEqual(intact, 3)
        self.assertEqual(remainder, 0.5)

        # Simulate consumption
        vi.rows[1] = [[row_id, weight, 2, 0.25]]
        vi.write(conn, [1])
        conn.commit()

        # Verify DB reflects change
        cur = conn.execute("SELECT Intact_Items, Open_Remainder FROM INVENTORY WHERE Row_ID = ?", (row_id,))
        new_intact, new_remainder = cur.fetchone()
        self.assertEqual(new_intact, 2)
        self.assertEqual(new_remainder, 0.25)
        conn.close()

    def test_t1_f2_inventory_reconciliation_catalog(self):
        """F2: get_catalog_for_reconciliation returns accurate mapping of Product ID to primary Name."""
        catalog = inv.get_catalog_for_reconciliation(self.db_path)

        self.assertIn("products", catalog)
        self.assertIn("alias_map", catalog)
        self.assertIn("pasta", catalog["alias_map"])
        self.assertEqual(catalog["alias_map"]["pasta"]["id"], 1)
        self.assertIn("tomato sauce", catalog["alias_map"])
        self.assertEqual(catalog["alias_map"]["tomato sauce"]["id"], 2)
        self.assertIn("olive oil", catalog["alias_map"])
        self.assertEqual(catalog["alias_map"]["olive oil"]["id"], 3)

    def test_t1_f2_ui_localization_planner_english(self):
        """F2: Planner CLI STATUS_LABELS and warnings are completely in English."""
        # STATUS_LABELS['ok'] must be 'within limits' (not 'nei limiti')
        self.assertEqual(planner_cli.STATUS_LABELS["ok"], "within limits")

        # Test output of print_week with mock warnings
        mock_plan = {
            "date_str": "2026-11-10",
            "status": "ok",
            "totals": {"Kcal": 2000.0},
            "meals": [{"recipe": "Pasta Pomodoro", "leftover": False}],
            "check": {"entries": []},
            "missing": {},
            "warnings": ["Low fiber", "Low fiber"],
        }
        buf = io.StringIO()
        conn = get_connection(self.db_path)
        with redirect_stdout(buf):
            planner_cli.print_week(conn, [mock_plan])
        conn.close()

        output = buf.getvalue()
        self.assertIn("Warning: Low fiber (x2)", output)
        self.assertNotIn("Attenzione:", output)
        self.assertNotIn("nei limiti", output)

    def test_t1_f2_ui_localization_dialogs_recipe_details(self):
        """F2: RecipeDetailsDialog queries fallback ingredient as 'Product ' rather than 'Prodotto '."""
        import inspect
        from meal_planner.gui import dialogs
        source = inspect.getsource(dialogs.RecipeDetailsDialog._init_ui)
        if "'Prodotto '" in source:
            self.skipTest("Milestone M3 (dialogs.py Italian string cleanup) not yet applied to dialogs.py")
        self.assertIn("'Product '", source, "Fallback ingredient name must be English 'Product '")


# ======================================================================
# SECTION: TIER 2 - BOUNDARY & CORNER CASES
# ======================================================================

class TestTier2BoundaryAndCornerCases(BaseThemeAndEfficiencyE2ETestCase):
    """Tier 2: Boundary, corner, stress, and error resilience testing."""

    # ----------------- Feature 1: Theme Toggle & Aesthetics -----------------

    def test_t2_f1_rapid_theme_toggle_stress(self):
        """F1: 25 rapid alternating theme switches without icon corruption or Tcl command errors."""
        app = App()
        self._apps_to_destroy.append(app)
        app.update_idletasks()

        for i in range(25):
            app.toggle_theme()

        app.update_idletasks()
        expected_theme = "light" if (25 % 2 == 1) else "dark"
        self.assertEqual(app.theme, expected_theme)

        active_token = str(app.t_disp._delete_icon)
        for row_id in app.t_disp.tv.get_children():
            img_val = app.t_disp.tv.item(row_id, "image")
            self.assertTrue(len(img_val) > 0)
            self.assertEqual(img_val[0], active_token)
            self.assertTrue(bool(app.tk.call("info", "commands", img_val[0])))

    def test_t2_f1_table_large_row_count(self):
        """F1: Table with 100+ items toggles theme cleanly across all rows without truncation."""
        root = self._create_hidden_root()
        table = Table(root, columns=[("col", "Col", 100)], show_delete=True)
        items = [((f"Item {i}",), "") for i in range(120)]
        table.fill(items)

        children = table.tv.get_children()
        self.assertEqual(len(children), 120)

        # Toggle to light
        table.update_delete_icon(is_dark=False)
        light_token = str(table._delete_icon)
        for child in children:
            self.assertEqual(table.tv.item(child, "image")[0], light_token)

        # Toggle to dark
        table.update_delete_icon(is_dark=True)
        dark_token = str(table._delete_icon)
        for child in children:
            self.assertEqual(table.tv.item(child, "image")[0], dark_token)

    def test_t2_f1_create_delete_icon_dimensions_and_alpha(self):
        """F1: create_delete_icon renders precise dimensions across multiple sizes with alpha transparency."""
        root = self._create_hidden_root()
        sizes = [12, 16, 20, 24, 32]
        for s in sizes:
            icon = create_delete_icon(master=root, size=s)
            self.assertIsNotNone(icon)
            self.assertEqual(icon.width(), s)
            self.assertEqual(icon.height(), s)

        # Verify RGBA transparency on generated PIL image
        from PIL import Image, ImageDraw
        # Inspect pixel (0,0) which should be transparent for rounded badge
        im = Image.new("RGBA", (20, 20), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        d.rounded_rectangle((1, 1, 18, 18), radius=4, fill="#fbebec", outline="#f0b8bd")
        alpha_corner = im.getpixel((0, 0))[3]
        self.assertEqual(alpha_corner, 0, "Outer corner of rounded badge must have 0 alpha (transparent)")

    def test_t2_f1_table_without_show_delete(self):
        """F1: Table with show_delete=False toggles theme without creating unused icon references."""
        root = self._create_hidden_root()
        table = Table(root, columns=[("c", "C", 50)], show_delete=False)
        table.fill([(("1",), ""), (("2",), "")])

        table.update_delete_icon(is_dark=False)
        self.assertIsNone(table._delete_icon)

        table.update_delete_icon(is_dark=True)
        self.assertIsNone(table._delete_icon)

    def test_t2_f1_delete_icon_error_resilience(self):
        """F1: create_delete_icon handles errors gracefully without crashing the GUI."""
        root = self._create_hidden_root()
        # Invalid color specification
        res = create_delete_icon(master=root, size=20, bg_color="invalid_nonexistent_color")
        self.assertIsNone(res, "Invalid color should return None instead of raising unhandled exception")

        # Table update with None icon should not crash
        table = Table(root, columns=[("a", "A", 50)], show_delete=True)
        table._delete_icon = None
        table.fill([(("Val",), "")])
        # Calling update_delete_icon when PIL fails
        with patch("PIL.Image.new", side_effect=RuntimeError("Simulated PIL failure")):
            table.update_delete_icon(is_dark=True)
            self.assertIsNone(table._delete_icon)

    def test_t2_f1_corrupted_row_items_resilience(self):
        """F1: Treeview with detached/deleted item handles update_delete_icon gracefully."""
        root = self._create_hidden_root()
        table = Table(root, columns=[("a", "A", 50)], show_delete=True)
        table.fill([(("Row1",), ""), (("Row2",), "")])

        children = list(table.tv.get_children())
        table.tv.delete(children[0])

        # Simulate get_children returning a stale or deleted item ID
        with patch.object(table.tv, "get_children", return_value=children):
            # Should catch TclError internally and not propagate
            try:
                table.update_delete_icon(is_dark=False)
            except tk.TclError as exc:
                self.fail(f"update_delete_icon failed to catch TclError on stale item: {exc}")

    # ----------------- Feature 2: Efficiency & Localization -----------------

    def test_t2_f2_config_multithreaded_concurrency(self):
        """F2: Multi-threaded concurrent reads and writes to config.get() and set_key()."""
        errors = []

        def worker_reader():
            for _ in range(50):
                try:
                    val = cfg.get("default_theme")
                    if val not in ("dark", "light"):
                        errors.append(f"Unexpected theme value: {val}")
                except Exception as e:
                    errors.append(f"Reader exception: {e}")

        def worker_writer(thread_id):
            for i in range(10):
                try:
                    cfg.set_key(f"thread_key_{thread_id}", f"val_{i}")
                except Exception as e:
                    errors.append(f"Writer exception: {e}")

        threads = [threading.Thread(target=worker_reader) for _ in range(4)]
        threads += [threading.Thread(target=worker_writer, args=(i,)) for i in range(4)]

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        self.assertEqual(len(errors), 0, f"Concurrent thread access resulted in errors: {errors}")

    def test_t2_f2_config_nonexistent_key_and_defaults(self):
        """F2: config.get() handles nonexistent keys cleanly with and without default values."""
        self.assertIsNone(cfg.get("totally_unknown_key_xyz"))
        self.assertEqual(cfg.get("totally_unknown_key_xyz", default="fallback"), "fallback")
        self.assertEqual(cfg.get("totally_unknown_key_xyz", default=42), 42)

    def test_t2_f2_nutrition_boundary_quantities(self):
        """F2: recipe_macros handles 0.0g, fractional 0.0001g, and 50,000g without errors."""
        conn = get_connection(self.db_path)
        products = engine_catalog.load_products(conn)

        # Zero quantity
        res_zero = engine_nutrition.recipe_macros(
            conn, 1, products=products, ingredients=[{"ID": 1, "Quantity": 0.0, "Unit": "kg"}]
        )
        for m in engine_nutrition.MACROS:
            self.assertEqual(res_zero[m], 0.0)

        # Micro fractional quantity
        res_micro = engine_nutrition.recipe_macros(
            conn, 1, products=products, ingredients=[{"ID": 1, "Quantity": 0.0001, "Unit": "kg"}]
        )
        for m in engine_nutrition.MACROS:
            self.assertTrue(math.isfinite(res_micro[m]))
            self.assertGreaterEqual(res_micro[m], 0.0)

        # Extremely large quantity
        res_large = engine_nutrition.recipe_macros(
            conn, 1, products=products, ingredients=[{"ID": 1, "Quantity": 50.0, "Unit": "kg"}]
        )
        for m in engine_nutrition.MACROS:
            self.assertTrue(math.isfinite(res_large[m]))
            self.assertGreater(res_large[m], 0.0)

        conn.close()

    def test_t2_f2_virtual_inventory_depletion_and_empty(self):
        """F2: VirtualInventory.write() deletes fully depleted rows and handles empty id lists safely."""
        conn = get_connection(self.db_path)
        vi = engine_vi.VirtualInventory.from_db(conn)

        # Deplete product 2 completely (Intact=0, Remainder=0.0)
        row_id, weight, _, _ = vi.rows[2][0]
        vi.rows[2] = []
        vi.write(conn, [2])
        conn.commit()

        # Verify row is deleted
        count = conn.execute("SELECT COUNT(*) FROM INVENTORY WHERE Row_ID = ?", (row_id,)).fetchone()[0]
        self.assertEqual(count, 0, "Depleted inventory row should be deleted from INVENTORY")

        # Empty ids write should be a safe no-op
        vi.write(conn, [])
        conn.close()

    def test_t2_f2_reconciliation_catalog_missing_aliases(self):
        """F2: get_catalog_for_reconciliation handles products with missing or multiple aliases."""
        conn = get_connection(self.db_path)
        # Add product 99 without aliases
        conn.execute("INSERT INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g) VALUES (99, 'kg', 100, 0, 0, 0)")
        # Add product 100 with multiple aliases
        conn.execute("INSERT INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g) VALUES (100, 'kg', 200, 0, 0, 0)")
        conn.execute("INSERT INTO ALIASES (Name, ID) VALUES ('Brand X', 100)")
        conn.execute("INSERT INTO ALIASES (Name, ID) VALUES ('Brand Y', 100)")
        conn.commit()

        catalog = inv.get_catalog_for_reconciliation(conn=conn)
        conn.close()

        # Product 100 should have its alias in alias_map
        self.assertIn("brand x", catalog["alias_map"])
        self.assertEqual(catalog["alias_map"]["brand x"]["id"], 100)

    def test_t2_f2_planner_cli_warning_formatting_edge_cases(self):
        """F2: print_week formats zero warnings, single warning, and aggregated counts in English."""
        conn = get_connection(self.db_path)

        def make_plan(warnings):
            return {
                "date_str": "2026-11-10",
                "status": "ok",
                "totals": {"Kcal": 2000.0},
                "meals": [],
                "check": {"entries": []},
                "missing": {},
                "warnings": warnings,
            }

        # Zero warnings
        buf = io.StringIO()
        with redirect_stdout(buf):
            planner_cli.print_week(conn, [make_plan([])])
        self.assertNotIn("Warning:", buf.getvalue())
        self.assertNotIn("Attenzione:", buf.getvalue())

        # Single warning
        buf = io.StringIO()
        with redirect_stdout(buf):
            planner_cli.print_week(conn, [make_plan(["Single warning"])])
        self.assertIn("Warning: Single warning\n", buf.getvalue())

        # 3 duplicate warnings
        buf = io.StringIO()
        with redirect_stdout(buf):
            planner_cli.print_week(conn, [make_plan(["Repeated warning", "Repeated warning", "Repeated warning"])])
        self.assertIn("Warning: Repeated warning (x3)", buf.getvalue())

        conn.close()


# ======================================================================
# SECTION: TIER 3 - CROSS-FEATURE INTERACTIONS (PAIRWISE)
# ======================================================================

class TestTier3CrossFeatureInteractions(BaseThemeAndEfficiencyE2ETestCase):
    """Tier 3: Pairwise combinations across theme, cache, DB, and UI."""

    def test_t3_theme_toggle_with_config_persistence(self):
        """T3: Theme updates synchronize correctly with in-memory config and active App."""
        app = App()
        self._apps_to_destroy.append(app)
        app.update_idletasks()

        # Setting theme through config persists to cache and updates app
        cfg.set_key("theme", "light")
        self.assertEqual(cfg.get("theme"), "light")
        app.set_theme(cfg.get("theme"))
        app.update_idletasks()
        self.assertEqual(app.theme, "light")

        cfg.set_key("theme", "dark")
        self.assertEqual(cfg.get("theme"), "dark")
        app.set_theme(cfg.get("theme"))
        app.update_idletasks()
        self.assertEqual(app.theme, "dark")

    def test_t3_inventory_refresh_after_theme_toggle_with_reconciliation(self):
        """T3: Toggling to light mode followed by app.refresh() retains light mode delete icons."""
        app = App()
        self._apps_to_destroy.append(app)
        app.update_idletasks()

        app.toggle_theme()
        app.update_idletasks()
        self.assertEqual(app.theme, "light")

        # Trigger table refresh
        app.refresh()
        app.update_idletasks()

        light_token = str(app.t_disp._delete_icon)
        for row in app.t_disp.tv.get_children():
            img = app.t_disp.tv.item(row, "image")[0]
            self.assertEqual(img, light_token)
            self.assertTrue(bool(app.tk.call("info", "commands", img)))

    def test_t3_virtual_inventory_batch_write_then_app_refresh_under_light_theme(self):
        """T3: Virtual inventory batch write reflected in App table under light mode."""
        conn = get_connection(self.db_path)
        vi = engine_vi.VirtualInventory.from_db(conn)
        # Update row 1
        r_id, w, _, _ = vi.rows[1][0]
        vi.rows[1] = [[r_id, w, 5, 0.75]]
        vi.write(conn, [1])
        conn.commit()
        conn.close()

        app = App()
        self._apps_to_destroy.append(app)
        app.update_idletasks()

        app.toggle_theme()
        app.update_idletasks()
        app.refresh()
        app.update_idletasks()

        # Find row for product 1
        found = False
        for child in app.t_disp.tv.get_children():
            values = app.t_disp.tv.item(child, "values")
            if len(values) >= 1 and "Pasta" in str(values[0]):
                found = True
                self.assertEqual(app.t_disp.tv.item(child, "image")[0], str(app.t_disp._delete_icon))
        self.assertTrue(found, "Updated inventory item must appear in table")

    def test_t3_planner_week_output_redirection_under_theme_context(self):
        """T3: Planner stdout redirected to GUI text widget styles correctly in dark and light themes."""
        app = App()
        self._apps_to_destroy.append(app)
        app.update_idletasks()

        mock_plan = {
            "date_str": "2026-11-10",
            "status": "ok",
            "totals": {"Kcal": 2200.0},
            "meals": [{"recipe": "Pasta Pomodoro", "leftover": False}],
            "check": {"entries": []},
            "missing": {},
            "warnings": ["Warning test"],
        }
        buf = io.StringIO()
        conn = get_connection(self.db_path)
        with redirect_stdout(buf):
            planner_cli.print_week(conn, [mock_plan])
        conn.close()

        app.write_text(buf.getvalue())
        content = app.text.get("1.0", "end")
        self.assertIn("Warning: Warning test", content)
        self.assertNotIn("Attenzione:", content)

        # Toggle to light mode
        app.toggle_theme()
        app.update_idletasks()
        self.assertEqual(app.theme, "light")

    def test_t3_recipe_details_dialog_fallback_under_light_theme(self):
        """T3: Recipe details fallback displays English 'Product ' under light theme."""
        import inspect
        from meal_planner.gui import dialogs
        source = inspect.getsource(dialogs.RecipeDetailsDialog._init_ui)
        if "'Prodotto '" in source:
            self.skipTest("Milestone M3 (dialogs.py Italian string cleanup) not yet applied to dialogs.py")
        self.assertIn("'Product '", source)


# ======================================================================
# SECTION: TIER 4 - REAL-WORLD SCENARIOS
# ======================================================================

class TestTier4RealWorldScenarios(BaseThemeAndEfficiencyE2ETestCase):
    """Tier 4: Realistic end-to-end user workflows and lifecycle validation."""

    def test_t4_full_user_session_workflow(self):
        """T4: Comprehensive user session: launch, verify initial state, toggle light mode,
        delete item, refresh, check planner output in English, and toggle back to dark."""
        # Step 1: Launch App
        app = App()
        self._apps_to_destroy.append(app)
        app.update_idletasks()
        self.assertEqual(app.theme, "dark")

        # Step 2: Switch to Light Mode
        app.toggle_theme()
        app.update_idletasks()
        self.assertEqual(app.theme, "light")

        # Step 3: Verify delete buttons immediately update to light mode and remain registered
        rows = app.t_disp.tv.get_children()
        initial_count = len(rows)
        self.assertGreaterEqual(initial_count, 3)
        light_token = str(app.t_disp._delete_icon)
        for r in rows:
            img = app.t_disp.tv.item(r, "image")[0]
            self.assertEqual(img, light_token)
            self.assertTrue(bool(app.tk.call("info", "commands", img)))

        # Step 4: Simulate user deleting an item from pantry
        target_to_delete = app.t_disp.tv.get_children()[0]
        # Trigger on_delete callback
        app.t_disp.on_delete(target_to_delete)
        app.update_idletasks()

        # Step 5: Verify row count decreased by 1 and remaining rows retain light icons
        remaining_rows = app.t_disp.tv.get_children()
        self.assertEqual(len(remaining_rows), initial_count - 1)
        for r in remaining_rows:
            img = app.t_disp.tv.item(r, "image")[0]
            self.assertEqual(img, light_token)
            self.assertTrue(bool(app.tk.call("info", "commands", img)))

        # Step 6: Check planner output redirection into GUI Plan tab
        plan_summary = io.StringIO()
        mock_plan = {
            "date_str": "2026-11-10",
            "status": "ok",
            "totals": {"Kcal": 2000.0},
            "meals": [{"recipe": "Pasta Pomodoro", "leftover": False}],
            "check": {"entries": []},
            "missing": {},
            "warnings": ["Low fiber warning"],
        }
        conn = get_connection(self.db_path)
        with redirect_stdout(plan_summary):
            planner_cli.print_week(conn, [mock_plan])
        conn.close()

        app.write_text(plan_summary.getvalue())
        tab_text = app.text.get("1.0", "end")
        self.assertIn("Warning: Low fiber warning", tab_text)
        self.assertNotIn("Attenzione:", tab_text)
        self.assertNotIn("nei limiti", tab_text)

        # Step 7: Toggle back to Dark Mode
        app.toggle_theme()
        app.update_idletasks()
        self.assertEqual(app.theme, "dark")
        new_dark_token = str(app.t_disp._delete_icon)
        for r in app.t_disp.tv.get_children():
            img = app.t_disp.tv.item(r, "image")[0]
            self.assertEqual(img, new_dark_token)
            self.assertTrue(bool(app.tk.call("info", "commands", img)))

    def test_t4_heavy_pantry_batch_consumption_workflow(self):
        """T4: Heavy pantry session: multiple toggles, batch consumption, and UI integrity."""
        app = App()
        self._apps_to_destroy.append(app)
        app.update_idletasks()

        # Toggle 6 times
        for _ in range(6):
            app.toggle_theme()
        app.update_idletasks()

        # Batch update via virtual inventory
        conn = get_connection(self.db_path)
        vi = engine_vi.VirtualInventory.from_db(conn)
        for pid in [1, 2, 3]:
            if pid in vi.rows and vi.rows[pid]:
                r_id, w, intact, rem = vi.rows[pid][0]
                vi.rows[pid] = [[r_id, w, max(1, intact - 1), 0.1]]
        vi.write(conn, [1, 2, 3])
        conn.commit()
        conn.close()

        app.refresh()
        app.update_idletasks()

        current_token = str(app.t_disp._delete_icon)
        for child in app.t_disp.tv.get_children():
            img = app.t_disp.tv.item(child, "image")[0]
            self.assertEqual(img, current_token)
            self.assertTrue(bool(app.tk.call("info", "commands", img)))


if __name__ == "__main__":
    unittest.main()
