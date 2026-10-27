"""Command line of the engine: recipes, day check, profile."""

import argparse
import sys

from meal_planner.db.connection import get_connection, DB_PATH
from meal_planner.engine.catalog import load_goals, load_products, load_profile, recipe_id_from_name, product_names
from meal_planner.engine.constants import PAL
from meal_planner.engine.nutrition import recipe_macros, sum_macros, check_day
from meal_planner.engine.profile import set_profile
from meal_planner.engine.virtual_inventory import VirtualInventory, simulate_recipe


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
    print(f"{name:<34}{mac['Kcal']:>6.0f}{mac['Protein']:>7.1f}{mac['Carbs']:>7.1f}"
          f"{mac['Fat']:>8.1f}{mac['Fiber']:>7.1f}{sim['open_coverage'] * 100:>7.0f}%"
          f"{sim['new_packages']:>6}  {outcome(sim, products, names)}")


HEADER = (f"{'Recipe':<34}{'kcal':>6}{'Prot':>7}{'Carb':>7}{'Fat':>8}"
                f"{'Fiber':>7}{'Open':>8}{'New':>6}  Outcome")


def print_check(res):
    for v in res["entries"]:
        status = "ok" if v["ok"] else f"{v['direction'].upper()} by {v['deviation']:.1f}"
        print(f"  {v['nutrient']:<14}{v['value']:>9.1f} {v['unit']:<5}"
              f"[{_fmt(v['minimum'])} - {_fmt(v['maximum'])}]  {status}")


def cmd_recipes(conn):
    products, names = load_products(conn), product_names(conn)
    inv = VirtualInventory.from_db(conn)
    print("Each recipe is simulated on the current inventory, independently of the others.")
    print(HEADER)
    for recipe_id, name in conn.execute("SELECT Recipe_ID, Name FROM RECIPES ORDER BY Recipe_ID").fetchall():
        sim = simulate_recipe(conn, inv.copy(), recipe_id, products)
        mac = recipe_macros(conn, recipe_id, products, sim["group_choices"])
        recipe_row(name, mac, sim, products, names)


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
        print("  Kcal: target not defined, energy check skipped "
              "(use --kcal or the 'profile' command).")
    print("\nOverall outcome:", "within limits" if res["ok"] else "OUT OF LIMITS")


def cmd_profile(conn, args):
    kcal = set_profile(conn, args.sex, args.activity, args.goal, args.kcal)
    print(f"Profile updated: sex {args.sex}, activity {args.activity}, "
          f"goal {args.goal}, target {kcal:.0f} kcal/day.")


def main():
    parser = argparse.ArgumentParser(description="Calculation engine for recipes and plan.")
    parser.add_argument("--db", default=DB_PATH, help="database path")
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

    conn = get_connection(args.db)
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


if __name__ == "__main__":
    main()
