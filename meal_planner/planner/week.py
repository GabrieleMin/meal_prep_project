"""Multi-day planning on top of the daily planner."""

from datetime import date, timedelta

from meal_planner import engine as m
from meal_planner.planner.day import plan_day
from meal_planner.planner.storage import _apply_plan, update_plan_shopping_list, save_plan


def plan_week(conn, start, days=7, kcal=None, fixed=None):
    """Plans for `days` consecutive days starting from `start`. It starts from the inventory minus the meals
    already planned before `start` (their stock stays reserved), then passes the stock from one
    day to the next. `fixed` = {date: {meal: Recipe_ID | None}}. It does not write to the DB."""
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
    """Saves all the days in a single transaction and recomputes the shopping list once."""
    for p in plans:
        save_plan(conn, p, update_shopping=False)
    update_plan_shopping_list(conn)
