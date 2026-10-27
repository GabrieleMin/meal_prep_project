"""
inventory.py

Implements the two fundamental operations on the inventory database:
    - add_item()
    - consume_quantity()

Each operation runs in a single transaction (commit only if all steps
succeed, automatic rollback on error), to guarantee that the database
never ends up in an inconsistent intermediate state.
"""

import sqlite3
import math
from meal_planner.db.connection import get_connection, DB_PATH


# ----------------------------------------------------------------------
# Custom exceptions, to tell the error cases apart explicitly
# when these functions are called from a higher layer (API).
# ----------------------------------------------------------------------

class AliasConflict(Exception):
    """The name entered already exists in ALIASES but is linked to a different ID."""
    pass


class InvalidID(Exception):
    """The ID provided is not in the expected format (integer)."""
    pass


class ProductNotFound(Exception):
    """Name not in ALIASES and no fallback ID provided, or a fallback ID
    that resolves neither to an existing product nor to enough data
    to create a new one."""
    pass


class InvalidInput(Exception):
    """Input values that break the business rules (e.g. quantity <= 0,
    fractional quantity for indivisible products)."""
    pass


# ----------------------------------------------------------------------
# Internal utilities
# ----------------------------------------------------------------------

def _round_down_to_hundredth(value: float) -> float:
    """Rounds down to the hundredth (0.01), e.g. 1.1111 -> 1.11."""
    return math.floor(round(value * 100, 6)) / 100


def _resolve_id_from_name(cur: sqlite3.Cursor, name: str):
    """Looks the name up in ALIASES. Returns the ID if found, otherwise None."""
    cur.execute("SELECT ID FROM ALIASES WHERE Name = ?", (name,))
    row = cur.fetchone()
    return row[0] if row else None


def _id_exists_in_products(cur: sqlite3.Cursor, product_id: int) -> bool:
    cur.execute("SELECT 1 FROM PRODUCTS WHERE ID = ?", (product_id,))
    return cur.fetchone() is not None


def _first_name_per_id(cur: sqlite3.Cursor, product_id: int) -> str:
    """Returns the first alias ever inserted for a given ID
    (used as Reference_Name in SHOPPING_LIST). 'First' = lowest rowid,
    i.e. the first inserted in chronological order."""
    cur.execute(
        "SELECT Name FROM ALIASES WHERE ID = ? ORDER BY rowid ASC LIMIT 1",
        (product_id,),
    )
    row = cur.fetchone()
    return row[0] if row else None


# ----------------------------------------------------------------------
# OPERATION 1: ADD ITEM
# ----------------------------------------------------------------------

