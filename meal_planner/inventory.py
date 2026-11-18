"""Pantry inventory management, FIFO consumption, unit normalization, and product catalog helpers."""

import contextlib
from datetime import date as _date_cls
import math
import re
import sqlite3

from meal_planner.db.connection import DB_PATH, get_connection

_INITIAL_DB_PATH = DB_PATH


class AliasConflict(Exception):
    """The name entered already exists in ALIASES but is linked to a different ID."""


class InvalidID(Exception):
    """The ID provided is not in the expected format (integer)."""


class ProductNotFound(Exception):
    """Name not in ALIASES and no fallback ID provided, or product/row/location not found."""


class InvalidInput(Exception):
    """Input values that break the business rules."""


try:
    from meal_planner.engine._speedups import round_down_to_hundredth as _round_down_to_hundredth
except (ImportError, AttributeError):
    def _round_down_to_hundredth(value: float) -> float:
        """Rounds down to the hundredth (0.01), e.g. 1.1111 -> 1.11."""
        return math.floor(round(value * 100, 6)) / 100


_UNSET = object()
_REQ_MACROS = ("Kcal_100g", "Protein_100g", "Carbs_100g", "Fat_100g")
_OPT_MACROS = ("Sugars_100g", "Saturated_fat_100g", "Fiber_100g", "Salt_100g")
_ALL_MACROS = _REQ_MACROS + _OPT_MACROS

_UNIT_SPEC = {
    "g": (0.001, "kg"), "gr": (0.001, "kg"), "kg": (1.0, "kg"), "kgs": (1.0, "kg"),
    "ml": (0.001, "L"), "cl": (0.01, "L"), "l": (1.0, "L"), "lt": (1.0, "L"),
    "pz": (1.0, "pcs"), "pcs": (1.0, "pcs"), "pc": (1.0, "pcs"),
}

_MACRO_KEY_MAP = {
    **{k.lower(): k for k in _ALL_MACROS},
    **{k.removesuffix("_100g").lower(): k for k in _ALL_MACROS},
    "prot": "Protein_100g",
    "carb": "Carbs_100g",
}


def _split_conn_arg(conn_or_arg, arg, conn):
    if isinstance(conn_or_arg, sqlite3.Connection) or hasattr(conn_or_arg, "cursor"):
        return conn_or_arg, arg
    return conn, (conn_or_arg if arg is None else arg)


@contextlib.contextmanager
def _db_ctx(db_path=None, conn=None, commit: bool = False, always_commit: bool = False):
    manage = False
    if isinstance(conn, sqlite3.Connection) or (conn is not None and not isinstance(conn, (str, bytes))):
        active = conn
    else:
        path = conn if isinstance(conn, str) else db_path
        if path is None and DB_PATH != _INITIAL_DB_PATH:
            path = DB_PATH
        active = get_connection(path)
        manage = True
    try:
        yield active
        if always_commit or (commit and manage):
            active.commit()
    except Exception:
        if manage:
            active.rollback()
        raise
    finally:
        if manage:
            active.close()


def _check_num(
    val,
    name: str,
    *,
    positive: bool = False,
    non_neg: bool = False,
    is_int: bool = False,
    allow_str: bool = False,
    allow_comma: bool = False,
    err_cls: type = InvalidInput,
):
    if val is None or isinstance(val, bool):
        raise err_cls(f"{name} must be a valid number, got {val!r}.")
    if is_int:
        if allow_str:
            if isinstance(val, float):
                raise err_cls(f"{name} must be an integer, got {val!r}.")
            try:
                num = int(val)
            except (ValueError, TypeError) as e:
                raise err_cls(f"{name} must be an integer, got {val!r}.") from e
        else:
            if not isinstance(val, int):
                raise err_cls(f"{name} must be an integer, got {val!r}.")
            num = val
    else:
        if not allow_str and not isinstance(val, (int, float)):
            raise err_cls(f"{name} must be numeric, got {val!r}.")
        try:
            s = str(val).replace(",", ".") if (allow_comma and isinstance(val, str)) else val
            num = float(s)
        except (ValueError, TypeError) as e:
            raise err_cls(f"{name} must be numeric, got {val!r}.") from e
        if not math.isfinite(num):
            raise err_cls(f"{name} must be finite, got {val!r}.")
    if positive and num <= 0:
        raise err_cls(f"{name} must be positive, got {val!r}.")
    if non_neg and num < 0:
        raise err_cls(f"{name} must be non-negative, got {val!r}.")
    return num


def _resolve_id_from_name(cur: sqlite3.Cursor, name: str):
    row = cur.execute("SELECT ID FROM ALIASES WHERE Name = ?", (name,)).fetchone()
    if row:
        return row[0]
    row = cur.execute("SELECT ID FROM ALIASES WHERE Name = ? COLLATE NOCASE", (name,)).fetchone()
    return row[0] if row else None


def _id_exists_in_products(cur: sqlite3.Cursor, product_id: int) -> bool:
    return cur.execute("SELECT 1 FROM PRODUCTS WHERE ID = ?", (product_id,)).fetchone() is not None


def _first_name_per_id(cur: sqlite3.Cursor, product_id: int) -> str:
    row = cur.execute("SELECT Name FROM ALIASES WHERE ID = ? ORDER BY rowid ASC LIMIT 1", (product_id,)).fetchone()
    return row[0] if row else None


