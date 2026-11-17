"""meal_planner.generator.pipeline

End-to-end recipe generator orchestration:
1. SQLite pantry inventory ingestion & FIFO / opened packaging prioritization.
2. Catalog fallback fetching for missing required slots (when pantry_only=False).
3. Archetype resolution & multi-role slot candidate pooling.
4. Food pairing mutual exclusion pruning & flavor harmony scoring.
5. Linear programming dosage optimization (scipy HiGHS + pure-Python knapsack fallback).
6. Dynamic Italian recipe titling synthesis.
7. Step-by-step procedural cooking directions synthesis.
8. RecipeCandidate generation and SQLite persistence compatibility.
"""
from __future__ import annotations

from datetime import date
import itertools
import math
import sqlite3
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from meal_planner.db.connection import DB_PATH, get_connection
from meal_planner.generator import (
    IngredientCandidate,
    RecipeCandidate,
    Role,
    SolverResult,
)
from meal_planner.generator.food_pairing import (
    are_mutually_exclusive,
    resolve_canonical,
    score_combination,
)
from meal_planner.generator.slots import (
    Archetype,
    Slot,
    get_archetype,
    get_default_archetype_for_meal,
)
from meal_planner.generator.solver import solve_ingredient_quantities
from meal_planner.generator.taxonomy import classify_food, get_portion_bounds


# ---------------------------------------------------------------------------
# Helper: Ingredient Name Normalization for Titling & Steps
# ---------------------------------------------------------------------------

CLEAN_NAME_PATTERNS = [
    ("spaghetti", "Spaghetti"),
    ("penne", "Penne Rigate"),
    ("carnaroli", "Rice"),
    ("riso", "Rice"),
    ("rice", "Rice"),
    ("farro", "Farro"),
    ("pollo", "Chicken Breast"),
    ("chicken", "Chicken Breast"),
    ("puree", "Tomato Sauce"),
    ("passata", "Tomato Sauce"),
    ("pomodoro", "Tomato Sauce"),
    ("tomato", "Tomato Sauce"),
    ("parmigiano", "Parmigiano"),
    ("parmesan", "Parmigiano"),
    ("grana", "Parmigiano"),
    ("olio", "Extra Virgin Olive Oil"),
    ("oil", "Extra Virgin Olive Oil"),
    ("latte", "Milk"),
    ("milk", "Milk"),
    ("yogurt", "Yogurt"),
    ("mela", "Apples"),
    ("apple", "Apples"),
    ("banana", "Bananas"),
    ("biscotti", "Biscuits"),
    ("salmone", "Salmon"),
    ("salmon", "Salmon"),
    ("tonno", "Tuna"),
    ("tuna", "Tuna"),
    ("uov", "Eggs"),
    ("egg", "Eggs"),
    ("zucchine", "Zucchini"),
    ("zucchini", "Zucchini"),
    ("funghi", "Mushrooms"),
    ("mushrooms", "Mushrooms"),
    ("broccoli", "Broccoli"),
    ("patate", "Potatoes"),
    ("potatoes", "Potatoes"),
    ("farina", "Flour"),
    ("flour", "Flour"),
    ("avena", "Oats"),
    ("oats", "Oats"),
]


def clean_display_name(raw_name: Any) -> str:
    """Returns a clean, capitalized English ingredient name for titling."""
    if not raw_name or not isinstance(raw_name, str):
        return ""
    clean = raw_name.strip().lower()
    for pattern, replacement in CLEAN_NAME_PATTERNS:
        if pattern in clean:
            return replacement
    return raw_name.strip().title()


# ---------------------------------------------------------------------------
# Titling Synthesis Engine
# ---------------------------------------------------------------------------

