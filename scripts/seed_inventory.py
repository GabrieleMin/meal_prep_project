"""
scripts.seed_inventory

Loads sample data into the database, going through the functions of
meal_planner.inventory (aggiungi_item / consuma_quantita), so that the data
follow exactly the same business rules as the application.

Usage (from the project root):
    python -m scripts.seed_inventory             # loads only if the DB is empty
    python -m scripts.seed_inventory --reset     # empties the tables and reloads
    python -m scripts.seed_inventory --db other.db

Design choices
--------------
- Product IDs start at 101: tests.test_inventory uses IDs 1 and 2 and the names
  "Penne Rigate", "soup_pasta", "Eggs", "Rice flour". With this seed none of those
  names/IDs is taken, so the test can also run on top of the loaded DB.
- 'pcs' products have Unit_Weight = 1 (weight is meaningless for pieces,
  but the CHECK constraint requires a value > 0).
- Consumed quantities are chosen to give exact results in floating point
  (0.25, 0.5, 1.5 ...).
- Nutritional values are indicative (per 100 g), good enough for tests.
"""

import argparse
import sys

from meal_planner.db.connection import get_connection, DB_PATH
from meal_planner.db.schema import create_schema
from meal_planner.inventory import add_item, consume_quantity, get_available_inventory, get_shopping_list

TABLES = ["PRODUCTS", "ALIASES", "INVENTORY", "SHOPPING_LIST"]


def _macros(kcal, prot, carb, sug, fat, sat_fat, fiber, salt):
    return {
        "Kcal_100g": kcal, "Protein_100g": prot, "Carbs_100g": carb,
        "Sugars_100g": sug, "Fat_100g": fat,
        "Saturated_fat_100g": sat_fat, "Fiber_100g": fiber, "Salt_100g": salt,
    }


# Each product: ID, main name, extra aliases, unit of measure,
# macros and packages (Unit_Weight, Intact_Items, Location).
PRODUCTS = [
    {"id": 101, "name": "Carnaroli rice", "alias": ["Rice"], "unit": "kg",
     "macro": _macros(350, 7.0, 78, 0.1, 0.6, 0.2, 1.0, 0.01),
     "packages": [(1.0, 2, "pantry"), (0.5, 1, "pantry")]},
    {"id": 102, "name": "Spaghetti n.5", "alias": ["Spaghetti", "long pasta"], "unit": "kg",
     "macro": _macros(355, 12.5, 72, 3.5, 1.5, 0.3, 3.0, 0.01),
     "packages": [(0.5, 4, "pantry")]},
    {"id": 103, "name": "Tomato puree", "alias": ["Puree", "Tomato passata"], "unit": "L",
     "macro": _macros(25, 1.2, 4.0, 3.8, 0.2, 0.0, 1.5, 0.3),
     "packages": [(0.5, 3, "pantry")]},
    {"id": 104, "name": "Whole milk", "alias": ["Milk"], "unit": "L",
     "macro": _macros(64, 3.3, 4.8, 4.8, 3.6, 2.3, 0.0, 0.1),
     "packages": [(1.0, 2, "fridge")]},
    {"id": 105, "name": "Extra virgin olive oil", "alias": ["EVO oil", "Oil"], "unit": "L",
     "macro": _macros(884, 0.0, 0.0, 0.0, 100, 14, 0.0, 0.0),
     "packages": [(1.0, 1, "pantry")]},
    {"id": 106, "name": "00 flour", "alias": ["Flour"], "unit": "kg",
     "macro": _macros(340, 11, 72, 1.0, 1.0, 0.2, 2.5, 0.01),
     "packages": [(1.0, 3, "pantry")]},
    {"id": 107, "name": "Sugar", "alias": [], "unit": "kg",
     "macro": _macros(400, 0.0, 100, 100, 0.0, 0.0, 0.0, 0.0),
     "packages": [(1.0, 1, "pantry")]},
    {"id": 108, "name": "Chicken breast", "alias": ["Chicken"], "unit": "kg",
     "macro": _macros(110, 23, 0.0, 0.0, 1.5, 0.4, 0.0, 0.2),
     "packages": [(0.5, 2, "fridge"), (0.8, 1, "freezer")]},
    {"id": 109, "name": "Parmigiano Reggiano", "alias": ["Parmesan"], "unit": "kg",
     "macro": _macros(392, 33, 0.0, 0.0, 28, 18, 0.0, 1.6),
     "packages": [(0.25, 1, "fridge")]},
    {"id": 110, "name": "Apples", "alias": ["Apple"], "unit": "pcs",
     "macro": _macros(52, 0.3, 14, 10, 0.2, 0.0, 2.4, 0.0),
     "packages": [(1, 6, "fridge")]},
    {"id": 111, "name": "Plain yogurt", "alias": ["Yogurt"], "unit": "pcs",
     "macro": _macros(61, 3.5, 4.7, 4.7, 3.3, 2.1, 0.0, 0.1),
     "packages": [(1, 8, "fridge")]},
]

