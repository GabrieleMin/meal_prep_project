"""
tests/test_recipe_management.py

Consolidated recipe management, on-the-fly products, servings scaling, and CSV/JSON import test suite.
"""


import contextlib
import io
import json
import math
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from meal_planner import recipes as rc
from meal_planner.db.connection import get_connection
from meal_planner.db.schema import migrate
from scripts.seed_inventory import seed as seed_products


class TestRecipesCore(unittest.TestCase):
    @contextlib.contextmanager
    def _archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "p.db")
            with contextlib.redirect_stdout(io.StringIO()):
                seed_products(path, False)
            conn = get_connection(path)
            migrate(conn)
            yield conn, tmp
            conn.close()

    def _count(self, conn, table):
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_create_recipe_with_free_names_and_warnings(self):
        with self._archive() as (conn, _):
            outcome = rc.create_recipe(conn, "Rice and chicken", ["lunch", "dinner"],
                                       [("carnaroli rice", 100, "g"), ("CHICKEN BREAST", "150", "g")])
            self.assertEqual(outcome["warnings"], [])
            self.assertEqual(self._count(conn, "RECIPE_MEALS"), 2)
            self.assertEqual(self._count(conn, "RECIPE_INGREDIENTS"), 2)
            kcal = conn.execute("SELECT Kcal FROM RECIPE_MACROS_VIEW WHERE Recipe_ID = ?", (outcome["Recipe_ID"],)).fetchone()[0]
            self.assertAlmostEqual(kcal, 515.0, delta=1e-6)
            self.assertTrue(bool(rc.create_recipe(conn, "Dessert", ["breakfast"], [("Sugar", 1, "pcs")])["warnings"]))

    def test_validations_leave_no_partial_writes(self):
        with self._archive() as (conn, _):
            rc.create_recipe(conn, "Base", ["lunch"], [("Rice", 100, "g")])
            cases = [("", ["lunch"], [("Rice", 1, "g")]), ("X", ["merenda"], [("Rice", 1, "g")]),
                     ("X", [], [("Rice", 1, "g")]), ("X", ["lunch"], []), ("X", ["lunch"], [("Marble", 1, "g")]),
                     ("X", ["lunch"], [("Rice", 0, "g")]), ("X", ["lunch"], [("Rice", 1, "quintals")]),
                     ("base", ["lunch"], [("Rice", 1, "g")])]
            for name, meals, ingredients in cases:
                with self.assertRaises(ValueError):
                    rc.create_recipe(conn, name, meals, ingredients)
            self.assertEqual((self._count(conn, "RECIPES"), self._count(conn, "RECIPE_MEALS"), self._count(conn, "RECIPE_INGREDIENTS")), (1, 1, 1))

    def test_import_json_and_csv_skipping_invalid_recipes(self):
        with self._archive() as (conn, tmp):
            j = os.path.join(tmp, "r.json")
            with open(j, "w", encoding="utf-8") as f:
                json.dump([{"name": "Pasta and passata", "meals": ["lunch"], "ingredients": [
                               {"product": "Spaghetti", "quantity": 100, "unit": "g"},
                               {"product": "Oil", "quantity": 1, "unit": "tbsp", "to_taste": True}]},
                           {"name": "Broken", "meals": ["lunch"], "ingredients": [{"product": "Marble", "quantity": 1, "unit": "g"}]}], f)
            created, errors = rc.import_file(conn, j)
            self.assertEqual(created, 1)
            self.assertEqual(len(errors), 1)
            self.assertIn("Marble", errors[0])
            self.assertEqual(conn.execute("SELECT To_Taste FROM RECIPE_INGREDIENTS WHERE Unit = 'tbsp'").fetchone()[0], 1)

            c = os.path.join(tmp, "r.csv")
            with open(c, "w", encoding="utf-8") as f:
                f.write(
                    "recipe;meals;product;quantity;unit\n"
                    "Yogurt and apples;breakfast|dinner;Yogurt;2;pcs\n"
                    "Yogurt and apples;breakfast|dinner;Apples;1,5;pcs\n"
                )
            self.assertEqual(rc.import_file(conn, c), (1, []))
            self.assertEqual(self._count(conn, "RECIPES"), 2)

    def test_delete_only_if_unused(self):
        with self._archive() as (conn, _):
            rid = rc.create_recipe(conn, "To use", ["lunch"], [("Rice", 100, "g")])["Recipe_ID"]
            rc.create_recipe(conn, "To delete", ["lunch"], [("Rice", 100, "g")])
            rc.delete_recipe(conn, "to delete")
            self.assertEqual(self._count(conn, "RECIPES"), 1)
            conn.execute("INSERT INTO COOKINGS (Recipe_ID, Cooking_Date, Total_Servings, Remaining_Servings) VALUES (?, '2026-10-24', 1, 1)", (rid,))
            conn.commit()
            for name in ("To use", "Nonexistent"):
                with self.assertRaises(ValueError):
                    rc.delete_recipe(conn, name)



# ======================================================================
# SECTION: tests/test_recipe_insertion_flow.py
# ======================================================================

"""
tests.test_recipe_insertion_flow - Comprehensive unit & integration tests for Requirement R4:
- register_product_on_the_fly and is_ingredient_known
- Recipe creation with Notes and To-Taste ("Q.b.") flags
- QuickProductDialog & AddRecipeDialog GUI lifecycles (headless wait=False)
- Bilingual meal mapping (English/Italian)
- Servings scaling and live macro summaries
- On-the-fly product creation integration during recipe entry
- CSV recipe import preservation (semicolon, comma, notes, to_taste, error resilience)
"""

import csv
import contextlib
import io
import os
import shutil
import sqlite3
import tempfile
import tkinter as tk
from tkinter import ttk
import unittest

from unittest.mock import patch

from meal_planner import recipes as rc
from meal_planner.db.connection import get_connection
from meal_planner.db.schema import migrate
from meal_planner.gui.app import App
from meal_planner.gui.dialogs import AddRecipeDialog, QuickProductDialog, _MealDict, RecipeDetailsDialog
from scripts.seed_inventory import seed


