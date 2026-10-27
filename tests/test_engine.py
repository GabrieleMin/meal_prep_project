"""
tests.test_engine

Assert-based tests of the calculation engine. Each test builds a TEMPORARY
database (seed products + recipes), so it never touches your real database
and can be re-run as often as you like.

Usage:
    python -m tests.test_engine        # runs everything and returns 1 if anything fails
    pytest tests/test_engine.py        # also works with pytest
"""

import contextlib
import io
import os
import random
import sys
import tempfile
import traceback

from meal_planner import engine as m
from meal_planner.db.connection import get_connection
from meal_planner.inventory import add_item, consume_quantity, get_available_inventory
from scripts.seed_inventory import seed as seed_products
from scripts.seed_recipes import seed as seed_recipes_data

TEST_MACROS = {"Kcal_100g": 340, "Protein_100g": 11, "Carbs_100g": 70, "Fat_100g": 2}


@contextlib.contextmanager
def temp_db():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "prova.db")
        with contextlib.redirect_stdout(io.StringIO()):
            seed_products(path, False)
            seed_recipes_data(path, False)
        yield path


def product_id(conn, name):
    return conn.execute("SELECT ID FROM ALIASES WHERE Name = ?", (name,)).fetchone()[0]


def inventory_snapshot(conn):
    return conn.execute("SELECT Row_ID, ID, Unit_Weight, Intact_Items, "
                        "Open_Remainder FROM INVENTORY ORDER BY Row_ID").fetchall()


# ----------------------------------------------------------------------

def test_recipe_macros_match_sql_view_and_hand_calculation():
    with temp_db() as path:
        conn = get_connection(path)
        products = m.load_products(conn)
        view = conn.execute(
            "SELECT Recipe_ID, Kcal, Protein, Carbs, Fat, Sugars, Saturated_fat, "
            "Fiber, Salt, Incomplete_Data FROM RECIPE_MACROS_VIEW ORDER BY Recipe_ID").fetchall()
        assert len(view) == 3
        macros = []
        for row in view:
            calc = m.recipe_macros(conn, row[0], products)
            for i, key in enumerate(m.MACROS):
                assert abs(calc[key] - row[i + 1]) < 1e-9, (row[0], key)
            assert calc["Incomplete_Data"] == bool(row[-1])
            macros.append(calc)
        tot = m.sum_macros(macros)
        expected = {"Kcal": 1995.2, "Protein": 109.54, "Carbs": 256.15, "Fat": 58.53,
                  "Sugars": 52.32, "Saturated_fat": 19.34, "Fiber": 13.02, "Salt": 1.897}
        for key, value in expected.items():
            assert abs(tot[key] - value) < 0.006, (key, tot[key], value)
        conn.close()


def test_unit_conversions():
    with temp_db() as path:
        conn = get_connection(path)
        pr = m.load_products(conn)
        yogurt, apples = pr[product_id(conn, "Plain yogurt")], pr[product_id(conn, "Apples")]
        puree = pr[product_id(conn, "Tomato puree")]
        oil = pr[product_id(conn, "Extra virgin olive oil")]
        rice = pr[product_id(conn, "Carnaroli rice")]
        assert m.grams(2, "pcs", yogurt) == 250
        assert m.grams(1, "pcs", apples) == 180
        assert abs(m.grams(0.5, "L", puree) - 520.0) < 1e-9
        assert abs(m.grams(1, "tbsp", oil) - 13.725) < 1e-9
        assert m.grams(5, "ml", rice) is None            # no density: nothing is invented
        assert abs(m.inventory_quantity(200, "g", puree) - 200 / 1040.0) < 1e-9
        assert m.inventory_quantity(100, "g", apples) == 1  # whole pieces, rounded up
        assert m.inventory_quantity(200, "g", apples) == 2
        liquid_without_density = {"unit": "L", "g_piece": None, "density": None, "macro": {}}
        assert abs(m.inventory_quantity(250, "ml", liquid_without_density) - 0.25) < 1e-12
        assert m.inventory_quantity(250, "g", liquid_without_density) is None
        conn.close()


