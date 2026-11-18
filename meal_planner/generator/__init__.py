"""meal_planner.generator - Deterministic Hybrid Recipe Generation Pipeline."""
from __future__ import annotations

import dataclasses
from datetime import date
import functools
import itertools
import logging
import math
import sqlite3
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

try:
    import scipy.optimize
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False

from meal_planner.db.connection import DB_PATH, get_connection
from .domain import (
    ARCHETYPES, ARCHETYPE_ALIASES, BREAKFAST_SNACK_BOWL, DEFAULT_PORTION_BOUNDS,
    GRAIN_PASTA_DISH, MAIN_AND_SIDE_PLATE, MEAL_ARCHETYPES, ONE_POT_BALANCED_BOWL,
    ROLE_COMPATIBILITY, Archetype, CulinaryTag, IngredientCandidate, RecipeCandidate,
    Role, Slot, SlotsDict, SlotValidationResult, SolverResult,
    are_mutually_exclusive, are_roles_compatible, clamp_ingredient_portion,
    classify_food, fingerprint_macros, get_archetype, get_archetypes_for_meal,
    get_default_archetype_for_meal, get_pairing_score, get_portion_bounds,
    get_slot_for_role, is_role_compatible, resolve_canonical, score_combination,
    validate_slot_filling,
)

# --- 1. Optimization Solver (SciPy HiGHS Simplex + Continuous Knapsack Fallback) ---

def _cand_field(cand: Any, canon: str, alias: str, default: Any = None) -> Any:
    snap = getattr(cand, "_alias_snapshot", None)
    if isinstance(snap, dict) and canon in snap:
        prev_c, prev_a = snap[canon]
        cur_c, cur_a = getattr(cand, canon, None), getattr(cand, alias, None)
        if cur_a != prev_a and cur_c == prev_c:
            return cur_a if cur_a is not None else default
        return cur_c if cur_c is not None else (None if canon == "days_to_expiration" else default)
    first, second = (canon, alias) if canon == "days_to_expiration" else (alias, canon)
    if (val := getattr(cand, first, None)) is not None:
        return val
    other = getattr(cand, second, None)
    return other if other is not None else default

def compute_ingredient_cost(
    cand: Any, pairing_bonus_weight: float = 2.0, open_bonus: float = -10.0,
    fifo_factor: float = -5.0, missing_penalty: float = 5.0,
) -> float:
    is_open = getattr(cand, "is_open", False) or getattr(cand, "open_remainder", 0.0) > 0
    c_open = open_bonus if is_open else 0.0
    days = _cand_field(cand, "days_to_expiration", "days_to_expiry", None)
    c_fifo = fifo_factor / max(1.0, float(days)) if days is not None else 0.0
    pairing = getattr(cand, "pairing_score", 0.5)
    c_pair = -pairing_bonus_weight * max(0.0, min(1.0, float(0.5 if pairing is None else pairing)))
    return c_open + c_fifo + c_pair + (missing_penalty if getattr(cand, "is_missing", False) else 0.0)

