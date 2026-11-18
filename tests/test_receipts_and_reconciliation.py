"""
tests/test_receipts_and_reconciliation.py

Consolidated receipt ingestion and reconciliation test suite.
"""

import math
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from meal_planner.gui.app import App, HAS_DND


class TestReceiptsTabGUI(unittest.TestCase):
    def setUp(self):
        self.app = App()
    def tearDown(self):
        try:
            self.app.destroy()
        except Exception:
            pass
    def test_receipts_tab_components(self):
        app = self.app
        tab_names = [app.nb.tab(i, 'text') for i in range(app.nb.index('end'))]
        self.assertNotIn('Receipts', tab_names)
        self.assertIn('Pantry', tab_names)
        self.assertTrue(hasattr(app, '_drop_zone'))
        self.assertTrue(hasattr(app, 'lbl_last_receipt'))
        self.assertIn('Last receipt loaded:', app._last_receipt_var.get())
        self.assertTrue(hasattr(app, '_receipt_log'))
        self.assertTrue(hasattr(app, 'btn_act_add'))
        self.assertTrue(hasattr(app, 'btn_act_use'))
        self.assertTrue(hasattr(app, 'btn_act_loc'))
        app.btn_act_use.invoke()
        app.update_idletasks()
        self.assertEqual(app.v_pantry_action.get(), 'Consume')
        app.btn_act_loc.invoke()
        app.update_idletasks()
        self.assertEqual(app.v_pantry_action.get(), 'Locations')
        app.btn_act_add.invoke()
        app.update_idletasks()
        self.assertEqual(app.v_pantry_action.get(), 'Add to inventory')
        self.assertTrue(HAS_DND)
        app._receipt_log_write('Sample log entry' + chr(10))
        self.assertIn('Sample log entry', app._receipt_log.get('1.0', 'end'))
        app.set_theme('light')
        app.set_theme('dark')



# ======================================================================
# SECTION: tests/test_adversarial_receipt_pipeline.py
# ======================================================================

"""Adversarial Test Harness for the Receipt Processing and Ingestion Pipeline.

Fuzzes and stress-tests:
1. valida_voci(voci) in meal_planner.receipts.pipeline
2. convert_receipt_quantity_unit in meal_planner.inventory
3. map_receipt_item_to_add_item_args in meal_planner.inventory
4. Database ingestion of 25+ diverse receipt items into an isolated temporary database
5. Boundary and failure modes of add_item (NaN, Inf, fractional package counts)
"""

import math
import os
import sqlite3
import tempfile
import unittest

from meal_planner.db.connection import get_connection
from meal_planner.db.schema import create_schema
from meal_planner.inventory import (
    AliasConflict,
    InvalidInput,
    add_item,
    convert_receipt_quantity_unit,
    get_available_inventory,
    map_receipt_item_to_add_item_args,
)
from meal_planner.receipts import Voce, valida_voci


class TestAdversarialValidaVoci(unittest.TestCase):
    """Stress tests and boundary checks for valida_voci."""

    def test_valida_voci_empty_and_none_handling(self):
        # Empty list -> returns empty items and empty errors
        items, errs = valida_voci([])
        self.assertEqual(items, [])
        self.assertEqual(errs, [])

        # Calling valida_voci(None) -> gracefully returns empty items and empty errors
        items_none, errs_none = valida_voci(None)
        self.assertEqual(items_none, [])
        self.assertEqual(errs_none, [])

        # Sequence containing None element -> handled gracefully without exception
        items_seq, errs_seq = valida_voci([None])
        self.assertEqual(items_seq, [])
        self.assertEqual(errs_seq, [])

    def test_valida_voci_name_variations_and_sanitization(self):
        # Empty and whitespace names are ignored
        items, errs = valida_voci([
            Voce("", "1", "kg", "1"),
            Voce("   ", "1", "kg", "1"),
            Voce("\t\n", "1", "kg", "1"),
            Voce(";;;", "1", "kg", "1"),
        ])
        self.assertEqual(len(items), 0)
        self.assertEqual(len(errs), 0)

        # Semicolons inside names are sanitized to spaces and whitespace collapsed
        items, errs = valida_voci([
            Voce("Pasta; Barilla;  Penne ", "500", "g", "2"),
            Voce("Olio;d'Oliva", "1", "L", "1"),
            Voce("Caffè   Macinato  100% ", "250", "g", "1"),
        ])
        self.assertEqual(len(errs), 0)
        self.assertEqual(items[0], ("pasta barilla penne", 500.0, "g", 2))
        self.assertEqual(items[1], ("olio d'oliva", 1.0, "l", 1))
        self.assertEqual(items[2], ("caffè macinato 100%", 250.0, "g", 1))

    def test_valida_voci_quantity_adversarial_fuzzing(self):
        # None quantity
        items, errs = valida_voci([Voce("pane", None, "kg", "1")])
        self.assertEqual(len(items), 0)
        self.assertIn("invalid quantity", errs[0])

        # Empty string quantity
        items, errs = valida_voci([Voce("pane", "", "kg", "1")])
        self.assertEqual(len(items), 0)
        self.assertIn("invalid quantity", errs[0])

        # Zero quantity
        items, errs = valida_voci([Voce("pane", "0", "kg", "1"), Voce("riso", "0.0", "kg", "1")])
        self.assertEqual(len(items), 0)
        self.assertEqual(len(errs), 2)
        self.assertIn("invalid quantity", errs[0])

        # Negative quantity
        items, errs = valida_voci([Voce("pane", "-1", "kg", "1"), Voce("riso", "-0.001", "kg", "1")])
        self.assertEqual(len(items), 0)
        self.assertEqual(len(errs), 2)

        # Non-numeric string
        items, errs = valida_voci([Voce("pane", "abc", "kg", "1")])
        self.assertEqual(len(items), 0)
        self.assertIn("invalid quantity", errs[0])

        # Multiple dots or commas
        items, errs = valida_voci([
            Voce("pane", "12.5.0", "kg", "1"),
            Voce("latte", "12,5,0", "l", "1"),
            Voce("olio", "1..5", "l", "1"),
        ])
        self.assertEqual(len(items), 0)
        self.assertEqual(len(errs), 3)

        # Italian comma is accepted and converted to dot
        items, errs = valida_voci([Voce("latte", "1,5", "l", "2")])
        self.assertEqual(errs, [])
        self.assertEqual(items, [("latte", 1.5, "l", 2)])

        # NaN and Inf values are rejected
        items, errs = valida_voci([
            Voce("pane", "nan", "kg", "1"),
            Voce("riso", "NaN", "kg", "1"),
            Voce("latte", "inf", "l", "1"),
            Voce("olio", "-inf", "l", "1"),
            Voce("vino", "+inf", "l", "1"),
        ])
        self.assertEqual(len(items), 0)
        self.assertEqual(len(errs), 5)

    def test_valida_voci_unit_adversarial(self):
        # Missing or None or semicolon-only unit
        items, errs = valida_voci([
            Voce("pane", "1", None, "1"),
            Voce("riso", "1", "", "1"),
            Voce("latte", "1", "   ", "1"),
            Voce("olio", "1", ";;", "1"),
        ])
        self.assertEqual(len(items), 0)
        self.assertEqual(len(errs), 4)
        for e in errs:
            self.assertIn("missing unit", e)

        # Unit with semicolon stripped
        items, errs = valida_voci([Voce("pane", "1", "kg;", "1")])
        self.assertEqual(errs, [])
        self.assertEqual(items[0][2], "kg")

    def test_valida_voci_packages_adversarial(self):
        # Missing, zero, negative, and non-numeric package counts
        items, errs = valida_voci([
            Voce("pane", "1", "kg", None),
            Voce("riso", "1", "kg", "0"),
            Voce("latte", "1", "l", "-1"),
            Voce("olio", "1", "l", "xyz"),
            Voce("pasta", "1", "kg", "1.5"),
        ])
        self.assertEqual(len(items), 0)
        self.assertEqual(len(errs), 5)
        for e in errs:
            self.assertIn("invalid packages", e)

        # Float string "1.0" representing a positive integer is accepted
        items, errs = valida_voci([Voce("pasta", "1", "kg", "1.0")])
        self.assertEqual(len(errs), 0)
        self.assertEqual(items, [("pasta", 1.0, "kg", 1)])


class TestAdversarialUnitConversion(unittest.TestCase):
    """Stress tests and boundary checks for convert_receipt_quantity_unit."""

    def test_unit_normalization_supported_units(self):
        # Mass: kg, kg., kgs, g, g., gr, gr.
        self.assertEqual(convert_receipt_quantity_unit(500, "g"), (0.5, "kg"))
        self.assertEqual(convert_receipt_quantity_unit(500, "g."), (0.5, "kg"))
        self.assertEqual(convert_receipt_quantity_unit(750, "gr"), (0.75, "kg"))
        self.assertEqual(convert_receipt_quantity_unit(750, "gr."), (0.75, "kg"))
        self.assertEqual(convert_receipt_quantity_unit(1.5, "kg"), (1.5, "kg"))
        self.assertEqual(convert_receipt_quantity_unit(1.5, "kg."), (1.5, "kg"))
        self.assertEqual(convert_receipt_quantity_unit(2.0, "kgs"), (2.0, "kg"))

        # Volume: l, l., lt, lt., ml, ml., cl, cl.
        self.assertEqual(convert_receipt_quantity_unit(1.0, "l"), (1.0, "L"))
        self.assertEqual(convert_receipt_quantity_unit(1.0, "l."), (1.0, "L"))
        self.assertEqual(convert_receipt_quantity_unit(2.5, "lt"), (2.5, "L"))
        self.assertEqual(convert_receipt_quantity_unit(2.5, "lt."), (2.5, "L"))
        self.assertEqual(convert_receipt_quantity_unit(250, "ml"), (0.25, "L"))
        self.assertEqual(convert_receipt_quantity_unit(250, "ml."), (0.25, "L"))
        self.assertEqual(convert_receipt_quantity_unit(33, "cl"), (0.33, "L"))
        self.assertEqual(convert_receipt_quantity_unit(33, "cl."), (0.33, "L"))

        # Pieces: pz, pz., pcs, pcs., pc, pc.
        for u in ("pz", "pz.", "pcs", "pcs.", "pc", "pc."):
            self.assertEqual(convert_receipt_quantity_unit(6, u), (6.0, "pcs"))

        # Whitespace and case-insensitivity
        self.assertEqual(convert_receipt_quantity_unit(1.0, "  KG. "), (1.0, "kg"))
        self.assertEqual(convert_receipt_quantity_unit(6, " Pz. "), (6.0, "pcs"))
        self.assertEqual(convert_receipt_quantity_unit(500, " Ml. "), (0.5, "L"))

    def test_unsupported_units_raise_invalid_input(self):
        for invalid_u in ("grammi", "chili", "etti", "hg", "dl", "cad", "bottiglie", "", None, 123):
            with self.assertRaises(InvalidInput):
                convert_receipt_quantity_unit(10, invalid_u)

    def test_quantity_edge_cases(self):
        # Non-positive values
        with self.assertRaises(InvalidInput):
            convert_receipt_quantity_unit(0, "kg")
        with self.assertRaises(InvalidInput):
            convert_receipt_quantity_unit(-5, "kg")
        with self.assertRaises(InvalidInput):
            convert_receipt_quantity_unit(None, "kg")
        with self.assertRaises(InvalidInput):
            convert_receipt_quantity_unit("abc", "kg")

        # NaN and Inf
        with self.assertRaises(InvalidInput):
            convert_receipt_quantity_unit(float("nan"), "kg")
        with self.assertRaises(InvalidInput):
            convert_receipt_quantity_unit(float("inf"), "kg")
        with self.assertRaises(InvalidInput):
            convert_receipt_quantity_unit(float("-inf"), "kg")

        # Sub-gram rounding down to <= 0 raises InvalidInput
        with self.assertRaises(InvalidInput):
            convert_receipt_quantity_unit(0.00001, "g")

        # Banker's rounding for pieces: 0.5 rounds to 0 -> raises InvalidInput
        with self.assertRaises(InvalidInput):
            convert_receipt_quantity_unit(0.5, "pz")

    def test_cross_dimensional_conversion_and_signature(self):
        # Target unit compatibility checks
        self.assertEqual(convert_receipt_quantity_unit(500, "g", target_unit="kg"), (0.5, "kg"))
        self.assertEqual(convert_receipt_quantity_unit(750, "ml", target_unit="L"), (0.75, "L"))

        # Incompatible dimensions raise InvalidInput
        with self.assertRaises(InvalidInput):
            convert_receipt_quantity_unit(500, "g", target_unit="L")
        with self.assertRaises(InvalidInput):
            convert_receipt_quantity_unit(6, "pz", target_unit="kg")
        with self.assertRaises(InvalidInput):
            convert_receipt_quantity_unit(1.0, "L", target_unit="pcs")

        # Cross-dimensional conversion succeeds when metrics are provided:
        # 6 pieces at 150g/piece -> 0.9 kg (testing positional 5-arg signature)
        self.assertEqual(convert_receipt_quantity_unit(6, "pz", "kg", 150.0, 1.0), (0.9, "kg"))

        # 6 pieces at 150g/piece -> 0.9 kg (keyword arguments)
        res_pcs_kg, unit_pcs_kg = convert_receipt_quantity_unit(
            6, "pz", "kg", grams_per_piece=150.0
        )
        self.assertEqual(res_pcs_kg, 0.9)
        self.assertEqual(unit_pcs_kg, "kg")

        # 1.5 kg at 150g/piece -> 10 pcs
        res_kg_pcs, unit_kg_pcs = convert_receipt_quantity_unit(
            1.5, "kg", "pcs", grams_per_piece=150.0
        )
        self.assertEqual(res_kg_pcs, 10.0)
        self.assertEqual(unit_kg_pcs, "pcs")

        # 2 L at density 0.92 kg/L -> 1.84 kg
        res_l_kg, unit_l_kg = convert_receipt_quantity_unit(
            2.0, "L", "kg", density_kg_per_l=0.92
        )
        self.assertEqual(res_l_kg, 1.84)
        self.assertEqual(unit_l_kg, "kg")

        # 0.92 kg at density 0.92 kg/L -> 1.0 L
        res_kg_l, unit_kg_l = convert_receipt_quantity_unit(
            0.92, "kg", "L", density_kg_per_l=0.92
        )
        self.assertEqual(res_kg_l, 1.0)
        self.assertEqual(unit_kg_l, "L")

        # Missing or non-positive conversion factor across dimensions raises InvalidInput
        for bad_m in (None, 0, -5, float("nan"), float("inf"), float("-inf"), "bad", True, False):
            with self.assertRaises(InvalidInput):
                convert_receipt_quantity_unit(6, "pz", target_unit="kg", grams_per_piece=bad_m)
            with self.assertRaises(InvalidInput):
                convert_receipt_quantity_unit(2.0, "L", target_unit="kg", density_kg_per_l=bad_m)