def test_check_day_on_sample_day():
    with temp_db() as path:
        conn = get_connection(path)
        products = m.load_products(conn)
        ids = [r[0] for r in conn.execute("SELECT Recipe_ID FROM RECIPES ORDER BY Recipe_ID")]
        totals = m.sum_macros([m.recipe_macros(conn, i, products) for i in ids])
        profile = {"Weight_kg": 87.0, "Kcal_target": 2500, "Kcal_tolerance_pct": 5.0}
        res = m.check_day(totals, profile, m.load_goals(conn))
        v = {x["nutrient"]: x for x in res["entries"]}
        assert res["ok"] is False
        assert v["Kcal"]["direction"] == "below" and abs(v["Kcal"]["deviation"] - (2375 - 1995.2)) < 0.01
        # fiber: the stricter rule between 25 g and 12.6 g/1000 kcal holds
        minimum_fiber = 12.6 * totals["Kcal"] / 1000.0
        assert abs(v["Fiber"]["minimum"] - minimum_fiber) < 1e-9 and minimum_fiber > 25
        assert v["Fiber"]["direction"] == "below"
        assert abs(v["Fiber"]["deviation"] - (minimum_fiber - totals["Fiber"])) < 1e-9
        for nutrient in ("Protein", "Carbs", "Fat", "Sugars", "Saturated_fat", "Salt"):
            assert v[nutrient]["ok"], nutrient
        assert abs(v["Protein"]["minimum"] - 78.3) < 1e-9 and abs(v["Protein"]["maximum"] - 139.2) < 1e-9
        # without a kcal target there is no energy check
        without = m.check_day(totals, dict(profile, Kcal_target=None), m.load_goals(conn))
        assert without["kcal_target_defined"] is False and "Kcal" not in {x["nutrient"] for x in without["entries"]}
        conn.close()


def test_extra_pct_widens_limits():
    with temp_db() as path:
        conn = get_connection(path)
        goals = m.load_goals(conn)
        profile = {"Weight_kg": 87.0, "Kcal_target": 2500, "Kcal_tolerance_pct": 5.0}
        day = {"Kcal": 2300, "Protein": 100, "Carbs": 300, "Fat": 80,
                  "Sugars": 50, "Saturated_fat": 20, "Fiber": 35, "Salt": 3}
        base = m.check_day(day, profile, goals)
        assert [x["nutrient"] for x in base["entries"] if not x["ok"]] == ["Kcal"]
        assert abs(base["entries"][0]["deviation"] - 75) < 1e-9
        assert m.check_day(day, profile, goals, extra_pct=5.0)["ok"] is True
        conn.close()


