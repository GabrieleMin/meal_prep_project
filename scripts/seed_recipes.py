"""
scripts.seed_recipes

Sample data for the recipe part:
  1. fills Grams_per_piece and Density_kg_per_L of the seed products
  2. inserts 3 recipes (breakfast, lunch/dinner, lunch/dinner)
  3. prints the macros per serving computed by RECIPE_MACROS_VIEW

Prerequisite: python -m scripts.seed_inventory   (seed products and aliases)
Usage:
    python -m scripts.seed_recipes            # idempotent: skips recipes already present
    python -m scripts.seed_recipes --reset    # deletes recipes, cookings and plan, then reinserts
    python -m scripts.seed_recipes --db other.db

Recipe quantities are per ONE serving. The macros of the seed products are
indicative: these numbers serve to check the calculation, not nutritional
accuracy.
"""

import argparse
import sys

from meal_planner.db.connection import get_connection, DB_PATH
from meal_planner.db.recipe_schema import migrate

# Name (as in ALIASES) -> (Grams_per_piece, Density_kg_per_L)
CONVERSIONS = {
    "Apples":                       (180, None),
    "Plain yogurt":              (125, None),
    "Whole milk":               (None, 1.03),
    "Tomato puree":        (None, 1.04),
    "Extra virgin olive oil": (None, 0.915),
}

# (Recipe name, allowed meals, [(product, quantity, unit), ...])
RECIPES = [
    ("Yogurt, apple and milk", ["breakfast"], [
        ("Plain yogurt", 2, "pcs"),
        ("Apples", 1, "pcs"),
        ("Whole milk", 200, "g"),
    ]),
    ("Tomato spaghetti", ["lunch", "dinner"], [
        ("Spaghetti n.5", 150, "g"),
        ("Tomato puree", 200, "g"),
        ("Extra virgin olive oil", 15, "g"),
        ("Parmigiano Reggiano", 20, "g"),
    ]),
    ("Chicken and rice", ["lunch", "dinner"], [
        ("Chicken breast", 250, "g"),
        ("Carnaroli rice", 120, "g"),
        ("Extra virgin olive oil", 15, "g"),
    ]),
]


def _product_id(cur, name: str) -> int:
    row = cur.execute("SELECT ID FROM ALIASES WHERE Name = ?", (name,)).fetchone()
    if row is None:
        sys.exit(f"Product '{name}' not found in ALIASES. Run first: python -m scripts.seed_inventory")
    return row[0]


def _clear_recipes(conn) -> None:
    for t in ("PLAN", "COOKINGS", "RECIPE_INGREDIENTS", "RECIPE_MEALS", "RECIPES"):
        conn.execute(f"DELETE FROM {t}")
    conn.execute("DELETE FROM sqlite_sequence WHERE name IN "
                 "('RECIPES', 'RECIPE_INGREDIENTS', 'COOKINGS', 'PLAN')")


def seed(db_path: str, reset: bool) -> None:
    conn = get_connection(db_path)
    try:
        migrate(conn)  # idempotent: makes sure the recipe schema exists
        cur = conn.cursor()
        if reset:
            _clear_recipes(conn)
            print("Recipes, cookings and plan deleted (--reset).")

        # 1) Conversion data
        for name, (g_piece, density) in CONVERSIONS.items():
            product_id = _product_id(cur, name)
            if g_piece is not None:
                cur.execute("UPDATE PRODUCTS SET Grams_per_piece = ? WHERE ID = ?", (g_piece, product_id))
            if density is not None:
                cur.execute("UPDATE PRODUCTS SET Density_kg_per_L = ? WHERE ID = ?", (density, product_id))
        print(f"Conversion data filled in for {len(CONVERSIONS)} products.")

        # 2) Recipes
        for name, meals, ingredients in RECIPES:
            cur.execute("INSERT OR IGNORE INTO RECIPES (Name) VALUES (?)", (name,))
            if cur.rowcount == 0:
                print(f"Recipe already present, skipped: {name}")
                continue
            recipe_id = cur.lastrowid
            cur.executemany("INSERT INTO RECIPE_MEALS (Recipe_ID, Meal) VALUES (?, ?)",
                            [(recipe_id, p) for p in meals])
            cur.executemany(
                "INSERT INTO RECIPE_INGREDIENTS (Recipe_ID, ID, Quantity, Unit) VALUES (?, ?, ?, ?)",
                [(recipe_id, _product_id(cur, prod), q, u) for prod, q, u in ingredients])
            print(f"Recipe inserted: {name} ({', '.join(meals)})")
        conn.commit()

        # 3) Macro summary per serving
        cols = ["Kcal", "Protein", "Carbs", "Fat", "Sugars",
                "Saturated_fat", "Fiber", "Salt"]
        rows = cur.execute(
            "SELECT Name, " + ", ".join(cols) + ", Incomplete_Data "
            "FROM RECIPE_MACROS_VIEW ORDER BY Recipe_ID").fetchall()
        print("\nMacros per serving (from RECIPE_MACROS_VIEW):")
        print(f"  {'Recipe':<24}" + "".join(f"{c[:7]:>9}" for c in cols) + "  incomplete")
        total = [0.0] * len(cols)
        for r in rows:
            print(f"  {r[0]:<24}" + "".join(f"{v:>9.2f}" for v in r[1:-1]) + f"  {r[-1]:>6}")
            total = [t + v for t, v in zip(total, r[1:-1])]
        print(f"  {'TOTAL (sum of recipes)':<24}" + "".join(f"{v:>9.2f}" for v in total))
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Loads sample recipes.")
    parser.add_argument("--reset", action="store_true",
                        help="delete recipes, cookings and plan before reinserting")
    parser.add_argument("--db", default=DB_PATH, help="database path")
    args = parser.parse_args()
    seed(args.db, args.reset)


if __name__ == "__main__":
    main()
