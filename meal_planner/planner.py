"""Daily/weekly meal planner, DB plan storage, shopping list sync, and CLI."""

import argparse
from collections import Counter
from datetime import date, timedelta
import sys

from meal_planner import engine as m
from meal_planner.db import connection as _db_conn
from meal_planner.inventory import get_exclusion_list

OPEN_WEIGHT, NEW_WEIGHT, SHARE_WEIGHT, EXTRA_PENALTY = 3.0, 1.0, 5.0, 1.0
MAX_CANDIDATES = 12
LEFTOVER_SIM = {
    "detail": [], "open_coverage": 1.0, "new_packages": 0, "missing": {},
    "feasible": True, "incomplete_data": False, "group_choices": {},
}
STATUS_LABELS = {
    "ok": "within limits",
    "extra": "within limits with extra tolerance (finishes an open package)",
    "out_of_limits": "OUT OF LIMITS (it is the closest plan to the limits)",
}

try:
    from meal_planner.engine._speedups import score as _c_score
    def _score(coverage, new, share_deviation):
        return _c_score(float(coverage), float(new), float(share_deviation))
except (ImportError, AttributeError):
    def _score(coverage, new, share_deviation):
        return OPEN_WEIGHT * coverage - NEW_WEIGHT * new - SHARE_WEIGHT * share_deviation


def _deviation(check):
    return sum(
        v["deviation"] / ((v["minimum"] if v["direction"] == "below" else v["maximum"]) or 1.0)
        for v in check["entries"] if not v["ok"]
    )


def _load_ingredients(conn, recipe_ids=None) -> dict:
    if recipe_ids is None:
        rows = conn.execute(
            "SELECT Recipe_ID, ID, Group_ID, Quantity, Unit, To_Taste "
            "FROM RECIPE_INGREDIENTS ORDER BY Recipe_ID, Row_ID"
        )
        ing = {}
    else:
        ids = list(recipe_ids)
        ing = {rid: [] for rid in ids}
        if not ids:
            return ing
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT Recipe_ID, ID, Group_ID, Quantity, Unit, To_Taste "
            f"FROM RECIPE_INGREDIENTS WHERE Recipe_ID IN ({placeholders}) ORDER BY Recipe_ID, Row_ID",
            ids,
        )
    for r in rows:
        ing.setdefault(r[0], []).append(
            {"ID": r[1], "Group_ID": r[2], "Quantity": r[3], "Unit": r[4], "To_Taste": r[5]}
        )
    return ing


