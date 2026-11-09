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
import re
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

try:
    from meal_planner.engine._speedups import round_down_to_hundredth as _round_down_to_hundredth
except (ImportError, AttributeError):
    def _round_down_to_hundredth(value: float) -> float:
        """Rounds down to the hundredth (0.01), e.g. 1.1111 -> 1.11."""
        return math.floor(round(value * 100, 6)) / 100


def _resolve_id_from_name(cur: sqlite3.Cursor, name: str):
    """Looks the name up in ALIASES. Returns the ID if found, otherwise None."""
    cur.execute("SELECT ID FROM ALIASES WHERE Name = ?", (name,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("SELECT ID FROM ALIASES WHERE Name = ? COLLATE NOCASE", (name,))
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
# RECEIPT OCR & UNIT NORMALIZATION HELPERS
# ----------------------------------------------------------------------

def convert_receipt_quantity_unit(
    quantity: float,
    raw_unit: str,
    target_unit: str = None,
    grams_per_piece: float = None,
    density_kg_per_l: float = None,
) -> tuple[float, str]:
    """
    Normalizes a receipt item quantity and raw unit into the canonical unit
    format required by the SQLite database ('kg', 'L', 'pcs').

    Parameters
    ----------
    quantity : numeric quantity from receipt (must be > 0 and finite).
    raw_unit : unit string from receipt (e.g. 'g', 'gr', 'kg', 'ml', 'cl', 'l', 'pz', 'pcs').
    target_unit : optional target unit to verify dimensional compatibility.
    grams_per_piece : weight in grams of 1 piece (required for pcs <-> kg / L).
    density_kg_per_l : density in kg/L (required for L <-> kg / pcs).

    Returns
    -------
    tuple of (normalized_quantity, canonical_unit)
    """
    if quantity is None:
        raise InvalidInput("Quantity cannot be None.")
    try:
        qty = float(quantity)
    except (ValueError, TypeError) as e:
        raise InvalidInput(f"Invalid quantity {quantity!r}: must be a numeric value.") from e

    if not math.isfinite(qty) or qty <= 0:
        raise InvalidInput(f"Quantity must be a positive finite number, got {quantity!r}.")

    if not raw_unit or not isinstance(raw_unit, str):
        raise InvalidInput("raw_unit must be a non-empty string.")

    unit_str = raw_unit.strip().lower().rstrip(".")

    if unit_str in ("g", "gr"):
        norm_qty = round(qty / 1000.0, 4)
        if norm_qty <= 0:
            raise InvalidInput(f"Quantity {qty} {raw_unit} rounds to <= 0.")
        canonical = "kg"
    elif unit_str in ("kg", "kgs"):
        norm_qty = round(qty, 4)
        if norm_qty <= 0:
            raise InvalidInput(f"Quantity {qty} {raw_unit} rounds to <= 0.")
        canonical = "kg"
    elif unit_str == "ml":
        norm_qty = round(qty / 1000.0, 4)
        if norm_qty <= 0:
            raise InvalidInput(f"Quantity {qty} {raw_unit} rounds to <= 0.")
        canonical = "L"
    elif unit_str == "cl":
        norm_qty = round(qty / 100.0, 4)
        if norm_qty <= 0:
            raise InvalidInput(f"Quantity {qty} {raw_unit} rounds to <= 0.")
        canonical = "L"
    elif unit_str in ("l", "lt"):
        norm_qty = round(qty, 4)
        if norm_qty <= 0:
            raise InvalidInput(f"Quantity {qty} {raw_unit} rounds to <= 0.")
        canonical = "L"
    elif unit_str in ("pz", "pcs", "pc"):
        rounded_cnt = round(qty)
        if rounded_cnt <= 0:
            raise InvalidInput(f"Quantity {qty} {raw_unit} rounds to <= 0 count.")
        norm_qty = float(rounded_cnt)
        canonical = "pcs"
    else:
        raise InvalidInput(f"Unsupported unit: {raw_unit!r}.")

    if target_unit is not None:
        if not isinstance(target_unit, str) or not target_unit.strip():
            raise InvalidInput("target_unit must be a non-empty string.")
        tgt = target_unit.strip().lower().rstrip(".")
        if tgt in ("kg", "kgs", "g", "gr"):
            tgt_canonical = "kg"
        elif tgt in ("l", "lt", "ml", "cl"):
            tgt_canonical = "L"
        elif tgt in ("pz", "pcs", "pc"):
            tgt_canonical = "pcs"
        else:
            raise InvalidInput(f"Unsupported or invalid target_unit: {target_unit!r}.")

        if tgt_canonical != canonical:
            def _validate_factor(val, factor_name: str) -> float:
                if val is None or isinstance(val, bool):
                    raise InvalidInput(
                        f"Missing or invalid conversion factor {factor_name} "
                        f"for {canonical} -> {tgt_canonical}."
                    )
                try:
                    f_val = float(val)
                except (ValueError, TypeError) as e:
                    raise InvalidInput(
                        f"Invalid {factor_name} {val!r}: must be numeric."
                    ) from e
                if not math.isfinite(f_val) or f_val <= 0:
                    raise InvalidInput(
                        f"{factor_name} must be a positive finite number, got {val!r}."
                    )
                return f_val

            if canonical == "pcs" and tgt_canonical == "kg":
                gpp = _validate_factor(grams_per_piece, "grams_per_piece")
                norm_qty = round((norm_qty * gpp) / 1000.0, 4)
                if norm_qty <= 0:
                    raise InvalidInput(f"Quantity {qty} {raw_unit} rounds to <= 0 kg.")
                canonical = "kg"
            elif canonical == "kg" and tgt_canonical == "pcs":
                gpp = _validate_factor(grams_per_piece, "grams_per_piece")
                norm_qty = float(round((norm_qty * 1000.0) / gpp))
                if norm_qty <= 0:
                    raise InvalidInput(f"Quantity {qty} {raw_unit} rounds to <= 0 pcs.")
                canonical = "pcs"
            elif canonical == "L" and tgt_canonical == "kg":
                density = _validate_factor(density_kg_per_l, "density_kg_per_l")
                norm_qty = round(norm_qty * density, 4)
                if norm_qty <= 0:
                    raise InvalidInput(f"Quantity {qty} {raw_unit} rounds to <= 0 kg.")
                canonical = "kg"
            elif canonical == "kg" and tgt_canonical == "L":
                density = _validate_factor(density_kg_per_l, "density_kg_per_l")
                norm_qty = round(norm_qty / density, 4)
                if norm_qty <= 0:
                    raise InvalidInput(f"Quantity {qty} {raw_unit} rounds to <= 0 L.")
                canonical = "L"
            elif canonical == "pcs" and tgt_canonical == "L":
                gpp = _validate_factor(grams_per_piece, "grams_per_piece")
                density = _validate_factor(density_kg_per_l, "density_kg_per_l")
                norm_qty = round(((norm_qty * gpp) / 1000.0) / density, 4)
                if norm_qty <= 0:
                    raise InvalidInput(f"Quantity {qty} {raw_unit} rounds to <= 0 L.")
                canonical = "L"
            elif canonical == "L" and tgt_canonical == "pcs":
                density = _validate_factor(density_kg_per_l, "density_kg_per_l")
                gpp = _validate_factor(grams_per_piece, "grams_per_piece")
                norm_qty = float(round((norm_qty * density * 1000.0) / gpp))
                if norm_qty <= 0:
                    raise InvalidInput(f"Quantity {qty} {raw_unit} rounds to <= 0 pcs.")
                canonical = "pcs"
            else:
                raise InvalidInput(
                    f"Incompatible target_unit {target_unit!r} (canonical {tgt_canonical}) "
                    f"for raw_unit {raw_unit!r} (canonical {canonical})."
                )

    return norm_qty, canonical


def map_receipt_item_to_add_item_args(
    name: str,
    raw_quantity: float,
    raw_unit: str,
    packages: int = 1,
    location: str = "pantry",
    target_unit: str = None,
    expiration_date: str = None,
    fallback_id: int = None,
    new_product_macros: dict = None,
    grams_per_piece: float = None,
    density_kg_per_l: float = None,
) -> dict:
    """
    Transforms a reconciled receipt item into the exact kwargs dictionary
    expected by add_item().
    """
    if not name or not str(name).strip():
        raise InvalidInput("Item name cannot be empty.")
    name_clean = str(name).strip()

    norm_qty, canonical_unit = convert_receipt_quantity_unit(
        raw_quantity,
        raw_unit,
        target_unit=target_unit,
        grams_per_piece=grams_per_piece,
        density_kg_per_l=density_kg_per_l,
    )

    try:
        if isinstance(packages, bool):
            pkg_count = 1
        else:
            p_val = float(str(packages if packages is not None else 1))
            if not math.isfinite(p_val) or p_val > 2**63 - 1 or p_val < 0:
                raise OverflowError("Package count must be finite, non-negative, and within 64-bit int range.")
            pkg_count = max(1, int(p_val))
    except (ValueError, TypeError, OverflowError):
        pkg_count = 1

    if canonical_unit == "pcs":
        unit_weight = 1.0
        try:
            total_pcs = norm_qty * pkg_count
            if not math.isfinite(total_pcs) or total_pcs > 2**63 - 1 or total_pcs < 0:
                raise OverflowError("Total pieces must be finite, non-negative, and within 64-bit int range.")
            intact_items = max(1, int(round(total_pcs)))
        except (ValueError, TypeError, OverflowError):
            intact_items = 1
    else:
        unit_weight = norm_qty
        intact_items = pkg_count

    exp_date = expiration_date.strip() if (expiration_date and str(expiration_date).strip()) else None
    loc = str(location).strip() if (location and str(location).strip()) else "pantry"

    args = {
        "name": name_clean,
        "unit_weight": unit_weight,
        "intact_items": intact_items,
        "location": loc,
        "expiration_date": exp_date,
        "fallback_id": fallback_id,
    }

    if fallback_id is not None:
        args["new_product_unit"] = canonical_unit
        if new_product_macros:
            args["new_product_macros"] = new_product_macros

    return args


# ----------------------------------------------------------------------
# OPERATION 1: ADD ITEM
# ----------------------------------------------------------------------

def add_item(
    name: str,
    unit_weight: float,
    intact_items: int,
    location: str,
    expiration_date: str = None,
    fallback_id: int = None,
    new_product_unit: str = None,
    new_product_macros: dict = None,
    db_path: str = None,
    conn: sqlite3.Connection = None,
) -> dict:
    """
    Adds a new instance of a product to the inventory.

    Parameters
    ----------
    name : name typed by the user (e.g. "Penne Rigate")
    unit_weight : weight/volume of a single package (> 0)
    intact_items : how many whole packages are being added (> 0)
    location : physical storage location
    expiration_date : optional expiry date in ISO-8601 format (YYYY-MM-DD).
                      Pass None (default) to leave it unset.
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
    db_path : optional path to SQLite database file.
    conn : optional caller-managed sqlite3.Connection. If provided,
           the caller is responsible for committing, rolling back, and closing.

    Returns a dictionary with the outcome of the operation.
    """
    if (
        unit_weight is None
        or isinstance(unit_weight, bool)
        or not isinstance(unit_weight, (int, float))
        or not math.isfinite(unit_weight)
        or unit_weight <= 0
    ):
        raise InvalidInput(
            f"unit_weight must be a positive finite number, got {unit_weight!r}."
        )
    unit_weight = float(unit_weight)

    if (
        intact_items is None
        or isinstance(intact_items, bool)
        or not isinstance(intact_items, int)
        or intact_items <= 0
    ):
        raise InvalidInput(
            f"intact_items must be a positive integer, got {intact_items!r}."
        )
    if not location:
        raise InvalidInput("Location is required.")
    if expiration_date is not None:
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', expiration_date):
            raise InvalidInput(
                "expiration_date must be in YYYY-MM-DD format (e.g. 2026-12-31)."
            )

    manage_conn = (conn is None)
    if manage_conn:
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
                for k in required:
                    val = new_product_macros.get(k)
                    if (
                        val is None
                        or isinstance(val, bool)
                        or not isinstance(val, (int, float))
                        or not math.isfinite(val)
                        or val < 0
                    ):
                        raise InvalidInput(
                            f"new_product_macros: '{k}' must be a non-negative finite number, got {val!r}."
                        )
                for k in ("Sugars_100g", "Saturated_fat_100g", "Fiber_100g", "Salt_100g"):
                    if k in new_product_macros and new_product_macros[k] is not None:
                        val = new_product_macros[k]
                        if (
                            isinstance(val, bool)
                            or not isinstance(val, (int, float))
                            or not math.isfinite(val)
                            or val < 0
                        ):
                            raise InvalidInput(
                                f"new_product_macros: '{k}' must be a non-negative finite number, got {val!r}."
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
                (ID, Unit_Weight, Intact_Items, Open_Remainder, Location, Expiration_Date)
            VALUES (?, ?, ?, 0, ?, ?)
            """,
            (product_id, unit_weight, intact_items, location, expiration_date),
        )
        created_row_id = cur.lastrowid

        if manage_conn:
            conn.commit()
        return {
            "outcome": "ok",
            "case": case,
            "ID": product_id,
            "ID_row_inventory": created_row_id,
        }

    except sqlite3.IntegrityError as e:
        if manage_conn:
            conn.rollback()
        err_msg = str(e).lower()
        if "not null" in err_msg:
            raise InvalidInput(f"Database NOT NULL constraint violation: {e}") from e
        elif "unique" in err_msg or "primary key" in err_msg:
            raise AliasConflict(f"Integrity conflict during insertion: {e}") from e
        else:
            raise InvalidInput(f"Database integrity constraint violation: {e}") from e
    except Exception:
        if manage_conn:
            conn.rollback()
        raise
    finally:
        if manage_conn:
            conn.close()


# ----------------------------------------------------------------------
# OPERATION 2: CONSUME QUANTITY
# ----------------------------------------------------------------------

def consume_quantity(name: str, quantity_to_consume: float, db_path: str = None) -> dict:
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
    if (
        quantity_to_consume is None
        or isinstance(quantity_to_consume, bool)
        or not isinstance(quantity_to_consume, (int, float))
        or not math.isfinite(quantity_to_consume)
        or quantity_to_consume <= 0
    ):
        raise InvalidInput(
            f"The quantity to consume must be a positive finite number, got {quantity_to_consume!r}."
        )

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
                    if remaining_to_consume <= 0 or item_count <= 0:
                        continue
                    max_weight = item_count * unit_weight
                    if remaining_to_consume >= max_weight:
                        remaining_to_consume = _round_down_to_hundredth(remaining_to_consume - max_weight)
                        item_count = 0
                        new_row_remainder = 0.0
                    else:
                        full_packs = int(remaining_to_consume // unit_weight)
                        rem = _round_down_to_hundredth(remaining_to_consume - (full_packs * unit_weight))
                        if rem > 0:
                            item_count -= (full_packs + 1)
                            new_row_remainder = _round_down_to_hundredth(unit_weight - rem)
                        else:
                            item_count -= full_packs
                            new_row_remainder = 0.0
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

def get_shopping_list(db_path: str = None) -> list:
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

def get_available_inventory(db_path: str = None) -> list:
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
            "Location": ...,
            "Expiration_Date": ...   # nearest (MIN) expiry date in the group, or None
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
                PRODUCTS.Unit,
                MIN(INVENTORY.Expiration_Date) AS Nearest_Expiration,
                (SELECT a.Name FROM ALIASES a WHERE a.ID = INVENTORY.ID ORDER BY a.rowid ASC LIMIT 1) AS Ref_Name
            FROM INVENTORY
            JOIN PRODUCTS ON PRODUCTS.ID = INVENTORY.ID
            GROUP BY INVENTORY.ID, INVENTORY.Location
            ORDER BY INVENTORY.ID, INVENTORY.Location
            """
        )
        rows = cur.fetchall()

        result = []
        for product_id, location, total, unit_of_measure, nearest_expiration, ref_name in rows:
            name = ref_name
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
                    "Expiration_Date": nearest_expiration,
                }
            )
        return result
    finally:
        conn.close()


