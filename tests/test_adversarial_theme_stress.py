"""
tests/test_adversarial_theme_stress.py

Adversarial Stress Test Suite for Milestone 1:
- Rapid alternating theme toggles (50+ switches, testing 100)
- Boundary scaling: 0 rows, 1 row, 500+ rows, dynamic row mutations
- Delete click callbacks before, during, and after theme toggles
- Tcl image token lifecycle and memory leak analysis
- Resilience under invalid/corrupted icon parameters
"""

import gc
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock

import tkinter as tk
from tkinter import ttk
from PIL import Image, ImageTk

from meal_planner.gui import widgets
from meal_planner.gui.widgets import Table, create_delete_icon


class HeadlessTkTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
            cls.root.withdraw()
        except tk.TclError:
            raise unittest.SkipTest("Tkinter display not available")

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:
            pass

    def setUp(self):
        self.frame = ttk.Frame(self.root)
        self.frame.pack()

    def tearDown(self):
        try:
            self.frame.destroy()
        except Exception:
            pass
        gc.collect()


class TestThemeToggleStress(HeadlessTkTestCase):
    def test_rapid_alternating_theme_toggles_100_cycles(self):
        """Stress-test 100 rapid alternating dark/light toggles with active table rows."""
        columns = [("name", "Product", 100), ("qty", "Quantity", 50)]
        table = Table(self.frame, columns, height=10, show_delete=True, is_dark=False)
        table.pack()

        rows = [
            ([f"Item {i}", f"{i * 10}g"], "ok", str(i))
            for i in range(25)
        ]
        table.fill(rows)

        start_time = time.perf_counter()
        switches = 100
        for i in range(switches):
            is_dark = (i % 2 == 1)
            table.update_delete_icon(is_dark=is_dark)

            # Invariant: delete icon must be valid PhotoImage
            self.assertIsNotNone(table._delete_icon, f"Delete icon is None at toggle {i}")
            icon_name = str(table._delete_icon)
            img_type = self.root.tk.call("image", "type", icon_name)
            self.assertEqual(img_type, "photo", f"Tcl image {icon_name} invalid type at toggle {i}")

            # Invariant: Every child item must have valid image assigned
            children = table.tv.get_children("")
            self.assertEqual(len(children), 25)
            for child in children:
                item_img = table.tv.item(child, "image")
                # In Tkinter, item image may be tuple or string representation
                assigned_img = item_img[0] if isinstance(item_img, (tuple, list)) else item_img
                self.assertEqual(assigned_img, icon_name, f"Row {child} desynchronized at toggle {i}")

        elapsed = time.perf_counter() - start_time
        # Performance check: 100 toggles across 25 rows should complete in under 5.0 seconds
        self.assertLess(elapsed, 5.0, f"Rapid toggle too slow: took {elapsed:.2f}s for {switches} toggles")

    def test_table_scaling_0_1_500_rows(self):
        """Stress-test theme toggles across boundary sizes: 0 rows, 1 row, and 500 rows."""
        columns = [("id", "ID", 50), ("title", "Title", 150)]
        table = Table(self.frame, columns, height=15, show_delete=True, is_dark=False)
        table.pack()

        # 1. Zero rows (Empty Table)
        self.assertEqual(len(table.tv.get_children("")), 0)
        # Toggle 10 times on empty table
        for i in range(10):
            table.update_delete_icon(is_dark=(i % 2 == 0))
        self.assertIsNotNone(table._delete_icon)

        # Fill after toggle on empty table
        table.fill([(["Single", "1"], "ok", "101")])
        self.assertEqual(len(table.tv.get_children("")), 1)
        child = table.tv.get_children("")[0]
        self.assertEqual(str(table._delete_icon), str(table.tv.item(child, "image")[0] if isinstance(table.tv.item(child, "image"), tuple) else table.tv.item(child, "image")))

        # 2. Exactly 1 row
        for i in range(10):
            table.update_delete_icon(is_dark=(i % 2 == 1))
            self.assertEqual(len(table.tv.get_children("")), 1)
            img = table.tv.item("101", "image")
            assigned = img[0] if isinstance(img, (tuple, list)) else img
            self.assertEqual(assigned, str(table._delete_icon))

        # 3. 500+ rows (Large Table)
        large_rows = [([f"Row {i}", f"Data {i * 2}"], "ok", str(2000 + i)) for i in range(500)]
        t0 = time.perf_counter()
        table.fill(large_rows)
        t_fill = time.perf_counter() - t0
        self.assertEqual(len(table.tv.get_children("")), 500)

        # Toggle dark and light on 500 rows
        t_toggle_start = time.perf_counter()
        for i in range(10):
            is_dark = (i % 2 == 0)
            table.update_delete_icon(is_dark=is_dark)
            # Spot check 5 random rows for icon consistency
            for check_id in ["2000", "2100", "2250", "2400", "2499"]:
                img = table.tv.item(check_id, "image")
                assigned = img[0] if isinstance(img, (tuple, list)) else img
                self.assertEqual(assigned, str(table._delete_icon))
        t_toggle_total = time.perf_counter() - t_toggle_start

        # 10 toggles across 500 rows (5,000 item reconfigurations) must finish reasonably
        self.assertLess(t_toggle_total, 3.0, f"500-row toggles took {t_toggle_total:.2f}s")

        # 4. Mutation under active theme: delete 250 rows, toggle theme, add 100 rows
        for i in range(250):
            table.tv.delete(str(2000 + i))
        self.assertEqual(len(table.tv.get_children("")), 250)

        table.update_delete_icon(is_dark=True)
        # Add 100 more rows
        more_rows = [([f"New {i}", f"New Data {i}"], "ok", str(3000 + i)) for i in range(100)]
        for r in more_rows:
            table.tv.insert("", "end", iid=r[2], values=r[0], tags=(r[1],), image=table._delete_icon)
        self.assertEqual(len(table.tv.get_children("")), 350)

        # Final theme toggle to light
        table.update_delete_icon(is_dark=False)
        for check_id in ["2250", "2499", "3000", "3099"]:
            img = table.tv.item(check_id, "image")
            assigned = img[0] if isinstance(img, (tuple, list)) else img
            self.assertEqual(assigned, str(table._delete_icon))

    def test_delete_callbacks_before_during_after_toggles(self):
        """Stress-test delete callbacks routing and robustness before, during, and after toggles."""
        deleted_ids = []
        selected_ids = []
        double_clicked_ids = []

        def on_del(row_id):
            deleted_ids.append(row_id)

        def on_sel(row_id):
            selected_ids.append(row_id)

        def on_dbl(row_id):
            double_clicked_ids.append(row_id)

        columns = [("col1", "Column 1", 100), ("col2", "Column 2", 100)]
        table = Table(
            self.frame,
            columns,
            height=10,
            show_delete=True,
            on_delete=on_del,
            on_select=on_sel,
            on_double_click=on_dbl,
            is_dark=False,
        )
        table.pack()

        test_rows = [([f"Val {i}", f"Detail {i}"], "", str(10 + i)) for i in range(10)]
        table.fill(test_rows)

        # Helper to simulate clicks
        class FakeEvent:
            def __init__(self, x, y):
                self.x = x
                self.y = y

        # Mock tv.identify and identify_row
        orig_identify = table.tv.identify
        orig_identify_row = table.tv.identify_row
        orig_identify_col = table.tv.identify_column

        # 1. Callback BEFORE toggle
        table.tv.identify = MagicMock(return_value="tree")
        table.tv.identify_row = MagicMock(return_value="10")
        table.tv.identify_column = MagicMock(return_value="#0")

        table._handle_click(FakeEvent(10, 10))
        self.assertEqual(deleted_ids, ["10"])
        self.assertEqual(selected_ids, [])

        # Non-delete column click -> select
        table.tv.identify = MagicMock(return_value="cell")
        table.tv.identify_column = MagicMock(return_value="#1")
        table._handle_click(FakeEvent(80, 10))
        self.assertEqual(selected_ids, ["10"])
        self.assertEqual(deleted_ids, ["10"])

        # 2. Interleaved toggles and clicks (DURING rapid toggling)
        deleted_ids.clear()
        selected_ids.clear()

        for cycle in range(30):
            # Toggle theme
            table.update_delete_icon(is_dark=(cycle % 2 == 1))

            target_row = str(10 + (cycle % 10))
            # Simulate delete click
            table.tv.identify = MagicMock(return_value="tree")
            table.tv.identify_row = MagicMock(return_value=target_row)
            table.tv.identify_column = MagicMock(return_value="#0")
            table._handle_click(FakeEvent(10, 10))

            # Simulate select click
            table.tv.identify = MagicMock(return_value="cell")
            table.tv.identify_column = MagicMock(return_value="#2")
            table._handle_click(FakeEvent(120, 10))

        self.assertEqual(len(deleted_ids), 30)
        self.assertEqual(len(selected_ids), 30)
        # Verify order and targeting
        for idx, row_id in enumerate(deleted_ids):
            self.assertEqual(row_id, str(10 + (idx % 10)))

        # 3. Double click on delete column vs non-delete column AFTER toggles
        table.update_delete_icon(is_dark=False)
        deleted_ids.clear()
        double_clicked_ids.clear()

        # Double click on delete badge -> should route to delete
        table.tv.identify = MagicMock(return_value="image")
        table.tv.identify_row = MagicMock(return_value="15")
        table.tv.identify_column = MagicMock(return_value="#0")
        table._handle_double_click(FakeEvent(15, 10))
        self.assertEqual(deleted_ids, ["15"])
        self.assertEqual(double_clicked_ids, [])

        # Double click on normal cell -> routes to on_double_click
        table.tv.identify = MagicMock(return_value="cell")
        table.tv.identify_column = MagicMock(return_value="#1")
        table._handle_double_click(FakeEvent(90, 10))
        self.assertEqual(double_clicked_ids, ["15"])

    def test_tcl_image_lifecycle_and_memory_leak(self):
        """Verify Tcl image token count does not leak linearly across theme toggles."""
        columns = [("c1", "C1", 100)]
        table = Table(self.frame, columns, height=5, show_delete=True, is_dark=False)
        table.pack()
        table.fill([(["A"], "", "1"), (["B"], "", "2")])

        # Initial Tcl images
        gc.collect()
        initial_images = set(self.root.tk.call("image", "names"))

        # Perform 60 toggles
        for i in range(60):
            table.update_delete_icon(is_dark=(i % 2 == 1))

        # Force Python garbage collection
        gc.collect()
        final_images = set(self.root.tk.call("image", "names"))

        # The net increase in active Tcl images should NOT be 60.
        # PhotoImage.__del__ calls "image delete", so Python GC reclaims old Tk images.
        # Allow at most 2 active images (current icon + possible transient).
        net_diff = len(final_images - initial_images)
        self.assertLessEqual(
            net_diff,
            2,
            f"Tcl image memory leak detected! Net increase of {net_diff} images after 60 toggles: "
            f"{final_images - initial_images}"
        )

        # Check that current table._delete_icon is inside final_images
        self.assertIn(str(table._delete_icon), final_images)

    def test_create_delete_icon_adversarial_parameters(self):
        """Test create_delete_icon under extreme, corrupted, and invalid parameters."""
        # 1. Normal call
        img = create_delete_icon(master=self.root, size=20)
        self.assertIsNotNone(img)
        self.assertIsInstance(img, ImageTk.PhotoImage)

        # 2. Negative and zero size
        self.assertIsNone(create_delete_icon(master=self.root, size=-10))
        self.assertIsNone(create_delete_icon(master=self.root, size=0))

        # 3. Invalid color strings/types
        self.assertIsNone(create_delete_icon(master=self.root, bg_color="not_a_color"))
        self.assertIsNone(create_delete_icon(master=self.root, border_color=""))
        self.assertIsNone(create_delete_icon(master=self.root, cross_color={"invalid": True}))

        # 4. Zero and negative scale
        self.assertIsNone(create_delete_icon(master=self.root, scale=0))
        self.assertIsNone(create_delete_icon(master=self.root, scale=-2))

        # 5. Master=None (fallback to default root)
        img_no_master = create_delete_icon(master=None, size=16)
        self.assertIsNotNone(img_no_master)

        # 6. Large valid size
        img_large = create_delete_icon(master=self.root, size=64, scale=2)
        self.assertIsNotNone(img_large)
        self.assertEqual(img_large.width(), 64)
        self.assertEqual(img_large.height(), 64)

    def test_tcl_image_external_invalidation_recovery(self):
        """Verify Table recovers automatically if the Tcl image token is deleted externally."""
        columns = [("c1", "C1", 100)]
        table = Table(self.frame, columns, height=5, show_delete=True, is_dark=False)
        table.pack()
        table.fill([(["Item A"], "ok", "10")])

        self.assertIsNotNone(table._delete_icon)
        old_icon_name = str(table._delete_icon)

        # Force external deletion of the image in Tcl interpreter
        table.tk.call("image", "delete", old_icon_name)

        # Confirm it's gone in Tcl
        with self.assertRaises(tk.TclError):
            table.tk.call("image", "type", old_icon_name)

        # Trigger fill() which calls _ensure_delete_icon()
        table.fill([(["Item B"], "ok", "20")])

        # A new valid image should have been generated and applied
        new_icon_name = str(table._delete_icon)
        self.assertNotEqual(old_icon_name, new_icon_name)
        self.assertEqual(table.tk.call("image", "type", new_icon_name), "photo")
        img = table.tv.item("20", "image")
        assigned = img[0] if isinstance(img, (tuple, list)) else img
        self.assertEqual(assigned, new_icon_name)

    def test_rapid_toggle_under_continuous_mutation(self):
        """Stress-test alternating theme toggles interleaved with inserts, updates, and deletes."""
        columns = [("name", "Name", 100), ("val", "Val", 50)]
        table = Table(self.frame, columns, height=10, show_delete=True, is_dark=False)
        table.pack()

        # Initial 20 rows
        current_rows = {i: f"Initial {i}" for i in range(20)}
        table.fill([([v, "0"], "", str(k)) for k, v in current_rows.items()])

        for cycle in range(50):
            is_dark = (cycle % 2 == 1)
            # 1. Toggle theme
            table.update_delete_icon(is_dark=is_dark)

            # 2. Insert 5 new rows
            new_id_base = 1000 + cycle * 10
            for offset in range(5):
                nid = new_id_base + offset
                current_rows[nid] = f"Cycle {cycle} Item {offset}"
                table.tv.insert("", "end", iid=str(nid), values=[current_rows[nid], "1"], image=table._delete_icon)

            # 3. Delete 3 existing rows
            keys_to_del = list(current_rows.keys())[:3]
            for k in keys_to_del:
                table.tv.delete(str(k))
                del current_rows[k]

            # 4. Spot check remaining rows for correct image assignment
            sample_ids = list(current_rows.keys())[-5:]
            for s_id in sample_ids:
                img = table.tv.item(str(s_id), "image")
                assigned = img[0] if isinstance(img, (tuple, list)) else img
                self.assertEqual(assigned, str(table._delete_icon))

        self.assertEqual(len(table.tv.get_children("")), len(current_rows))


