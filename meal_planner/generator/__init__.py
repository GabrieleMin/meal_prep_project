"""meal_planner.generator - Deterministic Hybrid Recipe Generation Pipeline.

Architecture:
1. Slot Architecture (Culinary Grammar & 4 Archetypes)
2. Food Pairing Graph (Flavor Harmony & Hard Exclusions)
3. Mathematical ILP/CSP Optimization (scipy HiGHS Simplex + pure-Python knapsack fallback)
4. Dynamic Recipe Titling & Cooking Instructions Synthesis
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union

# Taxonomy and classification
from .taxonomy import (
    DEFAULT_PORTION_BOUNDS,
    Role,
    ROLE_COMPATIBILITY,
    are_roles_compatible,
    classify_food,
    fingerprint_macros,
    get_portion_bounds,
    is_role_compatible,
)

# Slots and Archetypes
from .slots import (
    ARCHETYPE_ALIASES,
    ARCHETYPES,
    BREAKFAST_SNACK_BOWL,
    GRAIN_PASTA_DISH,
    MAIN_AND_SIDE_PLATE,
    MEAL_ARCHETYPES,
    ONE_POT_BALANCED_BOWL,
    Archetype,
    Slot,
    SlotValidationResult,
    SlotsDict,
    clamp_ingredient_portion,
    get_archetype,
    get_archetypes_for_meal,
    get_default_archetype_for_meal,
    get_slot_for_role,
    validate_slot_filling,
)


@dataclass
class IngredientCandidate:
    """Candidate ingredient extracted from pantry inventory or catalog fallback.

    Attributes:
        product_id: ID of the product in SQLite PRODUCTS.
        name: Name of the product or alias.
        role: Primary culinary grammatical Role.
        unit: Unit of measure ('g', 'kg', 'pcs', 'L', 'ml').
        kcal_100g: Energy content in kcal per 100g.
        protein_100g: Protein in grams per 100g.
        carbs_100g: Carbohydrates in grams per 100g.
        fat_100g: Total fat in grams per 100g.
        fiber_100g: Dietary fiber in grams per 100g.
        sugars_100g: Sugars in grams per 100g.
        grams_per_piece: Weight of one discrete piece in grams if unit == 'pcs'.
        density_kg_per_l: Density in kg/L if liquid.
        available_stock_g: Available pantry stock converted to grams.
        open_remainder: Opened remainder quantity in natural units.
        is_open: True if open_remainder > 0.
        days_to_expiration: Days until expiration date (None if no expiration).
        is_missing: True if not present in pantry stock.
        to_taste: True for micro-condiments ("q.b.").
        portion_min_g: Minimum portion bound in grams.
        portion_max_g: Maximum portion bound in grams.
        cost_coefficient: Objective cost weight for linear programming.
        resolved_quantity_g: Optimal portion solved by the optimizer in grams.
        all_eligible_roles: All roles this product can fulfill.
        min_g: Alias/property for portion_min_g.
        max_g: Alias/property for portion_max_g.
        available_g: Alias/property for available_stock_g.
        days_to_expiry: Alias/property for days_to_expiration.
        pairing_score: Pairwise or slot flavor pairing score.
    """

    product_id: int = 0
    name: str = ""
    role: Role = Role.CARB_BASE
    unit: str = "g"
    kcal_100g: float = 0.0
    protein_100g: float = 0.0
    carbs_100g: float = 0.0
    fat_100g: float = 0.0
    fiber_100g: float = 0.0
    sugars_100g: float = 0.0
    grams_per_piece: Optional[float] = None
    density_kg_per_l: Optional[float] = None
    available_stock_g: float = 0.0
    open_remainder: float = 0.0
    is_open: bool = False
    days_to_expiration: Optional[float] = None
    is_missing: bool = False
    to_taste: bool = False
    portion_min_g: float = 0.0
    portion_max_g: float = 0.0
    cost_coefficient: float = 0.0
    resolved_quantity_g: float = 0.0
    all_eligible_roles: List[Role] = field(default_factory=list)
    min_g: Optional[float] = None
    max_g: Optional[float] = None
    available_g: Optional[float] = None
    days_to_expiry: Optional[float] = None
    pairing_score: float = 0.5

    def __post_init__(self) -> None:
        if isinstance(self.role, str) and not isinstance(self.role, Role):
            try:
                self.role = Role(self.role)
            except ValueError:
                pass
        # Harmonize min_g <-> portion_min_g
        if self.min_g is not None:
            self.portion_min_g = float(self.min_g)
        else:
            self.min_g = float(self.portion_min_g)
        # Harmonize max_g <-> portion_max_g
        if self.max_g is not None:
            self.portion_max_g = float(self.max_g)
        else:
            self.max_g = float(self.portion_max_g)
        # Harmonize available_g <-> available_stock_g
        if self.available_g is not None:
            self.available_stock_g = float(self.available_g)
        else:
            self.available_g = float(self.available_stock_g)
        # Harmonize days_to_expiry <-> days_to_expiration
        if self.days_to_expiry is not None:
            self.days_to_expiration = float(self.days_to_expiry)
        else:
            self.days_to_expiry = self.days_to_expiration
        # Open package flag
        if self.open_remainder > 0:
            self.is_open = True
        if not self.all_eligible_roles:
            self.all_eligible_roles = [self.role] if isinstance(self.role, Role) else []

    def calculate_display_quantity(self) -> Tuple[float, str]:
        """Converts resolved_quantity_g to the product's natural unit and quantity."""
        raw_g = self.resolved_quantity_g if self.resolved_quantity_g > 0 else (self.portion_min_g or 1.0)
        u = self.unit.strip().lower()
        if u == "kg":
            return max(0.001, round(raw_g / 1000.0, 3)), "kg"
        if u == "g":
            return max(0.1, round(raw_g, 1)), "g"
        if u in ("pcs", "pz"):
            gpp = self.grams_per_piece or 50.0
            return max(1.0, float(round(raw_g / gpp))), "pcs"
        if u in ("l", "liter"):
            density = self.density_kg_per_l or 1.0
            vol_l = raw_g / (1000.0 * density)
            return max(0.001, round(vol_l, 3)), "L"
        if u in ("ml", "cl"):
            density = self.density_kg_per_l or 1.0
            vol_ml = raw_g / density
            return max(0.1, round(vol_ml, 1)), "ml"
        return max(0.1, round(raw_g, 1)), self.unit

    def quantized_grams(self) -> float:
        """Returns the exact gram weight corresponding to calculate_display_quantity() (matching SQLite INGREDIENT_GRAMS_VIEW)."""
        qty, unit = self.calculate_display_quantity()
        u = unit.strip().lower()
        if u == "kg":
            return round(qty * 1000.0, 2)
        if u == "g":
            return round(qty, 2)
        if u in ("pcs", "pz"):
            return round(qty * (self.grams_per_piece or 50.0), 2)
        if u in ("l", "liter"):
            return round(qty * 1000.0 * (self.density_kg_per_l or 1.0), 2)
        if u in ("ml", "cl"):
            return round(qty * (self.density_kg_per_l or 1.0), 2)
        if u == "tbsp":
            return round(qty * 15.0 * (self.density_kg_per_l or 1.0), 2)
        if u == "tsp":
            return round(qty * 5.0 * (self.density_kg_per_l or 1.0), 2)
        return round(qty, 2)

    def compute_macros_for_quantity(self, grams: Optional[float] = None) -> Dict[str, float]:
        """Calculates actual nutritional macros for the given quantity in grams."""
        g = self.resolved_quantity_g if grams is None else grams
        factor = max(0.0, g) / 100.0
        return {
            "Kcal": round(self.kcal_100g * factor, 1),
            "Protein": round(self.protein_100g * factor, 1),
            "Carbs": round(self.carbs_100g * factor, 1),
            "Fat": round(self.fat_100g * factor, 1),
            "Fiber": round(self.fiber_100g * factor, 1),
            "Sugars": round(self.sugars_100g * factor, 1),
        }

    def to_recipe_ingredient_dict(self) -> Dict[str, Any]:
        """Converts to format expected by meal_planner.recipes.create_recipe."""
        qty, unit = self.calculate_display_quantity()
        return {
            "product": self.name,
            "quantity": qty,
            "unit": unit,
            "to_taste": self.to_taste,
        }


