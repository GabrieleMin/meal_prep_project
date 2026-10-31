"""
Engine: calculation engine for recipes and the meal plan. It NEVER writes to the
real inventory: it works on an in-memory copy (VirtualInventory). The only
write is the "profile" command, which updates the PROFILE table.

Contents
- recipe_macros():     macros per serving, with pcs/L/ml/tablespoon -> grams conversion
- VirtualInventory:  same logic as consume_quantity() (open remainders first, then
                       new packages in FIFO order) but in memory
- simulate_recipe():    open-package coverage, new packages, missing items
                       (picks the member of a substitutable group by itself)
- check_day():   day totals against kcal and nutritional limits
- estimate_maintenance() estimated energy requirement (Mifflin-St Jeor)

Rules
- Limits in % of energy and "per 1000 kcal" are evaluated on the ACTUAL kcal of the
  day; "per kg" limits on the profile weight. Several rules on the same
  nutrient hold together (intersection). Kcal are compared with
  Kcal_target +- Kcal_tolerance_pct.
- extra_pct (default 0) widens all limits: the planner uses it
  (Open_extra_pct of the profile) only if that finishes an open remainder.
- Open coverage of an ingredient = share of the requested quantity taken from
  already-open remainders (0..1); for "pcs" it is always 0.
  Recipe coverage = average over its ingredients.
- "pcs" products: whole pieces only; a quantity in g/ml is converted to pieces,
  rounding up.

Command line (recipe names with spaces go in quotes):
    python -m meal_planner.engine recipes
    python -m meal_planner.engine day "Yogurt, apple and milk" "Tomato spaghetti" "Chicken and rice" --kcal 2500
    python -m meal_planner.engine profile --sex M --activity moderate --goal maintenance
    python -m meal_planner.engine profile --sex M --activity moderate --goal weight_loss --kcal 2400
"""

from meal_planner.engine.catalog import load_goals, load_products, load_profile, recipe_id_from_name, recipe_ingredients, group_members, product_names
from meal_planner.engine.cli import HEADER, cmd_day, cmd_profile, cmd_recipes, outcome, main, recipe_row, print_check
from meal_planner.engine.constants import EPS, KCAL_PER_G, MACROS, LIMITED_NUTRIENTS, OPTIONAL, PAL
from meal_planner.engine.nutrition import grams, limits_in_grams, recipe_macros, inventory_quantity, sum_macros, check_day
from meal_planner.engine.profile import set_profile, estimate_maintenance
from meal_planner.engine.virtual_inventory import VirtualInventory, simulate_recipe

__all__ = ["EPS", "HEADER", "VirtualInventory", "KCAL_PER_G", "MACROS", "LIMITED_NUTRIENTS", "OPTIONAL", "PAL", "load_goals", "load_products", "load_profile", "cmd_day", "cmd_profile", "cmd_recipes", "outcome", "grams", "recipe_id_from_name", "set_profile", "recipe_ingredients", "limits_in_grams", "recipe_macros", "main", "group_members", "product_names", "inventory_quantity", "recipe_row", "simulate_recipe", "sum_macros", "print_check", "estimate_maintenance", "check_day"]