class BaseRecipeFlowTestCase(unittest.TestCase):
    """Fixture providing isolated ephemeral SQLite database and headless Tkinter root."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_flow.db")
        with contextlib.redirect_stdout(io.StringIO()):
            seed(self.db_path, False)
        conn = get_connection(self.db_path)
        migrate(conn)
        conn.close()

        self._orig_env = os.environ.get("MEAL_PLANNER_DB")
        os.environ["MEAL_PLANNER_DB"] = self.db_path

        # Patch modal alerts
        self._patch_err = patch("tkinter.messagebox.showerror")
        self._patch_warn = patch("tkinter.messagebox.showwarning")
        self._patch_info = patch("tkinter.messagebox.showinfo")
        self.mock_err = self._patch_err.start()
        self.mock_warn = self._patch_warn.start()
        self.mock_info = self._patch_info.start()

        self.root = tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        self._patch_err.stop()
        self._patch_warn.stop()
        self._patch_info.stop()
        try:
            self.root.destroy()
        except Exception:
            pass
        if self._orig_env is not None:
            os.environ["MEAL_PLANNER_DB"] = self._orig_env
        else:
            os.environ.pop("MEAL_PLANNER_DB", None)
        shutil.rmtree(self.temp_dir, ignore_errors=True)


class TestRegisterProductOnTheFly(BaseRecipeFlowTestCase):
    """Unit tests for register_product_on_the_fly and is_ingredient_known."""

    def test_valid_product_registration(self):
        conn = get_connection(self.db_path)
        macros = {"Kcal_100g": 140.0, "Protein_100g": 15.0, "Carbs_100g": 2.0, "Fat_100g": 8.0}
        pid = rc.register_product_on_the_fly(conn, name="Tofu Naturale", unit="kg", macros=macros)
        self.assertIsInstance(pid, int)
        self.assertGreater(pid, 0)

        # Check ALIASES
        cur = conn.cursor()
        cur.execute("SELECT ID FROM ALIASES WHERE LOWER(Name) = 'tofu naturale'")
        self.assertEqual(cur.fetchone()[0], pid)

        # Check PRODUCTS
        cur.execute("SELECT Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g FROM PRODUCTS WHERE ID = ?", (pid,))
        row = cur.fetchone()
        self.assertEqual(row, ("kg", 140.0, 15.0, 2.0, 8.0))
        conn.close()

    def test_unit_canonicalization(self):
        conn = get_connection(self.db_path)
        macros = {"Kcal_100g": 50.0, "Protein_100g": 1.0, "Carbs_100g": 10.0, "Fat_100g": 0.5}

        # 'g' -> 'kg'
        pid_g = rc.register_product_on_the_fly(conn, name="Zucchero di Canna", unit="g", macros=macros)
        cur = conn.cursor()
        cur.execute("SELECT Unit FROM PRODUCTS WHERE ID = ?", (pid_g,))
        self.assertEqual(cur.fetchone()[0], "kg")

        # 'ml' -> 'L'
        pid_ml = rc.register_product_on_the_fly(conn, name="Latte di Mandorla", unit="ml", macros=macros)
        cur.execute("SELECT Unit FROM PRODUCTS WHERE ID = ?", (pid_ml,))
        self.assertEqual(cur.fetchone()[0], "L")

        # 'pz' -> 'pcs'
        pid_pz = rc.register_product_on_the_fly(conn, name="Uova Medie", unit="pz", macros=macros)
        cur.execute("SELECT Unit FROM PRODUCTS WHERE ID = ?", (pid_pz,))
        self.assertEqual(cur.fetchone()[0], "pcs")

        # Invalid unit raises ValueError
        with self.assertRaises(ValueError):
            rc.register_product_on_the_fly(conn, name="Invalid Item", unit="gallons", macros=macros)
        conn.close()

    def test_optional_macros_and_factors(self):
        conn = get_connection(self.db_path)
        macros = {
            "Kcal_100g": 250.0,
            "Protein_100g": 12.0,
            "Carbs_100g": 40.0,
            "Fat_100g": 5.0,
            "Fiber_100g": 6.5,
            "Sugars_100g": 2.0,
            "Saturated_fat_100g": 1.0,
            "Salt_100g": 0.3,
        }
        pid = rc.register_product_on_the_fly(
            conn,
            name="Pane Integrale Artigianale",
            unit="pcs",
            macros=macros,
            grams_per_piece=60.0,
        )
        cur = conn.cursor()
        cur.execute("""
            SELECT Fiber_100g, Sugars_100g, Saturated_fat_100g, Salt_100g, Grams_per_piece
            FROM PRODUCTS WHERE ID = ?
        """, (pid,))
        row = cur.fetchone()
        self.assertEqual(row, (6.5, 2.0, 1.0, 0.3, 60.0))
        conn.close()

    def test_duplicate_alias_idempotency(self):
        conn = get_connection(self.db_path)
        macros = {"Kcal_100g": 100.0, "Protein_100g": 5.0, "Carbs_100g": 10.0, "Fat_100g": 2.0}
        pid1 = rc.register_product_on_the_fly(conn, name="Seitan Naturale", unit="kg", macros=macros)
        # Calling again with identical or different casing returns same ID
        pid2 = rc.register_product_on_the_fly(conn, name="seitan naturale", unit="kg", macros=macros)
        self.assertEqual(pid1, pid2)
        conn.close()

    def test_substitute_group_collision(self):
        conn = get_connection(self.db_path)
        # Seed a substitute group
        cur = conn.cursor()
        cur.execute("INSERT INTO SUBSTITUTE_GROUPS (Name) VALUES ('Cereal Group')")
        conn.commit()

        macros = {"Kcal_100g": 100.0, "Protein_100g": 5.0, "Carbs_100g": 10.0, "Fat_100g": 2.0}
        with self.assertRaises(ValueError) as ctx:
            rc.register_product_on_the_fly(conn, name="Cereal Group", unit="kg", macros=macros)
        self.assertIn("conflicts with an existing substitute group", str(ctx.exception))
        conn.close()

    def test_invalid_macro_validations(self):
        conn = get_connection(self.db_path)
        # Missing required macro
        with self.assertRaises(ValueError):
            rc.register_product_on_the_fly(conn, name="Bad Item", unit="kg", macros={"Kcal_100g": 100})

        # Negative macro
        with self.assertRaises(ValueError):
            rc.register_product_on_the_fly(
                conn, name="Neg Item", unit="kg",
                macros={"Kcal_100g": -10, "Protein_100g": 5, "Carbs_100g": 5, "Fat_100g": 2}
            )

        # Boolean macro
        with self.assertRaises(ValueError):
            rc.register_product_on_the_fly(
                conn, name="Bool Item", unit="kg",
                macros={"Kcal_100g": True, "Protein_100g": 5, "Carbs_100g": 5, "Fat_100g": 2}
            )

        # Empty name
        with self.assertRaises(ValueError):
            rc.register_product_on_the_fly(
                conn, name="   ", unit="kg",
                macros={"Kcal_100g": 100, "Protein_100g": 5, "Carbs_100g": 5, "Fat_100g": 2}
            )
        conn.close()

    def test_is_ingredient_known(self):
        conn = get_connection(self.db_path)
        # Aliases from seed: Rice exists
        self.assertTrue(rc.is_ingredient_known(conn, "Rice"))
        self.assertTrue(rc.is_ingredient_known(conn, "rice"))
        self.assertFalse(rc.is_ingredient_known(conn, "Unknown Space Food"))
        self.assertFalse(rc.is_ingredient_known(conn, ""))
        self.assertFalse(rc.is_ingredient_known(conn, None))
        conn.close()


class TestCreateRecipeNotesAndToTaste(BaseRecipeFlowTestCase):
    """Tests for create_recipe handling Notes and 4-tuple To-Taste ("Q.b.") entries."""

    def test_recipe_with_notes_saved(self):
        conn = get_connection(self.db_path)
        res = rc.create_recipe(
            conn,
            name="Pasta con Note",
            meals=["lunch"],
            ingredients=[("Rice", 100, "g")],
            notes="Step 1: Boil water.\nStep 2: Cook 10 mins.",
        )
        self.assertIn("Recipe_ID", res)
        cur = conn.cursor()
        cur.execute("SELECT Notes FROM RECIPES WHERE Recipe_ID = ?", (res["Recipe_ID"],))
        self.assertEqual(cur.fetchone()[0], "Step 1: Boil water.\nStep 2: Cook 10 mins.")
        conn.close()

    def test_recipe_4_tuple_to_taste(self):
        conn = get_connection(self.db_path)
        # 4-tuples with to_taste 0 and 1
        res = rc.create_recipe(
            conn,
            name="Zuppa di Riso e Sale",
            meals=["dinner"],
            ingredients=[
                ("Rice", 80, "g", 0),
                ("Rice", 5, "g", 1),
            ],
        )
        self.assertIn("Recipe_ID", res)
        cur = conn.cursor()
        cur.execute("SELECT To_Taste FROM RECIPE_INGREDIENTS WHERE Recipe_ID = ? ORDER BY Row_ID", (res["Recipe_ID"],))
        rows = cur.fetchall()
        self.assertEqual(rows, [(0,), (1,)])
        conn.close()

    def test_recipe_dict_to_taste_truthy_variations(self):
        conn = get_connection(self.db_path)
        for flag in (1, "yes", "si", "sì", "true", "x"):
            name = f"Recipe Taste {flag}"
            res = rc.create_recipe(
                conn,
                name=name,
                meals=["lunch"],
                ingredients=[{"product": "Rice", "quantity": 50, "unit": "g", "to_taste": flag}],
            )
            cur = conn.cursor()
            cur.execute("SELECT To_Taste FROM RECIPE_INGREDIENTS WHERE Recipe_ID = ?", (res["Recipe_ID"],))
            self.assertEqual(cur.fetchone()[0], 1, f"Failed for flag: {flag}")
        conn.close()

    def test_create_recipe_with_on_the_fly_product(self):
        conn = get_connection(self.db_path)
        new_pid = rc.register_product_on_the_fly(
            conn,
            name="Tempeh Affumicato",
            unit="kg",
            macros={"Kcal_100g": 190.0, "Protein_100g": 20.0, "Carbs_100g": 3.0, "Fat_100g": 11.0},
        )
        res = rc.create_recipe(
            conn,
            name="Tempeh Bowl",
            meals=["lunch", "dinner"],
            ingredients=[("Tempeh Affumicato", 120, "g")],
        )
        self.assertIn("Recipe_ID", res)
        conn.close()


class TestAddRecipeDialogGUI(BaseRecipeFlowTestCase):
    """Integration tests for AddRecipeDialog and QuickProductDialog in headless mode."""

    def test_quick_product_dialog_save_and_cancel(self):
        dlg = QuickProductDialog(self.root, name="Edamame", suggested_unit="g", wait=False, db_path=self.db_path)
        dlg.v_kcal.set("122")
        dlg.v_prot.set("11")
        dlg.v_carb.set("9")
        dlg.v_fat.set("5")
        dlg.v_fiber.set("5.2")
        self.assertTrue(dlg.save())
        self.assertIsNotNone(dlg.result)
        self.assertEqual(dlg.result["name"], "Edamame")
        self.assertEqual(dlg.result["macros"]["Kcal_100g"], 122.0)
        self.assertEqual(dlg.result["macros"]["Fiber_100g"], 5.2)

        # Cancel test
        dlg2 = QuickProductDialog(self.root, name="Canceme", wait=False, db_path=self.db_path)
        dlg2.cancel()
        self.assertIsNone(dlg2.result)

    def test_add_recipe_dialog_headless_lifecycle(self):
        saved_outcomes = []

        dlg = AddRecipeDialog(
            self.root,
            db_path=self.db_path,
            wait=False,
            on_save_callback=lambda res: saved_outcomes.append(res),
        )
        self.assertIsInstance(dlg, tk.Toplevel)

        # Fill Recipe details
        dlg.v_name.set("Insalata di Riso Veloce")
        dlg.notes = "Servire fredda con limone."
        self.assertEqual(dlg.notes, "Servire fredda con limone.")

        # Test bilingual meal selection (Italian: pranzo -> lunch)
        self.assertIn("pranzo", dlg.v_meals)
        dlg.v_meals["pranzo"].set(True)
        self.assertTrue(dlg.v_meals["lunch"].get())

        # Add ingredient: Rice 100g
        dlg.v_ingredient_name.set("Rice")
        dlg.v_quantity.set("100")
        dlg.v_unit.set("g")
        self.assertTrue(dlg.add_ingredient())
        self.assertEqual(len(dlg.ingredients), 1)

        # Add To-Taste ingredient with blank quantity (should default to nominal 1.0)
        dlg.v_ingredient_name.set("Rice")
        dlg.v_quantity.set("")
        dlg.v_to_taste.set(True)
        self.assertTrue(dlg.add_ingredient())
        self.assertEqual(len(dlg.ingredients), 2)
        self.assertEqual(dlg.ingredients[1]["quantity"], 1.0)
        self.assertEqual(dlg.ingredients[1]["to_taste"], 1)

        # Save dialog
        self.assertTrue(dlg.save())
        self.assertEqual(len(saved_outcomes), 1)
        self.assertEqual(saved_outcomes[0]["Name"], "Insalata di Riso Veloce")

        # Verify persisted in DB
        conn = get_connection(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT Notes FROM RECIPES WHERE Name = 'Insalata di Riso Veloce'")
        row = cur.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "Servire fredda con limone.")
        conn.close()

    def test_servings_scaling(self):
        dlg = AddRecipeDialog(self.root, db_path=self.db_path, wait=False)
        dlg.v_name.set("Pasta per Due")
        dlg.v_meals["dinner"].set(True)
        dlg.v_servings.set("2")

        dlg.v_ingredient_name.set("Rice")
        dlg.v_quantity.set("200")
        dlg.v_unit.set("g")
        dlg.add_ingredient()

        self.assertTrue(dlg.save())

        conn = get_connection(self.db_path)
        cur = conn.cursor()
        cur.execute("""
            SELECT ri.Quantity
            FROM RECIPE_INGREDIENTS ri
            JOIN RECIPES r ON ri.Recipe_ID = r.Recipe_ID
            WHERE r.Name = 'Pasta per Due'
        """)
        qty = cur.fetchone()[0]
        # 200g entered for 2 servings -> 100g per 1 serving in DB
        self.assertEqual(qty, 100.0)
        conn.close()

    def test_remove_ingredient(self):
        dlg = AddRecipeDialog(self.root, db_path=self.db_path, wait=False)
        dlg.v_ingredient_name.set("Rice")
        dlg.v_quantity.set("100")
        dlg.add_ingredient()
        self.assertEqual(len(dlg.ingredients), 1)

        self.assertTrue(dlg.remove_ingredient())
        self.assertEqual(len(dlg.ingredients), 0)

    def test_on_the_fly_creation_during_recipe_entry(self):
        # When an unknown ingredient is added, on_unknown_product_callback is triggered
        def mock_unknown(name, unit):
            return {
                "name": name,
                "unit": unit,
                "macros": {"Kcal_100g": 180.0, "Protein_100g": 14.0, "Carbs_100g": 8.0, "Fat_100g": 6.0},
            }

        dlg = AddRecipeDialog(
            self.root,
            db_path=self.db_path,
            wait=False,
            on_unknown_product_callback=mock_unknown,
        )
        dlg.v_name.set("Piatto di Ceci Neri")
        dlg.v_meals["lunch"].set(True)

        dlg.v_ingredient_name.set("Ceci Neri Rari")
        dlg.v_quantity.set("150")
        dlg.v_unit.set("g")
        self.assertTrue(dlg.add_ingredient())

        # Ingredient should now be known in DB
        conn = get_connection(self.db_path)
        self.assertTrue(rc.is_ingredient_known(conn, "Ceci Neri Rari"))
        conn.close()

        self.assertTrue(dlg.save())

    def test_validation_guardrails(self):
        dlg = AddRecipeDialog(self.root, db_path=self.db_path, wait=False)

        # Empty name
        self.assertFalse(dlg.save())

        # Name provided, but no meals
        dlg.v_name.set("Invalid Recipe")
        self.assertFalse(dlg.save())

        # Meals provided, but no ingredients
        dlg.v_meals["lunch"].set(True)
        self.assertFalse(dlg.save())

        # Invalid servings
        dlg.v_ingredient_name.set("Rice")
        dlg.v_quantity.set("50")
        dlg.add_ingredient()
        dlg.v_servings.set("-2")
        self.assertFalse(dlg.save())


class TestAppRecipeIntegration(BaseRecipeFlowTestCase):
    """Tests integrating AddRecipeDialog into the main App window."""

    def test_app_button_and_dialog_open(self):
        app = App()
        app.withdraw()
        app.db_path = self.db_path
        app.refresh()

        self.assertTrue(hasattr(app, "btn_add_recipe"))
        dlg = app.open_add_recipe_dialog(wait=False)
        self.assertIsInstance(dlg, AddRecipeDialog)
        dlg.cancel()
        app.destroy()

    def test_app_open_dialogs_without_explicit_db_path(self):
        """Ensure opening AddRecipeDialog and RecipeDetailsDialog without explicit app.db_path does not raise NameError."""
        app = App()
        app.withdraw()
        dlg = app.open_add_recipe_dialog(wait=False)
        self.assertIsInstance(dlg, AddRecipeDialog)
        dlg.destroy()

        if app.t_ric.tv.get_children():
            app.t_ric.tv.selection_set(app.t_ric.tv.get_children()[0])
            details = app.open_recipe_details(wait=False)
            self.assertIsInstance(details, RecipeDetailsDialog)
            details.destroy()

        app.destroy()

    def test_app_save_recipe_updates_status_and_table(self):
        app = App()
        app.withdraw()
        app.db_path = self.db_path
        app.refresh()

        dlg = app.add_recipe(wait=False)
        dlg.v_name.set("Ricetta da App GUI")
        dlg.v_meals["lunch"].set(True)
        dlg.v_ingredient_name.set("Rice")
        dlg.v_quantity.set("100")
        dlg.add_ingredient()
        self.assertTrue(dlg.save())

        # Status bar should show saved message
        self.assertIn("Ricetta da App GUI", app.status.get())

        # Recipe should be present in app table
        recipe_names = [app.t_ric.tv.item(iid, "values")[0] for iid in app.t_ric.tv.get_children()]
        self.assertIn("Ricetta da App GUI", recipe_names)

        # Test opening recipe details dialog
        app.t_ric.tv.selection_set(app.t_ric.tv.get_children()[0])
        details_dlg = app.open_recipe_details(wait=False)
        self.assertIsInstance(details_dlg, RecipeDetailsDialog)
        self.assertTrue("Recipe Details" in details_dlg.title() or "Dettagli Ricetta" in details_dlg.title())
        details_dlg.destroy()

        app.destroy()

    def test_add_recipe_dialog_keyboard_shortcuts_and_refocus(self):
        app = App()
        app.withdraw()
        app.db_path = self.db_path
        app.refresh()

        dlg = app.open_add_recipe_dialog(wait=False)
        self.assertTrue(hasattr(dlg, "_set_initial_focus"))
        dlg.v_name.set("Keyboard Test Recipe")
        dlg.v_meals["lunch"].set(True)

        # Add ingredient and verify inputs cleared
        dlg.v_ingredient_name.set("Rice")
        dlg.v_quantity.set("150")
        self.assertTrue(dlg.add_ingredient())
        self.assertEqual(dlg.v_ingredient_name.get(), "")
        self.assertEqual(dlg.v_quantity.get(), "")
        self.assertEqual(len(dlg.ingredients), 1)

        # Save and verify
        self.assertTrue(dlg._on_save())
        app.destroy()


class TestCSVRecipeImportPreservation(BaseRecipeFlowTestCase):
    """Tests preserving and enhancing CSV recipe import."""

    def test_semicolon_delimited_csv(self):
        csv_path = os.path.join(self.temp_dir, "recipes_semi.csv")
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            f.write("recipe;meals;product;quantity;unit;to_taste\n")
            f.write("Pasta al Pomodoro;lunch|dinner;Rice;80;g;0\n")
            f.write("Pasta al Pomodoro;lunch|dinner;Rice;5;g;1\n")

        conn = get_connection(self.db_path)
        created, errors = rc.import_file(conn, csv_path)
        self.assertEqual(created, 1)
        self.assertEqual(errors, [])
        conn.close()

    def test_comma_delimited_csv_with_notes(self):
        csv_path = os.path.join(self.temp_dir, "recipes_comma.csv")
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            f.write("recipe,meals,product,quantity,unit,to_taste,notes\n")
            f.write('Riso Curry,lunch,Rice,100,g,0,"Consumare caldo"\n')

        conn = get_connection(self.db_path)
        created, errors = rc.import_file(conn, csv_path)
        self.assertEqual(created, 1)
        self.assertEqual(errors, [])

        cur = conn.cursor()
        cur.execute("SELECT Notes FROM RECIPES WHERE Name = 'Riso Curry'")
        row = cur.fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "Consumare caldo")
        conn.close()

    def test_csv_import_atomic_skip_invalid(self):
        csv_path = os.path.join(self.temp_dir, "recipes_mixed.csv")
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            f.write("recipe;meals;product;quantity;unit;to_taste\n")
            f.write("Good Recipe;lunch;Rice;100;g;0\n")
            f.write("Bad Recipe;lunch;NonExistentAlienIngredient;100;g;0\n")

        conn = get_connection(self.db_path)
        created, errors = rc.import_file(conn, csv_path)
        self.assertEqual(created, 1)
        self.assertEqual(len(errors), 1)
        self.assertIn("unknown ingredient 'NonExistentAlienIngredient'", errors[0])

        cur = conn.cursor()
        cur.execute("SELECT 1 FROM RECIPES WHERE Name = 'Good Recipe'")
        self.assertIsNotNone(cur.fetchone())
        cur.execute("SELECT 1 FROM RECIPES WHERE Name = 'Bad Recipe'")
        self.assertIsNone(cur.fetchone())
        conn.close()




# ======================================================================
# SECTION: tests/test_adversarial_recipe_flow.py
# ======================================================================

"""
tests.test_adversarial_recipe_flow - Adversarial verification harness for Milestone M3 Challenger 2.
Challenges AddRecipeDialog GUI and CSV Import Preservation under stressful and edge-case conditions.
"""

import contextlib
import io
import os
import shutil
import sqlite3
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

from meal_planner import recipes as rc
from meal_planner.db.connection import get_connection
from meal_planner.db.schema import migrate
from meal_planner.gui.dialogs import AddRecipeDialog, QuickProductDialog, _MealDict, RecipeDetailsDialog
from scripts.seed_inventory import seed


class BaseAdversarialTestCase(unittest.TestCase):
    """Fixture providing isolated ephemeral SQLite database and headless Tkinter root."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "adversarial_test.db")
        with contextlib.redirect_stdout(io.StringIO()):
            seed(self.db_path, False)
        conn = get_connection(self.db_path)
        migrate(conn)
        conn.close()

        self._orig_env = os.environ.get("MEAL_PLANNER_DB")
        os.environ["MEAL_PLANNER_DB"] = self.db_path

        # Patch UI dialog popups
        self._patch_err = patch("tkinter.messagebox.showerror")
        self._patch_warn = patch("tkinter.messagebox.showwarning")
        self._patch_info = patch("tkinter.messagebox.showinfo")
        self.mock_err = self._patch_err.start()
        self.mock_warn = self._patch_warn.start()
        self.mock_info = self._patch_info.start()

        self.root = tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        self._patch_err.stop()
        self._patch_warn.stop()
        self._patch_info.stop()
        try:
            self.root.destroy()
        except Exception:
            pass
        if self._orig_env is not None:
            os.environ["MEAL_PLANNER_DB"] = self._orig_env
        else:
            os.environ.pop("MEAL_PLANNER_DB", None)
        shutil.rmtree(self.temp_dir, ignore_errors=True)


