"""
tests/test_inventory_management.py

Consolidated inventory management, edit dialog, macro propagation, and deletion test suite.
"""


import math
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch


# ======================================================================
# SECTION: tests/test_inventory_edit_delete.py
# ======================================================================

"""
Unit and integration tests for Milestone M1 (Requirement R1):
- Individual inventory item retrieval and characteristics editing
- Inventory item delete button with confirmation
- Macro modification prompt and propagation logic (Option 1 decoupled vs Option 2 in-place)
- Table widget column #0 delete button support
"""

import os
import re
import sqlite3
import tempfile
import tkinter as tk
from unittest.mock import MagicMock, patch
import unittest

from meal_planner.db.connection import get_connection
from meal_planner.db.schema import create_schema
from meal_planner.db.schema import migrate
from meal_planner.gui.app import App
from meal_planner.gui.dialogs import InventoryItemEditDialog, MacroPromptDialog
from meal_planner.gui.widgets import Table, create_delete_icon
from meal_planner.inventory import (
    AliasConflict,
    InvalidInput,
    ProductNotFound,
    add_item,
    consume_quantity,
    delete_inventory_item,
    get_inventory_item,
    get_inventory_rows,
    get_next_product_id,
    is_product_macro_linked,
    update_inventory_item,
)


class BaseInventoryTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tk.Tk()
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:
            pass

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp.name
        self.tmp.close()

        self._orig_env = os.environ.get("MEAL_PLANNER_DB")
        os.environ["MEAL_PLANNER_DB"] = self.db_path

        conn = get_connection(self.db_path)
        create_schema(conn)
        migrate(conn)
        conn.close()

    def tearDown(self):
        if self._orig_env is not None:
            os.environ["MEAL_PLANNER_DB"] = self._orig_env
        else:
            os.environ.pop("MEAL_PLANNER_DB", None)

        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except OSError:
                pass


class TestInventoryBackendOperations(BaseInventoryTestCase):
    """Tests for get_inventory_rows, get_inventory_item, delete_inventory_item, and is_product_macro_linked."""

    def test_get_inventory_rows_empty_and_populated(self):
        # Empty DB returns empty list
        rows = get_inventory_rows(self.db_path)
        self.assertEqual(rows, [])

        # Add items
        add_item("Pasta Penne", 0.5, 2, "pantry", "2027-01-01",
                 fallback_id=1, new_product_unit="kg",
                 new_product_macros={"Kcal_100g": 350, "Protein_100g": 12, "Carbs_100g": 72, "Fat_100g": 1.5},
                 db_path=self.db_path)
        add_item("Uova", 1.0, 6, "fridge", "2026-12-15",
                 fallback_id=2, new_product_unit="pcs",
                 new_product_macros={"Kcal_100g": 140, "Protein_100g": 12, "Carbs_100g": 1, "Fat_100g": 10},
                 db_path=self.db_path)

        rows = get_inventory_rows(self.db_path)
        self.assertEqual(len(rows), 2)

        # Check fields of row 1 (kg)
        r1 = rows[0]
        self.assertEqual(r1["Name"], "Pasta Penne")
        self.assertEqual(r1["Unit"], "kg")
        self.assertEqual(r1["Unit_Weight"], 0.5)
        self.assertEqual(r1["Intact_Items"], 2)
        self.assertEqual(r1["Open_Remainder"], 0.0)
        self.assertEqual(r1["Total_Available_Weight"], 1.0)
        self.assertEqual(r1["Location"], "pantry")
        self.assertEqual(r1["Expiration_Date"], "2027-01-01")
        self.assertEqual(r1["Kcal_100g"], 350.0)
        self.assertEqual(r1["Protein_100g"], 12.0)
        self.assertIn("macros", r1)

        # Check fields of row 2 (pcs)
        r2 = rows[1]
        self.assertEqual(r2["Name"], "Uova")
        self.assertEqual(r2["Unit"], "pcs")
        self.assertEqual(r2["Intact_Items"], 6)
        self.assertEqual(r2["Total_Available_Weight"], 6.0)

    def test_get_inventory_item(self):
        add_item("Riso Carnaroli", 1.0, 3, "dispensa", "2026-11-30",
                 fallback_id=10, new_product_unit="kg",
                 new_product_macros={"Kcal_100g": 360, "Protein_100g": 7, "Carbs_100g": 78, "Fat_100g": 1.0},
                 db_path=self.db_path)
        rows = get_inventory_rows(self.db_path)
        self.assertEqual(len(rows), 1)
        row_id = rows[0]["Row_ID"]

        item = get_inventory_item(row_id, db_path=self.db_path)
        self.assertIsNotNone(item)
        self.assertEqual(item["Row_ID"], row_id)
        self.assertEqual(item["Name"], "Riso Carnaroli")
        self.assertEqual(item["Unit_Weight"], 1.0)
        self.assertEqual(item["Intact_Items"], 3)

        # Non-existent item returns None
        self.assertIsNone(get_inventory_item(9999, db_path=self.db_path))

    def test_delete_inventory_item(self):
        add_item("Mela Fuji", 0.2, 4, "fridge", "2026-11-20",
                 fallback_id=20, new_product_unit="kg",
                 new_product_macros={"Kcal_100g": 52, "Protein_100g": 0.3, "Carbs_100g": 14, "Fat_100g": 0.2},
                 db_path=self.db_path)
        rows = get_inventory_rows(self.db_path)
        self.assertEqual(len(rows), 1)
        row_id = rows[0]["Row_ID"]

        # Successful deletion
        deleted = delete_inventory_item(row_id=row_id, db_path=self.db_path)
        self.assertTrue(deleted)
        self.assertEqual(get_inventory_rows(self.db_path), [])

        # Deleting already deleted row returns False
        deleted_again = delete_inventory_item(row_id=row_id, db_path=self.db_path)
        self.assertFalse(deleted_again)

        # External connection calling pattern
        add_item("Latte", 1.0, 1, "fridge", "2026-11-10",
                 fallback_id=21, new_product_unit="L",
                 new_product_macros={"Kcal_100g": 45, "Protein_100g": 3.2, "Carbs_100g": 4.8, "Fat_100g": 1.5},
                 db_path=self.db_path)
        new_row_id = get_inventory_rows(self.db_path)[0]["Row_ID"]

        conn = get_connection(self.db_path)
        del_res = delete_inventory_item(conn, new_row_id)
        conn.commit()
        conn.close()
        self.assertTrue(del_res)

        # Invalid row ID inputs
        with self.assertRaises(InvalidInput):
            delete_inventory_item(row_id="invalid", db_path=self.db_path)
        with self.assertRaises(InvalidInput):
            delete_inventory_item(row_id=-1, db_path=self.db_path)

    def test_is_product_macro_linked(self):
        conn = get_connection(self.db_path)
        # Create product 50
        conn.execute(
            "INSERT INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g) VALUES (50, 'kg', 300, 10, 60, 2)"
        )
        conn.execute("INSERT INTO ALIASES (Name, ID) VALUES ('Pollo', 50)")
        conn.execute("INSERT INTO INVENTORY (ID, Unit_Weight, Intact_Items, Open_Remainder, Location) VALUES (50, 1.0, 1, 0, 'fridge')")
        conn.commit()

        # Currently 1 alias, 1 inventory row, 0 recipes -> NOT linked
        self.assertFalse(is_product_macro_linked(conn, 50))
        self.assertFalse(is_product_macro_linked(50, db_path=self.db_path))

        # Add second alias -> IS linked
        conn.execute("INSERT INTO ALIASES (Name, ID) VALUES ('Petto di pollo', 50)")
        conn.commit()
        self.assertTrue(is_product_macro_linked(conn, 50))

        # Remove second alias -> NOT linked again
        conn.execute("DELETE FROM ALIASES WHERE Name = 'Petto di pollo'")
        conn.commit()
        self.assertFalse(is_product_macro_linked(conn, 50))

        # Add second inventory row -> IS linked
        conn.execute("INSERT INTO INVENTORY (ID, Unit_Weight, Intact_Items, Open_Remainder, Location) VALUES (50, 0.5, 2, 0, 'freezer')")
        conn.commit()
        self.assertTrue(is_product_macro_linked(conn, 50))

        # Remove second inventory row -> NOT linked again
        conn.execute("DELETE FROM INVENTORY WHERE Location = 'freezer'")
        conn.commit()
        self.assertFalse(is_product_macro_linked(conn, 50))

        # Add to recipe -> IS linked
        conn.execute("INSERT INTO RECIPES (Recipe_ID, Name) VALUES (1, 'Pollo arrosto')")
        conn.execute("INSERT INTO RECIPE_INGREDIENTS (Recipe_ID, ID, Quantity, Unit) VALUES (1, 50, 200, 'g')")
        conn.commit()
        self.assertTrue(is_product_macro_linked(conn, 50))
        conn.close()


class TestInventoryUpdateAndMacroPropagation(BaseInventoryTestCase):
    """Tests for update_inventory_item across all scenarios."""

    def test_update_inventory_fields_only_no_macro_change(self):
        add_item("Pasta Spaghetti", 0.5, 2, "pantry", "2026-12-31",
                 fallback_id=30, new_product_unit="kg",
                 new_product_macros={"Kcal_100g": 350, "Protein_100g": 12, "Carbs_100g": 72, "Fat_100g": 1.5},
                 db_path=self.db_path)
        row_id = get_inventory_rows(self.db_path)[0]["Row_ID"]

        # Update location, weight, intact items, expiration
        res = update_inventory_item(
            row_id=row_id,
            item_fields={
                "Unit_Weight": 1.0,
                "Intact_Items": 5,
                "Open_Remainder": 0.25,
                "Location": "scaffale_alto",
                "Expiration_Date": "2027-06-30",
            },
            db_path=self.db_path,
        )
        self.assertEqual(res["outcome"], "ok")
        self.assertEqual(res["product_id"], 30)
        self.assertFalse(res["created_new_product"])
        self.assertFalse(res["macros_changed"])

        item = get_inventory_item(row_id, db_path=self.db_path)
        self.assertEqual(item["Unit_Weight"], 1.0)
        self.assertEqual(item["Intact_Items"], 5)
        self.assertEqual(item["Open_Remainder"], 0.25)
        self.assertEqual(item["Location"], "scaffale_alto")
        self.assertEqual(item["Expiration_Date"], "2027-06-30")

    def test_update_unlinked_product_macros_updates_in_place(self):
        # Product with only 1 alias, 1 inventory row, 0 recipes
        add_item("Olio EVO", 1.0, 1, "pantry", "2027-12-31",
                 fallback_id=31, new_product_unit="L",
                 new_product_macros={"Kcal_100g": 884, "Protein_100g": 0, "Carbs_100g": 0, "Fat_100g": 100},
                 db_path=self.db_path)
        row_id = get_inventory_rows(self.db_path)[0]["Row_ID"]

        res = update_inventory_item(
            row_id=row_id,
            macro_fields={"Kcal_100g": 890, "Protein_100g": 0, "Carbs_100g": 0, "Fat_100g": 100},
            propagate=False,  # Product is not linked anyway
            db_path=self.db_path,
        )
        self.assertEqual(res["outcome"], "ok")
        self.assertEqual(res["product_id"], 31)
        self.assertFalse(res["created_new_product"])
        self.assertTrue(res["macros_changed"])

        # Check PRODUCTS in place
        item = get_inventory_item(row_id, db_path=self.db_path)
        self.assertEqual(item["Kcal_100g"], 890.0)

    def test_update_linked_product_option_1_modify_only_item(self):
        """
        Option 1: Allocates new product ID, inserts new product row, updates alias,
        and re-points the edited INVENTORY row to the new product ID, leaving other
        linked instances untouched.
        """
        conn = get_connection(self.db_path)
        # Seed linked product 40 with 2 inventory rows and 1 recipe
        conn.execute("INSERT INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g) VALUES (40, 'kg', 350, 12, 70, 2)")
        conn.execute("INSERT INTO ALIASES (Name, ID) VALUES ('Fusilli Barilla', 40)")
        conn.execute("INSERT INTO INVENTORY (ID, Unit_Weight, Intact_Items, Open_Remainder, Location) VALUES (40, 0.5, 2, 0, 'pantry')")
        conn.execute("INSERT INTO INVENTORY (ID, Unit_Weight, Intact_Items, Open_Remainder, Location) VALUES (40, 0.5, 4, 0, 'cantina')")
        conn.execute("INSERT INTO RECIPES (Recipe_ID, Name) VALUES (1, 'Pasta al pomodoro')")
        conn.execute("INSERT INTO RECIPE_INGREDIENTS (Recipe_ID, ID, Quantity, Unit) VALUES (1, 40, 80, 'g')")
        conn.commit()
        conn.close()

        rows = get_inventory_rows(self.db_path)
        self.assertEqual(len(rows), 2)
        row1_id = rows[0]["Row_ID"]
        row2_id = rows[1]["Row_ID"]

        # Confirm product is linked
        self.assertTrue(is_product_macro_linked(40, db_path=self.db_path))

        # Execute Option 1 (propagate=False) on row 1
        res = update_inventory_item(
            row_id=row1_id,
            macro_fields={"Kcal_100g": 380, "Protein_100g": 14, "Carbs_100g": 68, "Fat_100g": 3.0},
            propagate=False,
            db_path=self.db_path,
        )
        self.assertEqual(res["outcome"], "ok")
        self.assertTrue(res["created_new_product"])
        new_pid = res["product_id"]
        self.assertNotEqual(new_pid, 40)

        # 1. Row 1 now points to new product ID with updated macros
        item1 = get_inventory_item(row1_id, db_path=self.db_path)
        self.assertEqual(item1["ID"], new_pid)
        self.assertEqual(item1["Kcal_100g"], 380.0)
        self.assertEqual(item1["Protein_100g"], 14.0)

        # 2. Row 2 STILL points to product 40 with original macros untouched
        item2 = get_inventory_item(row2_id, db_path=self.db_path)
        self.assertEqual(item2["ID"], 40)
        self.assertEqual(item2["Kcal_100g"], 350.0)
        self.assertEqual(item2["Protein_100g"], 12.0)

        # 3. Recipe STILL points to product 40 with original macros
        conn = get_connection(self.db_path)
        rec_ing = conn.execute("SELECT ID FROM RECIPE_INGREDIENTS WHERE Recipe_ID = 1").fetchone()
        orig_prod = conn.execute("SELECT Kcal_100g, Protein_100g FROM PRODUCTS WHERE ID = 40").fetchone()
        conn.close()
        self.assertEqual(rec_ing[0], 40)
        self.assertEqual(orig_prod, (350.0, 12.0))

    def test_update_linked_product_option_2_propagate_backwards(self):
        """
        Option 2: Updates PRODUCTS record in place, preserving product ID so all
        linked rows and recipes reflect the change backwards.
        """
        conn = get_connection(self.db_path)
        conn.execute("INSERT INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g) VALUES (45, 'kg', 350, 12, 70, 2)")
        conn.execute("INSERT INTO ALIASES (Name, ID) VALUES ('Penne Rigate', 45)")
        conn.execute("INSERT INTO INVENTORY (ID, Unit_Weight, Intact_Items, Open_Remainder, Location) VALUES (45, 0.5, 1, 0, 'pantry')")
        conn.execute("INSERT INTO INVENTORY (ID, Unit_Weight, Intact_Items, Open_Remainder, Location) VALUES (45, 1.0, 2, 0, 'cantina')")
        conn.execute("INSERT INTO RECIPES (Recipe_ID, Name) VALUES (2, 'Penne all arrabbiata')")
        conn.execute("INSERT INTO RECIPE_INGREDIENTS (Recipe_ID, ID, Quantity, Unit) VALUES (2, 45, 100, 'g')")
        conn.commit()
        conn.close()

        rows = get_inventory_rows(self.db_path)
        row1_id = rows[0]["Row_ID"]
        row2_id = rows[1]["Row_ID"]

        # Execute Option 2 (propagate=True)
        res = update_inventory_item(
            row_id=row1_id,
            macro_fields={"Kcal_100g": 365, "Protein_100g": 13.5, "Carbs_100g": 71, "Fat_100g": 1.8},
            propagate=True,
            db_path=self.db_path,
        )
        self.assertEqual(res["outcome"], "ok")
        self.assertEqual(res["product_id"], 45)
        self.assertFalse(res["created_new_product"])

        # Both rows and product in database now have the new macros
        item1 = get_inventory_item(row1_id, db_path=self.db_path)
        item2 = get_inventory_item(row2_id, db_path=self.db_path)
        self.assertEqual(item1["Kcal_100g"], 365.0)
        self.assertEqual(item2["Kcal_100g"], 365.0)

        conn = get_connection(self.db_path)
        prod = conn.execute("SELECT Kcal_100g, Protein_100g FROM PRODUCTS WHERE ID = 45").fetchone()
        conn.close()
        self.assertEqual(prod, (365.0, 13.5))

    def test_validation_errors(self):
        add_item("Tonno", 0.16, 3, "dispensa", "2028-05-10",
                 fallback_id=50, new_product_unit="kg",
                 new_product_macros={"Kcal_100g": 120, "Protein_100g": 26, "Carbs_100g": 0, "Fat_100g": 1.5},
                 db_path=self.db_path)
        row_id = get_inventory_rows(self.db_path)[0]["Row_ID"]

        # Negative unit weight
        with self.assertRaises(InvalidInput):
            update_inventory_item(row_id=row_id, item_fields={"Unit_Weight": -0.5}, db_path=self.db_path)

        # Negative intact items
        with self.assertRaises(InvalidInput):
            update_inventory_item(row_id=row_id, item_fields={"Intact_Items": -1}, db_path=self.db_path)

        # Total quantity = 0
        with self.assertRaises(InvalidInput):
            update_inventory_item(row_id=row_id, item_fields={"Intact_Items": 0, "Open_Remainder": 0}, db_path=self.db_path)

        # Empty location
        with self.assertRaises(InvalidInput):
            update_inventory_item(row_id=row_id, item_fields={"Location": ""}, db_path=self.db_path)

        # Malformed expiration date
        with self.assertRaises(InvalidInput):
            update_inventory_item(row_id=row_id, item_fields={"Expiration_Date": "2026/12/31"}, db_path=self.db_path)

        # Invalid unit
        with self.assertRaises(InvalidInput):
            update_inventory_item(row_id=row_id, item_fields={"Unit": "boxes"}, db_path=self.db_path)

        # Negative macros
        with self.assertRaises(InvalidInput):
            update_inventory_item(row_id=row_id, macro_fields={"Kcal_100g": -50}, db_path=self.db_path)

        # Non-finite float values rejected across all float fields
        bad_floats = [float("inf"), float("-inf"), float("nan"), "inf", "-inf", "+inf", "NaN", "nan"]
        for bad in bad_floats:
            with self.assertRaises(InvalidInput):
                update_inventory_item(row_id=row_id, item_fields={"Unit_Weight": bad}, db_path=self.db_path)
            with self.assertRaises(InvalidInput):
                update_inventory_item(row_id=row_id, item_fields={"Open_Remainder": bad}, db_path=self.db_path)
            with self.assertRaises(InvalidInput):
                update_inventory_item(row_id=row_id, item_fields={"Grams_per_piece": bad}, db_path=self.db_path)
            with self.assertRaises(InvalidInput):
                update_inventory_item(row_id=row_id, item_fields={"Density_kg_per_L": bad}, db_path=self.db_path)
            with self.assertRaises(InvalidInput):
                update_inventory_item(row_id=row_id, macro_fields={"Kcal_100g": bad}, db_path=self.db_path)
            with self.assertRaises(InvalidInput):
                update_inventory_item(row_id=row_id, macro_fields={"Protein_100g": bad}, db_path=self.db_path)


