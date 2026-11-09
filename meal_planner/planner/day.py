"""Daily planner: one recipe per meal (limits, open packages, leftovers)."""

from datetime import date

from meal_planner import engine as m
from meal_planner.inventory import get_exclusion_list


OPEN_WEIGHT, NEW_WEIGHT, SHARE_WEIGHT, EXTRA_PENALTY = 3.0, 1.0, 5.0, 1.0


MAX_CANDIDATES = 12      # recipes evaluated per meal: the best ones on the single-recipe score


# A ready leftover: no ingredient to consume, counts as 100% open.
LEFTOVER_SIM = {"detail": [], "open_coverage": 1.0, "new_packages": 0, "missing": {},
              "feasible": True, "incomplete_data": False, "group_choices": {}}


try:
    from meal_planner.engine._speedups import score as _c_score
    def _score(coverage, new, share_deviation):
        return _c_score(float(coverage), float(new), float(share_deviation))
except (ImportError, AttributeError):
    def _score(coverage, new, share_deviation):
        return OPEN_WEIGHT * coverage - NEW_WEIGHT * new - SHARE_WEIGHT * share_deviation


def _deviation(check):
    """Sum of the deviations, each relative to the violated limit."""
    return sum(v["deviation"] / ((v["minimum"] if v["direction"] == "below" else v["maximum"]) or 1.0)
               for v in check["entries"] if not v["ok"])