def plan_day(conn, date_str, kcal=None, inv=None, fixed=None, planned_cooks=None, leftovers_used=None):
    """Plans one day (`date_str` YYYY-MM-DD) choosing one recipe per meal without writing to DB."""
    products, goals, profile = m.load_products(conn), m.load_goals(conn), m.load_profile(conn) or {}
    if kcal:
        profile["Kcal_target"] = kcal
    target = profile.get("Kcal_target")
    if not target:
        raise ValueError("Kcal target not defined: use --kcal or 'python -m meal_planner.engine profile ...'.")
    meals = conn.execute("SELECT Name, Energy_Share_pct FROM MEALS ORDER BY Sort_Order").fetchall()
    order = {name: i for i, (name, _) in enumerate(meals)}
    shares = {name: q / 100.0 for name, q in meals}
    recipe_names = dict(conn.execute("SELECT Recipe_ID, Name FROM RECIPES").fetchall())
    loaded_ing = _load_ingredients(conn)
    ing = {rid: loaded_ing.get(rid, []) for rid in recipe_names}
    inv = (inv if inv is not None else m.VirtualInventory.from_db(conn)).copy()
    open_before = {i for i in inv.rows if inv.open_remainder(i) > 0}
    warnings = []

    excluded_ids = {r["ID"] for r in get_exclusion_list(target_date=date_str, conn=conn)}
    today_d = date.fromisoformat(date_str)
    _expiry_days = {}
    for pid, exp_str in conn.execute("SELECT ID, MIN(Expiration_Date) FROM INVENTORY WHERE Expiration_Date IS NOT NULL GROUP BY ID").fetchall():
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
        return {
            "meal": meal, "Recipe_ID": rid, "recipe": recipe_names.get(rid), "origin": origin,
            "sim": sim, "macro": mac, "Cooking_ID": cooking_id,
            "leftover": bool(cooking_id) and origin == "planned",
        }

    fixed = fixed or {}
    for meal in fixed:
        if meal not in shares:
            raise ValueError(f"Unknown meal: {meal}")
    in_db = {
        meal: (rid, "db", cooking_id, cooking_status == "planned")
        for meal, rid, cooking_id, cooking_status in conn.execute(
            "SELECT p.Meal, c.Recipe_ID, p.Cooking_ID, c.Status FROM PLAN p LEFT JOIN COOKINGS c USING (Cooking_ID) "
            "WHERE p.Date = ? AND p.Status != 'proposed'", (date_str,),
        ).fetchall()
    }
    locked = {**in_db, **{p: (r, "requested", None, True) for p, r in fixed.items()}}
    fixed_meals = {}
    for meal, _ in meals:
        if meal in locked:
            rid, origin, cooking_id, to_cook = locked[meal]
            sim = (m.simulate_recipe(conn, inv, rid, products, ingredients=ing[rid]) if to_cook else LEFTOVER_SIM) if rid else None
            fixed_meals[meal] = entry(meal, rid, sim, origin, cooking_id)

    used = {v["Recipe_ID"] for v in fixed_meals.values() if v["Recipe_ID"]}
    no_repeat_days = profile["No_repeat_days"]
    recent = {
        r[0] for r in conn.execute(
            "SELECT DISTINCT c.Recipe_ID FROM COOKINGS c LEFT JOIN PLAN p USING (Cooking_ID) "
            "WHERE c.Cooking_Date != ? AND ABS(julianday(c.Cooking_Date) - julianday(?)) < ? "
            "AND COALESCE(p.Status, '') != 'proposed'", (date_str, date_str, no_repeat_days),
        )
    } | {
        rid for rid, d in (planned_cooks or [])
        if d != date_str and abs((date.fromisoformat(d) - date.fromisoformat(date_str)).days) < no_repeat_days
    }

    leftovers = {}
    for cooking_id, rid, remaining, reserved in conn.execute(
        "SELECT c.Cooking_ID, c.Recipe_ID, c.Remaining_Servings, "
        "(SELECT COUNT(*) FROM PLAN p WHERE p.Cooking_ID = c.Cooking_ID AND p.Status = 'confirmed') "
        "FROM COOKINGS c WHERE c.Status = 'cooked' AND c.Remaining_Servings > 0"
    ):
        avail = remaining - reserved - (leftovers_used or {}).get(cooking_id, 0)
        if avail > 0:
            leftovers[cooking_id] = (rid, avail)

    def preselection(rid, cooking_id, meal):
        sim = LEFTOVER_SIM if cooking_id else m.simulate_recipe(conn, inv.copy(), rid, products, ingredients=ing[rid])
        recipe_kcal = _get_macros(rid, sim["group_choices"])["Kcal"]
        deviation = abs(recipe_kcal / target - shares[meal])
        recipe_ing_ids = {i["ID"] for i in (ing.get(rid) or []) if i.get("ID") is not None}
        uses_excluded = bool(recipe_ing_ids & excluded_ids)
        days_list = [_expiry_days[i] for i in recipe_ing_ids if i in _expiry_days]
        expiry_bonus = (1.0 / min(days_list)) if days_list else 0.0
        return (
            len(sim["missing"]) + sim["incomplete_data"] + (1000 if uses_excluded else 0),
            -_score(sim["open_coverage"], sim["new_packages"], deviation) - expiry_bonus,
        )

    free, cands = [], {}
    for meal, _ in meals:
        if meal in fixed_meals:
            continue
        per_meal = [r for (r,) in conn.execute("SELECT Recipe_ID FROM RECIPE_MEALS WHERE Meal = ? ORDER BY Recipe_ID", (meal,))]
        candidates = [r for r in per_meal if r not in used]
        allowed = [r for r in candidates if r not in recent]
        with_leftover = [(rid, cid) for cid, (rid, _) in leftovers.items() if rid in per_meal]
        if candidates and not allowed and not with_leftover:
            warnings.append(f"{meal}: recipes used recently, repetition allowed")
        pool = [(r, None) for r in (allowed or ([] if with_leftover else candidates))] + with_leftover
        if not pool:
            warnings.append(f"{meal}: no recipe available")
            continue
        cands[meal] = sorted(pool, key=lambda c: preselection(c[0], c[1], meal))[:MAX_CANDIDATES]
        free.append(meal)

    def explore(i, inv_i, chosen, distinct):
        if i == len(free):
            yield chosen, inv_i
            return
        meal = free[i]
        for rid, cooking_id in cands[meal]:
            if cooking_id is None and distinct and rid in {s["Recipe_ID"] for s in chosen if not s["Cooking_ID"]}:
                continue
            if cooking_id and sum(1 for s in chosen if s["Cooking_ID"] == cooking_id) >= leftovers[cooking_id][1]:
                continue
            inv_n = inv_i.copy()
            sim = LEFTOVER_SIM if cooking_id else m.simulate_recipe(conn, inv_n, rid, products, ingredients=ing[rid])
            yield from explore(i + 1, inv_n, chosen + [entry(meal, rid, sim, "planned", cooking_id)], distinct)

    best = None
    for distinct in (True, False):
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
            share_dev = sum(abs(v["macro"]["Kcal"] / (totals["Kcal"] or 1.0) - shares[v["meal"]] / q_tot) for v in present)
            score = _score(sum(s["open_coverage"] for s in sims), sum(s["new_packages"] for s in sims), share_dev)
            score -= EXTRA_PENALTY if status == "extra" else 0.0
            missing = {}
            for s in sims:
                for i, q in s["missing"].items():
                    missing[i] = missing.get(i, 0.0) + q
            problems = len(missing) + sum(s["incomplete_data"] for s in sims)
            out = status == "out_of_limits"
            key = (out, _deviation(v0) if out else 0.0, problems, -score)
            if best is None or key < best[0]:
                best = (key, {
                    "date_str": date_str, "meals": entries, "totals": totals, "check": v0,
                    "status": status, "missing": missing, "score": score,
                    "kcal_target": target, "inventory": inv_fin,
                })
        if best:
            if not distinct:
                warnings.append("recipes repeated within the same day: not enough alternatives")
            break
    plan = best[1]
    plan["warnings"] = warnings
    return plan