class TestAddRecipeDialogAdversarial(BaseAdversarialTestCase):
    """Adversarial stress-tests against AddRecipeDialog."""

    def test_headless_lifecycle_and_cancellation(self):
        """Verify dialog initializes with wait=False, can be cancelled, leaving no DB side-effects."""
        dlg = AddRecipeDialog(self.root, db_path=self.db_path, wait=False)
        self.assertIsInstance(dlg, tk.Toplevel)
        self.assertIsNone(dlg.result)

        # Partial input
        dlg.v_name.set("Ricetta Annullata")
        dlg.v_meals["lunch"].set(True)
        dlg.v_ingredient_name.set("Rice")
        dlg.v_quantity.set("50")
        dlg.add_ingredient()

        # Cancel dialog
        dlg.cancel()
        self.assertIsNone(dlg.result)

        conn = get_connection(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM RECIPES WHERE Name = 'Ricetta Annullata'")
        self.assertEqual(cur.fetchone()[0], 0)
        conn.close()

    def test_bilingual_meal_mapping_full_spectrum(self):
        """Stress-test bilingual meal lookup: colazione, pranzo, cena, breakfast, lunch, dinner."""
        dlg = AddRecipeDialog(self.root, db_path=self.db_path, wait=False)

        # Italian aliases -> English canonical
        self.assertIn("colazione", dlg.v_meals)
        self.assertIn("pranzo", dlg.v_meals)
        self.assertIn("cena", dlg.v_meals)

        dlg.v_meals["colazione"].set(True)
        self.assertTrue(dlg.v_meals["breakfast"].get())
        dlg.v_meals["colazione"].set(False)
        self.assertFalse(dlg.v_meals["breakfast"].get())

        dlg.v_meals["pranzo"].set(True)
        self.assertTrue(dlg.v_meals["lunch"].get())

        dlg.v_meals["cena"].set(True)
        self.assertTrue(dlg.v_meals["dinner"].get())

        # Test case-insensitivity: 'COLAZIONE', 'Pranzo', 'CenA'
        self.assertTrue(dlg.v_meals["PRANZO"].get())
        self.assertTrue(dlg.v_meals["CenA"].get())

        # Save with Italian meal flags active
        dlg.v_name.set("Risotto Bilingue")
        dlg.v_ingredient_name.set("Rice")
        dlg.v_quantity.set("120")
        dlg.add_ingredient()

        self.assertTrue(dlg.save())

        conn = get_connection(self.db_path)
        cur = conn.cursor()
        cur.execute("""
            SELECT rm.Meal
            FROM RECIPE_MEALS rm
            JOIN RECIPES r ON rm.Recipe_ID = r.Recipe_ID
            WHERE r.Name = 'Risotto Bilingue'
            ORDER BY rm.Meal
        """)
        meals_in_db = [row[0] for row in cur.fetchall()]
        # pranzo -> lunch, cena -> dinner
        self.assertEqual(meals_in_db, ["dinner", "lunch"])
        conn.close()

    def test_bilingual_snack_merenda_spuntino_mapping(self):
        """Test snack/spuntino/merenda mapping when snack is present in MEALS table."""
        conn = get_connection(self.db_path)
        # Add snack to MEALS
        conn.execute("INSERT INTO MEALS (Name, Sort_Order, Energy_Share_pct) VALUES ('snack', 4, 10.0)")
        conn.commit()
        conn.close()

        dlg = AddRecipeDialog(self.root, db_path=self.db_path, wait=False)
        self.assertIn("snack", dlg.v_meals)
        self.assertIn("spuntino", dlg.v_meals)
        self.assertIn("merenda", dlg.v_meals)

        # Setting 'spuntino' sets 'snack' and 'merenda'
        dlg.v_meals["spuntino"].set(True)
        self.assertTrue(dlg.v_meals["snack"].get())
        self.assertTrue(dlg.v_meals["merenda"].get())

        dlg.v_name.set("Spuntino Proteico")
        dlg.v_ingredient_name.set("Rice")
        dlg.v_quantity.set("40")
        dlg.add_ingredient()

        self.assertTrue(dlg.save())

        conn = get_connection(self.db_path)
        cur = conn.cursor()
        cur.execute("""
            SELECT rm.Meal
            FROM RECIPE_MEALS rm
            JOIN RECIPES r ON rm.Recipe_ID = r.Recipe_ID
            WHERE r.Name = 'Spuntino Proteico'
        """)
        self.assertEqual(cur.fetchall(), [("snack",)])
        conn.close()

    def test_servings_scaling_adversarial_values(self):
        """Test servings scaling with various divisors (4 servings, 2.5 servings, 1,5 servings)."""
        scenarios = [
            ("4", 400.0, 100.0),      # 4 servings: 400g / 4 = 100g
            ("2.5", 250.0, 100.0),    # 2.5 servings: 250g / 2.5 = 100g
            ("1,5", 300.0, 200.0),    # 1,5 servings (decimal comma): 300g / 1.5 = 200g
            ("8", 80.0, 10.0),        # 8 servings: 80g / 8 = 10g
        ]

        for servings_str, raw_qty, expected_scaled in scenarios:
            name = f"Scaling Recipe {servings_str}"
            dlg = AddRecipeDialog(self.root, db_path=self.db_path, wait=False)
            dlg.v_name.set(name)
            dlg.v_meals["lunch"].set(True)
            dlg.v_servings.set(servings_str)

            dlg.v_ingredient_name.set("Rice")
            dlg.v_quantity.set(str(raw_qty))
            dlg.v_unit.set("g")
            dlg.add_ingredient()

            self.assertTrue(dlg.save(), f"Failed to save recipe for servings '{servings_str}'")

            conn = get_connection(self.db_path)
            cur = conn.cursor()
            cur.execute("""
                SELECT ri.Quantity
                FROM RECIPE_INGREDIENTS ri
                JOIN RECIPES r ON ri.Recipe_ID = r.Recipe_ID
                WHERE r.Name = ?
            """, (name,))
            qty = cur.fetchone()[0]
            self.assertAlmostEqual(qty, expected_scaled, places=4,
                                   msg=f"Scaling failed for servings={servings_str}: got {qty}, expected {expected_scaled}")
            conn.close()

    def test_servings_scaling_with_multiple_ingredients_and_units(self):
        """Verify scaling applies uniformly to multiple ingredients with different units."""
        dlg = AddRecipeDialog(self.root, db_path=self.db_path, wait=False)
        dlg.v_name.set("Multi-ingredient Banquet")
        dlg.v_meals["dinner"].set(True)
        dlg.v_servings.set("4")

        # 1. Rice 400g -> 100g
        dlg.v_ingredient_name.set("Rice")
        dlg.v_quantity.set("400")
        dlg.v_unit.set("g")
        dlg.add_ingredient()

        # 2. Rice 0.8 kg -> 0.2 kg
        dlg.v_ingredient_name.set("Rice")
        dlg.v_quantity.set("0.8")
        dlg.v_unit.set("kg")
        dlg.add_ingredient()

        # 3. Rice to-taste: blank quantity defaults to 1.0 -> 0.25 in DB
        dlg.v_ingredient_name.set("Rice")
        dlg.v_quantity.set("")
        dlg.v_to_taste.set(True)
        dlg.add_ingredient()

        self.assertTrue(dlg.save())

        conn = get_connection(self.db_path)
        cur = conn.cursor()
        cur.execute("""
            SELECT ri.Quantity, ri.Unit, ri.To_Taste
            FROM RECIPE_INGREDIENTS ri
            JOIN RECIPES r ON ri.Recipe_ID = r.Recipe_ID
            WHERE r.Name = 'Multi-ingredient Banquet'
            ORDER BY ri.Row_ID
        """)
        rows = cur.fetchall()
        self.assertEqual(len(rows), 3)
        self.assertAlmostEqual(rows[0][0], 100.0)
        self.assertEqual(rows[0][1], "g")
        self.assertEqual(rows[0][2], 0)

        self.assertAlmostEqual(rows[1][0], 0.2)
        self.assertEqual(rows[1][1], "kg")
        self.assertEqual(rows[1][2], 0)

        self.assertAlmostEqual(rows[2][0], 0.25)
        self.assertEqual(rows[2][2], 1)
        conn.close()

    def test_preparation_notes_multiline_unicode_emojis_injection(self):
        """Stress-test notes with multiline newlines, unicode accents, emojis, quotes, and SQL injection strings."""
        test_notes = (
            "Line 1: Tagliare a cubetti le carote & patate.\n"
            "Line 2: Rosolare con olio extravergine d'oliva a fuoco vivo.\n"
            "Line 3: 🍕 Delizioso! Emojis: 🍲🥗👨‍🍳❤️🔥\n"
            "Line 4: Accents: Caffè, crème fraîche, purée, à la carte, piñata.\n"
            "Line 5: Injection attempt: '; DROP TABLE RECIPES; -- \"quoted\" <tag>&</tag>"
        )

        dlg = AddRecipeDialog(self.root, db_path=self.db_path, wait=False)
        dlg.v_name.set("Gourmet Unicode Dish")
        dlg.v_meals["lunch"].set(True)
        dlg.notes = test_notes
        self.assertEqual(dlg.notes, test_notes)

        dlg.v_ingredient_name.set("Rice")
        dlg.v_quantity.set("100")
        dlg.add_ingredient()

        self.assertTrue(dlg.save())

        conn = get_connection(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT Notes FROM RECIPES WHERE Name = 'Gourmet Unicode Dish'")
        saved_notes = cur.fetchone()[0]
        self.assertEqual(saved_notes, test_notes)
        conn.close()

    def test_to_taste_toggle_default_nominal_and_explicit_quantity(self):
        """Stress-test Q.b. (To-Taste) toggle with blank vs explicit quantities."""
        dlg = AddRecipeDialog(self.root, db_path=self.db_path, wait=False)
        dlg.v_name.set("Q.b. Precision Recipe")
        dlg.v_meals["lunch"].set(True)

        # 1. Blank quantity + to_taste=True -> defaults to 1.0 nominal quantity
        dlg.v_ingredient_name.set("Rice")
        dlg.v_quantity.set("")
        dlg.v_to_taste.set(True)
        self.assertTrue(dlg.add_ingredient())
        self.assertEqual(dlg.ingredients[0]["quantity"], 1.0)
        self.assertEqual(dlg.ingredients[0]["to_taste"], 1)

        # 2. Explicit quantity + to_taste=True -> keeps explicit quantity
        dlg.v_ingredient_name.set("Rice")
        dlg.v_quantity.set("2.5")
        dlg.v_to_taste.set(True)
        self.assertTrue(dlg.add_ingredient())
        self.assertEqual(dlg.ingredients[1]["quantity"], 2.5)
        self.assertEqual(dlg.ingredients[1]["to_taste"], 1)

        # 3. Explicit quantity + to_taste=False -> to_taste=0
        dlg.v_ingredient_name.set("Rice")
        dlg.v_quantity.set("150")
        dlg.v_to_taste.set(False)
        self.assertTrue(dlg.add_ingredient())
        self.assertEqual(dlg.ingredients[2]["quantity"], 150.0)
        self.assertEqual(dlg.ingredients[2]["to_taste"], 0)

        self.assertTrue(dlg.save())

        conn = get_connection(self.db_path)
        cur = conn.cursor()
        cur.execute("""
            SELECT Quantity, To_Taste
            FROM RECIPE_INGREDIENTS ri
            JOIN RECIPES r ON ri.Recipe_ID = r.Recipe_ID
            WHERE r.Name = 'Q.b. Precision Recipe'
            ORDER BY ri.Row_ID
        """)
        rows = cur.fetchall()
        self.assertEqual(rows, [(1.0, 1), (2.5, 1), (150.0, 0)])
        conn.close()

    def test_draft_treeview_ingredient_removal_and_macro_summary(self):
        """Stress-test adding, removing by selection, removing by index, and macro summary calculation."""
        dlg = AddRecipeDialog(self.root, db_path=self.db_path, wait=False)
        dlg.v_name.set("Treeview Stress Test")
        dlg.v_meals["lunch"].set(True)

        # Add 3 ingredients
        dlg.v_ingredient_name.set("Rice")
        dlg.v_quantity.set("100")
        dlg.add_ingredient()

        dlg.v_ingredient_name.set("Rice")
        dlg.v_quantity.set("50")
        dlg.add_ingredient()

        dlg.v_ingredient_name.set("Rice")
        dlg.v_quantity.set("25")
        dlg.add_ingredient()

        self.assertEqual(len(dlg.ingredients), 3)
        children = dlg.t_ingredients.get_children()
        self.assertEqual(len(children), 3)

        # Remove middle ingredient by index
        dlg.remove_ingredient(index=1)
        self.assertEqual(len(dlg.ingredients), 2)
        self.assertEqual(dlg.ingredients[0]["quantity"], 100.0)
        self.assertEqual(dlg.ingredients[1]["quantity"], 25.0)

        # Remove by selection
        children = dlg.t_ingredients.get_children()
        dlg.t_ingredients.selection_set(children[0])
        dlg.remove_ingredient()
        self.assertEqual(len(dlg.ingredients), 1)
        self.assertEqual(dlg.ingredients[0]["quantity"], 25.0)

        # Remove remaining item without selection (pops last)
        dlg.t_ingredients.selection_remove(dlg.t_ingredients.selection())
        dlg.remove_ingredient()
        self.assertEqual(len(dlg.ingredients), 0)

        # Saving with 0 ingredients must fail guardrail
        self.assertFalse(dlg.save())

        # Re-add ingredient and save successfully
        dlg.v_ingredient_name.set("Rice")
        dlg.v_quantity.set("80")
        dlg.add_ingredient()
        self.assertTrue(dlg.save())

    def test_validation_guardrails_negative_scenarios(self):
        """Stress-test all validation guardrails: empty name, missing meals, zero ingredients, invalid quantities."""
        dlg = AddRecipeDialog(self.root, db_path=self.db_path, wait=False)

        # 1. Empty name
        dlg.v_name.set("")
        self.assertFalse(dlg.save())
        dlg.v_name.set("   ")
        self.assertFalse(dlg.save())

        # 2. Missing meals
        dlg.v_name.set("Valid Name")
        for m, var in dlg.v_meals.items():
            var.set(False)
        self.assertFalse(dlg.save())

        # 3. Missing ingredients
        dlg.v_meals["dinner"].set(True)
        self.assertFalse(dlg.save())

        # 4. Invalid ingredient quantities during add_ingredient
        dlg.v_ingredient_name.set("Rice")
        for invalid_qty in ("0", "-5", "-0.1", "abc", "!@#"):
            dlg.v_quantity.set(invalid_qty)
            self.assertFalse(dlg.add_ingredient(), f"add_ingredient unexpectedly succeeded for quantity: {invalid_qty}")

        # 5. Invalid servings during save
        dlg.v_quantity.set("100")
        self.assertTrue(dlg.add_ingredient())
        for invalid_serv in ("0", "-1", "-2.5", "not_a_number"):
            dlg.v_servings.set(invalid_serv)
            self.assertFalse(dlg.save(), f"save unexpectedly succeeded for servings: {invalid_serv}")

        # 6. Duplicate recipe name error handling
        dlg.v_servings.set("1")
        self.assertTrue(dlg.save())

        # Second dialog trying to save same recipe name
        dlg2 = AddRecipeDialog(self.root, db_path=self.db_path, wait=False)
        dlg2.v_name.set("Valid Name")
        dlg2.v_meals["dinner"].set(True)
        dlg2.v_ingredient_name.set("Rice")
        dlg2.v_quantity.set("50")
        dlg2.add_ingredient()
        self.assertFalse(dlg2.save())
        self.assertIn("already exists", str(dlg2.last_error))


class TestCSVImportAdversarial(BaseAdversarialTestCase):
    """Adversarial stress-tests against CSV Recipe Import."""

    def test_semicolon_delimited_with_decimal_commas(self):
        """CSV with semicolon delimiter and decimal commas (e.g. 1,5 kg)."""
        csv_path = os.path.join(self.temp_dir, "semi_decimal.csv")
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            f.write("recipe;meals;product;quantity;unit;to_taste;notes\n")
            f.write("Semicolon Risotto;lunch|dinner;Rice;1,5;kg;0;Cucinare lentamente\n")
            f.write("Semicolon Risotto;lunch|dinner;Rice;0,25;kg;1;Sale q.b.\n")

        conn = get_connection(self.db_path)
        created, errors = rc.import_file(conn, csv_path)
        self.assertEqual(created, 1)
        self.assertEqual(errors, [])

        cur = conn.cursor()
        cur.execute("""
            SELECT ri.Quantity, ri.Unit, ri.To_Taste, r.Notes
            FROM RECIPE_INGREDIENTS ri
            JOIN RECIPES r ON ri.Recipe_ID = r.Recipe_ID
            WHERE r.Name = 'Semicolon Risotto'
            ORDER BY ri.Row_ID
        """)
        rows = cur.fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], (1.5, "kg", 0, "Cucinare lentamente"))
        self.assertEqual(rows[1], (0.25, "kg", 1, "Cucinare lentamente"))
        conn.close()

    def test_comma_delimited_with_quoted_fields_and_decimals(self):
        """CSV with comma delimiter, quoted decimal numbers, and quoted notes containing commas."""
        csv_path = os.path.join(self.temp_dir, "comma_decimal.csv")
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            f.write('recipe,meals,product,quantity,unit,to_taste,notes\n')
            f.write('Comma Delight,lunch,Rice,"1,5",kg,0,"Note, with a comma, and quotes"\n')

        conn = get_connection(self.db_path)
        created, errors = rc.import_file(conn, csv_path)
        self.assertEqual(created, 1)
        self.assertEqual(errors, [])

        cur = conn.cursor()
        cur.execute("SELECT ri.Quantity, r.Notes FROM RECIPE_INGREDIENTS ri JOIN RECIPES r ON ri.Recipe_ID = r.Recipe_ID WHERE r.Name = 'Comma Delight'")
        row = cur.fetchone()
        self.assertEqual(row, (1.5, "Note, with a comma, and quotes"))
        conn.close()

    def test_to_taste_truthy_and_falsy_spectrum(self):
        """Test to_taste column handling various truthy flags: 1, yes, si, sì, true, x, and falsy flags: 0, no, false, empty."""
        truthy_flags = ["1", "yes", "YES", "si", "SI", "sì", "SÌ", "true", "TRUE", "x", "X"]
        falsy_flags = ["0", "no", "NO", "false", "FALSE", ""]

        csv_path = os.path.join(self.temp_dir, "taste_flags.csv")
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            f.write("recipe;meals;product;quantity;unit;to_taste\n")
            for idx, flag in enumerate(truthy_flags):
                f.write(f"Taste Recipe True {idx};lunch;Rice;50;g;{flag}\n")
            for idx, flag in enumerate(falsy_flags):
                f.write(f"Taste Recipe False {idx};lunch;Rice;50;g;{flag}\n")

        conn = get_connection(self.db_path)
        created, errors = rc.import_file(conn, csv_path)
        self.assertEqual(created, len(truthy_flags) + len(falsy_flags))
        self.assertEqual(errors, [])

        cur = conn.cursor()
        for idx, flag in enumerate(truthy_flags):
            cur.execute("""
                SELECT ri.To_Taste
                FROM RECIPE_INGREDIENTS ri
                JOIN RECIPES r ON ri.Recipe_ID = r.Recipe_ID
                WHERE r.Name = ?
            """, (f"Taste Recipe True {idx}",))
            val = cur.fetchone()[0]
            self.assertEqual(val, 1, f"Flag '{flag}' was not parsed as truthy 1")

        for idx, flag in enumerate(falsy_flags):
            cur.execute("""
                SELECT ri.To_Taste
                FROM RECIPE_INGREDIENTS ri
                JOIN RECIPES r ON ri.Recipe_ID = r.Recipe_ID
                WHERE r.Name = ?
            """, (f"Taste Recipe False {idx}",))
            val = cur.fetchone()[0]
            self.assertEqual(val, 0, f"Flag '{flag}' was not parsed as falsy 0")
        conn.close()

    def test_csv_notes_unicode_emojis_and_symbols(self):
        """Test CSV notes with unicode accents, emojis, and punctuation."""
        csv_path = os.path.join(self.temp_dir, "notes_unicode.csv")
        notes_text = 'Bollire il riso con zafferano & purée. 🍕 Delizioso! 👨‍🍳 ❤️'
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            f.write('recipe;meals;product;quantity;unit;to_taste;notes\n')
            f.write(f'Risotto Esotico;dinner;Rice;90;g;0;"{notes_text}"\n')

        conn = get_connection(self.db_path)
        created, errors = rc.import_file(conn, csv_path)
        self.assertEqual(created, 1)
        self.assertEqual(errors, [])

        cur = conn.cursor()
        cur.execute("SELECT Notes FROM RECIPES WHERE Name = 'Risotto Esotico'")
        self.assertEqual(cur.fetchone()[0], notes_text)
        conn.close()

    def test_atomic_skip_mixed_valid_and_invalid_recipes(self):
        """Test atomic skip: invalid recipes are rejected without partial writes, valid recipes are created."""
        csv_path = os.path.join(self.temp_dir, "mixed_batch.csv")
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            f.write("recipe;meals;product;quantity;unit;to_taste\n")
            # 1. Valid recipe
            f.write("Valid Alpha;lunch;Rice;100;g;0\n")
            # 2. Invalid: unknown ingredient
            f.write("Invalid Beta;lunch;MartianKryptonite;100;g;0\n")
            # 3. Valid recipe
            f.write("Valid Gamma;dinner;Rice;75;g;0\n")
            # 4. Invalid: negative quantity
            f.write("Invalid Delta;lunch;Rice;-50;g;0\n")
            # 5. Invalid: unknown meal
            f.write("Invalid Epsilon;space_meal;Rice;100;g;0\n")
            # 6. Valid recipe
            f.write("Valid Zeta;breakfast;Rice;60;g;0\n")

        conn = get_connection(self.db_path)
        created, errors = rc.import_file(conn, csv_path)
        self.assertEqual(created, 3)
        self.assertEqual(len(errors), 3)

        cur = conn.cursor()
        # Verify valid recipes exist
        for name in ("Valid Alpha", "Valid Gamma", "Valid Zeta"):
            cur.execute("SELECT 1 FROM RECIPES WHERE Name = ?", (name,))
            self.assertIsNotNone(cur.fetchone(), f"Valid recipe '{name}' was not created")

        # Verify invalid recipes do not exist
        for name in ("Invalid Beta", "Invalid Delta", "Invalid Epsilon"):
            cur.execute("SELECT 1 FROM RECIPES WHERE Name = ?", (name,))
            self.assertIsNone(cur.fetchone(), f"Invalid recipe '{name}' was unexpectedly created")

        # Verify no orphan recipe meals or recipe ingredients exist
        cur.execute("""
            SELECT COUNT(*) FROM RECIPE_MEALS WHERE Recipe_ID NOT IN (SELECT Recipe_ID FROM RECIPES)
        """)
        self.assertEqual(cur.fetchone()[0], 0)
        cur.execute("""
            SELECT COUNT(*) FROM RECIPE_INGREDIENTS WHERE Recipe_ID NOT IN (SELECT Recipe_ID FROM RECIPES)
        """)
        self.assertEqual(cur.fetchone()[0], 0)
        conn.close()

    def test_legacy_csv_without_notes_column(self):
        """Verify backwards compatibility with 6-column legacy CSV files lacking 'notes'."""
        csv_path = os.path.join(self.temp_dir, "legacy.csv")
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            f.write("recipe;meals;product;quantity;unit;to_taste\n")
            f.write("Legacy Dish;lunch;Rice;100;g;0\n")

        conn = get_connection(self.db_path)
        created, errors = rc.import_file(conn, csv_path)
        self.assertEqual(created, 1)
        self.assertEqual(errors, [])

        cur = conn.cursor()
        cur.execute("SELECT Notes FROM RECIPES WHERE Name = 'Legacy Dish'")
        self.assertIsNone(cur.fetchone()[0])
        conn.close()

    def test_csv_notes_multiline_splitlines_behavior(self):
        """Empirically verify the legacy CSV reader behavior with quoted multiline strings:
        Due to text.splitlines() in _from_csv, newlines inside quoted CSV fields are stripped."""
        csv_path = os.path.join(self.temp_dir, "multiline_quoted.csv")
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            f.write('recipe;meals;product;quantity;unit;to_taste;notes\n')
            f.write('Multiline Test;lunch;Rice;100;g;0;"Line 1\nLine 2"\n')

        conn = get_connection(self.db_path)
        created, errors = rc.import_file(conn, csv_path)
        self.assertEqual(created, 1)
        self.assertEqual(errors, [])

        cur = conn.cursor()
        cur.execute("SELECT Notes FROM RECIPES WHERE Name = 'Multiline Test'")
        saved_note = cur.fetchone()[0]
        # Documents that splitlines() without keepends=True concatenates without newline
        self.assertEqual(saved_note, "Line 1Line 2")
        conn.close()