@dataclass
class SolverResult:
    """Result of linear programming optimization or pure-Python knapsack fallback."""

    success: bool
    quantities_g: Dict[int, float] = field(default_factory=dict)
    total_kcal: float = 0.0
    total_protein: float = 0.0
    total_carbs: float = 0.0
    total_fat: float = 0.0
    total_fiber: float = 0.0
    solver_name: str = "highs"  # 'highs' or 'knapsack_fallback'
    message: str = ""
    iterations: int = 0
    quantities: Dict[str, float] = field(default_factory=dict)
    quantities_by_idx: List[float] = field(default_factory=list)


@dataclass
class RecipeCandidate:
    """Complete generated recipe proposal ready for GUI display and SQLite persistence."""

    title: str
    archetype_name: str
    meal_type: str = "lunch"
    ingredients: List[IngredientCandidate] = field(default_factory=list)
    instructions: List[str] = field(default_factory=list)
    notes: str = ""
    target_kcal: float = 0.0
    total_kcal: float = 0.0
    total_protein: float = 0.0
    total_carbs: float = 0.0
    total_fat: float = 0.0
    total_fiber: float = 0.0
    caloric_delta_pct: float = 0.0
    pantry_only: bool = True
    solver_status: str = "optimal"
    warnings: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.notes and self.instructions:
            self.notes = "\n".join(f"{i+1}. {step}" for i, step in enumerate(self.instructions))
        if self.target_kcal > 0 and self.total_kcal > 0:
            self.caloric_delta_pct = round(
                abs(self.total_kcal - self.target_kcal) / self.target_kcal * 100.0, 2
            )

    def sync_macros_with_display_quantities(self) -> None:
        """Synchronizes resolved_quantity_g and macro totals with quantized display units (matching SQLite RECIPE_MACROS_VIEW)."""
        if not self.ingredients:
            return
        tot_kcal = 0.0
        tot_p = 0.0
        tot_c = 0.0
        tot_f = 0.0
        tot_fib = 0.0
        for ing in self.ingredients:
            qg = ing.quantized_grams()
            ing.resolved_quantity_g = qg
            factor = max(0.0, qg) / 100.0
            tot_kcal += ing.kcal_100g * factor
            tot_p += ing.protein_100g * factor
            tot_c += ing.carbs_100g * factor
            tot_f += ing.fat_100g * factor
            tot_fib += ing.fiber_100g * factor
        self.total_kcal = round(tot_kcal, 1)
        self.total_protein = round(tot_p, 1)
        self.total_carbs = round(tot_c, 1)
        self.total_fat = round(tot_f, 1)
        self.total_fiber = round(tot_fib, 1)
        if self.target_kcal > 0 and self.total_kcal > 0:
            self.caloric_delta_pct = round(
                abs(self.total_kcal - self.target_kcal) / self.target_kcal * 100.0, 2
            )

    def to_create_recipe_kwargs(self) -> Dict[str, Any]:
        """Returns kwargs dict ready for meal_planner.recipes.create_recipe(conn, **kwargs)."""
        return {
            "name": self.title,
            "meals": [self.meal_type],
            "ingredients": [ing.to_recipe_ingredient_dict() for ing in self.ingredients],
            "notes": self.notes,
        }