class TestAdversarialMapReceiptItemToAddItemArgs(unittest.TestCase):
    """Stress tests and boundary checks for map_receipt_item_to_add_item_args."""

    def test_missing_and_whitespace_names(self):
        for bad_name in (None, "", "   ", "\t\n"):
            with self.assertRaises(InvalidInput):
                map_receipt_item_to_add_item_args(name=bad_name, raw_quantity=1.0, raw_unit="kg")

    def test_packages_parsing_and_overflow(self):
        # 0 or negative packages clamped to 1
        res0 = map_receipt_item_to_add_item_args("pasta", 500, "g", packages=0)
        self.assertEqual(res0["intact_items"], 1)

        res_neg = map_receipt_item_to_add_item_args("pasta", 500, "g", packages=-4)
        self.assertEqual(res_neg["intact_items"], 1)

        # String float packages coerced cleanly
        res_float_str = map_receipt_item_to_add_item_args("pasta", 500, "g", packages="3.0")
        self.assertEqual(res_float_str["intact_items"], 3)

        # None or invalid string packages defaults to 1
        res_none = map_receipt_item_to_add_item_args("pasta", 500, "g", packages=None)
        self.assertEqual(res_none["intact_items"], 1)

        res_invalid = map_receipt_item_to_add_item_args("pasta", 500, "g", packages="invalid")
        self.assertEqual(res_invalid["intact_items"], 1)

        # Calling with float('inf') or "inf" packages gracefully defaults to 1 without OverflowError
        res_inf = map_receipt_item_to_add_item_args("pasta", 500, "g", packages=float("inf"))
        self.assertEqual(res_inf["intact_items"], 1)

        res_inf_str = map_receipt_item_to_add_item_args("pasta", 500, "g", packages="inf")
        self.assertEqual(res_inf_str["intact_items"], 1)

    def test_location_and_expiration_date_defaults(self):
        # Location defaults to 'pantry' if None or whitespace
        res = map_receipt_item_to_add_item_args("pasta", 500, "g", location=None)
        self.assertEqual(res["location"], "pantry")

        res_ws = map_receipt_item_to_add_item_args("pasta", 500, "g", location="   ")
        self.assertEqual(res_ws["location"], "pantry")

        res_strip = map_receipt_item_to_add_item_args("pasta", 500, "g", location=" fridge ")
        self.assertEqual(res_strip["location"], "fridge")

        # Expiration date defaults to None if None or whitespace
        res_exp_none = map_receipt_item_to_add_item_args("pasta", 500, "g", expiration_date=None)
        self.assertIsNone(res_exp_none["expiration_date"])

        res_exp_ws = map_receipt_item_to_add_item_args("pasta", 500, "g", expiration_date="   ")
        self.assertIsNone(res_exp_ws["expiration_date"])

        res_exp_valid = map_receipt_item_to_add_item_args("pasta", 500, "g", expiration_date="2026-12-31")
        self.assertEqual(res_exp_valid["expiration_date"], "2026-12-31")

    def test_pcs_vs_mass_volume_mapping(self):
        # Piece product: unit_weight is always 1.0, intact_items is norm_qty * packages
        res_pcs = map_receipt_item_to_add_item_args("uova", 6.0, "pz", packages=2)
        self.assertEqual(res_pcs["unit_weight"], 1.0)
        self.assertEqual(res_pcs["intact_items"], 12)

        # Mass product: unit_weight is normalized quantity, intact_items is packages
        res_kg = map_receipt_item_to_add_item_args("pasta", 500, "g", packages=3)
        self.assertEqual(res_kg["unit_weight"], 0.5)
        self.assertEqual(res_kg["intact_items"], 3)

        # Volume product: unit_weight is normalized volume, intact_items is packages
        res_l = map_receipt_item_to_add_item_args("latte", 750, "ml", packages=2)
        self.assertEqual(res_l["unit_weight"], 0.75)
        self.assertEqual(res_l["intact_items"], 2)


class TestAdversarialReceiptDatabaseIngestion(unittest.TestCase):
    """Simulates 25+ diverse receipt items ingestion and tests boundary conditions in SQLite."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp.name
        self.tmp.close()

        conn = get_connection(self.db_path)
        create_schema(conn)

        # Seed initial products and aliases
        conn.execute(
            """INSERT INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g)
               VALUES (1, 'kg', 355.0, 12.0, 72.0, 1.5)"""
        )
        conn.execute(
            """INSERT INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g)
               VALUES (2, 'pcs', 143.0, 12.6, 0.7, 9.9)"""
        )
        conn.execute(
            """INSERT INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g)
               VALUES (3, 'L', 46.0, 3.3, 4.9, 1.6)"""
        )
        conn.execute("INSERT INTO ALIASES (Name, ID) VALUES ('Penne Rigate', 1)")
        conn.execute("INSERT INTO ALIASES (Name, ID) VALUES ('Barilla Penne', 1)")
        conn.execute("INSERT INTO ALIASES (Name, ID) VALUES ('Uova Fresche', 2)")
        conn.execute("INSERT INTO ALIASES (Name, ID) VALUES ('Latte Parz Scremato', 3)")
        conn.commit()
        conn.close()

    def tearDown(self):
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except OSError:
                pass

    def test_25_diverse_items_end_to_end_batch_ingestion(self):
        """Simulates 25 diverse items spanning Case A, B1, B2, pieces, liquids, weights, and accents."""
        items = [
            # 1. Existing product (Case A)
            {"name": "Penne Rigate", "unit_weight": 0.5, "intact_items": 2, "location": "pantry", "expiration_date": "2027-01-01"},
            # 2. Existing alias (Case A)
            {"name": "Barilla Penne", "unit_weight": 0.5, "intact_items": 3, "location": "pantry", "expiration_date": "2027-02-01"},
            # 3. Existing pcs product (Case A)
            {"name": "Uova Fresche", "unit_weight": 1.0, "intact_items": 12, "location": "fridge", "expiration_date": "2026-12-01"},
            # 4. Existing L product (Case A)
            {"name": "Latte Parz Scremato", "unit_weight": 1.0, "intact_items": 4, "location": "fridge", "expiration_date": "2026-11-20"},
            # 5. New alias for Penne (Case B1)
            {"name": "Penne Integrali Barilla", "unit_weight": 0.5, "intact_items": 1, "location": "pantry", "fallback_id": 1},
            # 6. New alias for Latte (Case B1)
            {"name": "Latte Granarolo Parz", "unit_weight": 1.0, "intact_items": 2, "location": "fridge", "fallback_id": 3},
            # 7. New alias for Uova (Case B1)
            {"name": "Uova Allevate a Terra", "unit_weight": 1.0, "intact_items": 6, "location": "fridge", "fallback_id": 2},
            # 8. New Product 1 (Case B2)
            {"name": "Riso Basmati", "unit_weight": 1.0, "intact_items": 2, "location": "pantry", "fallback_id": 4, "new_product_unit": "kg", "new_product_macros": {"Kcal_100g": 360, "Protein_100g": 8.0, "Carbs_100g": 78.0, "Fat_100g": 1.0}},
            # 9. New Product 2 (Case B2)
            {"name": "Olio EVO", "unit_weight": 0.75, "intact_items": 1, "location": "pantry", "fallback_id": 5, "new_product_unit": "L", "new_product_macros": {"Kcal_100g": 884, "Protein_100g": 0.0, "Carbs_100g": 0.0, "Fat_100g": 100.0}},
            # 10. New Product 3 (Case B2)
            {"name": "Tonno in Scatola", "unit_weight": 1.0, "intact_items": 6, "location": "pantry", "fallback_id": 6, "new_product_unit": "pcs", "new_product_macros": {"Kcal_100g": 130, "Protein_100g": 26.0, "Carbs_100g": 0.0, "Fat_100g": 3.0}},
            # 11. New Product 4 (Case B2)
            {"name": "Yogurt Greco", "unit_weight": 0.15, "intact_items": 4, "location": "fridge", "fallback_id": 7, "new_product_unit": "kg", "new_product_macros": {"Kcal_100g": 59, "Protein_100g": 10.0, "Carbs_100g": 3.6, "Fat_100g": 0.4}},
            # 12. New Product 5 (Case B2)
            {"name": "Petto di Pollo", "unit_weight": 0.45, "intact_items": 1, "location": "fridge", "fallback_id": 8, "new_product_unit": "kg", "new_product_macros": {"Kcal_100g": 110, "Protein_100g": 23.0, "Carbs_100g": 0.0, "Fat_100g": 1.5}},
            # 13. New Product 6 (Case B2)
            {"name": "Biscotti Integrali", "unit_weight": 0.4, "intact_items": 2, "location": "pantry", "fallback_id": 9, "new_product_unit": "kg", "new_product_macros": {"Kcal_100g": 440, "Protein_100g": 8.0, "Carbs_100g": 68.0, "Fat_100g": 14.0}},
            # 14. New Product 7 (Case B2)
            {"name": "Birra Moretti", "unit_weight": 0.66, "intact_items": 3, "location": "cellar", "fallback_id": 10, "new_product_unit": "L", "new_product_macros": {"Kcal_100g": 43, "Protein_100g": 0.5, "Carbs_100g": 3.5, "Fat_100g": 0.0}},
            # 15. New Product 8 (Case B2)
            {"name": "Mele Fuji", "unit_weight": 1.2, "intact_items": 1, "location": "pantry", "fallback_id": 11, "new_product_unit": "kg", "new_product_macros": {"Kcal_100g": 52, "Protein_100g": 0.3, "Carbs_100g": 14.0, "Fat_100g": 0.2}},
            # 16. New Product 9 (Case B2)
            {"name": "Banane Cavendish", "unit_weight": 0.95, "intact_items": 1, "location": "pantry", "fallback_id": 12, "new_product_unit": "kg", "new_product_macros": {"Kcal_100g": 89, "Protein_100g": 1.1, "Carbs_100g": 23.0, "Fat_100g": 0.3}},
            # 17. New Product 10 (Case B2)
            {"name": "Parmigiano Reggiano", "unit_weight": 0.25, "intact_items": 2, "location": "fridge", "fallback_id": 13, "new_product_unit": "kg", "new_product_macros": {"Kcal_100g": 392, "Protein_100g": 33.0, "Carbs_100g": 0.0, "Fat_100g": 28.0}},
            # 18. New Product 11 (Case B2)
            {"name": "Passata di Pomodoro", "unit_weight": 0.7, "intact_items": 3, "location": "pantry", "fallback_id": 14, "new_product_unit": "kg", "new_product_macros": {"Kcal_100g": 24, "Protein_100g": 1.3, "Carbs_100g": 4.0, "Fat_100g": 0.2}},
            # 19. New Product 12 (Case B2)
            {"name": "Aceto Balsamico", "unit_weight": 0.5, "intact_items": 1, "location": "pantry", "fallback_id": 15, "new_product_unit": "L", "new_product_macros": {"Kcal_100g": 88, "Protein_100g": 0.5, "Carbs_100g": 17.0, "Fat_100g": 0.0}},
            # 20. New Product 13 (Case B2)
            {"name": "Cioccolato Extra Fondente", "unit_weight": 0.1, "intact_items": 5, "location": "pantry", "fallback_id": 16, "new_product_unit": "kg", "new_product_macros": {"Kcal_100g": 580, "Protein_100g": 8.0, "Carbs_100g": 35.0, "Fat_100g": 43.0}},
            # 21. New Product 14 (Case B2)
            {"name": "Pane Casereccio", "unit_weight": 0.5, "intact_items": 1, "location": "pantry", "fallback_id": 17, "new_product_unit": "kg", "new_product_macros": {"Kcal_100g": 265, "Protein_100g": 9.0, "Carbs_100g": 49.0, "Fat_100g": 3.2}},
            # 22. New Product 15 (Case B2)
            {"name": "Avocado Bio", "unit_weight": 1.0, "intact_items": 2, "location": "pantry", "fallback_id": 18, "new_product_unit": "pcs", "new_product_macros": {"Kcal_100g": 160, "Protein_100g": 2.0, "Carbs_100g": 9.0, "Fat_100g": 15.0}},
            # 23. Second instance of New Product 1 (Case A after B2)
            {"name": "Riso Basmati", "unit_weight": 1.0, "intact_items": 1, "location": "cellar"},
            # 24. Second instance with alias of New Product 2 (Case B1 after B2)
            {"name": "Olio Extra Vergine", "unit_weight": 0.75, "intact_items": 2, "location": "pantry", "fallback_id": 5},
            # 25. Item with accented characters and symbols (Case B2)
            {"name": "Caffè Macinato 100% Arabica", "unit_weight": 0.25, "intact_items": 2, "location": "pantry", "fallback_id": 19, "new_product_unit": "kg", "new_product_macros": {"Kcal_100g": 280, "Protein_100g": 14.0, "Carbs_100g": 40.0, "Fat_100g": 0.5}},
        ]

        for it in items:
            res = add_item(**it, db_path=self.db_path)
            self.assertEqual(res["outcome"], "ok")

        conn = get_connection(self.db_path)
        cur = conn.cursor()

        inv_count = cur.execute("SELECT COUNT(*) FROM INVENTORY").fetchone()[0]
        prod_count = cur.execute("SELECT COUNT(*) FROM PRODUCTS").fetchone()[0]
        alias_count = cur.execute("SELECT COUNT(*) FROM ALIASES").fetchone()[0]
        fk_violations = cur.execute("PRAGMA foreign_key_check").fetchall()
        integrity = cur.execute("PRAGMA integrity_check").fetchone()[0]
        conn.close()

        self.assertEqual(inv_count, 25)
        self.assertEqual(prod_count, 19)
        self.assertEqual(alias_count, 24)
        self.assertEqual(len(fk_violations), 0)
        self.assertEqual(integrity, "ok")

    def test_adversarial_add_item_inf_and_nan_behavior(self):
        """Verifies that add_item rejects inf, nan, and fractional intact_items with InvalidInput."""
        # 1. unit_weight = float('inf') is rejected upfront with InvalidInput
        with self.assertRaises(InvalidInput):
            add_item("Penne Rigate", float("inf"), 1, "pantry", db_path=self.db_path)

        # 2. unit_weight = float('nan') is rejected upfront with InvalidInput (not AliasConflict)
        with self.assertRaises(InvalidInput):
            add_item("Penne Rigate", float("nan"), 1, "pantry", db_path=self.db_path)

        # 3. intact_items = 1.5 is rejected upfront with InvalidInput
        with self.assertRaises(InvalidInput):
            add_item("Penne Rigate", 0.5, 1.5, "pantry", db_path=self.db_path)

        # 4. intact_items = float('inf') is rejected upfront with InvalidInput
        with self.assertRaises(InvalidInput):
            add_item("Penne Rigate", 0.5, float("inf"), "pantry", db_path=self.db_path)

        # 5. Booleans and non-positive numbers rejected upfront
        with self.assertRaises(InvalidInput):
            add_item("Penne Rigate", True, 1, "pantry", db_path=self.db_path)
        with self.assertRaises(InvalidInput):
            add_item("Penne Rigate", 0.5, True, "pantry", db_path=self.db_path)
        with self.assertRaises(InvalidInput):
            add_item("Penne Rigate", -0.5, 1, "pantry", db_path=self.db_path)
        with self.assertRaises(InvalidInput):
            add_item("Penne Rigate", 0.5, 0, "pantry", db_path=self.db_path)
        with self.assertRaises(InvalidInput):
            add_item("Penne Rigate", 0.5, -2, "pantry", db_path=self.db_path)

        # 6. macro nutrients with negative, inf, nan, or bool values raise InvalidInput
        with self.assertRaises(InvalidInput):
            add_item(
                "Nuovo Prodotto Inf Macro", 0.5, 1, "pantry",
                fallback_id=99,
                new_product_unit="kg",
                new_product_macros={"Kcal_100g": float("inf"), "Protein_100g": 10, "Carbs_100g": 10, "Fat_100g": 10},
                db_path=self.db_path,
            )
        with self.assertRaises(InvalidInput):
            add_item(
                "Nuovo Prodotto Neg Macro", 0.5, 1, "pantry",
                fallback_id=100,
                new_product_unit="kg",
                new_product_macros={"Kcal_100g": -5, "Protein_100g": 10, "Carbs_100g": 10, "Fat_100g": 10},
                db_path=self.db_path,
            )
        with self.assertRaises(InvalidInput):
            add_item(
                "Nuovo Prodotto Bool Macro", 0.5, 1, "pantry",
                fallback_id=101,
                new_product_unit="kg",
                new_product_macros={"Kcal_100g": True, "Protein_100g": 10, "Carbs_100g": 10, "Fat_100g": 10},
                db_path=self.db_path,
            )
        with self.assertRaises(InvalidInput):
            add_item(
                "Nuovo Prodotto NaN Macro", 0.5, 1, "pantry",
                fallback_id=102,
                new_product_unit="kg",
                new_product_macros={"Kcal_100g": float("nan"), "Protein_100g": 10, "Carbs_100g": 10, "Fat_100g": 10},
                db_path=self.db_path,
            )




# ======================================================================
# SECTION: tests/test_receipt_to_db_targeted.py
# ======================================================================

"""Targeted and exhaustive test suite for the Receipt -> Database pipeline.

Verifies the entire workflow:
1. Parsing & Unit conversion normalization (convert_receipt_quantity_unit).
2. First review modal (FinestraModifica) row modification, validation, file output, and on_save callback.
3. Second review modal (ReceiptReconciliationDialog) catalog matching, indicator colors,
   alias mapping, new product creation with custom macros, location selection, and expiry date.
