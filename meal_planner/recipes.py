"""
recipes.py - Recipe creation, import and deletion (quantities for ONE serving).

Command line:
    python -m meal_planner.recipes import recipes.json
    python -m meal_planner.recipes import recipes.csv

JSON format: [{"name": "...", "meals": ["lunch", "dinner"], "notes": "...",
               "ingredients": [{"product": "Rice", "quantity": 100, "unit": "g"},
                               {"group": "Cereal", "quantity": 80, "unit": "g", "to_taste": false}]}]
CSV format (one row per ingredient, separator ';' or ','): columns
    recipe;meals;product;quantity;unit;to_taste      (meals separated by '|', e.g. lunch|dinner)
Products and groups are given by name (aliases too, case-insensitive).
"""

import argparse
import csv
import json
import os
import sqlite3
import sys

from meal_planner import engine as m
from meal_planner.db.connection import get_connection, DB_PATH

UNITS = ("g", "kg", "ml", "L", "pcs", "tbsp", "tsp")


def _id(conn, table, column, name):
    r = conn.execute(f"SELECT {column} FROM {table} WHERE Name = ? COLLATE NOCASE", (name,)).fetchone()
    return r[0] if r else None


def _yes(value):
    return str(value).strip().lower() in ("1", "yes", "y", "true", "x", "si", "sì")   # "si"/"sì": CSV files from the Italian version


def create_recipe(conn, name, meals, ingredients, notes=None) -> dict:
    """Creates a recipe atomically. `ingredients` = [(name, quantity, unit)] or dicts with
    'product' or 'group', 'quantity', 'unit', 'to_taste'. ValueError lists all the problems."""
    name, errors, rows = (name or "").strip(), [], []
    if not name:
        errors.append("name is missing")
    valid = {r[0] for r in conn.execute("SELECT Name FROM MEALS")}
    errors += [f"unknown meal '{p}' (valid: {', '.join(sorted(valid))})" for p in meals if p not in valid]
    if not meals:
        errors.append("specify at least one meal")
    for ing in ingredients:
        d = ing if isinstance(ing, dict) else dict(zip(("product", "quantity", "unit"), ing))
        entry = d.get("product") or d.get("group")
        product_id, group_id = _id(conn, "ALIASES", "ID", entry), None
        if product_id is None:
            group_id = _id(conn, "SUBSTITUTE_GROUPS", "Group_ID", entry)
        try:
            q = float(str(d.get("quantity")).replace(",", "."))
        except ValueError:
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
    except sqlite3.IntegrityError:
        raise ValueError(f"A recipe named '{name}' already exists.")
    conn.executemany("INSERT INTO RECIPE_MEALS (Recipe_ID, Meal) VALUES (?, ?)", [(rid, p) for p in meals])
    conn.executemany("INSERT INTO RECIPE_INGREDIENTS (Recipe_ID, ID, Group_ID, Quantity, Unit, To_Taste) "
                     "VALUES (?, ?, ?, ?, ?, ?)", [(rid, *r) for r in rows])
    conn.commit()
    warnings = []
    if m.recipe_macros(conn, rid)["Incomplete_Data"]:
        warnings.append("conversion data missing (g per piece, density) or optional nutrients: "
                      "the macros may be underestimated")
    return {"Recipe_ID": rid, "warnings": warnings}


def delete_recipe(conn, name) -> None:
    rid = _id(conn, "RECIPES", "Recipe_ID", name)
    if rid is None:
        raise ValueError(f"Recipe '{name}' not found.")
    if conn.execute("SELECT 1 FROM COOKINGS WHERE Recipe_ID = ?", (rid,)).fetchone():
        raise ValueError(f"'{name}' is used in plans or cookings: it cannot be deleted.")
    conn.execute("DELETE FROM RECIPES WHERE Recipe_ID = ?", (rid,))
    conn.commit()


def _from_csv(path) -> list:
    with open(path, newline="", encoding="utf-8-sig") as f:
        text = f.read()
    separator = ";" if ";" in text.splitlines()[0] else ","
    recipes = {}
    for r in csv.DictReader(text.splitlines(), delimiter=separator):
        entry = recipes.setdefault(r["recipe"], {"name": r["recipe"], "ingredients": [],
                                                 "meals": [p.strip() for p in r["meals"].split("|") if p.strip()]})
        entry["ingredients"].append({"product": r["product"], "quantity": r["quantity"], "unit": r["unit"],
                                    "to_taste": r.get("to_taste", "")})
    return list(recipes.values())


def import_file(conn, path):
    """Imports a .json or .csv file. Each recipe is atomic: the invalid ones are skipped and reported.
    Returns (recipes created, list of errors)."""
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
    for r in listing:
        try:
            create_recipe(conn, r.get("name"), r.get("meals", []), r.get("ingredients", []), r.get("notes"))
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