class TestInventoryGUIComponents(BaseInventoryTestCase):
    """Tests for Table delete button column #0, MacroPromptDialog, and InventoryItemEditDialog."""

    def test_table_delete_button_column_rendering(self):
        frame = tk.Frame(self.root)
        on_del_mock = MagicMock()
        on_sel_mock = MagicMock()

        table = Table(
            frame,
            [("n", "Name", 150), ("q", "Qty", 80)],
            height=5,
            show_delete=True,
            on_delete=on_del_mock,
            on_select=on_sel_mock,
        )
        table.pack()

        # Check Treeview show configuration includes 'tree' for column #0
        show_attr = str(table.tv.cget("show"))
        self.assertIn("tree", show_attr)
        self.assertIn("headings", show_attr)

        # Populate rows with row IDs
        table.fill([
            (("Pasta", "1.0"), None, 101),
            (("Riso", "0.5"), "expiring", 102),
        ])

        children = table.tv.get_children()
        self.assertEqual(len(children), 2)
        self.assertEqual(children[0], "101")
        self.assertEqual(children[1], "102")

        # Test simulated delete click on column #0
        mock_del_event = MagicMock()
        mock_del_event.x = 10
        mock_del_event.y = 10
        table.tv.identify = MagicMock(return_value="cell")
        table.tv.identify_row = MagicMock(return_value="101")
        table.tv.identify_column = MagicMock(return_value="#0")

        table._handle_click(mock_del_event)
        on_del_mock.assert_called_once_with("101")
        on_sel_mock.assert_not_called()

        # Test simulated select click on data column #1
        on_del_mock.reset_mock()
        mock_sel_event = MagicMock()
        mock_sel_event.x = 100
        mock_sel_event.y = 10
        table.tv.identify = MagicMock(return_value="cell")
        table.tv.identify_row = MagicMock(return_value="102")
        table.tv.identify_column = MagicMock(return_value="#1")

        table._handle_click(mock_sel_event)
        on_sel_mock.assert_called_once_with("102")
        on_del_mock.assert_not_called()

    def test_macro_prompt_dialog_options(self):
        # Test Option 1 (Single)
        dlg1 = MacroPromptDialog(self.root, "Pasta", product_id=1, wait=False)
        dlg1.choose_single()
        self.assertEqual(dlg1.result, "single")

        # Test Option 2 (Propagate)
        dlg2 = MacroPromptDialog(self.root, "Pasta", product_id=1, wait=False)
        dlg2.choose_propagate()
        self.assertEqual(dlg2.result, "propagate")

        # Test Cancel
        dlg3 = MacroPromptDialog(self.root, "Pasta", product_id=1, wait=False)
        dlg3.cancel()
        self.assertIsNone(dlg3.result)

    def test_inventory_item_edit_dialog_prefill_and_save(self):
        add_item("Biscotti Avena", 0.35, 2, "dispensa", "2027-04-15",
                 fallback_id=70, new_product_unit="kg",
                 new_product_macros={"Kcal_100g": 430, "Protein_100g": 8.5, "Carbs_100g": 65, "Fat_100g": 15},
                 db_path=self.db_path)
        row_id = get_inventory_rows(self.db_path)[0]["Row_ID"]

        # Open dialog non-blocking
        dlg = InventoryItemEditDialog(self.root, row_id=row_id, db_path=self.db_path, wait=False)

        # Check all pre-filled characteristics
        self.assertEqual(dlg.v_name.get(), "Biscotti Avena")
        self.assertEqual(dlg.v_unit_weight.get(), "0.35")
        self.assertEqual(dlg.v_intact.get(), "2")
        self.assertEqual(dlg.v_location.get(), "dispensa")
        self.assertEqual(dlg.v_expiration.get(), "2027-04-15")
        self.assertEqual(dlg.v_unit.get(), "kg")
        self.assertEqual(dlg.v_kcal.get(), "430")
        self.assertEqual(dlg.v_protein.get(), "8.5")
        self.assertEqual(dlg.v_carbs.get(), "65")
        self.assertEqual(dlg.v_fat.get(), "15")

        # Modify inventory and save
        dlg.v_location.set("mobile_cucina")
        dlg.v_intact.set("3")
        saved = dlg.save()
        self.assertTrue(saved)

        # Verify DB updated
        item = get_inventory_item(row_id, db_path=self.db_path)
        self.assertEqual(item["Location"], "mobile_cucina")
        self.assertEqual(item["Intact_Items"], 3)

    def test_app_pantry_delete_and_edit_integration(self):
        add_item("Yogurt Greco", 0.17, 4, "fridge", "2026-11-25",
                 fallback_id=80, new_product_unit="kg",
                 new_product_macros={"Kcal_100g": 59, "Protein_100g": 10, "Carbs_100g": 3.6, "Fat_100g": 0.4},
                 db_path=self.db_path)
        row_id = get_inventory_rows(self.db_path)[0]["Row_ID"]

        app = App()
        app.db_path = self.db_path
        app.refresh()

        try:
            # 1. Pantry table contains the item
            pantry_items = app.t_disp.tv.get_children()
            self.assertIn(str(row_id), pantry_items)

            # 2. Test delete confirmation rejected (User clicks No)
            with patch("tkinter.messagebox.askyesno", return_value=False):
                app._on_delete_inventory_row(str(row_id))
            # Item remains
            self.assertIsNotNone(get_inventory_item(row_id, db_path=self.db_path))

            # 3. Test delete confirmation accepted (User clicks Yes)
            with patch("tkinter.messagebox.askyesno", return_value=True):
                app._on_delete_inventory_row(str(row_id))
            # Item removed
            self.assertIsNone(get_inventory_item(row_id, db_path=self.db_path))
            self.assertNotIn(str(row_id), app.t_disp.tv.get_children())
            self.assertIn("Deleted 'Yogurt Greco'", app.status.get())

        finally:
            try:
                app.destroy()
            except Exception:
                pass




# ======================================================================
# SECTION: tests/test_tier5_adversarial_inventory.py
# ======================================================================

"""
tests/test_tier5_adversarial_inventory.py

Tier 5 Adversarial Coverage Hardening Test Suite:
- White-box source code gap analysis verification for:
  - meal_planner/inventory.py (editing, macro propagation Option 1 vs Option 2, delete logic, float validations)
  - meal_planner/gui/dialogs.py (InventoryItemEditDialog, MacroPromptDialog, destructive confirmations)
- Stress harnesses, boundary conditions, transaction aborts, float validations, and invariant enforcement.
- Strict preservation of production inventory.db row counts: (PRODUCTS: 14, ALIASES: 30, INVENTORY: 36, RECIPES: 3).
"""

import math
import os
import sqlite3
import tempfile
import tkinter as tk
from unittest.mock import MagicMock, patch
import unittest

from meal_planner.db.connection import get_connection
from meal_planner.db.schema import create_schema
from meal_planner.db.schema import migrate
from meal_planner.engine import recipe_macros
from meal_planner.gui.app import App
from meal_planner.gui.dialogs import InventoryItemEditDialog, MacroPromptDialog
from meal_planner.inventory import (
    AliasConflict,
    InvalidInput,
    ProductNotFound,
    add_item,
    clear_inventory,
    delete_inventory_item,
    get_available_inventory,
    get_inventory_item,
    get_inventory_rows,
    get_locations,
    get_next_product_id,
    is_product_macro_linked,
    update_inventory_item,
)


class BaseAdversarialInventoryTestCase(unittest.TestCase):
    """
    Base test case with rigorous test isolation and production DB preservation check.
    Uses temporary SQLite databases exclusively.
    """
    @classmethod
    def setUpClass(cls):
        cls.root = tk.Tk()
        cls.root.withdraw()

        # Capture baseline inventory.db counts
        cls.prod_db_path = os.path.abspath("data/inventory.db")
        if os.path.exists(cls.prod_db_path):
            conn = sqlite3.connect(cls.prod_db_path)
            cur = conn.cursor()
            cls.baseline_counts = {
                "PRODUCTS": cur.execute("SELECT COUNT(*) FROM PRODUCTS").fetchone()[0],
                "ALIASES": cur.execute("SELECT COUNT(*) FROM ALIASES").fetchone()[0],
                "INVENTORY": cur.execute("SELECT COUNT(*) FROM INVENTORY").fetchone()[0],
                "RECIPES": cur.execute("SELECT COUNT(*) FROM RECIPES").fetchone()[0],
            }
            conn.close()
        else:
            cls.baseline_counts = None

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:
            pass

    def setUp(self):
        # Strict isolation: always use a fresh temporary SQLite database
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp.name
        self.tmp.close()

        self._orig_env = os.environ.get("MEAL_PLANNER_DB")
        os.environ["MEAL_PLANNER_DB"] = self.db_path

        conn = get_connection(self.db_path)
        create_schema(conn)
        migrate(conn)
        conn.close()

    def tearDown(self):
        if self._orig_env is not None:
            os.environ["MEAL_PLANNER_DB"] = self._orig_env
        else:
            os.environ.pop("MEAL_PLANNER_DB", None)

        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except OSError:
                pass

        # Invariant check: disk inventory.db must NEVER be modified
        if self.baseline_counts:
            conn = sqlite3.connect(self.prod_db_path)
            cur = conn.cursor()
            current_counts = {
                "PRODUCTS": cur.execute("SELECT COUNT(*) FROM PRODUCTS").fetchone()[0],
                "ALIASES": cur.execute("SELECT COUNT(*) FROM ALIASES").fetchone()[0],
                "INVENTORY": cur.execute("SELECT COUNT(*) FROM INVENTORY").fetchone()[0],
                "RECIPES": cur.execute("SELECT COUNT(*) FROM RECIPES").fetchone()[0],
            }
            conn.close()
            self.assertEqual(
                self.baseline_counts,
                current_counts,
                f"Production inventory.db was corrupted! Expected {self.baseline_counts}, got {current_counts}"
            )

    def _seed_sample_item(self, name="Test Item", unit="kg", unit_weight=1.0, intact=2, remainder=0.0,
                          loc="pantry", exp="2027-01-01", pid=1, kcal=250.0, prot=10.0, carb=40.0, fat=5.0):
        """Helper to insert a single product and inventory row."""
        return add_item(
            name=name,
            unit_weight=unit_weight,
            intact_items=intact,
            location=loc,
            expiration_date=exp,
            fallback_id=pid,
            new_product_unit=unit,
            new_product_macros={
                "Kcal_100g": kcal,
                "Protein_100g": prot,
                "Carbs_100g": carb,
                "Fat_100g": fat,
            },
            db_path=self.db_path,
        )


class TestInventoryAdversarialInputsAndFloats(BaseAdversarialInventoryTestCase):
    """
    Stress tests for update_inventory_item input validation, finite float boundary checks,
    and type robustness.
    """

    def test_invalid_row_id_boundaries(self):
        # None row_id
        with self.assertRaises(InvalidInput):
            update_inventory_item(row_id=None, db_path=self.db_path)

        # Non-positive row_id
        for invalid_id in [0, -1, -999]:
            with self.assertRaises(InvalidInput):
                update_inventory_item(row_id=invalid_id, db_path=self.db_path)

        # Non-integer string
        for invalid_str in ["abc", "12.34", ""]:
            with self.assertRaises(InvalidInput):
                update_inventory_item(row_id=invalid_str, db_path=self.db_path)

        # Row ID not found in database
        with self.assertRaises(ProductNotFound):
            update_inventory_item(row_id=999999, item_fields={"Location": "pantry"}, db_path=self.db_path)

    def test_unit_weight_adversarial_values(self):
        self._seed_sample_item()
        row_id = get_inventory_rows(self.db_path)[0]["Row_ID"]

        # Non-finite and negative/zero unit weights
        adversarial_weights = [
            0, -0.01, -100, float("nan"), float("inf"), float("-inf"),
            "nan", "inf", "-inf", "1e309", "-1e309", "zero", ""
        ]
        for w in adversarial_weights:
            with self.subTest(weight=w):
                with self.assertRaises(InvalidInput):
                    update_inventory_item(row_id=row_id, item_fields={"Unit_Weight": w}, db_path=self.db_path)

        # Valid boundary weight
        res = update_inventory_item(row_id=row_id, item_fields={"Unit_Weight": 0.0001}, db_path=self.db_path)
        self.assertEqual(res["outcome"], "ok")
        item = get_inventory_item(row_id, db_path=self.db_path)
        self.assertAlmostEqual(item["Unit_Weight"], 0.0001)

    def test_intact_and_open_remainder_adversarial_values(self):
        self._seed_sample_item()
        row_id = get_inventory_rows(self.db_path)[0]["Row_ID"]

        # Negative intact items
        for inv_intact in [-1, -50, "abc", "1.5"]:
            with self.subTest(intact=inv_intact):
                with self.assertRaises(InvalidInput):
                    update_inventory_item(row_id=row_id, item_fields={"Intact_Items": inv_intact}, db_path=self.db_path)

        # Negative or non-finite open remainder
        for inv_rem in [-0.01, -10, float("nan"), float("inf"), float("-inf"), "1e309", "xyz"]:
            with self.subTest(rem=inv_rem):
                with self.assertRaises(InvalidInput):
                    update_inventory_item(row_id=row_id, item_fields={"Open_Remainder": inv_rem}, db_path=self.db_path)

        # Zero total quantity check: Intact_Items == 0 and Open_Remainder <= 0
        with self.assertRaises(InvalidInput) as cm:
            update_inventory_item(row_id=row_id, item_fields={"Intact_Items": 0, "Open_Remainder": 0.0}, db_path=self.db_path)
        self.assertIn("Item quantity cannot be 0", str(cm.exception))

        # Intact 0 with positive remainder > 0 is valid (opened package)
        res = update_inventory_item(row_id=row_id, item_fields={"Intact_Items": 0, "Open_Remainder": 0.25}, db_path=self.db_path)
        self.assertEqual(res["outcome"], "ok")
        item = get_inventory_item(row_id, db_path=self.db_path)
        self.assertEqual(item["Intact_Items"], 0)
        self.assertAlmostEqual(item["Open_Remainder"], 0.25)

    def test_location_and_expiration_date_adversarial(self):
        self._seed_sample_item()
        row_id = get_inventory_rows(self.db_path)[0]["Row_ID"]

        # Empty or whitespace location
        for loc in ["", "   ", "\t\n"]:
            with self.subTest(location=loc):
                with self.assertRaises(InvalidInput):
                    update_inventory_item(row_id=row_id, item_fields={"Location": loc}, db_path=self.db_path)

        # New location auto-registration in LOCATIONS table
        update_inventory_item(row_id=row_id, item_fields={"Location": "deep_cellar"}, db_path=self.db_path)
        locations = get_locations(db_path=self.db_path)
        self.assertIn("deep_cellar", locations)

        # Invalid expiration date formats
        for exp in ["2026/12/31", "31-12-2026", "2026-02-31", "2026-13-01", "2026-00-10", "tomorrow", "99999-99-99"]:
            with self.subTest(exp=exp):
                with self.assertRaises(InvalidInput):
                    update_inventory_item(row_id=row_id, item_fields={"Expiration_Date": exp}, db_path=self.db_path)

        # Clearing expiration date with None or empty string
        res = update_inventory_item(row_id=row_id, item_fields={"Expiration_Date": None}, db_path=self.db_path)
        self.assertEqual(res["outcome"], "ok")
        item = get_inventory_item(row_id, db_path=self.db_path)
        self.assertIsNone(item["Expiration_Date"])

    def test_unit_and_conversion_factors_adversarial(self):
        self._seed_sample_item()
        row_id = get_inventory_rows(self.db_path)[0]["Row_ID"]

        # Invalid unit
        for u in ["boxes", "liters", "pack", "GR", 123]:
            with self.subTest(unit=u):
                with self.assertRaises(InvalidInput):
                    update_inventory_item(row_id=row_id, item_fields={"Unit": u}, db_path=self.db_path)

        # Grams_per_piece and Density_kg_per_L non-positive or non-finite
        for invalid_factor in [0, -1, float("nan"), float("inf"), "1e309", "xyz"]:
            with self.subTest(factor=invalid_factor):
                with self.assertRaises(InvalidInput):
                    update_inventory_item(row_id=row_id, item_fields={"Grams_per_piece": invalid_factor}, db_path=self.db_path)
                with self.assertRaises(InvalidInput):
                    update_inventory_item(row_id=row_id, item_fields={"Density_kg_per_L": invalid_factor}, db_path=self.db_path)

        # Clearing conversion factors
        res = update_inventory_item(
            row_id=row_id,
            item_fields={"Grams_per_piece": "", "Density_kg_per_L": None},
            db_path=self.db_path
        )
        self.assertEqual(res["outcome"], "ok")
        item = get_inventory_item(row_id, db_path=self.db_path)
        self.assertIsNone(item["Grams_per_piece"])
        self.assertIsNone(item["Density_kg_per_L"])

    def test_macro_fields_adversarial_and_empty_checks(self):
        self._seed_sample_item()
        row_id = get_inventory_rows(self.db_path)[0]["Row_ID"]

        # Required macros cannot be negative, non-finite, or empty
        for req_macro in ["Kcal_100g", "Protein_100g", "Carbs_100g", "Fat_100g"]:
            for bad_val in [-0.01, -10, float("nan"), float("inf"), "1e309", "bad", "", None]:
                with self.subTest(macro=req_macro, val=bad_val):
                    with self.assertRaises(InvalidInput):
                        update_inventory_item(row_id=row_id, macro_fields={req_macro: bad_val}, db_path=self.db_path)

        # Zero is valid for macros (e.g. 0 carbs in olive oil)
        res = update_inventory_item(row_id=row_id, macro_fields={"Carbs_100g": 0.0}, db_path=self.db_path)
        self.assertEqual(res["outcome"], "ok")
        item = get_inventory_item(row_id, db_path=self.db_path)
        self.assertEqual(item["Carbs_100g"], 0.0)

        # Optional macros can be set to None or empty string (cleared to NULL)
        for opt_macro in ["Sugars_100g", "Saturated_fat_100g", "Fiber_100g", "Salt_100g"]:
            res = update_inventory_item(row_id=row_id, macro_fields={opt_macro: None}, db_path=self.db_path)
            self.assertEqual(res["outcome"], "ok")
            item = get_inventory_item(row_id, db_path=self.db_path)
            self.assertIsNone(item[opt_macro])