def synthesize_recipe_title(
    archetype: Archetype,
    ingredients: Sequence[IngredientCandidate],
    conn: Optional[sqlite3.Connection] = None,
) -> str:
    """Generates an appetizing English title based on archetype style and key ingredients."""
    by_role: Dict[Role, List[str]] = {}
    for ing in ingredients:
        r = ing.role if isinstance(ing.role, Role) else Role(ing.role)
        name = clean_display_name(ing.name)
        by_role.setdefault(r, []).append(name)

    arch_name = (archetype.name or "").lower()

    # 1. Grain / Pasta Dish
    if "grain" in arch_name or "pasta" in arch_name:
        carb = by_role.get(Role.CARB_BASE, ["Pasta"])[0]
        sauce_list = by_role.get(Role.VEG_SAUCE, []) + by_role.get(Role.VEGETABLE, [])
        sauce = sauce_list[0] if sauce_list else None
        accent_list = by_role.get(Role.PROTEIN_ACCENT, []) or by_role.get(Role.PROTEIN_MAIN, [])
        accent = accent_list[0] if accent_list else None
        aroma_list = by_role.get(Role.AROMA_FINISHING, []) or by_role.get(Role.AROMA_TASTE, [])
        aroma = aroma_list[0] if aroma_list else None

        if sauce and accent and accent != "Parmigiano":
            title = f"{carb} with {sauce} and {accent}"
        elif sauce and (accent == "Parmigiano" or aroma == "Parmigiano"):
            title = f"{carb} with {sauce} and Parmigiano"
        elif sauce:
            title = f"{carb} with {sauce}"
        elif accent:
            title = f"{carb} with {accent}"
        elif aroma == "Parmigiano":
            title = f"{carb} with Olive Oil and Parmigiano"
        else:
            title = f"Classic {carb} Dish"

    # 2. Main & Side Plate
    elif "main" in arch_name or "plate" in arch_name:
        prot_list = by_role.get(Role.PROTEIN_MAIN, [])
        prot = prot_list[0] if prot_list else "Main Dish"
        side_list = by_role.get(Role.VEG_SIDE, []) or by_role.get(Role.VEGETABLE, [])
        side = side_list[0] if side_list else None
        carb_side_list = by_role.get(Role.CARB_SIDE, [])
        carb_side = carb_side_list[0] if carb_side_list else None

        if side and carb_side:
            title = f"{prot} with {side} and {carb_side}"
        elif side:
            title = f"{prot} with {side}"
        else:
            title = f"Seared {prot} Plate"

    # 3. One-Pot Balanced Bowl
    elif "bowl" in arch_name and "breakfast" not in arch_name:
        carb = by_role.get(Role.CARB_BASE, ["Grains"])[0]
        prot_list = by_role.get(Role.PROTEIN_MAIN, [])
        prot = prot_list[0] if prot_list else None
        veg_list = by_role.get(Role.VEGETABLE, [])
        veg = veg_list[0] if veg_list else None

        if prot and veg:
            title = f"{carb} Bowl with {prot} and {veg}"
        elif prot:
            title = f"{carb} and {prot} Bowl"
        else:
            title = f"{carb} Power Bowl"

    # 4. Breakfast / Snack Bowl
    elif "breakfast" in arch_name or "snack" in arch_name:
        dairy = by_role.get(Role.DAIRY_BASE, ["Yogurt"])[0]
        cereal = by_role.get(Role.CEREAL_CARB, ["Grains"])[0]
        fruit_list = by_role.get(Role.FRUIT, [])
        fruit = fruit_list[0] if fruit_list else None

        if cereal == "Oats":
            title = f"Oat Porridge with {fruit or 'Fruit'} and {dairy}"
        elif cereal == "Biscuits":
            title = f"{dairy} Bowl with Biscuits" + (f" and {fruit}" if fruit else "")
        elif fruit:
            title = f"{dairy} Bowl with {cereal} and {fruit}"
        else:
            title = f"{dairy} and {cereal} Bowl"
    else:
        title = f"{archetype.name} Recipe"

    # Uniqueness check against SQLite RECIPES table
    if conn is not None:
        try:
            base_title = title
            counter = 2
            while True:
                cur = conn.cursor()
                cur.execute("SELECT 1 FROM RECIPES WHERE Name = ? COLLATE NOCASE", (title,))
                if not cur.fetchone():
                    break
                title = f"{base_title} (Variant {counter})"
                counter += 1
        except Exception:
            pass

    return title


# ---------------------------------------------------------------------------
# Step-by-Step Cooking Directions Synthesis Engine
# ---------------------------------------------------------------------------

