# Project: Hybrid Recipe Generation Pipeline (Slot + Food Pairing + ILP/CSP)

## Architecture
The system generates balanced culinary recipes matching user-specified meal types, nutritional goals (calories, protein, carbs, fat), and dietary constraints by extracting available food items from the local SQLite pantry inventory (`INVENTORY`, `PRODUCTS`, `ALIASES`), prioritizing opened packages and expiring items (FIFO), selecting structured grammatical slots from culinary archetypes, filtering incompatible combinations via a food pairing graph, and solving exact portion weights using mathematical linear programming (`scipy.optimize.linprog` with HiGHS simplex and pure-Python fallback).

```
                      [ SQLite Database: INVENTORY, PRODUCTS, ALIASES ]
                                            │
                                            ▼
                    [ meal_planner.generator.pipeline.RecipeGenerator ]
                                            │
     ┌──────────────────────────────────────┼──────────────────────────────────────┐
     ▼                                      ▼                                      ▼
[ taxonomy.py ]                        [ slots.py ]                        [ food_pairing.py ]
• Tier A: Semantic taxonomy            • 4 Archetypes                      • Hard mutual exclusions
• Tier B: Macro fingerprinting         • Slot definitions & counts         • Soft affinity matrix
• Multi-role portion bounds            • Physiological [min, max]          • Compatibility scoring
     │                                      │                                      │
     └──────────────────────────────────────┼──────────────────────────────────────┘
                                            ▼
                                   [ solver.py (ILP/CSP) ]
                                   • HiGHS linprog optimizer
                                   • Pure-Python bounded knapsack fallback
                                   • FIFO & opened package bonuses
                                   • Target Kcal ±5% bounds
                                            │
                                            ▼
                           [ RecipeCandidate Data Object ]
                           • Ingredient weights & units
                           • Generated Title & Step Instructions
                           • Exact macro totals
                                            │
                    ┌───────────────────────┴───────────────────────┐
                    ▼                                               ▼
       [ meal_planner.gui.dialogs ]                     [ meal_planner.recipes ]
       • RecipeGeneratorDialog                          • create_recipe() persistence
       • Interactive preview & edits                    • Atomic DB transaction
```

## Code Layout
- `meal_planner/generator/`:
  - `__init__.py`: Public exports (`RecipeGenerator`, `RecipeCandidate`, `IngredientCandidate`, `Archetype`, `Slot`).
  - `taxonomy.py`: Classification engine (Tier A dictionary, Tier B macro-fingerprinter, multi-role bounds).
  - `slots.py`: Archetype definitions, slot cardinalities, physiological min/max portions.
  - `food_pairing.py`: Mutual exclusions graph and soft affinity scoring.
  - `solver.py`: LP/CSP dosage optimizer (`scipy.optimize.linprog` HiGHS + pure-Python fallback).
  - `pipeline.py`: Main `RecipeGenerator` pipeline orchestrator, stock extractor, NLP titling and cooking instructions synthesis.
- `meal_planner/gui/app.py`: Recipes tab toolbar button "✨ Generate Recipe..." and dialog launcher.
- `meal_planner/gui/dialogs.py`: `RecipeGeneratorDialog` implementation with preview, editing, and save capabilities.
- `tests/test_recipe_generator.py`: Complete test suite covering taxonomy, archetypes, pairing, LP solver, pipeline, and GUI dialog.
- `scripts/setup_project.py`: Project runner registering the new test module.
- `docs/GUIDA_FUNZIONAMENTO.md`: User guide documentation.
- `docs/STORICO_MODIFICHE.md`: Project change log.