def _apply_plan(conn, inv, products, before=None):
    missing = {}
    entries = conn.execute(
        "SELECT c.Recipe_ID FROM PLAN p JOIN COOKINGS c USING (Cooking_ID) "
        "WHERE p.Status IN ('proposed', 'confirmed') AND c.Status = 'planned' AND (? IS NULL OR p.Date < ?) "
        "ORDER BY p.Date, (SELECT Sort_Order FROM MEALS WHERE Name = p.Meal)", (before, before),
    ).fetchall()
    ing_map = _load_ingredients(conn, {rid for (rid,) in entries if rid is not None})
    for (rid,) in entries:
        for i, q in m.simulate_recipe(conn, inv, rid, products, ingredients=ing_map.get(rid))["missing"].items():
            missing[i] = missing.get(i, 0.0) + q
    return missing


def update_plan_shopping_list(conn):
    """Recomputes PLAN_SHOPPING_LIST from scratch with missing items of the whole plan."""
    missing = _apply_plan(conn, m.VirtualInventory.from_db(conn), m.load_products(conn))
    conn.execute("DELETE FROM PLAN_SHOPPING_LIST")
    conn.executemany("INSERT INTO PLAN_SHOPPING_LIST (ID, Missing_Quantity) VALUES (?, ?)",
                     [(i, q) for i, q in missing.items() if q > 0])
    conn.commit()


