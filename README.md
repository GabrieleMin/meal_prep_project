# Meal Planner

Desktop application (Tkinter) to manage a pantry, store recipes and plan meals that respect nutritional
limits while using already-opened packages first. Standard library only: Python 3.10+ (on Linux install the
system package `python3-tk` if Tkinter is missing).

## Run

    python -m meal_planner          # opens the window; creates inventory.db in the project root on first start

The database path can be changed with the `MEAL_PLANNER_DB` environment variable.

**Coming from the earlier Italian-named version?** Convert your old database once (the old file is never modified):

    python -m scripts.migrate_legacy_db inventario.db inventory.db

## Getting started (tabs)

1. **Profile** - set sex, activity level and goal (daily kcal target).
2. **Pantry** - add products (a new name asks for unit and values per 100 g) and consume quantities.
3. **Recipes** - add recipes with the form or import `.json` / `.csv` files; delete unused ones.
4. **Plan** - *Propose plan*, *Save plan*, then *Mark as cooked* (number of servings: extra servings become
   leftovers). Meals can be locked to a recipe or skipped.
5. **Shopping** - what is missing (consumption beyond availability + ingredients of the plan).

Meals are `breakfast`, `lunch`, `dinner`. Units: `kg`, `L`, `pcs` for products; `g`, `kg`, `ml`, `L`, `pcs`, `tbsp`, `tsp` in recipes.

### Recipe import (quantities are per ONE serving)

    JSON: [{"name": "Chicken and rice", "meals": ["lunch", "dinner"],
            "ingredients": [{"product": "Rice", "quantity": 100, "unit": "g"}]}]
    CSV (";" or ","): recipe;meals;product;quantity;unit;to_taste        (meals separated by "|")

## Project layout

    meal_planner/
      db/           connection.py, schema.py (base tables), recipe_schema.py (recipes, plan, goals, views)
      engine/       catalog (read data), nutrition (conversions, macros, limit checks),
                    virtual_inventory (in-memory simulation), profile, constants, cli
      planner/      day (daily planner), week (multi-day), storage (save plans, shopping list, "cooked"), cli
      inventory.py  add / consume items, availability, shopping list
      recipes.py    create, import, delete recipes
      gui/          app (main window), widgets, dialogs, support
    scripts/        setup_project.py, seed_inventory.py, seed_recipes.py (demo data), migrate_legacy_db.py
    tests/          test_inventory, test_engine, test_planner, test_recipes

## Command line (optional)

    python -m meal_planner.engine profile --sex M --activity moderate --goal maintenance
    python -m meal_planner.engine recipes
    python -m meal_planner.engine day "Yogurt, apple and milk" "Tomato spaghetti" "Chicken and rice" --kcal 2500
    python -m meal_planner.planner day --kcal 2000 --lock lunch="Chicken and rice" --skip dinner --save
    python -m meal_planner.planner week --start 2026-10-26 --days 7 --save
    python -m meal_planner.planner shopping
    python -m meal_planner.recipes import recipes.json

## Development

    python -m scripts.setup_project --test      # database, demo data, integrity check, all test suites
    python -m scripts.setup_project --reset -y  # wipe and reload demo data

## Future Implementations
- poter dire al planner di voler cucinare oggi i pasti per, per es. lunedì e mercoledì (meal prep) e fargliene tenere conto per gli altri pasti
- inserire campo per data di scadenza dell'alimento