def _normalize_macro_dict(macros: dict) -> dict:
    """Normalizes variations of macro dictionary keys to standard schema column names."""
    if not macros:
        return {}
    return {_MACRO_KEY_MAP.get(str(k).lower(), k): v for k, v in macros.items()}


def _write_product(
    cur: sqlite3.Cursor,
    product_id,
    unit: str,
    macros: dict,
    grams_per_piece,
    density_kg_per_l,
    update_existing: bool = False,
) -> int:
    vals = (
        unit, macros["Kcal_100g"], macros["Protein_100g"], macros["Carbs_100g"], macros["Fat_100g"],
        macros.get("Sugars_100g"), macros.get("Saturated_fat_100g"), macros.get("Fiber_100g"), macros.get("Salt_100g"),
        grams_per_piece, density_kg_per_l,
    )
    if update_existing:
        cur.execute(
            "UPDATE PRODUCTS SET Unit=?, Kcal_100g=?, Protein_100g=?, Carbs_100g=?, Fat_100g=?, "
            "Sugars_100g=?, Saturated_fat_100g=?, Fiber_100g=?, Salt_100g=?, "
            "Grams_per_piece=?, Density_kg_per_L=? WHERE ID=?",
            (*vals, product_id),
        )
        return product_id
    if product_id is not None:
        cur.execute(
            "INSERT INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g, "
            "Sugars_100g, Saturated_fat_100g, Fiber_100g, Salt_100g, Grams_per_piece, Density_kg_per_L) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (product_id, *vals),
        )
        return product_id
    cur.execute(
        "INSERT INTO PRODUCTS (Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g, "
        "Sugars_100g, Saturated_fat_100g, Fiber_100g, Salt_100g, Grams_per_piece, Density_kg_per_L) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        vals,
    )
    return cur.lastrowid


def _validate_and_save_product(
    cur: sqlite3.Cursor,
    *,
    unit: str,
    macros: dict,
    product_id: int = None,
    grams_per_piece=None,
    density_kg_per_l=None,
    base_macros: dict = None,
    partial: bool = False,
    normalize_keys: bool = False,
    allow_str_macros: bool = False,
    allow_comma: bool = False,
    allow_str_factors: bool = False,
    default_liquid_density: bool = False,
    write_db: bool = True,
    update_existing: bool = False,
    err_cls: type = InvalidInput,
) -> tuple[int | None, dict, bool]:
    if unit not in ("kg", "L", "pcs"):
        raise err_cls("Unit must be 'kg', 'L' or 'pcs'.")

    if not partial:
        if not isinstance(macros, dict):
            raise err_cls("macros must be a dictionary.")
        if not macros and err_cls is InvalidInput:
            raise err_cls("new_product_macros is required for a new product (Case B2).")
        src = _normalize_macro_dict(macros) if normalize_keys else dict(macros)
        missing = [k for k in _REQ_MACROS if k not in src or src[k] is None]
        if missing:
            raise err_cls(f"Missing required macro fields: {', '.join(missing)}")
        clean = dict(src)
        for k in _ALL_MACROS:
            if clean.get(k) is not None:
                clean[k] = _check_num(
                    clean[k], k, non_neg=True, allow_str=allow_str_macros, allow_comma=allow_comma, err_cls=err_cls
                )
        macros_changed = True
    else:
        src = _normalize_macro_dict(macros) if (normalize_keys and macros) else dict(macros or {})
        clean = dict(base_macros or {})
        macros_changed = False
        for k in _ALL_MACROS:
            if k in src:
                raw_val = src[k]
                if raw_val is not None and str(raw_val).strip():
                    num_val = _check_num(
                        raw_val, k, non_neg=True, allow_str=allow_str_macros, allow_comma=allow_comma, err_cls=err_cls
                    )
                    clean[k] = num_val
                    old_val = (base_macros or {}).get(k)
                    if old_val is None or abs(old_val - num_val) > 1e-5:
                        macros_changed = True
                else:
                    if k in _REQ_MACROS:
                        raise err_cls(f"{k} is required and cannot be empty.")
                    clean[k] = None
                    if (base_macros or {}).get(k) is not None:
                        macros_changed = True

    if default_liquid_density and unit == "L" and density_kg_per_l is None:
        density_kg_per_l = 1.0
    if grams_per_piece is not None:
        grams_per_piece = _check_num(
            grams_per_piece, "grams_per_piece", positive=True, allow_str=allow_str_factors, err_cls=err_cls
        )
    if density_kg_per_l is not None:
        density_kg_per_l = _check_num(
            density_kg_per_l, "density_kg_per_l", positive=True, allow_str=allow_str_factors, err_cls=err_cls
        )

    if write_db:
        product_id = _write_product(
            cur, product_id, unit, clean, grams_per_piece, density_kg_per_l, update_existing=update_existing
        )
    return product_id, clean, macros_changed