4. Database ingestion (add_item, atomic transactions, ALIASES, PRODUCTS, INVENTORY).
5. App GUI integration (_on_receipt_review_saved, pantry table refresh, log reporting).
6. Complete end-to-end flow from raw receipt text/items to final SQLite inventory tables.
"""

from datetime import date, timedelta
import math
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import tkinter as tk
from unittest.mock import MagicMock, patch
import unittest

from meal_planner import receipts as tp
from meal_planner.receipts import Voce, valida_voci, FinestraModifica

from meal_planner.db.connection import get_connection
from meal_planner.db.schema import create_schema
from meal_planner.db.schema import migrate
from meal_planner.gui.app import App
from meal_planner.gui.dialogs import (
    COLOR_RECOGNIZED,
    COLOR_UNRECOGNIZED,
    ReceiptReconciliationDialog,
)
from meal_planner.inventory import (
    add_item,
    convert_receipt_quantity_unit,
    get_available_inventory,
    get_catalog_for_reconciliation,
    get_locations,
    get_next_product_id,
    InvalidInput,
    ProductNotFound,
)


class TestReceiptToDatabaseTargeted(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Create root Tk for GUI tests and withdraw it so it stays headless
        cls.root = tk.Tk()
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:
            pass

    def setUp(self):
        # Dedicated isolated temporary DB for each test method
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp.name
        self.tmp.close()

        self._orig_env = os.environ.get("MEAL_PLANNER_DB")
        os.environ["MEAL_PLANNER_DB"] = self.db_path

        conn = get_connection(self.db_path)
        create_schema(conn)
        migrate(conn)
        cur = conn.cursor()

        # Seed initial canonical products
        # ID 1: Penne Rigate (kg)
        cur.execute(
            """INSERT INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Sugars_100g, Fat_100g, Saturated_fat_100g, Fiber_100g, Salt_100g)
               VALUES (1, 'kg', 355.0, 12.5, 72.0, 3.2, 1.5, 0.3, 3.0, 0.005)"""
        )
        # ID 2: Uova Fresche (pcs)
        cur.execute(
            """INSERT INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Sugars_100g, Fat_100g, Saturated_fat_100g, Fiber_100g, Salt_100g)
               VALUES (2, 'pcs', 143.0, 12.6, 0.7, 0.7, 9.9, 3.1, 0.0, 0.3)"""
        )
        # ID 3: Latte Parz Scremato (L)
        cur.execute(
            """INSERT INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Sugars_100g, Fat_100g, Saturated_fat_100g, Fiber_100g, Salt_100g)
               VALUES (3, 'L', 46.0, 3.3, 4.9, 4.9, 1.6, 1.0, 0.0, 0.1)"""
        )

        # Seed initial aliases
        cur.execute("INSERT INTO ALIASES (Name, ID) VALUES ('Penne Rigate', 1)")
        cur.execute("INSERT INTO ALIASES (Name, ID) VALUES ('Barilla Penne', 1)")
        cur.execute("INSERT INTO ALIASES (Name, ID) VALUES ('Uova Fresche', 2)")
        cur.execute("INSERT INTO ALIASES (Name, ID) VALUES ('Latte Parz Scremato', 3)")
        conn.commit()
        conn.close()

        # Patch showerror / showwarning in messagebox to avoid blocking modal dialogs
        self._patch_err = patch("tkinter.messagebox.showerror")
        self._patch_warn = patch("tkinter.messagebox.showwarning")
        self.mock_showerror = self._patch_err.start()
        self.mock_showwarning = self._patch_warn.start()

    def tearDown(self):
        self._patch_err.stop()
        self._patch_warn.stop()

        if self._orig_env is not None:
            os.environ["MEAL_PLANNER_DB"] = self._orig_env
        else:
            os.environ.pop("MEAL_PLANNER_DB", None)

        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except OSError:
                pass

    # =========================================================================
    # PART 1: UNIT CONVERSION AND NORMALIZATION
    # =========================================================================

    def test_convert_receipt_quantity_unit_grams_to_kg(self):
        """Verify g / gr -> kg normalization."""
        qty, unit = convert_receipt_quantity_unit(500, "g")
        self.assertEqual(qty, 0.5)
        self.assertEqual(unit, "kg")

        qty2, unit2 = convert_receipt_quantity_unit("1250", "gr")
        self.assertEqual(qty2, 1.25)
        self.assertEqual(unit2, "kg")

        qty3, unit3 = convert_receipt_quantity_unit(2.5, "kg")
        self.assertEqual(qty3, 2.5)
        self.assertEqual(unit3, "kg")

        qty4, unit4 = convert_receipt_quantity_unit(1.0, "kgs")
        self.assertEqual(qty4, 1.0)
        self.assertEqual(unit4, "kg")

        qty5, unit5 = convert_receipt_quantity_unit(1.5, "kg.")
        self.assertEqual(qty5, 1.5)
        self.assertEqual(unit5, "kg")

        qty6, unit6 = convert_receipt_quantity_unit(500, "g.")
        self.assertEqual(qty6, 0.5)
        self.assertEqual(unit6, "kg")

    def test_convert_receipt_quantity_unit_liquids_to_liters(self):
        """Verify ml / cl / l / lt -> L normalization."""
        qty_ml, unit_ml = convert_receipt_quantity_unit(750, "ml")
        self.assertEqual(qty_ml, 0.75)
        self.assertEqual(unit_ml, "L")

        qty_cl, unit_cl = convert_receipt_quantity_unit(33, "cl")
        self.assertEqual(qty_cl, 0.33)
        self.assertEqual(unit_cl, "L")

        qty_l, unit_l = convert_receipt_quantity_unit(1.5, "l")
        self.assertEqual(qty_l, 1.5)
        self.assertEqual(unit_l, "L")

        qty_lt, unit_lt = convert_receipt_quantity_unit(2.0, "lt")
        self.assertEqual(qty_lt, 2.0)
        self.assertEqual(unit_lt, "L")

    def test_convert_receipt_quantity_unit_pieces(self):
        """Verify pz / pcs / pc -> pcs normalization."""
        for u in ("pz", "pcs", "pc", "pz.", "pcs."):
            qty, unit = convert_receipt_quantity_unit(6, u)
            self.assertEqual(qty, 6.0)
            self.assertEqual(unit, "pcs")

    def test_convert_receipt_quantity_unit_incompatible_dimensions(self):
        """Verify incompatible dimensional conversions raise InvalidInput."""
        with self.assertRaises(InvalidInput):
            # Cannot convert grams to liters
            convert_receipt_quantity_unit(500, "g", target_unit="L")
        with self.assertRaises(InvalidInput):
            # Cannot convert liters to pieces
            convert_receipt_quantity_unit(1.0, "L", target_unit="pcs")
        with self.assertRaises(InvalidInput):
            # Cannot convert pieces to kg
            convert_receipt_quantity_unit(6, "pcs", target_unit="kg")

    def test_convert_receipt_quantity_unit_invalid_inputs(self):
        """Verify negative quantities, unsupported units, or empty units raise InvalidInput."""
        with self.assertRaises(InvalidInput):
            convert_receipt_quantity_unit(-5, "kg")
        with self.assertRaises(InvalidInput):
            convert_receipt_quantity_unit(0, "kg")
        with self.assertRaises(InvalidInput):
            convert_receipt_quantity_unit(10, "")
        with self.assertRaises(InvalidInput):
            convert_receipt_quantity_unit(10, "unknown_unit_xyz")
        with self.assertRaises(InvalidInput):
            convert_receipt_quantity_unit(250, "grammi")
        with self.assertRaises(InvalidInput):
            convert_receipt_quantity_unit(5, "dl")

    # =========================================================================
    # PART 2: FIRST REVIEW WINDOW (FinestraModifica)
    # =========================================================================

    def test_finestra_modifica_lifecycle_edit_and_save(self):
        """Verify FinestraModifica loads entries, allows modifications, validates, and fires on_save."""
        voci = [
            Voce("barilla penne 500g", "500", "g", "2", 0.95),
            Voce("latte parz", "1", "L", "1", 0.88),
            Voce("uova bio", "6", "pz", "1", 0.99),
        ]

        with tempfile.TemporaryDirectory() as tmp_dir:
            out_file = Path(tmp_dir) / "test_out.txt"
            saved_items_sink = []

            def handle_save(items):
                saved_items_sink.extend(items)

            top = tk.Toplevel(self.root)
            try:
                fm = FinestraModifica(top, voci, out_file, "Test Review", on_save=handle_save)

                # Check rows loaded
                self.assertEqual(len(fm.righe), 3)

                # Edit row 0: change quantity to 450
                fm.righe[0]["vars"]["qta"].set("450")

                # Add a row
                fm.aggiungi_riga()
                self.assertEqual(len(fm.righe), 4)
                fm.righe[3]["vars"]["nome"].set("mele gala")
                fm.righe[3]["vars"]["qta"].set("1.5")
                fm.righe[3]["vars"]["unita"].set("kg")
                fm.righe[3]["vars"]["confezioni"].set("1")

                # Remove row 2 (uova bio)
                fm.rimuovi_riga(fm.righe[2])
                self.assertEqual(len(fm.righe), 3)

                # Trigger save
                save_result = fm.salva()
                self.assertTrue(save_result)

                # Verify file was written
                self.assertTrue(out_file.exists())
                content = out_file.read_text(encoding="utf-8")
                self.assertIn("barilla penne 500g;450;g;2", content)
                self.assertIn("latte parz;1;l;1", content)
                self.assertIn("mele gala;1.5;kg;1", content)

                # Verify on_save callback was called with parsed items
                self.assertEqual(len(saved_items_sink), 3)
                self.assertEqual(saved_items_sink[0], ("barilla penne 500g", 450.0, "g", 2))
                self.assertEqual(saved_items_sink[1], ("latte parz", 1.0, "l", 1))
                self.assertEqual(saved_items_sink[2], ("mele gala", 1.5, "kg", 1))
            finally:
                try:
                    top.destroy()
                except Exception:
                    pass

    def test_finestra_modifica_validation_blocks_save_on_invalid_data(self):
        """Verify FinestraModifica refuses to save when quantity or packages are invalid."""
        voci = [Voce("invalid item", "-2", "kg", "1", 0.95)]
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_file = Path(tmp_dir) / "test_out_fail.txt"
            saved_items_sink = []

            top = tk.Toplevel(self.root)
            try:
                fm = FinestraModifica(top, voci, out_file, "Test Fail", on_save=saved_items_sink.append)
                save_res = fm.salva()

                # Must fail validation
                self.assertFalse(save_res)
                # Output file must NOT exist
                self.assertFalse(out_file.exists())
                # on_save must NOT have been called
                self.assertEqual(len(saved_items_sink), 0)
                # Window must remain open
                self.assertTrue(top.winfo_exists())
            finally:
                try:
                    top.destroy()
                except Exception:
                    pass

        # Direct adversarial validation checks on valida_voci
        items, errs = valida_voci([
            Voce(None, None, None, None),       # Empty/None Voce safely ignored
            Voce("pasta", "nan", "kg", "1"),    # NaN quantity rejected
            Voce("riso", "inf", "kg", "1"),     # Inf quantity rejected
            Voce("olio", "0", "L", "1"),        # Zero quantity rejected
            Voce("latte", "1", "", "1"),        # Missing unit rejected
            Voce("uova", "6", "pz", "0"),       # Zero packages rejected
            Voce("mele", "1", "kg", "-1"),      # Negative packages rejected
            Voce("pere", "1", "kg", "abc"),     # Non-numeric packages rejected
        ])
        self.assertEqual(len(items), 0)
        self.assertEqual(len(errs), 7)

    # =========================================================================
    # PART 3: SECOND REVIEW WINDOW (ReceiptReconciliationDialog)
    # =========================================================================

    def test_reconciliation_dialog_recognized_product_prefill(self):
        """Case A: Product matching existing alias is highlighted green with prefilled canonical macros."""
        # 'Barilla Penne' is an alias for 'Penne Rigate' (ID 1)
        items = [("Barilla Penne", 0.5, "kg", 2)]
        dlg = ReceiptReconciliationDialog(self.root, items, db_path=self.db_path, wait=False)
        try:
            row = dlg.rows[0]
            # Verify status indicator is green
            self.assertEqual(row["status"], COLOR_RECOGNIZED)
            self.assertEqual(row["status_canvas"].itemcget(row["rect_id"], "fill"), COLOR_RECOGNIZED)

            # Verify canonical product selected
            self.assertEqual(row["product_var"].get(), "Penne Rigate")
            # Verify macros prefilled from PRODUCTS ID 1
            self.assertEqual(float(row["kcal_var"].get()), 355.0)
            self.assertEqual(float(row["prot_var"].get()), 12.5)
            self.assertEqual(float(row["carb_var"].get()), 72.0)
            self.assertEqual(float(row["fat_var"].get()), 1.5)

            # Set expiration date and custom location
            row["loc_var"].set("pantry")
            row["exp_var"].set("2026-11-20")

            save_ok = dlg.save()
            self.assertTrue(save_ok)
            self.assertIsNotNone(dlg.result)
            self.assertEqual(len(dlg.result), 1)

            res = dlg.result[0]
            self.assertEqual(res["name"], "Barilla Penne")
            self.assertEqual(res["canonical_name"], "Penne Rigate")
            self.assertEqual(res["unit_weight"], 0.5)
            self.assertEqual(res["intact_items"], 2)
            self.assertEqual(res["location"], "pantry")
            self.assertEqual(res["expiration_date"], "2026-11-20")
            self.assertEqual(res["fallback_id"], 1)
            self.assertFalse(res["is_new_product"])
        finally:
            try:
                dlg.destroy()
            except Exception:
                pass

    def test_reconciliation_dialog_unrecognized_mapped_to_existing(self):
        """Case B1: Unrecognized item starts orange, turns green when mapped to an existing product."""
        # 'pasta de cecco penne' is not in ALIASES
        items = [("pasta de cecco penne", 0.5, "kg", 3)]
        dlg = ReceiptReconciliationDialog(self.root, items, db_path=self.db_path, wait=False)
        try:
            row = dlg.rows[0]
            # Initially unrecognized -> orange
            self.assertEqual(row["status"], COLOR_UNRECOGNIZED)
            self.assertEqual(row["status_canvas"].itemcget(row["rect_id"], "fill"), COLOR_UNRECOGNIZED)
            self.assertEqual(row["product_var"].get(), "[New Product]")

            # User chooses 'Penne Rigate' from the dropdown
            row["product_var"].set("Penne Rigate")
            dlg._on_product_change(row)

            # Should now switch to green
            self.assertEqual(row["status"], COLOR_RECOGNIZED)
            self.assertEqual(row["status_canvas"].itemcget(row["rect_id"], "fill"), COLOR_RECOGNIZED)
            # Macros auto-populated
            self.assertEqual(float(row["kcal_var"].get()), 355.0)

            save_ok = dlg.save()
            self.assertTrue(save_ok)
            res = dlg.result[0]
            self.assertEqual(res["name"], "pasta de cecco penne")
            self.assertEqual(res["canonical_name"], "Penne Rigate")
            self.assertEqual(res["fallback_id"], 1)
            self.assertFalse(res["is_new_product"])
        finally:
            try:
                dlg.destroy()
            except Exception:
                pass

    def test_reconciliation_dialog_new_product_with_custom_macros(self):
        """Case B2: Unrecognized item kept as [New Product] with user-entered macros."""
        items = [("Tofu Bio Vellutato", 0.3, "kg", 2)]
        dlg = ReceiptReconciliationDialog(self.root, items, db_path=self.db_path, wait=False)
        try:
            row = dlg.rows[0]
            self.assertEqual(row["status"], COLOR_UNRECOGNIZED)

            # Enter custom macros
            row["kcal_var"].set("120")
            row["prot_var"].set("14.5")
            row["carb_var"].set("2.0")
            row["fat_var"].set("6.0")
            row["loc_var"].set("fridge")
            row["exp_var"].set("2026-12-10")

            save_ok = dlg.save()
            self.assertTrue(save_ok)
            res = dlg.result[0]
            self.assertEqual(res["name"], "Tofu Bio Vellutato")
            self.assertTrue(res["is_new_product"])
            # Next available product ID must be >= 4 (since 1, 2, 3 exist)
            self.assertGreaterEqual(res["fallback_id"], 4)
            self.assertEqual(res["new_product_unit"], "kg")
            self.assertEqual(
                res["new_product_macros"],
                {"Kcal_100g": 120.0, "Protein_100g": 14.5, "Carbs_100g": 2.0, "Fat_100g": 6.0},
            )
            self.assertEqual(res["location"], "fridge")
            self.assertEqual(res["expiration_date"], "2026-12-10")
        finally:
            try:
                dlg.destroy()
            except Exception:
                pass

    def test_reconciliation_dialog_piece_product_normalization(self):
        """Verify pcs products enforce unit_weight = 1.0 and intact_items = max(1, round(weight * packs))."""
        # E.g. 6 eggs per pack, 2 packs -> 12 items
        items = [("Uova Fresche", 6.0, "pcs", 2)]
        dlg = ReceiptReconciliationDialog(self.root, items, db_path=self.db_path, wait=False)
        try:
            self.assertTrue(dlg.save())
            res = dlg.result[0]
            self.assertEqual(res["unit_weight"], 1.0)
            self.assertEqual(res["intact_items"], 12)
        finally:
            try:
                dlg.destroy()
            except Exception:
                pass

    def test_reconciliation_dialog_validation_failures(self):
        """Verify invalid inputs in reconciliation dialog prevent saving."""
        # 1. Invalid weight <= 0
        dlg1 = ReceiptReconciliationDialog(self.root, [("Penne Rigate", 0, "kg", 1)], db_path=self.db_path, wait=False)
        try:
            self.assertFalse(dlg1.save())
            self.assertIn("Weight/Volume must be a positive number", dlg1.last_error)
        finally:
            dlg1.destroy()

        # 2. Invalid packages < 1
        dlg2 = ReceiptReconciliationDialog(self.root, [("Penne Rigate", 0.5, "kg", 0)], db_path=self.db_path, wait=False)
        try:
            self.assertFalse(dlg2.save())
            self.assertIn("Packages must be an integer >= 1", dlg2.last_error)
        finally:
            dlg2.destroy()

        # 3. Invalid expiration date format
        dlg3 = ReceiptReconciliationDialog(self.root, [("Penne Rigate", 0.5, "kg", 1)], db_path=self.db_path, wait=False)
        try:
            dlg3.rows[0]["exp_var"].set("31-12-2026")  # Not YYYY-MM-DD
            self.assertFalse(dlg3.save())
            self.assertIn("Expiration date must be YYYY-MM-DD", dlg3.last_error)
        finally:
            dlg3.destroy()

        # 4. New product with missing macros
        dlg4 = ReceiptReconciliationDialog(self.root, [("New Unseen Product", 1.0, "kg", 1)], db_path=self.db_path, wait=False)
        try:
            self.assertFalse(dlg4.save())
            self.assertIn("nutritional macros", dlg4.last_error)
        finally:
            dlg4.destroy()

    def test_reconciliation_dialog_cancellation(self):
        """Verify clicking cancel sets result=None and cleans up."""
        items = [("Penne Rigate", 0.5, "kg", 1)]
        dlg = ReceiptReconciliationDialog(self.root, items, db_path=self.db_path, wait=False)
        try:
            dlg.cancel()
            self.assertIsNone(dlg.result)
        finally:
            try:
                dlg.destroy()
            except Exception:
                pass

    # =========================================================================
    # PART 4: DIRECT DATABASE INGESTION ENGINE (add_item)
    # =========================================================================

    def test_database_ingestion_case_a_known_alias(self):
        """Case A: Ingestion of known item into INVENTORY."""
        outcome = add_item(
            name="Penne Rigate",
            unit_weight=0.5,
            intact_items=4,
            location="pantry",
            expiration_date="2027-01-15",
            db_path=self.db_path,
        )
        self.assertEqual(outcome["outcome"], "ok")
        self.assertEqual(outcome["case"], "A")
        self.assertEqual(outcome["ID"], 1)

        # Verify SQLite row
        conn = get_connection(self.db_path)
        cur = conn.cursor()
        rows = cur.execute(
            "SELECT ID, Unit_Weight, Intact_Items, Open_Remainder, Location, Expiration_Date FROM INVENTORY WHERE ID = 1"
        ).fetchall()
        conn.close()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0], (1, 0.5, 4, 0.0, "pantry", "2027-01-15"))

    def test_database_ingestion_case_b1_new_alias_to_existing_product(self):
        """Case B1: Ingestion of new alias creates row in ALIASES and INVENTORY."""
        outcome = add_item(
            name="Rummo N.66",
            unit_weight=0.5,
            intact_items=2,
            location="pantry",
            expiration_date=None,
            fallback_id=1,  # Maps to Penne Rigate (ID 1)
            db_path=self.db_path,
        )
        self.assertEqual(outcome["outcome"], "ok")
        self.assertEqual(outcome["case"], "B1")
        self.assertEqual(outcome["ID"], 1)

        # Verify ALIASES contains Rummo N.66 pointing to 1
        conn = get_connection(self.db_path)
        cur = conn.cursor()
        alias_row = cur.execute("SELECT ID FROM ALIASES WHERE Name = 'Rummo N.66'").fetchone()
        inv_row = cur.execute(
            "SELECT ID, Unit_Weight, Intact_Items, Location, Expiration_Date FROM INVENTORY WHERE Row_ID = ?",
            (outcome["ID_row_inventory"],)
        ).fetchone()
        conn.close()

        self.assertIsNotNone(alias_row)
        self.assertEqual(alias_row[0], 1)
        self.assertEqual(inv_row, (1, 0.5, 2, "pantry", None))

    def test_database_ingestion_case_b2_new_product(self):
        """Case B2: Ingestion of brand new product creates PRODUCTS, ALIASES, and INVENTORY."""
        outcome = add_item(
            name="Tempeh Naturale",
            unit_weight=0.2,
            intact_items=3,
            location="fridge",
            expiration_date="2026-11-25",
            fallback_id=10,
            new_product_unit="kg",
            new_product_macros={
                "Kcal_100g": 192.0,
                "Protein_100g": 20.3,
                "Carbs_100g": 7.6,
                "Fat_100g": 10.8,
            },
            db_path=self.db_path,
        )
        self.assertEqual(outcome["outcome"], "ok")
        self.assertEqual(outcome["case"], "B2")
        self.assertEqual(outcome["ID"], 10)

        # Verify PRODUCTS
        conn = get_connection(self.db_path)
        cur = conn.cursor()
        prod = cur.execute(
            "SELECT ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g FROM PRODUCTS WHERE ID = 10"
        ).fetchone()
        alias = cur.execute("SELECT ID FROM ALIASES WHERE Name = 'Tempeh Naturale'").fetchone()
        inv = cur.execute(
            "SELECT ID, Unit_Weight, Intact_Items, Location, Expiration_Date FROM INVENTORY WHERE Row_ID = ?",
            (outcome["ID_row_inventory"],)
        ).fetchone()
        conn.close()

        self.assertEqual(prod, (10, "kg", 192.0, 20.3, 7.6, 10.8))
        self.assertEqual(alias, (10,))
        self.assertEqual(inv, (10, 0.2, 3, "fridge", "2026-11-25"))

    def test_database_ingestion_atomic_batch_with_connection(self):
        """Verify batch ingestion using external transaction commits atomically."""
        conn = get_connection(self.db_path)
        try:
            add_item("Penne Rigate", 0.5, 2, "pantry", conn=conn)
            add_item("Latte Parz Scremato", 1.0, 3, "fridge", conn=conn)
            conn.commit()
        finally:
            conn.close()

        conn = get_connection(self.db_path)
        count = conn.cursor().execute("SELECT COUNT(*) FROM INVENTORY").fetchone()[0]
        conn.close()
        self.assertEqual(count, 2)

    # =========================================================================
    # PART 5: APP GUI INTEGRATION & FULL BATCH PIPELINE
    # =========================================================================

    def test_app_on_receipt_review_saved_full_batch_success(self):
        """Verify App._on_receipt_review_saved handles mixed batch with dialog and updates GUI & DB."""
        app = App()
        app.db_path = self.db_path
        try:
            # 1. Known alias: Barilla Penne (0.5 kg, 2 packs) -> Case A
            # 2. Unrecognized alias mapped to ID 3: 'latte scremato bio' -> Case B1 (1 new alias)
            # 3. New product: 'Seitan Alla Piastra' (0.25 kg, 1 pack, macros) -> Case B2 (1 new product)
            # 4. Known item: 'Uova Fresche' (6 pcs, 2 packs -> 12 pcs) -> Case A
            raw_items = [
                ("Barilla Penne", 0.5, "kg", 2),
                ("latte scremato bio", 1.0, "L", 2),
                ("Seitan Alla Piastra", 0.25, "kg", 1),
                ("Uova Fresche", 6.0, "pz", 2),
            ]

            reconciled_items = [
                {
                    "name": "Barilla Penne",
                    "canonical_name": "Penne Rigate",
                    "unit_weight": 0.5,
                    "intact_items": 2,
                    "location": "pantry",
                    "expiration_date": "2027-06-30",
                    "fallback_id": 1,
                    "new_product_unit": None,
                    "new_product_macros": None,
                    "is_new_product": False,
                },
                {
                    "name": "latte scremato bio",
                    "canonical_name": "Latte Parz Scremato",
                    "unit_weight": 1.0,
                    "intact_items": 2,
                    "location": "fridge",
                    "expiration_date": "2026-11-15",
                    "fallback_id": 3,
                    "new_product_unit": None,
                    "new_product_macros": None,
                    "is_new_product": False,
                },
                {
                    "name": "Seitan Alla Piastra",
                    "canonical_name": "Seitan Alla Piastra",
                    "unit_weight": 0.25,
                    "intact_items": 1,
                    "location": "fridge",
                    "expiration_date": "2026-12-01",
                    "fallback_id": 4,
                    "new_product_unit": "kg",
                    "new_product_macros": {
                        "Kcal_100g": 168.0,
                        "Protein_100g": 24.5,
                        "Carbs_100g": 8.2,
                        "Fat_100g": 2.1,
                    },
                    "is_new_product": True,
                },
                {
                    "name": "Uova Fresche",
                    "canonical_name": "Uova Fresche",
                    "unit_weight": 1.0,
                    "intact_items": 12,
                    "location": "fridge",
                    "expiration_date": "2026-11-20",
                    "fallback_id": 2,
                    "new_product_unit": None,
                    "new_product_macros": None,
                    "is_new_product": False,
                },
            ]

            with patch("meal_planner.gui.app.ReceiptReconciliationDialog") as mock_dialog:
                mock_dlg_inst = MagicMock()
                mock_dlg_inst.result = reconciled_items
                mock_dialog.return_value = mock_dlg_inst

                app._on_receipt_review_saved(raw_items)

            # 1. Verify receipt log output in app
            log_text = app._receipt_log.get("1.0", tk.END)
            self.assertIn("Successfully imported 4 item(s) into inventory", log_text)
            self.assertIn("1 new product(s)", log_text)
            self.assertIn("1 new alias(es)", log_text)

            # 2. Verify status bar message
            self.assertIn("Imported 4 receipt item(s) into inventory.", app.status.get())

            # 3. Verify SQLite tables
            conn = get_connection(self.db_path)
            cur = conn.cursor()
            inv_rows = cur.execute("SELECT ID, Unit_Weight, Intact_Items, Location, Expiration_Date FROM INVENTORY").fetchall()
            alias_rows = cur.execute("SELECT Name, ID FROM ALIASES WHERE Name IN ('latte scremato bio', 'Seitan Alla Piastra')").fetchall()
            new_prod = cur.execute("SELECT ID, Unit, Kcal_100g FROM PRODUCTS WHERE ID = 4").fetchone()
            conn.close()

            # 4 rows in INVENTORY
            self.assertEqual(len(inv_rows), 4)
            # Both new aliases recorded
            self.assertEqual(len(alias_rows), 2)
            # New product registered
            self.assertIsNotNone(new_prod)
            self.assertEqual(new_prod, (4, "kg", 168.0))

            # 4. Verify GUI pantry treeview was refreshed and contains items
            pantry_items = app.t_disp.tv.get_children()
            self.assertGreaterEqual(len(pantry_items), 4)

        finally:
            try:
                app.destroy()
            except Exception:
                pass

    def test_app_on_receipt_review_saved_user_cancels(self):
        """Verify App._on_receipt_review_saved handles dialog cancellation cleanly."""
        app = App()
        app.db_path = self.db_path
        try:
            with patch("meal_planner.gui.app.ReceiptReconciliationDialog") as mock_dialog:
                mock_dlg_inst = MagicMock()
                mock_dlg_inst.result = None  # User clicked cancel
                mock_dialog.return_value = mock_dlg_inst

                app._on_receipt_review_saved([("Penne Rigate", 0.5, "kg", 1)])

            log_text = app._receipt_log.get("1.0", tk.END)
            self.assertIn("Reconciliation cancelled by user. No items imported.", log_text)

            conn = get_connection(self.db_path)
            count = conn.cursor().execute("SELECT COUNT(*) FROM INVENTORY").fetchone()[0]
            conn.close()
            self.assertEqual(count, 0)
        finally:
            try:
                app.destroy()
            except Exception:
                pass

    def test_app_on_receipt_review_saved_partial_failure_resilience(self):
        """Verify if one item in the list fails DB insertion, others proceed and failure is logged."""
        app = App()
        app.db_path = self.db_path
        try:
            reconciled_items = [
                # Valid item 1
                {
                    "name": "Penne Rigate",
                    "canonical_name": "Penne Rigate",
                    "unit_weight": 0.5,
                    "intact_items": 1,
                    "location": "pantry",
                    "expiration_date": None,
                    "fallback_id": 1,
                    "is_new_product": False,
                },
                # Faulty item: missing fallback_id for unknown product -> raises ProductNotFound
                {
                    "name": "Mystery Unknown Item",
                    "canonical_name": "Mystery Unknown Item",
                    "unit_weight": 1.0,
                    "intact_items": 1,
                    "location": "pantry",
                    "expiration_date": None,
                    "fallback_id": None,
                    "is_new_product": False,
                },
                # Valid item 2
                {
                    "name": "Latte Parz Scremato",
                    "canonical_name": "Latte Parz Scremato",
                    "unit_weight": 1.0,
                    "intact_items": 2,
                    "location": "fridge",
                    "expiration_date": None,
                    "fallback_id": 3,
                    "is_new_product": False,
                },
            ]

            with patch("meal_planner.gui.app.ReceiptReconciliationDialog") as mock_dialog:
                mock_dlg_inst = MagicMock()
                mock_dlg_inst.result = reconciled_items
                mock_dialog.return_value = mock_dlg_inst

                app._on_receipt_review_saved([("dummy", 1, "kg", 1)])

            log_text = app._receipt_log.get("1.0", tk.END)
            # Faulty item logged
            self.assertIn("[ERROR] Failed to add 'Mystery Unknown Item'", log_text)
            # The other 2 items successfully imported
            self.assertIn("Successfully imported 2 item(s) into inventory", log_text)

            conn = get_connection(self.db_path)
            count = conn.cursor().execute("SELECT COUNT(*) FROM INVENTORY").fetchone()[0]
            conn.close()
            self.assertEqual(count, 2)
        finally:
            try:
                app.destroy()
            except Exception:
                pass

    # =========================================================================
    # PART 6: COMPLETE END-TO-END AUDIT TRAIL PIPELINE
    # =========================================================================

    def test_complete_receipt_file_to_final_database_pipeline(self):
        """End-to-End Test: Simulates file -> OCR Voce -> FinestraModifica -> Reconciliation -> DB."""
        # 1. OCR outputs raw detected items
        raw_ocr_voci = [
            Voce("barilla penne", "500", "g", "2", 0.98),
            Voce("latte parz scremato", "1000", "ml", "3", 0.92),
            Voce("biscotti avena bio", "300", "g", "1", 0.85),
        ]

        with tempfile.TemporaryDirectory() as tmp_dir:
            out_file = Path(tmp_dir) / "receipt_audit.txt"

            # 2. App receives the OCR items and opens FinestraModifica
            app = App()
            app.db_path = self.db_path
            try:
                top_fm = tk.Toplevel(app)
                captured_for_reconciliation = []

                fm = FinestraModifica(
                    top_fm, raw_ocr_voci, out_file, "Audit Receipt",
                    on_save=captured_for_reconciliation.extend
                )

                # Simulate user correcting row 0's packages from 2 to 4
                fm.righe[0]["vars"]["confezioni"].set("4")

                # User saves the first review window
                saved_fm = fm.salva()
                self.assertTrue(saved_fm)
                self.assertTrue(out_file.exists())
                self.assertEqual(len(captured_for_reconciliation), 3)

                # Now simulate user action in ReceiptReconciliationDialog:
                # - Barilla Penne (0.5 kg, 4 packs) -> maps to Penne Rigate (ID 1)
                # - Latte Parz Scremato (1.0 L, 3 packs) -> maps to Latte Parz Scremato (ID 3)
                # - Biscotti Avena Bio (0.3 kg, 1 pack) -> New Product (ID 4) with macros
                reconciled = [
                    {
                        "name": "barilla penne",
                        "canonical_name": "Penne Rigate",
                        "unit_weight": 0.5,
                        "intact_items": 4,
                        "location": "pantry",
                        "expiration_date": "2027-12-31",
                        "fallback_id": 1,
                        "is_new_product": False,
                    },
                    {
                        "name": "latte parz scremato",
                        "canonical_name": "Latte Parz Scremato",
                        "unit_weight": 1.0,
                        "intact_items": 3,
                        "location": "fridge",
                        "expiration_date": "2026-11-28",
                        "fallback_id": 3,
                        "is_new_product": False,
                    },
                    {
                        "name": "biscotti avena bio",
                        "canonical_name": "biscotti avena bio",
                        "unit_weight": 0.3,
                        "intact_items": 1,
                        "location": "pantry",
                        "expiration_date": "2027-05-10",
                        "fallback_id": 4,
                        "new_product_unit": "kg",
                        "new_product_macros": {
                            "Kcal_100g": 460.0,
                            "Protein_100g": 9.5,
                            "Carbs_100g": 65.0,
                            "Fat_100g": 18.0,
                        },
                        "is_new_product": True,
                    },
                ]

                # Ingest reconciled batch into DB
                with patch("meal_planner.gui.app.ReceiptReconciliationDialog") as mock_dialog:
                    mock_dlg_inst = MagicMock()
                    mock_dlg_inst.result = reconciled
                    mock_dialog.return_value = mock_dlg_inst

                    app._on_receipt_review_saved(captured_for_reconciliation)

                # 3. Final SQL verification
                conn = get_connection(self.db_path)
                cur = conn.cursor()

                # Verify INVENTORY rows
                inv = cur.execute(
                    "SELECT ID, Unit_Weight, Intact_Items, Location, Expiration_Date FROM INVENTORY ORDER BY ID"
                ).fetchall()
                self.assertEqual(len(inv), 3)
                self.assertEqual(inv[0], (1, 0.5, 4, "pantry", "2027-12-31"))
                self.assertEqual(inv[1], (3, 1.0, 3, "fridge", "2026-11-28"))
                self.assertEqual(inv[2], (4, 0.3, 1, "pantry", "2027-05-10"))

                # Verify PRODUCTS has the new biscuits
                prod = cur.execute("SELECT ID, Unit, Kcal_100g, Protein_100g FROM PRODUCTS WHERE ID = 4").fetchone()
                self.assertEqual(prod, (4, "kg", 460.0, 9.5))

                # Verify ALIASES has all items (case-insensitive check)
                aliases = {name.lower(): pid for name, pid in cur.execute("SELECT Name, ID FROM ALIASES").fetchall()}
                self.assertEqual(aliases.get("barilla penne"), 1)
                self.assertEqual(aliases.get("latte parz scremato"), 3)
                self.assertEqual(aliases.get("biscotti avena bio"), 4)

                conn.close()

            finally:
                try:
                    top_fm.destroy()
                except Exception:
                    pass
                try:
                    app.destroy()
                except Exception:
                    pass




# ======================================================================
# SECTION: tests/test_reconciliation_adversarial.py
# ======================================================================

"""Adversarial stress-testing suite for ReceiptReconciliationDialog and dialog preservation.

