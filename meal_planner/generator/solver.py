"""meal_planner.generator.solver

Mathematical optimization solver for recipe ingredient portion weights:
1. SciPy HiGHS Simplex Linear Programming (method='highs') with pantry FIFO prioritization.
2. Pure-Python continuous knapsack fallback for resilience when scipy is absent or constraints over-bounded.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

try:
    import scipy.optimize
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False

from meal_planner.generator.taxonomy import Role


def compute_ingredient_cost(
    cand: Any,
    pairing_bonus_weight: float = 2.0,
    open_bonus: float = -10.0,
    fifo_factor: float = -5.0,
    missing_penalty: float = 5.0,
) -> float:
    """Computes the linear programming objective cost coefficient for an ingredient candidate.

    Cost factors:
    1. Opened package bonus (Open_Remainder > 0 or is_open): -10.0
    2. FIFO expiration bonus: -5.0 / days_to_expiry
    3. Pairing harmony bonus: -2.0 * pairing_score
    4. Missing item penalty: +5.0
    """
    # 1. Open package bonus
    is_open = getattr(cand, "is_open", False) or getattr(cand, "open_remainder", 0.0) > 0
    c_open = open_bonus if is_open else 0.0

    # 2. FIFO bonus
    days = getattr(cand, "days_to_expiration", None)
    if days is None:
        days = getattr(cand, "days_to_expiry", None)
    c_fifo = 0.0
    if days is not None:
        c_fifo = fifo_factor / max(1.0, float(days))

    # 3. Pairing affinity bonus
    pairing = getattr(cand, "pairing_score", 0.5)
    c_pair = -pairing_bonus_weight * max(0.0, min(1.0, float(pairing if pairing is not None else 0.5)))

    # 4. Missing item penalty
    is_missing = getattr(cand, "is_missing", False)
    c_miss = missing_penalty if is_missing else 0.0

    return c_open + c_fifo + c_pair + c_miss


def solve_ingredient_quantities(
    candidates: Sequence[Any],
    target_kcal: float,
    macro_bounds: Optional[Dict[str, Tuple[float, float]]] = None,
    tolerance: float = 0.05,
    force_fallback: bool = False,
) -> Any:
    """Optimizes exact ingredient portion weights in grams.

    Args:
        candidates: Sequence of IngredientCandidate objects.
        target_kcal: Target recipe calories in kcal.
        macro_bounds: Optional dict of macro name to (min_grams, max_grams).
        tolerance: Allowed caloric deviation (default 0.05 = +/- 5%).
        force_fallback: If True, bypasses SciPy and runs pure-Python fallback.

    Returns:
        SolverResult containing solved quantities, macro totals, and status.
    """
    from meal_planner.generator import SolverResult

    if not candidates:
        return SolverResult(success=False, message="No ingredient candidates provided.")

    if target_kcal <= 0:
        return SolverResult(success=False, message="Target calories must be positive.")

    # Try SciPy HiGHS first unless forced to fallback or unavailable
    if HAS_SCIPY and not force_fallback:
        res = _solve_highs(candidates, target_kcal, macro_bounds, tolerance)
        if res.success:
            return res
        logger.info("HiGHS solver did not converge (%s); invoking fallback knapsack.", res.message)

    return _solve_knapsack_fallback(candidates, target_kcal, macro_bounds, tolerance)


def _solve_highs(
    candidates: Sequence[Any],
    target_kcal: float,
    macro_bounds: Optional[Dict[str, Tuple[float, float]]],
    tolerance: float,
) -> Any:
    """Internal HiGHS LP goal-programming solver implementation."""
    from meal_planner.generator import SolverResult

    n = len(candidates)
    c_vec: List[float] = []
    bounds: List[Tuple[float, float]] = []
    kcal_per_g: List[float] = []

    for cand in candidates:
        e = max(0.0, float(getattr(cand, "kcal_100g", 0.0) or 0.0)) / 100.0
        kcal_per_g.append(e)

        cost = compute_ingredient_cost(cand)
        c_vec.append(cost)

        # Lower bound
        min_raw = getattr(cand, "min_g", None)
        if min_raw is None:
            min_raw = getattr(cand, "portion_min_g", None)
        min_g = float(min_raw) if min_raw is not None else 0.0
        l_bound = max(0.0, min_g)

        # Upper bound
        is_missing = getattr(cand, "is_missing", False)
        max_raw = getattr(cand, "max_g", None)
        if max_raw is None:
            max_raw = getattr(cand, "portion_max_g", None)
        max_g = float(max_raw) if max_raw is not None else 250.0

        if is_missing:
            u_bound = max(l_bound, max_g)
        else:
            avail_raw = getattr(cand, "available_g", None)
            if avail_raw is None:
                avail_raw = getattr(cand, "available_stock_g", None)
            avail = float(avail_raw) if avail_raw is not None else max_g
            u_bound = min(max_g, avail)
            if u_bound < l_bound:
                l_bound = u_bound  # Defensive relaxation for low stock

        bounds.append((l_bound, u_bound))

    # Goal programming with slack variables: x = [q_0, ..., q_n-1, s_plus, s_minus]
    # sum(q_i * e_i) - s_plus + s_minus = target_kcal
    lambda_slack = 20.0
    c_full = list(c_vec) + [lambda_slack, lambda_slack]

    A_eq = [kcal_per_g + [-1.0, 1.0]]
    b_eq = [target_kcal]

    max_slack = tolerance * target_kcal
    bounds_full = bounds + [(0.0, max_slack), (0.0, max_slack)]

    A_ub: List[List[float]] = []
    b_ub: List[float] = []

    # Optional macro constraints
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
            c=c_full,
            A_ub=A_ub if A_ub else None,
            b_ub=b_ub if b_ub else None,
            A_eq=A_eq,
            b_eq=b_eq,
            bounds=bounds_full,
            method="highs",
        )
    except Exception as exc:
        return SolverResult(success=False, message=f"HiGHS exception: {exc}")

    if not opt_res.success:
        return SolverResult(success=False, message=opt_res.message)

    raw_q = opt_res.x[:n]
    quantities: Dict[str, float] = {}
    quantities_g: Dict[int, float] = {}
    quantities_by_idx: List[float] = []
    tot_kcal = 0.0
    tot_p = 0.0
    tot_c = 0.0
    tot_f = 0.0
    tot_fib = 0.0

    for i, cand in enumerate(candidates):
        q = round(float(raw_q[i]), 1)
        cand_name = getattr(cand, "name", f"Ingredient_{i}")
        quantities[cand_name] = q
        pid = getattr(cand, "product_id", i)
        quantities_g[pid] = q
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
        success=True,
        quantities=quantities,
        total_kcal=round(tot_kcal, 1),
        total_protein=round(tot_p, 1),
        total_carbs=round(tot_c, 1),
        total_fat=round(tot_f, 1),
        total_fiber=round(tot_fib, 1),
        quantities_g=quantities_g,
        quantities_by_idx=quantities_by_idx,
        solver_name="highs",
        message="Optimal solution found by HiGHS simplex.",
        iterations=getattr(opt_res, "nit", 0),
    )


def _solve_knapsack_fallback(
    candidates: Sequence[Any],
    target_kcal: float,
    macro_bounds: Optional[Dict[str, Tuple[float, float]]],
    tolerance: float,
) -> Any:
    """Internal bounded continuous knapsack solver with proportional scaling."""
    from meal_planner.generator import SolverResult

    items: List[Dict[str, Any]] = []
    for idx, cand in enumerate(candidates):
        e = max(0.0, float(getattr(cand, "kcal_100g", 0.0) or 0.0)) / 100.0
        min_raw = getattr(cand, "min_g", None)
        if min_raw is None:
            min_raw = getattr(cand, "portion_min_g", None)
        min_g = float(min_raw) if min_raw is not None else 0.0
        l_bound = max(0.0, min_g)

        is_missing = getattr(cand, "is_missing", False)
        max_raw = getattr(cand, "max_g", None)
        if max_raw is None:
            max_raw = getattr(cand, "portion_max_g", None)
        max_g = float(max_raw) if max_raw is not None else 250.0

        avail_raw = getattr(cand, "available_g", None)
        if avail_raw is None:
            avail_raw = getattr(cand, "available_stock_g", None)
        avail = float(avail_raw) if avail_raw is not None else max_g

        if is_missing:
            u_bound = max(l_bound, max_g)
        else:
            u_bound = min(max_g, avail)
            if u_bound < l_bound:
                l_bound = u_bound

        cost = compute_ingredient_cost(cand)
        items.append({
            "idx": idx,
            "cand": cand,
            "l": l_bound,
            "u": u_bound,
            "e": e,
            "avail": avail if not is_missing else float("inf"),
            "eff": (cost / e) if e > 0.001 else 999999.0,
        })

    k_min = (1.0 - tolerance) * target_kcal
    k_max = (1.0 + tolerance) * target_kcal

    # 1. Start all at lower bound
    q = {it["idx"]: it["l"] for it in items}
    curr_kcal = sum(q[it["idx"]] * it["e"] for it in items)

    message = "Optimal (knapsack fallback)"

    # Check Case A: Over-constrained high
    if curr_kcal > k_max:
        scale = target_kcal / curr_kcal if curr_kcal > 0 else 1.0
        for it in items:
            q[it["idx"]] = round(it["l"] * scale, 1)
        message = "Portions scaled down proportionally (minimum portions exceeded caloric target)."
    else:
        # Check Case B: Over-constrained low
        max_kcal = sum(it["u"] * it["e"] for it in items)
        if max_kcal < k_min:
            # First set to max
            for it in items:
                q[it["idx"]] = it["u"]
            curr_kcal = max_kcal
            deficit = target_kcal - curr_kcal

            # Expand items with remaining physical stock
            expandable = [it for it in items if it["u"] > 0.0 and it["avail"] > q[it["idx"]] and it["e"] > 0.001]
            if expandable:
                for it in sorted(expandable, key=lambda x: x["eff"]):
                    headroom_g = it["avail"] - q[it["idx"]]
                    headroom_k = headroom_g * it["e"]
                    if deficit <= headroom_k:
                        q[it["idx"]] += deficit / it["e"]
                        deficit = 0.0
                        break
                    else:
                        q[it["idx"]] += headroom_g
                        deficit -= headroom_k

            if deficit > 0:
                tot_curr = sum(q[it["idx"]] * it["e"] for it in items)
                if tot_curr > 0:
                    scale = target_kcal / tot_curr
                    for it in items:
                        q[it["idx"]] = round(q[it["idx"]] * scale, 1)
            message = "Portions scaled up proportionally (maximum portions below caloric target)."
        else:
            # Case C: Feasible knapsack range
            deficit = target_kcal - curr_kcal
            if deficit > 0:
                sorted_items = sorted([it for it in items if it["e"] > 0.001], key=lambda x: x["eff"])
                for it in sorted_items:
                    capacity_g = it["u"] - q[it["idx"]]
                    if capacity_g <= 0:
                        continue
                    capacity_k = capacity_g * it["e"]
                    if deficit <= capacity_k:
                        q[it["idx"]] += deficit / it["e"]
                        deficit = 0.0
                        break
                    else:
                        q[it["idx"]] = it["u"]
                        deficit -= capacity_k

    # Compile result
    quantities: Dict[str, float] = {}
    quantities_g: Dict[int, float] = {}
    quantities_by_idx: List[float] = []
    tot_kcal = 0.0
    tot_p = 0.0
    tot_c = 0.0
    tot_f = 0.0
    tot_fib = 0.0

    for it in items:
        idx = it["idx"]
        cand = it["cand"]
        qty = round(q[idx], 1)
        cand_name = getattr(cand, "name", f"Ingredient_{idx}")
        quantities[cand_name] = qty
        pid = getattr(cand, "product_id", idx)
        quantities_g[pid] = qty
        quantities_by_idx.append(qty)
        if hasattr(cand, "resolved_quantity_g"):
            cand.resolved_quantity_g = qty

        factor = qty / 100.0
        tot_kcal += getattr(cand, "kcal_100g", 0.0) * factor
        tot_p += getattr(cand, "protein_100g", 0.0) * factor
        tot_c += getattr(cand, "carbs_100g", 0.0) * factor
        tot_f += getattr(cand, "fat_100g", 0.0) * factor
        tot_fib += getattr(cand, "fiber_100g", 0.0) * factor

    return SolverResult(
        success=True,
        quantities=quantities,
        total_kcal=round(tot_kcal, 1),
        total_protein=round(tot_p, 1),
        total_carbs=round(tot_c, 1),
        total_fat=round(tot_f, 1),
        total_fiber=round(tot_fib, 1),
        quantities_g=quantities_g,
        quantities_by_idx=quantities_by_idx,
        solver_name="knapsack_fallback",
        message=message,
        iterations=len(items),
    )
