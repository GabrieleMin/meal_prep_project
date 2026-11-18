"""recipes.py - Recipe creation, import and deletion (quantities for ONE serving)."""

import argparse
import csv
import json
import math
import os
import sqlite3
import sys

from meal_planner import engine as m
from meal_planner.db.connection import DB_PATH, get_connection
from meal_planner.inventory import _validate_and_save_product

UNITS = ("g", "kg", "ml", "L", "pcs", "tbsp", "tsp")

CANONICAL_PRODUCT_UNITS = {
    "kg": "kg", "kgs": "kg", "g": "kg", "gr": "kg", "gram": "kg", "grams": "kg",
    "l": "L", "lt": "L", "ml": "L", "cl": "L", "liter": "L", "liters": "L",
    "pcs": "pcs", "pz": "pcs", "pc": "pcs", "piece": "pcs", "pieces": "pcs",
}


def is_ingredient_known(conn: sqlite3.Connection, name: str) -> bool:
    """Checks if an ingredient name resolves to ALIASES or SUBSTITUTE_GROUPS (case-insensitive)."""
    if not name or not isinstance(name, str) or not name.strip():
        return False
    entry = name.strip()
    return conn.execute(
        "SELECT 1 FROM ALIASES WHERE Name = ? COLLATE NOCASE "
        "UNION ALL SELECT 1 FROM SUBSTITUTE_GROUPS WHERE Name = ? COLLATE NOCASE LIMIT 1",
        (entry, entry),
    ).fetchone() is not None


def register_product_on_the_fly(
    conn: sqlite3.Connection = None,
    name: str = "",
    unit: str = "kg",
    macros: dict = None,
    grams_per_piece: float = None,
    density_kg_per_l: float = None,
    db_path: str = None,
) -> int:
    """Registers an unknown product and its primary alias on-the-fly into PRODUCTS and ALIASES."""
    manage_conn = conn is None
    if manage_conn:
        conn = get_connection(db_path)
    try:
        if not name or not isinstance(name, str) or not name.strip():
            raise ValueError("Product name cannot be empty.")
        clean_name = name.strip()
        cur = conn.cursor()

        row = cur.execute("SELECT ID FROM ALIASES WHERE Name = ? COLLATE NOCASE", (clean_name,)).fetchone()
        if row:
            return row[0]

        if cur.execute("SELECT 1 FROM SUBSTITUTE_GROUPS WHERE Name = ? COLLATE NOCASE", (clean_name,)).fetchone():
            raise ValueError(f"Name '{clean_name}' conflicts with an existing substitute group.")

        raw_u = (unit if isinstance(unit, str) else "").strip().lower().rstrip(".")
        canonical_unit = CANONICAL_PRODUCT_UNITS.get(raw_u)
        if canonical_unit not in ("kg", "L", "pcs"):
            raise ValueError(f"Invalid unit '{unit}'. Must be one of kg, g, L, ml, pcs/pz.")

        try:
            next_id, _, _ = _validate_and_save_product(
                cur, unit=canonical_unit, macros=macros,
                grams_per_piece=grams_per_piece, density_kg_per_l=density_kg_per_l,
                normalize_keys=True, allow_str_macros=True, allow_comma=True,
                allow_str_factors=False, default_liquid_density=True, err_cls=ValueError,
            )
            cur.execute("INSERT INTO ALIASES (Name, ID) VALUES (?, ?)", (clean_name, next_id))
            conn.commit()
            return next_id
        except Exception:
            conn.rollback()
            raise
    finally:
        if manage_conn:
            conn.close()


def _id(conn, table, column, name):
    if not name or not isinstance(name, str) or not name.strip():
        return None
    r = conn.execute(f"SELECT {column} FROM {table} WHERE Name = ? COLLATE NOCASE", (name.strip(),)).fetchone()
    return r[0] if r else None


def _yes(value):
    return str(value).strip().lower() in ("1", "yes", "y", "true", "x", "si", "sì")


