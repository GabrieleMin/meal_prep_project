"""Unified calculation engine: constants, catalog access, nutrition, VirtualInventory, profile, and CLI."""

import argparse
import contextlib
import math
from pathlib import Path
import sqlite3
import sys

from meal_planner.db import connection as _db_conn

MACROS = ["Kcal", "Protein", "Carbs", "Fat", "Sugars", "Saturated_fat", "Fiber", "Salt"]
LIMITED_NUTRIENTS = [m for m in MACROS if m != "Kcal"]
OPTIONAL = ("Sugars", "Saturated_fat", "Fiber", "Salt")
KCAL_PER_G = {"Protein": 4.0, "Carbs": 4.0, "Sugars": 4.0, "Fat": 9.0, "Saturated_fat": 9.0}
PAL = {"light": 1.4, "moderate": 1.6, "heavy": 1.75}
KCAL_PER_KG = 7700
EPS = 1e-9

try:
    from meal_planner.engine._speedups import sum_macros as _c_sum_macros
    def sum_macros(items: list) -> dict:
        return _c_sum_macros(items)
except (ImportError, AttributeError):
    def sum_macros(items: list) -> dict:
        return {m: sum(x[m] for x in items) for m in MACROS}

try:
    from meal_planner.engine._speedups import round_down_to_hundredth as _arr
except (ImportError, AttributeError):
    def _arr(value: float) -> float:
        return math.floor(round(value * 100, 6)) / 100


@contextlib.contextmanager
def _conn_ctx(conn=None, db_path=None, commit: bool = False):
    if conn is not None and not isinstance(conn, (str, bytes, Path)):
        active_conn, manage = conn, False
    else:
        target = conn if isinstance(conn, (str, Path)) else db_path
        active_conn, manage = _db_conn.get_connection(target), True
    try:
        yield active_conn
        if commit:
            active_conn.commit()
    except Exception:
        if manage:
            active_conn.rollback()
        raise
    finally:
        if manage:
            active_conn.close()


# ---- Catalog ----

def load_products(conn) -> dict:
    rows = conn.execute(
        "SELECT ID, Unit, Grams_per_piece, Density_kg_per_L, Kcal_100g, Protein_100g, "
        "Carbs_100g, Fat_100g, Sugars_100g, Saturated_fat_100g, Fiber_100g, Salt_100g FROM PRODUCTS"
    ).fetchall()
    return {
        r[0]: {"ID": r[0], "unit": r[1], "g_piece": r[2], "density": r[3], "macro": dict(zip(MACROS, r[4:]))}
        for r in rows
    }


def product_names(conn) -> dict:
    names = {}
    for product_id, name in conn.execute("SELECT ID, Name FROM ALIASES ORDER BY rowid"):
        names.setdefault(product_id, name)
    return names


def recipe_id_from_name(conn, name: str):
    r = conn.execute("SELECT Recipe_ID FROM RECIPES WHERE Name = ?", (name,)).fetchone()
    return r[0] if r else None


def recipe_ingredients(conn, recipe_id: int) -> list:
    rows = conn.execute(
        "SELECT ID, Group_ID, Quantity, Unit, To_Taste FROM RECIPE_INGREDIENTS "
        "WHERE Recipe_ID = ? ORDER BY Row_ID", (recipe_id,),
    ).fetchall()
    return [dict(zip(("ID", "Group_ID", "Quantity", "Unit", "To_Taste"), r)) for r in rows]


def group_members(conn, group_id: int) -> list:
    return [r[0] for r in conn.execute("SELECT ID FROM GROUP_MEMBERS WHERE Group_ID = ? ORDER BY ID", (group_id,))]


def load_profile(conn=None, db_path: str = None) -> dict | None:
    with _conn_ctx(conn, db_path) as active_conn:
        cur = active_conn.execute(
            "SELECT Weight_kg, Height_cm, Age_years, Sex, Activity_Level, Goal, "
            "Kcal_target, Kcal_tolerance_pct, Open_extra_pct, No_repeat_days, Target_weight_kg, "
            "Weekly_rate_kg, Maintenance_manual, Use_manual_maintenance, Kcal_manual, Use_manual_kcal, "
            "Default_servings FROM PROFILE WHERE ID = 1"
        )
        r = cur.fetchone()
        return None if r is None else dict(zip([d[0] for d in cur.description], r))


