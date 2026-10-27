"""tests.test_recipes - assert-based tests of recipes.py on temporary databases. Usage: python -m tests.test_recipes"""

import contextlib
import io
import json
import os
import sys
import tempfile
import traceback

from meal_planner import recipes as r
from meal_planner.db.connection import get_connection
from meal_planner.db.recipe_schema import migrate
from scripts.seed_inventory import seed as seed_products


@contextlib.contextmanager
def archive():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "p.db")
        with contextlib.redirect_stdout(io.StringIO()):
            seed_products(path, False)
        conn = get_connection(path)
        migrate(conn)
        yield conn, tmp
        conn.close()


def count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_create_recipe_with_free_names_and_warnings():
    with archive() as (conn, _):
        outcome = r.create_recipe(conn, "Rice and chicken", ["lunch", "dinner"],
                               [("carnaroli rice", 100, "g"), ("CHICKEN BREAST", "150", "g")])
        assert outcome["warnings"] == [] and count(conn, "RECIPE_MEALS") == 2 and count(conn, "RECIPE_INGREDIENTS") == 2
        kcal = conn.execute("SELECT Kcal FROM RECIPE_MACROS_VIEW WHERE Recipe_ID = ?", (outcome["Recipe_ID"],)).fetchone()[0]
        assert abs(kcal - 515) < 1e-9                                  # 350 (rice) + 165 (1.5 x 110)
        assert r.create_recipe(conn, "Dessert", ["breakfast"], [("Sugar", 1, "pcs")])["warnings"]   # no g per piece


def test_validations_leave_no_partial_writes():
    with archive() as (conn, _):
        r.create_recipe(conn, "Base", ["lunch"], [("Rice", 100, "g")])
        cases = [("", ["lunch"], [("Rice", 1, "g")]), ("X", ["merenda"], [("Rice", 1, "g")]),
                ("X", [], [("Rice", 1, "g")]), ("X", ["lunch"], []), ("X", ["lunch"], [("Marble", 1, "g")]),
                ("X", ["lunch"], [("Rice", 0, "g")]), ("X", ["lunch"], [("Rice", 1, "quintals")]),
                ("base", ["lunch"], [("Rice", 1, "g")])]                 # duplicate name (also in lowercase)
        for name, meals, ingredients in cases:
            try:
                r.create_recipe(conn, name, meals, ingredients)
                assert False, (name, meals, ingredients)
            except ValueError:
                pass
        assert (count(conn, "RECIPES"), count(conn, "RECIPE_MEALS"), count(conn, "RECIPE_INGREDIENTS")) == (1, 1, 1)


def test_import_json_and_csv_skipping_invalid_recipes():
    with archive() as (conn, tmp):
        j = os.path.join(tmp, "r.json")
        json.dump([{"name": "Pasta and passata", "meals": ["lunch"], "ingredients": [
                       {"product": "Spaghetti", "quantity": 100, "unit": "g"},
                       {"product": "Oil", "quantity": 1, "unit": "tbsp", "to_taste": True}]},
                   {"name": "Broken", "meals": ["lunch"], "ingredients": [{"product": "Marble", "quantity": 1, "unit": "g"}]}],
                  open(j, "w", encoding="utf-8"))
        created, errors = r.import_file(conn, j)
        assert created == 1 and len(errors) == 1 and "Marble" in errors[0]
        assert conn.execute("SELECT To_Taste FROM RECIPE_INGREDIENTS WHERE Unit = 'tbsp'").fetchone()[0] == 1
        c = os.path.join(tmp, "r.csv")
        open(c, "w", encoding="utf-8").write("recipe;meals;product;quantity;unit\n"
                                             "Yogurt and apples;breakfast|dinner;Yogurt;2;pcs\nYogurt and apples;breakfast|dinner;Apples;1,5;pcs\n")
        assert r.import_file(conn, c) == (1, [])
        assert count(conn, "RECIPES") == 2 and conn.execute("SELECT COUNT(*) FROM RECIPE_MEALS WHERE Meal = 'dinner'").fetchone()[0] == 1
        try:
            r.import_file(conn, os.path.join(tmp, "does_not_exist.json"))
            assert False
        except ValueError:
            pass


def test_delete_only_if_unused():
    with archive() as (conn, _):
        rid = r.create_recipe(conn, "To use", ["lunch"], [("Rice", 100, "g")])["Recipe_ID"]
        r.create_recipe(conn, "To delete", ["lunch"], [("Rice", 100, "g")])
        r.delete_recipe(conn, "to delete")
        assert count(conn, "RECIPES") == 1 and count(conn, "RECIPE_INGREDIENTS") == 1       # cascade onto the ingredients
        conn.execute("INSERT INTO COOKINGS (Recipe_ID, Cooking_Date, Total_Servings, Remaining_Servings) VALUES (?, '2026-10-24', 1, 1)", (rid,))
        conn.commit()
        for name in ("To use", "Nonexistent"):
            try:
                r.delete_recipe(conn, name)
                assert False
            except ValueError:
                pass


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
