"""
tests.test_planner

Assert-based tests of the planner, on TEMPORARY databases (your real database
is not touched).

Synthetic scenario: four products "Mix 1..4" with identical macros (200 kcal, 27 g
carbohydrates, 10 g protein, 6.2 g fat, 3 g fiber per 100 g). With a 2000 kcal target
any combination from 950 to 1050 g in total meets all the limits, so the choice
depends only on open packages, new packages and the meal split.

Usage:
    python -m tests.test_planner      (or: pytest tests/test_planner.py)
"""

import contextlib
import io
import os
import sys
import tempfile
import traceback

from meal_planner import engine as m
from meal_planner import planner as pf
from meal_planner.db.connection import get_connection
from meal_planner.db.recipe_schema import migrate
from meal_planner.inventory import add_item, consume_quantity
from scripts.seed_inventory import seed as seed_products
from scripts.seed_recipes import seed as seed_recipes_data

MIX = {"Kcal_100g": 200, "Protein_100g": 10, "Carbs_100g": 27, "Fat_100g": 6.2,
       "Sugars_100g": 3, "Saturated_fat_100g": 1.5, "Fiber_100g": 3, "Salt_100g": 0.3}
DATE_STR = "2026-10-23"
RECIPES = {  # name: (meals, [(product, grams)])
    "Breakfast 250": (["breakfast"], [("Mix 3", 250)]),
    "Breakfast 150": (["breakfast"], [("Mix 3", 150)]),
    "Lunch closed 400": (["lunch"], [("Mix 1", 400)]),
    "Lunch open 400": (["lunch"], [("Mix 2", 400)]),   # Mix 2 has an open remainder
    "Dinner 350": (["dinner"], [("Mix 4", 350)]),
}

CHAIN_RECIPES = {  # Mix 2 is shared by two lunch recipes; Mix 3 and Mix 4 have plenty of stock
    "Breakfast 250": (["breakfast"], [("Mix 3", 250)]),
    "Lunch X": (["lunch"], [("Mix 2", 400)]),
    "Lunch Y": (["lunch"], [("Mix 2", 400)]),
    "Dinner 350": (["dinner"], [("Mix 4", 350)]),
}