class TestIsProductMacroLinkedCombinatorics(BaseAdversarialInventoryTestCase):
    """
    Exhaustive combinatoric testing of is_product_macro_linked oracle.
    Tests every permutation of (aliases > 1, inventory > 1, recipes > 0, groups > 0).
    """

    def test_isolated_product_returns_false(self):
        # 1 alias, 1 inventory row, 0 recipes, 0 groups
        self._seed_sample_item(pid=10)
        self.assertFalse(is_product_macro_linked(10, db_path=self.db_path))

    def test_multiple_aliases_returns_true(self):
        self._seed_sample_item(name="Yogurt Greco", pid=20)
        conn = get_connection(self.db_path)
        conn.execute("INSERT INTO ALIASES (Name, ID) VALUES (?, ?)", ("Greek Yogurt", 20))
        conn.commit()
        conn.close()

        self.assertTrue(is_product_macro_linked(20, db_path=self.db_path))

    def test_multiple_inventory_rows_returns_true(self):
        self._seed_sample_item(name="Tonno", pid=30, loc="pantry")
        # Add second row for same product
        add_item("Tonno", 0.08, 3, "cantina", "2028-01-01", db_path=self.db_path)

        rows = get_inventory_rows(self.db_path)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["ID"], 30)
        self.assertEqual(rows[1]["ID"], 30)
        self.assertTrue(is_product_macro_linked(30, db_path=self.db_path))

    def test_recipe_ingredient_link_returns_true(self):
        self._seed_sample_item(name="Farina 00", pid=40)
        conn = get_connection(self.db_path)
        # Create recipe with product 40
        conn.execute("INSERT INTO RECIPES (Recipe_ID, Name, Notes) VALUES (1, 'Torta', 'Mix')")
        conn.execute("INSERT INTO RECIPE_INGREDIENTS (Recipe_ID, ID, Quantity, Unit) VALUES (1, 40, 0.3, 'kg')")
        conn.commit()
        conn.close()

        self.assertTrue(is_product_macro_linked(40, db_path=self.db_path))

    def test_group_member_link_returns_true(self):
        self._seed_sample_item(name="Latte Parzialmente Scremato", pid=50)
        conn = get_connection(self.db_path)
        conn.execute("INSERT INTO SUBSTITUTE_GROUPS (Group_ID, Name) VALUES (1, 'Dairy')")
        conn.execute("INSERT INTO GROUP_MEMBERS (Group_ID, ID) VALUES (1, 50)")
        conn.commit()
        conn.close()

        self.assertTrue(is_product_macro_linked(50, db_path=self.db_path))

    def test_nonexistent_or_invalid_product_id(self):
        # Non-existent product ID has all counts = 0 -> returns False
        self.assertFalse(is_product_macro_linked(99999, db_path=self.db_path))

        # Invalid or None product ID raises InvalidInput
        with self.assertRaises(InvalidInput):
            is_product_macro_linked(None, db_path=self.db_path)
        with self.assertRaises(InvalidInput):
            is_product_macro_linked("bad_id", db_path=self.db_path)

    def test_calling_signatures_support(self):
        self._seed_sample_item(pid=60)
        conn = get_connection(self.db_path)
        # Signature: (conn, pid)
        self.assertFalse(is_product_macro_linked(conn, 60))
        # Signature: (conn=conn, product_id=pid)
        self.assertFalse(is_product_macro_linked(conn=conn, product_id=60))
        conn.close()


