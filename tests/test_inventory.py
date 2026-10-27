import os
import tempfile

# Isolated database: this suite writes real data.
os.environ["MEAL_PLANNER_DB"] = os.path.join(tempfile.mkdtemp(), "inventory_test.db")

import sqlite3
from meal_planner.inventory import add_item, consume_quantity, ProductNotFound, InvalidInput
from meal_planner.db.connection import DB_PATH

from meal_planner.db.connection import get_connection
from meal_planner.db.schema import create_schema

_c = get_connection()
create_schema(_c)
_c.close()


def dump(title):
    print(f"\n--- {title} ---")
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    for table in ["ALIASES", "PRODUCTS", "INVENTORY", "SHOPPING_LIST"]:
        cur.execute(f"SELECT * FROM {table}")
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
        print(f"{table} ({', '.join(cols)}):")
        for r in rows:
            print(" ", r)
    conn.close()


print("=== TEST 1: Case B2 - new product (Penne Rigate) ===")
res = add_item(
    name="Penne Rigate",
    unit_weight=0.5,
    intact_items=2,
    location="crate",
    fallback_id=1,
    new_product_unit="kg",
    new_product_macros={
        "Kcal_100g": 350, "Protein_100g": 12, "Carbs_100g": 71, "Fat_100g": 1.5
    },
)
print(res)

print("\n=== TEST 2: Case B1 - new alias for an existing product (soup_pasta -> ID 1) ===")
res = add_item(
    name="soup_pasta",
    unit_weight=0.5,
    intact_items=1,
    location="pantry",
    fallback_id=1,
)
print(res)

print("\n=== TEST 3: Case A - known name (Penne Rigate again, another row, no merge) ===")
res = add_item(
    name="Penne Rigate",
    unit_weight=1.0,
    intact_items=1,
    location="crate",
)
print(res)

dump("STATE AFTER ADDITIONS")

print("\n=== TEST 4: Partial consumption - opens a unit, creates a remainder ===")
# Current rows for ID 1: (0.5kg x2, crate), (0.5kg x1, pantry), (1.0kg x1, crate)
# Consume 0.3 kg -> FIFO: first row (lowest Row_ID) with no open remainder -> opens 1 unit of 0.5kg
res = consume_quantity("Penne Rigate", 0.3)
print(res)

dump("STATE AFTER CONSUMING 0.3kg")

print("\n=== TEST 5: Consumption that crosses several units (uses the open remainder + opens another) ===")
# Expected open remainder: 0.2kg. Consume 0.5kg -> uses the 0.2 remainder, then opens a new unit (another 0.3kg from a 0.5 or 1kg pack)
res = consume_quantity("Penne Rigate", 0.5)
print(res)

dump("STATE AFTER CONSUMING 0.5kg (crosses several units)")

print("\n=== TEST 6: Piece-based product - eggs ===")
res = add_item(
    name="Eggs",
    unit_weight=1,  # not relevant for pcs, but required > 0
    intact_items=6,
    location="fridge",
    fallback_id=2,
    new_product_unit="pcs",
    new_product_macros={
        "Kcal_100g": 143, "Protein_100g": 12.6, "Carbs_100g": 0.7, "Fat_100g": 9.9
    },
)
print(res)

print("\n=== TEST 7: Piece consumption with a fractional quantity -> must fail ===")
try:
    consume_quantity("Eggs", 1.5)
    print("ERROR: it should have raised an exception")
except InvalidInput as e:
    print("OK, expected exception:", e)

print("\n=== TEST 8: Valid piece consumption ===")
res = consume_quantity("Eggs", 2)
print(res)

dump("STATE AFTER CONSUMING EGGS")

print("\n=== TEST 9: Insufficient quantity -> consumes everything + updates SHOPPING_LIST ===")
res = consume_quantity("Eggs", 100)  # 4 are left, we ask for 100
print(res)

dump("STATE AFTER EXCESSIVE EGG REQUEST (must end up in SHOPPING_LIST, INVENTORY row deleted)")

print("\n=== TEST 10: Second insufficient request on the same ID -> adds up in SHOPPING_LIST ===")
res = consume_quantity("Eggs", 5)
print(res)

dump("FINAL STATE - check the sum in SHOPPING_LIST")

print("\n=== TEST 11: Unknown product ===")
try:
    consume_quantity("Rice flour", 1)
    print("ERROR: it should have raised an exception")
except ProductNotFound as e:
    print("OK, expected exception:", e)

print("\n=== TEST 12: Alias conflict (name already existing with a different ID) ===")
try:
    add_item(
        name="Penne Rigate",  # already linked to ID 1
        unit_weight=0.5,
        intact_items=1,
        location="crate",
        fallback_id=99,  # different ID -> but since the name is already in ALIASES, it goes to Case A, not B!
    )
except Exception as e:
    print("Note:", e)

print("\nAll tests completed.")