## Feature Inventory
| # | Feature | Description | Milestone | Source |
|---|---------|-------------|-----------|--------|
| 1 | Tier A Semantic Taxonomy | Dictionary mapping known food items and aliases to primary/secondary culinary roles | M1 | survey |
| 2 | Tier B Macro Fingerprinting | Mathematical classifier assigning culinary roles from 100g nutritional macros for custom foods | M1 | survey |
| 3 | Multi-Role Food Eligibility | Context-sensitive role and portion bounding (e.g. Parmigiano as aroma vs protein) | M1 | survey |
| 4 | Portion Range Bounds | Physiological upper and lower weight bounds per ingredient role | M1 | survey |
| 5 | Archetype 1: Grain / Pasta Dish | Primi Piatti archetype with Carb base, Fat seasoning, Veg sauce, Protein accent | M1 | survey |
| 6 | Archetype 2: Main & Side Plate | Secondi archetype with Protein main, Veg side, Fat seasoning, Carb side | M1 | survey |
| 7 | Archetype 3: One-Pot Balanced Bowl | Fitness/balanced bowl archetype with Carb base, Protein main, Veg, Fat | M1 | survey |
| 8 | Archetype 4: Breakfast / Snack Bowl | Colazione archetype with Dairy base, Cereal carb, Fruit, Nuts/Sweet | M1 | survey |
| 9 | Food Pairing Hard Exclusions | Mutual exclusion pruning rules (e.g. seafood + sweet dairy, pasta + sweet fruit) | M2 | survey |
| 10 | Food Pairing Soft Affinity Scoring | Flavor harmony weights between ingredient pairs to prioritize delicious combinations | M2 | survey |
| 11 | Pantry Stock Ingestion & FIFO | SQLite extraction sorting by opened packages first (`Open_Remainder > 0`) then expiration date | M2 | survey |
| 12 | HiGHS Linear Programming Solver | Optimal ingredient portion determination using `scipy.optimize.linprog(method='highs')` | M2 | survey |
| 13 | Pure-Python Knapsack Fallback | Resilient bounded continuous knapsack solver when scipy is unavailable or infeasible | M2 | survey |
| 14 | Caloric Window & Macro Bounds | Strict caloric convergence within $\pm 5\%$ of target and macro ratio bounds | M2 | survey |
| 15 | Dynamic Recipe Titling Synthesis | Natural culinary title generation based on main ingredients and archetype style | M2 | survey |
| 16 | Step-by-Step Cooking Instructions | Procedural recipe directions synthesis (prep, cook, combine, season, serve) | M2 | survey |
| 17 | Recipes Tab "✨ Generate Recipe..." | Toolbar button in Recipes tab launching the interactive generator modal | M3 | survey |
| 18 | RecipeGeneratorDialog Modal | Tkinter modal dialog with meal type, archetype, target macro selectors, and pantry toggle | M3 | survey |
| 19 | Live Preview & Ingredient Table | Calculated portion weights, to-taste tags, pantry vs missing status, macro meters | M3 | survey |
| 20 | Atomic Recipe Persistence | Commits generated recipes to `RECIPES`, `RECIPE_MEALS`, `RECIPE_INGREDIENTS` via `create_recipe` | M3 | survey |
| 21 | E2E Testing Suite (Tiers 1-4) | Opaque-box and feature unit test suite in `tests/test_recipe_generator.py` | M4 | survey |
| 22 | Technical Documentation Updates | Italian documentation updates in `GUIDA_FUNZIONAMENTO.md` and `STORICO_MODIFICHE.md` | M5 | survey |

## Milestones
| # | Name | Scope | Dependencies | Status |
|---|------|-------|-------------|--------|
| M1 | Generator Foundation & Taxonomy | `meal_planner/generator/__init__.py`, `taxonomy.py`, `slots.py` | none | IN_PROGRESS |
| M2 | Pairing & Optimization Pipeline | `meal_planner/generator/food_pairing.py`, `solver.py`, `pipeline.py` | M1 | PLANNED |
| M3 | GUI Integration | `meal_planner/gui/app.py`, `meal_planner/gui/dialogs.py` | M1, M2 | PLANNED |
| M4 | E2E Testing Track & Runner Integration | `tests/test_recipe_generator.py`, `scripts/setup_project.py` | M1, M2, M3 | PLANNED |
| M5 | Final Hardening & Documentation | `docs/GUIDA_FUNZIONAMENTO.md`, `docs/STORICO_MODIFICHE.md`, full test pass | M1, M2, M3, M4 | PLANNED |

## Interface Contracts

### `meal_planner.generator.taxonomy`
- `classify_food(name: str, kcal_100g: float, protein_100g: float, carbs_100g: float, fat_100g: float, fiber_100g: float = 0.0, sugars_100g: float = 0.0) -> List[Role]`
- `get_portion_bounds(role: Role, food_name: str = "") -> Tuple[float, float]`

### `meal_planner.generator.slots`
- `Archetype(name: str, required_slots: Dict[Role, int], optional_slots: Dict[Role, int], default_meal: str)`
- `ARCHETYPES: Dict[str, Archetype]`

### `meal_planner.generator.food_pairing`
- `are_mutually_exclusive(item_a: str, roles_a: Set[Role], item_b: str, roles_b: Set[Role]) -> bool`
- `get_pairing_score(item_a: str, item_b: str) -> float` (returns $0.0 \dots 1.0$)

### `meal_planner.generator.solver`
- `solve_ingredient_quantities(candidates: List[IngredientCandidate], target_kcal: float, macro_bounds: Optional[Dict[str, Tuple[float, float]]] = None) -> SolverResult`

### `meal_planner.generator.pipeline`
- `RecipeGenerator.generate_recipe(target_kcal: float, meal_type: str = "lunch", archetype_name: Optional[str] = None, pantry_only: bool = True, db_path: Optional[str] = None) -> RecipeCandidate`

### `meal_planner.gui.dialogs`
- `RecipeGeneratorDialog(parent, db_path: Optional[str] = None, wait: bool = True, on_save_callback: Optional[Callable] = None)`
