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
**Meal Planning & Suggestion Engine**

* **Advance Meal Prep Scheduling:** Allow the user to schedule the preparation of meals on the current day for specific future days (e.g., cooking today for Monday and Wednesday), automatically updating ingredient availability for the remaining days of the plan.
* **Ingredient Variety Constraint (Day-Meal Association):** Prevent the generation engine from suggesting the same food items in preceding meals if those items are already assigned to future Meal Prep days, ensuring proper ingredient rotation.
* **Expiring Food Priority:** Add a parameter to the planner algorithm to automatically prioritize ingredients that are closest to their expiration date during meal generation.
* **Multi-Day Expiration Validation & Exclusion List:** During multi-day meal planning, validate each ingredient's expiration date against the specific target date of every scheduled meal. Automatically place any item that is already expired or will expire before the target meal date into an **Exclusion List**, preventing the engine from including it in meal proposals.
* **Startup Expired Items Alert:** Trigger a warning pop-up at every application startup if the **Exclusion List** is not empty, notifying the user of the specific expired food items that need to be discarded.

**Inventory, Locations & Expiration Management**

* **Expiration Date Field:** Add a dedicated field to record the expiration date for each food item in the inventory.
* **Visual Expiration Indicators:** Apply a color-coded red gradient in the main inventory list to indicate items approaching their expiration date, and highlight already expired items with a distinct, high-contrast alert color.
* **Storage Location Management:** Create a dedicated section to create, edit, and rename physical storage locations for food items (e.g., fridge, pantry, freezer).

**Receipt Interface & GUI**

* **Receipt Module GUI Integration:** Align the visual style of the receipt module with the main application's design system and embed it directly into the primary GUI.
* **Drag & Drop and File Explorer Upload:** Implement an interactive drop zone labeled *"Drag receipts here"*, paired with a dedicated button to open the native system file explorer for manual file selection.
* **Last Upload Log:** Place a text field directly below the receipt upload area to record and display details or notes regarding the most recently imported receipt.
* **Receipt Alias Mapping Modal:** Develop a secondary pop-up window triggered immediately after the receipt item selection interface, allowing users to map unrecognized or abbreviated receipt entries (aliases) to standardized food items in the database.