def load_goals(conn) -> list:
    return [
        dict(zip(("Nutrient", "Type", "Min_Value", "Max_Value"), r))
        for r in conn.execute("SELECT Nutrient, Type, Min_Value, Max_Value FROM GOALS")
    ]


# ---- Nutrition ----

def grams(quantity: float, unit: str, prod: dict):
    """Recipe quantity -> grams (None if conversion data is missing)."""
    if unit == "g":
        return quantity
    if unit == "kg":
        return quantity * 1000.0
    if unit == "pcs":
        return quantity * prod["g_piece"] if prod["g_piece"] else None
    if not prod["density"]:
        return None
    return quantity * {"ml": 1.0, "L": 1000.0, "tbsp": 15.0, "tsp": 5.0}[unit] * prod["density"]


def inventory_quantity(quantity: float, unit: str, prod: dict):
    """Recipe quantity -> inventory unit of the product (kg, L or pcs), or None if not convertible."""
    u = prod["unit"]
    if u == "pcs":
        if unit == "pcs":
            return math.ceil(round(quantity, 6))
        g = grams(quantity, unit, prod)
        return math.ceil(round(g / prod["g_piece"], 6)) if (g is not None and prod["g_piece"]) else None
    if u == "L" and unit in ("ml", "L", "tbsp", "tsp"):
        return quantity * {"ml": 0.001, "L": 1.0, "tbsp": 0.015, "tsp": 0.005}[unit]
    g = grams(quantity, unit, prod)
    if g is None:
        return None
    if u == "kg":
        return g / 1000.0
    return g / (1000.0 * prod["density"]) if prod["density"] else None


def recipe_macros(conn, recipe_id: int, products: dict = None, choices: dict = None,
                  servings: int = 1, ingredients: list = None) -> dict:
    """Macros of `servings` servings."""
    products = products if products is not None else load_products(conn)
    choices = choices or {}
    tot = {m: 0.0 for m in MACROS}
    incomplete = False
    for ing in (ingredients if ingredients is not None else recipe_ingredients(conn, recipe_id)):
        product_id = ing["ID"] if ing["ID"] is not None else choices.get(ing["Group_ID"])
        if product_id is None:
            incomplete = True
            continue
        p = products[product_id]
        g = grams(ing["Quantity"] * servings, ing["Unit"], p)
        if g is None:
            incomplete = True
            continue
        factor = g / 100.0
        for m in MACROS:
            v = p["macro"][m]
            if v is None:
                incomplete = True
                v = 0.0
            tot[m] += factor * v
    tot["Incomplete_Data"] = incomplete
    return tot


def _convert(kind, nutrient, value, kcal, weight_kg):
    if value is None:
        return None
    if kind == "absolute":
        return value
    if kind == "per_kg":
        return value * weight_kg
    if kind == "per_1000kcal":
        return value * kcal / 1000.0
    if kind == "pct_energy":
        if nutrient not in KCAL_PER_G:
            raise ValueError(f"pct_energy not applicable to {nutrient}")
        return value / 100.0 * kcal / KCAL_PER_G[nutrient]
    raise ValueError(f"Unknown goal type: {kind}")


def limits_in_grams(goals: list, day_kcal: float, weight_kg: float) -> dict:
    """{nutrient: (min_g, max_g)}: rules of the same nutrient are intersected."""
    limits = {}
    for o in goals:
        n = o["Nutrient"]
        mn = _convert(o["Type"], n, o["Min_Value"], day_kcal, weight_kg)
        mx = _convert(o["Type"], n, o["Max_Value"], day_kcal, weight_kg)
        cur_mn, cur_mx = limits.get(n, (None, None))
        if mn is not None:
            cur_mn = mn if cur_mn is None else max(cur_mn, mn)
        if mx is not None:
            cur_mx = mx if cur_mx is None else min(cur_mx, mx)
        limits[n] = (cur_mn, cur_mx)
    return limits


