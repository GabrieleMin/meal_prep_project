"""
tests/test_domain_core.py

Consolidated core domain test suite covering:
1. Configuration management (meal_planner.config)
2. Inventory database operations & business logic (meal_planner.inventory)
3. Nutrition calculation engine & views (meal_planner.engine)
4. Meal planner algorithmic engine for days & weeks (meal_planner.planner)
5. C extension speedups, COW semantics & performance benchmarks
"""

from pathlib import Path
import contextlib
import io
import json
import math
import os
import random
import shutil
import sqlite3
import tempfile
import time
import unittest

from meal_planner import config
from meal_planner.gui import theme
from meal_planner.db.connection import get_connection
from meal_planner.db.schema import create_schema, migrate
from meal_planner.inventory import (
    add_item,
    consume_quantity,
    get_available_inventory,
    get_locations,
    add_location,
    rename_location,
    delete_location,
    ProductNotFound,
    InvalidInput,
    convert_receipt_quantity_unit,
    get_catalog_for_reconciliation,
    get_next_product_id,
    map_receipt_item_to_add_item_args,
    get_exclusion_list,
    _round_down_to_hundredth as inv_round_down,
)
from meal_planner import engine as m
from meal_planner.engine import MACROS
from meal_planner.engine import VirtualInventory
from meal_planner.planner import _score
from meal_planner import planner as plan_day
from meal_planner import planner as plan_week
from scripts.seed_inventory import seed as seed_products
from scripts.seed_recipes import seed as seed_recipes_data