def create_recipe(conn, name, meals, ingredients, notes=None, _alias_cache=None, _group_cache=None) -> dict:
    """Creates a recipe atomically. ValueError lists all validation problems."""
    name, errors, rows = (name or "").strip(), [], []
    if not name:
        errors.append("name is missing")
    valid = {r[0] for r in conn.execute("SELECT Name FROM MEALS")}
    errors += [f"unknown meal '{p}' (valid: {', '.join(sorted(valid))})" for p in meals if p not in valid]
    if not meals:
        errors.append("specify at least one meal")
    for ing in ingredients:
        d = ing if isinstance(ing, dict) else dict(zip(("product", "quantity", "unit", "to_taste"), ing))
        entry = d.get("product") or d.get("group")
        entry_key = entry.strip().lower() if (entry and isinstance(entry, str)) else None
        product_id = _alias_cache.get(entry_key) if (_alias_cache is not None and entry_key) else _id(conn, "ALIASES", "ID", entry)
        group_id = (
            (_group_cache.get(entry_key) if (_group_cache is not None and entry_key) else _id(conn, "SUBSTITUTE_GROUPS", "Group_ID", entry))
            if product_id is None else None
        )
        try:
            q = float(str(d.get("quantity")).replace(",", "."))
            if not math.isfinite(q) or q <= 0:
                q = 0
        except (ValueError, TypeError):
            q = 0
        if product_id is None and group_id is None:
            errors.append(f"unknown ingredient '{entry}'")
        if q <= 0:
            errors.append(f"invalid quantity for '{entry}'")
        if d.get("unit") not in UNITS:
            errors.append(f"invalid unit for '{entry}' (valid: {', '.join(UNITS)})")
        rows.append((product_id, group_id, q, d.get("unit"), int(_yes(d.get("to_taste", 0)))))
    if not ingredients:
        errors.append("specify at least one ingredient")
    if errors:
        raise ValueError(f"Recipe '{name}' is not valid: " + "; ".join(errors))
    try:
        rid = conn.execute("INSERT INTO RECIPES (Name, Notes) VALUES (?, ?)", (name, notes)).lastrowid
        conn.executemany("INSERT INTO RECIPE_MEALS (Recipe_ID, Meal) VALUES (?, ?)", [(rid, p) for p in meals])
        conn.executemany(
            "INSERT INTO RECIPE_INGREDIENTS (Recipe_ID, ID, Group_ID, Quantity, Unit, To_Taste) VALUES (?, ?, ?, ?, ?, ?)",
            [(rid, *r) for r in rows],
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        raise ValueError(f"A recipe named '{name}' already exists.")
    except Exception:
        conn.rollback()
        raise
    warnings = (
        ["conversion data missing (g per piece, density) or optional nutrients: the macros may be underestimated"]
        if m.recipe_macros(conn, rid)["Incomplete_Data"] else []
    )
    return {"Recipe_ID": rid, "warnings": warnings}


def delete_recipe(conn, name) -> None:
    rid = _id(conn, "RECIPES", "Recipe_ID", name)
    if rid is None:
        raise ValueError(f"Recipe '{name}' not found.")
    if conn.execute("SELECT 1 FROM COOKINGS WHERE Recipe_ID = ?", (rid,)).fetchone():
        raise ValueError(f"'{name}' is used in plans or cookings: it cannot be deleted.")
    try:
        conn.execute("DELETE FROM RECIPE_INGREDIENTS WHERE Recipe_ID = ?", (rid,))
        conn.execute("DELETE FROM RECIPE_MEALS WHERE Recipe_ID = ?", (rid,))
        conn.execute("DELETE FROM RECIPES WHERE Recipe_ID = ?", (rid,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _from_csv(path) -> list:
    with open(path, newline="", encoding="utf-8-sig") as f:
        text = f.read()
    lines = text.splitlines()
    separator = ";" if ";" in lines[0] else ","
    recipes = {}
    for r in csv.DictReader(lines, delimiter=separator):
        entry = recipes.setdefault(
            r["recipe"],
            {"name": r["recipe"], "ingredients": [], "meals": [p.strip() for p in r["meals"].split("|") if p.strip()]},
        )
        if r.get("notes") and "notes" not in entry:
            entry["notes"] = r["notes"].strip()
        entry["ingredients"].append({
            "product": r["product"], "quantity": r["quantity"], "unit": r["unit"], "to_taste": r.get("to_taste", ""),
        })
    return list(recipes.values())


def import_file(conn, path):
    """Imports a .json or .csv file. Returns (recipes_created, errors)."""
    try:
        if os.path.splitext(path)[1].lower() == ".json":
            with open(path, encoding="utf-8") as f:
                listing = json.load(f)
            if not isinstance(listing, list):
                raise ValueError("the JSON must be a list of recipes")
        else:
            listing = _from_csv(path)
    except (OSError, ValueError, KeyError) as e:
        raise ValueError(f"File not readable: {e}")

    created, errors = 0, []
    alias_cache = {r[0].strip().lower(): r[1] for r in conn.execute("SELECT Name, ID FROM ALIASES WHERE Name IS NOT NULL")}
    group_cache = {r[0].strip().lower(): r[1] for r in conn.execute("SELECT Name, Group_ID FROM SUBSTITUTE_GROUPS WHERE Name IS NOT NULL")}
    for r in listing:
        try:
            create_recipe(
                conn, r.get("name"), r.get("meals", []), r.get("ingredients", []), r.get("notes"),
                _alias_cache=alias_cache, _group_cache=group_cache,
            )
            created += 1
        except ValueError as e:
            errors.append(str(e))
    return created, errors


def main():
    parser = argparse.ArgumentParser(description="Recipe management.")
    parser.add_argument("--db", default=DB_PATH)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("import", help="import recipes from .json or .csv").add_argument("file")
    args = parser.parse_args()
    conn = get_connection(args.db)
    try:
        created, errors = import_file(conn, args.file)
    except ValueError as e:
        sys.exit(str(e))
    finally:
        conn.close()
    print(f"Recipes imported: {created}")
    for e in errors:
        print("Skipped:", e)


if __name__ == "__main__":
    main()