def plan_day(conn, date_str, kcal=None, inv=None, fixed=None, planned_cooks=None, leftovers_used=None):
    """Plan for `date` (YYYY-MM-DD). `fixed` = {meal: Recipe_ID, or None to skip}.
    `inv` (virtual inventory) lets you chain several days; it is not modified.
    `planned_cooks` = [(Recipe_ID, date)] already planned in memory: they count for repetitions."""
    products, goals, profile = m.load_products(conn), m.load_goals(conn), m.load_profile(conn)
    if profile is None:
        profile = {}
    if kcal:
        profile["Kcal_target"] = kcal
    target = profile.get("Kcal_target")
    if not target:
        raise ValueError("Kcal target not defined: use --kcal or 'python -m meal_planner.engine profile ...'.")
    meals = conn.execute("SELECT Name, Energy_Share_pct FROM MEALS ORDER BY Sort_Order").fetchall()
    order = {name: i for i, (name, _) in enumerate(meals)}
    shares = {name: q / 100.0 for name, q in meals}
    recipe_names = dict(conn.execute("SELECT Recipe_ID, Name FROM RECIPES").fetchall())
    ing = {rid: m.recipe_ingredients(conn, rid) for rid in recipe_names}
    inv = (inv if inv is not None else m.VirtualInventory.from_db(conn)).copy()
    open_before = {i for i in inv.rows if inv.open_remainder(i) > 0}
    warnings = []

    # Build exclusion set: IDs of ingredients expired by this date
    excluded_ids = {r["ID"] for r in get_exclusion_list(target_date=date_str, conn=conn)}

    # Build expiry map: {product_id: nearest_days_to_expiry} for non-excluded items
    today_d = date.fromisoformat(date_str)
    _expiry_rows = conn.execute(
        "SELECT ID, MIN(Expiration_Date) FROM INVENTORY "
        "WHERE Expiration_Date IS NOT NULL GROUP BY ID"
    ).fetchall()
    _expiry_days = {}
    for pid, exp_str in _expiry_rows:
        if pid in excluded_ids:
            continue
        try:
            days = (date.fromisoformat(exp_str) - today_d).days
            if days > 0:
                _expiry_days[pid] = days
        except ValueError:
            pass

    _macro_cache = {}

    def _get_macros(rid, choices):
        if not rid:
            return None
        ckey = (rid, tuple(sorted(choices.items())) if choices else ())
        mac = _macro_cache.get(ckey)
        if mac is None:
            mac = m.recipe_macros(conn, rid, products, choices, ingredients=ing[rid])
            _macro_cache[ckey] = mac
        return dict(mac)

    def entry(meal, rid, sim, origin, cooking_id=None):
        mac = _get_macros(rid, sim["group_choices"]) if rid else None
        return {"meal": meal, "Recipe_ID": rid, "recipe": recipe_names.get(rid), "origin": origin,
                "sim": sim, "macro": mac, "Cooking_ID": cooking_id, "leftover": bool(cooking_id) and origin == "planned"}

    # --- fixed meals: already decided in the DB (not 'proposed') or requested by the caller ---
    fixed = fixed or {}
    for meal in fixed:
        if meal not in shares:
            raise ValueError(f"Unknown meal: {meal}")
    # a DB meal already cooked (or eating a leftover) does not consume the ingredients again
    in_db = {meal: (rid, "db", cooking_id, cooking_status == "planned") for meal, rid, cooking_id, cooking_status in conn.execute(
        "SELECT p.Meal, c.Recipe_ID, p.Cooking_ID, c.Status FROM PLAN p LEFT JOIN COOKINGS c USING (Cooking_ID) "
        "WHERE p.Date = ? AND p.Status != 'proposed'", (date_str,)).fetchall()}
    locked = {**in_db, **{p: (r, "requested", None, True) for p, r in fixed.items()}}
    fixed_meals = {}
    for meal, _ in meals:
        if meal in locked:
            rid, origin, cooking_id, to_cook = locked[meal]
            sim = (m.simulate_recipe(conn, inv, rid, products, ingredients=ing[rid]) if to_cook
                   else LEFTOVER_SIM) if rid else None
            fixed_meals[meal] = entry(meal, rid, sim, origin, cooking_id)

    # --- candidates for the free meals ---
    used = {v["Recipe_ID"] for v in fixed_meals.values() if v["Recipe_ID"]}
    no_repeat_days = profile["No_repeat_days"]
    recent = {r[0] for r in conn.execute(
        "SELECT DISTINCT c.Recipe_ID FROM COOKINGS c LEFT JOIN PLAN p USING (Cooking_ID) "
        "WHERE c.Cooking_Date != ? AND ABS(julianday(c.Cooking_Date) - julianday(?)) < ? "
        "AND COALESCE(p.Status, '') != 'proposed'", (date_str, date_str, no_repeat_days))}
    recent |= {rid for rid, d in (planned_cooks or []) if d != date_str
                and abs((date.fromisoformat(d) - date.fromisoformat(date_str)).days) < no_repeat_days}

    # leftovers: servings already cooked and not yet assigned to confirmed meals nor used in this run
    leftovers = {}
    for cooking_id, rid, remaining, reserved in conn.execute(
            "SELECT c.Cooking_ID, c.Recipe_ID, c.Remaining_Servings, (SELECT COUNT(*) FROM PLAN p "
            "WHERE p.Cooking_ID = c.Cooking_ID AND p.Status = 'confirmed') FROM COOKINGS c "
            "WHERE c.Status = 'cooked' AND c.Remaining_Servings > 0"):
        if remaining - reserved - (leftovers_used or {}).get(cooking_id, 0) > 0:
            leftovers[cooking_id] = (rid, remaining - reserved - (leftovers_used or {}).get(cooking_id, 0))

    def preselection(rid, cooking_id, meal):
        sim = LEFTOVER_SIM if cooking_id else m.simulate_recipe(conn, inv.copy(), rid, products, ingredients=ing[rid])
        recipe_kcal = _get_macros(rid, sim["group_choices"])["Kcal"]
        deviation = abs(recipe_kcal / target - shares[meal])
        # Penalize recipes that use excluded (expired by target date) ingredients
        recipe_ing_ids = {i["ID"] for i in (ing.get(rid) or []) if i.get("ID") is not None}
        uses_excluded = bool(recipe_ing_ids & excluded_ids)
        # Expiring priority: prefer recipes using soon-to-expire (but not yet excluded) ingredients
        min_days = None
        for ing_id in recipe_ing_ids:
            d = _expiry_days.get(ing_id)
            if d is not None and (min_days is None or d < min_days):
                min_days = d
        expiry_bonus = (1.0 / min_days) if min_days else 0.0
        return (len(sim["missing"]) + sim["incomplete_data"] + (1000 if uses_excluded else 0),
                -_score(sim["open_coverage"], sim["new_packages"], deviation) - expiry_bonus)

    free, cands = [], {}
    for meal, _ in meals:
        if meal in fixed_meals:
            continue
        per_meal = [r for (r,) in conn.execute("SELECT Recipe_ID FROM RECIPE_MEALS WHERE Meal = ? "
                                                "ORDER BY Recipe_ID", (meal,))]
        candidates = [r for r in per_meal if r not in used]
        allowed = [r for r in candidates if r not in recent]
        with_leftover = [(rid, cooking_id) for cooking_id, (rid, _) in leftovers.items() if rid in per_meal]
        if candidates and not allowed and not with_leftover:
            warnings.append(f"{meal}: recipes used recently, repetition allowed")
        pool = [(r, None) for r in (allowed or ([] if with_leftover else candidates))] + with_leftover
        if not pool:
            warnings.append(f"{meal}: no recipe available")
            continue
        cands[meal] = sorted(pool, key=lambda c: preselection(c[0], c[1], meal))[:MAX_CANDIDATES]
        free.append(meal)

    # --- search: every combination of candidates, with stock shared across meals ---
    def explore(i, inv_i, chosen, distinct):
        if i == len(free):
            yield chosen, inv_i
            return
        meal = free[i]
        for rid, cooking_id in cands[meal]:
            if cooking_id is None and distinct and rid in {s["Recipe_ID"] for s in chosen if not s["Cooking_ID"]}:
                continue
            if cooking_id and sum(1 for s in chosen if s["Cooking_ID"] == cooking_id) >= leftovers[cooking_id][1]:
                continue                                  # all the servings of the leftover are already assigned
            inv_n = inv_i.copy()
            sim = LEFTOVER_SIM if cooking_id else m.simulate_recipe(conn, inv_n, rid, products, ingredients=ing[rid])
            yield from explore(i + 1, inv_n, chosen + [entry(meal, rid, sim, "planned", cooking_id)], distinct)

    best = None
    for distinct in (True, False):          # if that is not enough, recipes repeated within the day are allowed
        for chosen, inv_fin in explore(0, inv, [], distinct):
            entries = sorted([*fixed_meals.values(), *chosen], key=lambda v: order[v["meal"]])
            present = [v for v in entries if v["Recipe_ID"]]
            sims = [v["sim"] for v in present]
            totals = m.sum_macros([v["macro"] for v in present])
            v0 = m.check_day(totals, profile, goals)
            vx = m.check_day(totals, profile, goals, profile["Open_extra_pct"])
            finishes_open = any(inv_fin.open_remainder(i) == 0 for i in open_before)
            status = "ok" if v0["ok"] else "extra" if vx["ok"] and finishes_open else "out_of_limits"
            q_tot = sum(shares[v["meal"]] for v in present) or 1.0
            share_deviation = sum(abs(v["macro"]["Kcal"] / (totals["Kcal"] or 1.0) - shares[v["meal"]] / q_tot)
                           for v in present)
            score = _score(sum(s["open_coverage"] for s in sims),
                                   sum(s["new_packages"] for s in sims), share_deviation)
            score -= EXTRA_PENALTY if status == "extra" else 0.0
            missing = {}
            for s in sims:
                for i, q in s["missing"].items():
                    missing[i] = missing.get(i, 0.0) + q
            problems = len(missing) + sum(s["incomplete_data"] for s in sims)
            out = status == "out_of_limits"
            key = (out, _deviation(v0) if out else 0.0, problems, -score)
            if best is None or key < best[0]:
                best = (key, {"date_str": date_str, "meals": entries, "totals": totals, "check": v0,
                                     "status": status, "missing": missing, "score": score,
                                     "kcal_target": target, "inventory": inv_fin})
        if best:
            if not distinct:
                warnings.append("recipes repeated within the same day: not enough alternatives")
            break
    plan = best[1]
    plan["warnings"] = warnings
    return plan