def save_plan(conn, plan, update_shopping=True):
    """Writes the plan for the day and optionally updates PLAN_SHOPPING_LIST."""
    d = plan["date_str"]
    new = [v for v in plan["meals"] if v["origin"] != "db"]
    placeholders = ",".join("?" * len(new))
    old = conn.execute(
        f"SELECT Entry_ID, Cooking_ID FROM PLAN WHERE Date = ? AND Status != 'consumed' "
        f"AND (Status = 'proposed' OR Meal IN ({placeholders}))", [d, *[v["meal"] for v in new]],
    ).fetchall()
    conn.executemany("DELETE FROM PLAN WHERE Entry_ID = ?", [(i,) for i, _ in old])
    conn.executemany("DELETE FROM COOKINGS WHERE Cooking_ID = ? AND Status = 'planned'", [(c,) for _, c in old if c])
    for v in new:
        cooking = v["Cooking_ID"] if v["Recipe_ID"] else None
        if v["Recipe_ID"] and cooking is None:
            cooking = conn.execute(
                "INSERT INTO COOKINGS (Recipe_ID, Cooking_Date, Total_Servings, Remaining_Servings) VALUES (?, ?, 1, 1)",
                (v["Recipe_ID"], d),
            ).lastrowid
        status = "skipped" if cooking is None else ("proposed" if v["origin"] == "planned" else "confirmed")
        conn.execute("INSERT INTO PLAN (Date, Meal, Cooking_ID, Status) VALUES (?, ?, ?, ?)", (d, v["meal"], cooking, status))
    if update_shopping:
        update_plan_shopping_list(conn)


def schedule_meal_prep(conn, recipe_id: int, cook_date: str, target_dates: list) -> int:
    """Schedules a meal prep cooking and returns the new Cooking_ID."""
    servings = len(target_dates)
    if servings < 1:
        raise ValueError("At least one target date is required.")
    cooking_id = conn.execute(
        "INSERT INTO COOKINGS (Recipe_ID, Cooking_Date, Total_Servings, Remaining_Servings, Status) "
        "VALUES (?, ?, ?, ?, 'planned')", (recipe_id, cook_date, servings, servings),
    ).lastrowid
    conn.commit()
    return cooking_id


def mark_cooked(conn, date_str, meal, servings=1):
    """Marks a meal as consumed and deducts ingredients (or one leftover serving) atomically."""
    r = conn.execute(
        "SELECT p.Cooking_ID, c.Recipe_ID, p.Status, c.Status, c.Remaining_Servings FROM PLAN p "
        "LEFT JOIN COOKINGS c USING (Cooking_ID) WHERE p.Date = ? AND p.Meal = ?", (date_str, meal),
    ).fetchone()
    if r is None or r[2] in ("consumed", "skipped"):
        raise ValueError(f"No meal to cook for {meal} on {date_str}.")
    cooking_id, rid, _, cooking_status, remaining = r
    if servings < 1:
        raise ValueError("Servings must be at least 1.")
    if cooking_status == "cooked":
        if servings != 1 or remaining < 1:
            raise ValueError("This meal uses a leftover: there are no other servings to cook or none remain.")
        conn.execute("UPDATE COOKINGS SET Remaining_Servings = Remaining_Servings - 1 WHERE Cooking_ID = ?", (cooking_id,))
    else:
        products, inv = m.load_products(conn), m.VirtualInventory.from_db(conn)
        sim = m.simulate_recipe(conn, inv, rid, products, servings=servings)
        if not sim["feasible"]:
            names = m.product_names(conn)
            raise ValueError("Ingredients missing: " + ", ".join(
                f"{names[i]} {q:.2f} {products[i]['unit']}" for i, q in sim["missing"].items()
            ))
        inv.write(conn, {d["ID"] for d in sim["detail"]})
        conn.execute(
            "UPDATE COOKINGS SET Status = 'cooked', Total_Servings = ?, Remaining_Servings = ? WHERE Cooking_ID = ?",
            (servings, servings - 1, cooking_id),
        )
    conn.execute("UPDATE PLAN SET Status = 'consumed' WHERE Date = ? AND Meal = ?", (date_str, meal))
    update_plan_shopping_list(conn)