# ----------------------------------------------------------------------
# LOCATION MANAGEMENT
# ----------------------------------------------------------------------

def get_locations(db_path: str = None, conn: sqlite3.Connection = None) -> list:
    """Returns the sorted list of location names from the LOCATIONS catalog."""
    manage_conn = (conn is None)
    if manage_conn:
        conn = get_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT Name FROM LOCATIONS ORDER BY Name")
        return [row[0] for row in cur.fetchall()]
    finally:
        if manage_conn:
            conn.close()


def add_location(name: str, db_path: str = None) -> None:
    """Adds a new location to the LOCATIONS catalog.

    Raises InvalidInput if the name is empty or already present.
    """
    if not name or not name.strip():
        raise InvalidInput("Location name cannot be empty.")
    name = name.strip()
    conn = get_connection(db_path)
    try:
        conn.execute("INSERT INTO LOCATIONS (Name) VALUES (?)", (name,))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        raise InvalidInput(f"Location '{name}' already exists.")
    finally:
        conn.close()


def rename_location(old_name: str, new_name: str, db_path: str = None) -> None:
    """Renames a location in LOCATIONS and updates all INVENTORY rows that
    reference the old name.

    Raises InvalidInput if new_name is empty or already exists.
    Raises ProductNotFound if old_name is not in LOCATIONS.
    """
    if not new_name or not new_name.strip():
        raise InvalidInput("New location name cannot be empty.")
    new_name = new_name.strip()
    conn = get_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM LOCATIONS WHERE Name = ?", (old_name,))
        if cur.fetchone() is None:
            raise ProductNotFound(f"Location '{old_name}' does not exist.")
        # Rename in LOCATIONS (INSERT new + DELETE old to handle PK constraint)
        cur.execute("INSERT INTO LOCATIONS (Name) VALUES (?)", (new_name,))
        cur.execute("UPDATE INVENTORY SET Location = ? WHERE Location = ?", (new_name, old_name))
        cur.execute("DELETE FROM LOCATIONS WHERE Name = ?", (old_name,))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        raise InvalidInput(f"Location '{new_name}' already exists.")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def delete_location(name: str, db_path: str = None) -> None:
    """Deletes a location from the LOCATIONS catalog.

    Raises InvalidInput if any INVENTORY row still references the location.
    Raises ProductNotFound if the location does not exist.
    """
    conn = get_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM LOCATIONS WHERE Name = ?", (name,))
        if cur.fetchone() is None:
            raise ProductNotFound(f"Location '{name}' does not exist.")
        cur.execute("SELECT COUNT(*) FROM INVENTORY WHERE Location = ?", (name,))
        count = cur.fetchone()[0]
        if count > 0:
            raise InvalidInput(
                f"Cannot delete '{name}': {count} inventory row(s) still use this location. "
                "Consume or move them first."
            )
        conn.execute("DELETE FROM LOCATIONS WHERE Name = ?", (name,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ----------------------------------------------------------------------
# READ 3: EXCLUSION LIST
# ----------------------------------------------------------------------

def get_exclusion_list(target_date: str = None, db_path: str = None, conn: sqlite3.Connection = None) -> list:
    """
    Returns the exclusion list: all inventory items that are expired or will expire
    before `target_date` (ISO-8601 YYYY-MM-DD). If `target_date` is None, uses today.
    Each entry is a dict with keys: ID, Name, Expiration_Date, Location.
    Items with no Expiration_Date are never excluded.
    The result is also persisted in the EXCLUSION_LIST table (replaced each call).
    If `conn` is provided, reuses the caller's connection directly.
    """
    from datetime import date as _date
    ref = target_date or _date.today().isoformat()
    close_at_end = False
    if conn is None:
        conn = get_connection(db_path)
        close_at_end = True
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT DISTINCT i.ID,
                   (SELECT a.Name FROM ALIASES a WHERE a.ID = i.ID ORDER BY a.rowid ASC LIMIT 1) AS Ref_Name,
                   MIN(i.Expiration_Date) AS Nearest_Exp,
                   i.Location
            FROM INVENTORY i
            WHERE i.Expiration_Date IS NOT NULL
              AND i.Expiration_Date <= ?
            GROUP BY i.ID, i.Location
        """, (ref,))
        rows = cur.fetchall()
        result = [{"ID": r[0], "Name": r[1], "Expiration_Date": r[2], "Location": r[3]} for r in rows]
        # Persist: replace EXCLUSION_LIST content
        conn.execute("DELETE FROM EXCLUSION_LIST")
        conn.executemany(
            "INSERT OR IGNORE INTO EXCLUSION_LIST (ID, Ref_Name, Expiration_Date, Location) VALUES (?,?,?,?)",
            [(r["ID"], r["Name"], r["Expiration_Date"], r["Location"]) for r in result]
        )
        conn.commit()
        return result
    finally:
        if close_at_end:
            conn.close()


# ----------------------------------------------------------------------
# RECONCILIATION & CATALOG HELPERS
# ----------------------------------------------------------------------

def get_catalog_for_reconciliation(
    db_path: str = None, conn: sqlite3.Connection = None
) -> dict:
    """
    Queries PRODUCTS and ALIASES to build an in-memory catalog for receipt reconciliation.

    Returns
    -------
    dict with:
      - 'products': list of dicts with keys 'id', 'name', 'unit', 'kcal', 'protein', 'carbs', 'fat'
      - 'alias_map': dict mapping lowercase alias Name to product dict
    """
    manage_conn = (conn is None)
    if manage_conn:
        conn = get_connection(db_path)
    try:
        cur = conn.cursor()

        # 1. Fetch canonical names (earliest alias by rowid for each product ID)
        cur.execute("""
            SELECT a.ID, a.Name
            FROM ALIASES a
            WHERE a.rowid = (SELECT MIN(a2.rowid) FROM ALIASES a2 WHERE a2.ID = a.ID)
        """)
        canonical_names = {row[0]: row[1] for row in cur.fetchall()}

        # 2. Fetch all products
        cur.execute("PRAGMA table_info(PRODUCTS)")
        cols = {row[1] for row in cur.fetchall()}
        piece_col = "Grams_per_piece" if "Grams_per_piece" in cols else "NULL AS Grams_per_piece"
        density_col = "Density_kg_per_L" if "Density_kg_per_L" in cols else "NULL AS Density_kg_per_L"

        cur.execute(f"""
            SELECT ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g,
                   Sugars_100g, Saturated_fat_100g, Fiber_100g, Salt_100g,
                   {piece_col}, {density_col}
            FROM PRODUCTS
            ORDER BY ID ASC
        """)
        prod_rows = cur.fetchall()

        products_by_id = {}
        products_list = []
        for r in prod_rows:
            pid = r[0]
            name = canonical_names.get(pid, f"Product #{pid}")
            p_dict = {
                "id": pid,
                "name": name,
                "canonical_name": name,
                "unit": r[1],
                "kcal": r[2],
                "protein": r[3],
                "carbs": r[4],
                "fat": r[5],
                "grams_per_piece": r[10] if len(r) > 10 else None,
                "density_kg_per_l": r[11] if len(r) > 11 else None,
                "macros": {
                    "Kcal_100g": r[2],
                    "Protein_100g": r[3],
                    "Carbs_100g": r[4],
                    "Fat_100g": r[5],
                    "Sugars_100g": r[6],
                    "Saturated_fat_100g": r[7],
                    "Fiber_100g": r[8],
                    "Salt_100g": r[9],
                },
            }
            products_by_id[pid] = p_dict
            products_list.append(p_dict)

        # Sort products by canonical name
        products_list.sort(key=lambda p: p["name"].lower())

        # 3. Build alias lookup dictionary with lowercased keys
        cur.execute("SELECT Name, ID FROM ALIASES")
        alias_map = {}
        for alias_name, pid in cur.fetchall():
            if pid in products_by_id:
                alias_map[alias_name.strip().lower()] = products_by_id[pid]

        return {
            "products": products_list,
            "alias_map": alias_map,
        }
    finally:
        if manage_conn:
            conn.close()


def get_next_product_id(db_path: str = None, conn: sqlite3.Connection = None) -> int:
    """Returns the next available integer ID for PRODUCTS (SELECT COALESCE(MAX(ID), 0) + 1 FROM PRODUCTS)."""
    manage_conn = (conn is None)
    if manage_conn:
        conn = get_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT COALESCE(MAX(ID), 0) + 1 FROM PRODUCTS")
        return cur.fetchone()[0]
    finally:
        if manage_conn:
            conn.close()


# ----------------------------------------------------------------------
# INVENTORY ITEM EDITING, DELETION & MACRO PROPAGATION
# ----------------------------------------------------------------------

def _normalize_macro_dict(macros: dict) -> dict:
    """Normalizes variations of macro dictionary keys to standard schema column names."""
    if not macros:
        return {}
    key_map = {
        "kcal": "Kcal_100g",
        "kcal_100g": "Kcal_100g",
        "protein": "Protein_100g",
        "protein_100g": "Protein_100g",
        "prot": "Protein_100g",
        "carbs": "Carbs_100g",
        "carbs_100g": "Carbs_100g",
        "carb": "Carbs_100g",
        "fat": "Fat_100g",
        "fat_100g": "Fat_100g",
        "sugars": "Sugars_100g",
        "sugars_100g": "Sugars_100g",
        "saturated_fat": "Saturated_fat_100g",
        "saturated_fat_100g": "Saturated_fat_100g",
        "fiber": "Fiber_100g",
        "fiber_100g": "Fiber_100g",
        "salt": "Salt_100g",
        "salt_100g": "Salt_100g",
    }
    normalized = {}
    for k, v in macros.items():
        canonical = key_map.get(str(k).lower(), k)
        normalized[canonical] = v
    return normalized


def get_inventory_rows(db_path: str = None, conn: sqlite3.Connection = None) -> list:
    """
    Returns all individual rows from the INVENTORY table, joined with PRODUCTS
    and resolving the canonical Name from ALIASES.

    Returns a list of dictionaries with full item characteristics:
      Row_ID, ID, Name, Unit_Weight, Intact_Items, Open_Remainder, Location,
      Expiration_Date, Unit, Total_Available_Weight, Kcal_100g, Protein_100g,
      Carbs_100g, Fat_100g, Sugars_100g, Saturated_fat_100g, Fiber_100g,
      Salt_100g, Grams_per_piece, Density_kg_per_L, macros.
    """
    manage_conn = (conn is None)
    if manage_conn:
        conn = get_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                i.Row_ID,
                i.ID,
                (SELECT a.Name FROM ALIASES a WHERE a.ID = i.ID ORDER BY a.rowid ASC LIMIT 1) AS Name,
                i.Unit_Weight,
                i.Intact_Items,
                i.Open_Remainder,
                i.Location,
                i.Expiration_Date,
                p.Unit,
                p.Kcal_100g,
                p.Protein_100g,
                p.Carbs_100g,
                p.Fat_100g,
                p.Sugars_100g,
                p.Saturated_fat_100g,
                p.Fiber_100g,
                p.Salt_100g,
                p.Grams_per_piece,
                p.Density_kg_per_L
            FROM INVENTORY i
            JOIN PRODUCTS p ON p.ID = i.ID
            ORDER BY i.Row_ID ASC
            """
        )
        rows = cur.fetchall()
        result = []
        for r in rows:
            unit = r[8]
            u_weight = r[3]
            intact = r[4]
            open_rem = r[5]
            if unit == "pcs":
                tot = intact
            else:
                tot = _round_down_to_hundredth((intact * u_weight) + open_rem)
            macros = {
                "Kcal_100g": r[9],
                "Protein_100g": r[10],
                "Carbs_100g": r[11],
                "Fat_100g": r[12],
                "Sugars_100g": r[13],
                "Saturated_fat_100g": r[14],
                "Fiber_100g": r[15],
                "Salt_100g": r[16],
            }
            result.append({
                "Row_ID": r[0],
                "ID": r[1],
                "Name": r[2] or f"Product #{r[1]}",
                "Unit_Weight": u_weight,
                "Intact_Items": intact,
                "Open_Remainder": open_rem,
                "Location": r[6],
                "Expiration_Date": r[7],
                "Unit": unit,
                "Total_Available_Weight": tot,
                "Kcal_100g": r[9],
                "Protein_100g": r[10],
                "Carbs_100g": r[11],
                "Fat_100g": r[12],
                "Sugars_100g": r[13],
                "Saturated_fat_100g": r[14],
                "Fiber_100g": r[15],
                "Salt_100g": r[16],
                "Grams_per_piece": r[17],
                "Density_kg_per_L": r[18],
                "macros": macros,
            })
        return result
    finally:
        if manage_conn:
            conn.close()


def get_inventory_item(row_id: int, db_path: str = None, conn: sqlite3.Connection = None) -> dict:
    """Returns a single inventory item dict by Row_ID, or None if not found."""
    manage_conn = (conn is None)
    if manage_conn:
        conn = get_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                i.Row_ID,
                i.ID,
                (SELECT a.Name FROM ALIASES a WHERE a.ID = i.ID ORDER BY a.rowid ASC LIMIT 1) AS Name,
                i.Unit_Weight,
                i.Intact_Items,
                i.Open_Remainder,
                i.Location,
                i.Expiration_Date,
                p.Unit,
                p.Kcal_100g,
                p.Protein_100g,
                p.Carbs_100g,
                p.Fat_100g,
                p.Sugars_100g,
                p.Saturated_fat_100g,
                p.Fiber_100g,
                p.Salt_100g,
                p.Grams_per_piece,
                p.Density_kg_per_L
            FROM INVENTORY i
            JOIN PRODUCTS p ON p.ID = i.ID
            WHERE i.Row_ID = ?
            """,
            (row_id,)
        )
        r = cur.fetchone()
        if not r:
            return None
        unit = r[8]
        u_weight = r[3]
        intact = r[4]
        open_rem = r[5]
        if unit == "pcs":
            tot = intact
        else:
            tot = _round_down_to_hundredth((intact * u_weight) + open_rem)
        macros = {
            "Kcal_100g": r[9],
            "Protein_100g": r[10],
            "Carbs_100g": r[11],
            "Fat_100g": r[12],
            "Sugars_100g": r[13],
            "Saturated_fat_100g": r[14],
            "Fiber_100g": r[15],
            "Salt_100g": r[16],
        }
        return {
            "Row_ID": r[0],
            "ID": r[1],
            "Name": r[2] or f"Product #{r[1]}",
            "Unit_Weight": u_weight,
            "Intact_Items": intact,
            "Open_Remainder": open_rem,
            "Location": r[6],
            "Expiration_Date": r[7],
            "Unit": unit,
            "Total_Available_Weight": tot,
            "Kcal_100g": r[9],
            "Protein_100g": r[10],
            "Carbs_100g": r[11],
            "Fat_100g": r[12],
            "Sugars_100g": r[13],
            "Saturated_fat_100g": r[14],
            "Fiber_100g": r[15],
            "Salt_100g": r[16],
            "Grams_per_piece": r[17],
            "Density_kg_per_L": r[18],
            "macros": macros,
        }
    finally:
        if manage_conn:
            conn.close()


def delete_inventory_item(conn_or_row_id=None, row_id: int = None, db_path: str = None, conn: sqlite3.Connection = None) -> bool:
    """
    Deletes a single item row from INVENTORY by Row_ID.
    Accepts delete_inventory_item(conn, row_id) or delete_inventory_item(row_id=row_id, db_path=db_path).
    Returns True if a row was deleted, False if no row with that Row_ID was found.
    """
    manage_conn = False
    if isinstance(conn_or_row_id, sqlite3.Connection) or hasattr(conn_or_row_id, "cursor"):
        active_conn = conn_or_row_id
        target_row_id = row_id
    elif conn is not None:
        active_conn = conn
        target_row_id = conn_or_row_id if row_id is None else row_id
    else:
        target_row_id = conn_or_row_id if row_id is None else row_id
        active_conn = get_connection(db_path)
        manage_conn = True

    if target_row_id is None:
        if manage_conn:
            active_conn.close()
        raise InvalidInput("row_id must be provided.")
    try:
        target_row_id = int(target_row_id)
        if target_row_id <= 0:
            raise ValueError
    except (ValueError, TypeError):
        if manage_conn:
            active_conn.close()
        raise InvalidInput("row_id must be a positive integer.")

    try:
        cur = active_conn.cursor()
        cur.execute("DELETE FROM INVENTORY WHERE Row_ID = ?", (target_row_id,))
        deleted = (cur.rowcount > 0)
        if manage_conn:
            active_conn.commit()
        return deleted
    except Exception:
        if manage_conn:
            active_conn.rollback()
        raise
    finally:
        if manage_conn:
            active_conn.close()


def is_product_macro_linked(conn_or_pid=None, product_id: int = None, db_path: str = None, conn: sqlite3.Connection = None) -> bool:
    """
    Returns True if product_id is referenced across multiple instances in the database:
    - More than 1 alias in ALIASES
    - More than 1 inventory row in INVENTORY
    - Any usage in RECIPE_INGREDIENTS or GROUP_MEMBERS
    Accepts is_product_macro_linked(conn, product_id) or is_product_macro_linked(product_id).
    """
    manage_conn = False
    if isinstance(conn_or_pid, sqlite3.Connection) or hasattr(conn_or_pid, "cursor"):
        active_conn = conn_or_pid
        pid = product_id
    elif conn is not None:
        active_conn = conn
        pid = conn_or_pid if product_id is None else product_id
    else:
        pid = conn_or_pid if product_id is None else product_id
        active_conn = get_connection(db_path)
        manage_conn = True

    if pid is None:
        if manage_conn:
            active_conn.close()
        raise InvalidInput("product_id must be provided.")
    try:
        pid = int(pid)
    except (ValueError, TypeError):
        if manage_conn:
            active_conn.close()
        raise InvalidInput("product_id must be a valid integer.")

    try:
        cur = active_conn.cursor()
        cur.execute("SELECT COUNT(*) FROM ALIASES WHERE ID = ?", (pid,))
        alias_count = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM INVENTORY WHERE ID = ?", (pid,))
        inv_count = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM RECIPE_INGREDIENTS WHERE ID = ?", (pid,))
        recipe_count = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM GROUP_MEMBERS WHERE ID = ?", (pid,))
        group_count = cur.fetchone()[0]

        return (alias_count > 1 or inv_count > 1 or recipe_count > 0 or group_count > 0)
    finally:
        if manage_conn:
            active_conn.close()


def update_inventory_item(
    conn_or_row_id=None,
    row_id: int = None,
    item_fields: dict = None,
    macro_fields: dict = None,
    propagate: bool = False,
    db_path: str = None,
    conn: sqlite3.Connection = None,
) -> dict:
    """
    Updates an inventory row's characteristics and/or macronutrients.

    If macronutrients are changed and the product is linked across multiple instances:
      - If propagate is False (Option 1): allocates a new product ID via get_next_product_id(),
        creates a new product record, updates/creates an alias, and re-points this inventory row
        to the new product ID, leaving other linked instances untouched.
      - If propagate is True (Option 2): updates the PRODUCTS record in place, preserving
        the product ID so all linked inventory rows and recipes reflect the change backwards.

    Parameters
    ----------
    conn_or_row_id : connection or row_id
    row_id : int (if conn_or_row_id was a connection)
    item_fields : dict with inventory attributes:
                  'Name', 'Unit_Weight', 'Intact_Items', 'Open_Remainder',
                  'Location', 'Expiration_Date', 'Unit', 'Grams_per_piece', 'Density_kg_per_L'
    macro_fields : dict with macronutrients:
                   'Kcal_100g', 'Protein_100g', 'Carbs_100g', 'Fat_100g',
                   'Sugars_100g', 'Saturated_fat_100g', 'Fiber_100g', 'Salt_100g'
    propagate : bool, whether to propagate macro changes backwards across all linked instances
    db_path : optional path to database file
    conn : optional connection

    Returns
    -------
    dict with outcome details.
    """
    manage_conn = False
    if isinstance(conn_or_row_id, sqlite3.Connection) or hasattr(conn_or_row_id, "cursor"):
        active_conn = conn_or_row_id
        target_row_id = row_id
    elif conn is not None:
        active_conn = conn
        target_row_id = conn_or_row_id if row_id is None else row_id
    else:
        target_row_id = conn_or_row_id if row_id is None else row_id
        active_conn = get_connection(db_path)
        manage_conn = True

    if target_row_id is None:
        if manage_conn:
            active_conn.close()
        raise InvalidInput("row_id must be provided.")
    try:
        target_row_id = int(target_row_id)
        if target_row_id <= 0:
            raise ValueError
    except (ValueError, TypeError):
        if manage_conn:
            active_conn.close()
        raise InvalidInput("row_id must be a positive integer.")

    item_fields = dict(item_fields or {})
    macro_fields = _normalize_macro_dict(macro_fields or {})

    try:
        cur = active_conn.cursor()

        # 1. Fetch current row and product info
        cur.execute(
            """
            SELECT i.ID, i.Unit_Weight, i.Intact_Items, i.Open_Remainder, i.Location, i.Expiration_Date,
                   p.Unit, p.Kcal_100g, p.Protein_100g, p.Carbs_100g, p.Fat_100g,
                   p.Sugars_100g, p.Saturated_fat_100g, p.Fiber_100g, p.Salt_100g,
                   p.Grams_per_piece, p.Density_kg_per_L
            FROM INVENTORY i
            JOIN PRODUCTS p ON p.ID = i.ID
            WHERE i.Row_ID = ?
            """,
            (target_row_id,)
        )
        current = cur.fetchone()
        if not current:
            raise ProductNotFound(f"Inventory row with Row_ID {target_row_id} not found.")

        orig_pid = current[0]
        orig_u_weight = current[1]
        orig_intact = current[2]
        orig_rem = current[3]
        orig_loc = current[4]
        orig_exp = current[5]
        orig_unit = current[6]
        orig_macros = {
            "Kcal_100g": current[7],
            "Protein_100g": current[8],
            "Carbs_100g": current[9],
            "Fat_100g": current[10],
            "Sugars_100g": current[11],
            "Saturated_fat_100g": current[12],
            "Fiber_100g": current[13],
            "Salt_100g": current[14],
        }
        orig_piece = current[15]
        orig_density = current[16]

        # 2. Parse and validate item_fields
        # Unit_Weight
        if "Unit_Weight" in item_fields and item_fields["Unit_Weight"] is not None:
            try:
                final_u_weight = float(item_fields["Unit_Weight"])
                if not math.isfinite(final_u_weight) or final_u_weight <= 0:
                    raise ValueError
            except (ValueError, TypeError):
                raise InvalidInput("Unit_Weight must be a positive number.")
        else:
            final_u_weight = orig_u_weight

        # Intact_Items
        if "Intact_Items" in item_fields and item_fields["Intact_Items"] is not None:
            try:
                final_intact = int(item_fields["Intact_Items"])
                if final_intact < 0:
                    raise ValueError
            except (ValueError, TypeError):
                raise InvalidInput("Intact_Items must be a non-negative integer.")
        else:
            final_intact = orig_intact

        # Open_Remainder
        if "Open_Remainder" in item_fields and item_fields["Open_Remainder"] is not None:
            try:
                final_rem = float(item_fields["Open_Remainder"])
                if not math.isfinite(final_rem) or final_rem < 0:
                    raise ValueError
            except (ValueError, TypeError):
                raise InvalidInput("Open_Remainder must be a non-negative number.")
        else:
            final_rem = orig_rem

        if final_intact == 0 and final_rem <= 0:
            raise InvalidInput("Item quantity cannot be 0. To remove this item, use the delete button.")

        # Location
        if "Location" in item_fields:
            loc_val = str(item_fields["Location"]).strip()
            if not loc_val:
                raise InvalidInput("Location cannot be empty.")
            final_loc = loc_val
        else:
            final_loc = orig_loc

        # Expiration_Date
        if "Expiration_Date" in item_fields:
            exp_val = item_fields["Expiration_Date"]
            if exp_val is not None and str(exp_val).strip():
                exp_str = str(exp_val).strip()
                from datetime import date as _date_cls
                try:
                    _date_cls.fromisoformat(exp_str)
                    if not re.match(r"^\d{4}-[0-1]\d-[0-3]\d$", exp_str):
                        raise ValueError
                    final_exp = exp_str
                except Exception:
                    raise InvalidInput("Expiration_Date must be in YYYY-MM-DD format.")
            else:
                final_exp = None
        else:
            final_exp = orig_exp

        # Unit
        if "Unit" in item_fields and item_fields["Unit"]:
            u_val = str(item_fields["Unit"]).strip()
            if u_val not in ("kg", "L", "pcs"):
                raise InvalidInput("Unit must be 'kg', 'L', or 'pcs'.")
            final_unit = u_val
        else:
            final_unit = orig_unit

        # Grams_per_piece
        if "Grams_per_piece" in item_fields:
            gpp_val = item_fields["Grams_per_piece"]
            if gpp_val is not None and str(gpp_val).strip():
                try:
                    final_piece = float(gpp_val)
                    if not math.isfinite(final_piece) or final_piece <= 0:
                        raise ValueError
                except (ValueError, TypeError):
                    raise InvalidInput("Grams_per_piece must be a positive number.")
            else:
                final_piece = None
        else:
            final_piece = orig_piece

        # Density_kg_per_L
        if "Density_kg_per_L" in item_fields:
            den_val = item_fields["Density_kg_per_L"]
            if den_val is not None and str(den_val).strip():
                try:
                    final_density = float(den_val)
                    if not math.isfinite(final_density) or final_density <= 0:
                        raise ValueError
                except (ValueError, TypeError):
                    raise InvalidInput("Density_kg_per_L must be a positive number.")
            else:
                final_density = None
        else:
            final_density = orig_density

        # 3. Macro fields parsing and change detection
        final_macros = dict(orig_macros)
        macros_changed = False
        if macro_fields:
            for k in ("Kcal_100g", "Protein_100g", "Carbs_100g", "Fat_100g",
                      "Sugars_100g", "Saturated_fat_100g", "Fiber_100g", "Salt_100g"):
                if k in macro_fields:
                    raw_val = macro_fields[k]
                    if raw_val is not None and str(raw_val).strip():
                        try:
                            num_val = float(raw_val)
                            if not math.isfinite(num_val) or num_val < 0:
                                raise ValueError
                        except (ValueError, TypeError):
                            raise InvalidInput(f"{k} must be a non-negative number.")
                        final_macros[k] = num_val
                        old_val = orig_macros.get(k)
                        if old_val is None or abs(old_val - num_val) > 1e-5:
                            macros_changed = True
                    else:
                        if k in ("Kcal_100g", "Protein_100g", "Carbs_100g", "Fat_100g"):
                            raise InvalidInput(f"{k} is required and cannot be empty.")
                        final_macros[k] = None
                        if orig_macros.get(k) is not None:
                            macros_changed = True

        if final_unit != orig_unit:
            macros_changed = True

        # Check if product is linked across multiple instances
        is_linked = is_product_macro_linked(active_conn, orig_pid)

        final_pid = orig_pid
        created_new_product = False

        if macros_changed and is_linked and not propagate:
            # Option 1: Modify only that specific item (decouple to new product ID)
            new_pid = get_next_product_id(conn=active_conn)
            cur.execute(
                """
                INSERT INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g,
                                      Sugars_100g, Saturated_fat_100g, Fiber_100g, Salt_100g,
                                      Grams_per_piece, Density_kg_per_L)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_pid,
                    final_unit,
                    final_macros["Kcal_100g"],
                    final_macros["Protein_100g"],
                    final_macros["Carbs_100g"],
                    final_macros["Fat_100g"],
                    final_macros.get("Sugars_100g"),
                    final_macros.get("Saturated_fat_100g"),
                    final_macros.get("Fiber_100g"),
                    final_macros.get("Salt_100g"),
                    final_piece,
                    final_density,
                )
            )

            # Assign alias for new product
            input_name = item_fields.get("Name")
            if input_name and str(input_name).strip():
                base_name = str(input_name).strip()
            else:
                cur.execute("SELECT Name FROM ALIASES WHERE ID = ? ORDER BY rowid ASC LIMIT 1", (orig_pid,))
                row = cur.fetchone()
                base_name = row[0] if row else f"Product #{orig_pid}"

            # Check if name is already in ALIASES
            cur.execute("SELECT ID FROM ALIASES WHERE Name = ?", (base_name,))
            ex_alias = cur.fetchone()
            if ex_alias is None:
                cur.execute("INSERT INTO ALIASES (Name, ID) VALUES (?, ?)", (base_name, new_pid))
            elif ex_alias[0] == new_pid:
                pass
            else:
                # Disambiguate name for isolated product
                cand = f"{base_name} (edited)"
                cur.execute("SELECT 1 FROM ALIASES WHERE Name = ?", (cand,))
                if cur.fetchone():
                    cand = f"{base_name} (#{new_pid})"
                cur.execute("INSERT INTO ALIASES (Name, ID) VALUES (?, ?)", (cand, new_pid))

            final_pid = new_pid
            created_new_product = True

        else:
            # Option 2 (propagate) OR unlinked product OR non-macro updates
            if macros_changed or (item_fields and any(k in item_fields for k in ("Unit", "Grams_per_piece", "Density_kg_per_L"))):
                cur.execute(
                    """
                    UPDATE PRODUCTS SET
                        Unit = ?,
                        Kcal_100g = ?,
                        Protein_100g = ?,
                        Carbs_100g = ?,
                        Fat_100g = ?,
                        Sugars_100g = ?,
                        Saturated_fat_100g = ?,
                        Fiber_100g = ?,
                        Salt_100g = ?,
                        Grams_per_piece = ?,
                        Density_kg_per_L = ?
                    WHERE ID = ?
                    """,
                    (
                        final_unit,
                        final_macros["Kcal_100g"],
                        final_macros["Protein_100g"],
                        final_macros["Carbs_100g"],
                        final_macros["Fat_100g"],
                        final_macros.get("Sugars_100g"),
                        final_macros.get("Saturated_fat_100g"),
                        final_macros.get("Fiber_100g"),
                        final_macros.get("Salt_100g"),
                        final_piece,
                        final_density,
                        orig_pid,
                    )
                )

            # Name update for existing product if changed
            input_name = item_fields.get("Name")
            if input_name and str(input_name).strip():
                clean_name = str(input_name).strip()
                cur.execute("SELECT ID FROM ALIASES WHERE Name = ?", (clean_name,))
                ex_alias = cur.fetchone()
                if ex_alias is None:
                    cur.execute("INSERT INTO ALIASES (Name, ID) VALUES (?, ?)", (clean_name, orig_pid))
                elif ex_alias[0] != orig_pid:
                    raise AliasConflict(f"Name '{clean_name}' already exists for another product ID {ex_alias[0]}.")

        # Ensure location is registered in LOCATIONS
        if final_loc:
            cur.execute("INSERT OR IGNORE INTO LOCATIONS (Name) VALUES (?)", (final_loc,))

        # Update INVENTORY row
        cur.execute(
            """
            UPDATE INVENTORY SET
                ID = ?,
                Unit_Weight = ?,
                Intact_Items = ?,
                Open_Remainder = ?,
                Location = ?,
                Expiration_Date = ?
            WHERE Row_ID = ?
            """,
            (
                final_pid,
                final_u_weight,
                final_intact,
                final_rem,
                final_loc,
                final_exp,
                target_row_id,
            )
        )

        if manage_conn:
            active_conn.commit()

        return {
            "outcome": "ok",
            "row_id": target_row_id,
            "product_id": final_pid,
            "previous_product_id": orig_pid,
            "propagated": propagate,
            "created_new_product": created_new_product,
            "macros_changed": macros_changed,
        }

    except Exception:
        if manage_conn:
            active_conn.rollback()
        raise
    finally:
        if manage_conn:
            active_conn.close()


