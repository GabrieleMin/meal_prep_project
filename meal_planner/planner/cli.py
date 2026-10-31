"""Command line of the planner: day, week, shopping list."""

from collections import Counter
from datetime import date
import argparse
import sys

from meal_planner import engine as m
from meal_planner.db.connection import get_connection, DB_PATH
from meal_planner.planner.day import plan_day
from meal_planner.planner.storage import update_plan_shopping_list, save_plan
from meal_planner.planner.week import plan_week, save_week


STATUS_LABELS = {"ok": "nei limiti",
         "extra": "within limits with extra tolerance (finishes an open package)",
         "out_of_limits": "OUT OF LIMITS (it is the closest plan to the limits)"}


def print_plan(conn, p):
    products, names = m.load_products(conn), m.product_names(conn)
    print(f"Plan for {p['date_str']}: {STATUS_LABELS[p['status']]}  (target {p['kcal_target']:.0f} kcal)")
    print(m.HEADER)
    for v in p["meals"]:
        if v["Recipe_ID"] is None:
            print(f"{v['meal']}: (skipped)")
        else:
            m.recipe_row(f"{v['meal']}: {v['recipe']}" + (" (leftover)" if v["leftover"] else ""), v["macro"], v["sim"], products, names)
    fixed = [f"{v['meal']} ({'already in the plan' if v['origin'] == 'db' else 'requested'})"
             for v in p["meals"] if v["origin"] != "planned"]
    if fixed:
        print("Fixed meals: " + ", ".join(fixed))
    print("\nDay check")
    m.print_check(p["check"])
    if p["missing"]:
        print("\nTo buy: " + ", ".join(f"{names[i]} {q:.2f} {products[i]['unit']}"
                                             for i, q in p["missing"].items()))
    for a in p["warnings"]:
        print("Attenzione:", a)


def print_week(conn, plans):
    products, names = m.load_products(conn), m.product_names(conn)
    print(f"{'Date':<12}{'Status':<14}{'kcal':>6}  Meals")
    missing, warnings = {}, Counter()
    for p in plans:
        meals = " / ".join((v["recipe"] + (" (leftover)" if v["leftover"] else "")) if v["recipe"] else "(skipped)"
                           for v in p["meals"])
        print(f"{p['date_str']:<12}{p['status']:<14}{p['totals']['Kcal']:>6.0f}  {meals}")
        out = "; ".join(f"{v['nutrient']} {v['direction']} by {v['deviation']:.1f}"
                          for v in p["check"]["entries"] if not v["ok"])
        if out:
            print(f"{'':<12}deviations: {out}")
        for i, q in p["missing"].items():
            missing[i] = missing.get(i, 0.0) + q
        warnings.update(p["warnings"])
    states = Counter(p["status"] for p in plans)
    print(f"\nDays: {states['ok']} ok, {states['extra']} with extra tolerance, "
          f"{states['out_of_limits']} out of limits")
    if missing:
        print("To buy: " + ", ".join(f"{names[i]} {q:.2f} {products[i]['unit']}"
                                          for i, q in missing.items()))
    for text, n in warnings.items():
        print(f"Attenzione: {text}" + (f" (x{n})" if n > 1 else ""))


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
    parser.add_argument("--db", default=DB_PATH, help="database path")
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

    conn = get_connection(args.db)
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
            for i, name, q, u in conn.execute(
                    "SELECT ID, Name, Missing_Quantity, Unit FROM SHOPPING_LIST_VIEW ORDER BY ID"):
                print(f"  [{i}] {name:<28}{q:>8.2f} {u}")
    except ValueError as e:
        sys.exit(str(e))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
