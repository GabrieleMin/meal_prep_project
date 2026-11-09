# Meal Planner

Desktop application (Tkinter) to manage a pantry, store recipes and plan meals that respect nutritional
limits while using already-opened packages first. Standard library only: Python 3.10+ (on Linux install the
system package `python3-tk` if Tkinter is missing).

## Run

    python -m meal_planner          # opens the window; creates inventory.db in the project root on first start

The database path can be changed with the `MEAL_PLANNER_DB` environment variable.

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
      db/           connection.py, schema.py (consolidated schema, migrations, views, defaults), recipe_schema.py
      engine/       catalog (read data), nutrition (conversions, macros, limit checks),
                    virtual_inventory (in-memory simulation), profile, constants, cli
      planner/      day (daily planner), week (multi-day), storage (save plans, shopping list, "cooked"), cli
      receipts/     reader.py, pipeline.py (receipt OCR parsing and reconciliation pipeline)
      inventory.py  add / consume items, availability, shopping list
      recipes.py    create, import, delete recipes
      gui/          app (main window), widgets, dialogs, support
    scripts/        setup_project.py, demo_receipt.py, seed_inventory.py, seed_recipes.py (demo data), build_extensions.py
    tests/          6 consolidated suites: test_domain_core, test_receipts_and_reconciliation, test_inventory_management, test_recipe_management, test_settings_and_profile, test_e2e_and_invariants

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

## Features Implemented

* **Advance Meal Prep Scheduling:** Schedule multi-serving cooks and assign future leftovers to targeted dates via `schedule_meal_prep()` and `MealPrepDialog`.
* **Ingredient Variety Constraint:** Prevents identical recipe and ingredient repetitions across adjacent meals and scheduled prep days within `No_repeat_days`.
* **Expiring Food Priority:** Ranks candidate recipes with a `(1.0 / min_days)` score bonus to prioritize pantry items closest to expiration.
* **Multi-Day Expiration & Exclusion List:** Filters out expired stock using the `EXCLUSION_LIST` table and date penalties during meal plan generation.
* **Startup Expired Items Alert:** Displays a non-blocking startup dialog listing expired food items that should be discarded.
* **Expiration Date Tracking & Visual Indicators:** ISO `YYYY-MM-DD` tracking with color-coded Treeview shelf-life badges (red: expired, orange: $\le$ 3 days, yellow: $\le$ 7 days).
* **Storage Location Management:** Full CRUD operations and foreign key safety for storage zones via the `LOCATIONS` table and interactive dialogs.
* **Embedded Receipt Management:** Sidebar upload area with Drag & Drop (`tkinterdnd2`), native file browser, and real-time parser log console.
* **Two-Step Receipt Ingestion Pipeline:** OCR raw review (`FinestraModifica`) followed by interactive catalog reconciliation (`ReceiptReconciliationDialog`) for atomic SQLite insertion.

### Code Refactoring, Cleanup & Pipeline Hardening

* **Package Internalization (`meal_planner/receipts/`):** Migrated receipt OCR and review logic from the external `features_development/` directory into a clean internal package (`reader.py`, `pipeline.py`, `__init__.py`). Completely eliminated dynamic `sys.path.insert` hacks across GUI, configuration, and test suites, and deleted `features_development/`.
* **Legacy Code & Artifact Removal:** Deleted `scripts/migrate_legacy_db.py`, removed obsolete `inventario.db` checks from `meal_planner/__main__.py`, and purged orphaned `.pyc` files from root `__pycache__`.
* **Consolidated Database Schema (`meal_planner/db/schema.py`):** Unified all 18 tables, 10 B-tree indices, 3 views (`SHOPPING_LIST_VIEW`, `INGREDIENT_GRAMS_VIEW`, `RECIPE_MACROS_VIEW`), defaults, and migrations into a single idempotent module; refactored `recipe_schema.py` into a backward-compatible proxy.
* **Cross-Dimensional Unit Normalization:** Extended `convert_receipt_quantity_unit()` with `grams_per_piece` and `density_kg_per_l` to support bidirectional conversions (`pcs` $\leftrightarrow$ `kg`, `L` $\leftrightarrow$ `kg`, `pcs` $\leftrightarrow$ `L`), preventing raw piece counts from being mistakenly stored as kilograms in `ReceiptReconciliationDialog`.
* **Ingestion & Numerical Hardening:** Protected `map_receipt_item_to_add_item_args()` against `OverflowError` on infinite/exponential package values; added `None` guards and integer-float string parsing (`"1.0"`) to `valida_voci()`; enforced `math.isfinite()` bounds on weights/macros and strictly typed integer `intact_items` in `add_item()`, cleanly isolating SQLite `NOT NULL` violations from `AliasConflict`.

