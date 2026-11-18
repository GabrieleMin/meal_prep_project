"""
scripts.seed_inventory — Loads sample inventory data via meal_planner.inventory.

Usage:
    python -m scripts.seed_inventory [--reset] [--db path]
"""

import argparse
import sys

from meal_planner.db.connection import DB_PATH, get_connection
from meal_planner.db.schema import create_schema
from meal_planner.inventory import add_item, consume_quantity, get_available_inventory, get_shopping_list

TABLES = ["PRODUCTS", "ALIASES", "INVENTORY", "SHOPPING_LIST"]


def _macros(kcal, prot, carb, sug, fat, sat_fat, fiber, salt):
    return {
        "Kcal_100g": kcal, "Protein_100g": prot, "Carbs_100g": carb, "Sugars_100g": sug,
        "Fat_100g": fat, "Saturated_fat_100g": sat_fat, "Fiber_100g": fiber, "Salt_100g": salt,
    }


PRODUCTS = [
    {"id": 101, "name": "Carnaroli rice", "alias": ["Rice"], "unit": "kg",
     "macro": _macros(350, 7.0, 78, 0.1, 0.6, 0.2, 1.0, 0.01), "packages": [(1.0, 2, "pantry"), (0.5, 1, "pantry")]},
    {"id": 102, "name": "Spaghetti n.5", "alias": ["Spaghetti", "long pasta"], "unit": "kg",
     "macro": _macros(355, 12.5, 72, 3.5, 1.5, 0.3, 3.0, 0.01), "packages": [(0.5, 4, "pantry")]},
    {"id": 103, "name": "Tomato puree", "alias": ["Puree", "Tomato passata"], "unit": "L",
     "macro": _macros(25, 1.2, 4.0, 3.8, 0.2, 0.0, 1.5, 0.3), "packages": [(0.5, 3, "pantry")]},
    {"id": 104, "name": "Whole milk", "alias": ["Milk"], "unit": "L",
     "macro": _macros(64, 3.3, 4.8, 4.8, 3.6, 2.3, 0.0, 0.1), "packages": [(1.0, 2, "fridge")]},
    {"id": 105, "name": "Extra virgin olive oil", "alias": ["EVO oil", "Oil"], "unit": "L",
     "macro": _macros(884, 0.0, 0.0, 0.0, 100, 14, 0.0, 0.0), "packages": [(1.0, 1, "pantry")]},
    {"id": 106, "name": "00 flour", "alias": ["Flour"], "unit": "kg",
     "macro": _macros(340, 11, 72, 1.0, 1.0, 0.2, 2.5, 0.01), "packages": [(1.0, 3, "pantry")]},
    {"id": 107, "name": "Sugar", "alias": [], "unit": "kg",
     "macro": _macros(400, 0.0, 100, 100, 0.0, 0.0, 0.0, 0.0), "packages": [(1.0, 1, "pantry")]},
    {"id": 108, "name": "Chicken breast", "alias": ["Chicken"], "unit": "kg",
     "macro": _macros(110, 23, 0.0, 0.0, 1.5, 0.4, 0.0, 0.2), "packages": [(0.5, 2, "fridge"), (0.8, 1, "freezer")]},
    {"id": 109, "name": "Parmigiano Reggiano", "alias": ["Parmesan"], "unit": "kg",
     "macro": _macros(392, 33, 0.0, 0.0, 28, 18, 0.0, 1.6), "packages": [(0.25, 1, "fridge")]},
    {"id": 110, "name": "Apples", "alias": ["Apple"], "unit": "pcs",
     "macro": _macros(52, 0.3, 14, 10, 0.2, 0.0, 2.4, 0.0), "packages": [(1, 6, "fridge")]},
    {"id": 111, "name": "Plain yogurt", "alias": ["Yogurt"], "unit": "pcs",
     "macro": _macros(61, 3.5, 4.7, 4.7, 3.3, 2.1, 0.0, 0.1), "packages": [(1, 8, "fridge")]},
]

CONSUMPTIONS = [
    ("Spaghetti n.5", 0.25), ("Tomato puree", 0.25), ("00 flour", 0.25), ("Extra virgin olive oil", 0.25),
    ("Chicken breast", 0.5), ("Apples", 2), ("Whole milk", 3.0), ("Sugar", 1.5),
]


def _clear(conn) -> None:
    existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    for t in ("PLAN", "COOKINGS", "RECIPE_INGREDIENTS", "RECIPE_MEALS", "RECIPES", "GROUP_MEMBERS",
              "SUBSTITUTE_GROUPS", "PLAN_SHOPPING_LIST", "SHOPPING_LIST", "INVENTORY", "ALIASES", "PRODUCTS"):
        if t in existing:
            conn.execute(f"DELETE FROM {t}")
    conn.execute("DELETE FROM sqlite_sequence WHERE name IN ('INVENTORY', 'RECIPES', 'SUBSTITUTE_GROUPS', 'RECIPE_INGREDIENTS', 'COOKINGS', 'PLAN')")
    conn.commit()


def seed(db_path: str, reset: bool) -> None:
    conn = get_connection(db_path)
    try:
        create_schema(conn)
        present = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
        if any(present.values()):
            if not reset:
                print("The database already contains data:")
                for t, n in present.items():
                    print(f"  {t}: {n} rows")
                print("No changes made. Use --reset to empty it and reload.")
                sys.exit(1)
            _clear(conn)
            print("Tables emptied (--reset).")
    finally:
        conn.close()

    for p in PRODUCTS:
        w, n, loc = p["packages"][0]
        add_item(name=p["name"], unit_weight=w, intact_items=n, location=loc,
                 fallback_id=p["id"], new_product_unit=p["unit"], new_product_macros=p["macro"], db_path=db_path)
        if p["alias"]:
            c = get_connection(db_path)
            try:
                c.executemany("INSERT INTO ALIASES (Name, ID) VALUES (?, ?)", [(a, p["id"]) for a in p["alias"]])
                c.commit()
            finally:
                c.close()
        for w, n, loc in p["packages"][1:]:
            add_item(name=p["name"], unit_weight=w, intact_items=n, location=loc, db_path=db_path)
    print(f"Loaded {len(PRODUCTS)} products.")

    for name, qty in CONSUMPTIONS:
        out = consume_quantity(name, qty, db_path=db_path)
        note = f"  (missing {out['shortfall']})" if out["insufficient_availability"] else ""
        print(f"Consume {qty} of '{name}': consumed {out['consumed']}{note}")

    print("\nAvailability by product and location:")
    for r in get_available_inventory(db_path=db_path):
        print(f"  [{r['ID']}] {r['Name']:<28} {r['Total_Available_Weight']:>7} {r['Unit']:<3} @ {r['Location']}")
    print("\nShopping list:")
    for r in get_shopping_list(db_path=db_path):
        print(f"  [{r['ID']}] {r['Name']:<28} {r['Missing_Quantity']:>7}")
    print(f"\nDatabase loaded: {db_path}")


def main():
    parser = argparse.ArgumentParser(description="Loads sample data into the database.")
    parser.add_argument("--reset", action="store_true", help="empty the tables before loading")
    parser.add_argument("--db", default=DB_PATH, help="database path")
    args = parser.parse_args()
    seed(args.db, args.reset)


if __name__ == "__main__":
    main()