Empirical verification of Milestone 3:
1. Piece products (`pcs`): unit_weight = 1.0, intact_items = max(1, int(round(weight * packs))).
2. Cancellation behavior: self.result is None, window destroyed on cancel/close/escape.
3. Preservation of existing dialogs (AliasMapDialog, ProductDialog, MealPrepDialog, ChoiceDialog).
4. Comma decimal entry (e.g. '1,75' -> 1.75).
5. Reactive status updates, catalog selection, and ID assignment deduplication.
6. Boundary conditions and invalid input rejection.
"""

from datetime import date
import math
import os
import sqlite3
import tempfile
import tkinter as tk
from unittest.mock import patch
import unittest

from meal_planner.db.schema import create_schema
from meal_planner.gui.dialogs import (
    AliasMapDialog,
    ChoiceDialog,
    COLOR_RECOGNIZED,
    COLOR_UNRECOGNIZED,
    MealPrepDialog,
    ProductDialog,
    ReceiptReconciliationDialog,
)


class TestReceiptReconciliationAdversarial(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tk.Tk()
        cls.root.withdraw()

        # Temporary DB with realistic catalog data
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.db_path = os.path.join(cls.temp_dir.name, "test_reconciliation.db")
        conn = sqlite3.connect(cls.db_path)
        create_schema(conn)
        cur = conn.cursor()
        # Add products
        cur.execute(
            """INSERT INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Sugars_100g, Fat_100g, Saturated_fat_100g, Fiber_100g, Salt_100g)
               VALUES (1, 'kg', 350.0, 12.0, 71.0, 3.0, 1.5, 0.3, 3.0, 0.01)"""
        )
        cur.execute(
            """INSERT INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Sugars_100g, Fat_100g, Saturated_fat_100g, Fiber_100g, Salt_100g)
               VALUES (2, 'pcs', 143.0, 12.6, 0.7, 0.7, 9.9, 3.1, 0.0, 0.3)"""
        )
        cur.execute(
            """INSERT INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Sugars_100g, Fat_100g, Saturated_fat_100g, Fiber_100g, Salt_100g)
               VALUES (3, 'L', 42.0, 3.4, 4.8, 4.8, 1.0, 0.6, 0.0, 0.1)"""
        )
        # Add aliases
        cur.execute("INSERT INTO ALIASES (Name, ID) VALUES ('Penne Rigate', 1)")
        cur.execute("INSERT INTO ALIASES (Name, ID) VALUES ('Barilla Penne', 1)")
        cur.execute("INSERT INTO ALIASES (Name, ID) VALUES ('Uova Fresche', 2)")
        cur.execute("INSERT INTO ALIASES (Name, ID) VALUES ('Latte Parz Scremato', 3)")
        conn.commit()
        conn.close()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:
            pass
        cls.temp_dir.cleanup()

    def setUp(self):
        self._msg_patcher = patch("meal_planner.gui.dialogs.messagebox.showerror")
        self.mock_showerror = self._msg_patcher.start()

    def tearDown(self):
        self._msg_patcher.stop()

    # ------------------------------------------------------------------
    # 1. PIECE PRODUCTS (`pcs`) TESTS
    # ------------------------------------------------------------------
    def test_piece_product_unit_weight_and_intact_items_standard(self):
        """Verify that for pcs: unit_weight = 1.0 and intact_items = max(1, int(round(weight * packs)))."""
        # 6 pieces per pack, 2 packs -> 12 items
        items = [("Uova Fresche", 6.0, "pcs", 2)]
        dlg = ReceiptReconciliationDialog(self.root, items, db_path=self.db_path, wait=False)
        self.assertTrue(dlg.save())
        self.assertIsNotNone(dlg.result)
        res = dlg.result[0]
        self.assertEqual(res["unit_weight"], 1.0)
        self.assertEqual(res["intact_items"], 12)
        self.assertEqual(res["canonical_name"], "Uova Fresche")
        self.assertFalse(res["is_new_product"])

    def test_piece_product_fractional_weight_rounding(self):
        """Test piece product with fractional inputs: 2.4 weight * 3 packs -> round(7.2) = 7 items."""
        items = [("Custom Piece", 1.0, "pcs", 3)]
        dlg = ReceiptReconciliationDialog(self.root, items, db_path=self.db_path, wait=False)
        # Explicitly set fractional weight in the UI entry
        dlg.rows[0]["weight_var"].set("2.4")
        # Fill macros for new product
        dlg.rows[0]["kcal_var"].set("100")
        dlg.rows[0]["prot_var"].set("5")
        dlg.rows[0]["carb_var"].set("10")
        dlg.rows[0]["fat_var"].set("2")

        self.assertTrue(dlg.save())
        res = dlg.result[0]
        self.assertEqual(res["unit_weight"], 1.0)
        self.assertEqual(res["intact_items"], 7)  # max(1, int(round(2.4 * 3))) = 7

    def test_piece_product_sub_unitary_weight_clamped_to_one(self):
        """Test piece product where weight * packs < 0.5 (rounds to 0), clamped to max(1, ...)."""
        items = [("Micro Piece", 0.2, "pcs", 1)]
        dlg = ReceiptReconciliationDialog(self.root, items, db_path=self.db_path, wait=False)
        dlg.rows[0]["kcal_var"].set("50")
        dlg.rows[0]["prot_var"].set("1")
        dlg.rows[0]["carb_var"].set("2")
        dlg.rows[0]["fat_var"].set("1")

        self.assertTrue(dlg.save())
        res = dlg.result[0]
        self.assertEqual(res["unit_weight"], 1.0)
        self.assertEqual(res["intact_items"], 1)  # max(1, 0) = 1

    def test_piece_product_unit_conversion_from_pz(self):
        """Test raw unit 'pz' converts to 'pcs' and correctly sets unit_weight=1.0 and intact_items."""
        items = [("Uova Fresche", 10.0, "pz", 1)]
        dlg = ReceiptReconciliationDialog(self.root, items, db_path=self.db_path, wait=False)
        self.assertEqual(dlg.rows[0]["unit_var"].get(), "pcs")
        self.assertTrue(dlg.save())
        res = dlg.result[0]
        self.assertEqual(res["unit_weight"], 1.0)
        self.assertEqual(res["intact_items"], 10)

    def test_weight_and_liquid_products_do_not_force_unit_weight_one(self):
        """Verify that kg and L products preserve unit_weight = weight and intact_items = packs."""
        items = [
            ("Penne Rigate", 0.5, "kg", 3),
            ("Latte Parz Scremato", 1.0, "L", 6),
        ]
        dlg = ReceiptReconciliationDialog(self.root, items, db_path=self.db_path, wait=False)
        self.assertTrue(dlg.save())
        r_pasta = dlg.result[0]
        r_milk = dlg.result[1]
        self.assertEqual(r_pasta["unit_weight"], 0.5)
        self.assertEqual(r_pasta["intact_items"], 3)
        self.assertEqual(r_milk["unit_weight"], 1.0)
        self.assertEqual(r_milk["intact_items"], 6)

    # ------------------------------------------------------------------
    # 2. CANCELLATION BEHAVIOR TESTS
    # ------------------------------------------------------------------
    def test_cancel_via_method(self):
        """Verify dlg.cancel() sets result to None and destroys window."""
        items = [("Penne Rigate", 0.5, "kg", 1)]
        dlg = ReceiptReconciliationDialog(self.root, items, db_path=self.db_path, wait=False)
        self.assertTrue(dlg.winfo_exists())
        dlg.cancel()
        self.assertIsNone(dlg.result)
        self.assertFalse(dlg.winfo_exists())

    def test_cancel_via_button_invoke(self):
        """Verify clicking Cancel button sets result to None and destroys window."""
        items = [("Penne Rigate", 0.5, "kg", 1)]
        dlg = ReceiptReconciliationDialog(self.root, items, db_path=self.db_path, wait=False)
        self.assertTrue(dlg.winfo_exists())
        dlg.btn_cancel.invoke()
        self.assertIsNone(dlg.result)
        self.assertFalse(dlg.winfo_exists())

    def test_cancel_via_wm_delete_window(self):
        """Verify WM_DELETE_WINDOW protocol sets result to None and destroys window."""
        items = [("Penne Rigate", 0.5, "kg", 1)]
        dlg = ReceiptReconciliationDialog(self.root, items, db_path=self.db_path, wait=False)
        # Trigger protocol callback
        dlg._on_cancel()
        self.assertIsNone(dlg.result)
        self.assertFalse(dlg.winfo_exists())

    def test_cancel_after_user_input(self):
        """Verify that canceling after filling data does NOT persist anything in result."""
        items = [("New Unknown Item", 1.0, "kg", 1)]
        dlg = ReceiptReconciliationDialog(self.root, items, db_path=self.db_path, wait=False)
        dlg.rows[0]["kcal_var"].set("250")
        dlg.rows[0]["prot_var"].set("10")
        dlg.rows[0]["carb_var"].set("40")
        dlg.rows[0]["fat_var"].set("5")
        dlg.cancel()
        self.assertIsNone(dlg.result)
        self.assertFalse(dlg.winfo_exists())

    # ------------------------------------------------------------------
    # 3. COMMA DECIMAL ENTRY TESTS
    # ------------------------------------------------------------------
    def test_comma_decimal_in_weight(self):
        """Verify '1,75' kg is parsed into float 1.75."""
        items = [("Penne Rigate", 1.0, "kg", 2)]
        dlg = ReceiptReconciliationDialog(self.root, items, db_path=self.db_path, wait=False)
        dlg.rows[0]["weight_var"].set("1,75")
        self.assertTrue(dlg.save())
        self.assertEqual(dlg.result[0]["unit_weight"], 1.75)
        self.assertEqual(dlg.result[0]["intact_items"], 2)

    def test_comma_decimal_in_macros(self):
        """Verify comma decimal in macros ('143,5', '12,6', '0,7', '9,9') are parsed as floats."""
        items = [("New Comma Food", 1.0, "kg", 1)]
        dlg = ReceiptReconciliationDialog(self.root, items, db_path=self.db_path, wait=False)
        dlg.rows[0]["weight_var"].set("0,75")
        dlg.rows[0]["kcal_var"].set("143,5")
        dlg.rows[0]["prot_var"].set("12,6")
        dlg.rows[0]["carb_var"].set("0,7")
        dlg.rows[0]["fat_var"].set("9,9")

        # Check status turns green reactively with commas
        self.assertEqual(dlg.rows[0]["status"], COLOR_RECOGNIZED)

        self.assertTrue(dlg.save())
        macros = dlg.result[0]["new_product_macros"]
        self.assertAlmostEqual(macros["Kcal_100g"], 143.5)
        self.assertAlmostEqual(macros["Protein_100g"], 12.6)
        self.assertAlmostEqual(macros["Carbs_100g"], 0.7)
        self.assertAlmostEqual(macros["Fat_100g"], 9.9)
        self.assertAlmostEqual(dlg.result[0]["unit_weight"], 0.75)

    def test_comma_in_packages_rejected(self):
        """Verify comma in packages field (e.g. '1,5') is rejected because packages must be integer."""
        items = [("Penne Rigate", 0.5, "kg", 1)]
        dlg = ReceiptReconciliationDialog(self.root, items, db_path=self.db_path, wait=False)
        dlg.rows[0]["packs_var"].set("1,5")
        self.assertFalse(dlg.save())
        self.assertIn("Packages must be an integer >= 1", dlg.last_error)
        self.assertIsNone(dlg.result)
        dlg.destroy()

    # ------------------------------------------------------------------
    # 4. PRESERVATION OF EXISTING DIALOGS
    # ------------------------------------------------------------------
    def test_preservation_product_dialog(self):
        """Verify ProductDialog exists, initializes, validates, and sets self.result."""
        with patch.object(tk.Misc, "wait_window", return_value=None):
            pd = ProductDialog(self.root, "Special Cheese", suggested_id=99)
            self.assertEqual(pd.form.v["id"].get(), "99")
            pd.form.v["unit"].set("kg")
            pd.form.v["kcal"].set("300")
            pd.form.v["prot"].set("20")
            pd.form.v["carb"].set("2")
            pd.form.v["fat"].set("25")
            pd.confirm()
            self.assertIsNotNone(pd.result)
            self.assertEqual(pd.result["fallback_id"], 99)
            self.assertEqual(pd.result["new_product_unit"], "kg")
            self.assertEqual(pd.result["new_product_macros"]["Kcal_100g"], 300.0)

    def test_preservation_choice_dialog(self):
        """Verify ChoiceDialog exists, initializes, and sets self.result upon confirm."""
        with patch.object(tk.Misc, "wait_window", return_value=None):
            cd = ChoiceDialog(self.root, "Select an Option", ["Option A", "Option B"])
            cd.v.set("Option B")
            cd.confirm()
            self.assertEqual(cd.result, "Option B")

    def test_preservation_alias_map_dialog(self):
        """Verify AliasMapDialog exists, initializes, and sets self.result."""
        with patch.object(tk.Misc, "wait_window", return_value=None):
            items = ["raw_pasta", "raw_milk"]
            known = ["Penne Rigate", "Latte"]
            amd = AliasMapDialog(self.root, items, known)
            amd._vars["raw_pasta"].set("Penne Rigate")
            amd._vars["raw_milk"].set("(skip)")
            amd._ok()
            self.assertEqual(amd.result["raw_pasta"], "Penne Rigate")
            self.assertIsNone(amd.result["raw_milk"])

    def test_preservation_meal_prep_dialog(self):
        """Verify MealPrepDialog exists, initializes, validates dates, and sets self.result."""
        with patch.object(tk.Misc, "wait_window", return_value=None):
            recipes = [(10, "Pasta al Pomodoro"), (20, "Insalata")]
            mpd = MealPrepDialog(self.root, recipes, default_cook_date="2026-11-04")
            self.assertEqual(mpd.v_cook.get(), "2026-11-04")
            mpd.v_dates.set("2026-11-05, 2026-11-06")
            mpd._ok()
            self.assertIsNotNone(mpd.result)
            self.assertEqual(mpd.result["recipe_id"], 10)
            self.assertEqual(mpd.result["cook_date"], "2026-11-04")
            self.assertEqual(mpd.result["target_dates"], ["2026-11-05", "2026-11-06"])

    # ------------------------------------------------------------------
    # 5. REACTIVITY AND CATALOG SELECTION TESTS
    # ------------------------------------------------------------------
    def test_selecting_catalog_product_autopopulates_and_turns_green(self):
        """Verify selecting a catalog product populates unit, macros, and switches color to green."""
        items = [("Unknown Item", 1.0, "kg", 1)]
        dlg = ReceiptReconciliationDialog(self.root, items, db_path=self.db_path, wait=False)
        row = dlg.rows[0]
        self.assertEqual(row["status"], COLOR_UNRECOGNIZED)
        self.assertEqual(row["kcal_var"].get(), "")

        # Select Penne Rigate
        row["product_var"].set("Penne Rigate")
        self.assertEqual(row["status"], COLOR_RECOGNIZED)
        self.assertEqual(row["kcal_var"].get(), "350")
        self.assertEqual(row["prot_var"].get(), "12")
        self.assertEqual(row["carb_var"].get(), "71")
        self.assertEqual(row["fat_var"].get(), "1.5")
        self.assertEqual(row["unit_var"].get(), "kg")

        # Switch back to [New Product] -> macros clear and status returns to Orange
        row["product_var"].set("[New Product]")
        self.assertEqual(row["status"], COLOR_UNRECOGNIZED)
        self.assertEqual(row["kcal_var"].get(), "")
        self.assertEqual(row["prot_var"].get(), "")

        # Enter macros manually -> status turns Green reactively
        row["kcal_var"].set("200")
        row["prot_var"].set("8")
        row["carb_var"].set("30")
        row["fat_var"].set("4")
        self.assertEqual(row["status"], COLOR_RECOGNIZED)

        # Invalidate one macro -> reverts to Orange
        row["fat_var"].set("")
        self.assertEqual(row["status"], COLOR_UNRECOGNIZED)
        dlg.destroy()

    def test_new_products_consecutive_fallback_ids_and_deduplication(self):
        """Verify consecutive fallback_ids for distinct new products and reuse for same-named new products."""
        items = [
            ("Alpha New", 1.0, "kg", 1),
            ("Beta New", 1.0, "kg", 1),
            ("Alpha New", 0.5, "kg", 2),
        ]
        dlg = ReceiptReconciliationDialog(self.root, items, db_path=self.db_path, wait=False)
        for r in dlg.rows:
            r["kcal_var"].set("100")
            r["prot_var"].set("5")
            r["carb_var"].set("10")
            r["fat_var"].set("2")

        self.assertTrue(dlg.save())
        res = dlg.result
        self.assertEqual(len(res), 3)

        # Fallback IDs: Alpha New gets ID, Beta New gets ID+1, second Alpha New reuses Alpha New's ID
        id_alpha_1 = res[0]["fallback_id"]
        id_beta = res[1]["fallback_id"]
        id_alpha_2 = res[2]["fallback_id"]

        self.assertEqual(id_beta, id_alpha_1 + 1)
        self.assertEqual(id_alpha_1, id_alpha_2)
        self.assertTrue(res[0]["is_new_product"])
        self.assertTrue(res[1]["is_new_product"])
        self.assertTrue(res[2]["is_new_product"])

    # ------------------------------------------------------------------
    # 6. ADVERSARIAL VALIDATION & ERROR HANDLING
    # ------------------------------------------------------------------
    def test_validation_rejects_empty_name(self):
        items = [("Penne Rigate", 1.0, "kg", 1)]
        dlg = ReceiptReconciliationDialog(self.root, items, db_path=self.db_path, wait=False)
        dlg.rows[0]["name_var"].set("   ")
        self.assertFalse(dlg.save())
        self.assertIn("Name cannot be empty", dlg.last_error)
        dlg.destroy()

    def test_validation_rejects_invalid_unit(self):
        items = [("Penne Rigate", 1.0, "kg", 1)]
        dlg = ReceiptReconciliationDialog(self.root, items, db_path=self.db_path, wait=False)
        dlg.rows[0]["unit_var"].set("meters")
        self.assertFalse(dlg.save())
        self.assertIn("Unit must be 'kg', 'L', or 'pcs'", dlg.last_error)
        dlg.destroy()

    def test_validation_rejects_nonpositive_weight(self):
        items = [("Penne Rigate", 1.0, "kg", 1)]
        dlg = ReceiptReconciliationDialog(self.root, items, db_path=self.db_path, wait=False)
        for bad_wt in ["0", "-1.5", "abc", "NaN"]:
            dlg.rows[0]["weight_var"].set(bad_wt)
            self.assertFalse(dlg.save(), f"Failed to reject invalid weight '{bad_wt}'")
            self.assertIn("Weight/Volume must be a positive number", dlg.last_error)
        dlg.destroy()

    def test_validation_rejects_nonpositive_packages(self):
        items = [("Penne Rigate", 1.0, "kg", 1)]
        dlg = ReceiptReconciliationDialog(self.root, items, db_path=self.db_path, wait=False)
        for bad_pk in ["0", "-2", "xyz"]:
            dlg.rows[0]["packs_var"].set(bad_pk)
            self.assertFalse(dlg.save(), f"Failed to reject invalid packages '{bad_pk}'")
            self.assertIn("Packages must be an integer >= 1", dlg.last_error)
        dlg.destroy()

    def test_validation_rejects_negative_or_nonfinite_macros(self):
        items = [("Unknown Food", 1.0, "kg", 1)]
        dlg = ReceiptReconciliationDialog(self.root, items, db_path=self.db_path, wait=False)
        # Missing macros
        self.assertFalse(dlg.save())
        self.assertIn("nutritional macros", dlg.last_error)

        # Negative macro
        dlg.rows[0]["kcal_var"].set("100")
        dlg.rows[0]["prot_var"].set("-5")
        dlg.rows[0]["carb_var"].set("20")
        dlg.rows[0]["fat_var"].set("2")
        self.assertFalse(dlg.save())
        self.assertIn("nutritional macros", dlg.last_error)

        # Non-numeric macro
        dlg.rows[0]["prot_var"].set("five")
        self.assertFalse(dlg.save())
        self.assertIn("nutritional macros", dlg.last_error)
        dlg.destroy()

    def test_validation_expiration_date_formats(self):
        """Verify expiration date: valid ISO calendar dates accepted, invalid formats/dates rejected."""
        items = [("Penne Rigate", 1.0, "kg", 1)]
        dlg = ReceiptReconciliationDialog(self.root, items, db_path=self.db_path, wait=False)

        # Invalid format
        dlg.rows[0]["exp_var"].set("31-12-2026")
        self.assertFalse(dlg.save())
        self.assertIn("Expiration date must be YYYY-MM-DD", dlg.last_error)

        # Invalid calendar date (Feb 31)
        dlg.rows[0]["exp_var"].set("2026-02-31")
        self.assertFalse(dlg.save())
        self.assertIn("Invalid date", dlg.last_error)

        # Non leap year Feb 29
        dlg.rows[0]["exp_var"].set("2025-02-29")
        self.assertFalse(dlg.save())
        self.assertIn("Invalid date", dlg.last_error)

        # Valid leap year Feb 29
        dlg.rows[0]["exp_var"].set("2024-02-29")
        self.assertTrue(dlg.save())
        self.assertEqual(dlg.result[0]["expiration_date"], "2024-02-29")

    def test_empty_expiration_date_results_in_none(self):
        """Verify that leaving expiration date blank sets expiration_date=None."""
        items = [("Penne Rigate", 1.0, "kg", 1)]
        dlg = ReceiptReconciliationDialog(self.root, items, db_path=self.db_path, wait=False)
        dlg.rows[0]["exp_var"].set("")
        self.assertTrue(dlg.save())
        self.assertIsNone(dlg.result[0]["expiration_date"])

    def test_empty_items_list(self):
        """Verify dialog handles empty items list gracefully."""
        dlg = ReceiptReconciliationDialog(self.root, [], db_path=self.db_path, wait=False)
        self.assertEqual(len(dlg.rows), 0)
        self.assertTrue(dlg.save())
        self.assertEqual(dlg.result, [])

    def test_reconciliation_dialog_banana_pieces_converted_to_kg(self):
        """Verify that 6 pz of bananas are converted to 0.9 kg rather than stored as 6.0 kg."""
        conn = sqlite3.connect(self.db_path)
        # Product ID 12: Banane (unit 'kg', Grams_per_piece = 150.0)
        conn.execute(
            """INSERT OR REPLACE INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g, Grams_per_piece)
               VALUES (12, 'kg', 89.0, 1.1, 23.0, 0.3, 150.0)"""
        )
        conn.execute("INSERT OR REPLACE INTO ALIASES (Name, ID) VALUES ('banane', 12)")
        conn.execute("INSERT OR REPLACE INTO ALIASES (Name, ID) VALUES ('Banane Cavendish', 12)")
        conn.commit()
        conn.close()

        # Case A: Auto-recognized item on receipt ('banane', 6 pz)
        items_auto = [("banane", 6.0, "pz", 1)]
        dlg_auto = ReceiptReconciliationDialog(self.root, items_auto, db_path=self.db_path, wait=False)
        try:
            row = dlg_auto.rows[0]
            self.assertEqual(row["unit_var"].get(), "kg")
            self.assertAlmostEqual(float(row["weight_var"].get()), 0.9)
            self.assertEqual(row["status"], COLOR_RECOGNIZED)
            self.assertTrue(dlg_auto.save())
            self.assertIsNotNone(dlg_auto.result)
            self.assertEqual(dlg_auto.result[0]["unit_weight"], 0.9)
            self.assertEqual(dlg_auto.result[0]["intact_items"], 1)
        finally:
            dlg_auto.destroy()

        # Case B: Unrecognized receipt item ('banane bio', 6 pz) mapped via Combobox
        items_manual = [("banane bio", 6.0, "pz", 1)]
        dlg_manual = ReceiptReconciliationDialog(self.root, items_manual, db_path=self.db_path, wait=False)
        try:
            row = dlg_manual.rows[0]
            self.assertEqual(row["product_var"].get(), "[New Product]")
            self.assertEqual(row["unit_var"].get(), "pcs")
            self.assertEqual(float(row["weight_var"].get()), 6.0)

            # User chooses 'Banane Cavendish' from dropdown
            row["product_var"].set("Banane Cavendish")
            dlg_manual._on_product_change(row)

            self.assertEqual(row["unit_var"].get(), "kg")
            self.assertAlmostEqual(float(row["weight_var"].get()), 0.9)
            self.assertEqual(row["status"], COLOR_RECOGNIZED)
            self.assertTrue(dlg_manual.save())
            self.assertEqual(dlg_manual.result[0]["unit_weight"], 0.9)
            self.assertEqual(dlg_manual.result[0]["intact_items"], 1)
        finally:
            dlg_manual.destroy()

    def test_validation_color_pulse_and_dynamic_recovery(self):
        """Verify smooth color flash, persistent warning tint, and recovery upon fix."""
        items = [("Penne Rigate", -1.0, "kg", 1), ("Penne Rigate", 1.0, "kg", 1)]
        dlg = ReceiptReconciliationDialog(self.root, items, db_path=self.db_path, wait=False)
        try:
            # 1. Invalid weight on row 0
            self.assertFalse(dlg.save())
            self.assertIn(dlg.rows[0]["ent_weight"], dlg._active_error_widgets)
            self.assertNotIn(dlg.rows[1]["ent_weight"], dlg._active_error_widgets)

            # 2. Fix row 0, invalidate row 1 packages
            dlg.rows[0]["weight_var"].set("1.5")
            dlg.rows[1]["packs_var"].set("0")
            self.assertFalse(dlg.save())
            self.assertNotIn(dlg.rows[0]["ent_weight"], dlg._active_error_widgets)
            self.assertIn(dlg.rows[1]["ent_packs"], dlg._active_error_widgets)
            self.assertEqual(dlg.rows[0]["ent_weight"].cget("bg"), dlg.color_std)

            # 3. Fix row 1 packages
            dlg.rows[1]["packs_var"].set("2")
            self.assertTrue(dlg.save())
            self.assertIsNone(dlg.last_error)
        finally:
            dlg.destroy()




# ======================================================================
# SECTION: tests/test_adversarial_reconciliation_feedback.py
# ======================================================================

"""Adversarial stress tests for ReceiptReconciliationDialog visual validation feedback and App OCR log label."""

import unittest
import tkinter as tk
from unittest.mock import patch, MagicMock

from meal_planner.gui.dialogs import (
    ReceiptReconciliationDialog,
    COLOR_RECOGNIZED,
    COLOR_UNRECOGNIZED,
)
from meal_planner.gui.app import App


class TestAdversarialReconciliationFeedback(unittest.TestCase):
    """Stress testing the validation feedback animation, color recovery, and OCR UI."""

    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()

    def tearDown(self):
        try:
            self.root.destroy()
        except Exception:
            pass

    def test_package_float_integer_strings_adversarial(self):
        """Float representations of integers ('1.0', '2,0') must be accepted, while fractional ('1.5') fails."""
        # 1. Valid float strings
        for valid_pack in ("1.0", "2,0", "3", " 4.00 "):
            dlg = ReceiptReconciliationDialog(
                self.root,
                [("Penne Rigate", 0.5, "kg", 1)],
                wait=False,
            )
            try:
                dlg.rows[0]["packs_var"].set(valid_pack)
                self.assertTrue(
                    dlg.save(),
                    f"Packages '{valid_pack}' should be accepted as integer",
                )
            finally:
                try:
                    dlg.destroy()
                except Exception:
                    pass

        # 2. Invalid fractional or negative packs
        for invalid_pack in ("1.5", "0.9", "0", "-1", "abc", "inf", "nan"):
            dlg = ReceiptReconciliationDialog(
                self.root,
                [("Penne Rigate", 0.5, "kg", 1)],
                wait=False,
            )
            try:
                dlg.rows[0]["packs_var"].set(invalid_pack)
                self.assertFalse(
                    dlg.save(),
                    f"Packages '{invalid_pack}' must be rejected",
                )
                self.assertIn(dlg.rows[0]["ent_packs"], dlg._active_error_widgets)
            finally:
                try:
                    dlg.destroy()
                except Exception:
                    pass

    def test_multi_field_simultaneous_failures_and_color_states(self):
        """All invalid fields on a new product must highlight; fixing them step-by-step restores neutral background."""
        items = [("[New Product]", -5.0, "invalid_unit", -2)]
        dlg = ReceiptReconciliationDialog(self.root, items, wait=False)
        try:
            # Name is empty, unit invalid, weight negative, packs negative, macros missing
            dlg.rows[0]["name_var"].set("")
            dlg.rows[0]["unit_var"].set("invalid_unit")
            self.assertFalse(dlg.save())

            # Verify all expected widgets are in error
            r = dlg.rows[0]
            expected_in_error = {
                r["ent_name"],
                r.get("frame_unit"),
                r["ent_weight"],
                r["ent_packs"],
                r["ent_kcal"],
                r["ent_prot"],
                r["ent_carb"],
                r["ent_fat"],
            }
            for w in expected_in_error:
                self.assertIn(w, dlg._active_error_widgets)

            # Let animation settle to yellow tint
            for _ in range(26):
                dlg.update()
                dlg.after(40)

            for w in expected_in_error:
                self.assertEqual(w.cget("bg"), dlg.color_yellow)

            # STEP 2: Partially fix name, unit, and weight. Leave packs and macros in error.
            r["name_var"].set("Farina Integrale")
            r["unit_var"].set("kg")
            r["weight_var"].set("1.0")

            self.assertFalse(dlg.save())

            # Farina name, unit frame, and weight should now be standard neutral color
            self.assertEqual(r["ent_name"].cget("bg"), dlg.color_std)
            self.assertEqual(r.get("frame_unit").cget("bg"), dlg.color_std)
            self.assertEqual(r["ent_weight"].cget("bg"), dlg.color_std)

            # Packs and macros must STILL be in error
            self.assertIn(r["ent_packs"], dlg._active_error_widgets)
            self.assertIn(r["ent_kcal"], dlg._active_error_widgets)

            # STEP 3: Fix packs and macros fully
            r["packs_var"].set("2")
            r["kcal_var"].set("340")
            r["prot_var"].set("12")
            r["carb_var"].set("68")
            r["fat_var"].set("2")

            self.assertTrue(dlg.save())
            self.assertIsNotNone(dlg.result)
            self.assertEqual(len(dlg.result), 1)
            self.assertEqual(dlg.result[0]["name"], "Farina Integrale")
            self.assertEqual(dlg.result[0]["unit_weight"], 1.0)
            self.assertEqual(dlg.result[0]["intact_items"], 2)
        finally:
            try:
                dlg.destroy()
            except Exception:
                pass

    def test_save_flooding_rapid_clicking_stability(self):
        """Clicking 'Save to Database' repeatedly in rapid succession must not crash or leak timers."""
        items = [("Penne Rigate", -1.0, "kg", 1)]
        dlg = ReceiptReconciliationDialog(self.root, items, wait=False)
        try:
            for _ in range(30):
                self.assertFalse(dlg.save())
                dlg.update()
            # Verify single timer remains active and correctly completes
            self.assertIsNotNone(dlg._anim_timer_id)
            for _ in range(26):
                dlg.update()
                dlg.after(40)
            self.assertIsNone(dlg._anim_timer_id)
            self.assertEqual(dlg.rows[0]["ent_weight"].cget("bg"), dlg.color_yellow)
        finally:
            try:
                dlg.destroy()
            except Exception:
                pass

    def test_window_destruction_mid_animation_no_tcl_errors(self):
        """Destroying the dialog during the color fade animation must cleanly cancel the timer."""
        items = [("Penne Rigate", -1.0, "kg", 1)]
        dlg = ReceiptReconciliationDialog(self.root, items, wait=False)
        dlg.save()
        self.assertIsNotNone(dlg._anim_timer_id)

        # Destroy immediately
        dlg.destroy()
        self.assertIsNone(dlg._anim_timer_id)

        # Spin event loop to ensure no orphan timer fires
        for _ in range(15):
            self.root.after(40)
            self.root.update()

    def test_small_and_large_screen_autosizing_safety(self):
        """Window geometry should comfortably fit columns without exceeding screen bounds."""
        items = [("Penne Rigate", 1.0, "kg", 1)]
        dlg = ReceiptReconciliationDialog(self.root, items, wait=False)
        try:
            dlg.update_idletasks()
            w = dlg.winfo_width()
            h = dlg.winfo_height()
            screen_w = dlg.winfo_screenwidth()
            screen_h = dlg.winfo_screenheight()

            self.assertLessEqual(w, screen_w)
            self.assertLessEqual(h, screen_h)
            self.assertGreaterEqual(w, 400)
            self.assertGreaterEqual(h, 300)
        finally:
            try:
                dlg.destroy()
            except Exception:
                pass

    def test_app_ocr_log_label_and_date_updates(self):
        """App must use a clean single-line label for OCR last receipt date without Text widget."""
        app = App()
        try:
            # 1. Verify lbl_last_receipt is a ttk.Label and not a Text widget
            self.assertTrue(hasattr(app, "lbl_last_receipt"))
            self.assertIsInstance(app.lbl_last_receipt, tk.ttk.Label)
            self.assertEqual(app._last_receipt_var.get(), "Last receipt loaded: None")

            # 2. Simulate processing a receipt and verify date update
            with patch("threading.Thread") as mock_thread:
                # Prevent background OCR thread from actually running
                mock_thread.return_value = MagicMock()
                app._process_receipt("/tmp/sample_scontrino.png")

            self.assertIn("Last receipt loaded: 202", app._last_receipt_var.get())
        finally:
            app.destroy()




# ======================================================================
# SECTION: tests/test_challenger_empirical_r1.py
# ======================================================================

"""
Empirical Adversarial Stress Test Suite for R1-R4
Targeting:
- Unit conversion cross-dimensional edge cases & invalid factor attacks
- Package overflow & type coercion fuzzing
- valida_voci sequence boundary & None fuzzing
- add_item mathematical sanity & SQLite constraint disambiguation
- ReceiptReconciliationDialog alias lookup, cross-dimensional scaling, and validation
- Database row preservation & read-only guarantee
"""

import math
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import tkinter as tk

from meal_planner.inventory import (
    convert_receipt_quantity_unit,
    map_receipt_item_to_add_item_args,
    add_item,
    get_catalog_for_reconciliation,
    get_next_product_id,
    InvalidInput,
    AliasConflict,
)
from meal_planner.receipts.pipeline import valida_voci, Voce
from meal_planner.gui.dialogs import ReceiptReconciliationDialog, COLOR_RECOGNIZED, COLOR_UNRECOGNIZED


class TestCrossDimensionalConversionAdversarial(unittest.TestCase):
    """Adversarially challenge convert_receipt_quantity_unit across all dimensions and edge cases."""

    def test_all_six_cross_dimensional_formulas(self):
        # 1. pcs -> kg (6 pcs of 150g = 0.9 kg)
        qty, unit = convert_receipt_quantity_unit(6, "pcs", "kg", grams_per_piece=150.0)
        self.assertEqual((qty, unit), (0.9, "kg"))

        # 2. kg -> pcs (0.9 kg with 150g/piece = 6 pcs)
        qty, unit = convert_receipt_quantity_unit(0.9, "kg", "pcs", grams_per_piece=150.0)
        self.assertEqual((qty, unit), (6.0, "pcs"))

        # 3. L -> kg (2.0 L with density 1.03 kg/L = 2.06 kg)
        qty, unit = convert_receipt_quantity_unit(2.0, "L", "kg", density_kg_per_l=1.03)
        self.assertEqual((qty, unit), (2.06, "kg"))

        # 4. kg -> L (2.06 kg with density 1.03 kg/L = 2.0 L)
        qty, unit = convert_receipt_quantity_unit(2.06, "kg", "L", density_kg_per_l=1.03)
        self.assertEqual((qty, unit), (2.0, "L"))

        # 5. pcs -> L (10 pcs of 100g = 1000g = 1.0 kg; density 0.8 kg/L -> 1.0 / 0.8 = 1.25 L)
        qty, unit = convert_receipt_quantity_unit(10, "pcs", "L", grams_per_piece=100.0, density_kg_per_l=0.8)
        self.assertEqual((qty, unit), (1.25, "L"))

        # 6. L -> pcs (1.25 L with density 0.8 kg/L = 1.0 kg = 1000g; 100g/pcs -> 10 pcs)
        qty, unit = convert_receipt_quantity_unit(1.25, "L", "pcs", grams_per_piece=100.0, density_kg_per_l=0.8)
        self.assertEqual((qty, unit), (10.0, "pcs"))

    def test_adversarial_factors_rejected(self):
        # Missing required factors
        with self.assertRaises(InvalidInput):
            convert_receipt_quantity_unit(6, "pcs", "kg")  # grams_per_piece missing
        with self.assertRaises(InvalidInput):
            convert_receipt_quantity_unit(2, "L", "kg")    # density missing
        with self.assertRaises(InvalidInput):
            convert_receipt_quantity_unit(5, "pcs", "L", grams_per_piece=100.0)  # density missing
        with self.assertRaises(InvalidInput):
            convert_receipt_quantity_unit(5, "pcs", "L", density_kg_per_l=1.0)   # grams missing

        # Negative, zero, non-finite, boolean factors
        adversarial_factors = [0, 0.0, -1, -150.0, float("inf"), float("-inf"), float("nan"), True, False, "invalid", []]
        for f in adversarial_factors:
            with self.assertRaises(InvalidInput):
                convert_receipt_quantity_unit(6, "pcs", "kg", grams_per_piece=f)
            with self.assertRaises(InvalidInput):
                convert_receipt_quantity_unit(2, "L", "kg", density_kg_per_l=f)

    def test_extreme_rounding_to_zero(self):
        # 1 piece of 0.0000001g rounds to 0.0000 kg -> must raise InvalidInput
        with self.assertRaises(InvalidInput):
            convert_receipt_quantity_unit(1, "pcs", "kg", grams_per_piece=0.0000001)


class TestPackageParsingAndOverflowAdversarial(unittest.TestCase):
    """Adversarially challenge map_receipt_item_to_add_item_args and valida_voci."""

    def test_map_receipt_item_overflow_fuzzing(self):
        # Floating point and string infinities and huge numbers
        for bad_pkg in [float("inf"), float("-inf"), float("nan"), "inf", "-inf", "nan", 10**100, 2**64, True, False, -5, "-1"]:
            args = map_receipt_item_to_add_item_args("test", 500, "g", packages=bad_pkg)
            self.assertEqual(args["intact_items"], 1)

    def test_map_receipt_item_pieces_overflow_safeguard(self):
        # Enormous piece quantity should not crash with unhandled OverflowError
        args = map_receipt_item_to_add_item_args("test", 10**40, "pcs", packages=1)
        self.assertIsInstance(args["intact_items"], int)
        self.assertGreaterEqual(args["intact_items"], 1)

    def test_valida_voci_sequence_robustness(self):
        # None sequence
        valid, invalid = valida_voci(None)
        self.assertEqual(valid, [])
        self.assertEqual(invalid, [])

        # Sequence containing None
        mixed = [None, Voce("pasta", "500", "g", "1.0", None), None]
        valid, invalid = valida_voci(mixed)
        self.assertEqual(len(valid), 1)
        self.assertEqual(valid[0][3], 1)

        # Accept integer-equivalent float strings
        item_one_point_zero = [Voce("latte", "1", "L", "2.0", None)]
        valid, _ = valida_voci(item_one_point_zero)
        self.assertEqual(len(valid), 1)
        self.assertEqual(valid[0][3], 2)

        # Reject non-integer float strings
        item_one_point_five = [Voce("latte", "1", "L", "1.5", None)]
        valid, invalid = valida_voci(item_one_point_five)
        self.assertEqual(len(valid), 0)
        self.assertEqual(len(invalid), 1)


class TestAddItemSanityAndDisambiguationAdversarial(unittest.TestCase):
    """Adversarially challenge add_item numerical validation and SQLite error disambiguation."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("""
            CREATE TABLE PRODUCTS (
                ID INTEGER PRIMARY KEY,
                Unit TEXT NOT NULL,
                Kcal_100g REAL NOT NULL,
                Protein_100g REAL NOT NULL,
                Carbs_100g REAL NOT NULL,
                Fat_100g REAL NOT NULL,
                Sugars_100g REAL,
                Saturated_fat_100g REAL,
                Fiber_100g REAL,
                Salt_100g REAL,
                Grams_per_piece REAL,
                Density_kg_per_L REAL
            )
        """)
        self.conn.execute("CREATE TABLE ALIASES (Name TEXT PRIMARY KEY, ID INTEGER NOT NULL)")
        self.conn.execute("CREATE TABLE LOCATIONS (Name TEXT PRIMARY KEY)")
        self.conn.execute("""
            CREATE TABLE INVENTORY (
                Row_ID INTEGER PRIMARY KEY AUTOINCREMENT,
                ID INTEGER NOT NULL,
                Unit_Weight REAL NOT NULL,
                Intact_Items INTEGER NOT NULL,
                Open_Remainder REAL NOT NULL DEFAULT 0.0,
                Location TEXT NOT NULL,
                Expiration_Date TEXT
            )
        """)
        self.conn.execute("CREATE TABLE SHOPPING_LIST (ID INTEGER PRIMARY KEY, Reference_Name TEXT, Missing_Quantity REAL)")
        self.conn.execute("INSERT INTO LOCATIONS VALUES ('pantry')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_add_item_rejects_nan_and_inf_unit_weight_with_invalid_input(self):
        valid_macros = {"Kcal_100g": 100.0, "Protein_100g": 10.0, "Carbs_100g": 20.0, "Fat_100g": 5.0}
        for bad_weight in [float("nan"), float("inf"), float("-inf"), 0, -1.0, True, False, "1.0", None]:
            with self.assertRaises(InvalidInput):
                add_item(
                    "New Item",
                    bad_weight,
                    1,
                    "pantry",
                    fallback_id=1,
                    new_product_unit="kg",
                    new_product_macros=valid_macros,
                    conn=self.conn,
                )

    def test_add_item_rejects_invalid_intact_items_with_invalid_input(self):
        valid_macros = {"Kcal_100g": 100.0, "Protein_100g": 10.0, "Carbs_100g": 20.0, "Fat_100g": 5.0}
        for bad_intact in [0, -1, 1.5, float("nan"), float("inf"), True, False, "1", None]:
            with self.assertRaises(InvalidInput):
                add_item(
                    "New Item",
                    1.0,
                    bad_intact,
                    "pantry",
                    fallback_id=1,
                    new_product_unit="kg",
                    new_product_macros=valid_macros,
                    conn=self.conn,
                )

    def test_add_item_rejects_negative_or_nonfinite_macros(self):
        # Negative Kcal
        with self.assertRaises(InvalidInput):
            add_item(
                "New Item", 1.0, 1, "pantry", fallback_id=1, new_product_unit="kg",
                new_product_macros={"Kcal_100g": -100.0, "Protein_100g": 10.0, "Carbs_100g": 20.0, "Fat_100g": 5.0},
                conn=self.conn,
            )
        # NaN protein
        with self.assertRaises(InvalidInput):
            add_item(
                "New Item", 1.0, 1, "pantry", fallback_id=1, new_product_unit="kg",
                new_product_macros={"Kcal_100g": 100.0, "Protein_100g": float("nan"), "Carbs_100g": 20.0, "Fat_100g": 5.0},
                conn=self.conn,
            )

    def test_add_item_disambiguates_alias_conflict_from_not_null(self):
        # 1. Verify NaN does not raise AliasConflict but InvalidInput
        with self.assertRaises(InvalidInput):
            add_item("Apple", float("nan"), 1, "pantry", conn=self.conn)

        # 2. Verify UNIQUE constraint error is mapped to AliasConflict
        valid_macros = {"Kcal_100g": 100.0, "Protein_100g": 10.0, "Carbs_100g": 20.0, "Fat_100g": 5.0}
        add_item("Apple Unique", 1.0, 1, "pantry", fallback_id=1, new_product_unit="kg", new_product_macros=valid_macros, conn=self.conn)

        # In case a concurrent or duplicate insert causes UNIQUE constraint failure:
        class FakeCursor:
            def execute(self, *args, **kwargs):
                raise sqlite3.IntegrityError("UNIQUE constraint failed: ALIASES.Name")
        class FakeConn:
            def cursor(self):
                return FakeCursor()
            def rollback(self):
                pass
        
        with self.assertRaises(AliasConflict):
            add_item("Apple Unique 2", 1.0, 1, "pantry", fallback_id=99, conn=FakeConn())

        # 3. Verify NOT NULL constraint violation raises InvalidInput (not AliasConflict)
        class FakeNotNullConn:
            def cursor(self):
                class FakeNotNullCursor:
                    def execute(self, *args, **kwargs):
                        raise sqlite3.IntegrityError("NOT NULL constraint failed: INVENTORY.Unit_Weight")
                return FakeNotNullCursor()
            def rollback(self):
                pass

        with self.assertRaises(InvalidInput):
            add_item("Apple Valid", 1.0, 1, "pantry", fallback_id=1, conn=FakeNotNullConn())


class TestReceiptReconciliationDialogAdversarial(unittest.TestCase):
    """Adversarially challenge ReceiptReconciliationDialog headless logic and propagation."""

    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db")
        self.db_path = self.tmp.name

        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE PRODUCTS (
                ID INTEGER PRIMARY KEY,
                Unit TEXT NOT NULL,
                Kcal_100g REAL NOT NULL,
                Protein_100g REAL NOT NULL,
                Carbs_100g REAL NOT NULL,
                Fat_100g REAL NOT NULL,
                Sugars_100g REAL,
                Saturated_fat_100g REAL,
                Fiber_100g REAL,
                Salt_100g REAL,
                Grams_per_piece REAL,
                Density_kg_per_L REAL
            )
        """)
        conn.execute("CREATE TABLE ALIASES (Name TEXT PRIMARY KEY, ID INTEGER NOT NULL)")
        conn.execute("CREATE TABLE LOCATIONS (Name TEXT PRIMARY KEY)")
        conn.execute("""
            CREATE TABLE INVENTORY (
                Row_ID INTEGER PRIMARY KEY AUTOINCREMENT,
                ID INTEGER NOT NULL,
                Unit_Weight REAL NOT NULL,
                Intact_Items INTEGER NOT NULL,
                Open_Remainder REAL NOT NULL DEFAULT 0.0,
                Location TEXT NOT NULL,
                Expiration_Date TEXT
            )
        """)
        conn.execute("CREATE TABLE SHOPPING_LIST (ID INTEGER PRIMARY KEY, Reference_Name TEXT, Missing_Quantity REAL)")
        conn.execute("INSERT INTO LOCATIONS VALUES ('pantry')")
        conn.execute("INSERT INTO LOCATIONS VALUES ('fridge')")
        
        # Product 1: Banana (kg, 150g/piece)
        conn.execute("""
            INSERT INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g, Grams_per_piece)
            VALUES (1, 'kg', 89.0, 1.1, 23.0, 0.3, 150.0)
        """)
        conn.execute("INSERT INTO ALIASES (Name, ID) VALUES ('Banane', 1)")
        conn.execute("INSERT INTO ALIASES (Name, ID) VALUES ('Banane Cavendish', 1)")

        # Product 2: Eggs (pcs)
        conn.execute("""
            INSERT INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g)
            VALUES (2, 'pcs', 143.0, 12.6, 0.7, 9.9)
        """)
        conn.execute("INSERT INTO ALIASES (Name, ID) VALUES ('Uova', 2)")
        conn.commit()
        conn.close()

    def tearDown(self):
        try:
            self.root.destroy()
        except Exception:
            pass
        self.tmp.close()

    def test_banana_6_pieces_scales_to_0_9_kg_and_does_not_save_unscaled(self):
        # 6 pieces of bananas on receipt
        items = [("Banane", 6.0, "pz", 1)]
        dlg = ReceiptReconciliationDialog(self.root, items, db_path=self.db_path, wait=False)
        try:
            row = dlg.rows[0]
            self.assertEqual(row["unit_var"].get(), "kg")
            self.assertAlmostEqual(float(row["weight_var"].get()), 0.9)
            self.assertTrue(dlg.save())
            res = dlg.result[0]
            # Must be scaled to 0.9 kg, NOT raw 6.0 kg!
            self.assertAlmostEqual(res["unit_weight"], 0.9)
            self.assertEqual(res["intact_items"], 1)
        finally:
            dlg.destroy()

    def test_alias_lookup_propagation_in_dropdown(self):
        # Item on receipt unrecognized: 'Banane Bio'
        items = [("Banane Bio", 6.0, "pz", 1)]
        dlg = ReceiptReconciliationDialog(self.root, items, db_path=self.db_path, wait=False)
        try:
            row = dlg.rows[0]
            # Mapping via alias 'Banane Cavendish'
            row["product_var"].set("Banane Cavendish")
            dlg._on_product_change(row)

            self.assertEqual(row["unit_var"].get(), "kg")
            self.assertAlmostEqual(float(row["weight_var"].get()), 0.9)
            self.assertEqual(row["status"], COLOR_RECOGNIZED)
            self.assertTrue(dlg.save())
            res = dlg.result[0]
            self.assertEqual(res["fallback_id"], 1)
            self.assertAlmostEqual(res["unit_weight"], 0.9)
        finally:
            dlg.destroy()

    def test_pieces_product_eggs_unit_weight_one_intact_items_count(self):
        # 12 eggs (pcs)
        items = [("Uova", 12.0, "pz", 1)]
        dlg = ReceiptReconciliationDialog(self.root, items, db_path=self.db_path, wait=False)
        try:
            self.assertTrue(dlg.save())
            res = dlg.result[0]
            self.assertEqual(res["unit_weight"], 1.0)
            self.assertEqual(res["intact_items"], 12)
        finally:
            dlg.destroy()

    @patch("meal_planner.gui.dialogs.messagebox.showerror")
    def test_dialog_validation_rejections(self, mock_error):
        # Weight <= 0
        dlg1 = ReceiptReconciliationDialog(self.root, [("Uova", 0, "pcs", 1)], db_path=self.db_path, wait=False)
        self.assertFalse(dlg1.save())
        self.assertIn("positive number", dlg1.last_error.lower())
        dlg1.destroy()

        # Packages <= 0
        dlg2 = ReceiptReconciliationDialog(self.root, [("Uova", 10, "pcs", 0)], db_path=self.db_path, wait=False)
        self.assertFalse(dlg2.save())
        self.assertIn("integer >= 1", dlg2.last_error.lower())
        dlg2.destroy()

        # Invalid date format
        dlg3 = ReceiptReconciliationDialog(self.root, [("Uova", 10, "pcs", 1)], db_path=self.db_path, wait=False)
        dlg3.rows[0]["exp_var"].set("2026/12/31")
        self.assertFalse(dlg3.save())
        self.assertIn("yyyy-mm-dd", dlg3.last_error.lower())
        dlg3.destroy()