class TestOnTheFlyProductCreationAdversarial(BaseAdversarialTestCase):
    """Stress tests for on-the-fly product creation and QuickProductDialog."""

    def test_zero_macros_product_registration(self):
        """Zero values for Kcal, Prot, Carb, Fat are valid (e.g. Water / Salt)."""
        conn = get_connection(self.db_path)
        pid = rc.register_product_on_the_fly(
            conn,
            name="Acqua Naturale Oligominerale",
            unit="L",
            macros={"Kcal_100g": 0.0, "Protein_100g": 0.0, "Carbs_100g": 0.0, "Fat_100g": 0.0},
        )
        self.assertIsInstance(pid, int)
        cur = conn.cursor()
        cur.execute("SELECT Unit, Kcal_100g, Protein_100g FROM PRODUCTS WHERE ID = ?", (pid,))
        self.assertEqual(cur.fetchone(), ("L", 0.0, 0.0))
        conn.close()

    def test_quick_product_dialog_validation_guardrails(self):
        """QuickProductDialog rejects empty name, negative macros, and non-numeric values."""
        # 1. Empty name
        dlg = QuickProductDialog(self.root, name="", wait=False, db_path=self.db_path)
        self.assertFalse(dlg.save())

        # 2. Negative macros
        dlg2 = QuickProductDialog(self.root, name="Negative Food", wait=False, db_path=self.db_path)
        dlg2.v_kcal.set("-50")
        dlg2.v_prot.set("10")
        dlg2.v_carb.set("5")
        dlg2.v_fat.set("2")
        self.assertFalse(dlg2.save())

        # 3. Non-numeric macros
        dlg3 = QuickProductDialog(self.root, name="NaN Food", wait=False, db_path=self.db_path)
        dlg3.v_kcal.set("abc")
        self.assertFalse(dlg3.save())

        # 4. Negative fiber
        dlg4 = QuickProductDialog(self.root, name="Bad Fiber", wait=False, db_path=self.db_path)
        dlg4.v_kcal.set("100")
        dlg4.v_prot.set("5")
        dlg4.v_carb.set("10")
        dlg4.v_fat.set("2")
        dlg4.v_fiber.set("-3")
        self.assertFalse(dlg4.save())

    def test_unknown_product_cancellation_in_recipe_dialog(self):
        """If on-the-fly product dialog is cancelled, ingredient is NOT added to recipe."""
        dlg = AddRecipeDialog(
            self.root,
            db_path=self.db_path,
            wait=False,
            on_unknown_product_callback=lambda name, unit: None,  # Simulates user clicking Cancel
        )
        dlg.v_ingredient_name.set("Ingredient Annullato")
        dlg.v_quantity.set("100")
        self.assertFalse(dlg.add_ingredient())
        self.assertEqual(len(dlg.ingredients), 0)