def synthesize_cooking_instructions(
    archetype: Archetype,
    ingredients: Sequence[IngredientCandidate],
) -> List[str]:
    """Generates procedural 4-step culinary cooking instructions in natural English."""
    by_role: Dict[Role, List[str]] = {}
    raw_by_role: Dict[Role, List[str]] = {}
    for ing in ingredients:
        r = ing.role if isinstance(ing.role, Role) else Role(ing.role)
        by_role.setdefault(r, []).append(clean_display_name(ing.name))
        raw_by_role.setdefault(r, []).append((ing.name or "").strip().lower())

    carb = (
        by_role.get(Role.CARB_BASE)
        or by_role.get(Role.CEREAL_CARB)
        or by_role.get(Role.CARB_SIDE)
        or [None]
    )[0]
    prot = (by_role.get(Role.PROTEIN_MAIN) or by_role.get(Role.PROTEIN_ACCENT) or [None])[0]
    veg = (by_role.get(Role.VEGETABLE) or by_role.get(Role.VEG_SAUCE) or by_role.get(Role.VEG_SIDE) or [None])[0]
    raw_veg = (
        raw_by_role.get(Role.VEGETABLE)
        or raw_by_role.get(Role.VEG_SAUCE)
        or raw_by_role.get(Role.VEG_SIDE)
        or [""]
    )[0]
    is_liquid_sauce = bool(by_role.get(Role.VEG_SAUCE)) or any(
        k in raw_veg for k in ("puree", "passata", "salsa", "sauce", "pelati")
    )
    fat = (by_role.get(Role.FAT_SEASONING) or ["a drizzle of extra virgin olive oil"])[0]
    aroma = (by_role.get(Role.AROMA_FINISHING) or by_role.get(Role.AROMA_TASTE) or by_role.get(Role.AROMA_HERB) or [None])[0]
    fruit = (by_role.get(Role.FRUIT) or [None])[0]
    dairy = (by_role.get(Role.DAIRY_BASE) or ["the dairy base"])[0]

    arch_name = (archetype.name or "").lower()
    steps: List[str] = []

    # 1. Step 1: Prep
    prep_parts: List[str] = []
    if fruit:
        prep_parts.append(f"Wash and slice {fruit} into bite-sized pieces.")
    if veg:
        if is_liquid_sauce:
            prep_parts.append(f"Prepare the {veg} sauce and measure out the required portion.")
        else:
            prep_parts.append(f"Wash and trim {veg}, cutting into even pieces.")
    if prot:
        prep_parts.append(f"Prepare {prot} by cutting into bite-sized pieces or portioning carefully.")
    if carb and "breakfast" not in arch_name:
        prep_parts.append(f"Weigh out {carb} and measure the seasonings accurately.")
    if not prep_parts:
        prep_parts.append("Arrange and measure all ingredients on the work surface.")
    steps.append(" ".join(prep_parts))

    # 2. Step 2: Cook
    cook_parts: List[str] = []
    if "pasta" in arch_name or "grain" in arch_name:
        cook_parts.append(f"Bring a pot of salted water to a boil and cook {carb} until al dente.")
        if veg and prot:
            cook_parts.append(f"Meanwhile, heat {fat} in a pan and sauté {prot} with {veg} over medium heat for 8-10 minutes.")
        elif veg:
            cook_parts.append(f"Meanwhile, warm {veg} in a pan with {fat} over medium heat for about 8 minutes.")
        elif prot:
            cook_parts.append(f"Meanwhile, sear {prot} in a pan with {fat} for 5-7 minutes until golden.")
    elif "breakfast" in arch_name:
        cook_parts.append(f"Pour {dairy} into a serving bowl and fold in {carb}.")
    else:
        if prot:
            cook_parts.append(f"In a hot skillet or grill pan, cook {prot} with {fat}, turning halfway through for an even sear.")
        if veg:
            cook_parts.append(f"Separately, simmer or sauté {veg} with a pinch of salt for 8-10 minutes.")
    steps.append(" ".join(cook_parts))

    # 3. Step 3: Combine
    if "pasta" in arch_name or "grain" in arch_name:
        steps.append(f"Drain {carb} al dente, reserving a splash of cooking water. Toss in the pan with the sauce and {fat} over high heat to bind the flavors.")
    elif "breakfast" in arch_name:
        steps.append(f"Top with {fruit or 'the fruit'} and stir gently to combine.")
    else:
        steps.append(f"Plate {prot or 'the main course'} alongside {veg or 'the side'} and drizzle with {fat}.")

    # 4. Step 4: Serve
    if aroma:
        steps.append(f"Finish the dish by sprinkling with {aroma} and serve hot immediately.")
    elif "breakfast" in arch_name:
        steps.append("Serve chilled and enjoy immediately.")
    else:
        steps.append("Season with salt and pepper to taste and serve hot.")

    return steps