def plan_week(conn, start, days=7, kcal=None, fixed=None):
    """Plans `days` consecutive days starting from `start` without writing to DB."""
    inv = m.VirtualInventory.from_db(conn)
    _apply_plan(conn, inv, m.load_products(conn), before=start)
    plans, planned_cooks, leftovers_used = [], [], {}
    for k in range(days):
        d = (date.fromisoformat(start) + timedelta(days=k)).isoformat()
        p = plan_day(conn, d, kcal, inv, (fixed or {}).get(d), planned_cooks, leftovers_used)
        planned_cooks += [(v["Recipe_ID"], d) for v in p["meals"] if v["Recipe_ID"] and v["origin"] != "db"]
        for v in p["meals"]:
            if v["leftover"]:
                leftovers_used[v["Cooking_ID"]] = leftovers_used.get(v["Cooking_ID"], 0) + 1
        inv = p["inventory"]
        plans.append(p)
    return plans


def save_week(conn, plans):
    """Saves all days in a single transaction and recomputes the shopping list once."""
    for p in plans:
        save_plan(conn, p, update_shopping=False)
    update_plan_shopping_list(conn)


def print_plan(conn, p):
    products, names = m.load_products(conn), m.product_names(conn)
    print(f"Plan for {p['date_str']}: {STATUS_LABELS[p['status']]}  (target {p['kcal_target']:.0f} kcal)")
    print(m.HEADER)
    for v in p["meals"]:
        if v["Recipe_ID"] is None:
            print(f"{v['meal']}: (skipped)")
        else:
            m.recipe_row(f"{v['meal']}: {v['recipe']}" + (" (leftover)" if v["leftover"] else ""),
                         v["macro"], v["sim"], products, names)
    fixed = [f"{v['meal']} ({'already in the plan' if v['origin'] == 'db' else 'requested'})"
             for v in p["meals"] if v["origin"] != "planned"]
    if fixed:
        print("Fixed meals: " + ", ".join(fixed))
    print("\nDay check")
    m.print_check(p["check"])
    if p["missing"]:
        print("\nTo buy: " + ", ".join(f"{names[i]} {q:.2f} {products[i]['unit']}" for i, q in p["missing"].items()))
    for a in p["warnings"]:
        print("Warning:", a)


def print_week(conn, plans):
    products, names = m.load_products(conn), m.product_names(conn)
    print(f"{'Date':<12}{'Status':<14}{'kcal':>6}  Meals")
    missing, warnings = {}, Counter()
    for p in plans:
        meals = " / ".join(
            (v["recipe"] + (" (leftover)" if v["leftover"] else "")) if v["recipe"] else "(skipped)"
            for v in p["meals"]
        )
        print(f"{p['date_str']:<12}{p['status']:<14}{p['totals']['Kcal']:>6.0f}  {meals}")
        out = "; ".join(f"{v['nutrient']} {v['direction']} by {v['deviation']:.1f}"
                        for v in p["check"]["entries"] if not v["ok"])
        if out:
            print(f"{'':<12}deviations: {out}")
        for i, q in p["missing"].items():
            missing[i] = missing.get(i, 0.0) + q
        warnings.update(p["warnings"])
    states = Counter(p["status"] for p in plans)
    print(f"\nDays: {states['ok']} ok, {states['extra']} with extra tolerance, {states['out_of_limits']} out of limits")
    if missing:
        print("To buy: " + ", ".join(f"{names[i]} {q:.2f} {products[i]['unit']}" for i, q in missing.items()))
    for text, n in warnings.items():
        print(f"Warning: {text}" + (f" (x{n})" if n > 1 else ""))