@contextlib.contextmanager
def scenario(recipes=RECIPES, mix2=(2, 0.2), kcal=2000):
    """Mix 2: `mix2[0]` packages of 1 kg of which `mix2[1]` kg are consumed (open remainder)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "prova.db")
        conn = get_connection(path)
        migrate(conn)
        for n in range(1, 5):
            add_item(f"Mix {n}", 1.0, mix2[0] if n == 2 else 3, "pantry", fallback_id=200 + n,
                          new_product_unit="kg", new_product_macros=MIX, db_path=path)
        consume_quantity("Mix 2", mix2[1], db_path=path)
        conn.execute("UPDATE PROFILE SET Kcal_target = ? WHERE ID = 1", (kcal,))
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


def choices(plan):
    return {v["meal"]: v["recipe"] for v in plan["meals"]}


def inventory(conn):
    return conn.execute("SELECT * FROM INVENTORY ORDER BY Row_ID").fetchall()


def cooked_on(conn, recipe_name, day):
    conn.execute("INSERT INTO COOKINGS (Recipe_ID, Cooking_Date, Total_Servings, Remaining_Servings, Status) "
                 "VALUES (?, ?, 1, 0, 'cooked')", (m.recipe_id_from_name(conn, recipe_name), day))
    conn.commit()


# ----------------------------------------------------------------------

def test_picks_recipe_using_open_package_without_touching_db():
    with scenario() as conn:
        before = inventory(conn)
        p = pf.plan_day(conn, DATE_STR)
        assert choices(p) == {"breakfast": "Breakfast 250", "lunch": "Lunch open 400", "dinner": "Dinner 350"}
        assert p["status"] == "ok" and not p["missing"] and not p["warnings"]
        assert abs(p["totals"]["Kcal"] - 2000) < 1e-6
        assert inventory(conn) == before, "the planning must not write to the inventory"


def test_locked_skipped_and_unknown_meals():
    with scenario() as conn:
        closed = m.recipe_id_from_name(conn, "Lunch closed 400")
        p = pf.plan_day(conn, DATE_STR, fixed={"lunch": closed})
        assert choices(p)["lunch"] == "Lunch closed 400" and p["status"] == "ok"
        assert [v["origin"] for v in p["meals"]] == ["planned", "requested", "planned"]
        p = pf.plan_day(conn, DATE_STR, fixed={"dinner": None})
        assert p["meals"][2]["Recipe_ID"] is None and p["status"] == "out_of_limits"   # 1300 kcal out of 2000
        try:
            pf.plan_day(conn, DATE_STR, fixed={"snack": None})
            assert False, "an unknown meal must be rejected"
        except ValueError:
            pass


def test_recent_recipes_excluded_and_repetition_allowed_only_if_forced():
    with scenario() as conn:
        cooked_on(conn, "Lunch open 400", "2026-10-22")
        assert choices(pf.plan_day(conn, "2026-10-23"))["lunch"] == "Lunch closed 400"
        assert choices(pf.plan_day(conn, "2026-10-24"))["lunch"] == "Lunch closed 400"
        assert choices(pf.plan_day(conn, "2026-10-25"))["lunch"] == "Lunch open 400"  # 3 days later
        cooked_on(conn, "Dinner 350", "2026-10-22")                  # only recipe for dinner
        p = pf.plan_day(conn, "2026-10-23")
        assert choices(p)["dinner"] == "Dinner 350" and any("dinner" in a for a in p["warnings"])


def test_without_valid_plans_the_closest_to_limits_wins():
    with scenario(kcal=3000) as conn:
        p = pf.plan_day(conn, DATE_STR)
        assert p["status"] == "out_of_limits" and choices(p)["breakfast"] == "Breakfast 250"
        kcal = next(v for v in p["check"]["entries"] if v["nutrient"] == "Kcal")
        assert kcal["direction"] == "below"


def test_extra_tolerance_only_if_it_finishes_an_open_package():
    recipes = {"Breakfast 250": RECIPES["Breakfast 250"], "Lunch open 400": RECIPES["Lunch open 400"],
               "Dinner 275": (["dinner"], [("Mix 4", 275)])}          # total 1850 kcal: below 1900, within -5%
    with scenario(recipes, mix2=(1, 0.6)) as conn:               # remainder 0.4 kg: the recipe finishes it
        p = pf.plan_day(conn, DATE_STR)
        assert p["status"] == "extra" and abs(p["totals"]["Kcal"] - 1850) < 1e-6
    with scenario(recipes, mix2=(1, 0.5)) as conn:               # remainder 0.5 kg: 0.1 is left
        assert pf.plan_day(conn, DATE_STR)["status"] == "out_of_limits"


def test_save_plan_replaces_proposals_and_updates_shopping_list():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "seed.db")
        with contextlib.redirect_stdout(io.StringIO()):
            seed_products(path, False)
            seed_recipes_data(path, False)
        conn = get_connection(path)
        conn.execute("UPDATE PROFILE SET Kcal_target = 2000 WHERE ID = 1")
        conn.commit()
        milk = conn.execute("SELECT ID FROM ALIASES WHERE Name = 'Whole milk'").fetchone()[0]
        count_rows = lambda t, extra="": conn.execute(f"SELECT COUNT(*) FROM {t} {extra}").fetchone()[0]

        p = pf.plan_day(conn, DATE_STR)
        assert list(p["missing"]) == [milk]                       # in the seed the milk has run out
        pf.save_plan(conn, p)
        assert count_rows("PLAN", f"WHERE Date = '{DATE_STR}' AND Status = 'proposed'") == 3 and count_rows("COOKINGS") == 3
        shopping = dict(conn.execute("SELECT ID, Missing_Quantity FROM PLAN_SHOPPING_LIST").fetchall())
        assert list(shopping) == [milk] and abs(shopping[milk] - 0.19) < 1e-9
        total = dict(conn.execute("SELECT ID, Missing_Quantity FROM SHOPPING_LIST_VIEW").fetchall())
        assert abs(total[milk] - 1.19) < 1e-9                    # 1.0 from consumption + 0.19 from the plan

        pf.save_plan(conn, pf.plan_day(conn, DATE_STR))       # redoing the plan duplicates nothing
        assert count_rows("PLAN") == 3 and count_rows("COOKINGS") == 3

        conn.execute("UPDATE PLAN SET Status = 'confirmed' WHERE Date = ? AND Meal = 'lunch'", (DATE_STR,))
        conn.commit()
        lunch = lambda: conn.execute("SELECT c.Recipe_ID FROM PLAN p JOIN COOKINGS c USING (Cooking_ID) "
                                      "WHERE p.Meal = 'lunch'").fetchone()[0]
        before = lunch()
        p2 = pf.plan_day(conn, DATE_STR)
        assert [v["origin"] for v in p2["meals"]] == ["planned", "db", "planned"]
        pf.save_plan(conn, p2)
        assert lunch() == before and count_rows("PLAN") == 3 and count_rows("COOKINGS") == 3
        conn.close()


def test_without_kcal_target_asks_to_set_it():
    with scenario(kcal=None) as conn:
        try:
            pf.plan_day(conn, DATE_STR)
            assert False, "without a target it must stop"
        except ValueError as e:
            assert "Kcal target" in str(e)
        assert pf.plan_day(conn, DATE_STR, kcal=2000)["status"] == "ok"


def test_week_chains_stock_and_respects_repetition_ban():
    with scenario(CHAIN_RECIPES, mix2=(1, 0.2)) as conn:        # Mix 2: only 0.8 kg, already open
        plans = pf.plan_week(conn, DATE_STR, days=3)
        lunches = [choices(p)["lunch"] for p in plans]
        assert lunches[0] != lunches[1] and set(lunches[:2]) == {"Lunch X", "Lunch Y"}
        assert [bool(p["missing"]) for p in plans] == [False, False, True]   # 0.4 + 0.4, then run out
        mix2 = conn.execute("SELECT ID FROM ALIASES WHERE Name = 'Mix 2'").fetchone()[0]
        assert list(plans[2]["missing"]) == [mix2] and abs(plans[2]["missing"][mix2] - 0.4) < 1e-9
        assert all(p["status"] == "ok" for p in plans)


def test_week_does_not_write_and_saved_proposals_reserve_stock():
    only_x = {k: v for k, v in CHAIN_RECIPES.items() if k != "Lunch Y"}
    with scenario(only_x, mix2=(1, 0.2)) as conn:
        def db_status():
            return (inventory(conn), conn.execute("SELECT COUNT(*) FROM PLAN").fetchone(),
                    conn.execute("SELECT COUNT(*) FROM COOKINGS").fetchone())
        before = db_status()
        plans = pf.plan_week(conn, "2026-10-24", days=2)
        assert [bool(p["missing"]) for p in plans] == [False, False]         # 0.4 + 0.4 = 0.8 available
        assert db_status() == before, "planning must not write to the database"
        pf.save_plan(conn, pf.plan_day(conn, "2026-10-23"))         # reserves 0.4 kg of Mix 2
        plans = pf.plan_week(conn, "2026-10-24", days=2)
        assert [bool(p["missing"]) for p in plans] == [False, True]


def test_save_week_writes_all_days_and_sums_shopping():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "seed.db")
        with contextlib.redirect_stdout(io.StringIO()):
            seed_products(path, False)
            seed_recipes_data(path, False)
        conn = get_connection(path)
        conn.execute("UPDATE PROFILE SET Kcal_target = 2000 WHERE ID = 1")
        conn.commit()
        milk = conn.execute("SELECT ID FROM ALIASES WHERE Name = 'Whole milk'").fetchone()[0]
        count_rows = lambda t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]

        pf.save_week(conn, pf.plan_week(conn, DATE_STR, days=3))
        assert count_rows("PLAN") == 9 and count_rows("COOKINGS") == 9
        shopping = dict(conn.execute("SELECT ID, Missing_Quantity FROM PLAN_SHOPPING_LIST").fetchall())
        assert list(shopping) == [milk] and abs(shopping[milk] - 3 * 0.19) < 1e-9   # 0.19 L per day
        pf.save_week(conn, pf.plan_week(conn, DATE_STR, days=3))  # re-planning duplicates nothing
        assert count_rows("PLAN") == 9 and count_rows("COOKINGS") == 9
        conn.close()


def test_mark_cooked_consumes_real_inventory_atomically():
    only_x = {k: v for k, v in CHAIN_RECIPES.items() if k != "Lunch Y"}
    with scenario(only_x, mix2=(1, 0.2)) as conn:                  # Mix 2: 0.8 kg open
        pf.save_plan(conn, pf.plan_day(conn, DATE_STR))
        mix2 = conn.execute("SELECT ID FROM ALIASES WHERE Name = 'Mix 2'").fetchone()[0]
        remainder = lambda: conn.execute("SELECT SUM(Open_Remainder) FROM INVENTORY WHERE ID = ?", (mix2,)).fetchone()[0]
        pf.mark_cooked(conn, DATE_STR, "lunch")
        assert abs(remainder() - 0.4) < 1e-9
        assert conn.execute("SELECT Status FROM PLAN WHERE Meal = 'lunch'").fetchone()[0] == "consumed"
        assert conn.execute("SELECT Status, Remaining_Servings FROM COOKINGS WHERE Recipe_ID = "
                            "(SELECT Recipe_ID FROM RECIPES WHERE Name = 'Lunch X')").fetchone() == ("cooked", 0)
        for _ in range(2):                                          # already consumed / meal does not exist
            try:
                pf.mark_cooked(conn, DATE_STR, "lunch")
                assert False
            except ValueError:
                pass
        mix4 = conn.execute("SELECT ID FROM ALIASES WHERE Name = 'Mix 4'").fetchone()[0]
        conn.execute("DELETE FROM INVENTORY WHERE ID = ?", (mix4,))     # without stock you cannot cook
        conn.commit()
        before = inventory(conn)
        try:
            pf.mark_cooked(conn, DATE_STR, "dinner")
            assert False
        except ValueError as e:
            assert "missing" in str(e)
        assert inventory(conn) == before
        assert conn.execute("SELECT Status FROM PLAN WHERE Meal = 'dinner'").fetchone()[0] == "proposed"


def test_multi_serving_cooking_creates_leftovers_reused_and_consumed_without_touching_stock():
    only_x = {k: v for k, v in CHAIN_RECIPES.items() if k != "Lunch Y"}
    with scenario(only_x, mix2=(3, 0.2)) as conn:                  # Mix 2: 2 kg closed + 0.8 kg open
        mix2 = conn.execute("SELECT ID FROM ALIASES WHERE Name = 'Mix 2'").fetchone()[0]
        available = lambda: m.VirtualInventory.from_db(conn).availability(mix2)
        pf.save_plan(conn, pf.plan_day(conn, DATE_STR))
        before = available()
        pf.mark_cooked(conn, DATE_STR, "lunch", servings=3)          # 3 servings = 1.2 kg
        assert abs(before - available() - 1.2) < 1e-9
        cooking = conn.execute("SELECT Cooking_ID, Total_Servings, Remaining_Servings, Status FROM COOKINGS WHERE "
                               "Recipe_ID = (SELECT Recipe_ID FROM RECIPES WHERE Name = 'Lunch X')").fetchone()
        assert cooking[1:] == (3, 2, "cooked")
        plans = pf.plan_week(conn, "2026-10-24", days=3)
        lunches = [next(v for v in p["meals"] if v["meal"] == "lunch") for p in plans]
        assert [v["leftover"] for v in lunches] == [True, True, False]      # 2 leftovers, then cook again
        assert all(v["Cooking_ID"] == cooking[0] for v in lunches[:2])
        assert all(p["status"] == "ok" for p in plans)
        pf.save_week(conn, plans)
        assert conn.execute("SELECT COUNT(*) FROM PLAN WHERE Cooking_ID = ?", (cooking[0],)).fetchone()[0] == 3
        before = available()
        for day, remaining in (("2026-10-24", 1), ("2026-10-25", 0)):
            pf.mark_cooked(conn, day, "lunch")                    # eating a leftover does not consume ingredients
            assert conn.execute("SELECT Remaining_Servings FROM COOKINGS WHERE Cooking_ID = ?",
                                (cooking[0],)).fetchone()[0] == remaining
        assert abs(available() - before) < 1e-9
        try:
            pf.mark_cooked(conn, "2026-10-26", "lunch", servings=2)  # the third is a new cooking: fine with 2 servings
        except ValueError:
            assert False
        try:
            pf.mark_cooked(conn, "2026-10-26", "lunch")              # already consumed
            assert False
        except ValueError:
            pass


def test_consumed_meal_is_not_consumed_twice_in_the_day():
    only_x = {k: v for k, v in CHAIN_RECIPES.items() if k != "Lunch Y"}
    with scenario(only_x, mix2=(3, 0.2)) as conn:
        mix2 = conn.execute("SELECT ID FROM ALIASES WHERE Name = 'Mix 2'").fetchone()[0]
        pf.save_plan(conn, pf.plan_day(conn, DATE_STR))
        pf.mark_cooked(conn, DATE_STR, "lunch")
        real = m.VirtualInventory.from_db(conn).availability(mix2)
        p = pf.plan_day(conn, DATE_STR)
        assert p["meals"][1]["origin"] == "db"
        assert abs(p["inventory"].availability(mix2) - real) < 1e-9


def test_saved_proposals_do_not_count_as_recent_cookings():
    with scenario() as conn:
        pf.save_plan(conn, pf.plan_day(conn, DATE_STR))            # proposals for the 23rd
        p = pf.plan_day(conn, "2026-10-24")                         # a proposal is not a real cooking
        assert not p["warnings"], p["warnings"]


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