# ---------------------------------------------------------------------------
# Database Extraction Helpers
# ---------------------------------------------------------------------------

def extract_pantry_candidates(conn: sqlite3.Connection) -> List[IngredientCandidate]:
    """Queries SQLite for pantry inventory items, computing net stock in grams and FIFO order."""
    today_str = date.today().isoformat()
    query = """
    SELECT 
        i.ID,
        (SELECT a.Name FROM ALIASES a WHERE a.ID = i.ID ORDER BY a.rowid ASC LIMIT 1) AS Name,
        p.Unit,
        p.Kcal_100g,
        p.Protein_100g,
        p.Carbs_100g,
        p.Fat_100g,
        p.Fiber_100g,
        p.Sugars_100g,
        p.Density_kg_per_L,
        p.Grams_per_piece,
        SUM(CASE WHEN p.Unit = 'pcs' 
                 THEN (i.Intact_Items * i.Unit_Weight) + i.Open_Remainder
                 ELSE (i.Intact_Items * i.Unit_Weight) + i.Open_Remainder 
            END) AS Available_Net,
        SUM(i.Open_Remainder) AS Total_Open_Remainder,
        MAX(i.Open_Remainder > 0) AS Has_Open,
        MIN(CASE WHEN i.Expiration_Date IS NOT NULL THEN i.Expiration_Date ELSE '9999-12-31' END) AS Earliest_Expiration
    FROM INVENTORY i
    JOIN PRODUCTS p ON i.ID = p.ID
    WHERE ((i.Intact_Items * i.Unit_Weight) + i.Open_Remainder) > 0
      AND (i.Expiration_Date IS NULL OR i.Expiration_Date > ?)
      AND NOT EXISTS (
          SELECT 1 FROM EXCLUSION_LIST e 
          WHERE e.ID = i.ID AND e.Location = i.Location
      )
    GROUP BY i.ID
    ORDER BY 
        Has_Open DESC,
        Earliest_Expiration ASC;
    """
    cur = conn.cursor()
    cur.execute(query, (today_str,))
    rows = cur.fetchall()

    candidates: List[IngredientCandidate] = []
    for r in rows:
        pid, name, unit, kcal, prot, carb, fat, fib, sug, den, gpp, avail_net, open_rem, has_open, exp = r
        if unit == "kg":
            avail_g = avail_net * 1000.0
        elif unit == "L":
            avail_g = avail_net * 1000.0 * (den or 1.0)
        elif unit == "pcs":
            avail_g = avail_net * (gpp or 50.0)
        else:
            avail_g = avail_net

        days_to_exp = None
        if exp != "9999-12-31":
            try:
                days_to_exp = max(0.0, float((date.fromisoformat(exp) - date.today()).days))
            except Exception:
                days_to_exp = None

        all_roles = classify_food(name, kcal, prot, carb, fat, fib or 0.0, sug or 0.0)
        primary_role = all_roles[0] if all_roles else Role.AROMA_TASTE

        cand = IngredientCandidate(
            product_id=pid,
            name=name or f"Product #{pid}",
            role=primary_role,
            unit=unit,
            kcal_100g=float(kcal or 0.0),
            protein_100g=float(prot or 0.0),
            carbs_100g=float(carb or 0.0),
            fat_100g=float(fat or 0.0),
            fiber_100g=float(fib or 0.0),
            sugars_100g=float(sug or 0.0),
            grams_per_piece=gpp,
            density_kg_per_l=den,
            available_stock_g=float(avail_g),
            open_remainder=float(open_rem or 0.0),
            is_open=bool(has_open),
            days_to_expiration=days_to_exp,
            is_missing=False,
            all_eligible_roles=all_roles,
        )
        candidates.append(cand)

    return candidates