# Food pairing engine
from .food_pairing import (
    CulinaryTag,
    are_mutually_exclusive,
    get_pairing_score,
    resolve_canonical,
    score_combination,
)

# Mathematical portion optimization solver
from .solver import (
    compute_ingredient_cost,
    solve_ingredient_quantities,
)

# Pipeline orchestrator
from .pipeline import (
    RecipeGenerator,
    clean_display_name,
    extract_catalog_candidates,
    extract_pantry_candidates,
    synthesize_cooking_instructions,
    synthesize_recipe_title,
)


__all__ = [
    # Roles & Taxonomy
    "Role",
    "ROLE_COMPATIBILITY",
    "are_roles_compatible",
    "is_role_compatible",
    "classify_food",
    "fingerprint_macros",
    "get_portion_bounds",
    "DEFAULT_PORTION_BOUNDS",
    # Slots & Archetypes
    "Slot",
    "SlotsDict",
    "Archetype",
    "ARCHETYPES",
    "ARCHETYPE_ALIASES",
    "MEAL_ARCHETYPES",
    "GRAIN_PASTA_DISH",
    "MAIN_AND_SIDE_PLATE",
    "ONE_POT_BALANCED_BOWL",
    "BREAKFAST_SNACK_BOWL",
    "get_archetype",
    "get_archetypes_for_meal",
    "get_default_archetype_for_meal",
    "get_slot_for_role",
    "clamp_ingredient_portion",
    "validate_slot_filling",
    "SlotValidationResult",
    # Candidates & Optimization Models
    "IngredientCandidate",
    "SolverResult",
    "RecipeCandidate",
    # Food Pairing (M2)
    "CulinaryTag",
    "resolve_canonical",
    "are_mutually_exclusive",
    "get_pairing_score",
    "score_combination",
    # Solver (M2)
    "compute_ingredient_cost",
    "solve_ingredient_quantities",
    # Pipeline Orchestrator (M2)
    "RecipeGenerator",
    "clean_display_name",
    "extract_pantry_candidates",
    "extract_catalog_candidates",
    "synthesize_recipe_title",
    "synthesize_cooking_instructions",
]
