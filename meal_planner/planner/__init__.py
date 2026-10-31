"""
Planner: daily planner that picks one recipe per meal, looking for the best
compromise between nutritional limits and use of open packages. It works on a
virtual inventory (the engine package): it writes to the database only with save_plan().

How it chooses (in order of importance)
  1. Plan within the limits (kcal and nutrients, with the profile tolerances).
     The extra overshoot (Open_extra_pct) is allowed only if the plan finishes
     an open remainder. If no plan meets the limits, the one with the smallest
     relative deviation wins (the missing quantity is reported).
  2. Fewer missing products (they end up in the plan's shopping list).
  3. Score = OPEN_WEIGHT * open-package coverage
               - NEW_WEIGHT  * new packages opened
               - SHARE_WEIGHT  * deviation from the meal split (e.g. 25/40/35)
               - EXTRA_PENALTY if the extra tolerance is used.

Rules
  - Different recipes within the same day and no recipes cooked in the last
    No_repeat_days days; if there are no alternatives, repetition is
    allowed and reported in "warnings". Saved proposals (status "proposed") do
    not constrain neighbouring days: only real or confirmed cookings count, so
    re-planning is repeatable.
  - Meals already confirmed/skipped/consumed in the DB, or passed with `fixed`,
    stay as they are and use up their stock.

Usage (from the project root):
    python -m meal_planner.planner day --kcal 2000
    python -m meal_planner.planner day --date 2026-10-24 --lock lunch="Chicken and rice" --skip dinner --save
    python -m meal_planner.planner week --start 2026-10-26 --days 7 --save
    python -m meal_planner.planner shopping
The kcal target is the profile one (python -m meal_planner.engine profile ...) if --kcal is missing.
"""

from meal_planner.planner.cli import STATUS_LABELS, main, print_plan, print_week
from meal_planner.planner.day import MAX_CANDIDATES, EXTRA_PENALTY, OPEN_WEIGHT, NEW_WEIGHT, SHARE_WEIGHT, LEFTOVER_SIM, plan_day
from meal_planner.planner.storage import update_plan_shopping_list, save_plan, mark_cooked
from meal_planner.planner.week import plan_week, save_week

__all__ = ["MAX_CANDIDATES", "EXTRA_PENALTY", "OPEN_WEIGHT", "NEW_WEIGHT", "SHARE_WEIGHT", "LEFTOVER_SIM", "STATUS_LABELS", "update_plan_shopping_list", "main", "plan_day", "plan_week", "save_plan", "save_week", "mark_cooked", "print_plan", "print_week"]