# Consumptions performed after loading, to have in the DB:
#   - open remainders (Open_Remainder > 0)
# - a finished package but the row still present (chicken)
#   - 'pcs' products consumed
# - entries in SHOPPING_LIST (milk and sugar requested beyond availability)
CONSUMPTIONS = [
    ("Spaghetti n.5", 0.25),
    ("Tomato puree", 0.25),
    ("00 flour", 0.25),
    ("Extra virgin olive oil", 0.25),
    ("Chicken breast", 0.5),
    ("Apples", 2),
    ("Whole milk", 3.0),   # 2 L available -> 1.0 L missing
    ("Sugar", 1.5),       # 1 kg available -> 0.5 kg missing
]


def _count_rows(conn) -> dict:
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}


def _clear(conn) -> None:
    # Reverse order with respect to the FK dependencies. The first entries are the tables
    # introduced by meal_planner.db.recipe_schema: they are emptied only if they exist (MEALS,
    # PROFILE and GOALS are configuration and stay).
    existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    order = ("PLAN", "COOKINGS", "RECIPE_INGREDIENTS", "RECIPE_MEALS", "RECIPES",
              "GROUP_MEMBERS", "SUBSTITUTE_GROUPS", "PLAN_SHOPPING_LIST",
              "SHOPPING_LIST", "INVENTORY", "ALIASES", "PRODUCTS")
    for t in order:
        if t in existing:
            conn.execute(f"DELETE FROM {t}")
    # Restart the AUTOINCREMENT numbering
    conn.execute("DELETE FROM sqlite_sequence WHERE name IN "
                 "('INVENTORY', 'RECIPES', 'SUBSTITUTE_GROUPS', 'RECIPE_INGREDIENTS', "
                 "'COOKINGS', 'PLAN')")
    conn.commit()


def _add_aliases(db_path: str, product_id: int, alias: list) -> None:
    if not alias:
        return
    conn = get_connection(db_path)
    try:
        conn.executemany(
            "INSERT INTO ALIASES (Name, ID) VALUES (?, ?)",
            [(a, product_id) for a in alias],
        )
        conn.commit()
    finally:
        conn.close()


def seed(db_path: str, reset: bool) -> None:
    conn = get_connection(db_path)
    try:
        create_schema(conn)  # idempotent (CREATE ... IF NOT EXISTS)
        present = _count_rows(conn)
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

    # --- Loading products, through add_item ---
    for p in PRODUCTS:
        weight, n, location = p["packages"][0]
        # First package: Case B2 (creates product, main alias and row)
        add_item(
            name=p["name"], unit_weight=weight, intact_items=n, location=location,
            fallback_id=p["id"], new_product_unit=p["unit"],
            new_product_macros=p["macro"], db_path=db_path,
        )
        _add_aliases(db_path, p["id"], p["alias"])
        # Following packages: Case A (known name, new row, no merge)
        for weight, n, location in p["packages"][1:]:
            add_item(
                name=p["name"], unit_weight=weight, intact_items=n,
                location=location, db_path=db_path,
            )
    print(f"Loaded {len(PRODUCTS)} products.")

    # --- Consumptions, through consume_quantity ---
    for name, quantity in CONSUMPTIONS:
        outcome = consume_quantity(name, quantity, db_path=db_path)
        note = f"  (missing {outcome['shortfall']})" if outcome["insufficient_availability"] else ""
        print(f"Consume {quantity} of '{name}': consumed {outcome['consumed']}{note}")

    # --- Final summary ---
    print("\nAvailability by product and location:")
    for r in get_available_inventory(db_path=db_path):
        print(f"  [{r['ID']}] {r['Name']:<28} {r['Total_Available_Weight']:>7} "
              f"{r['Unit']:<3} @ {r['Location']}")
    print("\nShopping list:")
    for r in get_shopping_list(db_path=db_path):
        print(f"  [{r['ID']}] {r['Name']:<28} {r['Missing_Quantity']:>7}")
    print(f"\nDatabase loaded: {db_path}")


def main():
    parser = argparse.ArgumentParser(description="Loads sample data into the database.")
    parser.add_argument("--reset", action="store_true",
                        help="empty the tables before loading")
    parser.add_argument("--db", default=DB_PATH,
                        help="database path (default: the project's database file)")
    args = parser.parse_args()
    seed(args.db, args.reset)


if __name__ == "__main__":
    main()