def extract_catalog_candidates(conn: sqlite3.Connection) -> List[IngredientCandidate]:
    """Queries all products in the PRODUCTS catalog to serve as missing item suggestions."""
    query = """
    SELECT 
        p.ID,
        (SELECT a.Name FROM ALIASES a WHERE a.ID = p.ID ORDER BY a.rowid ASC LIMIT 1) AS Name,
        p.Unit,
        p.Kcal_100g,
        p.Protein_100g,
        p.Carbs_100g,
        p.Fat_100g,
        p.Fiber_100g,
        p.Sugars_100g,
        p.Density_kg_per_L,
        p.Grams_per_piece
    FROM PRODUCTS p
    ORDER BY p.ID ASC;
    """
    cur = conn.cursor()
    cur.execute(query)
    rows = cur.fetchall()

    catalog_items: List[IngredientCandidate] = []
    for r in rows:
        pid, name, unit, kcal, prot, carb, fat, fib, sug, den, gpp = r
        all_roles = classify_food(name, kcal, prot, carb, fat, fib or 0.0, sug or 0.0)
        primary_role = all_roles[0] if all_roles else Role.AROMA_TASTE

        cand = IngredientCandidate(
            product_id=pid,
            name=name or f"Catalog #{pid}",
            role=primary_role,
            unit=unit,
            kcal_100g=float(kcal or 0.0),
            protein_100g=float(prot or 0.0),
            carbs_100g=float(carb or 0.0),
            fat_100g=float(fat or 0.0),
            fiber_100g=float(fib or 0.0),
            sugars_100g=float(sug or 0.0),
            grams_per_piece=gpp,
            density_kg_per_l=den,
            available_stock_g=9999.0,
            open_remainder=0.0,
            is_open=False,
            days_to_expiration=None,
            is_missing=True,
            cost_coefficient=5.0,
            all_eligible_roles=all_roles,
        )
        catalog_items.append(cand)
    return catalog_items


def get_default_target_kcal(conn: sqlite3.Connection, meal_type: str = "lunch") -> float:
    """Computes target kcal from user PROFILE and MEALS energy share pct, or standard defaults."""
    clean_meal = (meal_type or "lunch").strip().lower()
    fallback_defaults = {"breakfast": 450.0, "lunch": 700.0, "dinner": 650.0}

    try:
        cur = conn.cursor()
        cur.execute("SELECT Kcal_target FROM PROFILE WHERE ID = 1")
        prof_row = cur.fetchone()
        if prof_row and prof_row[0] and prof_row[0] > 0:
            total_target = float(prof_row[0])
            cur.execute("SELECT Energy_Share_pct FROM MEALS WHERE Name = ? COLLATE NOCASE", (clean_meal,))
            meal_row = cur.fetchone()
            share = float(meal_row[0]) if meal_row else (40.0 if clean_meal == "lunch" else 35.0)
            return round(total_target * (share / 100.0), 0)
    except Exception:
        pass

    return fallback_defaults.get(clean_meal, 700.0)


# ---------------------------------------------------------------------------
# Pipeline Orchestrator Class
# ---------------------------------------------------------------------------