class TestMacroPropagationAdversarialScenarios(BaseAdversarialInventoryTestCase):
    """
    Rigorous verification of Option 1 (isolation/decoupling) vs Option 2 (in-place backward propagation),
    including disambiguation cascades and recipe nutrition protection.
    """

    def setUp(self):
        super().setUp()
        # Create complex linked graph: Product 100 with 2 aliases, 2 inventory rows, and 1 recipe
        conn = get_connection(self.db_path)
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g)
            VALUES (100, 'kg', 350.0, 12.0, 71.0, 1.5)
            """
        )
        cur.execute("INSERT INTO ALIASES (Name, ID) VALUES ('Spaghetti Barilla', 100)")
        cur.execute("INSERT INTO ALIASES (Name, ID) VALUES ('Penne Barilla', 100)")
        cur.execute("INSERT INTO INVENTORY (ID, Unit_Weight, Intact_Items, Open_Remainder, Location) VALUES (100, 0.5, 2, 0.0, 'pantry')")
        cur.execute("INSERT INTO INVENTORY (ID, Unit_Weight, Intact_Items, Open_Remainder, Location) VALUES (100, 0.5, 3, 0.1, 'crate')")
        cur.execute("INSERT INTO RECIPES (Recipe_ID, Name, Notes) VALUES (10, 'Carbonara', 'Cook pasta')")
        cur.execute("INSERT INTO RECIPE_INGREDIENTS (Recipe_ID, ID, Quantity, Unit) VALUES (10, 100, 0.2, 'kg')")
        conn.commit()
        conn.close()

    def test_option1_decoupling_with_alias_disambiguation_cascade(self):
        """
        Option 1: User edits Row 1 macros without propagate (propagate=False).
        Row 1 must be decoupled to new product ID. Original Product 100, Row 2, and Carbonara must remain unchanged.
        Tests alias disambiguation when 'Spaghetti Barilla (edited)' already exists.
        """
        # Pre-seed an alias 'Spaghetti Barilla (edited)' pointing to another product ID (e.g. 99)
        conn = get_connection(self.db_path)
        conn.execute("INSERT INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g) VALUES (99, 'kg', 100, 1, 1, 1)")
        conn.execute("INSERT INTO ALIASES (Name, ID) VALUES ('Spaghetti Barilla (edited)', 99)")
        conn.commit()
        conn.close()

        rows = get_inventory_rows(self.db_path)
        row1 = rows[0]
        row2 = rows[1]
        self.assertEqual(row1["ID"], 100)
        self.assertEqual(row2["ID"], 100)

        # Baseline recipe macros
        conn = get_connection(self.db_path)
        base_recipe_macros = recipe_macros(conn, 10)
        conn.close()
        self.assertAlmostEqual(base_recipe_macros["Kcal"], 700.0)  # 0.2kg * 350 kcal/100g * 10 = 700 kcal

        # Execute Option 1: Modify Row 1 macros
        res = update_inventory_item(
            row_id=row1["Row_ID"],
            macro_fields={"Kcal_100g": 420.0, "Protein_100g": 15.0},
            propagate=False,
            db_path=self.db_path,
        )

        self.assertEqual(res["outcome"], "ok")
        self.assertFalse(res["propagated"])
        self.assertTrue(res["created_new_product"])
        new_pid = res["product_id"]
        self.assertNotEqual(new_pid, 100)

        # Verify Row 1 has new product ID and new macros
        updated_row1 = get_inventory_item(row1["Row_ID"], db_path=self.db_path)
        self.assertEqual(updated_row1["ID"], new_pid)
        self.assertAlmostEqual(updated_row1["Kcal_100g"], 420.0)
        self.assertAlmostEqual(updated_row1["Protein_100g"], 15.0)

        # Disambiguation cascade: since 'Spaghetti Barilla (edited)' was taken by ID 99,
        # it should disambiguate to 'Spaghetti Barilla (#{new_pid})'
        conn = get_connection(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT Name FROM ALIASES WHERE ID = ?", (new_pid,))
        new_aliases = [r[0] for r in cur.fetchall()]
        expected_alias = f"Spaghetti Barilla (#{new_pid})"
        self.assertIn(expected_alias, new_aliases)

        # INVARIANT: Row 2 still points to Product 100 with original macros
        updated_row2 = get_inventory_item(row2["Row_ID"], db_path=self.db_path)
        self.assertEqual(updated_row2["ID"], 100)
        self.assertAlmostEqual(updated_row2["Kcal_100g"], 350.0)
        self.assertAlmostEqual(updated_row2["Protein_100g"], 12.0)

        # INVARIANT: Recipe 10 ingredient and nutrition is untouched
        post_recipe_macros = recipe_macros(conn, 10)
        conn.close()
        self.assertEqual(base_recipe_macros, post_recipe_macros)

    def test_option2_propagate_in_place_updates_all_dependents(self):
        """
        Option 2: User edits Row 1 macros with propagate=True.
        Product 100 must be updated in-place. Both Row 1 and Row 2 must reflect the new macros,
        and Recipe 10 nutrition must automatically reflect the updated macros!
        """
        rows = get_inventory_rows(self.db_path)
        row1 = rows[0]
        row2 = rows[1]

        res = update_inventory_item(
            row_id=row1["Row_ID"],
            macro_fields={"Kcal_100g": 380.0, "Protein_100g": 14.0},
            propagate=True,
            db_path=self.db_path,
        )

        self.assertEqual(res["outcome"], "ok")
        self.assertTrue(res["propagated"])
        self.assertFalse(res["created_new_product"])
        self.assertEqual(res["product_id"], 100)

        # Row 1 and Row 2 both reflect updated macros
        u1 = get_inventory_item(row1["Row_ID"], db_path=self.db_path)
        u2 = get_inventory_item(row2["Row_ID"], db_path=self.db_path)
        self.assertEqual(u1["ID"], 100)
        self.assertEqual(u2["ID"], 100)
        self.assertAlmostEqual(u1["Kcal_100g"], 380.0)
        self.assertAlmostEqual(u2["Kcal_100g"], 380.0)
        self.assertAlmostEqual(u1["Protein_100g"], 14.0)
        self.assertAlmostEqual(u2["Protein_100g"], 14.0)

        # Recipe 10 macros reflect backward propagation: 0.2kg * 380 kcal/100g * 10 = 760 kcal
        conn = get_connection(self.db_path)
        post_recipe_macros = recipe_macros(conn, 10)
        conn.close()
        self.assertAlmostEqual(post_recipe_macros["Kcal"], 760.0)
        self.assertAlmostEqual(post_recipe_macros["Protein"], 28.0)

    def test_alias_conflict_rejection_on_name_update(self):
        """
        When updating item Name in Option 2, if the name already belongs to another product ID,
        it must raise AliasConflict.
        """
        # Create product 200 with alias "Riso Basmati"
        self._seed_sample_item(name="Riso Basmati", pid=200)

        rows = get_inventory_rows(self.db_path)
        row100 = [r for r in rows if r["ID"] == 100][0]

        # Attempt to rename row100 item to "Riso Basmati"
        with self.assertRaises(AliasConflict):
            update_inventory_item(
                row_id=row100["Row_ID"],
                item_fields={"Name": "Riso Basmati"},
                propagate=True,
                db_path=self.db_path,
            )


class TestDeleteInventoryItemAndDestructiveGuardrails(BaseAdversarialInventoryTestCase):
    """
    Verification of delete_inventory_item, clear_inventory, and destructive action guardrails.
    """

    def test_delete_inventory_item_preserves_product_and_aliases(self):
        self._seed_sample_item(name="Biscotti", pid=500)
        rows = get_inventory_rows(self.db_path)
        self.assertEqual(len(rows), 1)
        row_id = rows[0]["Row_ID"]

        # Delete row
        deleted = delete_inventory_item(row_id=row_id, db_path=self.db_path)
        self.assertTrue(deleted)
        self.assertEqual(get_inventory_rows(self.db_path), [])

        # INVARIANT: Product 500 and its ALIAS 'Biscotti' MUST still exist in database!
        conn = get_connection(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM PRODUCTS WHERE ID = 500")
        self.assertIsNotNone(cur.fetchone(), "PRODUCTS row was erroneously deleted!")
        cur.execute("SELECT 1 FROM ALIASES WHERE Name = 'Biscotti' AND ID = 500")
        self.assertIsNotNone(cur.fetchone(), "ALIASES row was erroneously deleted!")
        conn.close()

    def test_delete_inventory_item_idempotency_and_invalid_inputs(self):
        self._seed_sample_item(pid=600)
        row_id = get_inventory_rows(self.db_path)[0]["Row_ID"]

        self.assertTrue(delete_inventory_item(row_id=row_id, db_path=self.db_path))
        # Deleting already-deleted row returns False (not error)
        self.assertFalse(delete_inventory_item(row_id=row_id, db_path=self.db_path))

        # Invalid row_ids
        for inv_id in [None, 0, -1, "abc"]:
            with self.assertRaises(InvalidInput):
                delete_inventory_item(row_id=inv_id, db_path=self.db_path)

    def test_clear_inventory_atomicity_and_preservation(self):
        # Seed multiple items and an exclusion item
        self._seed_sample_item(name="Item 1", pid=701)
        self._seed_sample_item(name="Item 2", pid=702)

        conn = get_connection(self.db_path)
        conn.execute("INSERT INTO EXCLUSION_LIST (ID, Ref_Name, Expiration_Date, Location) VALUES (701, 'Item 1', '2026-11-10', 'pantry')")
        conn.commit()
        conn.close()

        # Hard-delete inventory
        success = clear_inventory(db_path=self.db_path)
        self.assertTrue(success)

        # INVENTORY and EXCLUSION_LIST must be empty
        self.assertEqual(get_inventory_rows(self.db_path), [])
        conn = get_connection(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM EXCLUSION_LIST")
        self.assertEqual(cur.fetchone()[0], 0)

        # PRODUCTS and ALIASES must remain completely intact
        cur.execute("SELECT COUNT(*) FROM PRODUCTS")
        self.assertEqual(cur.fetchone()[0], 2)
        cur.execute("SELECT COUNT(*) FROM ALIASES")
        self.assertEqual(cur.fetchone()[0], 2)
        conn.close()

    def test_destructive_action_gui_confirmation_guardrails(self):
        """
        Verify App destructive action methods:
        - When messagebox.askyesno returns False (user cancels), NO deletion occurs.
        - When messagebox.askyesno returns True (user confirms), action executes cleanly.
        """
        self._seed_sample_item(name="Guardrail Test Item", pid=800)
        rows = get_inventory_rows(self.db_path)
        self.assertEqual(len(rows), 1)
        row_id = rows[0]["Row_ID"]

        app = App()
        app.withdraw()

        try:
            # 1. Test _on_delete_inventory_row CANCEL
            with patch("tkinter.messagebox.askyesno", return_value=False):
                app._on_delete_inventory_row(str(row_id))
            self.assertEqual(len(get_inventory_rows(self.db_path)), 1, "Item deleted despite user cancelling!")

            # 2. Test _on_delete_inventory_row CONFIRM
            with patch("tkinter.messagebox.askyesno", return_value=True):
                app._on_delete_inventory_row(str(row_id))
            self.assertEqual(len(get_inventory_rows(self.db_path)), 0, "Item was not deleted after confirmation!")

            # Re-seed for clear_inventory test
            self._seed_sample_item(name="Pantry Item", pid=801)
            self.assertEqual(len(get_inventory_rows(self.db_path)), 1)

            # 3. Test on_clear_inventory CANCEL
            with patch("tkinter.messagebox.askyesno", return_value=False):
                app.on_clear_inventory()
            self.assertEqual(len(get_inventory_rows(self.db_path)), 1, "Inventory wiped despite user cancelling!")

            # 4. Test on_clear_inventory CONFIRM
            with patch("tkinter.messagebox.askyesno", return_value=True):
                app.on_clear_inventory()
            self.assertEqual(len(get_inventory_rows(self.db_path)), 0, "Inventory not wiped after confirmation!")

        finally:
            app.destroy()


class TestInventoryEditDialogAdversarialInteractions(BaseAdversarialInventoryTestCase):
    """
    Direct stress testing of InventoryItemEditDialog and MacroPromptDialog GUI classes.
    """

    def test_dialog_validation_error_aggregation(self):
        self._seed_sample_item(pid=900)
        row_id = get_inventory_rows(self.db_path)[0]["Row_ID"]

        dlg = InventoryItemEditDialog(self.root, row_id=row_id, db_path=self.db_path, wait=False)

        # Inject invalid form inputs: empty name, negative unit weight, intact=0 and rem=0
        dlg.v_name.set("")
        dlg.v_location.set("")
        dlg.v_unit_weight.set("-10.0")
        dlg.v_intact.set("0")
        dlg.v_remainder.set("0.0")
        dlg.v_kcal.set("")

        # Save should fail and capture validation errors
        with patch("tkinter.messagebox.showerror") as mock_err:
            saved = dlg.save()
            mock_err.assert_called_once()
        self.assertFalse(saved)
        self.assertIsNotNone(dlg.last_error)
        self.assertIn("Name cannot be empty", dlg.last_error)
        self.assertIn("Location cannot be empty", dlg.last_error)
        self.assertIn("Total quantity cannot be 0", dlg.last_error)
        self.assertIn("Kcal is required", dlg.last_error)
        dlg.destroy()

    def test_macro_prompt_dialog_button_flows(self):
        # Test Option 1 button
        dlg1 = MacroPromptDialog(self.root, product_name="Test Prod", product_id=1, wait=False)
        dlg1.choose_single()
        self.assertEqual(dlg1.result, "single")

        # Test Option 2 button
        dlg2 = MacroPromptDialog(self.root, product_name="Test Prod", product_id=1, wait=False)
        dlg2.choose_propagate()
        self.assertEqual(dlg2.result, "propagate")

        # Test Cancel button
        dlg3 = MacroPromptDialog(self.root, product_name="Test Prod", product_id=1, wait=False)
        dlg3.cancel()
        self.assertIsNone(dlg3.result)

    def test_dialog_macro_cancel_aborts_save(self):
        """
        When editing macros on a linked product, if the user cancels the MacroPromptDialog,
        the edit dialog must abort saving and leave database untouched.
        """
        # Create linked product (2 inventory rows)
        self._seed_sample_item(name="Linked Item", pid=950)
        add_item("Linked Item", 0.5, 1, "pantry", "2027-01-01", db_path=self.db_path)
        row_id = get_inventory_rows(self.db_path)[0]["Row_ID"]

        dlg = InventoryItemEditDialog(self.root, row_id=row_id, db_path=self.db_path, wait=False)
        dlg.v_kcal.set("999.0")  # Change kcal

        # Simulate user cancelling the macro prompt
        with patch.object(MacroPromptDialog, "__init__", lambda self, parent, product_name, product_id, wait=True: setattr(self, "result", None)):
            saved = dlg.save()

        self.assertFalse(saved)
        # Verify DB was NOT updated
        item = get_inventory_item(row_id, db_path=self.db_path)
        self.assertAlmostEqual(item["Kcal_100g"], 250.0)
        dlg.destroy()


class TestTransactionAbortAndRollbackAtomicity(BaseAdversarialInventoryTestCase):
    """
    Adversarial verification of transaction atomicity and rollback safety.
    Ensures no dirty or half-written records persist on errors.
    """

    def test_internal_transaction_rollback_on_failure(self):
        """
        Simulate an unexpected error during update_inventory_item in Option 1
        after new product insertion but before completion.
        """
        # Seed product with 2 aliases so it is linked
        self._seed_sample_item(name="Atomicity Pasta", pid=1000)
        conn = get_connection(self.db_path)
        conn.execute("INSERT INTO ALIASES (Name, ID) VALUES ('Atomicity Alias', 1000)")
        conn.commit()
        conn.close()

        row_id = get_inventory_rows(self.db_path)[0]["Row_ID"]

        # Mock connection and cursor to fail on INVENTORY update
        real_conn = get_connection(self.db_path)
        real_cur = real_conn.cursor()

        proxy_conn = MagicMock(wraps=real_conn)
        proxy_cur = MagicMock(wraps=real_cur)

        def faulty_execute(sql, *args, **kwargs):
            if "UPDATE INVENTORY SET" in str(sql):
                raise sqlite3.OperationalError("Simulated disk error during inventory update")
            return real_cur.execute(sql, *args, **kwargs)

        proxy_cur.execute.side_effect = faulty_execute
        proxy_conn.cursor.return_value = proxy_cur
        proxy_conn.commit.side_effect = real_conn.commit
        proxy_conn.rollback.side_effect = real_conn.rollback
        proxy_conn.close.side_effect = real_conn.close

        with patch("meal_planner.inventory.get_connection", return_value=proxy_conn):
            with self.assertRaises(sqlite3.OperationalError):
                update_inventory_item(
                    row_id=row_id,
                    macro_fields={"Kcal_100g": 500.0},
                    propagate=False,
                    db_path=self.db_path,
                )

        # Verify rollback: no new product ID was committed to PRODUCTS
        conn = get_connection(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT ID FROM PRODUCTS WHERE Kcal_100g = 500.0")
        self.assertIsNone(cur.fetchone(), "Zombie product record was left un-rolled back!")
        conn.close()

    def test_external_connection_caller_controlled_transaction(self):
        """
        Verify that passing an external connection preserves caller-managed transaction semantics.
        """
        self._seed_sample_item(name="Batch Item", pid=1100)
        row_id = get_inventory_rows(self.db_path)[0]["Row_ID"]

        conn = get_connection(self.db_path)
        # Update within external transaction
        update_inventory_item(
            conn=conn,
            row_id=row_id,
            item_fields={"Location": "external_tx_location"},
        )
        # Rollback external transaction
        conn.rollback()
        conn.close()

        # Location must NOT be changed in the database
        item = get_inventory_item(row_id, db_path=self.db_path)
        self.assertEqual(item["Location"], "pantry")




# ======================================================================
# SECTION: tests/test_challenger_m1_2.py
# ======================================================================

"""
Milestone M1 Challenger 2 Empirical Stress Test Suite.
Exhaustively and adversarially stress-tests:
1. Inventory item deletion across states (1 pkg, multiple pkgs, expired, open remainder, combined, pcs/kg/L).
2. Foreign key preservation & referential integrity (PRODUCTS, ALIASES, RECIPES, RECIPE_INGREDIENTS, GROUPS, SHOPPING_LIST).
3. Cancel on delete confirmation dialog preserves item completely in DB and UI.
4. Table widget click disambiguation: delete button (#0 / tree) vs row selection (#1..#N) and double clicks.
5. Invalid inputs to editing dialog & backend (bad dates, negative quantities, non-numerics, zero total qty).
6. Production disk inventory.db row counts preservation (36, 3, 14, 30).
"""

import os
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import tkinter as tk

from meal_planner.db.connection import get_connection
from meal_planner.db.schema import create_schema
from meal_planner.db.schema import migrate
from meal_planner.gui.app import App
from meal_planner.gui.dialogs import InventoryItemEditDialog, MacroPromptDialog
from meal_planner.gui.widgets import Table, create_delete_icon
from meal_planner.inventory import (
    AliasConflict,
    InvalidInput,
    ProductNotFound,
    add_item,
    delete_inventory_item,
    get_inventory_item,
    get_inventory_rows,
    get_next_product_id,
    is_product_macro_linked,
    update_inventory_item,
)


class BaseIsolatedTestCase(unittest.TestCase):
    """Ensures 100% database isolation using temporary SQLite databases."""
    @classmethod
    def setUpClass(cls):
        cls.root = tk.Tk()
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:
            pass

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp.name
        self.tmp.close()

        self._orig_env = os.environ.get("MEAL_PLANNER_DB")
        os.environ["MEAL_PLANNER_DB"] = self.db_path

        conn = get_connection(self.db_path)
        create_schema(conn)
        migrate(conn)
        conn.close()

    def tearDown(self):
        if self._orig_env is not None:
            os.environ["MEAL_PLANNER_DB"] = self._orig_env
        else:
            os.environ.pop("MEAL_PLANNER_DB", None)

        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except OSError:
                pass


class TestInventoryDeletionStates(BaseIsolatedTestCase):
    """Requirement 1: Test deletion across all inventory package states."""

    def test_delete_item_single_package_remains(self):
        # 1 intact package, 0 remainder
        add_item("Farina 00", 1.0, 1, "pantry", "2027-01-01",
                 fallback_id=101, new_product_unit="kg",
                 new_product_macros={"Kcal_100g": 350, "Protein_100g": 10, "Carbs_100g": 70, "Fat_100g": 1},
                 db_path=self.db_path)
        rows = get_inventory_rows(self.db_path)
        self.assertEqual(len(rows), 1)
        row_id = rows[0]["Row_ID"]
        self.assertEqual(rows[0]["Intact_Items"], 1)
        self.assertEqual(rows[0]["Open_Remainder"], 0.0)

        # Delete single package item
        deleted = delete_inventory_item(row_id=row_id, db_path=self.db_path)
        self.assertTrue(deleted)
        self.assertEqual(get_inventory_rows(self.db_path), [])
        self.assertIsNone(get_inventory_item(row_id, db_path=self.db_path))

    def test_delete_item_multiple_packages_remains(self):
        # Multiple intact packages (e.g. 10 packages)
        add_item("Tonno in scatola", 0.08, 10, "pantry", "2028-12-31",
                 fallback_id=102, new_product_unit="kg",
                 new_product_macros={"Kcal_100g": 130, "Protein_100g": 26, "Carbs_100g": 0, "Fat_100g": 2.5},
                 db_path=self.db_path)
        rows = get_inventory_rows(self.db_path)
        row_id = rows[0]["Row_ID"]
        self.assertEqual(rows[0]["Intact_Items"], 10)

        # Deleting the item removes the entire row, not just one package
        deleted = delete_inventory_item(row_id=row_id, db_path=self.db_path)
        self.assertTrue(deleted)
        self.assertEqual(get_inventory_rows(self.db_path), [])

    def test_delete_expired_package(self):
        # Expired package with past expiration date
        add_item("Yogurt Scaduto", 0.125, 2, "fridge", "2021-03-15",
                 fallback_id=103, new_product_unit="kg",
                 new_product_macros={"Kcal_100g": 60, "Protein_100g": 4, "Carbs_100g": 5, "Fat_100g": 3},
                 db_path=self.db_path)
        rows = get_inventory_rows(self.db_path)
        row_id = rows[0]["Row_ID"]
        self.assertEqual(rows[0]["Expiration_Date"], "2021-03-15")

        deleted = delete_inventory_item(row_id=row_id, db_path=self.db_path)
        self.assertTrue(deleted)
        self.assertEqual(get_inventory_rows(self.db_path), [])

    def test_delete_open_remainder_only(self):
        # Item with 0 intact packages and only open remainder (e.g. 0.35 kg left)
        conn = get_connection(self.db_path)
        conn.execute("INSERT INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g) VALUES (104, 'kg', 200, 5, 30, 2)")
        conn.execute("INSERT INTO ALIASES (Name, ID) VALUES ('Marmellata aperta', 104)")
        conn.execute("INSERT INTO INVENTORY (ID, Unit_Weight, Intact_Items, Open_Remainder, Location) VALUES (104, 0.5, 0, 0.35, 'fridge')")
        conn.commit()
        conn.close()

        rows = get_inventory_rows(self.db_path)
        self.assertEqual(len(rows), 1)
        row_id = rows[0]["Row_ID"]
        self.assertEqual(rows[0]["Intact_Items"], 0)
        self.assertEqual(rows[0]["Open_Remainder"], 0.35)

        deleted = delete_inventory_item(row_id=row_id, db_path=self.db_path)
        self.assertTrue(deleted)
        self.assertEqual(get_inventory_rows(self.db_path), [])

    def test_delete_combined_intact_and_open_remainder(self):
        # Item with both intact packages and an open remainder
        conn = get_connection(self.db_path)
        conn.execute("INSERT INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g) VALUES (105, 'L', 45, 3.2, 4.8, 1.5)")
        conn.execute("INSERT INTO ALIASES (Name, ID) VALUES ('Latte Aperto e Scorte', 105)")
        conn.execute("INSERT INTO INVENTORY (ID, Unit_Weight, Intact_Items, Open_Remainder, Location) VALUES (105, 1.0, 3, 0.65, 'fridge')")
        conn.commit()
        conn.close()

        rows = get_inventory_rows(self.db_path)
        row_id = rows[0]["Row_ID"]
        self.assertEqual(rows[0]["Intact_Items"], 3)
        self.assertEqual(rows[0]["Open_Remainder"], 0.65)

        deleted = delete_inventory_item(row_id=row_id, db_path=self.db_path)
        self.assertTrue(deleted)
        self.assertEqual(get_inventory_rows(self.db_path), [])

    def test_delete_one_item_preserves_other_inventory_rows_of_same_product(self):
        # Product 106 has TWO inventory rows (e.g. 1 in pantry, 1 in freezer)
        conn = get_connection(self.db_path)
        conn.execute("INSERT INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g) VALUES (106, 'kg', 250, 20, 0, 15)")
        conn.execute("INSERT INTO ALIASES (Name, ID) VALUES ('Carne Macinata', 106)")
        conn.execute("INSERT INTO INVENTORY (Row_ID, ID, Unit_Weight, Intact_Items, Open_Remainder, Location) VALUES (501, 106, 0.5, 2, 0, 'fridge')")
        conn.execute("INSERT INTO INVENTORY (Row_ID, ID, Unit_Weight, Intact_Items, Open_Remainder, Location) VALUES (502, 106, 0.5, 4, 0, 'freezer')")
        conn.commit()
        conn.close()

        # Delete row 501 (fridge)
        deleted = delete_inventory_item(row_id=501, db_path=self.db_path)
        self.assertTrue(deleted)

        # Row 502 (freezer) must remain untouched
        remaining = get_inventory_rows(self.db_path)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["Row_ID"], 502)
        self.assertEqual(remaining[0]["Location"], "freezer")
        self.assertEqual(remaining[0]["Intact_Items"], 4)

    def test_delete_inventory_item_validation_adversarial(self):
        # Non-existent ID returns False
        self.assertFalse(delete_inventory_item(row_id=999999, db_path=self.db_path))

        # Invalid row ID inputs must raise InvalidInput
        for bad_id in [None, -1, 0, -999, "invalid", "1.5", "", [], {}]:
            with self.assertRaises(InvalidInput):
                delete_inventory_item(row_id=bad_id, db_path=self.db_path)


class TestForeignKeyPreservation(BaseIsolatedTestCase):
    """Requirement 2: Verify deletion of inventory item preserves PRODUCTS and ALIASES."""

    def test_delete_inventory_preserves_product_and_aliases(self):
        conn = get_connection(self.db_path)
        pid = 201
        conn.execute(
            """
            INSERT INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g, Sugars_100g)
            VALUES (?, 'kg', 400.0, 15.0, 60.0, 10.0, 5.0)
            """,
            (pid,)
        )
        # Register two aliases for this product
        conn.execute("INSERT INTO ALIASES (Name, ID) VALUES ('Muesli Frutta', ?)", (pid,))
        conn.execute("INSERT INTO ALIASES (Name, ID) VALUES ('Granola Crunchy', ?)", (pid,))
        # Register inventory row
        conn.execute(
            "INSERT INTO INVENTORY (Row_ID, ID, Unit_Weight, Intact_Items, Open_Remainder, Location) VALUES (888, ?, 0.75, 2, 0, 'pantry')",
            (pid,)
        )
        conn.commit()

        # Sanity check foreign keys before delete
        fk_errors_before = list(conn.execute("PRAGMA foreign_key_check"))
        self.assertEqual(fk_errors_before, [])

        # Perform deletion of the inventory row
        deleted = delete_inventory_item(conn, 888)
        conn.commit()
        self.assertTrue(deleted)

        # 1. INVENTORY row is gone
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM INVENTORY WHERE Row_ID = 888")
        self.assertEqual(c.fetchone()[0], 0)

        # 2. PRODUCT row STILL EXISTS and macros are unchanged
        c.execute("SELECT Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g, Sugars_100g FROM PRODUCTS WHERE ID = ?", (pid,))
        prod = c.fetchone()
        self.assertIsNotNone(prod, "Product record was unexpectedly deleted!")
        self.assertEqual(prod[0], "kg")
        self.assertEqual(prod[1], 400.0)
        self.assertEqual(prod[2], 15.0)
        self.assertEqual(prod[3], 60.0)
        self.assertEqual(prod[4], 10.0)
        self.assertEqual(prod[5], 5.0)

        # 3. ALIASES rows STILL EXIST
        c.execute("SELECT Name FROM ALIASES WHERE ID = ? ORDER BY Name ASC", (pid,))
        aliases = [r[0] for r in c.fetchall()]
        self.assertEqual(aliases, ["Granola Crunchy", "Muesli Frutta"])

        # 4. Foreign key check has 0 violations
        fk_errors_after = list(conn.execute("PRAGMA foreign_key_check"))
        self.assertEqual(fk_errors_after, [])

        # 5. Can add back into inventory without re-creating product or alias
        conn.execute("INSERT INTO INVENTORY (Row_ID, ID, Unit_Weight, Intact_Items, Open_Remainder, Location) VALUES (889, ?, 0.75, 1, 0, 'pantry')", (pid,))
        conn.commit()
        rows = get_inventory_rows(conn=conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ID"], pid)
        self.assertEqual(rows[0]["Kcal_100g"], 400.0)
        conn.close()

    def test_delete_inventory_preserves_recipe_associations(self):
        conn = get_connection(self.db_path)
        pid = 202
        conn.execute("INSERT INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g) VALUES (?, 'kg', 120, 20, 0, 4)", (pid,))
        conn.execute("INSERT INTO ALIASES (Name, ID) VALUES ('Merluzzo', ?)", (pid,))
        conn.execute("INSERT INTO INVENTORY (Row_ID, ID, Unit_Weight, Intact_Items, Open_Remainder, Location) VALUES (901, ?, 0.4, 2, 0, 'freezer')", (pid,))

        # Create recipe referencing this product
        conn.execute("INSERT INTO RECIPES (Recipe_ID, Name) VALUES (10, 'Merluzzo al vapore')")
        conn.execute("INSERT INTO RECIPE_INGREDIENTS (Recipe_ID, ID, Quantity, Unit) VALUES (10, ?, 0.2, 'kg')", (pid,))
        conn.commit()

        # Delete inventory row
        deleted = delete_inventory_item(conn, 901)
        conn.commit()
        self.assertTrue(deleted)

        # Recipe and ingredients must remain completely intact
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM RECIPES WHERE Recipe_ID = 10")
        self.assertEqual(c.fetchone()[0], 1)
        c.execute("SELECT ID, Quantity, Unit FROM RECIPE_INGREDIENTS WHERE Recipe_ID = 10")
        ing = c.fetchone()
        self.assertEqual(ing[0], pid)
        self.assertEqual(ing[1], 0.2)
        self.assertEqual(ing[2], "kg")

        # Foreign keys check must pass
        self.assertEqual(list(conn.execute("PRAGMA foreign_key_check")), [])
        conn.close()


class TestDeleteConfirmationDialogCancel(BaseIsolatedTestCase):
    """Requirement 3: Verify cancel on delete confirmation dialog preserves inventory item completely."""

    def test_cancel_delete_confirmation_preserves_item(self):
        add_item("Olio Extravergine", 1.0, 2, "pantry", "2027-06-30",
                 fallback_id=301, new_product_unit="L",
                 new_product_macros={"Kcal_100g": 824, "Protein_100g": 0, "Carbs_100g": 0, "Fat_100g": 91.6},
                 db_path=self.db_path)
        rows = get_inventory_rows(self.db_path)
        row_id = rows[0]["Row_ID"]

        app = App()
        app.db_path = self.db_path
        app.refresh()

        try:
            # Verify table has item initially
            self.assertIn(str(row_id), app.t_disp.tv.get_children())

            # User clicks 'No' (Cancel) in confirmation dialog
            with patch("tkinter.messagebox.askyesno", return_value=False) as mock_ask:
                app._on_delete_inventory_row(str(row_id))
                mock_ask.assert_called_once()

            # Item must STILL EXIST in DB with identical values
            item = get_inventory_item(row_id, db_path=self.db_path)
            self.assertIsNotNone(item, "Item was deleted despite user clicking Cancel!")
            self.assertEqual(item["Name"], "Olio Extravergine")
            self.assertEqual(item["Unit_Weight"], 1.0)
            self.assertEqual(item["Intact_Items"], 2)
            self.assertEqual(item["Open_Remainder"], 0.0)
            self.assertEqual(item["Location"], "pantry")
            self.assertEqual(item["Expiration_Date"], "2027-06-30")

            # Status bar must NOT report deleted
            self.assertNotIn("Deleted", app.status.get())

            # Table still contains the item
            self.assertIn(str(row_id), app.t_disp.tv.get_children())

        finally:
            try:
                app.destroy()
            except Exception:
                pass

    def test_confirm_delete_removes_item(self):
        add_item("Caffe in grani", 0.5, 1, "pantry", "2027-12-31",
                 fallback_id=302, new_product_unit="kg",
                 new_product_macros={"Kcal_100g": 200, "Protein_100g": 10, "Carbs_100g": 30, "Fat_100g": 2},
                 db_path=self.db_path)
        row_id = get_inventory_rows(self.db_path)[0]["Row_ID"]

        app = App()
        app.db_path = self.db_path
        app.refresh()

        try:
            # User clicks 'Yes' (Confirm)
            with patch("tkinter.messagebox.askyesno", return_value=True) as mock_ask:
                app._on_delete_inventory_row(str(row_id))
                mock_ask.assert_called_once()

            # Item is deleted
            self.assertIsNone(get_inventory_item(row_id, db_path=self.db_path))
            self.assertNotIn(str(row_id), app.t_disp.tv.get_children())
            self.assertIn("Deleted 'Caffe in grani'", app.status.get())

        finally:
            try:
                app.destroy()
            except Exception:
                pass


class TestTableWidgetClickDisambiguation(BaseIsolatedTestCase):
    """Requirement 4: Verify delete button click vs row select click disambiguation."""

    def test_click_disambiguation_column_zero_vs_data_columns(self):
        frame = tk.Frame(self.root)
        on_delete_mock = MagicMock()
        on_select_mock = MagicMock()
        on_double_click_mock = MagicMock()

        table = Table(
            frame,
            [("p", "Product", 200), ("q", "Qty", 80), ("u", "Unit", 60)],
            height=6,
            show_delete=True,
            on_delete=on_delete_mock,
            on_select=on_select_mock,
            on_double_click=on_double_click_mock,
        )
        table.pack()

        table.fill([
            (("Pane Integrale", "1", "kg"), None, 701),
            (("Latte Intero", "2", "L"), None, 702),
        ])

        mock_event = MagicMock()

        # 1. Single click on column #0 (Delete Icon)
        mock_event.x = 15
        mock_event.y = 25
        table.tv.identify = MagicMock(return_value="cell")
        table.tv.identify_row = MagicMock(return_value="701")
        table.tv.identify_column = MagicMock(return_value="#0")

        table._handle_click(mock_event)
        on_delete_mock.assert_called_once_with("701")
        on_select_mock.assert_not_called()
        on_double_click_mock.assert_not_called()

        # Reset mocks
        on_delete_mock.reset_mock()
        on_select_mock.reset_mock()
        on_double_click_mock.reset_mock()

        # 2. Single click on region "image" or "tree"
        table.tv.identify = MagicMock(return_value="image")
        table.tv.identify_row = MagicMock(return_value="701")
        table.tv.identify_column = MagicMock(return_value="#0")

        table._handle_click(mock_event)
        on_delete_mock.assert_called_once_with("701")
        on_select_mock.assert_not_called()

        # Reset mocks
        on_delete_mock.reset_mock()
        on_select_mock.reset_mock()

        # 3. Single click on data column #1 (Product Name)
        table.tv.identify = MagicMock(return_value="cell")
        table.tv.identify_row = MagicMock(return_value="701")
        table.tv.identify_column = MagicMock(return_value="#1")

        table._handle_click(mock_event)
        on_select_mock.assert_called_once_with("701")
        on_delete_mock.assert_not_called()

        # Reset mocks
        on_delete_mock.reset_mock()
        on_select_mock.reset_mock()

        # 4. Single click on data column #2 (Qty)
        table.tv.identify = MagicMock(return_value="cell")
        table.tv.identify_row = MagicMock(return_value="702")
        table.tv.identify_column = MagicMock(return_value="#2")

        table._handle_click(mock_event)
        on_select_mock.assert_called_once_with("702")
        on_delete_mock.assert_not_called()

        # Reset mocks
        on_delete_mock.reset_mock()
        on_select_mock.reset_mock()

        # 5. Double click on column #0 (Delete Icon) -> still only triggers on_delete
        table.tv.identify = MagicMock(return_value="cell")
        table.tv.identify_row = MagicMock(return_value="701")
        table.tv.identify_column = MagicMock(return_value="#0")

        table._handle_double_click(mock_event)
        on_delete_mock.assert_called_once_with("701")
        on_double_click_mock.assert_not_called()
        on_select_mock.assert_not_called()

        # Reset mocks
        on_delete_mock.reset_mock()
        on_select_mock.reset_mock()
        on_double_click_mock.reset_mock()

        # 6. Double click on data column #1 -> triggers on_double_click
        table.tv.identify = MagicMock(return_value="cell")
        table.tv.identify_row = MagicMock(return_value="702")
        table.tv.identify_column = MagicMock(return_value="#1")

        table._handle_double_click(mock_event)
        on_double_click_mock.assert_called_once_with("702")
        on_delete_mock.assert_not_called()

        # 7. Click on heading -> neither is called
        on_select_mock.reset_mock()
        on_delete_mock.reset_mock()
        table.tv.identify = MagicMock(return_value="heading")
        table._handle_click(mock_event)
        on_select_mock.assert_not_called()
        on_delete_mock.assert_not_called()

        # 8. Click on blank area (no row) -> neither is called
        table.tv.identify = MagicMock(return_value="cell")
        table.tv.identify_row = MagicMock(return_value="")
        table._handle_click(mock_event)
        on_select_mock.assert_not_called()
        on_delete_mock.assert_not_called()

    def test_delete_icon_visual_style_conformance(self):
        # Verify create_delete_icon creates a valid PhotoImage
        icon_light = create_delete_icon(master=self.root, size=20, bg_color="#fef2f2", border_color="#991b1b", cross_color="#7f1d1d")
        self.assertIsNotNone(icon_light)
        self.assertEqual(icon_light.width(), 20)
        self.assertEqual(icon_light.height(), 20)

        # Dark theme icon
        icon_dark = create_delete_icon(master=self.root, size=20, bg_color="#2a1518", border_color="#b91c1c", cross_color="#ef4444")
        self.assertIsNotNone(icon_dark)
        self.assertEqual(icon_dark.width(), 20)
        self.assertEqual(icon_dark.height(), 20)


class TestEditingDialogInputValidation(BaseIsolatedTestCase):
    """Requirement 5: Verify invalid inputs to editing dialog."""

    def setUp(self):
        super().setUp()
        add_item("Farro Perlato", 0.5, 3, "dispensa", "2026-12-31",
                 fallback_id=401, new_product_unit="kg",
                 new_product_macros={"Kcal_100g": 335, "Protein_100g": 14, "Carbs_100g": 65, "Fat_100g": 2.5},
                 db_path=self.db_path)
        self.row_id = get_inventory_rows(self.db_path)[0]["Row_ID"]

    def test_invalid_date_strings_rejected(self):
        invalid_dates = [
            "2026-02-30",      # Invalid February date
            "2026-11-31",      # November has 30 days
            "2026-13-01",      # Month 13
            "2026-00-10",      # Month 00
            "31-12-2026",      # DD-MM-YYYY format
            "2026/12/31",      # Slash separator
            "not-a-date",      # Non-date string
            "2026-1-1",        # Missing leading zeroes
            "2026-12-32",      # Day 32
        ]

        dlg = InventoryItemEditDialog(self.root, row_id=self.row_id, db_path=self.db_path, wait=False)
        try:
            for bad_date in invalid_dates:
                dlg.v_expiration.set(bad_date)
                with patch("tkinter.messagebox.showerror") as mock_err:
                    saved = dlg.save()
                    self.assertFalse(saved, f"Invalid date '{bad_date}' was accepted!")
                    mock_err.assert_called()
                    self.assertIn("Expiration", dlg.last_error)
        finally:
            dlg.destroy()

        # Verify DB remained untouched
        item = get_inventory_item(self.row_id, db_path=self.db_path)
        self.assertEqual(item["Expiration_Date"], "2026-12-31")

    def test_negative_and_zero_quantities_rejected(self):
        dlg = InventoryItemEditDialog(self.root, row_id=self.row_id, db_path=self.db_path, wait=False)
        try:
            # 1. Negative Unit_Weight
            dlg.v_unit_weight.set("-0.5")
            with patch("tkinter.messagebox.showerror"):
                self.assertFalse(dlg.save())
                self.assertIn("Unit Weight", dlg.last_error)
            dlg.v_unit_weight.set("0.0")
            with patch("tkinter.messagebox.showerror"):
                self.assertFalse(dlg.save())
                self.assertIn("Unit Weight", dlg.last_error)
            dlg.v_unit_weight.set("0.5")  # restore

            # 2. Negative Intact_Items
            dlg.v_intact.set("-2")
            with patch("tkinter.messagebox.showerror"):
                self.assertFalse(dlg.save())
                self.assertIn("Intact Packages", dlg.last_error)
            dlg.v_intact.set("3")  # restore

            # 3. Negative Open_Remainder
            dlg.v_remainder.set("-0.1")
            with patch("tkinter.messagebox.showerror"):
                self.assertFalse(dlg.save())
                self.assertIn("Open Remainder", dlg.last_error)
            dlg.v_remainder.set("0.0")  # restore

            # 4. Zero Total Quantity (intact=0 and remainder=0)
            dlg.v_intact.set("0")
            dlg.v_remainder.set("0")
            with patch("tkinter.messagebox.showerror"):
                self.assertFalse(dlg.save())
                self.assertIn("Total quantity cannot be 0", dlg.last_error)

        finally:
            dlg.destroy()

    def test_non_numeric_values_rejected(self):
        dlg = InventoryItemEditDialog(self.root, row_id=self.row_id, db_path=self.db_path, wait=False)
        try:
            for bad_val in ["abc", "NaN", "nan", "inf", "-inf", "+inf", "None", "$10", "invalid", "++1", "foo_bar"]:
                dlg.v_unit_weight.set(bad_val)
                with patch("tkinter.messagebox.showerror"):
                    self.assertFalse(dlg.save())
                dlg.v_unit_weight.set("0.5")

                dlg.v_intact.set(bad_val)
                with patch("tkinter.messagebox.showerror"):
                    self.assertFalse(dlg.save())
                dlg.v_intact.set("3")

                dlg.v_kcal.set(bad_val)
                with patch("tkinter.messagebox.showerror"):
                    self.assertFalse(dlg.save())
                dlg.v_kcal.set("335")

        finally:
            dlg.destroy()

    def test_non_finite_infinity_behavior(self):
        """
        Adversarial edge case: test behavior when floating point 'inf' is supplied.
        Verifies that float('inf') is rejected by validation and not written to DB.
        """
        dlg = InventoryItemEditDialog(self.root, row_id=self.row_id, db_path=self.db_path, wait=False)
        try:
            dlg.v_remainder.set("inf")
            with patch("tkinter.messagebox.showerror"):
                saved = dlg.save()
            self.assertFalse(saved, "Dialog must reject non-finite 'inf'")
            self.assertIsNotNone(dlg.last_error)
            self.assertIn("Open Remainder", dlg.last_error)

            # Directly verify DB content was untouched
            conn = get_connection(self.db_path)
            cur = conn.cursor()
            cur.execute("SELECT Open_Remainder FROM INVENTORY WHERE Row_ID = ?", (self.row_id,))
            val = cur.fetchone()[0]
            conn.close()
            self.assertNotEqual(val, float("inf"))
        finally:
            try:
                dlg.destroy()
            except Exception:
                pass

    def test_negative_macros_and_empty_required_macros_rejected(self):
        dlg = InventoryItemEditDialog(self.root, row_id=self.row_id, db_path=self.db_path, wait=False)
        try:
            # Negative macros
            for field_var, name in [
                (dlg.v_kcal, "Kcal"),
                (dlg.v_protein, "Protein"),
                (dlg.v_carbs, "Carbs"),
                (dlg.v_fat, "Fat"),
                (dlg.v_sugars, "Sugars"),
                (dlg.v_saturated_fat, "Saturated Fat"),
                (dlg.v_fiber, "Fiber"),
                (dlg.v_salt, "Salt"),
            ]:
                old = field_var.get()
                field_var.set("-5")
                with patch("tkinter.messagebox.showerror"):
                    self.assertFalse(dlg.save(), f"Negative {name} was accepted!")
                    self.assertIn(name, dlg.last_error)
                field_var.set(old)

            # Empty required macros
            for field_var, name in [
                (dlg.v_kcal, "Kcal"),
                (dlg.v_protein, "Protein"),
                (dlg.v_carbs, "Carbs"),
                (dlg.v_fat, "Fat"),
            ]:
                old = field_var.get()
                field_var.set("")
                with patch("tkinter.messagebox.showerror"):
                    self.assertFalse(dlg.save(), f"Empty required macro {name} was accepted!")
                    self.assertIn(name, dlg.last_error)
                field_var.set(old)

        finally:
            dlg.destroy()

    def test_empty_name_and_location_rejected(self):
        dlg = InventoryItemEditDialog(self.root, row_id=self.row_id, db_path=self.db_path, wait=False)
        try:
            dlg.v_name.set("   ")
            with patch("tkinter.messagebox.showerror"):
                self.assertFalse(dlg.save())
                self.assertIn("Name", dlg.last_error)
            dlg.v_name.set("Farro Perlato")

            dlg.v_location.set("   ")
            with patch("tkinter.messagebox.showerror"):
                self.assertFalse(dlg.save())
                self.assertIn("Location", dlg.last_error)

        finally:
            dlg.destroy()


class TestDiskDatabasePreservation(unittest.TestCase):
    """Requirement 6: Verify disk inventory.db row counts (36, 3, 14, 30) remain intact."""

    def test_disk_inventory_db_counts(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        db_file = os.path.join(repo_root, "data", "inventory.db")
        self.assertTrue(os.path.exists(db_file), f"Database file not found at {db_file}")

        conn = sqlite3.connect(db_file)
        c = conn.cursor()
        counts = {
            t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ["INVENTORY", "RECIPES", "PRODUCTS", "ALIASES"]
        }
        conn.close()

        self.assertEqual(counts["INVENTORY"], 36, f"Expected 36 INVENTORY rows, got {counts['INVENTORY']}")
        self.assertEqual(counts["RECIPES"], 3, f"Expected 3 RECIPES rows, got {counts['RECIPES']}")
        self.assertEqual(counts["PRODUCTS"], 14, f"Expected 14 PRODUCTS rows, got {counts['PRODUCTS']}")
        self.assertEqual(counts["ALIASES"], 30, f"Expected 30 ALIASES rows, got {counts['ALIASES']}")




# ======================================================================
# SECTION: tests/test_challenger_adversarial_m1.py
# ======================================================================

"""
Adversarial Stress Test Suite for Milestone M1 (Requirement R1):
- Complex alias graphs (products with 3+ aliases, multiple inventory rows, recipes, groups)
- Macro propagation Option 1 (isolated item decoupling, new product ID, clean alias handling, recipe preservation)
- Macro propagation Option 2 (in-place backward propagation to all linked rows, aliases, recipes)
- Cancel prompt verification (no modifications on cancel)
- Edge cases and adversarial inputs (validation boundaries, alias conflicts, zero quantity)
- Production database preservation invariant (inventory.db counts remain 36, 3, 14, 30)
"""

import os
import sqlite3
import tempfile
import tkinter as tk
from unittest.mock import MagicMock, patch
import unittest

from meal_planner.db.connection import get_connection
from meal_planner.db.schema import create_schema
from meal_planner.db.schema import migrate
from meal_planner.engine import recipe_macros
from meal_planner.gui.dialogs import InventoryItemEditDialog, MacroPromptDialog
from meal_planner.inventory import (
    AliasConflict,
    InvalidInput,
    ProductNotFound,
    delete_inventory_item,
    get_inventory_item,
    get_inventory_rows,
    get_next_product_id,
    is_product_macro_linked,
    update_inventory_item,
)


class BaseAdversarialTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tk.Tk()
        cls.root.withdraw()

        # Capture baseline inventory.db counts
        cls.prod_db_path = os.path.abspath("data/inventory.db")
        if os.path.exists(cls.prod_db_path):
            conn = sqlite3.connect(cls.prod_db_path)
            cur = conn.cursor()
            cls.baseline_counts = {
                "INVENTORY": cur.execute("SELECT COUNT(*) FROM INVENTORY").fetchone()[0],
                "RECIPES": cur.execute("SELECT COUNT(*) FROM RECIPES").fetchone()[0],
                "PRODUCTS": cur.execute("SELECT COUNT(*) FROM PRODUCTS").fetchone()[0],
                "ALIASES": cur.execute("SELECT COUNT(*) FROM ALIASES").fetchone()[0],
            }
            conn.close()
        else:
            cls.baseline_counts = None

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:
            pass

    def setUp(self):
        # Strict isolation: always use a fresh temporary SQLite database
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp.name
        self.tmp.close()

        self._orig_env = os.environ.get("MEAL_PLANNER_DB")
        os.environ["MEAL_PLANNER_DB"] = self.db_path

        conn = get_connection(self.db_path)
        create_schema(conn)
        migrate(conn)
        conn.close()

    def tearDown(self):
        if self._orig_env is not None:
            os.environ["MEAL_PLANNER_DB"] = self._orig_env
        else:
            os.environ.pop("MEAL_PLANNER_DB", None)

        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except OSError:
                pass

        # Invariant check: disk inventory.db must NEVER be touched
        if self.baseline_counts:
            conn = sqlite3.connect(self.prod_db_path)
            cur = conn.cursor()
            current_counts = {
                "INVENTORY": cur.execute("SELECT COUNT(*) FROM INVENTORY").fetchone()[0],
                "RECIPES": cur.execute("SELECT COUNT(*) FROM RECIPES").fetchone()[0],
                "PRODUCTS": cur.execute("SELECT COUNT(*) FROM PRODUCTS").fetchone()[0],
                "ALIASES": cur.execute("SELECT COUNT(*) FROM ALIASES").fetchone()[0],
            }
            conn.close()
            self.assertEqual(
                current_counts,
                self.baseline_counts,
                f"Production database inventory.db was modified! Current: {current_counts}, Baseline: {self.baseline_counts}",
            )


class TestComplexAliasGraphAndMacroPropagation(BaseAdversarialTestCase):
    """
    Stress-tests complex alias graphs with multiple inventory rows across
    different locations, linked recipes, and verify Option 1 vs Option 2.
    """

    def _setup_complex_graph(self):
        """
        Creates Product 100 with:
        - 4 aliases: 'Spaghetti N.5', 'Spaghetti Barilla', 'Spaghettoni', 'Pasta Lunga'
        - 4 inventory rows:
          Row 1: pantry, 0.5kg, intact=3, open=0.0, exp=2027-01-15
          Row 2: fridge, 0.5kg, intact=1, open=0.1, exp=2026-11-20
          Row 3: cellar, 1.0kg, intact=5, open=0.0, exp=2027-06-30
          Row 4: freezer, 0.25kg, intact=2, open=0.0, exp=2027-12-31
        - 2 recipes:
          Recipe 1: 'Carbonara' with 100g of Product 100
          Recipe 2: 'Amatriciana' with 80g of Product 100
        """
        conn = get_connection(self.db_path)
        cur = conn.cursor()

        # Product 100
        cur.execute(
            """
            INSERT INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g,
                                  Sugars_100g, Saturated_fat_100g, Fiber_100g, Salt_100g)
            VALUES (100, 'kg', 350.0, 12.0, 72.0, 1.5, 3.0, 0.3, 2.5, 0.01)
            """
        )

        # 4 aliases
        aliases = ['Spaghetti N.5', 'Spaghetti Barilla', 'Spaghettoni', 'Pasta Lunga']
        for a in aliases:
            cur.execute("INSERT INTO ALIASES (Name, ID) VALUES (?, 100)", (a,))

        # 4 inventory rows
        cur.execute(
            "INSERT INTO INVENTORY (ID, Unit_Weight, Intact_Items, Open_Remainder, Location, Expiration_Date) "
            "VALUES (100, 0.5, 3, 0.0, 'pantry', '2027-01-15')"
        )
        r1_id = cur.lastrowid

        cur.execute(
            "INSERT INTO INVENTORY (ID, Unit_Weight, Intact_Items, Open_Remainder, Location, Expiration_Date) "
            "VALUES (100, 0.5, 1, 0.1, 'fridge', '2026-11-20')"
        )
        r2_id = cur.lastrowid

        cur.execute(
            "INSERT INTO INVENTORY (ID, Unit_Weight, Intact_Items, Open_Remainder, Location, Expiration_Date) "
            "VALUES (100, 1.0, 5, 0.0, 'cellar', '2027-06-30')"
        )
        r3_id = cur.lastrowid

        cur.execute(
            "INSERT INTO INVENTORY (ID, Unit_Weight, Intact_Items, Open_Remainder, Location, Expiration_Date) "
            "VALUES (100, 0.25, 2, 0.0, 'freezer', '2027-12-31')"
        )
        r4_id = cur.lastrowid

        # 2 recipes
        cur.execute("INSERT INTO RECIPES (Recipe_ID, Name) VALUES (1, 'Carbonara')")
        cur.execute("INSERT INTO RECIPE_INGREDIENTS (Recipe_ID, ID, Quantity, Unit) VALUES (1, 100, 100, 'g')")

        cur.execute("INSERT INTO RECIPES (Recipe_ID, Name) VALUES (2, 'Amatriciana')")
        cur.execute("INSERT INTO RECIPE_INGREDIENTS (Recipe_ID, ID, Quantity, Unit) VALUES (2, 100, 80, 'g')")

        conn.commit()
        conn.close()

        return {
            "p_id": 100,
            "aliases": aliases,
            "rows": [r1_id, r2_id, r3_id, r4_id],
            "recipes": [1, 2],
        }

    def test_linkage_detection_on_complex_graph(self):
        """Verify is_product_macro_linked correctly detects linkage on complex alias graph."""
        graph = self._setup_complex_graph()
        self.assertTrue(is_product_macro_linked(graph["p_id"], db_path=self.db_path))

        conn = get_connection(self.db_path)
        self.assertTrue(is_product_macro_linked(conn, graph["p_id"]))
        conn.close()

    def test_option_1_macro_modification_complete_isolation(self):
        """
        Adversarially tests Option 1 ('Modify only that specific item'):
        1. Modifies macros of Row 2 (fridge row).
        2. Verifies Row 2 gets a newly allocated product ID.
        3. Verifies ONLY Row 2 changes its product ID and macros.
        4. Verifies Row 1, Row 3, and Row 4 retain Product 100 and original macros.
        5. Verifies all 4 original aliases remain bound to Product 100.
        6. Verifies recipes 1 and 2 retain Product 100 and their nutrition is unchanged.
        7. Performs a SECOND Option 1 edit on Row 4 (freezer row) and tests collision avoidance.
        """
        graph = self._setup_complex_graph()
        r1_id, r2_id, r3_id, r4_id = graph["rows"]

        # Compute initial recipe nutrition
        conn = get_connection(self.db_path)
        init_m1 = recipe_macros(conn, 1)
        init_m2 = recipe_macros(conn, 2)
        conn.close()
        # Recipe 1 has 100g of pasta (350 kcal, 12g protein)
        self.assertAlmostEqual(init_m1["Kcal"], 350.0, places=2)
        self.assertAlmostEqual(init_m1["Protein"], 12.0, places=2)

        # 1. Edit Row 2 with Option 1 (propagate=False)
        res = update_inventory_item(
            row_id=r2_id,
            macro_fields={"Kcal_100g": 450, "Protein_100g": 18, "Carbs_100g": 65, "Fat_100g": 3.5},
            propagate=False,
            db_path=self.db_path,
        )

        self.assertEqual(res["outcome"], "ok")
        self.assertTrue(res["created_new_product"])
        new_pid_1 = res["product_id"]
        self.assertNotEqual(new_pid_1, 100)

        # Verify Row 2 characteristics
        item2 = get_inventory_item(r2_id, db_path=self.db_path)
        self.assertEqual(item2["ID"], new_pid_1)
        self.assertEqual(item2["Kcal_100g"], 450.0)
        self.assertEqual(item2["Protein_100g"], 18.0)
        self.assertEqual(item2["Location"], "fridge")
        self.assertEqual(item2["Open_Remainder"], 0.1)

        # Verify other rows (Row 1, Row 3, Row 4) are completely untouched
        for r_id in (r1_id, r3_id, r4_id):
            it = get_inventory_item(r_id, db_path=self.db_path)
            self.assertEqual(it["ID"], 100, f"Row {r_id} should still point to product 100")
            self.assertEqual(it["Kcal_100g"], 350.0, f"Row {r_id} Kcal should remain 350.0")
            self.assertEqual(it["Protein_100g"], 12.0, f"Row {r_id} Protein should remain 12.0")

        # Verify original aliases are intact on Product 100
        conn = get_connection(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT Name FROM ALIASES WHERE ID = 100 ORDER BY Name ASC")
        current_aliases_100 = [row[0] for row in cur.fetchall()]
        self.assertEqual(sorted(current_aliases_100), sorted(graph["aliases"]))

        # Verify alias for new_pid_1 was created
        cur.execute("SELECT Name FROM ALIASES WHERE ID = ?", (new_pid_1,))
        new_aliases = [row[0] for row in cur.fetchall()]
        self.assertGreaterEqual(len(new_aliases), 1)
        # Disambiguated alias should start with original name
        self.assertTrue(any("Spaghetti" in a for a in new_aliases))

        # Verify recipes still point to Product 100 with initial nutrition
        post_m1 = recipe_macros(conn, 1)
        post_m2 = recipe_macros(conn, 2)
        conn.close()

        self.assertAlmostEqual(post_m1["Kcal"], init_m1["Kcal"], places=2)
        self.assertAlmostEqual(post_m1["Protein"], init_m1["Protein"], places=2)
        self.assertAlmostEqual(post_m2["Kcal"], init_m2["Kcal"], places=2)
        self.assertAlmostEqual(post_m2["Protein"], init_m2["Protein"], places=2)

        # 2. Perform a SECOND Option 1 edit on Row 4 (freezer row) keeping default name
        res2 = update_inventory_item(
            row_id=r4_id,
            macro_fields={"Kcal_100g": 280, "Protein_100g": 8, "Carbs_100g": 55, "Fat_100g": 1.0},
            propagate=False,
            db_path=self.db_path,
        )
        self.assertEqual(res2["outcome"], "ok")
        self.assertTrue(res2["created_new_product"])
        new_pid_2 = res2["product_id"]
        self.assertNotIn(new_pid_2, (100, new_pid_1))

        # Verify Row 4 has new_pid_2 macros
        item4 = get_inventory_item(r4_id, db_path=self.db_path)
        self.assertEqual(item4["ID"], new_pid_2)
        self.assertEqual(item4["Kcal_100g"], 280.0)

        # Verify Row 1 and Row 3 still have Product 100
        item1 = get_inventory_item(r1_id, db_path=self.db_path)
        item3 = get_inventory_item(r3_id, db_path=self.db_path)
        self.assertEqual(item1["ID"], 100)
        self.assertEqual(item3["ID"], 100)

        # Verify Row 2 still has new_pid_1 macros
        item2_check = get_inventory_item(r2_id, db_path=self.db_path)
        self.assertEqual(item2_check["ID"], new_pid_1)
        self.assertEqual(item2_check["Kcal_100g"], 450.0)

    def test_option_2_macro_modification_backward_propagation(self):
        """
        Adversarially tests Option 2 ('Propagate modification backwards across all linked instances'):
        1. Modifies macros of Row 3 (cellar row) on Product 100.
        2. Verifies Product 100 is updated in place.
        3. Verifies ALL linked rows (Row 1, Row 3, Row 4) reflect the new macros backwards.
        4. Verifies ALL 4 original aliases still resolve to Product 100 and reflect new macros.
        5. Verifies existing recipes (Recipe 1 & 2) reflect the new macros backwards in calculations.
        """
        graph = self._setup_complex_graph()
        r1_id, r2_id, r3_id, r4_id = graph["rows"]

        # 1. First decouple Row 2 using Option 1 so we have an isolated item to test non-interference
        update_inventory_item(
            row_id=r2_id,
            macro_fields={"Kcal_100g": 480, "Protein_100g": 20, "Carbs_100g": 70, "Fat_100g": 4.0},
            propagate=False,
            db_path=self.db_path,
        )
        decoupled_item = get_inventory_item(r2_id, db_path=self.db_path)
        decoupled_pid = decoupled_item["ID"]
        self.assertNotEqual(decoupled_pid, 100)

        # 2. Now perform Option 2 on Row 3 (cellar row)
        res_prop = update_inventory_item(
            row_id=r3_id,
            macro_fields={"Kcal_100g": 320, "Protein_100g": 14, "Carbs_100g": 62, "Fat_100g": 2.2},
            propagate=True,
            db_path=self.db_path,
        )
        self.assertEqual(res_prop["outcome"], "ok")
        self.assertEqual(res_prop["product_id"], 100)
        self.assertFalse(res_prop["created_new_product"])

        # 3. Verify ALL rows linked to Product 100 (Row 1, Row 3, Row 4) reflect the new macros
        for r_id in (r1_id, r3_id, r4_id):
            it = get_inventory_item(r_id, db_path=self.db_path)
            self.assertEqual(it["ID"], 100)
            self.assertEqual(it["Kcal_100g"], 320.0, f"Row {r_id} did not reflect updated Kcal")
            self.assertEqual(it["Protein_100g"], 14.0, f"Row {r_id} did not reflect updated Protein")
            self.assertEqual(it["Carbs_100g"], 62.0)
            self.assertEqual(it["Fat_100g"], 2.2)

        # 4. Verify all 4 aliases still exist and point to Product 100
        conn = get_connection(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT Name FROM ALIASES WHERE ID = 100")
        aliases_found = [row[0] for row in cur.fetchall()]
        self.assertEqual(sorted(aliases_found), sorted(graph["aliases"]))

        # 5. Verify recipes reflect new macros backwards in calculations
        rec1_macros = recipe_macros(conn, 1)
        rec2_macros = recipe_macros(conn, 2)
        conn.close()

        # Recipe 1 has 100g -> 320 kcal, 14g protein
        self.assertAlmostEqual(rec1_macros["Kcal"], 320.0, places=2)
        self.assertAlmostEqual(rec1_macros["Protein"], 14.0, places=2)

        # Recipe 2 has 80g -> 80 * 3.2 = 256 kcal, 80 * 0.14 = 11.2g protein
        self.assertAlmostEqual(rec2_macros["Kcal"], 256.0, places=2)
        self.assertAlmostEqual(rec2_macros["Protein"], 11.2, places=2)

        # 6. Verify the decoupled Row 2 was NOT affected by Option 2 on Product 100
        item2_after = get_inventory_item(r2_id, db_path=self.db_path)
        self.assertEqual(item2_after["ID"], decoupled_pid)
        self.assertEqual(item2_after["Kcal_100g"], 480.0)
        self.assertEqual(item2_after["Protein_100g"], 20.0)

    def test_cancel_on_prompt_leaves_database_untouched(self):
        """
        Adversarially tests cancel behavior in InventoryItemEditDialog:
        1. Opens dialog on a linked item.
        2. Modifies characteristics and macros.
        3. Simulates user pressing Cancel when MacroPromptDialog appears.
        4. Verifies dialog save fails, returns False.
        5. Verifies database has zero modifications.
        """
        graph = self._setup_complex_graph()
        r1_id = graph["rows"][0]

        # Initial state capture
        init_item = get_inventory_item(r1_id, db_path=self.db_path)

        dlg = InventoryItemEditDialog(self.root, row_id=r1_id, db_path=self.db_path, wait=False)

        # Modify values in UI fields
        dlg.v_kcal.set("999")
        dlg.v_protein.set("88")
        dlg.v_location.set("altered_location")
        dlg.v_unit_weight.set("10.5")

        # Mock MacroPromptDialog to simulate user cancelling the choice
        with patch("meal_planner.gui.dialogs.MacroPromptDialog") as mock_prompt:
            instance = MagicMock()
            instance.result = None  # User pressed Cancel
            mock_prompt.return_value = instance

            save_success = dlg.save()

            # Dialog save should return False (aborted)
            self.assertFalse(save_success)

        # Explicitly cancel/close dialog
        dlg.cancel()

        # Verify DB is completely untouched
        item_after = get_inventory_item(r1_id, db_path=self.db_path)
        self.assertEqual(item_after["Kcal_100g"], init_item["Kcal_100g"])
        self.assertEqual(item_after["Protein_100g"], init_item["Protein_100g"])
        self.assertEqual(item_after["Location"], init_item["Location"])
        self.assertEqual(item_after["Unit_Weight"], init_item["Unit_Weight"])

    def test_cancel_button_direct_click(self):
        """Verifies that clicking cancel directly on InventoryItemEditDialog causes no side effects."""
        graph = self._setup_complex_graph()
        r1_id = graph["rows"][0]
        init_item = get_inventory_item(r1_id, db_path=self.db_path)

        dlg = InventoryItemEditDialog(self.root, row_id=r1_id, db_path=self.db_path, wait=False)
        dlg.v_name.set("Completely New Name")
        dlg.v_intact.set("999")
        dlg.cancel()

        self.assertIsNone(dlg.result)
        item_after = get_inventory_item(r1_id, db_path=self.db_path)
        self.assertEqual(item_after["Name"], init_item["Name"])
        self.assertEqual(item_after["Intact_Items"], init_item["Intact_Items"])

    def test_non_macro_edits_on_linked_product_do_not_decouple(self):
        """
        When editing ONLY inventory fields (e.g. Location, Intact_Items, Unit_Weight)
        on a linked product, decoupling must NOT occur and no new product should be created.
        """
        graph = self._setup_complex_graph()
        r1_id = graph["rows"][0]

        res = update_inventory_item(
            row_id=r1_id,
            item_fields={
                "Location": "nuova_dispensa",
                "Intact_Items": 9,
                "Unit_Weight": 1.5,
            },
            propagate=False,
            db_path=self.db_path,
        )

        self.assertEqual(res["outcome"], "ok")
        self.assertFalse(res["created_new_product"])
        self.assertFalse(res["macros_changed"])
        self.assertEqual(res["product_id"], 100)

        item = get_inventory_item(r1_id, db_path=self.db_path)
        self.assertEqual(item["ID"], 100)
        self.assertEqual(item["Location"], "nuova_dispensa")
        self.assertEqual(item["Intact_Items"], 9)
        self.assertEqual(item["Unit_Weight"], 1.5)

    def test_option_1_custom_name_and_collision_resolution(self):
        """
        Option 1 handling of custom user names:
        - When user specifies a unique new name, it is registered in ALIASES for the new product.
        - When user specifies an already-existing name from another product, it is disambiguated safely.
        """
        graph = self._setup_complex_graph()
        r1_id = graph["rows"][0]
        r2_id = graph["rows"][1]

        # Case A: Unique custom name
        res_a = update_inventory_item(
            row_id=r1_id,
            item_fields={"Name": "Spaghetti Gourmet 100% Grano Italiano"},
            macro_fields={"Kcal_100g": 360, "Protein_100g": 13, "Carbs_100g": 70, "Fat_100g": 2.0},
            propagate=False,
            db_path=self.db_path,
        )
        pid_a = res_a["product_id"]
        conn = get_connection(self.db_path)
        alias_a = conn.execute("SELECT ID FROM ALIASES WHERE Name = 'Spaghetti Gourmet 100% Grano Italiano'").fetchone()
        conn.close()
        self.assertIsNotNone(alias_a)
        self.assertEqual(alias_a[0], pid_a)

        # Case B: Conflicting existing name from graph['aliases'] (e.g. 'Spaghettoni' belongs to product 100)
        res_b = update_inventory_item(
            row_id=r2_id,
            item_fields={"Name": "Spaghettoni"},  # Belongs to 100
            macro_fields={"Kcal_100g": 390, "Protein_100g": 15, "Carbs_100g": 66, "Fat_100g": 2.5},
            propagate=False,
            db_path=self.db_path,
        )
        pid_b = res_b["product_id"]
        conn = get_connection(self.db_path)
        cur = conn.cursor()
        # Original 'Spaghettoni' still belongs to 100
        cur.execute("SELECT ID FROM ALIASES WHERE Name = 'Spaghettoni'")
        self.assertEqual(cur.fetchone()[0], 100)

        # Disambiguated alias created for pid_b
        cur.execute("SELECT Name FROM ALIASES WHERE ID = ?", (pid_b,))
        disambiguated = cur.fetchone()[0]
        conn.close()
        self.assertTrue("Spaghettoni (edited)" in disambiguated or f"Spaghettoni (#{pid_b})" in disambiguated)

    def test_gui_dialog_option_1_and_option_2_execution(self):
        """Verifies end-to-end GUI flow in InventoryItemEditDialog with MacroPromptDialog mocking."""
        graph = self._setup_complex_graph()
        r1_id, r2_id, r3_id, r4_id = graph["rows"]

        # Test Option 1 via dialog
        dlg1 = InventoryItemEditDialog(self.root, row_id=r1_id, db_path=self.db_path, wait=False)
        dlg1.v_kcal.set("410")
        dlg1.v_protein.set("15.5")

        with patch("meal_planner.gui.dialogs.MacroPromptDialog") as mock_prompt1:
            inst1 = MagicMock()
            inst1.result = "single"  # Option 1
            mock_prompt1.return_value = inst1
            saved1 = dlg1.save()

        self.assertTrue(saved1)
        self.assertIsNotNone(dlg1.result)
        self.assertTrue(dlg1.result["created_new_product"])
        opt1_pid = dlg1.result["product_id"]

        # Check DB
        it1 = get_inventory_item(r1_id, db_path=self.db_path)
        self.assertEqual(it1["ID"], opt1_pid)
        self.assertEqual(it1["Kcal_100g"], 410.0)

        # Test Option 2 via dialog on r3_id (cellar)
        dlg2 = InventoryItemEditDialog(self.root, row_id=r3_id, db_path=self.db_path, wait=False)
        dlg2.v_kcal.set("333")
        dlg2.v_protein.set("11.1")

        with patch("meal_planner.gui.dialogs.MacroPromptDialog") as mock_prompt2:
            inst2 = MagicMock()
            inst2.result = "propagate"  # Option 2
            mock_prompt2.return_value = inst2
            saved2 = dlg2.save()

        self.assertTrue(saved2)
        self.assertIsNotNone(dlg2.result)
        self.assertFalse(dlg2.result["created_new_product"])
        self.assertEqual(dlg2.result["product_id"], 100)

        # Check DB: all remaining linked rows reflect new macros
        it3 = get_inventory_item(r3_id, db_path=self.db_path)
        it4 = get_inventory_item(r4_id, db_path=self.db_path)
        self.assertEqual(it3["Kcal_100g"], 333.0)
        self.assertEqual(it4["Kcal_100g"], 333.0)


class TestAdversarialEdgeCases(BaseAdversarialTestCase):
    """Stress tests boundary conditions, invalid inputs, and alias conflicts."""

    def test_zero_quantity_rejection(self):
        """Attempting to set intact=0 and remainder=0 must be rejected."""
        conn = get_connection(self.db_path)
        conn.execute("INSERT INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g) VALUES (1, 'kg', 100, 10, 10, 1)")
        conn.execute("INSERT INTO INVENTORY (ID, Unit_Weight, Intact_Items, Open_Remainder, Location) VALUES (1, 1.0, 2, 0.0, 'pantry')")
        conn.commit()
        conn.close()

        row_id = get_inventory_rows(self.db_path)[0]["Row_ID"]

        with self.assertRaises(InvalidInput) as ctx:
            update_inventory_item(
                row_id=row_id,
                item_fields={"Intact_Items": 0, "Open_Remainder": 0.0},
                db_path=self.db_path,
            )
        self.assertIn("quantity cannot be 0", str(ctx.exception).lower())

        # Verify item quantity was NOT set to 0
        item = get_inventory_item(row_id, db_path=self.db_path)
        self.assertEqual(item["Intact_Items"], 2)

    def test_alias_conflict_rejection(self):
        """Renaming a product to an alias that already belongs to another product ID must raise AliasConflict."""
        conn = get_connection(self.db_path)
        conn.execute("INSERT INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g) VALUES (1, 'kg', 100, 10, 10, 1)")
        conn.execute("INSERT INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g) VALUES (2, 'kg', 200, 20, 20, 2)")
        conn.execute("INSERT INTO ALIASES (Name, ID) VALUES ('Product A', 1)")
        conn.execute("INSERT INTO ALIASES (Name, ID) VALUES ('Product B', 2)")
        conn.execute("INSERT INTO INVENTORY (ID, Unit_Weight, Intact_Items, Open_Remainder, Location) VALUES (1, 1.0, 1, 0, 'pantry')")
        conn.commit()
        conn.close()

        row_id = get_inventory_rows(self.db_path)[0]["Row_ID"]

        # Attempt to rename Product A's row to 'Product B' without macro decoupling
        with self.assertRaises(AliasConflict):
            update_inventory_item(
                row_id=row_id,
                item_fields={"Name": "Product B"},
                propagate=True,
                db_path=self.db_path,
            )

    def test_unit_change_triggers_macro_propagation_prompt(self):
        """Changing Unit from 'kg' to 'pcs' changes macro semantics and must trigger macro linkage handling."""
        conn = get_connection(self.db_path)
        conn.execute("INSERT INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g) VALUES (1, 'kg', 100, 10, 10, 1)")
        conn.execute("INSERT INTO ALIASES (Name, ID) VALUES ('Item A1', 1)")
        conn.execute("INSERT INTO ALIASES (Name, ID) VALUES ('Item A2', 1)")  # Linked!
        conn.execute("INSERT INTO INVENTORY (ID, Unit_Weight, Intact_Items, Open_Remainder, Location) VALUES (1, 1.0, 1, 0, 'pantry')")
        conn.commit()
        conn.close()

        row_id = get_inventory_rows(self.db_path)[0]["Row_ID"]

        # Option 1: changing unit to 'pcs' with propagate=False creates new product
        res = update_inventory_item(
            row_id=row_id,
            item_fields={"Unit": "pcs"},
            propagate=False,
            db_path=self.db_path,
        )
        self.assertEqual(res["outcome"], "ok")
        self.assertTrue(res["created_new_product"])
        self.assertNotEqual(res["product_id"], 1)

        new_item = get_inventory_item(row_id, db_path=self.db_path)
        self.assertEqual(new_item["Unit"], "pcs")

    def test_delete_inventory_item_adversarial(self):
        """Adversarial delete testing: non-existent ID, negative ID, string ID, and cascade safety."""
        conn = get_connection(self.db_path)
        conn.execute("INSERT INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g) VALUES (1, 'kg', 100, 10, 10, 1)")
        conn.execute("INSERT INTO ALIASES (Name, ID) VALUES ('Product 1', 1)")
        conn.execute("INSERT INTO INVENTORY (ID, Unit_Weight, Intact_Items, Open_Remainder, Location) VALUES (1, 1.0, 1, 0, 'pantry')")
        conn.commit()
        conn.close()

        # Non-existent row ID returns False
        self.assertFalse(delete_inventory_item(row_id=99999, db_path=self.db_path))

        # Invalid inputs raise InvalidInput
        with self.assertRaises(InvalidInput):
            delete_inventory_item(row_id=0, db_path=self.db_path)
        with self.assertRaises(InvalidInput):
            delete_inventory_item(row_id=-10, db_path=self.db_path)
        with self.assertRaises(InvalidInput):
            delete_inventory_item(row_id="not_an_int", db_path=self.db_path)

        # Deleting the inventory row must NOT delete the product or alias
        row_id = get_inventory_rows(self.db_path)[0]["Row_ID"]
        self.assertTrue(delete_inventory_item(row_id=row_id, db_path=self.db_path))
        self.assertEqual(get_inventory_rows(self.db_path), [])

        conn = get_connection(self.db_path)
        prod = conn.execute("SELECT ID FROM PRODUCTS WHERE ID = 1").fetchone()
        alias = conn.execute("SELECT Name FROM ALIASES WHERE ID = 1").fetchone()
        conn.close()
        self.assertIsNotNone(prod)
        self.assertIsNotNone(alias)


class TestProductionDatabasePreservation(unittest.TestCase):
    """
    Directly checks the disk database 'inventory.db' to verify that
    production baseline row counts remain intact:
    INVENTORY: 36, RECIPES: 3, PRODUCTS: 14, ALIASES: 30.
    """

    def test_production_db_row_counts(self):
        db_path = os.path.abspath("data/inventory.db")
        self.assertTrue(os.path.exists(db_path), "data/inventory.db does not exist!")

        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        counts = {
            "INVENTORY": cur.execute("SELECT COUNT(*) FROM INVENTORY").fetchone()[0],
            "RECIPES": cur.execute("SELECT COUNT(*) FROM RECIPES").fetchone()[0],
            "PRODUCTS": cur.execute("SELECT COUNT(*) FROM PRODUCTS").fetchone()[0],
            "ALIASES": cur.execute("SELECT COUNT(*) FROM ALIASES").fetchone()[0],
        }
        conn.close()

        self.assertEqual(counts["INVENTORY"], 36, f"Expected 36 INVENTORY rows, got {counts['INVENTORY']}")
        self.assertEqual(counts["RECIPES"], 3, f"Expected 3 RECIPES rows, got {counts['RECIPES']}")
        self.assertEqual(counts["PRODUCTS"], 14, f"Expected 14 PRODUCTS rows, got {counts['PRODUCTS']}")
        self.assertEqual(counts["ALIASES"], 30, f"Expected 30 ALIASES rows, got {counts['ALIASES']}")




# ======================================================================
# SECTION: tests/test_challenger_empirical_m1_r2.py
# ======================================================================

"""
Empirical Gate Verification Suite: Milestone M1 Iteration 2 Challenger 2.
Deeply tests:
1. Delete button interactions:
   - Single item deletion via App and table column #0
   - Cancel delete confirmation preserves item completely
   - Delete all items of a multi-row product, verifying PRODUCT and ALIASES remain intact
   - Referential integrity (PRAGMA foreign_key_check)