def add_item(
    name: str,
    unit_weight: float,
    intact_items: int,
    location: str,
    fallback_id: int = None,
    new_product_unit: str = None,
    new_product_macros: dict = None,
    db_path: str = DB_PATH,
) -> dict:
    """
    Adds a new instance of a product to the inventory.

    Parameters
    ----------
    name : name typed by the user (e.g. "Penne Rigate")
    unit_weight : weight/volume of a single package (> 0)
    intact_items : how many whole packages are being added (> 0)
    location : physical storage location
    fallback_id : required ONLY if 'name' is not already in ALIASES
                  (Case B). If the ID already exists in PRODUCTS -> Case B1
                  (new alias for an existing product). If it does not ->
                  Case B2 (new product, requires new_product_unit
                  and new_product_macros).
    new_product_unit : 'kg' | 'L' | 'pcs', required only in Case B2
    new_product_macros : dict with keys Kcal_100g, Protein_100g,
                            Carbs_100g, Fat_100g (required) and
                            optionally Sugars_100g, Saturated_fat_100g,
                            Fiber_100g, Salt_100g. Required only in Case B2.

    Returns a dictionary with the outcome of the operation.
    """
    if unit_weight is None or unit_weight <= 0:
        raise InvalidInput("Unit_Weight must be greater than 0.")
    if intact_items is None or intact_items <= 0:
        raise InvalidInput(
            "Intact_Items must be greater than 0 "
            "(adding 0 items makes no sense)."
        )
    if not location:
        raise InvalidInput("Location is required.")

    conn = get_connection(db_path)
    try:
        cur = conn.cursor()

        # ---- Step 1: name resolution ----
        product_id = _resolve_id_from_name(cur, name)
        case = None

        if product_id is not None:
            # Case A: name found
            case = "A"
        else:
            # Case B: name not found, fallback_id needed
            if fallback_id is None:
                raise ProductNotFound(
                    f"Name '{name}' not found in ALIASES. "
                    f"Provide fallback_id to proceed (Case B)."
                )
            if not isinstance(fallback_id, int):
                raise InvalidID("fallback_id must be an integer.")

            if _id_exists_in_products(cur, fallback_id):
                # Case B1: ID already exists -> new alias for an existing product
                case = "B1"
                product_id = fallback_id
                cur.execute(
                    "INSERT INTO ALIASES (Name, ID) VALUES (?, ?)",
                    (name, product_id),
                )
            else:
                # Case B2: ID does not exist -> new product
                case = "B2"
                product_id = fallback_id

                if new_product_unit not in ("kg", "L", "pcs"):
                    raise InvalidInput(
                        "new_product_unit must be 'kg', 'L' or 'pcs'."
                    )
                if not new_product_macros:
                    raise InvalidInput(
                        "new_product_macros is required for a new product (Case B2)."
                    )
                required = ["Kcal_100g", "Protein_100g", "Carbs_100g", "Fat_100g"]
                missing = [k for k in required if k not in new_product_macros]
                if missing:
                    raise InvalidInput(
                        f"new_product_macros: required fields missing: {missing}"
                    )

                cur.execute(
                    """
                    INSERT INTO PRODUCTS
                        (ID, Unit, Kcal_100g, Protein_100g,
                         Carbs_100g, Sugars_100g, Fat_100g,
                         Saturated_fat_100g, Fiber_100g, Salt_100g)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        product_id,
                        new_product_unit,
                        new_product_macros["Kcal_100g"],
                        new_product_macros["Protein_100g"],
                        new_product_macros["Carbs_100g"],
                        new_product_macros.get("Sugars_100g"),
                        new_product_macros["Fat_100g"],
                        new_product_macros.get("Saturated_fat_100g"),
                        new_product_macros.get("Fiber_100g"),
                        new_product_macros.get("Salt_100g"),
                    ),
                )
                cur.execute(
                    "INSERT INTO ALIASES (Name, ID) VALUES (?, ?)",
                    (name, product_id),
                )

        # ---- Step 2: inventory write (always a new row, never a merge) ----
        cur.execute(
            """
            INSERT INTO INVENTORY
                (ID, Unit_Weight, Intact_Items, Open_Remainder, Location)
            VALUES (?, ?, ?, 0, ?)
            """,
            (product_id, unit_weight, intact_items, location),
        )
        created_row_id = cur.lastrowid

        conn.commit()
        return {
            "outcome": "ok",
            "case": case,
            "ID": product_id,
            "ID_row_inventory": created_row_id,
        }

    except sqlite3.IntegrityError as e:
        conn.rollback()
        # Typically raised when trying to insert into ALIASES a Name that
        # already exists (PRIMARY KEY constraint on Name) linked to a different ID:
        # the conflict described in the specification.
        raise AliasConflict(
            f"Integrity conflict during insertion: {e}"
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ----------------------------------------------------------------------
# OPERATION 2: CONSUME QUANTITY
# ----------------------------------------------------------------------

def consume_quantity(name: str, quantity_to_consume: float, db_path: str = DB_PATH) -> dict:
    """
    Consumes a quantity of a product from the inventory, following the
    priority: already-open units first (Open_Remainder), then opening
    new intact units in FIFO order (increasing Row_ID).

    If the total availability is insufficient, it consumes everything
    available and records (adding to it if already present) the missing
    quantity in SHOPPING_LIST. The operation never blocks in this case.

    For products with Unit = 'pcs', the quantity must be an
    integer: Intact_Items is decremented directly, never
    generating or using Open_Remainder.

    Returns a dictionary with the details of the outcome.
    """
    if quantity_to_consume is None or quantity_to_consume <= 0:
        raise InvalidInput("The quantity to consume must be greater than 0.")

    conn = get_connection(db_path)
    try:
        cur = conn.cursor()

        # ---- Step 1: name -> ID resolution ----
        product_id = _resolve_id_from_name(cur, name)
        if product_id is None:
            raise ProductNotFound(f"Name '{name}' not found in ALIASES.")

        cur.execute("SELECT Unit FROM PRODUCTS WHERE ID = ?", (product_id,))
        row = cur.fetchone()
        if row is None:
            # Should not happen if the FK constraints are respected, but
            # we handle it anyway for robustness.
            raise ProductNotFound(f"No PRODUCTS record for ID {product_id}.")
        unit_of_measure = row[0]

        # Indivisible products constraint: the quantity must be a whole number
        if unit_of_measure == "pcs":
            if quantity_to_consume != int(quantity_to_consume):
                raise InvalidInput(
                    "Indivisible product (unit 'pcs'): the requested quantity "
                    "must be a whole number of items."
                )
            quantity_to_consume = int(quantity_to_consume)

        # ---- Step 2: fetch the inventory rows and compute availability ----
        cur.execute(
            """
            SELECT Row_ID, Unit_Weight, Intact_Items, Open_Remainder
            FROM INVENTORY
            WHERE ID = ?
            ORDER BY Row_ID ASC
            """,
            (product_id,),
        )
        rows = cur.fetchall()  # list of tuples (Row_ID, Unit_Weight, Intact_Items, Open_Remainder)

        total_availability = sum(
            r[2] if unit_of_measure == "pcs" else (r[2] * r[1]) + r[3] for r in rows
        )

        missing_quantity = 0.0
        quantity_to_cover = quantity_to_consume

        if total_availability < quantity_to_consume:
            # Insufficient availability: consume everything, record the missing remainder
            missing_quantity = _round_down_to_hundredth(
                quantity_to_consume - total_availability
            ) if unit_of_measure != "pcs" else (quantity_to_consume - total_availability)
            quantity_to_cover = total_availability  # we consume at most what is available

        # ---- Step 3/4: actual consumption, row by row, with priority ----
        remaining_to_consume = quantity_to_cover

        if unit_of_measure == "pcs":
            # No open remainder for pieces: decrement directly
            # Intact_Items, FIFO across rows.
            for row_id, unit_weight, item_count, _weight_res in rows:
                if remaining_to_consume <= 0:
                    break
                withdrawal = min(item_count, remaining_to_consume)
                if withdrawal > 0:
                    new_number = item_count - withdrawal
                    cur.execute(
                        "UPDATE INVENTORY SET Intact_Items = ? WHERE Row_ID = ?",
                        (new_number, row_id),
                    )
                    remaining_to_consume -= withdrawal
        else:
            # Phase 1: consume from the already-open remainders first
            for row_id, unit_weight, item_count, remainder_weight in rows:
                if remaining_to_consume <= 0:
                    break
                if remainder_weight > 0:
                    withdrawal = min(remainder_weight, remaining_to_consume)
                    new_remainder = _round_down_to_hundredth(remainder_weight - withdrawal)
                    cur.execute(
                        "UPDATE INVENTORY SET Open_Remainder = ? WHERE Row_ID = ?",
                        (new_remainder, row_id),
                    )
                    remaining_to_consume -= withdrawal
                    # update the local value for phase 2, in case it needs re-reading
                    rows = [
                        (r[0], r[1], r[2], new_remainder) if r[0] == row_id else r
                        for r in rows
                    ]

            # Phase 2: open new intact units, FIFO, as long as needed
            if remaining_to_consume > 0:
                for idx, (row_id, unit_weight, item_count, remainder_weight) in enumerate(rows):
                    while remaining_to_consume > 0 and item_count > 0:
                        item_count -= 1
                        if remaining_to_consume >= unit_weight:
                            # the whole unit that was opened is consumed right away
                            remaining_to_consume -= unit_weight
                            new_row_remainder = 0.0
                        else:
                            new_row_remainder = _round_down_to_hundredth(
                                unit_weight - remaining_to_consume
                            )
                            remaining_to_consume = 0.0

                        cur.execute(
                            "UPDATE INVENTORY SET Intact_Items = ?, Open_Remainder = ? WHERE Row_ID = ?",
                            (item_count, new_row_remainder, row_id),
                        )
                    if remaining_to_consume <= 0:
                        break

        # ---- Step 6: clean up emptied rows ----
        cur.execute(
            """
            DELETE FROM INVENTORY
            WHERE ID = ? AND Intact_Items = 0 AND Open_Remainder = 0
            """,
            (product_id,),
        )

        # ---- Upsert SHOPPING_LIST if a quantity is missing ----
        if missing_quantity > 0:
            cur.execute("SELECT Missing_Quantity FROM SHOPPING_LIST WHERE ID = ?", (product_id,))
            existing = cur.fetchone()
            if existing is not None:
                new_total = existing[0] + missing_quantity
                if unit_of_measure != "pcs":
                    new_total = _round_down_to_hundredth(new_total)
                cur.execute(
                    "UPDATE SHOPPING_LIST SET Missing_Quantity = ? WHERE ID = ?",
                    (new_total, product_id),
                )
            else:
                reference_name = _first_name_per_id(cur, product_id)
                cur.execute(
                    "INSERT INTO SHOPPING_LIST (ID, Reference_Name, Missing_Quantity) VALUES (?, ?, ?)",
                    (product_id, reference_name, missing_quantity),
                )

        conn.commit()

        return {
            "outcome": "ok",
            "ID": product_id,
            "requested": quantity_to_consume,
            "consumed": quantity_to_cover,
            "shortfall": missing_quantity,
            "insufficient_availability": missing_quantity > 0,
        }

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ----------------------------------------------------------------------
# READ 1: SHOPPING LIST
# ----------------------------------------------------------------------

def get_shopping_list(db_path: str = DB_PATH) -> list:
    """
    Returns the current content of SHOPPING_LIST: all the products for which
    at least one consumption request was not fully satisfied,
    with the missing quantity accumulated over time.

    Returns a list of dictionaries:
        [{"ID": ..., "Name": ..., "Missing_Quantity": ...}, ...]
    """
    conn = get_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT ID, Reference_Name, Missing_Quantity FROM SHOPPING_LIST ORDER BY ID"
        )
        rows = cur.fetchall()
        return [
            {"ID": product_id, "Name": reference_name, "Missing_Quantity": quantity}
            for product_id, reference_name, quantity in rows
        ]
    finally:
        conn.close()


# ----------------------------------------------------------------------
# READ 2: AVAILABILITY VIEW (computed on the fly at every call,
# not persisted: always reflects the latest state of INVENTORY)
# ----------------------------------------------------------------------

def get_available_inventory(db_path: str = DB_PATH) -> list:
    """
    Aggregates INVENTORY by (ID, Location) and computes, for each combination,
    the total availability with the same formula used in
    consume_quantity():
        (Intact_Items * Unit_Weight) + Open_Remainder
    (for 'pcs' products: Intact_Items only), summed over all the rows
    that share the same product and the same location.

    Returns a list of dictionaries, one row for every (product, location)
    pair actually present in INVENTORY:
        [{
            "ID": ...,
            "Name": ...,                       # first historical alias for the ID
            "Total_Available_Weight": ...,
            "Unit": ...,             # 'kg' | 'L' | 'pcs'
            "Location": ...
        }, ...]
    """
    conn = get_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                INVENTORY.ID,
                INVENTORY.Location,
                SUM(CASE WHEN PRODUCTS.Unit = 'pcs'
                         THEN INVENTORY.Intact_Items
                         ELSE INVENTORY.Intact_Items * INVENTORY.Unit_Weight
                              + INVENTORY.Open_Remainder END) AS Totale,
                PRODUCTS.Unit
            FROM INVENTORY
            JOIN PRODUCTS ON PRODUCTS.ID = INVENTORY.ID
            GROUP BY INVENTORY.ID, INVENTORY.Location
            ORDER BY INVENTORY.ID, INVENTORY.Location
            """
        )
        rows = cur.fetchall()

        result = []
        for product_id, location, total, unit_of_measure in rows:
            name = _first_name_per_id(cur, product_id)
            total_value = (
                total if unit_of_measure == "pcs" else _round_down_to_hundredth(total)
            )
            result.append(
                {
                    "ID": product_id,
                    "Name": name,
                    "Total_Available_Weight": total_value,
                    "Unit": unit_of_measure,
                    "Location": location,
                }
            )
        return result
    finally:
        conn.close()