## Code Optimizations & Native C Extensions

To maximize planner throughput and responsiveness, several performance bottlenecks were identified, profiled, and eliminated:

1. **Native C Speedups Extension (`_speedups.c`):**
   - Implemented `round_down_to_hundredth` in C (`c_round_down`) for high-speed rounding during stock consumption, achieving a **4.5x - 8.3x speedup** over pure Python.
   - Implemented `sum_macros` in C (`py_sum_macros`) with single-pass struct/vector aggregation and interned Python Unicode strings, achieving a **5.7x - 6.9x speedup** over Python dictionary comprehensions.
   - Implemented `score` in C (`py_score`) for fast evaluation of candidate meals in `meal_planner/planner/day.py`.
   - Created `scripts/build_extensions.py` for automated compilation into `meal_planner/engine/_speedups.so`, with transparent, 100% backward-compatible pure-Python fallbacks when the C extension is not compiled.

2. **Copy-on-Write (COW) VirtualInventory:**
   - In `meal_planner/engine/virtual_inventory.py`, replaced full recursive deep copies with lazy Copy-on-Write semantics. `copy()` performs an instantaneous shallow dictionary copy, and row lists are cloned only upon calling `consume()` for the specific modified product ID.
   - Reduced tree search memory churn and achieved a **20.8x - 24.2x speedup** (from ~68 ms down to ~2.8 ms per 10,000 branch copies).

3. **Database Connection Reuse:**
   - Modified `get_exclusion_list` in `meal_planner/inventory.py` to accept an optional active SQLite connection `conn=conn`.
   - Wired `conn=conn` into `plan_day()` in `meal_planner/planner/day.py`, eliminating repeated file opens, locking, and disk sync overhead for a **14.6x - 24.8x speedup** per query.

4. **Planner Macro Memoization:**
   - Added an in-memory `_macro_cache` dictionary within `plan_day()` in `meal_planner/planner/day.py` keyed by `(Recipe_ID, group_choices)`.
   - Reused precomputed macronutrient breakdowns across both `preselection()` and recursive `explore()` calls, eliminating redundant nutritional recalculations.

5. **SQLite B-Tree Indexing:**
   - Added database indexes `idx_inventory_expiration` on `INVENTORY(Expiration_Date)`, `idx_inventory_product_loc` on `INVENTORY(ID, Location)`, `idx_plan_date_status` on `PLAN(Date, Status)`, and `idx_cookings_date` on `COOKINGS(Cooking_Date)` in `meal_planner/db/schema.py` and `meal_planner/db/recipe_schema.py`.
   - Converted full table scans into indexed B-tree lookups for near-instant query execution.

6. **Accelerated Receipt OCR Matching:**
   - In `meal_planner/receipts/reader.py`, integrated C++ `rapidfuzz.fuzz.ratio` with standard library `difflib` fallback, accelerating string similarity checks during receipt reconciliation.

7. **Verification & Benchmark Test Suite:**
   - Consolidated into `tests/test_domain_core.py` (`TestOptimizationBenchmarks`) containing automated verification tests for C vs. Python numerical equivalence, Copy-on-Write state isolation, and runtime speedup assertions across all optimized components.