class TestAppLevelAdversarialThemeStress(unittest.TestCase):
    """Full App integration stress test with ephemeral DB and rapid toggling."""

    @classmethod
    def setUpClass(cls):
        # Record baseline row counts of production database
        cls.prod_path = "data/inventory.db"
        cls.baseline_counts = {}
        if os.path.exists(cls.prod_path):
            conn = sqlite3.connect(cls.prod_path)
            for tbl in ["INVENTORY", "PRODUCTS", "RECIPES", "ALIASES", "PROFILE", "APP_SETTINGS"]:
                try:
                    cls.baseline_counts[tbl] = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                except Exception:
                    pass
            conn.close()

    @classmethod
    def tearDownClass(cls):
        # Baseline preservation invariant
        if os.path.exists(cls.prod_path) and cls.baseline_counts:
            conn = sqlite3.connect(cls.prod_path)
            current_counts = {}
            for tbl in ["INVENTORY", "PRODUCTS", "RECIPES", "ALIASES", "PROFILE", "APP_SETTINGS"]:
                try:
                    current_counts[tbl] = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
                except Exception:
                    pass
            conn.close()
            assert current_counts == cls.baseline_counts, f"Baseline changed: {cls.baseline_counts} vs {current_counts}"

    def setUp(self):
        import os
        import tempfile
        from meal_planner.db.connection import get_connection
        from meal_planner.db.schema import create_schema, migrate

        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp.name
        self.tmp.close()

        self._orig_env = os.environ.get("MEAL_PLANNER_DB")
        os.environ["MEAL_PLANNER_DB"] = self.db_path

        conn = get_connection(self.db_path)
        create_schema(conn)
        migrate(conn)
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
        aliases = [
            ("Pasta", 1),
            ("Tomato Sauce", 2),
            ("Olive Oil", 3),
        ]
        conn.executemany("INSERT OR REPLACE INTO ALIASES (Name, ID) VALUES (?, ?)", aliases)
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
        conn.commit()
        conn.close()

        from meal_planner.gui.app import App
        self.app = App()
        self.app.update_idletasks()

    def tearDown(self):
        import os
        try:
            self.app.destroy()
        except Exception:
            pass
        if self._orig_env:
            os.environ["MEAL_PLANNER_DB"] = self._orig_env
        else:
            os.environ.pop("MEAL_PLANNER_DB", None)
        try:
            if os.path.exists(self.db_path):
                os.remove(self.db_path)
        except Exception:
            pass
        gc.collect()

    def test_app_e2e_50_toggles_with_delete_click_and_refresh(self):
        """Stress-test 50 toggles on live App with real database and verify delete flow."""
        self.assertEqual(len(self.app.t_disp.tv.get_children("")), 3)

        # 50 rapid toggles on the full GUI application
        for cycle in range(50):
            self.app.toggle_theme()
            self.app.update_idletasks()

            expected_theme = "light" if cycle % 2 == 0 else "dark"
            self.assertEqual(self.app.theme, expected_theme)

            # Verify every row in t_disp has valid PhotoImage
            icon_token = str(self.app.t_disp._delete_icon)
            for row_id in self.app.t_disp.tv.get_children(""):
                img = self.app.t_disp.tv.item(row_id, "image")
                assigned = img[0] if isinstance(img, (tuple, list)) else img
                self.assertEqual(assigned, icon_token)

        # Perform a delete click on row '2' with mock confirmation
        from unittest.mock import patch
        with patch("meal_planner.gui.app.messagebox.askyesno", return_value=True):
            self.app.t_disp.on_delete("2")
            self.app.update_idletasks()

        # Verify row 2 is deleted and exactly 2 rows remain
        remaining_rows = self.app.t_disp.tv.get_children("")
        self.assertEqual(len(remaining_rows), 2)
        self.assertNotIn("2", remaining_rows)

        # Verify all remaining rows still have valid active delete badges
        final_token = str(self.app.t_disp._delete_icon)
        for row_id in remaining_rows:
            img = self.app.t_disp.tv.item(row_id, "image")
            assigned = img[0] if isinstance(img, (tuple, list)) else img
            self.assertEqual(assigned, final_token)


if __name__ == "__main__":
    unittest.main()