class TestProductionDatabaseInvariant(unittest.TestCase):
    """Verify production database invariant on disk."""

    def test_production_db_row_counts(self):
        db_path = "data/inventory.db"
        self.assertTrue(os.path.exists(db_path), "Production data/inventory.db file does not exist")
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('PRODUCTS', 'ALIASES', 'INVENTORY', 'RECIPES') ORDER BY name")
        # Query exact counts
        counts = [
            ("PRODUCTS", cur.execute("SELECT COUNT(*) FROM PRODUCTS").fetchone()[0]),
            ("ALIASES", cur.execute("SELECT COUNT(*) FROM ALIASES").fetchone()[0]),
            ("INVENTORY", cur.execute("SELECT COUNT(*) FROM INVENTORY").fetchone()[0]),
            ("RECIPES", cur.execute("SELECT COUNT(*) FROM RECIPES").fetchone()[0]),
        ]
        conn.close()
        expected = [('PRODUCTS', 14), ('ALIASES', 30), ('INVENTORY', 36), ('RECIPES', 3)]
        self.assertEqual(counts, expected, f"Production database counts mismatch: {counts} vs expected {expected}")




# ======================================================================
# SECTION: tests/test_challenger_m3_catalog.py
# ======================================================================

"""
tests/test_challenger_m3_catalog.py - Adversarial and empirical stress test suite for M3:
Catalog & Product Creation (`register_product_on_the_fly` and `is_ingredient_known`).

Tests:
1. Unit Canonicalization & Boundaries:
   - Valid units ('g', 'gr', 'kg', 'ml', 'cl', 'L', 'pz', 'pcs' etc.)
   - Invalid units ('ounces', 'cups', '', None, invalid types)
2. Macro Validations & Non-Finite Floats:
   - Required macros presence and typing
   - Non-finite floats: inf, -inf, nan, "inf", "-inf", "nan"
   - Negative values and booleans
   - Non-numeric strings and commas
   - Conversion factors (grams_per_piece, density_kg_per_l)
3. Collisions & Idempotency:
   - Case-insensitive duplicate alias idempotency
   - Substitute group name collision rejection
   - Whitespace and empty name rejection
   - is_ingredient_known resolution
4. Atomic Rollback on Failure:
   - Failure during alias insertion leaves no orphan PRODUCTS or dirty transaction
5. Integration with Recipe Creation and Macros:
   - Newly created products used in create_recipe
   - Accurate recipe_macros calculation across g, ml, pcs
   - Recipe deletion guards
"""

import contextlib
import io
import math
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from meal_planner import engine as m
from meal_planner import recipes as rc
from meal_planner.db.connection import get_connection
from meal_planner.db.schema import migrate
from scripts.seed_inventory import seed


class BaseChallengerM3TestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "challenger_m3.db")
        with contextlib.redirect_stdout(io.StringIO()):
            seed(self.db_path, False)
        conn = get_connection(self.db_path)
        migrate(conn)
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _std_macros(self):
        return {
            "Kcal_100g": 120.0,
            "Protein_100g": 10.0,
            "Carbs_100g": 15.0,
            "Fat_100g": 2.5,
        }