# ======================================================================
# 1. Config Tests
# ======================================================================
class TestConfig(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.test_cfg_path = Path(self.tmp_dir.name) / "config_test.json"
        self._orig_path = config.CONFIG_PATH
        config.CONFIG_PATH = self.test_cfg_path
        theme.CONFIG = self.test_cfg_path

    def tearDown(self):
        config.CONFIG_PATH = self._orig_path
        theme.CONFIG = self._orig_path
        self.tmp_dir.cleanup()

    def test_default_config_when_file_missing(self):
        cfg = config.load_config()
        self.assertEqual(cfg["theme"], "dark")
        self.assertEqual(cfg["expiration_warning_days"], 7)
        self.assertEqual(cfg["receipts_output_dir"], "receipts_output")

    def test_save_and_load_preserves_keys(self):
        config.save_config({"custom_key": 42})
        loaded = config.load_config()
        self.assertEqual(loaded["custom_key"], 42)
        self.assertEqual(loaded["theme"], "dark")
        self.assertEqual(loaded["expiration_warning_days"], 7)

    def test_set_key(self):
        config.set_key("expiration_warning_days", 14)
        self.assertEqual(config.get("expiration_warning_days"), 14)
        disk_data = json.loads(self.test_cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(disk_data["expiration_warning_days"], 14)

    def test_theme_save_preserves_extended_config(self):
        config.save_config({"expiration_warning_days": 10, "receipts_output_dir": "custom/dir"})
        theme.save("light")
        self.assertEqual(theme.load(), "light")
        self.assertEqual(config.get("expiration_warning_days"), 10)
        self.assertEqual(config.get("receipts_output_dir"), "custom/dir")


# ======================================================================
# 2. Inventory Operations Tests
# ======================================================================
class TestInventoryDomain(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "inv_test.db")
        self.orig_env = os.environ.get("MEAL_PLANNER_DB")
        os.environ["MEAL_PLANNER_DB"] = self.db_path

        import meal_planner.db.connection as _conn_mod
        self.orig_conn_path = _conn_mod.DB_PATH
        _conn_mod.DB_PATH = self.db_path

        import meal_planner.inventory as _inv_mod
        self.orig_inv_path = _inv_mod.DB_PATH
        _inv_mod.DB_PATH = self.db_path

        conn = get_connection(self.db_path)
        create_schema(conn)
        conn.close()

    def tearDown(self):
        import meal_planner.db.connection as _conn_mod
        _conn_mod.DB_PATH = self.orig_conn_path
        import meal_planner.inventory as _inv_mod
        _inv_mod.DB_PATH = self.orig_inv_path

        if self.orig_env is not None:
            os.environ["MEAL_PLANNER_DB"] = self.orig_env
        else:
            os.environ.pop("MEAL_PLANNER_DB", None)
        self.tmp_dir.cleanup()

    def test_add_item_lifecycle(self):
        res1 = add_item(
            name="Penne Rigate",
            unit_weight=0.5,
            intact_items=2,
            location="crate",
            fallback_id=1,
            new_product_unit="kg",
            new_product_macros={"Kcal_100g": 350, "Protein_100g": 12, "Carbs_100g": 71, "Fat_100g": 1.5},
            db_path=self.db_path,
        )
        self.assertEqual(res1["outcome"], "ok")
        self.assertEqual(res1["ID"], 1)

        # Alias for existing product
        res2 = add_item(
            name="soup_pasta",
            unit_weight=0.5,
            intact_items=1,
            location="crate",
            fallback_id=1,
            db_path=self.db_path,
        )
        self.assertEqual(res2["outcome"], "ok")
        self.assertEqual(res2["ID"], 1)

        # Existing alias
        res3 = add_item(name="soup_pasta", unit_weight=0.5, intact_items=3, location="crate", db_path=self.db_path)
        self.assertEqual(res3["outcome"], "ok")

        # Check total availability in DB
        conn = get_connection(self.db_path)
        tot_intact = conn.execute("SELECT SUM(Intact_Items) FROM INVENTORY WHERE ID = 1").fetchone()[0]
        self.assertEqual(tot_intact, 6)
        conn.close()

        avail = get_available_inventory(db_path=self.db_path)
        pasta_items = [r for r in avail if r["ID"] == 1]
        self.assertTrue(len(pasta_items) >= 1)
        self.assertAlmostEqual(pasta_items[0]["Total_Available_Weight"], 3.0)

    def test_consume_quantity_and_shopping_list(self):
        add_item("Eggs", 1.0, 6, "fridge", fallback_id=2, new_product_unit="pcs",
                 new_product_macros={"Kcal_100g": 143, "Protein_100g": 12.6, "Carbs_100g": 0.7, "Fat_100g": 9.9},
                 db_path=self.db_path)

        with self.assertRaises(InvalidInput):
            consume_quantity("Eggs", 1.5, db_path=self.db_path)

        res = consume_quantity("Eggs", 2, db_path=self.db_path)
        self.assertEqual(res["consumed"], 2)

        # Over-consume -> sends shortfall to shopping list
        res_over = consume_quantity("Eggs", 100, db_path=self.db_path)
        self.assertEqual(res_over["consumed"], 4)
        self.assertEqual(res_over["shortfall"], 96)

        conn = get_connection(self.db_path)
        shop_qty = conn.execute("SELECT Missing_Quantity FROM SHOPPING_LIST WHERE ID = 2").fetchone()[0]
        self.assertEqual(shop_qty, 96)
        conn.close()

    def test_consume_quantity_invalid_inputs(self):
        add_item("Eggs", 1.0, 6, "fridge", fallback_id=2, new_product_unit="pcs",
                 new_product_macros={"Kcal_100g": 143, "Protein_100g": 12.6, "Carbs_100g": 0.7, "Fat_100g": 9.9},
                 db_path=self.db_path)

        invalid_quantities = [
            None,
            True,
            False,
            float("nan"),
            float("inf"),
            float("-inf"),
            0,
            0.0,
            -1,
            -0.5,
            "2",
            [2],
        ]
        for bad_qty in invalid_quantities:
            with self.subTest(bad_qty=bad_qty):
                with self.assertRaises(InvalidInput):
                    consume_quantity("Eggs", bad_qty, db_path=self.db_path)

    def test_locations_crud(self):
        locs = get_locations(db_path=self.db_path)
        self.assertIn("pantry", locs)
        add_location("cellar", db_path=self.db_path)
        self.assertIn("cellar", get_locations(db_path=self.db_path))
        rename_location("cellar", "basement", db_path=self.db_path)
        self.assertIn("basement", get_locations(db_path=self.db_path))
        self.assertNotIn("cellar", get_locations(db_path=self.db_path))
        delete_location("basement", db_path=self.db_path)
        self.assertNotIn("basement", get_locations(db_path=self.db_path))

    def test_expiration_dates(self):
        add_item("Milk", 1.0, 2, "fridge", expiration_date="2026-12-31", fallback_id=3,
                 new_product_unit="L",
                 new_product_macros={"Kcal_100g": 64, "Protein_100g": 3.2, "Carbs_100g": 4.8, "Fat_100g": 3.5},
                 db_path=self.db_path)
        inv = get_available_inventory(db_path=self.db_path)
        milk = [r for r in inv if r["Name"] == "Milk"]
        self.assertEqual(milk[0]["Expiration_Date"], "2026-12-31")

        with self.assertRaises(InvalidInput):
            add_item("Yogurt", 0.15, 4, "fridge", expiration_date="31/12/2026", fallback_id=5,
                     new_product_unit="kg",
                     new_product_macros={"Kcal_100g": 60, "Protein_100g": 4, "Carbs_100g": 5, "Fat_100g": 3},
                     db_path=self.db_path)

    def test_external_conn_batch_and_rollback(self):
        conn = get_connection(self.db_path)
        add_item("Penne Rigate", 0.5, 1, "crate", fallback_id=1, new_product_unit="kg",
                 new_product_macros={"Kcal_100g": 350, "Protein_100g": 12, "Carbs_100g": 71, "Fat_100g": 1.5},
                 conn=conn)
        conn.commit()
        cnt1 = conn.execute("SELECT COUNT(*) FROM INVENTORY").fetchone()[0]
        self.assertEqual(cnt1, 1)

        try:
            with conn:
                add_item("Penne Rigate", 0.5, 2, "crate", conn=conn)
                raise RuntimeError("Forced rollback")
        except RuntimeError:
            pass
        cnt2 = conn.execute("SELECT COUNT(*) FROM INVENTORY").fetchone()[0]
        self.assertEqual(cnt2, 1)

        catalog = get_catalog_for_reconciliation(conn=conn)
        self.assertTrue(len(catalog) >= 1)
        next_id = get_next_product_id(conn=conn)
        self.assertEqual(next_id, 2)
        conn.close()


# ======================================================================
# 3. Nutrition Engine & Views Tests
# ======================================================================
class TestEngineDomain(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp_dir.name, "engine_test.db")
        with contextlib.redirect_stdout(io.StringIO()):
            seed_products(self.db_path, False)
            seed_recipes_data(self.db_path, False)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def _product_id(self, conn, name):
        return conn.execute("SELECT ID FROM ALIASES WHERE Name = ?", (name,)).fetchone()[0]

    def test_recipe_macros_match_sql_view(self):
        conn = get_connection(self.db_path)
        products = m.load_products(conn)
        view = conn.execute(
            "SELECT Recipe_ID, Kcal, Protein, Carbs, Fat, Sugars, Saturated_fat, "
            "Fiber, Salt, Incomplete_Data FROM RECIPE_MACROS_VIEW ORDER BY Recipe_ID"
        ).fetchall()
        self.assertEqual(len(view), 3)

        macros = []
        for row in view:
            calc = m.recipe_macros(conn, row[0], products)
            for i, key in enumerate(m.MACROS):
                self.assertAlmostEqual(calc[key], row[i + 1], delta=1e-6)
            self.assertEqual(calc["Incomplete_Data"], bool(row[-1]))
            macros.append(calc)

        tot = m.sum_macros(macros)
        expected = {"Kcal": 1995.2, "Protein": 109.54, "Carbs": 256.15, "Fat": 58.53,
                    "Sugars": 52.32, "Saturated_fat": 19.34, "Fiber": 13.02, "Salt": 1.897}
        for key, value in expected.items():
            self.assertAlmostEqual(tot[key], value, delta=0.01)
        conn.close()

    def test_unit_conversions(self):
        conn = get_connection(self.db_path)
        pr = m.load_products(conn)
        yogurt = pr[self._product_id(conn, "Plain yogurt")]
        apples = pr[self._product_id(conn, "Apples")]
        puree = pr[self._product_id(conn, "Tomato puree")]
        oil = pr[self._product_id(conn, "Extra virgin olive oil")]
        rice = pr[self._product_id(conn, "Carnaroli rice")]

        self.assertEqual(m.grams(2, "pcs", yogurt), 250)
        self.assertEqual(m.grams(1, "pcs", apples), 180)
        self.assertAlmostEqual(m.grams(0.5, "L", puree), 520.0, delta=1e-6)
        self.assertAlmostEqual(m.grams(1, "tbsp", oil), 13.725, delta=1e-6)
        self.assertIsNone(m.grams(5, "ml", rice))
        self.assertAlmostEqual(m.inventory_quantity(200, "g", puree), 200 / 1040.0, delta=1e-6)
        self.assertEqual(m.inventory_quantity(100, "g", apples), 1)
        self.assertEqual(m.inventory_quantity(200, "g", apples), 2)
        conn.close()

    def test_check_day_and_extra_pct(self):
        conn = get_connection(self.db_path)
        products = m.load_products(conn)
        ids = [r[0] for r in conn.execute("SELECT Recipe_ID FROM RECIPES ORDER BY Recipe_ID")]
        totals = m.sum_macros([m.recipe_macros(conn, i, products) for i in ids])
        profile = {"Weight_kg": 87.0, "Kcal_target": 2500, "Kcal_tolerance_pct": 5.0}
        res = m.check_day(totals, profile, m.load_goals(conn))
        self.assertFalse(res["ok"])

        # Extra pct widens limits
        day = {"Kcal": 2300, "Protein": 100, "Carbs": 300, "Fat": 80,
               "Sugars": 50, "Saturated_fat": 20, "Fiber": 35, "Salt": 3}
        goals = m.load_goals(conn)
        self.assertTrue(m.check_day(day, profile, goals, extra_pct=5.0)["ok"])
        conn.close()

    def test_virtual_inventory_comparison(self):
        rnd = random.Random(20261023)
        conn = get_connection(self.db_path)
        test_macros = {"Kcal_100g": 340, "Protein_100g": 11, "Carbs_100g": 70, "Fat_100g": 2}
        add_item("Test eggs", 0.06, 6, "fridge", fallback_id=913,
                 new_product_unit="pcs", new_product_macros=test_macros, db_path=self.db_path)
        ids = [r[0] for r in conn.execute("SELECT ID FROM PRODUCTS ORDER BY ID")]
        names = m.product_names(conn)
        unit = dict(conn.execute("SELECT ID, Unit FROM PRODUCTS").fetchall())
        inv = m.VirtualInventory.from_db(conn)

        for product_id in rnd.choices(ids, k=50):
            if not inv.rows.get(product_id):
                continue
            q = rnd.randint(1, 2) if unit[product_id] == "pcs" else rnd.choice([0.1, 0.2, 0.5])
            real = consume_quantity(names[product_id], q, db_path=self.db_path)
            virtual = inv.consume(product_id, q)
            self.assertAlmostEqual(real["consumed"], virtual["consumed"], delta=1e-6)
            self.assertAlmostEqual(real["shortfall"], virtual["shortfall"], delta=1e-6)
        conn.close()


# ======================================================================
# 4. Planner Algorithmic Engine Tests
# ======================================================================
DATE_STR = "2026-10-23"
MIX_MACROS = {"Kcal_100g": 200, "Protein_100g": 10, "Carbs_100g": 27, "Fat_100g": 6.2,
              "Sugars_100g": 3, "Saturated_fat_100g": 1.5, "Fiber_100g": 3, "Salt_100g": 0.3}
RECIPES_PLAN = {
    "Breakfast 250": (["breakfast"], [("Mix 3", 250)]),
    "Breakfast 150": (["breakfast"], [("Mix 3", 150)]),
    "Lunch closed 400": (["lunch"], [("Mix 1", 400)]),
    "Lunch open 400": (["lunch"], [("Mix 2", 400)]),
    "Dinner 350": (["dinner"], [("Mix 4", 350)]),
}


class TestPlannerDomain(unittest.TestCase):
    @contextlib.contextmanager
    def _scenario(self, recipes=RECIPES_PLAN, mix2=(2, 0.2), kcal=2000):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "prova.db")
            conn = get_connection(path)
            migrate(conn)
            for n in range(1, 5):
                add_item(f"Mix {n}", 1.0, mix2[0] if n == 2 else 3, "pantry", fallback_id=200 + n,
                         new_product_unit="kg", new_product_macros=MIX_MACROS, db_path=path)
            consume_quantity("Mix 2", mix2[1], db_path=path)
            conn.execute("INSERT OR REPLACE INTO PROFILE (ID, Weight_kg, Height_cm, Age_years, Kcal_target) "
                         "VALUES (1, 70.0, 175.0, 25, ?)", (kcal,))
            for name, (meals, ingredients) in recipes.items():
                rid = conn.execute("INSERT INTO RECIPES (Name) VALUES (?)", (name,)).lastrowid
                conn.executemany("INSERT INTO RECIPE_MEALS (Recipe_ID, Meal) VALUES (?, ?)",
                                 [(rid, p) for p in meals])
                for product, grams in ingredients:
                    conn.execute("INSERT INTO RECIPE_INGREDIENTS (Recipe_ID, ID, Quantity, Unit) "
                                 "VALUES (?, (SELECT ID FROM ALIASES WHERE Name = ?), ?, 'g')",
                                 (rid, product, grams))
            conn.commit()
            yield conn
            conn.close()

    def test_picks_recipe_using_open_package_without_touching_db(self):
        with self._scenario() as conn:
            before = conn.execute("SELECT * FROM INVENTORY ORDER BY Row_ID").fetchall()
            p = plan_day.plan_day(conn, DATE_STR)
            choices = {v["meal"]: v["recipe"] for v in p["meals"]}
            self.assertEqual(choices, {"breakfast": "Breakfast 250", "lunch": "Lunch open 400", "dinner": "Dinner 350"})
            self.assertEqual(p["status"], "ok")
            after = conn.execute("SELECT * FROM INVENTORY ORDER BY Row_ID").fetchall()
            self.assertEqual(before, after)

    def test_locked_and_skipped_meals(self):
        with self._scenario() as conn:
            closed = m.recipe_id_from_name(conn, "Lunch closed 400")
            p = plan_day.plan_day(conn, DATE_STR, fixed={"lunch": closed})
            choices = {v["meal"]: v["recipe"] for v in p["meals"]}
            self.assertEqual(choices["lunch"], "Lunch closed 400")
            self.assertEqual(p["status"], "ok")

            p_skip = plan_day.plan_day(conn, DATE_STR, fixed={"dinner": None})
            self.assertIsNone(p_skip["meals"][2]["Recipe_ID"])

    def test_week_plan_and_repetition(self):
        with self._scenario() as conn:
            days = plan_week.plan_week(conn, DATE_STR, days=3)
            self.assertEqual(len(days), 3)
            for d in days:
                self.assertIn("meals", d)


# ======================================================================
# 5. Speedups, COW & Benchmark Tests
# ======================================================================
def _py_round_down(val: float) -> float:
    return math.floor(round(val * 100.0, 6)) / 100.0


def _py_sum_macros(items: list) -> dict:
    return {k: sum(x[k] for x in items) for k in MACROS}


def _py_score(coverage: float, new: float, share_deviation: float) -> float:
    return 3.0 * coverage - 1.0 * new - 5.0 * share_deviation


class TestSpeedupsAndEquivalence(unittest.TestCase):
    def test_round_down_equivalence(self):
        try:
            from meal_planner.engine._speedups import round_down_to_hundredth as c_round_down
        except ImportError:
            self.skipTest("C extension _speedups not compiled")

        test_values = [0.0, 0.001, 0.009, 0.01, 0.019, 0.02, 0.999999, 1.0, 1.23456, 12.34, 100.0]
        for val in test_values:
            py_res = _py_round_down(val)
            c_res = c_round_down(val)
            inv_res = inv_round_down(val)
            self.assertEqual(py_res, c_res)
            self.assertEqual(py_res, inv_res)

    def test_sum_macros_equivalence(self):
        try:
            from meal_planner.engine._speedups import sum_macros as c_sum_macros
        except ImportError:
            self.skipTest("C extension _speedups not compiled")

        sample_items = [
            {"Kcal": 250.5, "Protein": 20.1, "Carbs": 30.2, "Fat": 8.4,
             "Sugars": 2.1, "Saturated_fat": 1.2, "Fiber": 4.5, "Salt": 0.8},
            {"Kcal": 150.0, "Protein": 5.0, "Carbs": 25.0, "Fat": 2.0,
             "Sugars": 1.0, "Saturated_fat": 0.5, "Fiber": 3.0, "Salt": 0.2},
        ]
        py_tot = _py_sum_macros(sample_items)
        c_tot = c_sum_macros(sample_items)
        eng_tot = m.sum_macros(sample_items)

        for nutrient in MACROS:
            self.assertAlmostEqual(py_tot[nutrient], c_tot[nutrient], places=7)
            self.assertAlmostEqual(py_tot[nutrient], eng_tot[nutrient], places=7)

    def test_score_equivalence(self):
        try:
            from meal_planner.engine._speedups import score as c_score
        except ImportError:
            self.skipTest("C extension _speedups not compiled")

        cases = [(1.0, 0.0, 0.0), (0.5, 2.0, 0.15), (0.0, 5.0, 1.2), (0.85, 1.0, 0.045)]
        for cov, new_pk, s_dev in cases:
            self.assertAlmostEqual(_py_score(cov, new_pk, s_dev), c_score(cov, new_pk, s_dev), places=7)
            self.assertAlmostEqual(_py_score(cov, new_pk, s_dev), _score(cov, new_pk, s_dev), places=7)


class TestVirtualInventoryCOW(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        migrate(self.conn)
        self.conn.execute("INSERT INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g) "
                          "VALUES (1, 'kg', 350, 12, 70, 1.5)")
        self.conn.execute("INSERT INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g) "
                          "VALUES (2, 'pcs', 140, 12, 1, 10)")
        self.conn.execute("INSERT INTO ALIASES (Name, ID) VALUES ('pasta', 1), ('eggs', 2)")
        self.conn.execute("INSERT INTO INVENTORY (ID, Unit_Weight, Intact_Items, Open_Remainder, Location) "
                          "VALUES (1, 0.5, 2, 0.2, 'pantry')")
        self.conn.execute("INSERT INTO INVENTORY (ID, Unit_Weight, Intact_Items, Open_Remainder, Location) "
                          "VALUES (2, 1.0, 6, 0.0, 'fridge')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_cow_isolation(self):
        inv_orig = VirtualInventory.from_db(self.conn)
        orig_rem = inv_orig.open_remainder(1)
        orig_avail = inv_orig.availability(1)

        inv_copy = inv_orig.copy()
        res = inv_copy.consume(1, 0.2)
        self.assertEqual(res["consumed"], 0.2)
        self.assertEqual(inv_copy.open_remainder(1), 0.0)

        # Original must be untouched
        self.assertEqual(inv_orig.open_remainder(1), orig_rem)
        self.assertEqual(inv_orig.availability(1), orig_avail)


class TestOptimizationBenchmarks(unittest.TestCase):
    def test_c_round_down_speedup(self):
        try:
            from meal_planner.engine._speedups import round_down_to_hundredth as c_round_down
        except ImportError:
            self.skipTest("C extension not available")

        test_val = 12.3456789

        # Warmup loop to stabilize CPU frequency scaling and instruction cache
        for _ in range(5000):
            _py_round_down(test_val)
            c_round_down(test_val)

        iterations = 40000

        # Best-of-3 runs to protect against OS context switches and background load
        best_py = float("inf")
        best_c = float("inf")
        for _ in range(3):
            t0 = time.perf_counter()
            for _ in range(iterations):
                _py_round_down(test_val)
            t_py = time.perf_counter() - t0
            if t_py < best_py:
                best_py = t_py

            t0 = time.perf_counter()
            for _ in range(iterations):
                c_round_down(test_val)
            t_c = time.perf_counter() - t0
            if t_c < best_c:
                best_c = t_c

        speedup = best_py / max(best_c, 1e-6)
        self.assertGreater(speedup, 1.1)

    def test_cow_copy_speedup(self):
        rows = {i: [[i, 1.0, 2, 0.5]] for i in range(1, 30)}
        unit = {i: "kg" for i in range(1, 30)}
        inv = VirtualInventory(rows, unit)

        t0 = time.perf_counter()
        for _ in range(4000):
            c = inv.copy()
        t_cow = time.perf_counter() - t0

        t0 = time.perf_counter()
        for _ in range(4000):
            old = VirtualInventory({k: [r[:] for r in v] for k, v in rows.items()}, unit)
        t_deep = time.perf_counter() - t0

        speedup = t_deep / max(t_cow, 1e-6)
        self.assertGreater(speedup, 2.0)


class TestDomainBoundaryEdgeCases(unittest.TestCase):
    """Boundary and adversarial edge-case verification for core domain logic."""

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

    def test_consume_quantity_non_finite_and_boolean_rejection(self):
        """consume_quantity must strictly reject non-finite floats, booleans, zero, negative, and None."""
        add_item(
            "Riso Carnaroli", 1.0, 5, "pantry", "2027-01-01",
            fallback_id=1, new_product_unit="kg",
            new_product_macros={"Kcal_100g": 350, "Protein_100g": 7, "Carbs_100g": 78, "Fat_100g": 1},
            db_path=self.db_path,
        )

        bad_quantities = [
            float("nan"),
            float("inf"),
            float("-inf"),
            True,
            False,
            0,
            0.0,
            -1,
            -0.5,
            None,
        ]
        for bad in bad_quantities:
            with self.subTest(quantity=bad):
                with self.assertRaises(InvalidInput):
                    consume_quantity("Riso Carnaroli", bad, db_path=self.db_path)

        with self.assertRaises(ProductNotFound):
            consume_quantity("ProdottoInesistente", 1.0, db_path=self.db_path)

    def test_engine_unit_conversion_boundary_and_missing_factors(self):
        """Unit conversions handle zero quantities, missing g_piece, and missing densities gracefully."""
        from meal_planner.engine import grams, inventory_quantity

        prod_no_piece = {"unit": "pcs", "g_piece": None, "density": None}
        self.assertEqual(grams(0, "g", prod_no_piece), 0.0)
        self.assertIsNone(grams(10, "pcs", prod_no_piece))
        self.assertIsNone(inventory_quantity(100, "g", prod_no_piece))
        self.assertEqual(inventory_quantity(10, "pcs", prod_no_piece), 10)
        self.assertIsNone(grams(10, "ml", {"unit": "L", "g_piece": None, "density": None}))

        prod_kg = {"unit": "kg", "g_piece": None, "density": 1.0}
        self.assertEqual(inventory_quantity(500, "g", prod_kg), 0.5)
        self.assertIsNone(inventory_quantity(10, "pcs", prod_kg))

    def test_recipe_macros_zero_servings_and_partial_nutrients(self):
        """recipe_macros correctly computes zero servings (0.0) and flags Incomplete_Data on NULL nutrients."""
        from meal_planner.engine import recipe_macros, MACROS

        conn = get_connection(self.db_path)
        conn.execute(
            """INSERT INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g,
                                     Sugars_100g, Saturated_fat_100g, Fiber_100g, Salt_100g)
               VALUES (10, 'kg', 400.0, 10.0, 80.0, 2.0, 15.0, 0.5, 3.0, 0.1)"""
        )
        conn.execute("INSERT INTO ALIASES (Name, ID) VALUES ('Avena Test', 10)")
        conn.execute("INSERT INTO RECIPES (Recipe_ID, Name, Notes) VALUES (5, 'Pappa Dolce', '')")
        conn.execute("INSERT INTO RECIPE_INGREDIENTS (Recipe_ID, ID, Quantity, Unit) VALUES (5, 10, 100, 'g')")
        conn.commit()

        # Zero servings: all macros must be 0.0, Incomplete_Data False
        m_zero = recipe_macros(conn, 5, servings=0)
        for macro_name in MACROS:
            self.assertEqual(m_zero[macro_name], 0.0)
        self.assertFalse(m_zero["Incomplete_Data"])

        # Set optional nutrient to NULL: Incomplete_Data becomes True
        conn.execute("UPDATE PRODUCTS SET Sugars_100g = NULL WHERE ID = 10")
        conn.commit()

        m_partial = recipe_macros(conn, 5, servings=1)
        self.assertTrue(m_partial["Incomplete_Data"])
        self.assertEqual(m_partial["Sugars"], 0.0)
        self.assertEqual(m_partial["Kcal"], 400.0)
        self.assertEqual(m_partial["Protein"], 10.0)
        conn.close()

    def test_location_crud_boundary_and_collisions(self):
        """Location additions, renames, and deletions strictly enforce uniqueness and integrity."""
        with self.assertRaises(InvalidInput):
            add_location("", db_path=self.db_path)
        with self.assertRaises(InvalidInput):
            add_location("   ", db_path=self.db_path)
        with self.assertRaises(InvalidInput):
            add_location("pantry", db_path=self.db_path)

        with self.assertRaises(InvalidInput):
            rename_location("pantry", "", db_path=self.db_path)
        with self.assertRaises(ProductNotFound):
            rename_location("nonexistent_location", "new_name", db_path=self.db_path)
        with self.assertRaises(InvalidInput):
            rename_location("pantry", "fridge", db_path=self.db_path)

        with self.assertRaises(ProductNotFound):
            delete_location("nonexistent_location", db_path=self.db_path)

        # Deleting a location used in INVENTORY raises InvalidInput
        add_item(
            "Riso Carnaroli", 1.0, 1, "pantry", "2027-01-01",
            fallback_id=1, new_product_unit="kg",
            new_product_macros={"Kcal_100g": 350, "Protein_100g": 7, "Carbs_100g": 78, "Fat_100g": 1},
            db_path=self.db_path,
        )
        with self.assertRaises(InvalidInput):
            delete_location("pantry", db_path=self.db_path)


if __name__ == "__main__":
    unittest.main()