class RecipeGenerator:
    """End-to-End Deterministic Hybrid Recipe Generator.

    Orchestrates:
    1. Pantry extraction & FIFO sorting.
    2. Archetype resolution & slot candidate pooling.
    3. Food pairing mutual exclusion pruning.
    4. HiGHS simplex & continuous knapsack dosage optimization.
    5. Dynamic Italian titling and cooking directions synthesis.
    """

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path or DB_PATH

    @classmethod
    def generate(
        cls,
        target_kcal: Optional[float] = None,
        meal_type: str = "lunch",
        archetype_name: Optional[str] = None,
        pantry_only: bool = True,
        db_path: Optional[str] = None,
        macro_bounds: Optional[Dict[str, Tuple[float, float]]] = None,
        variation_index: int = 0,
    ) -> RecipeCandidate:
        """Classmethod entry point creating a transient RecipeGenerator instance."""
        inst = cls(db_path=db_path)
        return inst.generate_recipe(
            target_kcal=target_kcal,
            meal_type=meal_type,
            archetype_name=archetype_name,
            pantry_only=pantry_only,
            db_path=db_path,
            macro_bounds=macro_bounds,
            variation_index=variation_index,
        )

    def generate_recipe(
        self_or_cls,
        target_kcal: Optional[float] = None,
        meal_type: str = "lunch",
        archetype_name: Optional[str] = None,
        pantry_only: bool = True,
        db_path: Optional[str] = None,
        macro_bounds: Optional[Dict[str, Tuple[float, float]]] = None,
        variation_index: int = 0,
    ) -> RecipeCandidate:
        """Main recipe generation pipeline orchestrator. Callable on instance or class."""
        if isinstance(self_or_cls, type):
            inst = self_or_cls(db_path=db_path)
            return inst.generate_recipe(
                target_kcal=target_kcal,
                meal_type=meal_type,
                archetype_name=archetype_name,
                pantry_only=pantry_only,
                db_path=db_path,
                macro_bounds=macro_bounds,
                variation_index=variation_index,
            )

        self = self_or_cls
        target_db = db_path or self.db_path
        conn = get_connection(target_db)
        try:
            clean_meal = (meal_type or "lunch").strip().lower()

            # 1. Resolve Archetype
            if archetype_name:
                archetype = get_archetype(archetype_name)
            else:
                archetype = get_default_archetype_for_meal(clean_meal)

            # 2. Resolve Caloric Target
            if target_kcal is None or float(target_kcal) <= 0:
                final_target_kcal = get_default_target_kcal(conn, clean_meal)
            else:
                final_target_kcal = float(target_kcal)

            # 3. Extract Pantry & Catalog Candidates
            pantry_candidates = extract_pantry_candidates(conn)
            pantry_ids = {c.product_id for c in pantry_candidates}

            catalog_candidates: List[IngredientCandidate] = []
            if not pantry_only:
                full_catalog = extract_catalog_candidates(conn)
                catalog_candidates = [c for c in full_catalog if c.product_id not in pantry_ids]

            # 4. Build Slot Candidate Pools
            slot_pools = self._build_slot_pools(
                archetype=archetype,
                pantry_candidates=pantry_candidates,
                catalog_candidates=catalog_candidates,
                pantry_only=pantry_only,
            )

            # 5. Generate and Rank Candidate Combinations
            candidate_combos = self._generate_candidate_combinations(
                archetype=archetype,
                slot_pools=slot_pools,
                max_combos=40,
            )

            if not candidate_combos:
                raise ValueError(
                    f"Could not generate any compatible ingredient combinations for archetype '{archetype.name}'. "
                    f"Verify pantry items or set pantry_only=False."
                )

            if variation_index and len(candidate_combos) > 1:
                offset = int(variation_index) % len(candidate_combos)
                candidate_combos = candidate_combos[offset:] + candidate_combos[:offset]

            # 6. Optimize Dosages on Best Combinations
            import dataclasses
            winning_combo: Optional[List[IngredientCandidate]] = None
            winning_solver_result: Optional[SolverResult] = None
            warnings: List[str] = []

            for combo in candidate_combos:
                cloned_combo = [dataclasses.replace(c) for c in combo]
                res = solve_ingredient_quantities(
                    candidates=cloned_combo,
                    target_kcal=final_target_kcal,
                    macro_bounds=macro_bounds,
                    tolerance=0.05,
                )
                if res.success:
                    delta_pct = abs(res.total_kcal - final_target_kcal) / final_target_kcal
                    if delta_pct <= 0.05:
                        winning_combo = cloned_combo
                        winning_solver_result = res
                        break
                    elif winning_combo is None:
                        winning_combo = cloned_combo
                        winning_solver_result = res

            if winning_combo is None or winning_solver_result is None or not winning_solver_result.success:
                # Fallback to knapsack solver on first combination
                winning_combo = [dataclasses.replace(c) for c in candidate_combos[0]]
                winning_solver_result = solve_ingredient_quantities(
                    candidates=winning_combo,
                    target_kcal=final_target_kcal,
                    macro_bounds=macro_bounds,
                    force_fallback=True,
                )
                warnings.append("Optimal solution within ±5% caloric tolerance was not found by LP; applied knapsack fallback.")

            # Update resolved quantities on winning candidates (by index to avoid product_id=0 collisions)
            for idx, cand in enumerate(winning_combo):
                if winning_solver_result.quantities_by_idx and idx < len(winning_solver_result.quantities_by_idx):
                    q = winning_solver_result.quantities_by_idx[idx]
                else:
                    q = winning_solver_result.quantities_g.get(cand.product_id, 0.0)
                cand.resolved_quantity_g = q

            # 7. Synthesize Title & Directions
            title = synthesize_recipe_title(archetype, winning_combo, conn=conn)
            instructions = synthesize_cooking_instructions(archetype, winning_combo)
            notes = "\n".join(f"{i+1}. {step}" for i, step in enumerate(instructions))

            # 8. Construct Final RecipeCandidate and synchronize macros with quantized display units
            recipe = RecipeCandidate(
                title=title,
                archetype_name=archetype.name,
                meal_type=clean_meal,
                ingredients=winning_combo,
                instructions=instructions,
                notes=notes,
                target_kcal=final_target_kcal,
                total_kcal=winning_solver_result.total_kcal,
                total_protein=winning_solver_result.total_protein,
                total_carbs=winning_solver_result.total_carbs,
                total_fat=winning_solver_result.total_fat,
                total_fiber=winning_solver_result.total_fiber,
                pantry_only=pantry_only,
                solver_status=winning_solver_result.solver_name,
                warnings=warnings,
            )
            recipe.sync_macros_with_display_quantities()
            return recipe

        finally:
            conn.close()

    def _build_slot_pools(
        self,
        archetype: Archetype,
        pantry_candidates: List[IngredientCandidate],
        catalog_candidates: List[IngredientCandidate],
        pantry_only: bool,
    ) -> Dict[Slot, List[IngredientCandidate]]:
        """Matches available candidates to archetype slots and applies physiological portion bounds."""
        slot_pools: Dict[Slot, List[IngredientCandidate]] = {}

        for slot in archetype.slots:
            matched: List[IngredientCandidate] = []

            # 1. Check pantry items
            for c in pantry_candidates:
                if any(slot.accepts_role(r) for r in c.all_eligible_roles):
                    clone = self._specialize_candidate_for_slot(c, slot)
                    # Include if available stock covers at least a minimal portion or not pantry_only
                    if clone.available_stock_g >= clone.portion_min_g or not pantry_only:
                        matched.append(clone)

            # 2. Check catalog fallback if required slot is empty
            if not matched and not pantry_only:
                for c in catalog_candidates:
                    if any(slot.accepts_role(r) for r in c.all_eligible_roles):
                        clone = self._specialize_candidate_for_slot(c, slot)
                        matched.append(clone)

            if not matched and not slot.is_optional:
                if pantry_only:
                    raise ValueError(
                        f"Pantry does not contain eligible items for required slot '{slot.name}' ({slot.role.value}). "
                        f"Set pantry_only=False to allow catalog suggestions."
                    )
                else:
                    raise ValueError(
                        f"No products in catalog or pantry can satisfy required slot '{slot.name}' ({slot.role.value})."
                    )

            slot_pools[slot] = matched

        return slot_pools

    def _specialize_candidate_for_slot(self, cand: IngredientCandidate, slot: Slot) -> IngredientCandidate:
        """Creates a clone of an ingredient candidate configured with slot-specific bounds."""
        spec_min, spec_max = get_portion_bounds(slot.role, cand.name)
        p_min = max(slot.portion_min_g, spec_min)
        p_max = min(slot.portion_max_g, spec_max)
        if p_min > p_max:
            p_min, p_max = slot.portion_min_g, slot.portion_max_g

        to_taste = (slot.role in (Role.AROMA_TASTE, Role.AROMA_HERB, Role.AROMA_FINISHING))

        return IngredientCandidate(
            product_id=cand.product_id,
            name=cand.name,
            role=slot.role,
            unit=cand.unit,
            kcal_100g=cand.kcal_100g,
            protein_100g=cand.protein_100g,
            carbs_100g=cand.carbs_100g,
            fat_100g=cand.fat_100g,
            fiber_100g=cand.fiber_100g,
            sugars_100g=cand.sugars_100g,
            grams_per_piece=cand.grams_per_piece,
            density_kg_per_l=cand.density_kg_per_l,
            available_stock_g=cand.available_stock_g,
            open_remainder=cand.open_remainder,
            is_open=cand.is_open,
            days_to_expiration=cand.days_to_expiration,
            is_missing=cand.is_missing,
            to_taste=to_taste,
            portion_min_g=p_min,
            portion_max_g=p_max,
            all_eligible_roles=list(cand.all_eligible_roles),
        )

    def _generate_candidate_combinations(
        self,
        archetype: Archetype,
        slot_pools: Dict[Slot, List[IngredientCandidate]],
        max_combos: int = 40,
    ) -> List[List[IngredientCandidate]]:
        """Generates, prunes (hard exclusions), and scores combination candidates."""
        required_slots = [s for s in archetype.slots if not s.is_optional]
        optional_slots = [s for s in archetype.slots if s.is_optional]

        # 1. Required slot product combinations (cap per-slot pool to top 12 FIFO/priority items to prevent combinatorial explosion)
        req_pools = [slot_pools.get(s, [])[:12] for s in required_slots]
        if any(len(p) == 0 for p in req_pools):
            return []

        scored_combinations: List[Tuple[float, List[IngredientCandidate]]] = []

        for req_selection in itertools.product(*req_pools):
            # Check unique product IDs and unique canonical tokens
            ids = {item.product_id for item in req_selection}
            canons = {resolve_canonical(item.name) for item in req_selection if item.name}
            if len(ids) != len(req_selection) or len(canons) != len(req_selection):
                continue

            # Hard exclusion check within required items
            if any(
                are_mutually_exclusive(
                    a.name, set(a.all_eligible_roles), b.name, set(b.all_eligible_roles)
                )
                for a, b in itertools.combinations(req_selection, 2)
            ):
                continue

            current_combo = list(req_selection)

            # 2. Greedily attempt to add the best matching item from each optional slot
            for opt_slot in optional_slots:
                pool = slot_pools.get(opt_slot, [])[:12]
                best_opt_item = None
                best_opt_harmony = -1.0

                for opt_item in pool:
                    opt_canon = resolve_canonical(opt_item.name)
                    if opt_item.product_id in ids or (opt_canon and opt_canon in canons):
                        continue
                    # Avoid dry pasta as a side dish (CARB_SIDE) on a Main & Side Plate
                    if opt_slot.role == Role.CARB_SIDE and opt_canon == "pasta":
                        continue

                    # Hard exclusion check with all currently selected items
                    clash = False
                    for existing in current_combo:
                        if are_mutually_exclusive(
                            opt_item.name,
                            set(opt_item.all_eligible_roles),
                            existing.name,
                            set(existing.all_eligible_roles),
                        ):
                            clash = True
                            break
                    if clash:
                        continue

                    # Soft harmony score
                    harmony = score_combination([x.name for x in current_combo + [opt_item]])
                    if harmony > best_opt_harmony:
                        best_opt_harmony = harmony
                        best_opt_item = opt_item

                # Add optional item if harmony is acceptable (>= 0.50)
                if best_opt_item is not None and best_opt_harmony >= 0.50:
                    current_combo.append(best_opt_item)
                    ids.add(best_opt_item.product_id)
                    best_canon = resolve_canonical(best_opt_item.name)
                    if best_canon:
                        canons.add(best_canon)

            # 3. Overall mutual exclusion check on complete combination
            combination_valid = True
            for a, b in itertools.combinations(current_combo, 2):
                if are_mutually_exclusive(
                    a.name, set(a.all_eligible_roles), b.name, set(b.all_eligible_roles)
                ):
                    combination_valid = False
                    break
            if not combination_valid:
                continue

            # 4. Score combination: Pairing Harmony + Pantry Priority Bonus
            harmony_score = score_combination([x.name for x in current_combo])
            if harmony_score <= 0.0:
                continue

            pantry_priority = 0.0
            for item in current_combo:
                if item.is_open:
                    pantry_priority += 10.0
                elif item.days_to_expiration is not None and item.days_to_expiration > 0:
                    pantry_priority += 5.0 / max(1.0, item.days_to_expiration)
                if item.is_missing:
                    pantry_priority -= 5.0

            composite_score = (2.0 * harmony_score) + (pantry_priority / len(current_combo))
            scored_combinations.append((composite_score, current_combo))

            # Also provide minimal required combo as fallback behind all full combinations
            if len(current_combo) > len(req_selection):
                req_harmony = score_combination([x.name for x in req_selection])
                if req_harmony > 0.0:
                    req_priority = 0.0
                    for item in req_selection:
                        if item.is_open:
                            req_priority += 10.0
                        elif item.days_to_expiration is not None and item.days_to_expiration > 0:
                            req_priority += 5.0 / max(1.0, item.days_to_expiration)
                        if item.is_missing:
                            req_priority -= 5.0
                    req_composite = (2.0 * req_harmony) + (req_priority / len(req_selection)) - 20.0
                    scored_combinations.append((req_composite, list(req_selection)))

        # Sort combinations by composite score descending
        scored_combinations.sort(key=lambda x: x[0], reverse=True)
        return [combo for _, combo in scored_combinations[:max_combos]]