class TestChallengerUnitCanonicalization(BaseChallengerM3TestCase):
    """Stress tests for unit canonicalization and boundary values."""

    def test_canonical_mass_units(self):
        conn = get_connection(self.db_path)
        valid_mass = [
            ("g", "kg"),
            ("gr", "kg"),
            ("gram", "kg"),
            ("grams", "kg"),
            ("kg", "kg"),
            ("kgs", "kg"),
            ("  G  ", "kg"),
            ("KG", "kg"),
            ("gr.", "kg"),
        ]
        for idx, (raw_u, expected) in enumerate(valid_mass):
            pname = f"Test Mass Product {idx}"
            pid = rc.register_product_on_the_fly(conn, name=pname, unit=raw_u, macros=self._std_macros())
            cur = conn.cursor()
            cur.execute("SELECT Unit FROM PRODUCTS WHERE ID = ?", (pid,))
            self.assertEqual(cur.fetchone()[0], expected, f"Failed canonicalization for '{raw_u}'")
        conn.close()

    def test_canonical_volume_units(self):
        conn = get_connection(self.db_path)
        valid_volume = [
            ("l", "L"),
            ("lt", "L"),
            ("ml", "L"),
            ("cl", "L"),
            ("liter", "L"),
            ("liters", "L"),
            ("  ML  ", "L"),
            ("L", "L"),
        ]
        for idx, (raw_u, expected) in enumerate(valid_volume):
            pname = f"Test Vol Product {idx}"
            pid = rc.register_product_on_the_fly(conn, name=pname, unit=raw_u, macros=self._std_macros())
            cur = conn.cursor()
            cur.execute("SELECT Unit, Density_kg_per_L FROM PRODUCTS WHERE ID = ?", (pid,))
            unit_val, density_val = cur.fetchone()
            self.assertEqual(unit_val, expected, f"Failed canonicalization for '{raw_u}'")
            self.assertEqual(density_val, 1.0, f"Expected default density 1.0 for volume '{raw_u}'")
        conn.close()

    def test_canonical_count_units(self):
        conn = get_connection(self.db_path)
        valid_count = [
            ("pcs", "pcs"),
            ("pz", "pcs"),
            ("pc", "pcs"),
            ("piece", "pcs"),
            ("pieces", "pcs"),
            ("  PZ  ", "pcs"),
            ("PCS", "pcs"),
        ]
        for idx, (raw_u, expected) in enumerate(valid_count):
            pname = f"Test Count Product {idx}"
            pid = rc.register_product_on_the_fly(
                conn, name=pname, unit=raw_u, macros=self._std_macros(), grams_per_piece=45.0
            )
            cur = conn.cursor()
            cur.execute("SELECT Unit, Grams_per_piece FROM PRODUCTS WHERE ID = ?", (pid,))
            unit_val, g_piece = cur.fetchone()
            self.assertEqual(unit_val, expected, f"Failed canonicalization for '{raw_u}'")
            self.assertEqual(g_piece, 45.0)
        conn.close()

    def test_invalid_units_rejected(self):
        conn = get_connection(self.db_path)
        invalid_units = [
            "ounces",
            "oz",
            "cup",
            "cups",
            "lb",
            "lbs",
            "stone",
            "tablespoon",
            "tbsp",
            "tsp",
            "pinch",
            "qb",
            "",
            "   ",
            None,
        ]
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM PRODUCTS")
        initial_prod_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM ALIASES")
        initial_alias_count = cur.fetchone()[0]

        for u in invalid_units:
            with self.subTest(unit=u):
                with self.assertRaises(ValueError):
                    rc.register_product_on_the_fly(conn, name=f"Bad Unit {u}", unit=u, macros=self._std_macros())

        # Verify no orphan or partial inserts
        cur.execute("SELECT COUNT(*) FROM PRODUCTS")
        self.assertEqual(cur.fetchone()[0], initial_prod_count)
        cur.execute("SELECT COUNT(*) FROM ALIASES")
        self.assertEqual(cur.fetchone()[0], initial_alias_count)
        conn.close()


class TestChallengerMacrosAndFloats(BaseChallengerM3TestCase):
    """Stress tests for macro validations, non-finite floats, booleans, and factors."""

    def test_missing_and_invalid_macro_containers(self):
        conn = get_connection(self.db_path)
        invalid_containers = [None, "macros", 123, [100, 10, 20, 5], set()]
        for c in invalid_containers:
            with self.subTest(container=c):
                with self.assertRaises(ValueError):
                    rc.register_product_on_the_fly(conn, name="Test Container", unit="kg", macros=c)

        # Missing required keys
        incomplete_macros = [
            {},
            {"Kcal_100g": 100},
            {"Kcal_100g": 100, "Protein_100g": 10},
            {"Kcal_100g": 100, "Protein_100g": 10, "Carbs_100g": 20},
            {"Kcal_100g": None, "Protein_100g": 10, "Carbs_100g": 20, "Fat_100g": 5},
        ]
        for m_dict in incomplete_macros:
            with self.subTest(m_dict=m_dict):
                with self.assertRaises(ValueError):
                    rc.register_product_on_the_fly(conn, name="Incomplete Macro", unit="kg", macros=m_dict)
        conn.close()

    def test_non_finite_floats_in_required_macros(self):
        conn = get_connection(self.db_path)
        bad_values = [
            float("inf"),
            float("-inf"),
            float("nan"),
            "inf",
            "-inf",
            "nan",
            "Infinity",
            "-Infinity",
            "NaN",
            "1e999",
            "-1e999",
        ]
        required_fields = ["Kcal_100g", "Protein_100g", "Carbs_100g", "Fat_100g"]
        for field in required_fields:
            for val in bad_values:
                with self.subTest(field=field, val=val):
                    macros = self._std_macros()
                    macros[field] = val
                    with self.assertRaises(ValueError):
                        rc.register_product_on_the_fly(conn, name=f"Bad Float {field}", unit="kg", macros=macros)
        conn.close()

    def test_non_finite_floats_in_optional_macros(self):
        conn = get_connection(self.db_path)
        bad_values = [float("inf"), float("-inf"), float("nan"), "inf", "-inf", "nan"]
        optional_fields = ["Fiber_100g", "Sugars_100g", "Saturated_fat_100g", "Salt_100g"]
        for field in optional_fields:
            for val in bad_values:
                with self.subTest(field=field, val=val):
                    macros = self._std_macros()
                    macros[field] = val
                    with self.assertRaises(ValueError):
                        rc.register_product_on_the_fly(conn, name=f"Bad Opt Float {field}", unit="kg", macros=macros)
        conn.close()

    def test_negative_values_and_booleans_rejected(self):
        conn = get_connection(self.db_path)
        all_macro_fields = [
            "Kcal_100g", "Protein_100g", "Carbs_100g", "Fat_100g",
            "Fiber_100g", "Sugars_100g", "Saturated_fat_100g", "Salt_100g"
        ]
        for field in all_macro_fields:
            for bad_val in [-0.001, -1.0, -100, True, False, "not_a_number"]:
                with self.subTest(field=field, bad_val=bad_val):
                    macros = self._std_macros()
                    macros[field] = bad_val
                    with self.assertRaises(ValueError):
                        rc.register_product_on_the_fly(conn, name=f"Neg/Bool {field}", unit="kg", macros=macros)
        conn.close()

    def test_comma_decimal_parsing(self):
        conn = get_connection(self.db_path)
        macros = {
            "Kcal_100g": "150,5",
            "Protein_100g": "12,2",
            "Carbs_100g": "20,8",
            "Fat_100g": "3,4",
            "Fiber_100g": "5,1",
        }
        pid = rc.register_product_on_the_fly(conn, name="Comma Macro Product", unit="kg", macros=macros)
        cur = conn.cursor()
        cur.execute("SELECT Kcal_100g, Protein_100g, Carbs_100g, Fat_100g, Fiber_100g FROM PRODUCTS WHERE ID = ?", (pid,))
        self.assertEqual(cur.fetchone(), (150.5, 12.2, 20.8, 3.4, 5.1))
        conn.close()

    def test_factor_validations(self):
        conn = get_connection(self.db_path)
        # Invalid grams_per_piece and density_kg_per_l
        invalid_factors = [0, -1, -0.5, float("inf"), float("-inf"), float("nan"), True, False, "50", "abc"]
        for factor_val in invalid_factors:
            with self.subTest(grams_per_piece=factor_val):
                with self.assertRaises(ValueError):
                    rc.register_product_on_the_fly(
                        conn, name="Bad Factor G", unit="pcs", macros=self._std_macros(), grams_per_piece=factor_val
                    )
            with self.subTest(density_kg_per_l=factor_val):
                with self.assertRaises(ValueError):
                    rc.register_product_on_the_fly(
                        conn, name="Bad Factor D", unit="L", macros=self._std_macros(), density_kg_per_l=factor_val
                    )
        conn.close()


class TestChallengerCollisionsAndIdempotency(BaseChallengerM3TestCase):
    """Stress tests for duplicate aliases, substitute group collisions, and name hygiene."""

    def test_name_hygiene_and_whitespace(self):
        conn = get_connection(self.db_path)
        bad_names = ["", "   ", "\t\r\n", None, 123, ["Bread"]]
        for name in bad_names:
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    rc.register_product_on_the_fly(conn, name=name, unit="kg", macros=self._std_macros())
        conn.close()

    def test_duplicate_alias_idempotency_and_casing(self):
        conn = get_connection(self.db_path)
        pid1 = rc.register_product_on_the_fly(conn, name="Farina di Farro", unit="kg", macros=self._std_macros())
        self.assertIsInstance(pid1, int)

        # Re-register with exact name
        pid2 = rc.register_product_on_the_fly(conn, name="Farina di Farro", unit="kg", macros=self._std_macros())
        self.assertEqual(pid1, pid2)

        # Re-register with uppercase, lowercase, whitespace padding
        pid3 = rc.register_product_on_the_fly(conn, name="FARINA DI FARRO", unit="kg", macros=self._std_macros())
        self.assertEqual(pid1, pid3)

        pid4 = rc.register_product_on_the_fly(conn, name="  farina di farro  ", unit="kg", macros=self._std_macros())
        self.assertEqual(pid1, pid4)

        # Check only 1 row in ALIASES and PRODUCTS for this item
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM ALIASES WHERE LOWER(Name) = 'farina di farro'")
        self.assertEqual(cur.fetchone()[0], 1)
        cur.execute("SELECT COUNT(*) FROM PRODUCTS WHERE ID = ?", (pid1,))
        self.assertEqual(cur.fetchone()[0], 1)
        conn.close()

    def test_existing_seed_alias_returns_existing_id(self):
        conn = get_connection(self.db_path)
        # "Rice" is seeded with ID 1
        cur = conn.cursor()
        cur.execute("SELECT ID FROM ALIASES WHERE Name = 'Rice' COLLATE NOCASE")
        seed_rice_id = cur.fetchone()[0]

        pid = rc.register_product_on_the_fly(conn, name="Rice", unit="kg", macros=self._std_macros())
        self.assertEqual(pid, seed_rice_id)

        pid_lower = rc.register_product_on_the_fly(conn, name="  rice  ", unit="kg", macros=self._std_macros())
        self.assertEqual(pid_lower, seed_rice_id)
        conn.close()

    def test_substitute_group_collision_rejection(self):
        conn = get_connection(self.db_path)
        cur = conn.cursor()
        cur.execute("INSERT INTO SUBSTITUTE_GROUPS (Name) VALUES ('Super Grains')")
        conn.commit()

        # Reject exact, lower, upper, and padded
        for variant in ["Super Grains", "super grains", "SUPER GRAINS", "  Super Grains  "]:
            with self.subTest(variant=variant):
                with self.assertRaises(ValueError) as ctx:
                    rc.register_product_on_the_fly(conn, name=variant, unit="kg", macros=self._std_macros())
                self.assertIn("conflicts with an existing substitute group", str(ctx.exception))
        conn.close()

    def test_is_ingredient_known(self):
        conn = get_connection(self.db_path)
        cur = conn.cursor()
        cur.execute("INSERT INTO SUBSTITUTE_GROUPS (Name) VALUES ('Mock Group')")
        conn.commit()

        # Known from seed
        self.assertTrue(rc.is_ingredient_known(conn, "Rice"))
        self.assertTrue(rc.is_ingredient_known(conn, "rice"))
        self.assertTrue(rc.is_ingredient_known(conn, "  RICE  "))

        # Known from substitute groups
        self.assertTrue(rc.is_ingredient_known(conn, "Mock Group"))
        self.assertTrue(rc.is_ingredient_known(conn, "mock group"))

        # Newly registered product
        self.assertFalse(rc.is_ingredient_known(conn, "Adversarial Mushroom"))
        rc.register_product_on_the_fly(conn, name="Adversarial Mushroom", unit="kg", macros=self._std_macros())
        self.assertTrue(rc.is_ingredient_known(conn, "Adversarial Mushroom"))
        self.assertTrue(rc.is_ingredient_known(conn, "adversarial mushroom"))

        # Unknowns, empties, non-strings
        self.assertFalse(rc.is_ingredient_known(conn, "Completely Unknown Ingredient 999"))
        self.assertFalse(rc.is_ingredient_known(conn, ""))
        self.assertFalse(rc.is_ingredient_known(conn, "   "))
        self.assertFalse(rc.is_ingredient_known(conn, None))
        self.assertFalse(rc.is_ingredient_known(conn, 12345))
        conn.close()