def convert_receipt_quantity_unit(
    quantity: float,
    raw_unit: str,
    target_unit: str = None,
    grams_per_piece: float = None,
    density_kg_per_l: float = None,
) -> tuple[float, str]:
    """Normalizes a receipt item quantity and raw unit into ('kg', 'L', 'pcs')."""
    if quantity is None or isinstance(quantity, bool):
        raise InvalidInput(f"Quantity must be a valid numeric value, got {quantity!r}.")
    try:
        qty = float(quantity)
    except (ValueError, TypeError) as e:
        raise InvalidInput(f"Invalid quantity {quantity!r}: must be a numeric value.") from e
    if not math.isfinite(qty) or qty <= 0:
        raise InvalidInput(f"Quantity must be a positive finite number, got {quantity!r}.")
    if not raw_unit or not isinstance(raw_unit, str):
        raise InvalidInput("raw_unit must be a non-empty string.")

    spec = _UNIT_SPEC.get(raw_unit.strip().lower().rstrip("."))
    if not spec:
        raise InvalidInput(f"Unsupported unit: {raw_unit!r}.")
    factor, canonical = spec
    norm_qty = float(round(qty)) if canonical == "pcs" else round(qty * factor, 4)
    if norm_qty <= 0:
        raise InvalidInput(f"Quantity {qty} {raw_unit} rounds to <= 0.")

    if target_unit is not None:
        if not isinstance(target_unit, str) or not target_unit.strip():
            raise InvalidInput("target_unit must be a non-empty string.")
        tgt_spec = _UNIT_SPEC.get(target_unit.strip().lower().rstrip("."))
        if not tgt_spec:
            raise InvalidInput(f"Unsupported or invalid target_unit: {target_unit!r}.")
        tgt_canonical = tgt_spec[1]
        if tgt_canonical != canonical:
            def _factor(val, name: str) -> float:
                if val is None or isinstance(val, bool):
                    raise InvalidInput(f"Missing or invalid conversion factor {name} for {canonical} -> {tgt_canonical}.")
                try:
                    fv = float(val)
                except (ValueError, TypeError) as e:
                    raise InvalidInput(f"Invalid {name} {val!r}: must be numeric.") from e
                if not math.isfinite(fv) or fv <= 0:
                    raise InvalidInput(f"{name} must be a positive finite number, got {val!r}.")
                return fv

            if canonical == "kg":
                mass_kg = norm_qty
            elif canonical == "pcs":
                mass_kg = (norm_qty * _factor(grams_per_piece, "grams_per_piece")) / 1000.0
            else:
                mass_kg = norm_qty * _factor(density_kg_per_l, "density_kg_per_l")

            if tgt_canonical == "kg":
                norm_qty = round(mass_kg, 4)
            elif tgt_canonical == "L":
                norm_qty = round(mass_kg / _factor(density_kg_per_l, "density_kg_per_l"), 4)
            else:
                norm_qty = float(round((mass_kg * 1000.0) / _factor(grams_per_piece, "grams_per_piece")))

            if norm_qty <= 0:
                raise InvalidInput(f"Quantity {qty} {raw_unit} rounds to <= 0 {tgt_canonical}.")
            canonical = tgt_canonical

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
    """Transforms a reconciled receipt item into the kwargs dict expected by add_item()."""
    if not name or not str(name).strip():
        raise InvalidInput("Item name cannot be empty.")
    name_clean = str(name).strip()

    norm_qty, canonical_unit = convert_receipt_quantity_unit(
        raw_quantity, raw_unit, target_unit=target_unit,
        grams_per_piece=grams_per_piece, density_kg_per_l=density_kg_per_l,
    )

    try:
        if isinstance(packages, bool):
            pkg_count = 1
        else:
            p_val = float(str(packages if packages is not None else 1))
            if not math.isfinite(p_val) or p_val > 2**63 - 1 or p_val < 0:
                raise OverflowError
            pkg_count = max(1, int(p_val))
    except (ValueError, TypeError, OverflowError):
        pkg_count = 1

    if canonical_unit == "pcs":
        unit_weight = 1.0
        try:
            total_pcs = norm_qty * pkg_count
            if not math.isfinite(total_pcs) or total_pcs > 2**63 - 1 or total_pcs < 0:
                raise OverflowError
            intact_items = max(1, int(round(total_pcs)))
        except (ValueError, TypeError, OverflowError):
            intact_items = 1
    else:
        unit_weight, intact_items = norm_qty, pkg_count

    args = {
        "name": name_clean,
        "unit_weight": unit_weight,
        "intact_items": intact_items,
        "location": str(location).strip() if (location and str(location).strip()) else "pantry",
        "expiration_date": expiration_date.strip() if (expiration_date and str(expiration_date).strip()) else None,
        "fallback_id": fallback_id,
    }
    if fallback_id is not None:
        args["new_product_unit"] = canonical_unit
        if new_product_macros:
            args["new_product_macros"] = new_product_macros
    return args


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
    """Adds a new instance of a product to the inventory atomically."""
    unit_weight = float(_check_num(unit_weight, "unit_weight", positive=True))
    intact_items = _check_num(intact_items, "intact_items", positive=True, is_int=True)
    if not location:
        raise InvalidInput("Location is required.")
    if expiration_date is not None and not re.match(r"^\d{4}-\d{2}-\d{2}$", expiration_date):
        raise InvalidInput("expiration_date must be in YYYY-MM-DD format (e.g. 2026-12-31).")

    try:
        with _db_ctx(db_path=db_path, conn=conn, commit=True) as active_conn:
            cur = active_conn.cursor()
            product_id = _resolve_id_from_name(cur, name)
            if product_id is not None:
                case = "A"
            else:
                if fallback_id is None:
                    raise ProductNotFound(
                        f"Name '{name}' not found in ALIASES. Provide fallback_id to proceed (Case B)."
                    )
                if isinstance(fallback_id, bool) or not isinstance(fallback_id, int):
                    raise InvalidID("fallback_id must be an integer.")
                product_id = fallback_id
                if _id_exists_in_products(cur, fallback_id):
                    case = "B1"
                else:
                    case = "B2"
                    _validate_and_save_product(
                        cur, unit=new_product_unit, macros=new_product_macros,
                        product_id=product_id, allow_str_macros=False, allow_str_factors=False,
                    )
                cur.execute("INSERT INTO ALIASES (Name, ID) VALUES (?, ?)", (name, product_id))

            cur.execute(
                "INSERT INTO INVENTORY (ID, Unit_Weight, Intact_Items, Open_Remainder, Location, Expiration_Date) "
                "VALUES (?, ?, ?, 0, ?, ?)",
                (product_id, unit_weight, intact_items, location, expiration_date),
            )
            return {"outcome": "ok", "case": case, "ID": product_id, "ID_row_inventory": cur.lastrowid}
    except sqlite3.IntegrityError as e:
        err_msg = str(e).lower()
        if "not null" in err_msg:
            raise InvalidInput(f"Database NOT NULL constraint violation: {e}") from e
        if "unique" in err_msg or "primary key" in err_msg:
            raise AliasConflict(f"Integrity conflict during insertion: {e}") from e
        raise InvalidInput(f"Database integrity constraint violation: {e}") from e