def _prepare_solver_items(candidates: Sequence[Any]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for idx, cand in enumerate(candidates):
        e = max(0.0, float(getattr(cand, "kcal_100g", 0.0) or 0.0)) / 100.0
        l_bound = max(0.0, float(_cand_field(cand, "portion_min_g", "min_g", 0.0)))
        is_missing = getattr(cand, "is_missing", False)
        max_g = float(_cand_field(cand, "portion_max_g", "max_g", 250.0))
        avail = float(_cand_field(cand, "available_stock_g", "available_g", max_g))
        if is_missing:
            u_bound = max(l_bound, max_g)
        else:
            u_bound = min(max_g, avail)
            if u_bound < l_bound:
                l_bound = u_bound
        cost = compute_ingredient_cost(cand)
        items.append({
            "idx": idx, "cand": cand, "l": l_bound, "u": u_bound, "e": e,
            "cost": cost, "avail": float("inf") if is_missing else avail,
            "eff": (cost / e) if e > 0.001 else 999999.0,
        })
    return items

def _build_solver_result(
    candidates: Sequence[Any], raw_quantities: Sequence[float],
    solver_name: str, message: str, iterations: int = 0,
) -> SolverResult:
    quantities, quantities_g, quantities_by_idx = {}, {}, []
    tot_kcal = tot_p = tot_c = tot_f = tot_fib = 0.0
    for i, cand in enumerate(candidates):
        q = round(float(raw_quantities[i]), 1)
        quantities[getattr(cand, "name", f"Ingredient_{i}")] = q
        quantities_g[getattr(cand, "product_id", i)] = q
        quantities_by_idx.append(q)
        if hasattr(cand, "resolved_quantity_g"):
            cand.resolved_quantity_g = q
        factor = q / 100.0
        tot_kcal += getattr(cand, "kcal_100g", 0.0) * factor
        tot_p += getattr(cand, "protein_100g", 0.0) * factor
        tot_c += getattr(cand, "carbs_100g", 0.0) * factor
        tot_f += getattr(cand, "fat_100g", 0.0) * factor
        tot_fib += getattr(cand, "fiber_100g", 0.0) * factor
    return SolverResult(
        success=True, quantities=quantities, total_kcal=round(tot_kcal, 1),
        total_protein=round(tot_p, 1), total_carbs=round(tot_c, 1),
        total_fat=round(tot_f, 1), total_fiber=round(tot_fib, 1),
        quantities_g=quantities_g, quantities_by_idx=quantities_by_idx,
        solver_name=solver_name, message=message, iterations=iterations,
    )

def solve_ingredient_quantities(
    candidates: Sequence[Any], target_kcal: float,
    macro_bounds: Optional[Dict[str, Tuple[float, float]]] = None,
    tolerance: float = 0.05, force_fallback: bool = False,
) -> SolverResult:
    if not candidates:
        return SolverResult(success=False, message="No ingredient candidates provided.")
    try:
        tk = float(target_kcal)
    except (TypeError, ValueError):
        return SolverResult(success=False, message="Target calories must be positive.")
    if isinstance(target_kcal, bool) or not math.isfinite(tk) or tk <= 0:
        return SolverResult(success=False, message="Target calories must be positive.")
    if HAS_SCIPY and not force_fallback:
        if (res := _solve_highs(candidates, tk, macro_bounds, tolerance)).success:
            return res
        logger.info("HiGHS solver did not converge (%s); invoking fallback knapsack.", res.message)
    return _solve_knapsack_fallback(candidates, tk, macro_bounds, tolerance)

def _solve_highs(
    candidates: Sequence[Any], target_kcal: float,
    macro_bounds: Optional[Dict[str, Tuple[float, float]]], tolerance: float,
) -> SolverResult:
    items = _prepare_solver_items(candidates)
    lambda_slack, max_slack = 20.0, tolerance * target_kcal
    c_full = [it["cost"] for it in items] + [lambda_slack, lambda_slack]
    A_eq = [[it["e"] for it in items] + [-1.0, 1.0]]
    bounds_full = [(it["l"], it["u"]) for it in items] + [(0.0, max_slack), (0.0, max_slack)]
    A_ub: List[List[float]] = []
    b_ub: List[float] = []
    if macro_bounds:
        for m_name, (m_min, m_max) in macro_bounds.items():
            key = f"{m_name.lower()}_100g"
            m_per_g = [max(0.0, float(getattr(cand, key, 0.0) or 0.0)) / 100.0 for cand in candidates] + [0.0, 0.0]
            if m_max is not None and m_max < 999999:
                A_ub.append(m_per_g)
                b_ub.append(float(m_max))
            if m_min is not None and m_min > 0:
                A_ub.append([-v for v in m_per_g])
                b_ub.append(-float(m_min))
    try:
        opt_res = scipy.optimize.linprog(
            c=c_full, A_ub=A_ub or None, b_ub=b_ub or None,
            A_eq=A_eq, b_eq=[target_kcal], bounds=bounds_full, method="highs",
        )
    except Exception as exc:
        return SolverResult(success=False, message=f"HiGHS exception: {exc}")
    if not opt_res.success:
        return SolverResult(success=False, message=opt_res.message)
    return _build_solver_result(candidates, opt_res.x[:len(items)], "highs", "Optimal solution found by HiGHS simplex.", getattr(opt_res, "nit", 0))

def _solve_knapsack_fallback(
    candidates: Sequence[Any], target_kcal: float,
    macro_bounds: Optional[Dict[str, Tuple[float, float]]], tolerance: float,
) -> SolverResult:
    items = _prepare_solver_items(candidates)
    k_min, k_max = (1.0 - tolerance) * target_kcal, (1.0 + tolerance) * target_kcal
    q = {it["idx"]: it["l"] for it in items}
    curr_kcal = sum(q[it["idx"]] * it["e"] for it in items)
    message = "Optimal (knapsack fallback)"

    if curr_kcal > k_max:
        scale = target_kcal / curr_kcal if curr_kcal > 0 else 1.0
        for it in items:
            q[it["idx"]] = round(it["l"] * scale, 1)
        message = "Portions scaled down proportionally (minimum portions exceeded caloric target)."
    elif (max_kcal := sum(it["u"] * it["e"] for it in items)) < k_min:
        for it in items:
            q[it["idx"]] = it["u"]
        deficit = target_kcal - max_kcal
        for it in sorted((it for it in items if it["u"] > 0.0 and it["avail"] > q[it["idx"]] and it["e"] > 0.001), key=lambda x: x["eff"]):
            headroom_g = it["avail"] - q[it["idx"]]
            if deficit <= headroom_g * it["e"]:
                q[it["idx"]] += deficit / it["e"]
                deficit = 0.0
                break
            q[it["idx"]] += headroom_g
            deficit -= headroom_g * it["e"]
        if deficit > 0 and (tot_curr := sum(q[it["idx"]] * it["e"] for it in items)) > 0:
            scale = target_kcal / tot_curr
            for it in items:
                q[it["idx"]] = round(q[it["idx"]] * scale, 1)
        message = "Portions scaled up proportionally (maximum portions below caloric target)."
    elif (deficit := target_kcal - curr_kcal) > 0:
        for it in sorted((it for it in items if it["e"] > 0.001), key=lambda x: x["eff"]):
            if (cap_g := it["u"] - q[it["idx"]]) <= 0:
                continue
            if deficit <= cap_g * it["e"]:
                q[it["idx"]] += deficit / it["e"]
                break
            q[it["idx"]] = it["u"]
            deficit -= cap_g * it["e"]

    return _build_solver_result(candidates, [q[it["idx"]] for it in items], "knapsack_fallback", message, len(items))

# --- 2. Name Normalization, Titling & Cooking Instructions ---

CLEAN_NAME_PATTERNS: List[Tuple[str, str]] = [
    ("spaghetti", "Spaghetti"), ("penne", "Penne Rigate"), ("carnaroli", "Rice"), ("riso", "Rice"),
    ("rice", "Rice"), ("farro", "Farro"), ("pollo", "Chicken Breast"), ("chicken", "Chicken Breast"),
    ("puree", "Tomato Sauce"), ("passata", "Tomato Sauce"), ("pomodoro", "Tomato Sauce"), ("tomato", "Tomato Sauce"),
    ("parmigiano", "Parmigiano"), ("parmesan", "Parmigiano"), ("grana", "Parmigiano"),
    ("olio", "Extra Virgin Olive Oil"), ("oil", "Extra Virgin Olive Oil"),
    ("latte", "Milk"), ("milk", "Milk"), ("yogurt", "Yogurt"), ("mela", "Apples"), ("apple", "Apples"),
    ("banana", "Bananas"), ("biscotti", "Biscuits"), ("salmone", "Salmon"), ("salmon", "Salmon"),
    ("tonno", "Tuna"), ("tuna", "Tuna"), ("uov", "Eggs"), ("egg", "Eggs"), ("zucchine", "Zucchini"),
    ("zucchini", "Zucchini"), ("funghi", "Mushrooms"), ("mushrooms", "Mushrooms"), ("broccoli", "Broccoli"),
    ("patate", "Potatoes"), ("potatoes", "Potatoes"), ("farina", "Flour"), ("flour", "Flour"),
    ("avena", "Oats"), ("oats", "Oats"),
]

def clean_display_name(raw_name: Any) -> str:
    if not raw_name or not isinstance(raw_name, str):
        return ""
    clean = raw_name.strip().lower()
    return next((rep for pat, rep in CLEAN_NAME_PATTERNS if pat in clean), raw_name.strip().title())

def _group_by_role(ingredients: Sequence[IngredientCandidate]) -> Tuple[Dict[Role, List[str]], Dict[Role, List[str]]]:
    by_role: Dict[Role, List[str]] = {}
    raw_by_role: Dict[Role, List[str]] = {}
    for ing in ingredients:
        r = ing.role if isinstance(ing.role, Role) else Role(ing.role)
        by_role.setdefault(r, []).append(clean_display_name(ing.name))
        raw_by_role.setdefault(r, []).append((ing.name or "").strip().lower())
    return by_role, raw_by_role

def synthesize_recipe_title(
    archetype: Archetype, ingredients: Sequence[IngredientCandidate], conn: Optional[sqlite3.Connection] = None,
) -> str:
    by_role, _ = _group_by_role(ingredients)
    arch_name = (archetype.name or "").lower()
    if "grain" in arch_name or "pasta" in arch_name:
        carb = by_role.get(Role.CARB_BASE, ["Pasta"])[0]
        sauce = (by_role.get(Role.VEG_SAUCE, []) + by_role.get(Role.VEGETABLE, []) or [None])[0]
        accent = (by_role.get(Role.PROTEIN_ACCENT) or by_role.get(Role.PROTEIN_MAIN) or [None])[0]
        aroma = (by_role.get(Role.AROMA_FINISHING) or by_role.get(Role.AROMA_TASTE) or [None])[0]
        if sauce and accent and accent != "Parmigiano":
            title = f"{carb} with {sauce} and {accent}"
        elif sauce and (accent == "Parmigiano" or aroma == "Parmigiano"):
            title = f"{carb} with {sauce} and Parmigiano"
        elif sauce:
            title = f"{carb} with {sauce}"
        elif accent:
            title = f"{carb} with {accent}"
        elif aroma == "Parmigiano":
            title = f"{carb} with Olive Oil and Parmigiano"
        else:
            title = f"Classic {carb} Dish"
    elif "main" in arch_name or "plate" in arch_name:
        prot = (by_role.get(Role.PROTEIN_MAIN) or ["Main Dish"])[0]
        side = (by_role.get(Role.VEG_SIDE) or by_role.get(Role.VEGETABLE) or [None])[0]
        carb_side = (by_role.get(Role.CARB_SIDE) or [None])[0]
        title = f"{prot} with {side} and {carb_side}" if (side and carb_side) else (f"{prot} with {side}" if side else f"Seared {prot} Plate")
    elif "bowl" in arch_name and "breakfast" not in arch_name:
        carb = by_role.get(Role.CARB_BASE, ["Grains"])[0]
        prot, veg = (by_role.get(Role.PROTEIN_MAIN) or [None])[0], (by_role.get(Role.VEGETABLE) or [None])[0]
        title = f"{carb} Bowl with {prot} and {veg}" if (prot and veg) else (f"{carb} and {prot} Bowl" if prot else f"{carb} Power Bowl")
    elif "breakfast" in arch_name or "snack" in arch_name:
        dairy, cereal, fruit = by_role.get(Role.DAIRY_BASE, ["Yogurt"])[0], by_role.get(Role.CEREAL_CARB, ["Grains"])[0], (by_role.get(Role.FRUIT) or [None])[0]
        if cereal == "Oats":
            title = f"Oat Porridge with {fruit or 'Fruit'} and {dairy}"
        elif cereal == "Biscuits":
            title = f"{dairy} Bowl with Biscuits" + (f" and {fruit}" if fruit else "")
        else:
            title = f"{dairy} Bowl with {cereal} and {fruit}" if fruit else f"{dairy} and {cereal} Bowl"
    else:
        title = f"{archetype.name} Recipe"

    if conn is not None:
        try:
            base_title, counter, cur = title, 2, conn.cursor()
            while cur.execute("SELECT 1 FROM RECIPES WHERE Name = ? COLLATE NOCASE", (title,)).fetchone():
                title = f"{base_title} (Variant {counter})"
                counter += 1
        except Exception:
            pass
    return title

def synthesize_cooking_instructions(archetype: Archetype, ingredients: Sequence[IngredientCandidate]) -> List[str]:
    by_role, raw_by_role = _group_by_role(ingredients)
    carb = (by_role.get(Role.CARB_BASE) or by_role.get(Role.CEREAL_CARB) or by_role.get(Role.CARB_SIDE) or [None])[0]
    prot = (by_role.get(Role.PROTEIN_MAIN) or by_role.get(Role.PROTEIN_ACCENT) or [None])[0]
    veg = (by_role.get(Role.VEGETABLE) or by_role.get(Role.VEG_SAUCE) or by_role.get(Role.VEG_SIDE) or [None])[0]
    raw_veg = (raw_by_role.get(Role.VEGETABLE) or raw_by_role.get(Role.VEG_SAUCE) or raw_by_role.get(Role.VEG_SIDE) or [""])[0]
    is_liquid_sauce = bool(by_role.get(Role.VEG_SAUCE)) or any(k in raw_veg for k in ("puree", "passata", "salsa", "sauce", "pelati"))
    fat = (by_role.get(Role.FAT_SEASONING) or ["a drizzle of extra virgin olive oil"])[0]
    aroma = (by_role.get(Role.AROMA_FINISHING) or by_role.get(Role.AROMA_TASTE) or by_role.get(Role.AROMA_HERB) or [None])[0]
    fruit, dairy, arch_name = (by_role.get(Role.FRUIT) or [None])[0], (by_role.get(Role.DAIRY_BASE) or ["the dairy base"])[0], (archetype.name or "").lower()

    prep = []
    if fruit:
        prep.append(f"Wash and slice {fruit} into bite-sized pieces.")
    if veg:
        prep.append(f"Prepare the {veg} sauce and measure out the required portion." if is_liquid_sauce else f"Wash and trim {veg}, cutting into even pieces.")
    if prot:
        prep.append(f"Prepare {prot} by cutting into bite-sized pieces or portioning carefully.")
    if carb and "breakfast" not in arch_name:
        prep.append(f"Weigh out {carb} and measure the seasonings accurately.")
    step1 = " ".join(prep) if prep else "Arrange and measure all ingredients on the work surface."

    cook = []
    if "pasta" in arch_name or "grain" in arch_name:
        cook.append(f"Bring a pot of salted water to a boil and cook {carb} until al dente.")
        if veg and prot:
            cook.append(f"Meanwhile, heat {fat} in a pan and sauté {prot} with {veg} over medium heat for 8-10 minutes.")
        elif veg:
            cook.append(f"Meanwhile, warm {veg} in a pan with {fat} over medium heat for about 8 minutes.")
        elif prot:
            cook.append(f"Meanwhile, sear {prot} in a pan with {fat} for 5-7 minutes until golden.")
        step3 = f"Drain {carb} al dente, reserving a splash of cooking water. Toss in the pan with the sauce and {fat} over high heat to bind the flavors."
    elif "breakfast" in arch_name:
        cook.append(f"Pour {dairy} into a serving bowl and fold in {carb}.")
        step3 = f"Top with {fruit or 'the fruit'} and stir gently to combine."
    else:
        if prot:
            cook.append(f"In a hot skillet or grill pan, cook {prot} with {fat}, turning halfway through for an even sear.")
        if veg:
            cook.append(f"Separately, simmer or sauté {veg} with a pinch of salt for 8-10 minutes.")
        step3 = f"Plate {prot or 'the main course'} alongside {veg or 'the side'} and drizzle with {fat}."

    if aroma:
        step4 = f"Finish the dish by sprinkling with {aroma} and serve hot immediately."
    elif "breakfast" in arch_name:
        step4 = "Serve chilled and enjoy immediately."
    else:
        step4 = "Season with salt and pepper to taste and serve hot."
    return [step1, " ".join(cook), step3, step4]

# --- 3. Database Candidate Extraction & Default Target ---

def _row_to_candidate(row: Tuple[Any, ...], from_pantry: bool) -> IngredientCandidate:
    pid, name, unit, kcal, prot, carb, fat, fib, sug, den, gpp = row[:11]
    all_roles = classify_food(name, kcal, prot, carb, fat, fib or 0.0, sug or 0.0)
    primary_role = all_roles[0] if all_roles else Role.AROMA_TASTE
    common = dict(
        product_id=pid, role=primary_role, unit=unit, kcal_100g=float(kcal or 0.0),
        protein_100g=float(prot or 0.0), carbs_100g=float(carb or 0.0), fat_100g=float(fat or 0.0),
        fiber_100g=float(fib or 0.0), sugars_100g=float(sug or 0.0),
        grams_per_piece=gpp, density_kg_per_l=den, all_eligible_roles=all_roles,
    )
    if from_pantry:
        avail_net, open_rem, has_open, exp = row[11:15]
        scale = {"kg": 1000.0, "L": 1000.0 * (den or 1.0), "pcs": gpp or 50.0}.get(unit, 1.0)
        days_to_exp = None
        if exp != "9999-12-31":
            try:
                days_to_exp = max(0.0, float((date.fromisoformat(exp) - date.today()).days))
            except Exception:
                days_to_exp = None
        return IngredientCandidate(
            name=name or f"Product #{pid}", available_stock_g=float(avail_net * scale),
            open_remainder=float(open_rem or 0.0), is_open=bool(has_open),
            days_to_expiration=days_to_exp, is_missing=False, **common,
        )
    return IngredientCandidate(
        name=name or f"Catalog #{pid}", available_stock_g=9999.0,
        open_remainder=0.0, is_open=False, days_to_expiration=None,
        is_missing=True, cost_coefficient=5.0, **common,
    )

def extract_pantry_candidates(conn: sqlite3.Connection) -> List[IngredientCandidate]:
    query = """
    SELECT i.ID, (SELECT a.Name FROM ALIASES a WHERE a.ID = i.ID ORDER BY a.rowid ASC LIMIT 1) AS Name,
        p.Unit, p.Kcal_100g, p.Protein_100g, p.Carbs_100g, p.Fat_100g,
        p.Fiber_100g, p.Sugars_100g, p.Density_kg_per_L, p.Grams_per_piece,
        SUM((i.Intact_Items * i.Unit_Weight) + i.Open_Remainder) AS Available_Net,
        SUM(i.Open_Remainder) AS Total_Open_Remainder, MAX(i.Open_Remainder > 0) AS Has_Open,
        MIN(CASE WHEN i.Expiration_Date IS NOT NULL THEN i.Expiration_Date ELSE '9999-12-31' END) AS Earliest_Expiration
    FROM INVENTORY i JOIN PRODUCTS p ON i.ID = p.ID
    WHERE ((i.Intact_Items * i.Unit_Weight) + i.Open_Remainder) > 0
      AND (i.Expiration_Date IS NULL OR i.Expiration_Date > ?)
      AND NOT EXISTS (SELECT 1 FROM EXCLUSION_LIST e WHERE e.ID = i.ID AND e.Location = i.Location)
    GROUP BY i.ID ORDER BY Has_Open DESC, Earliest_Expiration ASC;
    """
    return [_row_to_candidate(r, True) for r in conn.cursor().execute(query, (date.today().isoformat(),)).fetchall()]

def extract_catalog_candidates(conn: sqlite3.Connection) -> List[IngredientCandidate]:
    query = """
    SELECT p.ID, (SELECT a.Name FROM ALIASES a WHERE a.ID = p.ID ORDER BY a.rowid ASC LIMIT 1) AS Name,
        p.Unit, p.Kcal_100g, p.Protein_100g, p.Carbs_100g, p.Fat_100g,
        p.Fiber_100g, p.Sugars_100g, p.Density_kg_per_L, p.Grams_per_piece
    FROM PRODUCTS p ORDER BY p.ID ASC;
    """
    return [_row_to_candidate(r, False) for r in conn.cursor().execute(query).fetchall()]

def get_default_target_kcal(conn: sqlite3.Connection, meal_type: str = "lunch") -> float:
    clean_meal = (meal_type or "lunch").strip().lower()
    fallback_defaults = {"breakfast": 450.0, "lunch": 700.0, "dinner": 650.0}
    try:
        cur = conn.cursor()
        if (prof_row := cur.execute("SELECT Kcal_target FROM PROFILE WHERE ID = 1").fetchone()) and prof_row[0] and prof_row[0] > 0:
            meal_row = cur.execute("SELECT Energy_Share_pct FROM MEALS WHERE Name = ? COLLATE NOCASE", (clean_meal,)).fetchone()
            share = float(meal_row[0]) if meal_row else (40.0 if clean_meal == "lunch" else 35.0)
            return round(float(prof_row[0]) * (share / 100.0), 0)
    except Exception:
        pass
    return fallback_defaults.get(clean_meal, 700.0)

# --- 4. Pipeline Orchestrator ---

def _pantry_priority(items: Sequence[IngredientCandidate]) -> float:
    return sum(
        (10.0 if it.is_open else (5.0 / max(1.0, it.days_to_expiration) if (it.days_to_expiration is not None and it.days_to_expiration > 0) else 0.0))
        + (-5.0 if it.is_missing else 0.0)
        for it in items
    )

def _has_clash(a: IngredientCandidate, b: IngredientCandidate) -> bool:
    return are_mutually_exclusive(a.name, set(a.all_eligible_roles), b.name, set(b.all_eligible_roles))

class _ClassOrInstanceMethod:
    def __init__(self, func: Any):
        self.func = func
        functools.update_wrapper(self, func)

    def __get__(self, obj: Any, objtype: Any = None) -> Any:
        return functools.partial(self.func, objtype if obj is None else obj)


class RecipeGenerator:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or DB_PATH

    @classmethod
    def generate(
        cls, target_kcal: Optional[float] = None, meal_type: str = "lunch",
        archetype_name: Optional[str] = None, pantry_only: bool = True,
        db_path: Optional[str] = None, macro_bounds: Optional[Dict[str, Tuple[float, float]]] = None,
        variation_index: int = 0,
    ) -> RecipeCandidate:
        return cls(db_path=db_path).generate_recipe(target_kcal, meal_type, archetype_name, pantry_only, db_path, macro_bounds, variation_index)

    @_ClassOrInstanceMethod
    def generate_recipe(
        self_or_cls, target_kcal: Optional[float] = None, meal_type: str = "lunch",
        archetype_name: Optional[str] = None, pantry_only: bool = True,
        db_path: Optional[str] = None, macro_bounds: Optional[Dict[str, Tuple[float, float]]] = None,
        variation_index: int = 0,
    ) -> RecipeCandidate:
        if isinstance(self_or_cls, type):
            return self_or_cls(db_path=db_path).generate_recipe(target_kcal, meal_type, archetype_name, pantry_only, db_path, macro_bounds, variation_index)
        self = self_or_cls
        conn = get_connection(db_path or self.db_path)
        try:
            clean_meal = (meal_type or "lunch").strip().lower()
            archetype = get_archetype(archetype_name) if archetype_name else get_default_archetype_for_meal(clean_meal)
            final_target_kcal = get_default_target_kcal(conn, clean_meal) if (target_kcal is None or not math.isfinite(float(target_kcal)) or float(target_kcal) <= 0) else float(target_kcal)
            pantry_candidates = extract_pantry_candidates(conn)
            pantry_ids = {c.product_id for c in pantry_candidates}
            catalog_candidates = [c for c in extract_catalog_candidates(conn) if c.product_id not in pantry_ids] if not pantry_only else []
            slot_pools = self._build_slot_pools(archetype, pantry_candidates, catalog_candidates, pantry_only)
            if not (candidate_combos := self._generate_candidate_combinations(archetype, slot_pools, max_combos=40)):
                raise ValueError(f"Could not generate any compatible ingredient combinations for archetype '{archetype.name}'. Verify pantry items or set pantry_only=False.")
            if variation_index and len(candidate_combos) > 1:
                offset = int(variation_index) % len(candidate_combos)
                candidate_combos = candidate_combos[offset:] + candidate_combos[:offset]

            winning_combo, winning_result, warnings = None, None, []
            for combo in candidate_combos:
                cloned = [dataclasses.replace(c) for c in combo]
                res = solve_ingredient_quantities(cloned, final_target_kcal, macro_bounds=macro_bounds, tolerance=0.05)
                if res.success:
                    if abs(res.total_kcal - final_target_kcal) / final_target_kcal <= 0.05:
                        winning_combo, winning_result = cloned, res
                        break
                    if winning_combo is None:
                        winning_combo, winning_result = cloned, res
            if winning_combo is None or winning_result is None or not winning_result.success:
                winning_combo = [dataclasses.replace(c) for c in candidate_combos[0]]
                winning_result = solve_ingredient_quantities(winning_combo, final_target_kcal, macro_bounds=macro_bounds, force_fallback=True)
                warnings.append("Optimal solution within ±5% caloric tolerance was not found by LP; applied knapsack fallback.")

            for idx, cand in enumerate(winning_combo):
                cand.resolved_quantity_g = (
                    winning_result.quantities_by_idx[idx]
                    if winning_result.quantities_by_idx and idx < len(winning_result.quantities_by_idx)
                    else winning_result.quantities_g.get(cand.product_id, 0.0)
                )
            title = synthesize_recipe_title(archetype, winning_combo, conn=conn)
            instructions = synthesize_cooking_instructions(archetype, winning_combo)
            recipe = RecipeCandidate(
                title=title, archetype_name=archetype.name, meal_type=clean_meal,
                ingredients=winning_combo, instructions=instructions,
                notes="\n".join(f"{i + 1}. {step}" for i, step in enumerate(instructions)),
                target_kcal=final_target_kcal, total_kcal=winning_result.total_kcal,
                total_protein=winning_result.total_protein, total_carbs=winning_result.total_carbs,
                total_fat=winning_result.total_fat, total_fiber=winning_result.total_fiber,
                pantry_only=pantry_only, solver_status=winning_result.solver_name, warnings=warnings,
            )
            recipe.sync_macros_with_display_quantities()
            return recipe
        finally:
            conn.close()

    def _build_slot_pools(
        self, archetype: Archetype, pantry_candidates: List[IngredientCandidate],
        catalog_candidates: List[IngredientCandidate], pantry_only: bool,
    ) -> Dict[Slot, List[IngredientCandidate]]:
        slot_pools: Dict[Slot, List[IngredientCandidate]] = {}
        for slot in archetype.slots:
            matched = [
                clone for c in pantry_candidates if any(slot.accepts_role(r) for r in c.all_eligible_roles)
                and ((clone := self._specialize_candidate_for_slot(c, slot)).available_stock_g >= clone.portion_min_g or not pantry_only)
            ]
            if not matched and not pantry_only:
                matched = [self._specialize_candidate_for_slot(c, slot) for c in catalog_candidates if any(slot.accepts_role(r) for r in c.all_eligible_roles)]
            if not matched and not slot.is_optional:
                if pantry_only:
                    raise ValueError(f"Pantry does not contain eligible items for required slot '{slot.name}' ({slot.role.value}). Set pantry_only=False to allow catalog suggestions.")
                raise ValueError(f"No products in catalog or pantry can satisfy required slot '{slot.name}' ({slot.role.value}).")
            slot_pools[slot] = matched
        return slot_pools

    def _specialize_candidate_for_slot(self, cand: IngredientCandidate, slot: Slot) -> IngredientCandidate:
        spec_min, spec_max = get_portion_bounds(slot.role, cand.name)
        p_min, p_max = max(slot.portion_min_g, spec_min), min(slot.portion_max_g, spec_max)
        if p_min > p_max:
            p_min, p_max = slot.portion_min_g, slot.portion_max_g
        return dataclasses.replace(
            cand, role=slot.role, to_taste=(slot.role in (Role.AROMA_TASTE, Role.AROMA_HERB, Role.AROMA_FINISHING)),
            portion_min_g=p_min, portion_max_g=p_max,
            cost_coefficient=0.0, resolved_quantity_g=0.0, pairing_score=0.5,
            all_eligible_roles=list(cand.all_eligible_roles),
        )

    def _generate_candidate_combinations(
        self, archetype: Archetype, slot_pools: Dict[Slot, List[IngredientCandidate]], max_combos: int = 40,
    ) -> List[List[IngredientCandidate]]:
        req_pools = [slot_pools.get(s, [])[:12] for s in archetype.slots if not s.is_optional]
        if any(len(p) == 0 for p in req_pools):
            return []
        optional_slots = [s for s in archetype.slots if s.is_optional]
        scored: List[Tuple[float, List[IngredientCandidate]]] = []
        for req_sel in itertools.product(*req_pools):
            ids, canons = {it.product_id for it in req_sel}, {resolve_canonical(it.name) for it in req_sel if it.name}
            if len(ids) != len(req_sel) or len(canons) != len(req_sel):
                continue
            if any(_has_clash(a, b) for a, b in itertools.combinations(req_sel, 2)):
                continue
            combo = list(req_sel)
            for opt_slot in optional_slots:
                best_item, best_harmony = None, -1.0
                for opt_item in slot_pools.get(opt_slot, [])[:12]:
                    opt_canon = resolve_canonical(opt_item.name)
                    if opt_item.product_id in ids or (opt_canon and opt_canon in canons) or (opt_slot.role == Role.CARB_SIDE and opt_canon == "pasta"):
                        continue
                    if any(_has_clash(opt_item, ex) for ex in combo):
                        continue
                    if (harmony := score_combination([x.name for x in combo + [opt_item]])) > best_harmony:
                        best_harmony, best_item = harmony, opt_item
                if best_item is not None and best_harmony >= 0.50:
                    combo.append(best_item)
                    ids.add(best_item.product_id)
                    if best_canon := resolve_canonical(best_item.name):
                        canons.add(best_canon)
            if any(_has_clash(a, b) for a, b in itertools.combinations(combo, 2)) or (harmony_score := score_combination([x.name for x in combo])) <= 0.0:
                continue
            scored.append(((2.0 * harmony_score) + (_pantry_priority(combo) / len(combo)), combo))
            if len(combo) > len(req_sel) and (req_harmony := score_combination([x.name for x in req_sel])) > 0.0:
                scored.append(((2.0 * req_harmony) + (_pantry_priority(req_sel) / len(req_sel)) - 20.0, list(req_sel)))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [combo for _, combo in scored[:max_combos]]

__all__ = [
    "Role", "ROLE_COMPATIBILITY", "are_roles_compatible", "is_role_compatible",
    "classify_food", "fingerprint_macros", "get_portion_bounds", "DEFAULT_PORTION_BOUNDS",
    "Slot", "SlotsDict", "Archetype", "ARCHETYPES", "ARCHETYPE_ALIASES", "MEAL_ARCHETYPES",
    "GRAIN_PASTA_DISH", "MAIN_AND_SIDE_PLATE", "ONE_POT_BALANCED_BOWL", "BREAKFAST_SNACK_BOWL",
    "get_archetype", "get_archetypes_for_meal", "get_default_archetype_for_meal",
    "get_slot_for_role", "clamp_ingredient_portion", "validate_slot_filling", "SlotValidationResult",
    "IngredientCandidate", "SolverResult", "RecipeCandidate",
    "CulinaryTag", "resolve_canonical", "are_mutually_exclusive", "get_pairing_score", "score_combination",
    "compute_ingredient_cost", "solve_ingredient_quantities",
    "RecipeGenerator", "clean_display_name", "extract_pantry_candidates", "extract_catalog_candidates",
    "synthesize_recipe_title", "synthesize_cooking_instructions", "get_default_target_kcal",
]