class TestChallengerAtomicRollback(BaseChallengerM3TestCase):
    """Stress tests ensuring atomic transactions and clean rollbacks on failure."""

    def test_atomic_rollback_on_alias_insertion_failure(self):
        conn = get_connection(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM PRODUCTS")
        initial_prod_count = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM ALIASES")
        initial_alias_count = cur.fetchone()[0]

        # Create a trigger that aborts INSERT into ALIASES for 'Crash Product'
        cur.execute("""
            CREATE TRIGGER fail_alias_insert BEFORE INSERT ON ALIASES
            FOR EACH ROW WHEN NEW.Name = 'Crash Product'
            BEGIN
                SELECT RAISE(ABORT, 'Simulated alias insert failure');
            END;
        """)
        conn.commit()

        # Calling register_product_on_the_fly should fail when inserting alias and rollback
        with self.assertRaises(sqlite3.DatabaseError):
            rc.register_product_on_the_fly(
                conn, name="Crash Product", unit="kg", macros=self._std_macros()
            )

        # Drop trigger
        cur.execute("DROP TRIGGER fail_alias_insert")
        conn.commit()

        # Verify rollback: PRODUCTS and ALIASES must have initial counts, no orphan row
        cur.execute("SELECT COUNT(*) FROM PRODUCTS")
        self.assertEqual(cur.fetchone()[0], initial_prod_count)
        cur.execute("SELECT COUNT(*) FROM ALIASES")
        self.assertEqual(cur.fetchone()[0], initial_alias_count)
        conn.close()

    def test_unmanaged_connection_db_path_mode(self):
        # Test calling register_product_on_the_fly with conn=None, db_path=self.db_path
        pid = rc.register_product_on_the_fly(
            conn=None,
            db_path=self.db_path,
            name="Unmanaged Product",
            unit="kg",
            macros=self._std_macros(),
        )
        self.assertIsInstance(pid, int)
        conn = get_connection(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT ID FROM ALIASES WHERE Name = 'Unmanaged Product'")
        self.assertEqual(cur.fetchone()[0], pid)
        conn.close()


class TestChallengerRecipeIntegration(BaseChallengerM3TestCase):
    """Stress tests verifying end-to-end integration with create_recipe and recipe_macros."""

    def test_nutritional_calculation_with_new_products(self):
        conn = get_connection(self.db_path)

        # 1. Solid product in kg
        # 100g: 400 kcal, 30g prot, 40g carb, 10g fat, 10g fiber
        pid_solid = rc.register_product_on_the_fly(
            conn,
            name="Adv Solid Protein Bar",
            unit="kg",
            macros={
                "Kcal_100g": 400.0,
                "Protein_100g": 30.0,
                "Carbs_100g": 40.0,
                "Fat_100g": 10.0,
                "Fiber_100g": 10.0,
                "Sugars_100g": 5.0,
                "Saturated_fat_100g": 2.0,
                "Salt_100g": 0.5,
            },
        )

        # 2. Liquid product in L with density = 1.05 kg/L
        # 100g: 50 kcal, 2.0g prot, 6.0g carb, 2.0g fat
        pid_liquid = rc.register_product_on_the_fly(
            conn,
            name="Adv Liquid Almond Milk",
            unit="L",
            density_kg_per_l=1.05,
            macros={
                "Kcal_100g": 50.0,
                "Protein_100g": 2.0,
                "Carbs_100g": 6.0,
                "Fat_100g": 2.0,
                "Fiber_100g": 1.0,
                "Sugars_100g": 4.0,
                "Saturated_fat_100g": 0.5,
                "Salt_100g": 0.1,
            },
        )

        # 3. Piece product in pcs with grams_per_piece = 60.0g
        # 100g: 150 kcal, 13g prot, 1g carb, 10g fat
        pid_piece = rc.register_product_on_the_fly(
            conn,
            name="Adv Organic Egg",
            unit="pcs",
            grams_per_piece=60.0,
            macros={
                "Kcal_100g": 150.0,
                "Protein_100g": 13.0,
                "Carbs_100g": 1.0,
                "Fat_100g": 10.0,
                "Fiber_100g": 0.0,
                "Sugars_100g": 0.5,
                "Saturated_fat_100g": 3.0,
                "Salt_100g": 0.3,
            },
        )

        # Create Recipe combining all three:
        # - 50g Protein Bar: grams = 50.0
        # - 200ml Almond Milk: grams = 200 * 1.05 = 210.0
        # - 2 pcs Egg: grams = 2 * 60.0 = 120.0
        recipe_name = "Adv Ultimate Fitness Breakfast"
        res = rc.create_recipe(
            conn,
            name=recipe_name,
            meals=["breakfast"],
            ingredients=[
                ("Adv Solid Protein Bar", 50, "g", 0),
                ("Adv Liquid Almond Milk", 200, "ml", 0),
                ("Adv Organic Egg", 2, "pcs", 0),
            ],
            notes="Whisk eggs and milk, eat bar on side.",
        )
        self.assertIn("Recipe_ID", res)
        rid = res["Recipe_ID"]

        # Expected nutritional sums:
        # Bar (50g):
        #   Kcal = 50/100 * 400 = 200.0
        #   Prot = 50/100 * 30  = 15.0
        #   Carb = 50/100 * 40  = 20.0
        #   Fat  = 50/100 * 10  = 5.0
        #   Fiber= 50/100 * 10  = 5.0
        # Milk (210g):
        #   Kcal = 210/100 * 50 = 105.0
        #   Prot = 210/100 * 2.0 = 4.2
        #   Carb = 210/100 * 6.0 = 12.6
        #   Fat  = 210/100 * 2.0 = 4.2
        #   Fiber= 210/100 * 1.0 = 2.1
        # Egg (120g):
        #   Kcal = 120/100 * 150 = 180.0
        #   Prot = 120/100 * 13  = 15.6
        #   Carb = 120/100 * 1   = 1.2
        #   Fat  = 120/100 * 10  = 12.0
        #   Fiber= 120/100 * 0   = 0.0
        # Totals:
        #   Kcal = 200 + 105 + 180 = 485.0
        #   Prot = 15 + 4.2 + 15.6 = 34.8
        #   Carb = 20 + 12.6 + 1.2 = 33.8
        #   Fat  = 5 + 4.2 + 12.0  = 21.2
        #   Fiber= 5 + 2.1 + 0     = 7.1

        calc = m.recipe_macros(conn, rid)
        self.assertFalse(calc["Incomplete_Data"])
        self.assertAlmostEqual(calc["Kcal"], 485.0, places=3)
        self.assertAlmostEqual(calc["Protein"], 34.8, places=3)
        self.assertAlmostEqual(calc["Carbs"], 33.8, places=3)
        self.assertAlmostEqual(calc["Fat"], 21.2, places=3)
        self.assertAlmostEqual(calc["Fiber"], 7.1, places=3)

        # Servings scaling: 2 servings
        calc2 = m.recipe_macros(conn, rid, servings=2)
        self.assertAlmostEqual(calc2["Kcal"], 970.0, places=3)
        self.assertAlmostEqual(calc2["Protein"], 69.6, places=3)
        self.assertAlmostEqual(calc2["Carbs"], 67.6, places=3)
        self.assertAlmostEqual(calc2["Fat"], 42.4, places=3)

        # Delete recipe
        rc.delete_recipe(conn, recipe_name)
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM RECIPES WHERE Recipe_ID = ?", (rid,))
        self.assertIsNone(cur.fetchone())
        conn.close()

    def test_recipe_creation_rejection_for_unregistered_ingredient(self):
        conn = get_connection(self.db_path)
        with self.assertRaises(ValueError) as ctx:
            rc.create_recipe(
                conn,
                name="Phantom Recipe",
                meals=["lunch"],
                ingredients=[("Totally Nonexistent Ingredient 999", 100, "g")],
            )
        self.assertIn("unknown ingredient 'Totally Nonexistent Ingredient 999'", str(ctx.exception))
        conn.close()


class TestRecipeManagementAdvancedEdgeCases(unittest.TestCase):
    """
    Advanced edge-case testing for Recipe Management (Milestone M5):
    - Extreme servings scaling (e.g. 1000 servings, invalid inputs: 0, -1, NaN, inf, text)
    - create_recipe rollback/validation atomicity (unknown ingredient, empty name/meals, duplicate name)
    - To-taste flag & multiline notes unicode persistence (emojis, accents, newlines)
    - register_product_on_the_fly adversarial validation (non-finite floats, booleans, empty name, units)
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "recipes_edge_test.db")
        self.orig_db = os.environ.get("MEAL_PLANNER_DB")
        os.environ["MEAL_PLANNER_DB"] = self.db_path
        conn = get_connection(self.db_path)
        from meal_planner.db.schema import create_schema
        create_schema(conn)
        conn.close()

    def tearDown(self):
        if self.orig_db is not None:
            os.environ["MEAL_PLANNER_DB"] = self.orig_db
        else:
            os.environ.pop("MEAL_PLANNER_DB", None)
        self.tmp.cleanup()

    def test_recipe_servings_scaling_extreme_values_and_formatting(self):
        conn = get_connection(self.db_path)
        macros = {"Kcal_100g": 350.0, "Protein_100g": 12.0, "Carbs_100g": 72.0, "Fat_100g": 1.5}
        rc.register_product_on_the_fly(conn, "Riso Carnaroli", "g", macros)
        conn.close()

        import tkinter as tk
        from meal_planner.gui.dialogs import AddRecipeDialog

        root = tk.Tk()
        root.withdraw()
        try:
            # 1. Test extreme servings = 1000 scaling down (100g / 1000 = 0.1g)
            dlg = AddRecipeDialog(root, db_path=self.db_path, wait=False)
            dlg.v_name.set("Maxi Risotto")
            dlg.v_meals["lunch"].set(True)
            dlg.v_servings.set("1000")
            dlg.ingredients.append({"product": "Riso Carnaroli", "quantity": 100.0, "unit": "g", "to_taste": 0})
            with patch("tkinter.messagebox.showerror") as mock_err:
                saved = dlg.save()
                self.assertTrue(saved, "Dialog should save successfully with servings = 1000")
                self.assertFalse(mock_err.called)

            conn = get_connection(self.db_path)
            cur = conn.cursor()
            cur.execute("SELECT Quantity FROM RECIPE_INGREDIENTS WHERE Recipe_ID = ?", (dlg.result["Recipe_ID"],))
            scaled_qty = cur.fetchone()[0]
            self.assertAlmostEqual(scaled_qty, 0.1, places=5)
            conn.close()

            # 2. Test invalid servings inputs (0, -1, 0.0, inf, nan, non-numeric)
            for invalid in ["0", "-1", "0.0", "inf", "nan", "abc"]:
                dlg_bad = AddRecipeDialog(root, db_path=self.db_path, wait=False)
                dlg_bad.v_name.set("Bad Recipe")
                dlg_bad.v_meals["lunch"].set(True)
                dlg_bad.v_servings.set(invalid)
                dlg_bad.ingredients.append({"product": "Riso Carnaroli", "quantity": 100.0, "unit": "g", "to_taste": 0})
                with patch("tkinter.messagebox.showerror") as mock_err:
                    saved = dlg_bad.save()
                    self.assertFalse(saved, f"Dialog must reject invalid servings value: {invalid!r}")
                    self.assertTrue(mock_err.called)
        finally:
            root.destroy()

    def test_create_recipe_validation_and_rollback_atomicity(self):
        conn = get_connection(self.db_path)
        rc.register_product_on_the_fly(conn, "Pasta", "g", {"Kcal_100g": 350, "Protein_100g": 12, "Carbs_100g": 70, "Fat_100g": 2})

        # 1. Unknown ingredient raises ValueError and leaves NO partial records
        with self.assertRaises(ValueError) as ctx:
            rc.create_recipe(conn, "Ghost Recipe", ["lunch"], [("Ghost Ingredient 999", 100, "g")])
        self.assertIn("unknown ingredient", str(ctx.exception))
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM RECIPES WHERE Name = ?", ("Ghost Recipe",))
        self.assertEqual(cur.fetchone()[0], 0, "No recipe row should be committed on validation error")
        cur.execute("SELECT COUNT(*) FROM RECIPE_MEALS WHERE Recipe_ID NOT IN (SELECT Recipe_ID FROM RECIPES)")
        self.assertEqual(cur.fetchone()[0], 0, "No orphan meals rows should exist")

        # 2. Empty name, empty meals, unknown meal
        with self.assertRaises(ValueError):
            rc.create_recipe(conn, "", ["lunch"], [("Pasta", 100, "g")])
        with self.assertRaises(ValueError):
            rc.create_recipe(conn, "No Meal Recipe", [], [("Pasta", 100, "g")])
        with self.assertRaises(ValueError):
            rc.create_recipe(conn, "Bad Meal Recipe", ["second_breakfast"], [("Pasta", 100, "g")])

        # 3. Create valid recipe
        r1 = rc.create_recipe(conn, "Standard Pasta", ["lunch"], [("Pasta", 100, "g")])
        self.assertIsNotNone(r1.get("Recipe_ID"))

        # 4. Duplicate recipe name raises ValueError and rolls back
        with self.assertRaises(ValueError) as ctx2:
            rc.create_recipe(conn, "Standard Pasta", ["dinner"], [("Pasta", 80, "g")])
        self.assertIn("already exists", str(ctx2.exception))
        cur.execute("SELECT COUNT(*) FROM RECIPES WHERE Name = ?", ("Standard Pasta",))
        self.assertEqual(cur.fetchone()[0], 1, "Only one recipe with the unique name should exist")
        conn.close()

    def test_recipe_to_taste_and_multiline_notes_unicode_persistence(self):
        conn = get_connection(self.db_path)
        rc.register_product_on_the_fly(conn, "Basilico", "g", {"Kcal_100g": 23, "Protein_100g": 3.1, "Carbs_100g": 2.6, "Fat_100g": 0.6})
        rc.register_product_on_the_fly(conn, "Spaghetti", "g", {"Kcal_100g": 360, "Protein_100g": 13, "Carbs_100g": 72, "Fat_100g": 1.5})

        notes = "Note speciali:\n- Cottura al dente: 9 minuti\n- Filo di olio EVO e basilico fresco 🌿\n- Servire caldo!"
        outcome = rc.create_recipe(
            conn,
            name="Spaghetti al Basilico",
            meals=["lunch", "dinner"],
            ingredients=[
                ("Spaghetti", 90.0, "g", 0),
                ("Basilico", 5.0, "g", 1),
            ],
            notes=notes
        )
        rid = outcome["Recipe_ID"]
        cur = conn.cursor()
        cur.execute("SELECT Notes FROM RECIPES WHERE Recipe_ID = ?", (rid,))
        saved_notes = cur.fetchone()[0]
        self.assertEqual(saved_notes, notes, "Multiline notes with unicode characters must persist verbatim")

        cur.execute("SELECT To_Taste FROM RECIPE_INGREDIENTS WHERE Recipe_ID = ? AND ID = (SELECT ID FROM ALIASES WHERE Name = ?)", (rid, "Basilico"))
        self.assertEqual(cur.fetchone()[0], 1, "To_Taste flag must be saved as 1")
        conn.close()

    def test_register_product_on_the_fly_adversarial_validation(self):
        conn = get_connection(self.db_path)
        # Empty name
        with self.assertRaises(ValueError):
            rc.register_product_on_the_fly(conn, "", "g", {"Kcal_100g": 100, "Protein_100g": 10, "Carbs_100g": 10, "Fat_100g": 1})

        # Unknown unit
        with self.assertRaises(ValueError):
            rc.register_product_on_the_fly(conn, "Mystery", "bushels", {"Kcal_100g": 100, "Protein_100g": 10, "Carbs_100g": 10, "Fat_100g": 1})

        # Negative macro
        with self.assertRaises(ValueError):
            rc.register_product_on_the_fly(conn, "Anti-Calorie", "g", {"Kcal_100g": -50, "Protein_100g": 10, "Carbs_100g": 10, "Fat_100g": 1})

        # Non-finite macro (NaN)
        with self.assertRaises(ValueError):
            rc.register_product_on_the_fly(conn, "NaN Food", "g", {"Kcal_100g": float("nan"), "Protein_100g": 10, "Carbs_100g": 10, "Fat_100g": 1})

        # Boolean macro
        with self.assertRaises(ValueError):
            rc.register_product_on_the_fly(conn, "Bool Food", "g", {"Kcal_100g": True, "Protein_100g": 10, "Carbs_100g": 10, "Fat_100g": 1})

        # Missing required macros
        with self.assertRaises(ValueError):
            rc.register_product_on_the_fly(conn, "Incomplete Food", "g", {"Kcal_100g": 100})

        # Valid registration returns positive integer ID
        pid = rc.register_product_on_the_fly(conn, "Valid New Item", "kg", {"Kcal_100g": 200, "Protein_100g": 15, "Carbs_100g": 20, "Fat_100g": 5})
        self.assertIsInstance(pid, int)
        self.assertGreater(pid, 0)
        conn.close()

    def test_dialog_lifecycle_centering_and_focus(self):
        """Verifies AddRecipeDialog and RecipeDetailsDialog center, lift, focus, and destroy cleanly without timer errors."""
        conn = get_connection(self.db_path)
        rc.register_product_on_the_fly(conn, "Pollo Test", "g", {"Kcal_100g": 150, "Protein_100g": 30, "Carbs_100g": 0, "Fat_100g": 3})
        rc.create_recipe(conn, "Lifecycle Recipe", ["lunch"], [("Pollo Test", 150, "g")])
        conn.close()

        app = App()
        app.withdraw()
        app.db_path = self.db_path
        app.refresh()

        # AddRecipeDialog lifecycle
        add_dlg = app.open_add_recipe_dialog(wait=False)
        self.assertIsInstance(add_dlg, AddRecipeDialog)
        self.assertTrue(add_dlg.winfo_exists())
        self.assertIn("Recipe", add_dlg.title())
        add_dlg.destroy()
        self.assertFalse(add_dlg.winfo_exists())

        # RecipeDetailsDialog lifecycle
        details_dlg = app.open_recipe_details(recipe_name="Lifecycle Recipe", wait=False)
        self.assertIsInstance(details_dlg, RecipeDetailsDialog)
        self.assertTrue(details_dlg.winfo_exists())
        self.assertIn("Recipe Details", details_dlg.title())
        details_dlg.destroy()
        self.assertFalse(details_dlg.winfo_exists())

        app.destroy()

    def test_recipe_to_taste_qb_clarification_in_dialogs(self):
        """Verifies To taste (q.b.) labels, table headings, and explanations in both dialogs."""
        conn = get_connection(self.db_path)
        rc.register_product_on_the_fly(conn, "Sale Test", "g", {"Kcal_100g": 0, "Protein_100g": 0, "Carbs_100g": 0, "Fat_100g": 0})
        rc.create_recipe(conn, "Q.b. Recipe", ["lunch"], [("Sale Test", 1, "g", 1)])
        conn.close()

        app = App()
        app.withdraw()
        app.db_path = self.db_path
        app.refresh()

        # AddRecipeDialog checks
        add_dlg = app.open_add_recipe_dialog(wait=False)
        chk_text = add_dlg.chk_to_taste.cget("text")
        self.assertTrue("taste" in chk_text.lower() and "q.b." in chk_text.lower())
        col_heading = add_dlg.t_ingredients.heading("to_taste", "text")
        self.assertIn("taste", col_heading.lower())
        add_dlg.destroy()

        # RecipeDetailsDialog checks
        details_dlg = app.open_recipe_details(recipe_name="Q.b. Recipe", wait=False)
        found_note = False
        for widget in details_dlg.winfo_children():
            for child in widget.winfo_children():
                if isinstance(child, ttk.Label):
                    txt = child.cget("text")
                    if "quanto basta" in txt.lower() or "to taste" in txt.lower():
                        found_note = True
                for grand in child.winfo_children():
                    if isinstance(grand, ttk.Label):
                        txt = grand.cget("text")
                        if "quanto basta" in txt.lower() or "to taste" in txt.lower():
                            found_note = True
        self.assertTrue(found_note, "RecipeDetailsDialog must include an explanatory note about 'To taste (q.b.)'")
        details_dlg.destroy()

        app.destroy()

    def test_open_recipe_details_auto_selects_first_row(self):
        """When no row is selected in recipes table, open_recipe_details auto-selects the first row."""
        conn = get_connection(self.db_path)
        rc.register_product_on_the_fly(conn, "Riso Test", "g", {"Kcal_100g": 350, "Protein_100g": 10, "Carbs_100g": 70, "Fat_100g": 1})
        rc.create_recipe(conn, "Auto Select Recipe", ["lunch"], [("Riso Test", 100, "g")])
        conn.close()

        app = App()
        app.withdraw()
        app.db_path = self.db_path
        app.refresh()

        self.assertGreater(len(app.t_ric.tv.get_children()), 0)
        app.t_ric.tv.selection_remove(app.t_ric.tv.selection())
        self.assertEqual(len(app.t_ric.tv.selection()), 0)

        details_dlg = app.open_recipe_details(wait=False)
        self.assertIsNotNone(details_dlg)
        self.assertIsInstance(details_dlg, RecipeDetailsDialog)
        self.assertGreater(len(app.t_ric.tv.selection()), 0, "First recipe row must be selected")
        details_dlg.destroy()

        # Also verify empty table shows info popup
        app.t_ric.tv.delete(*app.t_ric.tv.get_children())
        with patch("tkinter.messagebox.showinfo") as mock_info:
            res = app.open_recipe_details(wait=False)
            self.assertIsNone(res)
            self.assertTrue(mock_info.called)

        app.destroy()

    def test_table_double_click_opens_details(self):
        """Table row double click callback selects the row and opens details dialog."""
        conn = get_connection(self.db_path)
        rc.register_product_on_the_fly(conn, "Pasta Test", "g", {"Kcal_100g": 350, "Protein_100g": 10, "Carbs_100g": 70, "Fat_100g": 1})
        rc.create_recipe(conn, "Double Click Recipe", ["dinner"], [("Pasta Test", 100, "g")])
        conn.close()

        app = App()
        app.deiconify()
        app.db_path = self.db_path
        app.refresh()

        # Switch to recipes tab and update geometry
        app.nb.select(2)
        app.update()

        children = app.t_ric.tv.get_children()
        self.assertGreater(len(children), 0)
        target_row = children[0]
        recipe_name = app.t_ric.tv.item(target_row, "values")[0]

        # 1. Verify <Double-1> binding genuinely executes Table._handle_double_click
        binding = app.t_ric.tv.bind("<Double-1>")
        self.assertIn("_handle_double_click", binding)
        self.assertNotIn("<lambda>", binding)

        # 2. Verify double-clicking heading or empty space does NOT trigger details dialog
        with patch.object(app, "open_recipe_details") as mock_open:
            event_head = type("Event", (), {"x": 10, "y": 10})()
            app.t_ric._handle_double_click(event_head)
            self.assertFalse(mock_open.called, "Header double click should not trigger details dialog")

            event_empty = type("Event", (), {"x": 10, "y": 800})()
            app.t_ric._handle_double_click(event_empty)
            self.assertFalse(mock_open.called, "Empty space double click should not trigger details dialog")

        # 3. Simulate row double-click via _handle_double_click
        bbox = app.t_ric.tv.bbox(target_row)
        ev_x = (bbox[0] + 10) if bbox else 50
        ev_y = (bbox[1] + 10) if bbox else 50

        # Clear selection first
        app.t_ric.tv.selection_remove(app.t_ric.tv.selection())
        self.assertEqual(app.t_ric.tv.selection(), ())

        event_row = type("Event", (), {"x": ev_x, "y": ev_y})()
        with patch.object(app, "open_recipe_details", wraps=app.open_recipe_details) as spy_open:
            app.t_ric._handle_double_click(event_row)
            self.assertTrue(spy_open.called, "open_recipe_details should be invoked for row double click")
            self.assertEqual(spy_open.call_args[1].get("recipe_name"), recipe_name)

        # Verify row is selected
        self.assertEqual(app.t_ric.tv.selection(), (target_row,))

        # Verify RecipeDetailsDialog instance was created
        from meal_planner.gui.dialogs import RecipeDetailsDialog
        dialogs = [w for w in app.winfo_children() if isinstance(w, RecipeDetailsDialog)]
        self.assertGreaterEqual(len(dialogs), 1, "RecipeDetailsDialog must be opened on double click")
        for d in dialogs:
            d.destroy()

        app.destroy()


if __name__ == "__main__":
    unittest.main()