def check_day(totals: dict, profile: dict, goals: list, extra_pct: float = 0.0) -> dict:
    """Compares day totals with kcal and nutritional limits."""
    f_min, f_max = 1.0 - extra_pct / 100.0, 1.0 + extra_pct / 100.0
    entries = []

    def add(name, unit, value, mn, mx):
        mn = None if mn is None else mn * f_min
        mx = None if mx is None else mx * f_max
        below = (mn - value) if mn is not None and value < mn else 0.0
        above = (value - mx) if mx is not None and value > mx else 0.0
        deviation = max(below, above)
        entries.append({
            "nutrient": name, "unit": unit, "value": value,
            "minimum": mn, "maximum": mx, "deviation": deviation,
            "direction": "below" if below > 0 else ("above" if above > 0 else None),
            "ok": deviation <= EPS,
        })

    kt = profile.get("Kcal_target")
    if kt:
        tol = profile["Kcal_tolerance_pct"] / 100.0
        add("Kcal", "kcal", totals["Kcal"], kt * (1 - tol), kt * (1 + tol))
    limits = limits_in_grams(goals, totals["Kcal"], profile["Weight_kg"])
    for n in LIMITED_NUTRIENTS:
        if n in limits:
            add(n, "g", totals[n], *limits[n])
    return {"entries": entries, "ok": all(v["ok"] for v in entries), "kcal_target_defined": bool(kt)}


# ---- VirtualInventory & Simulation ----

