"""Unit conversions, recipe macros and daily limit checks."""

import math

from meal_planner.engine.catalog import load_products, recipe_ingredients
from meal_planner.engine.constants import EPS, KCAL_PER_G, MACROS, LIMITED_NUTRIENTS


def grams(quantity: float, unit: str, prod: dict):
    """Recipe quantity -> grams (same logic as INGREDIENT_GRAMS_VIEW).
    None if the conversion data is missing."""
    if unit == "g":
        return quantity
    if unit == "kg":
        return quantity * 1000.0
    if unit == "pcs":
        return quantity * prod["g_piece"] if prod["g_piece"] else None
    if not prod["density"]:
        return None
    factors = {"ml": 1.0, "L": 1000.0, "tbsp": 15.0, "tsp": 5.0}
    return quantity * factors[unit] * prod["density"]


def inventory_quantity(quantity: float, unit: str, prod: dict):
    """Recipe quantity -> inventory unit of the product (kg, L or pcs).
    None if not convertible."""
    u = prod["unit"]
    if u == "pcs":
        if unit == "pcs":
            return math.ceil(round(quantity, 6))
        g = grams(quantity, unit, prod)
        if g is None or not prod["g_piece"]:
            return None
        return math.ceil(round(g / prod["g_piece"], 6))
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
    """Macros of `servings` servings. A group ingredient with no choice in
    `choices` ({group_id: product_id}) is not summed and sets Incomplete_Data."""
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
        for m in MACROS:
            v = p["macro"][m]
            if v is None:          # missing optional nutrient: counts as 0 and is reported
                incomplete = True
                v = 0.0
            tot[m] += g / 100.0 * v
    tot["Incomplete_Data"] = incomplete
    return tot


def sum_macros(items: list) -> dict:
    return {m: sum(x[m] for x in items) for m in MACROS}


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
    """{nutrient: (min_g, max_g)}: the rules of the same nutrient are
    intersected (the highest minimum and the lowest maximum hold)."""
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
    """Compares the day totals with kcal and limits. Each entry reports value,
    limits (already widened by extra_pct), deviation (>= 0) and direction."""
    f_min, f_max = 1.0 - extra_pct / 100.0, 1.0 + extra_pct / 100.0
    entries = []

    def add(name, unit, value, mn, mx):
        mn = None if mn is None else mn * f_min
        mx = None if mx is None else mx * f_max
        below = (mn - value) if mn is not None and value < mn else 0.0
        above = (value - mx) if mx is not None and value > mx else 0.0
        deviation = max(below, above)
        entries.append({"nutrient": name, "unit": unit, "value": value,
                     "minimum": mn, "maximum": mx, "deviation": deviation,
                     "direction": "below" if below > 0 else ("above" if above > 0 else None),
                     "ok": deviation <= EPS})

    kt = profile.get("Kcal_target")
    if kt:
        tol = profile["Kcal_tolerance_pct"] / 100.0
        add("Kcal", "kcal", totals["Kcal"], kt * (1 - tol), kt * (1 + tol))
    limits = limits_in_grams(goals, totals["Kcal"], profile["Weight_kg"])
    for n in LIMITED_NUTRIENTS:
        if n in limits:
            add(n, "g", totals[n], *limits[n])
    return {"entries": entries, "ok": all(v["ok"] for v in entries), "kcal_target_defined": bool(kt)}