class TestReceiptEdgeCasesAndCorruptFiles(unittest.TestCase):
    """Edge cases for fuzzy catalog correction, corrupt files, empty saves, and detail line parsing."""

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

    def test_correggi_con_catalogo_fuzzy_edge_cases(self):
        """correggi_con_catalogo handles empty catalogs, empty inputs, noise, and ties gracefully."""
        from meal_planner.receipts.reader import correggi_con_catalogo

        # Empty catalog returns unchanged name
        self.assertEqual(correggi_con_catalogo("Pasta", []), "Pasta")
        # Empty input string returns empty string
        self.assertEqual(correggi_con_catalogo("", ["Pasta"]), "")
        # Punctuation and noisy input
        self.assertEqual(correggi_con_catalogo("***PENNE---RIGATE***", ["Penne Rigate"]), "Penne Rigate")
        # Tie-breaker: later matching item in catalog with >= score wins
        self.assertEqual(correggi_con_catalogo("pasta", ["pasta a", "pasta b"], soglia=50.0), "pasta b")

    def test_corrupt_and_zero_byte_receipt_files(self):
        """0-byte and corrupted receipt image files fail gracefully without unhandled crashes."""
        from meal_planner.receipts.reader import leggi_scontrino
        from meal_planner.receipts.pipeline import leggi_voci

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"")
            zero_path = f.name
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(b"this is corrupted file content not an image")
            corrupt_path = f.name

        try:
            r0 = leggi_scontrino(zero_path)
            self.assertEqual(r0.errore, "Impossibile caricare l'immagine")
            self.assertEqual(len(r0.articoli), 0)

            rc = leggi_scontrino(corrupt_path)
            self.assertEqual(rc.errore, "Impossibile caricare l'immagine")
            self.assertEqual(len(rc.articoli), 0)

            with self.assertRaises(RuntimeError) as ctx:
                leggi_voci(corrupt_path)
            self.assertIn("impossibile caricare l'immagine", str(ctx.exception).lower())

            r_none = leggi_scontrino("/path/that/does/not/exist.png")
            self.assertEqual(r_none.errore, "Impossibile caricare l'immagine")
        finally:
            if os.path.exists(zero_path):
                os.remove(zero_path)
            if os.path.exists(corrupt_path):
                os.remove(corrupt_path)

    def test_empty_receipt_and_empty_review_save(self):
        """Calling _on_receipt_review_saved with empty or None items exits cleanly without modifying the DB."""
        app = App()
        app.withdraw()
        try:
            conn = get_connection(self.db_path)
            c_inv_before = conn.execute("SELECT COUNT(*) FROM INVENTORY").fetchone()[0]
            c_log_before = conn.execute("SELECT COUNT(*) FROM RECEIPT_LOGS").fetchone()[0]
            conn.close()

            # Empty list
            app._on_receipt_review_saved([])
            # None
            app._on_receipt_review_saved(None)

            conn = get_connection(self.db_path)
            c_inv_after = conn.execute("SELECT COUNT(*) FROM INVENTORY").fetchone()[0]
            c_log_after = conn.execute("SELECT COUNT(*) FROM RECEIPT_LOGS").fetchone()[0]
            conn.close()

            self.assertEqual(c_inv_before, c_inv_after)
            self.assertEqual(c_log_before, c_log_after)
        finally:
            try:
                app.destroy()
            except Exception:
                pass

    def test_receipt_detail_lines_without_article(self):
        """parsa_righe skips orphan detail lines without raising IndexError."""
        from meal_planner.receipts.reader import Riga, parsa_righe

        righe = [
            Riga(0, "2 X CAD 1.50", 0.9),
            Riga(25, "PASTA RIGATA 500G", 0.9),
        ]
        articoli = parsa_righe(righe)
        self.assertEqual(len(articoli), 1)
        self.assertEqual(articoli[0].nome, "PASTA RIGATA")
        self.assertEqual(articoli[0].qta, 500.0)
        self.assertEqual(articoli[0].unita, "g")

        # Only detail lines without preceding article
        detail_only = [
            Riga(0, "2 X CAD 1.50", 0.9),
            Riga(15, "1.250 KG", 0.9),
        ]
        self.assertEqual(parsa_righe(detail_only), [])


