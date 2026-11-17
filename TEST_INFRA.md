# E2E Test Infra: Hybrid Recipe Generation Pipeline

## Test Philosophy
- Opaque-box, requirement-driven. Derived from `ORIGINAL_REQUEST.md` and approved architectural plan.
- Methodology: Category-Partition + Boundary Value Analysis (BVA) + Pairwise Combinatorial + Real-World Workload Testing.
- Strictly offline, using isolated ephemeral in-memory or temporary SQLite test databases.

## Feature Inventory & Test Coverage Goals
| # | Feature | Source (requirement) | Tier 1 | Tier 2 | Tier 3 |
|---|---------|---------------------|:------:|:------:|:------:|
| 1 | Tier A Semantic Taxonomy | ORIGINAL_REQUEST §1, plan §2.2 | 5 | 5 | ✓ |
| 2 | Tier B Macro Fingerprinting | ORIGINAL_REQUEST §1, plan §2.2 | 5 | 5 | ✓ |
| 3 | Multi-Role Food Eligibility | ORIGINAL_REQUEST §1, plan §2.2 | 5 | 5 | ✓ |
| 4 | Portion Range Bounds | ORIGINAL_REQUEST §1, plan §2.3 | 5 | 5 | ✓ |
| 5 | Archetype 1: Grain / Pasta Dish | ORIGINAL_REQUEST §1, plan §2.3 | 5 | 5 | ✓ |
| 6 | Archetype 2: Main & Side Plate | ORIGINAL_REQUEST §1, plan §2.3 | 5 | 5 | ✓ |
| 7 | Archetype 3: One-Pot Balanced Bowl | ORIGINAL_REQUEST §1, plan §2.3 | 5 | 5 | ✓ |
| 8 | Archetype 4: Breakfast / Snack Bowl | ORIGINAL_REQUEST §1, plan §2.3 | 5 | 5 | ✓ |
| 9 | Food Pairing Hard Exclusions | ORIGINAL_REQUEST §1, plan §3 | 5 | 5 | ✓ |
| 10 | Food Pairing Soft Affinity Scoring | ORIGINAL_REQUEST §1, plan §3 | 5 | 5 | ✓ |
| 11 | Pantry Stock Ingestion & FIFO | ORIGINAL_REQUEST §1, plan §2.1 | 5 | 5 | ✓ |
| 12 | HiGHS Linear Programming Solver | ORIGINAL_REQUEST §1, plan §4 | 5 | 5 | ✓ |
| 13 | Pure-Python Knapsack Fallback | ORIGINAL_REQUEST §1, plan §4 | 5 | 5 | ✓ |
| 14 | Caloric Window & Macro Bounds | ORIGINAL_REQUEST §1, plan §4 | 5 | 5 | ✓ |
| 15 | Dynamic Recipe Titling Synthesis | ORIGINAL_REQUEST §1, plan §1 | 5 | 5 | ✓ |
| 16 | Step-by-Step Cooking Instructions | ORIGINAL_REQUEST §1, plan §1 | 5 | 5 | ✓ |
| 17 | Recipes Tab "✨ Generate Recipe..." | ORIGINAL_REQUEST §2, plan §5 | 5 | 5 | ✓ |
| 18 | RecipeGeneratorDialog Modal | ORIGINAL_REQUEST §3, plan §5 | 5 | 5 | ✓ |
| 19 | Live Preview & Ingredient Table | ORIGINAL_REQUEST §3, plan §5 | 5 | 5 | ✓ |
| 20 | Atomic Recipe Persistence | ORIGINAL_REQUEST §3, plan §5 | 5 | 5 | ✓ |

## Test Architecture
- Test runner: `tests/test_recipe_generator.py` executed via `.venv/bin/python -m unittest` and `scripts/setup_project.py --test`.
- Pass/Fail Semantics: 100% assertions pass, 0 errors, 0 regressions, exit code 0.
- Database isolation: Tests create ephemeral test databases using `tempfile.NamedTemporaryFile` or SQLite memory, never modifying `data/inventory.db`.

## Real-World Application Scenarios (Tier 4)
| # | Scenario | Features Exercised | Complexity |
|---|----------|--------------------|------------|
| 1 | Standard Lunch Pasta Generation with Pantry FIFO | F1, F4, F5, F10, F11, F12, F14, F15, F16 | High |
| 2 | High-Protein Dinner Plate with Custom Unseen Ingredient | F2, F3, F6, F9, F12, F14, F15, F16 | High |
| 3 | Breakfast Bowl Generation with Yogurt & Fruit | F1, F8, F9, F10, F12, F14, F15, F16 | Medium |
| 4 | Offline Fallback (No Scipy / Infeasible LP recovery) | F13, F14, F15, F16 | High |
| 5 | Full GUI Headless Flow: Open Dialog, Generate, Preview, Save to Database | F17, F18, F19, F20 | High |

## Coverage Thresholds
- Tier 1: Feature Coverage (≥5 per feature)
- Tier 2: Boundary & Corner Cases (empty pantry, extreme kcal targets, near-zero inventory, single ingredient)
- Tier 3: Pairwise Combinations (interactions between archetypes, pairing rules, and solver tolerances)
- Tier 4: Realistic E2E Application Scenarios