class VirtualInventory:
    """In-memory copy of INVENTORY mirroring consume_quantity() (open remainders first, then FIFO)."""

    def __init__(self, rows: dict, unit: dict):
        self.rows = rows
        self.unit = unit

    @classmethod
    def from_db(cls, conn):
        rows = {}
        for row_id, product_id, weight, number, remainder in conn.execute(
            "SELECT Row_ID, ID, Unit_Weight, Intact_Items, Open_Remainder FROM INVENTORY ORDER BY ID, Row_ID"
        ):
            rows.setdefault(product_id, []).append([row_id, weight, number, remainder])
        return cls(rows, dict(conn.execute("SELECT ID, Unit FROM PRODUCTS").fetchall()))

    def copy(self):
        return VirtualInventory(self.rows.copy(), self.unit)

    def availability(self, product_id: int) -> float:
        rows = self.rows.get(product_id, [])
        if self.unit[product_id] == "pcs":
            return sum(r[2] for r in rows)
        return sum(r[2] * r[1] + r[3] for r in rows)

    def open_remainder(self, product_id: int) -> float:
        return sum(r[3] for r in self.rows.get(product_id, []))

    def preview(self, product_id: int, quantity: float) -> dict:
        return VirtualInventory({product_id: [r[:] for r in self.rows.get(product_id, [])]}, self.unit).consume(product_id, quantity)

    def write(self, conn, ids) -> None:
        deletes, updates = [], []
        for product_id in ids:
            new = {r[0]: r for r in self.rows.get(product_id, [])}
            for (row_id,) in conn.execute("SELECT Row_ID FROM INVENTORY WHERE ID = ?", (product_id,)).fetchall():
                if row_id not in new:
                    deletes.append((row_id,))
            for row_id, (_, _, number, remainder) in new.items():
                updates.append((number, remainder, row_id))
        if deletes:
            conn.executemany("DELETE FROM INVENTORY WHERE Row_ID = ?", deletes)
        if updates:
            conn.executemany("UPDATE INVENTORY SET Intact_Items = ?, Open_Remainder = ? WHERE Row_ID = ?", updates)

    def consume(self, product_id: int, quantity: float) -> dict:
        rows = [r[:] for r in self.rows.get(product_id, [])]
        pz = self.unit[product_id] == "pcs"
        disp = sum(r[2] for r in rows) if pz else sum(r[2] * r[1] + r[3] for r in rows)
        shortfall, to_cover = 0.0, quantity
        if disp < quantity:
            shortfall = (quantity - disp) if pz else _arr(quantity - disp)
            to_cover = disp
        remaining, from_open, opened = to_cover, 0.0, 0
        if pz:
            for r in rows:
                if remaining <= 0:
                    break
                withdrawal = min(r[2], remaining)
                if withdrawal > 0:
                    r[2] -= withdrawal
                    remaining -= withdrawal
        else:
            for r in rows:
                if remaining <= 0:
                    break
                if r[3] > 0:
                    withdrawal = min(r[3], remaining)
                    r[3] = _arr(r[3] - withdrawal)
                    remaining -= withdrawal
                    from_open += withdrawal
            for r in rows:
                if remaining <= 0 or r[2] <= 0:
                    continue
                max_weight = r[2] * r[1]
                if remaining >= max_weight:
                    remaining = _arr(remaining - max_weight)
                    opened += r[2]
                    r[2], r[3] = 0, 0.0
                else:
                    full_packs = int(remaining // r[1])
                    rem = _arr(remaining - (full_packs * r[1]))
                    if rem > 0:
                        r[2] -= full_packs + 1
                        opened += full_packs + 1
                        r[3] = _arr(r[1] - rem)
                    else:
                        r[2] -= full_packs
                        opened += full_packs
                        r[3] = 0.0
                    remaining = 0.0
                if remaining <= 0:
                    break
        self.rows[product_id] = [r for r in rows if not (r[2] == 0 and r[3] == 0)]
        return {"consumed": to_cover, "shortfall": shortfall, "from_open": from_open, "packages_opened": opened}


def _choose_member(conn, inv, products, group_id, quantity, unit):
    best = None
    for product_id in group_members(conn, group_id):
        q = inventory_quantity(quantity, unit, products[product_id])
        if q is None:
            continue
        trial = inv.preview(product_id, q)
        key = (trial["shortfall"] > 0, -min(1.0, trial["from_open"] / q), trial["packages_opened"], product_id)
        if best is None or key < best[0]:
            best = (key, product_id)
    return best[1] if best else None


def simulate_recipe(conn, inv: VirtualInventory, recipe_id: int, products: dict = None,
                    servings: int = 1, choices: dict = None, ingredients: list = None) -> dict:
    """Consumes the recipe from `inv` (which is MODIFIED; pass inv.copy() for a trial)."""
    products = products if products is not None else load_products(conn)
    choices = dict(choices or {})
    detail, missing, coverages, new, incomplete = [], {}, [], 0, False
    for ing in (ingredients if ingredients is not None else recipe_ingredients(conn, recipe_id)):
        qty = ing["Quantity"] * servings
        product_id = ing["ID"]
        if product_id is None:
            gid = ing["Group_ID"]
            if gid not in choices:
                choices[gid] = _choose_member(conn, inv, products, gid, qty, ing["Unit"])
            product_id = choices[gid]
        if product_id is None:
            incomplete = True
            continue
        p = products[product_id]
        q = inventory_quantity(qty, ing["Unit"], p)
        if q is None:
            incomplete = True
            continue
        r = inv.consume(product_id, q)
        coverages.append(min(1.0, r["from_open"] / q) if q > 0 else 0.0)
        new += r["packages_opened"]
        if r["shortfall"] > 0:
            missing[product_id] = missing.get(product_id, 0.0) + r["shortfall"]
        detail.append({
            "ID": product_id, "requested": q, "unit": p["unit"],
            "from_open": r["from_open"], "packages_opened": r["packages_opened"], "shortfall": r["shortfall"],
        })
    return {
        "detail": detail,
        "open_coverage": sum(coverages) / len(coverages) if coverages else 0.0,
        "new_packages": new,
        "missing": missing,
        "feasible": not missing and not incomplete,
        "incomplete_data": incomplete,
        "group_choices": choices,
    }


# ---- Profile ----

def estimate_maintenance(weight_kg, height_cm, age, sex, activity) -> int:
    bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age + (5 if sex == "M" else -161)
    return int(math.floor(bmr * PAL[activity] + 0.5))


def bmi(weight_kg, height_cm):
    value = weight_kg / (height_cm / 100) ** 2
    category = (
        "underweight" if value < 18.5 else "normal" if value < 25
        else "overweight" if value < 30 else "obese"
    )
    return value, category


def estimates(p):
    """-> (calculated maintenance, calculated target, target in use) from a profile dict."""
    calc = (
        estimate_maintenance(p["Weight_kg"], p["Height_cm"], p["Age_years"], p["Sex"], p["Activity_Level"])
        if (p["Sex"] and p["Activity_Level"]) else None
    )
    maintenance = p["Maintenance_manual"] if p["Use_manual_maintenance"] else calc
    target = None
    if maintenance:
        if p["Goal"] == "maintenance":
            target = maintenance
        elif p["Goal"] and p["Weekly_rate_kg"]:
            daily = p["Weekly_rate_kg"] * KCAL_PER_KG / 7
            target = int(math.floor(maintenance + (daily if p["Goal"] == "weight_gain" else -daily) + 0.5))
    return calc, target, (p["Kcal_manual"] if p["Use_manual_kcal"] else target)


def reset_profile(conn=None, db_path=None) -> bool:
    """Hard-deletes all rows from PROFILE and WEIGHT_LOG."""
    with _conn_ctx(conn, db_path, commit=True) as active_conn:
        active_conn.execute("DELETE FROM PROFILE")
        active_conn.execute("DELETE FROM WEIGHT_LOG")
        return True


def update_profile(conn=None, values=None, db_path=None):
    """Saves the profile form and logs today's measurements; returns the target in use."""
    if values is None and isinstance(conn, dict):
        values, conn = conn, None
    v = dict(values)
    log = (v.pop("Body_fat_pct", None), v.pop("Waist_cm", None))
    if (v.get("Use_manual_maintenance") and v.get("Maintenance_manual") is None) or (
        v.get("Use_manual_kcal") and v.get("Kcal_manual") is None
    ):
        raise ValueError("Enter the manual value or untick 'Use manual'.")
    v["Kcal_target"] = estimates(v)[2]
    if v["Kcal_target"] is None:
        raise ValueError("For weight_loss or weight_gain set the weekly rate (or a manual kcal/day).")

    with _conn_ctx(conn, db_path, commit=True) as active_conn:
        if active_conn.execute("SELECT 1 FROM PROFILE WHERE ID = 1").fetchone() is not None:
            active_conn.execute("UPDATE PROFILE SET " + ", ".join(f"{k} = ?" for k in v) + " WHERE ID = 1", list(v.values()))
        else:
            v["ID"] = 1
            cols = list(v.keys())
            active_conn.execute(
                "INSERT INTO PROFILE (" + ", ".join(cols) + ") VALUES (" + ", ".join("?" for _ in cols) + ")",
                list(v.values()),
            )
        if v.get("Weight_kg") is not None:
            active_conn.execute(
                "INSERT OR REPLACE INTO WEIGHT_LOG (Date, Weight_kg, Body_fat_pct, Waist_cm) "
                "VALUES (date('now', 'localtime'), ?, ?, ?)",
                (v["Weight_kg"], *log),
            )
        return v["Kcal_target"]


def set_meal_shares(conn, shares):
    total = sum(shares.values())
    if abs(total - 100) > 0.01:
        raise ValueError(f"Meal energy shares must add up to 100% (now {total:g}%).")
    conn.executemany("UPDATE MEALS SET Energy_Share_pct = ? WHERE Name = ?", [(s, n) for n, s in shares.items()])
    conn.commit()


def set_profile(conn, sex, activity, goal, kcal_target=None):
    p = load_profile(conn)
    if kcal_target is None:
        if goal != "maintenance":
            raise ValueError("For weight_loss or weight_gain pass --kcal: no automatic deficit or surplus is applied.")
        kcal_target = estimate_maintenance(p["Weight_kg"], p["Height_cm"], p["Age_years"], sex, activity)
    conn.execute(
        "UPDATE PROFILE SET Sex = ?, Activity_Level = ?, Goal = ?, Kcal_target = ? WHERE ID = 1",
        (sex, activity, goal, kcal_target),
    )
    conn.commit()
    return kcal_target


# ---- CLI ----

def _fmt(v):
    return "-" if v is None else f"{v:.1f}"


def outcome(sim, products, names):
    if sim["feasible"]:
        return "feasible"
    parts = [f"{names[i]} {q:.2f} {products[i]['unit']}" for i, q in sim["missing"].items()]
    if sim["incomplete_data"]:
        parts.append("incomplete data")
    return "missing: " + ", ".join(parts)


def recipe_row(name, mac, sim, products, names):
    print(
        f"{name:<34}{mac['Kcal']:>6.0f}{mac['Protein']:>7.1f}{mac['Carbs']:>7.1f}"
        f"{mac['Fat']:>8.1f}{mac['Fiber']:>7.1f}{sim['open_coverage'] * 100:>7.0f}%"
        f"{sim['new_packages']:>6}  {outcome(sim, products, names)}"
    )


HEADER = f"{'Recipe':<34}{'kcal':>6}{'Prot':>7}{'Carb':>7}{'Fat':>8}{'Fiber':>7}{'Open':>8}{'New':>6}  Outcome"


def print_check(res):
    for v in res["entries"]:
        status = "ok" if v["ok"] else f"{v['direction'].upper()} by {v['deviation']:.1f}"
        print(f"  {v['nutrient']:<14}{v['value']:>9.1f} {v['unit']:<5}[{_fmt(v['minimum'])} - {_fmt(v['maximum'])}]  {status}")


def cmd_recipes(conn):
    products, names = load_products(conn), product_names(conn)
    inv = VirtualInventory.from_db(conn)
    print("Each recipe is simulated on the current inventory, independently of the others.")
    print(HEADER)
    for recipe_id, name in conn.execute("SELECT Recipe_ID, Name FROM RECIPES ORDER BY Recipe_ID").fetchall():
        sim = simulate_recipe(conn, inv.copy(), recipe_id, products)
        recipe_row(name, recipe_macros(conn, recipe_id, products, sim["group_choices"]), sim, products, names)


def cmd_day(conn, recipe_names, kcal):
    products, names = load_products(conn), product_names(conn)
    profile = load_profile(conn)
    if kcal:
        profile["Kcal_target"] = kcal
    inv = VirtualInventory.from_db(conn)
    print("The recipes consume the same stock in sequence (virtual inventory).")
    print(HEADER)
    macros = []
    for name in recipe_names:
        recipe_id = recipe_id_from_name(conn, name)
        if recipe_id is None:
            sys.exit(f"Recipe '{name}' not found.")
        sim = simulate_recipe(conn, inv, recipe_id, products)
        mac = recipe_macros(conn, recipe_id, products, sim["group_choices"])
        macros.append(mac)
        recipe_row(name, mac, sim, products, names)
    totals = sum_macros(macros)
    res = check_day(totals, profile, load_goals(conn))
    kt = profile.get("Kcal_target")
    print("\nDay check" + (f" (target {kt:.0f} kcal +-{profile['Kcal_tolerance_pct']:.0f}%)" if kt else ""))
    print_check(res)
    if not res["kcal_target_defined"]:
        print("  Kcal: target not defined, energy check skipped (use --kcal or the 'profile' command).")
    print("\nOverall outcome:", "within limits" if res["ok"] else "OUT OF LIMITS")


def cmd_profile(conn, args):
    kcal = set_profile(conn, args.sex, args.activity, args.goal, args.kcal)
    print(f"Profile updated: sex {args.sex}, activity {args.activity}, goal {args.goal}, target {kcal:.0f} kcal/day.")


def main():
    parser = argparse.ArgumentParser(description="Calculation engine for recipes and plan.")
    parser.add_argument("--db", default=_db_conn.DB_PATH, help="database path")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("recipes", help="macros and feasibility of every recipe")
    g = sub.add_parser("day", help="check a day: one recipe per meal")
    g.add_argument("recipes", nargs="+")
    g.add_argument("--kcal", type=float, help="kcal target (otherwise the profile one)")
    p = sub.add_parser("profile", help="set sex, activity, goal and kcal target")
    p.add_argument("--sex", required=True, choices=["M", "F"])
    p.add_argument("--activity", required=True, choices=sorted(PAL))
    p.add_argument("--goal", required=True, choices=["maintenance", "weight_loss", "weight_gain"])
    p.add_argument("--kcal", type=float, help="required for weight_loss and weight_gain")
    args = parser.parse_args()

    conn = _db_conn.get_connection(args.db)
    try:
        if args.cmd == "recipes":
            cmd_recipes(conn)
        elif args.cmd == "day":
            cmd_day(conn, args.recipes, args.kcal)
        else:
            cmd_profile(conn, args)
    except ValueError as e:
        sys.exit(str(e))
    finally:
        conn.close()