def test_virtual_inventory_identical_to_real_consume_quantity():
    """Differential comparison: same random operations on the real DB and on a virtual copy."""
    rnd = random.Random(20261023)
    with temp_db() as path:
        conn = get_connection(path)
        # piece-based product with Unit_Weight != 1 (case that used to give wrong results)
        add_item("Test eggs", 0.06, 6, "fridge", fallback_id=913,
                      new_product_unit="pcs", new_product_macros=TEST_MACROS, db_path=path)
        ids = [r[0] for r in conn.execute("SELECT ID FROM PRODUCTS ORDER BY ID")]
        names = m.product_names(conn)
        unit = dict(conn.execute("SELECT ID, Unit FROM PRODUCTS").fetchall())
        inv = m.VirtualInventory.from_db(conn)
        quantity_kg_l = [0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 0.75, 1.0, 1.7, 5.0]
        operations = rnd.choices(ids, k=400)
        consumptions_con_shortfall = 0
        for product_id in operations:
            if not inv.rows.get(product_id):                       # exhausted: refill
                weight = 0.06 if unit[product_id] == "pcs" and product_id == 913 else (1 if unit[product_id] == "pcs" else rnd.choice([0.25, 0.5, 1.0]))
                add_item(names[product_id], weight, rnd.randint(1, 3), "pantry", db_path=path)
                inv = m.VirtualInventory.from_db(conn)
                continue
            q = rnd.randint(1, 3) if unit[product_id] == "pcs" else rnd.choice(quantity_kg_l)
            real = consume_quantity(names[product_id], q, db_path=path)
            virtual = inv.consume(product_id, q)
            assert abs(real["consumed"] - virtual["consumed"]) < 1e-9, (names[product_id], q)
            assert abs(real["shortfall"] - virtual["shortfall"]) < 1e-9, (names[product_id], q)
            consumptions_con_shortfall += virtual["shortfall"] > 0
            db = {}
            for row_id, i, weight, number, remainder in conn.execute(
                    "SELECT Row_ID, ID, Unit_Weight, Intact_Items, Open_Remainder "
                    "FROM INVENTORY ORDER BY ID, Row_ID"):
                db.setdefault(i, []).append([row_id, weight, number, remainder])
            assert set(db) == {k for k, v in inv.rows.items() if v}, "different products"
            for i, rows in db.items():
                assert len(rows) == len(inv.rows[i]), (names[i], rows, inv.rows[i])
                for a, b in zip(rows, inv.rows[i]):
                    assert a[0] == b[0] and a[2] == b[2], (names[i], a, b)
                    assert abs(a[3] - b[3]) < 1e-9, (names[i], a, b)
        assert consumptions_con_shortfall > 0, "the test must also cover insufficient availability"
        conn.close()


def test_pieces_with_unit_weight_other_than_one():
    with temp_db() as path:
        add_item("Test eggs", 0.06, 6, "fridge", fallback_id=913,
                      new_product_unit="pcs", new_product_macros=TEST_MACROS, db_path=path)
        disp = {r["ID"]: r["Total_Available_Weight"] for r in get_available_inventory(path)}
        assert disp[913] == 6
        r = consume_quantity("Test eggs", 2, db_path=path)
        assert r["consumed"] == 2 and r["shortfall"] == 0 and not r["insufficient_availability"]
        r = consume_quantity("Test eggs", 10, db_path=path)
        assert r["consumed"] == 4 and r["shortfall"] == 6


def test_recipe_simulation_on_seed_inventory():
    with temp_db() as path:
        conn = get_connection(path)
        products = m.load_products(conn)
        before = inventory_snapshot(conn)
        inv = m.VirtualInventory.from_db(conn)
        rid = lambda name: m.recipe_id_from_name(conn, name)

        spaghetti = m.simulate_recipe(conn, inv.copy(), rid("Tomato spaghetti"), products)
        assert spaghetti["feasible"] and not spaghetti["missing"]
        assert abs(spaghetti["open_coverage"] - 0.75) < 1e-9   # 3 ingredients out of 4 from open packages
        assert spaghetti["new_packages"] == 1                  # only the parmesan

        chicken = m.simulate_recipe(conn, inv.copy(), rid("Chicken and rice"), products)
        assert chicken["feasible"]
        assert abs(chicken["open_coverage"] - 1 / 3) < 1e-9       # only the oil is already open
        assert chicken["new_packages"] == 2                      # chicken and rice

        yogurt = m.simulate_recipe(conn, inv.copy(), rid("Yogurt, apple and milk"), products)
        assert not yogurt["feasible"]
        assert list(yogurt["missing"]) == [product_id(conn, "Whole milk")]
        assert abs(list(yogurt["missing"].values())[0] - 0.19) < 1e-9   # 0.194 L, floored to the hundredth

        assert inventory_snapshot(conn) == before, "the simulation must not touch the database"

        # shared stock: the second cooking finds already open what the first one opened
        shared = inv.copy()
        m.simulate_recipe(conn, shared, rid("Chicken and rice"), products)
        second = m.simulate_recipe(conn, shared, rid("Chicken and rice"), products)
        assert abs(second["open_coverage"] - 1.0) < 1e-9 and second["new_packages"] == 0
        conn.close()


