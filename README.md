# Meal Planner

**Meal Planner** is a 100% offline desktop application (`Tkinter` + `sv-ttk` + `SQLite`) for pantry management, receipt OCR ingestion, intelligent recipe generation, and multi-day nutritional meal planning with FIFO (First-In, First-Out) and opened-package prioritization.

---

## Table of Contents

1. [Quick Start & CLI](#1-quick-start--cli)
2. [Navigation & Application Tabs](#2-navigation--application-tabs)
3. [Key Features & Functional Modules](#3-key-features--functional-modules)
4. [Hybrid Recipe Generation Pipeline](#4-hybrid-recipe-generation-pipeline)
5. [Project Layout](#5-project-layout)
6. [Architecture & Performance Optimizations](#6-architecture--performance-optimizations)
7. [Testing & Verification](#7-testing--verification)
8. [Documentation](#8-documentation)

---

## 1. Quick Start & CLI

### Launch the GUI Application

```bash
python -m meal_planner          # Opens on the Pantry tab; uses data/inventory.db
```

The database is stored exclusively in `data/inventory.db` and can be overridden via the `MEAL_PLANNER_DB` environment variable.

### Setup, Demo Seeding & Test Suite

```bash
python -m scripts.setup_project              # First-time setup (creates schema & demo data if absent)
python -m scripts.setup_project --test       # Verifies DB integrity and runs all test suites on isolated DBs
python -m scripts.setup_project --reset -y   # Wipes and reloads baseline demo products, inventory & recipes
python -m scripts.setup_project --launch     # Runs setup and launches the GUI window
```

### Receipt Pipeline Demo Launcher

```bash
python -m scripts.demo_receipt --drop        # Launches GUI and simulates drag-and-drop receipt upload
python -m scripts.demo_receipt --reconcile   # Opens ReceiptReconciliationDialog directly with sample items
```

### Optional Command-Line Interface

```bash
python -m meal_planner.engine profile --sex M --activity moderate --goal maintenance
python -m meal_planner.engine recipes
python -m meal_planner.engine day "Yogurt, apple and milk" "Tomato spaghetti" "Chicken and rice" --kcal 2500
python -m meal_planner.planner day --kcal 2000 --lock lunch="Chicken and rice" --skip dinner --save
python -m meal_planner.planner week --start 2026-10-26 --days 7 --save
python -m meal_planner.planner shopping
python -m meal_planner.recipes import recipes.json
```

---

## 2. Navigation & Application Tabs

The interface is localized **100% in English**, opens by default on the **Pantry** tab, and supports both persistent default themes (`Settings`) and instant session-level **Dark ⇄ Light mode** toggling from the header bar:

1. **Pantry** — View and manage inventory stock with color-coded expiration badges, per-row soft-coral delete buttons (`X`), double-click editing (`InventoryItemEditDialog`), storage location management (`LOCATIONS`), and drag-and-drop receipt OCR ingestion.
2. **Shopping** — Displays missing quantities required by the active meal plan or consumed beyond available stock (`SHOPPING_LIST_VIEW`).
3. **Recipes** — Full-width recipe table showing per-serving macronutrients (`kcal`, `Prot`, `Carb`, `Fat`, `Fiber`), open-package share (`Open`), and stock feasibility:
   - `➕ New Recipe...` (`AddRecipeDialog`): keyboard-driven entry (`Enter` chaining, `Delete` removal, `Ctrl+S` / `Ctrl+Enter` save), multi-serving scaling (`Servings`), preparation notes, `"To taste (q.b.)"` support, and on-the-fly product registration (`QuickProductDialog`).
   - `✨ Generate Recipe...` (`RecipeGeneratorDialog`): deterministic hybrid recipe generator combining culinary slots, food pairing, and linear programming (`scipy.optimize.linprog`).
   - `👁️ Recipe Details...` (`RecipeDetailsDialog`) or double-click any row: non-destructive inspection of meals, notes, ingredients, and live macros from `RECIPE_MACROS_VIEW`.
   - `📂 Import from file (.json / .csv)...` and `🗑️ Delete Selected Recipe`.
4. **Plan** — Propose daily or multi-day meal plans (*Propose plan*, *Save plan*, *Mark as cooked*, *Schedule Meal Prep*), lock specific meals, or skip meals while respecting variety and shelf-life priority.
5. **Profile** — Dynamic weight log, BMI calculation, metabolic estimates (BMR, maintenance, caloric target), and an interactive debounced weight history chart (`LineChart`). Displays a clean rounded empty-state overlay (`"Hello, add your profile to get started"`) with `ProfileSetupDialog` when no profile is configured.
6. **Settings** — Non-monolithic 4-section controller:
   - **Style & Config**: Default startup theme (`dark` / `light`), expiration warning days, and receipt output directory stored in SQLite `APP_SETTINGS`.
   - **Full Profile**: Static metrics (sex, height) and complete read-only nutritional summary.
   - **Receipt Logs**: Audit history of processed receipts (`RECEIPT_LOGS`).
   - **Destructive Actions**: Confirmation-guarded *Hard-Delete Entire Inventory* and *Reset User Profile*.

---

## 3. Key Features & Functional Modules

* **Interactive Pantry Row Editing & Macro Disambiguation (`InventoryItemEditDialog` & `MacroPromptDialog`):**
  Double-clicking any pantry row edits stock quantities, locations, expiration dates, conversion factors (`Grams_per_piece`, `Density_kg_per_L`), and 100g nutritional profiles. If modified macros affect shared aliases or recipes, `MacroPromptDialog` lets the user choose between:
  - **Option 1 (Isolate to this item only):** Creates a dedicated `Product_ID` for the single item.
  - **Option 2 (Propagate globally):** Updates the shared `PRODUCTS` record across all linked items and recipes.
* **4x Supersampled Anti-Aliased Delete Badges:**
  Custom Pillow-rendered rounded delete icons (`create_delete_icon`) using 4x Lanczos supersampling and a WCAG-compliant soft coral/rose palette (`#2d1f22` / `#f87171` in Dark mode; `#fbebec` / `#c94a53` in Light mode) with automatic Tcl token propagation across theme switches.
* **Two-Step Receipt OCR & Catalog Reconciliation Pipeline (`meal_planner/receipts/`):**
  Drag-and-drop or browse receipt images $\to$ OCR review (`FinestraModifica`) $\to$ interactive catalog matching (`ReceiptReconciliationDialog`) with cross-dimensional unit conversion (`pcs` $\leftrightarrow$ `kg` $\leftrightarrow$ `L`) and atomic SQLite commits.
* **Advance Meal Prep & Leftover Scheduling:**
  Schedule multi-serving cooks via `schedule_meal_prep()` and `MealPrepDialog`, tracking leftover servings in `COOKINGS` and enforcing ingredient variety (`No_repeat_days`) and expiring-stock priority (`EXCLUSION_LIST`).

### Recipe Import Formats (per 1 serving)

```json
[{"name": "Chicken and rice", "meals": ["lunch", "dinner"],
  "ingredients": [{"product": "Rice", "quantity": 100, "unit": "g"}]}]
```

```text
CSV (";" or ","): recipe;meals;product;quantity;unit;to_taste        (meals separated by "|")
```

---

## 4. Hybrid Recipe Generation Pipeline

The [`meal_planner/generator/`](meal_planner/generator/) package synthesizes complete, nutritionally balanced recipes **100% offline** through a 3-stage architecture:

1. **Two-Tier Taxonomy & Culinary Slot Grammar (`taxonomy.py`, `slots.py`):**
   - **Tier A (Canonical Semantic Dictionary)** + **Tier B (Automated Macro Fingerprinting)**: classifies any known or custom user product into culinary roles (`CARB_BASE`, `PROTEIN_MAIN`, `VEGETABLE`, `FAT_SEASONING`, `DAIRY_BASE`, `FRUIT`, `AROMA_TASTE`) with multi-role eligibility (e.g. *Parmigiano Reggiano* as finishing aroma or main protein; *Eggs* as protein or binder).
   - **4 Structured Archetypes**: `Grain / Pasta Dish`, `Main & Side Plate`, `One-Pot Balanced Bowl`, and `Breakfast / Snack Bowl`, each enforcing physiological portion bounds `[min_g, max_g]`.
2. **Food Pairing Graph (`food_pairing.py`):**
   - Enforces **17 hard mutual exclusion rules** (e.g. seafood with sweet dairy, cooking oils in sweet breakfast bowls, raw flour in cold dishes) and ranks combinations via a 4-tier flavor harmony matrix with `lru_cache` memoization.
3. **ILP / CSP Mathematical Dosage Solver (`solver.py`, `pipeline.py`):**
   - Solves exact gram dosages via `scipy.optimize.linprog(method='highs')` (with a pure-Python continuous knapsack fallback) to match target meal calories within $\pm 5\%$ while prioritizing opened packages (`Open_Remainder > 0`) and near-expiry FIFO stock.
   - Synchronizes post-quantization display quantities (`quantized_grams()`) with SQLite `RECIPE_MACROS_VIEW` and synthesizes English recipe titles and 4-step cooking instructions.

---

## 5. Project Layout

```text
meal_planner_project/
├── data/
│   └── inventory.db              # Primary SQLite database
├── docs/
│   ├── GUIDA_FUNZIONAMENTO.md    # Comprehensive visual & architectural manual (14 sections)
│   ├── STORICO_MODIFICHE.md      # Complete chronological engineering changelog (17 phases)
│   └── images/                   # 16 automated headless UI screenshots
├── meal_planner/
│   ├── config.py                 # Thread-safe in-memory cached settings backed by APP_SETTINGS
│   ├── inventory.py              # Pantry CRUD, FIFO consumption, locations, unit conversions
│   ├── recipes.py                # Recipe creation, on-the-fly product registration, CSV/JSON import
│   ├── db/
│   │   ├── connection.py         # SQLite connection manager with WAL & directory caching
│   │   ├── schema.py             # Unified idempotent schema (20 tables, views, triggers, indices)
│   │   └── recipe_schema.py      # Backward-compatible schema facade
│   ├── engine/
│   │   ├── catalog.py            # Indexed product & alias loading
│   │   ├── nutrition.py          # Factorized macro calculations & nutritional goal validation
│   │   ├── virtual_inventory.py  # Copy-on-Write (COW) in-memory inventory simulation
│   │   ├── profile.py            # BMR, BMI, caloric estimates & profile persistence
│   │   └── _speedups.c           # Optional C extension for rounding, macro summation & scoring
│   ├── generator/
│   │   ├── taxonomy.py           # Two-tier food role classifier & macro fingerprinter
│   │   ├── slots.py              # Culinary archetypes, slot cardinalities & portion bounds
│   │   ├── food_pairing.py       # Flavor affinity graph & 17 hard mutual exclusions
│   │   ├── solver.py             # SciPy HiGHS LP optimizer & pure-Python knapsack fallback
│   │   └── pipeline.py           # End-to-end RecipeGenerator, titling & step synthesis
│   ├── planner/
│   │   ├── day.py                # Daily meal planner with bulk ingredient loading & memoization
│   │   ├── week.py               # Multi-day planner with variety & expiration bonuses
│   │   └── storage.py            # Plan persistence, shopping list sync & meal prep scheduling
│   ├── receipts/
│   │   ├── reader.py             # Pre-compiled regex OCR parser & fuzzy catalog matching
│   │   └── pipeline.py           # Two-step receipt review & reconciliation pipeline
│   └── gui/
│       ├── app.py                # Main application window & lazy tab refresh controller
│       ├── dialogs.py            # Modal dialogs (AddRecipe, RecipeDetails, RecipeGenerator, etc.)
│       ├── widgets.py            # Custom Table (antialiased delete icon) & debounced LineChart
│       ├── theme.py              # Dark/Light theme loader & palette definitions
│       └── support.py            # @safe error decorator, DB context manager & numeric parser
├── scripts/
│   ├── setup_project.py          # Environment setup, DB integrity verifier & isolated test runner
│   ├── demo_receipt.py           # Unified receipt drag-and-drop & reconciliation demo
│   ├── capture_docs_screenshots.py # Automated headless screenshot generator for docs/images/
│   ├── seed_inventory.py         # Demo products & inventory seeder
│   ├── seed_recipes.py           # Demo recipes seeder
│   └── build_extensions.py       # C extension builder for meal_planner/engine/_speedups.so
└── tests/                        # 547 automated offline tests across 10 test modules
```

---

## 6. Architecture & Performance Optimizations

1. **In-Memory Configuration & SQLite Connection Caching (`config.py`, `db/connection.py`):**
   - Thread-safe `_CACHED_CONFIG` turns `config.get()` into $O(1)$ memory lookups with atomic invalidation on write; directory verification (`_VERIFIED_DIRS`) and selective `PRAGMA journal_mode = WAL` eliminate redundant filesystem I/O.
2. **Copy-on-Write (COW) `VirtualInventory` & Batched Writes (`engine/virtual_inventory.py`):**
   - Lazy shallow dictionary copies clone row lists only on `consume()`, achieving a **20.8x – 24.2x speedup** during planner tree search, while `write()` batches updates via `conn.executemany()`.
3. **Bulk SQL Queries & Factorized Nutrition Math (`planner/day.py`, `engine/nutrition.py`, `recipes.py`):**
   - Eliminated $N+1$ query patterns in daily/weekly planning and recipe imports via single-pass bulk queries and `UNION ALL` existence checks; factorized `g / 100.0` scalar multiplications across all 8 macronutrients.
4. **Memoized Food Pairing Graph & LP Solver (`generator/food_pairing.py`, `generator/solver.py`):**
   - `@functools.lru_cache` on canonical token resolution, culinary tag extraction, and role inference enables 80-item pantry recipe generation in **< 0.15s**.
5. **GUI Responsiveness & Lazy Evaluation (`gui/app.py`, `gui/widgets.py`, `gui/dialogs.py`):**
   - Deferred recipe feasibility simulation (`_recipes_dirty`) until the user views the `Recipes` tab; `after_idle()` resize debouncing and precomputed date ordinals in `LineChart`; cached flash styles and container-level scroll bindings in `ReceiptReconciliationDialog`.
6. **Optional Native C Extension (`_speedups.c`):**
   - Provides C-accelerated `c_round_down`, `py_sum_macros`, and `py_score` with transparent pure-Python fallbacks.

---

## 7. Testing & Verification

All **547 tests** execute **100% offline** against isolated temporary databases, preserving the golden baseline in `data/inventory.db` (`36 inventory`, `14 products`, `3 recipes`, `30 aliases`, `0 profile`, `3 settings`):

```bash
# Run the primary consolidated suites via the project setup runner:
.venv/bin/python -m scripts.setup_project --test

# Or discover and run the entire 547-test suite directly:
.venv/bin/python -m unittest discover -s tests
```

### Test Suites Overview

* `tests/test_domain_core.py` — Config, inventory CRUD, nutrition engine, planner & C/COW benchmarks.
* `tests/test_receipts_and_reconciliation.py` — OCR reader, drop-zone ingestion, unit conversions & reconciliation dialog.
* `tests/test_inventory_management.py` — Row-level editing, macro disambiguation (Option 1 vs 2), delete badges & FIFO consumption.
* `tests/test_recipe_management.py` — Recipe creation, on-the-fly products, keyboard shortcuts, `RecipeDetailsDialog` & CSV/JSON import.
* `tests/test_settings_and_profile.py` — 4-section Settings controller, theme persistence, clean Profile overlay & destructive action guardrails.
* `tests/test_e2e_and_invariants.py` — End-to-end workflows, foreign key constraints, finite-float hardening & concurrency invariants.
* `tests/test_recipe_generator.py` — **75 tests** covering taxonomy fingerprinting, slot bounds, 17 food pairing exclusions, HiGHS/knapsack solver precision, `RecipeGeneratorDialog` GUI workflows, high-volume pantry stress tests, and SQLite `RECIPE_MACROS_VIEW` mathematical identity.
* `tests/test_e2e_theme_and_efficiency.py`, `tests/test_adversarial_theme_stress.py`, `tests/test_gui_efficiency_and_localization.py` — Bidirectional Dark/Light theme stress tests (100 rapid toggles, 500+ rows), config caching & 100% English UI verification.

---

## 8. Documentation

* **[`docs/GUIDA_FUNZIONAMENTO.md`](docs/GUIDA_FUNZIONAMENTO.md)** — Complete functional and architectural guide with 16 embedded screenshots in [`docs/images/`](docs/images/).
* **[`docs/STORICO_MODIFICHE.md`](docs/STORICO_MODIFICHE.md)** — Detailed chronological engineering log across all 17 evolutionary phases of the project.