def _parse_date(text):
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        sys.exit("Invalid date: use the YYYY-MM-DD format.")


def _fixed_from_arguments(conn, locked, skipped):
    fixed = {}
    for arg in locked or []:
        meal, _, name = arg.partition("=")
        rid = m.recipe_id_from_name(conn, name)
        if rid is None:
            sys.exit(f"--lock {arg!r}: use MEAL=\"Recipe name\" with an existing recipe")
        fixed[meal] = rid
    return {**fixed, **{meal: None for meal in skipped or []}}


def main():
    parser = argparse.ArgumentParser(description="Meal planner.")
    parser.add_argument("--db", default=_db_conn.DB_PATH, help="database path")
    sub = parser.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("day", help="propose the plan for one day")
    g.add_argument("--date", default=date.today().isoformat(), help="YYYY-MM-DD (default: today)")
    g.add_argument("--kcal", type=float, help="kcal target (otherwise the profile one)")
    g.add_argument("--lock", action="append", metavar='MEAL="RECIPE"', help="fix the recipe of a meal")
    g.add_argument("--skip", action="append", metavar="MEAL", help="skip a meal")
    g.add_argument("--save", action="store_true", help="save the plan and update the shopping list")
    w = sub.add_parser("week", help="propose the plan for several consecutive days")
    w.add_argument("--start", default=date.today().isoformat(), help="first day YYYY-MM-DD (default: today)")
    w.add_argument("--days", type=int, default=7, help="how many days, 1 to 31 (default 7)")
    w.add_argument("--kcal", type=float, help="kcal target (otherwise the profile one)")
    w.add_argument("--save", action="store_true", help="save the plans and update the shopping list")
    sub.add_parser("shopping", help="recompute and show the shopping list (consumption + plan)")
    args = parser.parse_args()

    conn = _db_conn.get_connection(args.db)
    try:
        if args.cmd == "day":
            plan = plan_day(conn, _parse_date(args.date), args.kcal,
                            fixed=_fixed_from_arguments(conn, args.lock, args.skip))
            print_plan(conn, plan)
            if args.save:
                save_plan(conn, plan)
                print("\nPlan saved. Plan shopping list updated.")
        elif args.cmd == "week":
            if not 1 <= args.days <= 31:
                sys.exit("--days must be between 1 and 31.")
            plans = plan_week(conn, _parse_date(args.start), args.days, args.kcal)
            print_week(conn, plans)
            if args.save:
                save_week(conn, plans)
                print("\nPlans saved. Plan shopping list updated.")
        else:
            update_plan_shopping_list(conn)
            print("Shopping list (consumption + plan):")
            for i, name, q, u in conn.execute("SELECT ID, Name, Missing_Quantity, Unit FROM SHOPPING_LIST_VIEW ORDER BY ID"):
                print(f"  [{i}] {name:<28}{q:>8.2f} {u}")
    except ValueError as e:
        sys.exit(str(e))
    finally:
        conn.close()


__all__ = [
    "MAX_CANDIDATES", "EXTRA_PENALTY", "OPEN_WEIGHT", "NEW_WEIGHT", "SHARE_WEIGHT",
    "LEFTOVER_SIM", "STATUS_LABELS", "update_plan_shopping_list", "main", "plan_day",
    "plan_week", "save_plan", "save_week", "schedule_meal_prep", "mark_cooked",
    "print_plan", "print_week", "_apply_plan", "_score", "_deviation",
]

if __name__ == "__main__":
    sys.exit(main())