# ----------------------------------------------------------------------
# DESTRUCTIVE ACTIONS & RECEIPT LOGGING (Milestone M2)
# ----------------------------------------------------------------------

def clear_inventory(conn=None, db_path=None) -> bool:
    """
    Destructive hard-delete of the entire inventory.
    Atomically deletes all items from INVENTORY and EXCLUSION_LIST,
    while preserving PRODUCTS, ALIASES, and LOCATIONS.
    """
    manage_conn = False
    active_conn = None
    if isinstance(conn, sqlite3.Connection):
        active_conn = conn
    elif conn is not None and not isinstance(conn, (str, bytes)):
        active_conn = conn
    else:
        target_path = conn if isinstance(conn, str) else db_path
        active_conn = get_connection(target_path)
        manage_conn = True

    try:
        cur = active_conn.cursor()
        cur.execute("DELETE FROM INVENTORY")
        cur.execute("DELETE FROM EXCLUSION_LIST")
        active_conn.commit()
        return True
    except Exception:
        if manage_conn:
            active_conn.rollback()
        raise
    finally:
        if manage_conn:
            active_conn.close()


def get_receipt_logs(conn=None, db_path=None) -> list[tuple[str, str, str]]:
    """
    Returns list of receipt log entries: (time, day, file_name) ordered by id DESC.
    """
    manage_conn = False
    active_conn = None
    if isinstance(conn, sqlite3.Connection):
        active_conn = conn
    elif conn is not None and not isinstance(conn, (str, bytes)):
        active_conn = conn
    else:
        target_path = conn if isinstance(conn, str) else db_path
        active_conn = get_connection(target_path)
        manage_conn = True

    try:
        cur = active_conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='RECEIPT_LOGS'")
        if cur.fetchone() is None:
            return []
        cur.execute("SELECT time, day, file_name FROM RECEIPT_LOGS ORDER BY id DESC")
        return cur.fetchall()
    finally:
        if manage_conn:
            active_conn.close()


def log_receipt(time_str: str, day_str: str, file_name: str, conn=None, db_path=None) -> int:
    """
    Logs an imported receipt into RECEIPT_LOGS.
    Returns the inserted row id.
    """
    manage_conn = False
    active_conn = None
    if isinstance(conn, sqlite3.Connection):
        active_conn = conn
    elif conn is not None and not isinstance(conn, (str, bytes)):
        active_conn = conn
    else:
        target_path = conn if isinstance(conn, str) else db_path
        active_conn = get_connection(target_path)
        manage_conn = True

    try:
        cur = active_conn.cursor()
        cur.execute(
            "CREATE TABLE IF NOT EXISTS RECEIPT_LOGS ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT NOT NULL, day TEXT NOT NULL, file_name TEXT NOT NULL)"
        )
        cur.execute(
            "INSERT INTO RECEIPT_LOGS (time, day, file_name) VALUES (?, ?, ?)",
            (time_str, day_str, file_name)
        )
        row_id = cur.lastrowid
        active_conn.commit()
        return row_id
    except Exception:
        if manage_conn:
            active_conn.rollback()
        raise
    finally:
        if manage_conn:
            active_conn.close()


add_receipt_log = log_receipt