# ======================================================================
# Extracted from meal_planner/receipts/pipeline.py inline tests
# ======================================================================

def _box(x, y, w=100, h=30):
    return [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]

_OCR_FINTO = [(_box(10, 100), "LENTICCHIE", 0.95), (_box(10, 150), "TOFU BIO", 0.3),
              (_box(10, 190), "2 Cad", 0.9), (_box(10, 250), "BANANE", 0.9),
              (_box(10, 290), "0,838 kg x 1,99", 0.9), (_box(10, 350), "SUBTOTALE", 0.9)]

def _immagine_vuota(percorso: Path) -> None:
    import cv2
    import numpy as np
    cv2.imencode(".png", np.full((1000, 1000), 255, np.uint8))[1].tofile(str(percorso))


class TestReceiptPipelineStandalone(unittest.TestCase):
    def test_leggi_voci_tutto_minuscolo(self):
        from meal_planner.receipts import reader as ls
        from meal_planner.receipts.pipeline import leggi_voci, SOGLIA_BASSA_CONFIDENZA
        with patch.object(ls, "_esegui_ocr", return_value=_OCR_FINTO):
            with tempfile.TemporaryDirectory() as td:
                p = Path(td) / "s.png"
                _immagine_vuota(p)
                voci = leggi_voci(str(p))
                self.assertEqual([(v.nome, v.qta, v.unita, v.confezioni) for v in voci], [
                    ("lenticchie", "1", "pz", "1"), ("tofu bio", "1", "pz", "2"), ("banane", "0.838", "kg", "1")])
                self.assertTrue(all(v.nome == v.nome.lower() for v in voci))
                self.assertLess(voci[1].conf_ocr, SOGLIA_BASSA_CONFIDENZA)

    def test_leggi_voci_file_inesistente(self):
        from meal_planner.receipts.pipeline import leggi_voci
        with self.assertRaises(RuntimeError):
            leggi_voci("non_esiste.jpg")

    def test_validazione_ok_virgola_e_maiuscole(self):
        from meal_planner.receipts.pipeline import valida_voci, Voce
        items, errori = valida_voci([Voce("  LATTE   Intero ", "1,5", "L", "2"), Voce("", "x", "", "")])
        self.assertEqual(errori, [])
        self.assertEqual(items, [("latte intero", 1.5, "l", 2)])

    def test_validazione_errori(self):
        from meal_planner.receipts.pipeline import valida_voci, Voce
        _, errori = valida_voci([Voce("a", "abc"), Voce("b", "0"), Voce("c", "1", ""),
                                 Voce("d", "1", "pz", "0"), Voce("e", "1", "pz", "x")])
        self.assertEqual(len(errori), 5)
        self.assertTrue(all(e == e.lower() for e in errori))

    def test_scrittura_out_senza_maiuscole(self):
        from meal_planner.receipts.pipeline import scrivi_out
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "out.txt"
            scrivi_out([("Banane; Bio", 0.838, "KG", 1), ("uova", 6.0, "pz", 2)], out)
            testo = out.read_text(encoding="utf-8")
            self.assertEqual(testo, testo.lower())
            self.assertEqual(testo.splitlines()[1], "uova;6;pz;2")

    def test_scrittura_out_separatore_nel_nome_sostituito(self):
        from meal_planner.receipts.pipeline import valida_voci, Voce, scrivi_out
        with tempfile.TemporaryDirectory() as td:
            items, _ = valida_voci([Voce("banane; bio", "1")])
            scrivi_out(items, Path(td) / "o.txt")
            self.assertEqual((Path(td) / "o.txt").read_text(), "banane bio;1;pz;1\n")

    def test_main_senza_finestra(self):
        from meal_planner.receipts import reader as ls
        from meal_planner.receipts.pipeline import main
        with patch.object(ls, "_esegui_ocr", return_value=_OCR_FINTO):
            with tempfile.TemporaryDirectory() as td:
                p, out = Path(td) / "s.png", Path(td) / "out.txt"
                _immagine_vuota(p)
                self.assertEqual(main([str(p), "--out", str(out), "--senza-finestra"]), 0)
                self.assertEqual(out.read_text(encoding="utf-8"), "lenticchie;1;pz;1\ntofu bio;1;pz;2\nbanane;0.838;kg;1\n")

    def test_main_file_mancante(self):
        from meal_planner.receipts.pipeline import main
        self.assertEqual(main(["non_esiste.png"]), 2)

    def test_finestra_forza_minuscolo_e_salva(self):
        import tkinter as tk
        from meal_planner.receipts.pipeline import FinestraModifica, Voce
        try:
            root = tk.Tk()
        except tk.TclError:
            self.skipTest("No display available")
        try:
            with tempfile.TemporaryDirectory() as td:
                out = Path(td) / "out.txt"
                f = FinestraModifica(root, [Voce("pere", "1", "kg", "1", 0.2)], out, "Titolo Con Maiuscole")
                self.assertEqual(root.title(), "titolo con maiuscole")
                f.righe[0]["vars"]["nome"].set("PERE Abate")
                self.assertEqual(f.righe[0]["vars"]["nome"].get(), "pere abate")
                f.righe[0]["vars"]["qta"].set("0,5")
                f.aggiungi_riga(Voce("riso", "boh"))
                self.assertFalse(f.salva())
                self.assertIn("invalid quantity", f.stato.get())
                f.rimuovi_riga(f.righe[-1])
                self.assertTrue(f.salva())
                self.assertEqual(out.read_text(), "pere abate;0.5;kg;1\n")
                self.assertEqual(f.stato.get(), f.stato.get().lower())
        finally:
            try:
                root.destroy()
            except Exception:
                pass

    def test_finestra_senza_articoli_ha_una_riga_vuota(self):
        import tkinter as tk
        from meal_planner.receipts.pipeline import FinestraModifica
        try:
            root = tk.Tk()
        except tk.TclError:
            self.skipTest("No display available")
        try:
            with tempfile.TemporaryDirectory() as td:
                f = FinestraModifica(root, [], Path(td) / "out.txt", "x")
                self.assertEqual(len(f.righe), 1)
                self.assertTrue(f.salva())
                self.assertEqual((Path(td) / "out.txt").read_text(), "")
        finally:
            try:
                root.destroy()
            except Exception:
                pass

    def test_finestra_on_save_callback_e_chiusura(self):
        import tkinter as tk
        from meal_planner.receipts.pipeline import FinestraModifica, Voce
        try:
            root = tk.Tk()
        except tk.TclError:
            self.skipTest("No display available")
        chiamate = []
        try:
            with tempfile.TemporaryDirectory() as td:
                out = Path(td) / "out.txt"
                voci = [Voce("latte", "1,5", "l", "2")]
                f = FinestraModifica(root, voci, out, "test", on_save=lambda items: chiamate.append(items))
                f.aggiungi_riga(Voce("pane", "non-valido"))
                self.assertFalse(f.salva())
                self.assertEqual(len(chiamate), 0)
                f.rimuovi_riga(f.righe[-1])
                self.assertTrue(f.salva())
                self.assertEqual(chiamate, [[("latte", 1.5, "l", 2)]])
                self.assertEqual(out.read_text(), "latte;1.5;l;2\n")
        finally:
            try:
                root.destroy()
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main()