def consume_quantity(name: str, quantity_to_consume: float, db_path: str = None) -> dict:
    """Consumes a quantity of a product from the inventory (open remainders first, then FIFO)."""
    _check_num(quantity_to_consume, "quantity_to_consume", positive=True)

    with _db_ctx(db_path=db_path, commit=True) as conn:
        cur = conn.cursor()
        product_id = _resolve_id_from_name(cur, name)
        if product_id is None:
            raise ProductNotFound(f"Name '{name}' not found in ALIASES.")

        row = cur.execute("SELECT Unit FROM PRODUCTS WHERE ID = ?", (product_id,)).fetchone()
        if row is None:
            raise ProductNotFound(f"No PRODUCTS record for ID {product_id}.")
        unit_of_measure = row[0]

        if unit_of_measure == "pcs":
            if quantity_to_consume != int(quantity_to_consume):
                raise InvalidInput("Indivisible product (unit 'pcs'): the requested quantity must be a whole number of items.")
            quantity_to_consume = int(quantity_to_consume)

        rows = cur.execute(
            "SELECT Row_ID, Unit_Weight, Intact_Items, Open_Remainder FROM INVENTORY WHERE ID = ? ORDER BY Row_ID ASC",
            (product_id,),
        ).fetchall()

        total_availability = sum(r[2] if unit_of_measure == "pcs" else (r[2] * r[1]) + r[3] for r in rows)
        missing_quantity = 0.0
        quantity_to_cover = quantity_to_consume

        if total_availability < quantity_to_consume:
            missing_quantity = (
                quantity_to_consume - total_availability if unit_of_measure == "pcs"
                else _round_down_to_hundredth(quantity_to_consume - total_availability)
            )
            quantity_to_cover = total_availability

        remaining = quantity_to_cover
        if unit_of_measure == "pcs":
            for row_id, _uw, item_count, _rem in rows:
                if remaining <= 0:
                    break
                withdrawal = min(item_count, remaining)
                if withdrawal > 0:
                    cur.execute("UPDATE INVENTORY SET Intact_Items = ? WHERE Row_ID = ?", (item_count - withdrawal, row_id))
                    remaining -= withdrawal
        else:
            for idx, (row_id, unit_weight, item_count, remainder_weight) in enumerate(rows):
                if remaining <= 0:
                    break
                if remainder_weight > 0:
                    withdrawal = min(remainder_weight, remaining)
                    new_rem = _round_down_to_hundredth(remainder_weight - withdrawal)
                    cur.execute("UPDATE INVENTORY SET Open_Remainder = ? WHERE Row_ID = ?", (new_rem, row_id))
                    remaining -= withdrawal
                    rows[idx] = (row_id, unit_weight, item_count, new_rem)

            for row_id, unit_weight, item_count, _rem in rows:
                if remaining <= 0 or item_count <= 0:
                    continue
                max_weight = item_count * unit_weight
                if remaining >= max_weight:
                    remaining = _round_down_to_hundredth(remaining - max_weight)
                    item_count, new_row_rem = 0, 0.0
                else:
                    full_packs = int(remaining // unit_weight)
                    rem = _round_down_to_hundredth(remaining - (full_packs * unit_weight))
                    if rem > 0:
                        item_count -= full_packs + 1
                        new_row_rem = _round_down_to_hundredth(unit_weight - rem)
                    else:
                        item_count -= full_packs
                        new_row_rem = 0.0
                    remaining = 0.0
                cur.execute("UPDATE INVENTORY SET Intact_Items = ?, Open_Remainder = ? WHERE Row_ID = ?",
                            (item_count, new_row_rem, row_id))
                if remaining <= 0:
                    break

        cur.execute("DELETE FROM INVENTORY WHERE ID = ? AND Intact_Items = 0 AND Open_Remainder = 0", (product_id,))

        if missing_quantity > 0:
            existing = cur.execute("SELECT Missing_Quantity FROM SHOPPING_LIST WHERE ID = ?", (product_id,)).fetchone()
            if existing is not None:
                new_total = existing[0] + missing_quantity
                if unit_of_measure != "pcs":
                    new_total = _round_down_to_hundredth(new_total)
                cur.execute("UPDATE SHOPPING_LIST SET Missing_Quantity = ? WHERE ID = ?", (new_total, product_id))
            else:
                cur.execute(
                    "INSERT INTO SHOPPING_LIST (ID, Reference_Name, Missing_Quantity) VALUES (?, ?, ?)",
                    (product_id, _first_name_per_id(cur, product_id), missing_quantity),
                )

        return {
            "outcome": "ok",
            "ID": product_id,
            "requested": quantity_to_consume,
            "consumed": quantity_to_cover,
            "shortfall": missing_quantity,
            "insufficient_availability": missing_quantity > 0,
        }


def get_shopping_list(db_path: str = None) -> list:
    """Returns the current content of SHOPPING_LIST."""
    with _db_ctx(db_path=db_path) as conn:
        rows = conn.execute("SELECT ID, Reference_Name, Missing_Quantity FROM SHOPPING_LIST ORDER BY ID").fetchall()
        return [{"ID": pid, "Name": name, "Missing_Quantity": qty} for pid, name, qty in rows]


def get_available_inventory(db_path: str = None) -> list:
    """Aggregates INVENTORY by (ID, Location) and returns total availability per pair."""
    with _db_ctx(db_path=db_path) as conn:
        rows = conn.execute(
            """
            SELECT INVENTORY.ID, INVENTORY.Location,
                   SUM(CASE WHEN PRODUCTS.Unit = 'pcs' THEN INVENTORY.Intact_Items
                            ELSE INVENTORY.Intact_Items * INVENTORY.Unit_Weight + INVENTORY.Open_Remainder END) AS Totale,
                   PRODUCTS.Unit, MIN(INVENTORY.Expiration_Date) AS Nearest_Expiration,
                   (SELECT a.Name FROM ALIASES a WHERE a.ID = INVENTORY.ID ORDER BY a.rowid ASC LIMIT 1) AS Ref_Name
            FROM INVENTORY JOIN PRODUCTS ON PRODUCTS.ID = INVENTORY.ID
            GROUP BY INVENTORY.ID, INVENTORY.Location ORDER BY INVENTORY.ID, INVENTORY.Location
            """
        ).fetchall()
        return [
            {
                "ID": pid, "Name": ref_name,
                "Total_Available_Weight": total if unit == "pcs" else _round_down_to_hundredth(total),
                "Unit": unit, "Location": loc, "Expiration_Date": nearest_exp,
            }
            for pid, loc, total, unit, nearest_exp, ref_name in rows
        ]


def get_locations(db_path: str = None, conn: sqlite3.Connection = None) -> list:
    """Returns the sorted list of location names from the LOCATIONS catalog."""
    with _db_ctx(db_path=db_path, conn=conn) as active_conn:
        return [row[0] for row in active_conn.execute("SELECT Name FROM LOCATIONS ORDER BY Name").fetchall()]


def add_location(name: str, db_path: str = None) -> None:
    """Adds a new location to the LOCATIONS catalog."""
    if not name or not name.strip():
        raise InvalidInput("Location name cannot be empty.")
    name = name.strip()
    try:
        with _db_ctx(db_path=db_path, commit=True) as conn:
            conn.execute("INSERT INTO LOCATIONS (Name) VALUES (?)", (name,))
    except sqlite3.IntegrityError:
        raise InvalidInput(f"Location '{name}' already exists.")


def rename_location(old_name: str, new_name: str, db_path: str = None) -> None:
    """Renames a location in LOCATIONS and updates all INVENTORY rows referencing it."""
    if not new_name or not new_name.strip():
        raise InvalidInput("New location name cannot be empty.")
    new_name = new_name.strip()
    try:
        with _db_ctx(db_path=db_path, commit=True) as conn:
            if conn.execute("SELECT 1 FROM LOCATIONS WHERE Name = ?", (old_name,)).fetchone() is None:
                raise ProductNotFound(f"Location '{old_name}' does not exist.")
            conn.execute("INSERT INTO LOCATIONS (Name) VALUES (?)", (new_name,))
            conn.execute("UPDATE INVENTORY SET Location = ? WHERE Location = ?", (new_name, old_name))
            conn.execute("DELETE FROM LOCATIONS WHERE Name = ?", (old_name,))
    except sqlite3.IntegrityError:
        raise InvalidInput(f"Location '{new_name}' already exists.")


def delete_location(name: str, db_path: str = None) -> None:
    """Deletes a location from the LOCATIONS catalog if unused."""
    with _db_ctx(db_path=db_path, commit=True) as conn:
        if conn.execute("SELECT 1 FROM LOCATIONS WHERE Name = ?", (name,)).fetchone() is None:
            raise ProductNotFound(f"Location '{name}' does not exist.")
        count = conn.execute("SELECT COUNT(*) FROM INVENTORY WHERE Location = ?", (name,)).fetchone()[0]
        if count > 0:
            raise InvalidInput(
                f"Cannot delete '{name}': {count} inventory row(s) still use this location. Consume or move them first."
            )
        conn.execute("DELETE FROM LOCATIONS WHERE Name = ?", (name,))


def get_exclusion_list(target_date: str = None, db_path: str = None, conn: sqlite3.Connection = None) -> list:
    """Returns and persists expired/expiring inventory items up to target_date."""
    ref = target_date or _date_cls.today().isoformat()
    with _db_ctx(db_path=db_path, conn=conn, always_commit=True) as active_conn:
        rows = active_conn.execute(
            """
            SELECT DISTINCT i.ID,
                   (SELECT a.Name FROM ALIASES a WHERE a.ID = i.ID ORDER BY a.rowid ASC LIMIT 1) AS Ref_Name,
                   MIN(i.Expiration_Date) AS Nearest_Exp, i.Location
            FROM INVENTORY i
            WHERE i.Expiration_Date IS NOT NULL AND i.Expiration_Date <= ?
            GROUP BY i.ID, i.Location
            """,
            (ref,),
        ).fetchall()
        result = [{"ID": r[0], "Name": r[1], "Expiration_Date": r[2], "Location": r[3]} for r in rows]
        active_conn.execute("DELETE FROM EXCLUSION_LIST")
        active_conn.executemany(
            "INSERT OR IGNORE INTO EXCLUSION_LIST (ID, Ref_Name, Expiration_Date, Location) VALUES (?,?,?,?)",
            [(r["ID"], r["Name"], r["Expiration_Date"], r["Location"]) for r in result],
        )
        return result


def get_catalog_for_reconciliation(db_path: str = None, conn: sqlite3.Connection = None) -> dict:
    """Queries PRODUCTS and ALIASES to build an in-memory catalog for receipt reconciliation."""
    with _db_ctx(db_path=db_path, conn=conn) as active_conn:
        canonical_names = {}
        for pid, name in active_conn.execute("SELECT ID, Name FROM ALIASES ORDER BY rowid ASC"):
            canonical_names.setdefault(pid, name)

        prod_rows = active_conn.execute(
            "SELECT ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g, "
            "Sugars_100g, Saturated_fat_100g, Fiber_100g, Salt_100g, Grams_per_piece, Density_kg_per_L "
            "FROM PRODUCTS ORDER BY ID ASC"
        ).fetchall()

        products_by_id, products_list = {}, []
        for r in prod_rows:
            pid = r[0]
            name = canonical_names.get(pid, f"Product #{pid}")
            p_dict = {
                "id": pid, "name": name, "canonical_name": name, "unit": r[1],
                "kcal": r[2], "protein": r[3], "carbs": r[4], "fat": r[5],
                "grams_per_piece": r[10], "density_kg_per_l": r[11],
                "macros": dict(zip(_ALL_MACROS, r[2:10])),
            }
            products_by_id[pid] = p_dict
            products_list.append(p_dict)

        products_list.sort(key=lambda p: p["name"].lower())
        alias_map = {
            alias_name.strip().lower(): products_by_id[pid]
            for alias_name, pid in active_conn.execute("SELECT Name, ID FROM ALIASES")
            if pid in products_by_id
        }
        return {"products": products_list, "alias_map": alias_map}


def get_next_product_id(db_path: str = None, conn: sqlite3.Connection = None) -> int:
    """Returns the next available integer ID for PRODUCTS."""
    with _db_ctx(db_path=db_path, conn=conn) as active_conn:
        return active_conn.execute("SELECT COALESCE(MAX(ID), 0) + 1 FROM PRODUCTS").fetchone()[0]


def _row_to_inventory_dict(r: tuple) -> dict:
    unit, u_weight, intact, open_rem = r[8], r[3], r[4], r[5]
    tot = intact if unit == "pcs" else _round_down_to_hundredth((intact * u_weight) + open_rem)
    macros = dict(zip(_ALL_MACROS, r[9:17]))
    return {
        "Row_ID": r[0], "ID": r[1], "Name": r[2] or f"Product #{r[1]}",
        "Unit_Weight": u_weight, "Intact_Items": intact, "Open_Remainder": open_rem,
        "Location": r[6], "Expiration_Date": r[7], "Unit": unit, "Total_Available_Weight": tot,
        **macros, "Grams_per_piece": r[17], "Density_kg_per_L": r[18], "macros": macros,
    }


def get_inventory_rows(db_path: str = None, conn: sqlite3.Connection = None, row_id=_UNSET) -> list:
    """Returns all (or a single matching) inventory rows joined with PRODUCTS and canonical ALIASES Name."""
    where = "" if row_id is _UNSET else "WHERE i.Row_ID = ?"
    params = () if row_id is _UNSET else (row_id,)
    with _db_ctx(db_path=db_path, conn=conn) as active_conn:
        rows = active_conn.execute(
            f"""
            SELECT i.Row_ID, i.ID,
                   (SELECT a.Name FROM ALIASES a WHERE a.ID = i.ID ORDER BY a.rowid ASC LIMIT 1) AS Name,
                   i.Unit_Weight, i.Intact_Items, i.Open_Remainder, i.Location, i.Expiration_Date,
                   p.Unit, p.Kcal_100g, p.Protein_100g, p.Carbs_100g, p.Fat_100g,
                   p.Sugars_100g, p.Saturated_fat_100g, p.Fiber_100g, p.Salt_100g,
                   p.Grams_per_piece, p.Density_kg_per_L
            FROM INVENTORY i JOIN PRODUCTS p ON p.ID = i.ID {where} ORDER BY i.Row_ID ASC
            """,
            params,
        ).fetchall()
    return [_row_to_inventory_dict(r) for r in rows]


def get_inventory_item(row_id: int, db_path: str = None, conn: sqlite3.Connection = None) -> dict | None:
    """Returns a single inventory item dict by Row_ID, or None if not found."""
    if row_id is None:
        return None
    rows = get_inventory_rows(db_path=db_path, conn=conn, row_id=row_id)
    return rows[0] if rows else None


def delete_inventory_item(conn_or_row_id=None, row_id: int = None, db_path: str = None, conn: sqlite3.Connection = None) -> bool:
    """Deletes a single item row from INVENTORY by Row_ID."""
    active_arg_conn, target_row_id = _split_conn_arg(conn_or_row_id, row_id, conn)
    target_row_id = _check_num(target_row_id, "row_id", positive=True, is_int=True, allow_str=True)
    with _db_ctx(db_path=db_path, conn=active_arg_conn, commit=True) as active_conn:
        cur = active_conn.cursor()
        cur.execute("DELETE FROM INVENTORY WHERE Row_ID = ?", (target_row_id,))
        return cur.rowcount > 0


def is_product_macro_linked(conn_or_pid=None, product_id: int = None, db_path: str = None, conn: sqlite3.Connection = None) -> bool:
    """Returns True if product_id is referenced across multiple aliases/rows or recipes/groups."""
    active_arg_conn, pid = _split_conn_arg(conn_or_pid, product_id, conn)
    pid = _check_num(pid, "product_id", is_int=True, allow_str=True)
    with _db_ctx(db_path=db_path, conn=active_arg_conn) as active_conn:
        return any(
            active_conn.execute(f"SELECT COUNT(*) FROM {tbl} WHERE ID = ?", (pid,)).fetchone()[0] > thresh
            for tbl, thresh in (("ALIASES", 1), ("INVENTORY", 1), ("RECIPE_INGREDIENTS", 0), ("GROUP_MEMBERS", 0))
        )


def update_inventory_item(
    conn_or_row_id=None,
    row_id: int = None,
    item_fields: dict = None,
    macro_fields: dict = None,
    propagate: bool = False,
    db_path: str = None,
    conn: sqlite3.Connection = None,
) -> dict:
    """Updates an inventory row's characteristics and/or macronutrients (with optional cloning/propagation)."""
    active_arg_conn, target_row_id = _split_conn_arg(conn_or_row_id, row_id, conn)
    target_row_id = _check_num(target_row_id, "row_id", positive=True, is_int=True, allow_str=True)

    item_fields = dict(item_fields or {})
    macro_fields = _normalize_macro_dict(macro_fields or {})

    with _db_ctx(db_path=db_path, conn=active_arg_conn, commit=True) as active_conn:
        cur = active_conn.cursor()
        current = cur.execute(
            "SELECT i.ID, i.Unit_Weight, i.Intact_Items, i.Open_Remainder, i.Location, i.Expiration_Date, "
            "p.Unit, p.Kcal_100g, p.Protein_100g, p.Carbs_100g, p.Fat_100g, "
            "p.Sugars_100g, p.Saturated_fat_100g, p.Fiber_100g, p.Salt_100g, "
            "p.Grams_per_piece, p.Density_kg_per_L "
            "FROM INVENTORY i JOIN PRODUCTS p ON p.ID = i.ID WHERE i.Row_ID = ?",
            (target_row_id,),
        ).fetchone()
        if not current:
            raise ProductNotFound(f"Inventory row with Row_ID {target_row_id} not found.")

        orig_pid, orig_u_weight, orig_intact, orig_rem, orig_loc, orig_exp, orig_unit = current[:7]
        orig_macros = dict(zip(_ALL_MACROS, current[7:15]))
        orig_piece, orig_density = current[15], current[16]

        final_u_weight = (
            _check_num(item_fields["Unit_Weight"], "Unit_Weight", positive=True, allow_str=True)
            if item_fields.get("Unit_Weight") is not None else orig_u_weight
        )
        final_intact = (
            _check_num(item_fields["Intact_Items"], "Intact_Items", non_neg=True, is_int=True, allow_str=True)
            if item_fields.get("Intact_Items") is not None else orig_intact
        )
        final_rem = (
            _check_num(item_fields["Open_Remainder"], "Open_Remainder", non_neg=True, allow_str=True)
            if item_fields.get("Open_Remainder") is not None else orig_rem
        )
        if final_intact == 0 and final_rem <= 0:
            raise InvalidInput("Item quantity cannot be 0. To remove this item, use the delete button.")

        if "Location" in item_fields:
            final_loc = str(item_fields["Location"]).strip()
            if not final_loc:
                raise InvalidInput("Location cannot be empty.")
        else:
            final_loc = orig_loc

        if "Expiration_Date" in item_fields:
            exp_val = item_fields["Expiration_Date"]
            if exp_val is not None and str(exp_val).strip():
                exp_str = str(exp_val).strip()
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

        if "Unit" in item_fields and item_fields["Unit"]:
            final_unit = str(item_fields["Unit"]).strip()
            if final_unit not in ("kg", "L", "pcs"):
                raise InvalidInput("Unit must be 'kg', 'L', or 'pcs'.")
        else:
            final_unit = orig_unit

        def _opt_pos(key: str, orig):
            if key not in item_fields:
                return orig
            v = item_fields[key]
            return None if (v is None or not str(v).strip()) else _check_num(v, key, positive=True, allow_str=True)

        final_piece = _opt_pos("Grams_per_piece", orig_piece)
        final_density = _opt_pos("Density_kg_per_L", orig_density)

        _, final_macros, macros_changed = _validate_and_save_product(
            cur, unit=final_unit, macros=macro_fields, grams_per_piece=final_piece, density_kg_per_l=final_density,
            base_macros=orig_macros, partial=True, allow_str_macros=True, allow_str_factors=True, write_db=False,
        )
        if final_unit != orig_unit:
            macros_changed = True

        is_linked = is_product_macro_linked(active_conn, orig_pid)
        final_pid, created_new_product = orig_pid, False

        if macros_changed and is_linked and not propagate:
            new_pid = get_next_product_id(conn=active_conn)
            _write_product(cur, new_pid, final_unit, final_macros, final_piece, final_density, update_existing=False)
            input_name = item_fields.get("Name")
            base_name = (
                str(input_name).strip() if (input_name and str(input_name).strip())
                else (_first_name_per_id(cur, orig_pid) or f"Product #{orig_pid}")
            )
            ex_alias = cur.execute("SELECT ID FROM ALIASES WHERE Name = ?", (base_name,)).fetchone()
            if ex_alias is None:
                cur.execute("INSERT INTO ALIASES (Name, ID) VALUES (?, ?)", (base_name, new_pid))
            elif ex_alias[0] != new_pid:
                cand = f"{base_name} (edited)"
                if cur.execute("SELECT 1 FROM ALIASES WHERE Name = ?", (cand,)).fetchone():
                    cand = f"{base_name} (#{new_pid})"
                cur.execute("INSERT INTO ALIASES (Name, ID) VALUES (?, ?)", (cand, new_pid))
            final_pid, created_new_product = new_pid, True
        else:
            if macros_changed or any(k in item_fields for k in ("Unit", "Grams_per_piece", "Density_kg_per_L")):
                _write_product(cur, orig_pid, final_unit, final_macros, final_piece, final_density, update_existing=True)
            input_name = item_fields.get("Name")
            if input_name and str(input_name).strip():
                clean_name = str(input_name).strip()
                ex_alias = cur.execute("SELECT ID FROM ALIASES WHERE Name = ?", (clean_name,)).fetchone()
                if ex_alias is None:
                    cur.execute("INSERT INTO ALIASES (Name, ID) VALUES (?, ?)", (clean_name, orig_pid))
                elif ex_alias[0] != orig_pid:
                    raise AliasConflict(f"Name '{clean_name}' already exists for another product ID {ex_alias[0]}.")

        if final_loc:
            cur.execute("INSERT OR IGNORE INTO LOCATIONS (Name) VALUES (?)", (final_loc,))
        cur.execute(
            "UPDATE INVENTORY SET ID = ?, Unit_Weight = ?, Intact_Items = ?, Open_Remainder = ?, "
            "Location = ?, Expiration_Date = ? WHERE Row_ID = ?",
            (final_pid, final_u_weight, final_intact, final_rem, final_loc, final_exp, target_row_id),
        )
        return {
            "outcome": "ok", "row_id": target_row_id, "product_id": final_pid,
            "previous_product_id": orig_pid, "propagated": propagate,
            "created_new_product": created_new_product, "macros_changed": macros_changed,
        }


def clear_inventory(conn=None, db_path=None) -> bool:
    """Atomically deletes all items from INVENTORY and EXCLUSION_LIST while preserving PRODUCTS/ALIASES/LOCATIONS."""
    with _db_ctx(db_path=db_path, conn=conn, always_commit=True) as active_conn:
        active_conn.execute("DELETE FROM INVENTORY")
        active_conn.execute("DELETE FROM EXCLUSION_LIST")
        return True


def get_receipt_logs(conn=None, db_path=None) -> list[tuple[str, str, str]]:
    """Returns list of receipt log entries: (time, day, file_name) ordered by id DESC."""
    with _db_ctx(db_path=db_path, conn=conn) as active_conn:
        if active_conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='RECEIPT_LOGS'").fetchone() is None:
            return []
        return active_conn.execute("SELECT time, day, file_name FROM RECEIPT_LOGS ORDER BY id DESC").fetchall()


def log_receipt(time_str: str, day_str: str, file_name: str, conn=None, db_path=None) -> int:
    """Logs an imported receipt into RECEIPT_LOGS and returns the inserted row id."""
    with _db_ctx(db_path=db_path, conn=conn, always_commit=True) as active_conn:
        active_conn.execute(
            "CREATE TABLE IF NOT EXISTS RECEIPT_LOGS ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT NOT NULL, day TEXT NOT NULL, file_name TEXT NOT NULL)"
        )
        return active_conn.execute(
            "INSERT INTO RECEIPT_LOGS (time, day, file_name) VALUES (?, ?, ?)",
            (time_str, day_str, file_name),
        ).lastrowid


add_receipt_log = log_receipt