def test_substitute_group_picks_already_open_product():
    with temp_db() as path:
        add_item("Pearl barley", 0.5, 1, "pantry", fallback_id=912, new_product_unit="kg",
                      new_product_macros=TEST_MACROS, db_path=path)
        consume_quantity("Pearl barley", 0.2, db_path=path)          # open remainder: 0.3 kg
        conn = get_connection(path)
        rice, barley = product_id(conn, "Carnaroli rice"), product_id(conn, "Pearl barley")
        conn.execute("INSERT INTO SUBSTITUTE_GROUPS (Name) VALUES ('Cereal')")
        gid = conn.execute("SELECT Group_ID FROM SUBSTITUTE_GROUPS").fetchone()[0]
        conn.executemany("INSERT INTO GROUP_MEMBERS (Group_ID, ID) VALUES (?, ?)",
                         [(gid, rice), (gid, barley)])
        conn.execute("INSERT INTO RECIPES (Name) VALUES ('Plain cereal')")
        rid = conn.execute("SELECT Recipe_ID FROM RECIPES WHERE Name = 'Plain cereal'").fetchone()[0]
        conn.execute("INSERT INTO RECIPE_INGREDIENTS (Recipe_ID, Group_ID, Quantity, Unit) "
                     "VALUES (?, ?, 100, 'g')", (rid, gid))
        conn.commit()
        products = m.load_products(conn)
        inv = m.VirtualInventory.from_db(conn)

        sim = m.simulate_recipe(conn, inv.copy(), rid, products)
        assert sim["group_choices"][gid] == barley and abs(sim["open_coverage"] - 1.0) < 1e-9
        mac = m.recipe_macros(conn, rid, products, sim["group_choices"])
        assert abs(mac["Kcal"] - 340.0) < 1e-9 and mac["Incomplete_Data"] is True   # test barley without fiber/salt
        assert m.recipe_macros(conn, rid, products)["Incomplete_Data"] is True      # group not resolved
        assert m.recipe_macros(conn, rid, products)["Kcal"] == 0.0

        # barley used up, the choice moves to rice
        inv.consume(barley, 5.0)
        sim = m.simulate_recipe(conn, inv.copy(), rid, products)
        assert sim["group_choices"][gid] == rice and sim["feasible"]
        conn.close()


def test_profile_and_maintenance_estimate():
    # values computed by hand with Mifflin-St Jeor: BMR man 1860, woman 1694
    assert m.estimate_maintenance(87, 176, 23, "M", "light") == 2604
    assert m.estimate_maintenance(87, 176, 23, "M", "moderate") == 2976
    assert m.estimate_maintenance(87, 176, 23, "F", "heavy") == 2965
    with temp_db() as path:
        conn = get_connection(path)
        assert m.load_profile(conn)["Kcal_target"] is None
        try:
            m.set_profile(conn, "M", "moderate", "weight_loss")
            assert False, "it must require explicit kcal"
        except ValueError:
            pass
        assert m.set_profile(conn, "M", "moderate", "maintenance") == 2976
        assert m.set_profile(conn, "M", "moderate", "weight_loss", 2400) == 2400
        p = m.load_profile(conn)
        assert p["Kcal_target"] == 2400 and p["Sex"] == "M" and p["Goal"] == "weight_loss"
        conn.close()


# ----------------------------------------------------------------------

def main() -> int:
    test = sorted((n, f) for n, f in globals().items() if n.startswith("test_") and callable(f))
    failed = 0
    for name, function in test:
        try:
            function()
            print(f"[PASS] {name}")
        except Exception:
            failed += 1
            print(f"[FAIL] {name}")
            traceback.print_exc()
    print(f"\n{len(test) - failed}/{len(test)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