2. Macro propagation Option 1 vs Option 2 across complex alias mappings:
   - 4 aliases, 4 inventory rows, 2 recipes
   - Option 1 decoupling: new product ID, alias disambiguation, recipes & other rows untouched
   - Option 2 propagation: in-place update, backward calculation update in recipes, decoupled rows untouched
   - Cancel macro prompt preserves state completely
3. Non-finite float rejection verification
4. Production DB isolation invariant: baseline counts (36, 3, 14, 30)
"""

import math
import os
import sqlite3
import tempfile
import tkinter as tk
from unittest.mock import MagicMock, patch
import unittest

from meal_planner.db.connection import get_connection
from meal_planner.db.schema import create_schema
from meal_planner.db.schema import migrate
from meal_planner.engine import recipe_macros
from meal_planner.gui.app import App
from meal_planner.gui.dialogs import InventoryItemEditDialog, MacroPromptDialog
from meal_planner.gui.widgets import Table
from meal_planner.inventory import (
    AliasConflict,
    InvalidInput,
    add_item,
    delete_inventory_item,
    get_inventory_item,
    get_inventory_rows,
    get_next_product_id,
    is_product_macro_linked,
    update_inventory_item,
)


class BaseEmpiricalGateTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tk.Tk()
        cls.root.withdraw()

        # Capture production DB counts
        cls.prod_db = os.path.abspath("data/inventory.db")
        if os.path.exists(cls.prod_db):
            conn = sqlite3.connect(cls.prod_db)
            c = conn.cursor()
            cls.baseline_counts = (
                c.execute("SELECT COUNT(*) FROM INVENTORY").fetchone()[0],
                c.execute("SELECT COUNT(*) FROM RECIPES").fetchone()[0],
                c.execute("SELECT COUNT(*) FROM PRODUCTS").fetchone()[0],
                c.execute("SELECT COUNT(*) FROM ALIASES").fetchone()[0],
            )
            conn.close()
        else:
            cls.baseline_counts = None

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:
            pass

    def setUp(self):
        # Strict isolation: temporary SQLite database
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp.name
        self.tmp.close()

        self._orig_env = os.environ.get("MEAL_PLANNER_DB")
        os.environ["MEAL_PLANNER_DB"] = self.db_path

        conn = get_connection(self.db_path)
        create_schema(conn)
        migrate(conn)
        conn.close()

    def tearDown(self):
        if self._orig_env is not None:
            os.environ["MEAL_PLANNER_DB"] = self._orig_env
        else:
            os.environ.pop("MEAL_PLANNER_DB", None)

        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except OSError:
                pass

        # Invariant: disk inventory.db must NEVER change
        if self.baseline_counts:
            conn = sqlite3.connect(self.prod_db)
            c = conn.cursor()
            curr = (
                c.execute("SELECT COUNT(*) FROM INVENTORY").fetchone()[0],
                c.execute("SELECT COUNT(*) FROM RECIPES").fetchone()[0],
                c.execute("SELECT COUNT(*) FROM PRODUCTS").fetchone()[0],
                c.execute("SELECT COUNT(*) FROM ALIASES").fetchone()[0],
            )
            conn.close()
            self.assertEqual(curr, self.baseline_counts, f"Production DB altered! Baseline={self.baseline_counts}, Current={curr}")


class TestDeleteButtonLifecycleAndIntegrity(BaseEmpiricalGateTestCase):
    """Deep empirical challenge of delete button interaction and catalog persistence."""

    def test_delete_single_item_and_cancel_confirmation(self):
        """Verify delete button interaction: cancel preserves item, confirm deletes item."""
        add_item("Olio di Oliva", 1.0, 3, "dispensa", "2027-12-31",
                 fallback_id=10, new_product_unit="L",
                 new_product_macros={"Kcal_100g": 884, "Protein_100g": 0, "Carbs_100g": 0, "Fat_100g": 100},
                 db_path=self.db_path)
        row_id = get_inventory_rows(self.db_path)[0]["Row_ID"]

        app = App()
        app.db_path = self.db_path
        app.refresh()

        try:
            # 1. Pantry table displays item
            self.assertIn(str(row_id), app.t_disp.tv.get_children())

            # 2. Cancel delete confirmation
            with patch("tkinter.messagebox.askyesno", return_value=False) as mock_ask:
                app._on_delete_inventory_row(str(row_id))
                mock_ask.assert_called_once()

            # Item still in DB
            item_check = get_inventory_item(row_id, db_path=self.db_path)
            self.assertIsNotNone(item_check)
            self.assertEqual(item_check["Name"], "Olio di Oliva")
            self.assertEqual(item_check["Intact_Items"], 3)
            self.assertIn(str(row_id), app.t_disp.tv.get_children())
            self.assertNotIn("Deleted", app.status.get())

            # 3. Confirm delete
            with patch("tkinter.messagebox.askyesno", return_value=True) as mock_ask_yes:
                app._on_delete_inventory_row(str(row_id))
                mock_ask_yes.assert_called_once()

            # Item removed from DB and UI
            self.assertIsNone(get_inventory_item(row_id, db_path=self.db_path))
            self.assertNotIn(str(row_id), app.t_disp.tv.get_children())
            self.assertIn("Deleted 'Olio di Oliva'", app.status.get())

            # Product and Alias remain in catalog
            conn = get_connection(self.db_path)
            prod = conn.execute("SELECT ID FROM PRODUCTS WHERE ID = 10").fetchone()
            alias = conn.execute("SELECT Name FROM ALIASES WHERE ID = 10").fetchone()
            conn.close()
            self.assertIsNotNone(prod)
            self.assertIsNotNone(alias)

        finally:
            try:
                app.destroy()
            except Exception:
                pass

    def test_delete_all_items_of_product_preserves_product_and_aliases(self):
        """
        Adversarial test: Delete all inventory items of a product one by one.
        Verify that even when inventory count drops to 0, PRODUCTS and ALIASES remain intact,
        and foreign key constraints are not violated.
        """
        conn = get_connection(self.db_path)
        cur = conn.cursor()
        pid = 20
        cur.execute(
            "INSERT INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g) "
            "VALUES (?, 'kg', 350.0, 12.0, 70.0, 2.0)", (pid,)
        )
        cur.execute("INSERT INTO ALIASES (Name, ID) VALUES ('Penne Barilla', ?)", (pid,))
        cur.execute("INSERT INTO ALIASES (Name, ID) VALUES ('Penne Rigate', ?)", (pid,))
        cur.execute("INSERT INTO ALIASES (Name, ID) VALUES ('Pasta N.73', ?)", (pid,))

        # 3 inventory rows for this product
        cur.execute("INSERT INTO INVENTORY (Row_ID, ID, Unit_Weight, Intact_Items, Open_Remainder, Location) VALUES (101, ?, 0.5, 2, 0.0, 'pantry')", (pid,))
        cur.execute("INSERT INTO INVENTORY (Row_ID, ID, Unit_Weight, Intact_Items, Open_Remainder, Location) VALUES (102, ?, 1.0, 1, 0.3, 'cantina')", (pid,))
        cur.execute("INSERT INTO INVENTORY (Row_ID, ID, Unit_Weight, Intact_Items, Open_Remainder, Location) VALUES (103, ?, 0.5, 5, 0.0, 'freezer')", (pid,))

        # Linked recipe
        cur.execute("INSERT INTO RECIPES (Recipe_ID, Name) VALUES (5, 'Pasta al sugo')")
        cur.execute("INSERT INTO RECIPE_INGREDIENTS (Recipe_ID, ID, Quantity, Unit) VALUES (5, ?, 100, 'g')", (pid,))
        conn.commit()
        conn.close()

        # Step 1: Delete row 101
        self.assertTrue(delete_inventory_item(row_id=101, db_path=self.db_path))
        rows = get_inventory_rows(self.db_path)
        self.assertEqual(len(rows), 2)
        row_ids = [r["Row_ID"] for r in rows]
        self.assertEqual(row_ids, [102, 103])

        # Step 2: Delete row 102
        self.assertTrue(delete_inventory_item(row_id=102, db_path=self.db_path))
        rows = get_inventory_rows(self.db_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Row_ID"], 103)

        # Step 3: Delete row 103 (LAST row of this product)
        self.assertTrue(delete_inventory_item(row_id=103, db_path=self.db_path))
        self.assertEqual(get_inventory_rows(self.db_path), [])

        # Invariant checks:
        conn = get_connection(self.db_path)
        c = conn.cursor()

        # 1. Product record STILL exists with exact macros
        prod_row = c.execute("SELECT Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g FROM PRODUCTS WHERE ID = ?", (pid,)).fetchone()
        self.assertIsNotNone(prod_row, "Product was unexpectedly deleted when last inventory item was deleted!")
        self.assertEqual(prod_row, ("kg", 350.0, 12.0, 70.0, 2.0))

        # 2. All 3 aliases STILL exist and point to pid
        aliases = [r[0] for r in c.execute("SELECT Name FROM ALIASES WHERE ID = ? ORDER BY Name ASC", (pid,)).fetchall()]
        self.assertEqual(aliases, ["Pasta N.73", "Penne Barilla", "Penne Rigate"])

        # 3. Recipe and Recipe Ingredients STILL exist and reference pid
        rec_ing = c.execute("SELECT ID, Quantity FROM RECIPE_INGREDIENTS WHERE Recipe_ID = 5").fetchone()
        self.assertIsNotNone(rec_ing)
        self.assertEqual(rec_ing[0], pid)

        # 4. Foreign key integrity verified
        self.assertEqual(list(conn.execute("PRAGMA foreign_key_check")), [])

        # 5. Adding new inventory row for this product succeeds immediately using existing alias
        conn.close()
        res_add = add_item("Penne Rigate", 0.5, 2, "pantry", db_path=self.db_path)
        self.assertEqual(res_add["outcome"], "ok")
        self.assertEqual(res_add["case"], "A")
        self.assertEqual(res_add["ID"], pid)

        rows_after = get_inventory_rows(self.db_path)
        self.assertEqual(len(rows_after), 1)
        self.assertEqual(rows_after[0]["ID"], pid)
        self.assertEqual(rows_after[0]["Kcal_100g"], 350.0)


class TestMacroPropagationOption1VsOption2Empirical(BaseEmpiricalGateTestCase):
    """Stress tests Option 1 (isolation) vs Option 2 (propagation) on complex alias graphs."""

    def _build_complex_graph(self):
        conn = get_connection(self.db_path)
        cur = conn.cursor()
        pid = 300
        cur.execute(
            "INSERT INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g, Sugars_100g, Fiber_100g) "
            "VALUES (?, 'kg', 340.0, 11.0, 68.0, 2.0, 2.5, 3.0)", (pid,)
        )
        aliases = ["Farro Perlato", "Farro Monococco", "Farro Dicocco", "Spelt Grain"]
        for a in aliases:
            cur.execute("INSERT INTO ALIASES (Name, ID) VALUES (?, ?)", (a, pid))

        # 4 inventory rows
        cur.execute("INSERT INTO INVENTORY (ID, Unit_Weight, Intact_Items, Open_Remainder, Location, Expiration_Date) VALUES (?, 0.5, 2, 0.0, 'pantry', '2027-01-01')", (pid,))
        r1 = cur.lastrowid
        cur.execute("INSERT INTO INVENTORY (ID, Unit_Weight, Intact_Items, Open_Remainder, Location, Expiration_Date) VALUES (?, 1.0, 1, 0.2, 'cantina', '2027-06-01')", (pid,))
        r2 = cur.lastrowid
        cur.execute("INSERT INTO INVENTORY (ID, Unit_Weight, Intact_Items, Open_Remainder, Location, Expiration_Date) VALUES (?, 0.5, 4, 0.0, 'freezer', '2027-12-31')", (pid,))
        r3 = cur.lastrowid
        cur.execute("INSERT INTO INVENTORY (ID, Unit_Weight, Intact_Items, Open_Remainder, Location, Expiration_Date) VALUES (?, 0.25, 1, 0.1, 'dispensa', '2026-11-30')", (pid,))
        r4 = cur.lastrowid

        # 2 recipes
        cur.execute("INSERT INTO RECIPES (Recipe_ID, Name) VALUES (101, 'Zuppa di Farro')")
        cur.execute("INSERT INTO RECIPE_INGREDIENTS (Recipe_ID, ID, Quantity, Unit) VALUES (101, ?, 150, 'g')", (pid,))

        cur.execute("INSERT INTO RECIPES (Recipe_ID, Name) VALUES (102, 'Insalata Estiva')")
        cur.execute("INSERT INTO RECIPE_INGREDIENTS (Recipe_ID, ID, Quantity, Unit) VALUES (102, ?, 80, 'g')", (pid,))

        conn.commit()
        conn.close()

        return {
            "pid": pid,
            "aliases": aliases,
            "rows": [r1, r2, r3, r4],
            "recipes": [101, 102],
        }

    def test_option_1_single_item_decoupling_lifecycle(self):
        """Option 1 must isolate the edited row to a newly created product ID and alias."""
        graph = self._build_complex_graph()
        r1, r2, r3, r4 = graph["rows"]
        pid = graph["pid"]

        # Confirm linkage initially
        self.assertTrue(is_product_macro_linked(pid, db_path=self.db_path))

        # Perform Option 1 on r2 (cantina row)
        res = update_inventory_item(
            row_id=r2,
            macro_fields={"Kcal_100g": 380, "Protein_100g": 15, "Carbs_100g": 68, "Fat_100g": 2.5},
            propagate=False,
            db_path=self.db_path,
        )
        self.assertEqual(res["outcome"], "ok")
        self.assertTrue(res["created_new_product"])
        new_pid = res["product_id"]
        self.assertNotEqual(new_pid, pid)

        # 1. Row 2 updated to new_pid
        item2 = get_inventory_item(r2, db_path=self.db_path)
        self.assertEqual(item2["ID"], new_pid)
        self.assertEqual(item2["Kcal_100g"], 380.0)
        self.assertEqual(item2["Protein_100g"], 15.0)

        # 2. Rows 1, 3, 4 still point to pid with original macros
        for r in (r1, r3, r4):
            it = get_inventory_item(r, db_path=self.db_path)
            self.assertEqual(it["ID"], pid)
            self.assertEqual(it["Kcal_100g"], 340.0)
            self.assertEqual(it["Protein_100g"], 11.0)

        # 3. All 4 original aliases still belong to pid
        conn = get_connection(self.db_path)
        c = conn.cursor()
        cur_aliases = [row[0] for row in c.execute("SELECT Name FROM ALIASES WHERE ID = ? ORDER BY Name ASC", (pid,)).fetchall()]
        self.assertEqual(sorted(cur_aliases), sorted(graph["aliases"]))

        # 4. New product has a disambiguated alias
        new_aliases = [row[0] for row in c.execute("SELECT Name FROM ALIASES WHERE ID = ?", (new_pid,)).fetchall()]
        self.assertGreaterEqual(len(new_aliases), 1)

        # 5. Recipes still use pid and nutrition calculations remain identical
        rec1 = recipe_macros(conn, 101)
        rec2 = recipe_macros(conn, 102)
        conn.close()

        # 150g * 3.4 = 510 kcal, 150g * 0.11 = 16.5g protein
        self.assertAlmostEqual(rec1["Kcal"], 510.0, places=2)
        self.assertAlmostEqual(rec1["Protein"], 16.5, places=2)
        # 80g * 3.4 = 272 kcal, 80g * 0.11 = 8.8g protein
        self.assertAlmostEqual(rec2["Kcal"], 272.0, places=2)
        self.assertAlmostEqual(rec2["Protein"], 8.8, places=2)

    def test_option_2_backward_propagation_lifecycle(self):
        """Option 2 must update the product in place, propagating backwards to all rows and recipes."""
        graph = self._build_complex_graph()
        r1, r2, r3, r4 = graph["rows"]
        pid = graph["pid"]

        # Decouple row 1 first using Option 1 to verify non-interference
        res_dec = update_inventory_item(
            row_id=r1,
            macro_fields={"Kcal_100g": 400, "Protein_100g": 18, "Carbs_100g": 60, "Fat_100g": 3.0},
            propagate=False,
            db_path=self.db_path,
        )
        decoupled_pid = res_dec["product_id"]

        # Now perform Option 2 on row 3 (freezer row)
        res_prop = update_inventory_item(
            row_id=r3,
            macro_fields={"Kcal_100g": 360, "Protein_100g": 14, "Carbs_100g": 65, "Fat_100g": 3.0},
            propagate=True,
            db_path=self.db_path,
        )
        self.assertEqual(res_prop["outcome"], "ok")
        self.assertEqual(res_prop["product_id"], pid)
        self.assertFalse(res_prop["created_new_product"])

        # 1. Product table updated in place
        conn = get_connection(self.db_path)
        c = conn.cursor()
        p_row = c.execute("SELECT Kcal_100g, Protein_100g, Carbs_100g, Fat_100g FROM PRODUCTS WHERE ID = ?", (pid,)).fetchone()
        self.assertEqual(p_row, (360.0, 14.0, 65.0, 3.0))

        # 2. All remaining linked rows (r2, r3, r4) reflect updated macros
        for r in (r2, r3, r4):
            it = get_inventory_item(r, db_path=self.db_path)
            self.assertEqual(it["ID"], pid)
            self.assertEqual(it["Kcal_100g"], 360.0)
            self.assertEqual(it["Protein_100g"], 14.0)

        # 3. Decoupled row (r1) remains completely untouched
        it1 = get_inventory_item(r1, db_path=self.db_path)
        self.assertEqual(it1["ID"], decoupled_pid)
        self.assertEqual(it1["Kcal_100g"], 400.0)
        self.assertEqual(it1["Protein_100g"], 18.0)

        # 4. Recipes reflect updated macros backwards in recipe_macros()
        rec1 = recipe_macros(conn, 101)
        rec2 = recipe_macros(conn, 102)
        conn.close()

        # Recipe 101: 150g * 3.6 = 540 kcal, 150g * 0.14 = 21g protein
        self.assertAlmostEqual(rec1["Kcal"], 540.0, places=2)
        self.assertAlmostEqual(rec1["Protein"], 21.0, places=2)

        # Recipe 102: 80g * 3.6 = 288 kcal, 80g * 0.14 = 11.2g protein
        self.assertAlmostEqual(rec2["Kcal"], 288.0, places=2)
        self.assertAlmostEqual(rec2["Protein"], 11.2, places=2)

    def test_gui_dialog_macro_prompt_integration(self):
        """End-to-end GUI dialog test for Option 1, Option 2, and Cancel prompt."""
        graph = self._build_complex_graph()
        r1, r2, r3, r4 = graph["rows"]
        pid = graph["pid"]

        # Case A: User cancels prompt -> dialog save returns False, DB untouched
        dlg_cancel = InventoryItemEditDialog(self.root, row_id=r1, db_path=self.db_path, wait=False)
        dlg_cancel.v_kcal.set("999")
        with patch("meal_planner.gui.dialogs.MacroPromptDialog") as mock_prompt:
            inst = MagicMock()
            inst.result = None  # User pressed Cancel
            mock_prompt.return_value = inst
            self.assertFalse(dlg_cancel.save())
        dlg_cancel.cancel()

        it1_check = get_inventory_item(r1, db_path=self.db_path)
        self.assertEqual(it1_check["Kcal_100g"], 340.0)

        # Case B: User selects Option 1 ("single")
        dlg_opt1 = InventoryItemEditDialog(self.root, row_id=r1, db_path=self.db_path, wait=False)
        dlg_opt1.v_kcal.set("420")
        with patch("meal_planner.gui.dialogs.MacroPromptDialog") as mock_prompt:
            inst = MagicMock()
            inst.result = "single"
            mock_prompt.return_value = inst
            self.assertTrue(dlg_opt1.save())
        dlg_opt1.cancel()

        it1_after = get_inventory_item(r1, db_path=self.db_path)
        self.assertNotEqual(it1_after["ID"], pid)
        self.assertEqual(it1_after["Kcal_100g"], 420.0)

        # Case C: User selects Option 2 ("propagate") on row 2
        dlg_opt2 = InventoryItemEditDialog(self.root, row_id=r2, db_path=self.db_path, wait=False)
        dlg_opt2.v_kcal.set("310")
        with patch("meal_planner.gui.dialogs.MacroPromptDialog") as mock_prompt:
            inst = MagicMock()
            inst.result = "propagate"
            mock_prompt.return_value = inst
            self.assertTrue(dlg_opt2.save())
        dlg_opt2.cancel()

        it2_after = get_inventory_item(r2, db_path=self.db_path)
        self.assertEqual(it2_after["ID"], pid)
        self.assertEqual(it2_after["Kcal_100g"], 310.0)

        # r3 and r4 also updated
        it3_after = get_inventory_item(r3, db_path=self.db_path)
        self.assertEqual(it3_after["Kcal_100g"], 310.0)


class TestNonFiniteFloatRejectionIntegrity(BaseEmpiricalGateTestCase):
    """Verifies that non-finite floats (inf, -inf, nan) are strictly rejected everywhere."""

    def test_non_finite_rejected_in_dialog_and_backend(self):
        add_item("Farina Manitoba", 1.0, 2, "dispensa", "2027-01-01",
                 fallback_id=50, new_product_unit="kg",
                 new_product_macros={"Kcal_100g": 350, "Protein_100g": 13, "Carbs_100g": 70, "Fat_100g": 1.5},
                 db_path=self.db_path)
        row_id = get_inventory_rows(self.db_path)[0]["Row_ID"]

        # Backend update_inventory_item rejection
        bad_floats = [float("inf"), float("-inf"), float("nan"), "inf", "-inf", "+inf", "NaN", "nan"]
        for bad in bad_floats:
            with self.assertRaises(InvalidInput):
                update_inventory_item(row_id=row_id, item_fields={"Unit_Weight": bad}, db_path=self.db_path)
            with self.assertRaises(InvalidInput):
                update_inventory_item(row_id=row_id, item_fields={"Open_Remainder": bad}, db_path=self.db_path)
            with self.assertRaises(InvalidInput):
                update_inventory_item(row_id=row_id, macro_fields={"Kcal_100g": bad}, db_path=self.db_path)

        # GUI Dialog rejection
        dlg = InventoryItemEditDialog(self.root, row_id=row_id, db_path=self.db_path, wait=False)
        try:
            dlg.v_unit_weight.set("inf")
            with patch("tkinter.messagebox.showerror"):
                self.assertFalse(dlg.save())

            dlg.v_unit_weight.set("1.0")
            dlg.v_remainder.set("nan")
            with patch("tkinter.messagebox.showerror"):
                self.assertFalse(dlg.save())

            dlg.v_remainder.set("0.0")
            dlg.v_kcal.set("inf")
            with patch("tkinter.messagebox.showerror"):
                self.assertFalse(dlg.save())
        finally:
            dlg.destroy()


class TestInventoryAdvancedEdgeCases(unittest.TestCase):
    """Advanced edge cases: concurrent rows FIFO consumption, macro decoupling, and remainder deletion."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp.name
        self.tmp.close()
        self._orig_env = os.environ.get("MEAL_PLANNER_DB")
        os.environ["MEAL_PLANNER_DB"] = self.db_path

        conn = get_connection(self.db_path)
        create_schema(conn)
        migrate(conn)
        conn.close()

    def tearDown(self):
        if self._orig_env is not None:
            os.environ["MEAL_PLANNER_DB"] = self._orig_env
        else:
            os.environ.pop("MEAL_PLANNER_DB", None)

        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except OSError:
                pass

    def test_concurrent_inventory_rows_deletion_and_fifo_consumption(self):
        """Deleting one of multiple inventory rows preserves the remaining rows and allows FIFO consumption."""
        add_item(
            "Riso Basmati", 1.0, 2, "pantry", "2027-01-01",
            fallback_id=1, new_product_unit="kg",
            new_product_macros={"Kcal_100g": 350, "Protein_100g": 7, "Carbs_100g": 78, "Fat_100g": 1},
            db_path=self.db_path,
        )
        add_item("Riso Basmati", 1.0, 3, "fridge", "2026-12-01", db_path=self.db_path)

        rows = get_inventory_rows(self.db_path)
        self.assertEqual(len(rows), 2)
        row1_id = rows[0]["Row_ID"]
        row2_id = rows[1]["Row_ID"]

        self.assertTrue(delete_inventory_item(row_id=row1_id, db_path=self.db_path))

        rows_after_del = get_inventory_rows(self.db_path)
        self.assertEqual(len(rows_after_del), 1)
        self.assertEqual(rows_after_del[0]["Row_ID"], row2_id)
        self.assertEqual(rows_after_del[0]["Intact_Items"], 3)

        outcome = consume_quantity("Riso Basmati", 1.0, db_path=self.db_path)
        self.assertEqual(outcome["outcome"], "ok")
        self.assertEqual(outcome["consumed"], 1.0)

        rows_after_consume = get_inventory_rows(self.db_path)
        self.assertEqual(len(rows_after_consume), 1)
        self.assertEqual(rows_after_consume[0]["Intact_Items"], 2)

    def test_empty_inventory_operations_resilience(self):
        """Operations on an empty inventory return safe falsy/empty values rather than raising errors."""
        self.assertEqual(get_inventory_rows(self.db_path), [])
        self.assertFalse(delete_inventory_item(row_id=999, db_path=self.db_path))
        self.assertFalse(is_product_macro_linked(product_id=999, db_path=self.db_path))

    def test_macro_propagation_decoupling_preserves_recipe_integrity(self):
        """Option 1 (propagate=False) decouples product ID and preserves recipe macros; Option 2 propagates."""
        from meal_planner.engine import recipe_macros

        conn = get_connection(self.db_path)
        conn.execute(
            """INSERT INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g,
                                     Sugars_100g, Saturated_fat_100g, Fiber_100g, Salt_100g)
               VALUES (1, 'kg', 380, 13, 68, 7, 1, 1, 10, 0.05)"""
        )
        conn.execute("INSERT INTO ALIASES (Name, ID) VALUES ('Avena', 1)")
        conn.execute("INSERT INTO RECIPES (Recipe_ID, Name, Notes) VALUES (1, 'Porridge', '')")
        conn.execute("INSERT INTO RECIPE_INGREDIENTS (Recipe_ID, ID, Quantity, Unit) VALUES (1, 1, 100, 'g')")
        conn.commit()

        m_before = recipe_macros(conn, 1)
        self.assertEqual(m_before["Kcal"], 380.0)
        self.assertEqual(m_before["Protein"], 13.0)
        conn.close()

        # Add inventory row I1
        add_item("Avena", 1.0, 1, "pantry", "2027-01-01", db_path=self.db_path)
        row1_id = get_inventory_rows(self.db_path)[0]["Row_ID"]

        # Option 1: propagate=False -> new product created, recipe untouched
        update_inventory_item(
            row_id=row1_id,
            item_fields={"Name": "Avena Arricchita"},
            macro_fields={"Kcal_100g": 450, "Protein_100g": 18, "Carbs_100g": 60, "Fat_100g": 8},
            propagate=False,
            db_path=self.db_path,
        )

        conn_check = get_connection(self.db_path)
        m_after_opt1 = recipe_macros(conn_check, 1)
        self.assertEqual(m_after_opt1["Kcal"], 380.0)
        self.assertEqual(m_after_opt1["Protein"], 13.0)

        p1 = conn_check.execute("SELECT Kcal_100g, Protein_100g FROM PRODUCTS WHERE ID = 1").fetchone()
        self.assertEqual(p1, (380.0, 13.0))

        # Option 2: propagate=True -> updates product 1 in place, recipe updates
        add_item("Avena", 1.0, 2, "pantry", "2027-01-01", db_path=self.db_path)
        row_p1 = [r for r in get_inventory_rows(self.db_path) if r["ID"] == 1][0]["Row_ID"]

        update_inventory_item(
            row_id=row_p1,
            item_fields={},
            macro_fields={"Kcal_100g": 500, "Protein_100g": 20, "Carbs_100g": 60, "Fat_100g": 8},
            propagate=True,
            db_path=self.db_path,
        )

        m_after_opt2 = recipe_macros(conn_check, 1)
        self.assertEqual(m_after_opt2["Kcal"], 500.0)
        self.assertEqual(m_after_opt2["Protein"], 20.0)
        p1_updated = conn_check.execute("SELECT Kcal_100g, Protein_100g FROM PRODUCTS WHERE ID = 1").fetchone()
        self.assertEqual(p1_updated, (500.0, 20.0))
        conn_check.close()

    def test_delete_inventory_item_with_open_remainder_and_invalid_ids(self):
        """delete_inventory_item safely deletes open-remainder rows and rejects invalid row IDs."""
        conn = get_connection(self.db_path)
        conn.execute(
            """INSERT INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g)
               VALUES (1, 'kg', 380, 13, 68, 7)"""
        )
        conn.execute("INSERT INTO ALIASES (Name, ID) VALUES ('Avena', 1)")
        conn.execute(
            """INSERT INTO INVENTORY (Row_ID, ID, Unit_Weight, Intact_Items, Open_Remainder, Location, Expiration_Date)
               VALUES (42, 1, 1.0, 1, 0.4, 'pantry', '2026-12-31')"""
        )
        conn.commit()
        conn.close()

        self.assertEqual(len(get_inventory_rows(self.db_path)), 1)
        self.assertTrue(delete_inventory_item(row_id=42, db_path=self.db_path))
        self.assertEqual(len(get_inventory_rows(self.db_path)), 0)

        for bad in ["invalid", -1, 0, None]:
            with self.assertRaises(InvalidInput):
                delete_inventory_item(row_id=bad, db_path=self.db_path)

        self.assertFalse(delete_inventory_item(row_id=999, db_path=self.db_path))


if __name__ == "__main__":
    unittest.main()
