"""tests/test_recipe_generator.py

Unit test suite for Milestone 1 of the Hybrid Recipe Generation Pipeline:
1. Generator types, enums, dataclasses, and serialization conversions.
2. Tier A semantic taxonomy classification (Italian and English).
3. Tier B automated macro fingerprinting (edge cases, zero-division protection).
4. Multi-role eligibility resolution and context-sensitive portion bounds.
5. Archetype definitions, slot cardinalities, meal mappings, and fulfillment validation.
6. Baseline database invariants preservation.
"""
from __future__ import annotations

import math
import os
import shutil
import sqlite3
import tempfile
import unittest

from meal_planner.generator import (
    ARCHETYPE_ALIASES,
    ARCHETYPES,
    BREAKFAST_SNACK_BOWL,
    DEFAULT_PORTION_BOUNDS,
    GRAIN_PASTA_DISH,
    MAIN_AND_SIDE_PLATE,
    MEAL_ARCHETYPES,
    ONE_POT_BALANCED_BOWL,
    Archetype,
    CulinaryTag,
    IngredientCandidate,
    RecipeCandidate,
    RecipeGenerator,
    Role,
    ROLE_COMPATIBILITY,
    Slot,
    SlotValidationResult,
    SolverResult,
    are_mutually_exclusive,
    are_roles_compatible,
    clamp_ingredient_portion,
    classify_food,
    clean_display_name,
    compute_ingredient_cost,
    extract_catalog_candidates,
    extract_pantry_candidates,
    fingerprint_macros,
    get_archetype,
    get_archetypes_for_meal,
    get_default_archetype_for_meal,
    get_pairing_score,
    get_portion_bounds,
    get_slot_for_role,
    is_role_compatible,
    resolve_canonical,
    score_combination,
    solve_ingredient_quantities,
    synthesize_cooking_instructions,
    synthesize_recipe_title,
    validate_slot_filling,
)
from meal_planner.db.connection import DB_PATH, get_connection
from meal_planner.recipes import create_recipe


class TestGeneratorTypesAndContracts(unittest.TestCase):
    """Verifies core types, enum behavior, dataclass invariants, and conversions."""

    def test_role_enum_string_compatibility(self):
        self.assertEqual(Role.CARB_BASE, "CARB_BASE")
        self.assertEqual(Role.PROTEIN_MAIN.value, "PROTEIN_MAIN")
        self.assertEqual(str(Role.VEGETABLE), "VEGETABLE")
        self.assertIn(Role.FAT_SEASONING, list(Role))
        self.assertIs(Role("CARB_BASE"), Role.CARB_BASE)

    def test_role_compatibility_mapping(self):
        # Identity
        self.assertTrue(are_roles_compatible(Role.CARB_BASE, Role.CARB_BASE))
        self.assertTrue(is_role_compatible(Role.CARB_BASE, [Role.CARB_BASE]))

        # Generic vegetable fulfills veg sauce and veg side slots
        self.assertTrue(are_roles_compatible(Role.VEGETABLE, Role.VEG_SAUCE))
        self.assertTrue(are_roles_compatible(Role.VEGETABLE, Role.VEG_SIDE))
        self.assertTrue(is_role_compatible(Role.VEG_SAUCE, [Role.VEGETABLE]))
        self.assertTrue(is_role_compatible(Role.VEG_SIDE, [Role.VEGETABLE]))

        # Main protein fulfills protein accent
        self.assertTrue(are_roles_compatible(Role.PROTEIN_MAIN, Role.PROTEIN_ACCENT))
        self.assertTrue(is_role_compatible(Role.PROTEIN_ACCENT, [Role.PROTEIN_MAIN]))

        # Incompatible combinations
        self.assertFalse(are_roles_compatible(Role.FAT_SEASONING, Role.CARB_BASE))
        self.assertFalse(are_roles_compatible(Role.FRUIT, Role.PROTEIN_MAIN))
        self.assertFalse(is_role_compatible(Role.CARB_BASE, [Role.FAT_SEASONING]))

        # Unknown string role handling
        self.assertFalse(are_roles_compatible("INVALID_ROLE", Role.CARB_BASE))
        self.assertFalse(is_role_compatible("INVALID_ROLE", [Role.CARB_BASE]))

    def test_slot_dataclass_validation(self):
        s = Slot(
            role=Role.CARB_BASE,
            cardinality_min=1,
            cardinality_max=1,
            portion_min_g=60.0,
            portion_max_g=120.0,
        )
        self.assertEqual(s.bounds, (60.0, 120.0))
        self.assertEqual(s.name, "Carb Base")
        self.assertTrue(s.accepts_role(Role.CARB_BASE))
        self.assertTrue(s.validate_cardinality(1))
        self.assertFalse(s.validate_cardinality(0))
        self.assertFalse(s.validate_cardinality(2))
        self.assertTrue(s.validate_portion(90.0))
        self.assertFalse(s.validate_portion(40.0))
        self.assertFalse(s.validate_portion(150.0))
        self.assertEqual(s.clamp_portion(40.0), 60.0)
        self.assertEqual(s.clamp_portion(150.0), 120.0)
        self.assertEqual(s.clamp_portion(90.0), 90.0)

        # Invariant validations
        with self.assertRaises(ValueError):
            Slot(role=Role.CARB_BASE, cardinality_min=2, cardinality_max=1)
        with self.assertRaises(ValueError):
            Slot(role=Role.CARB_BASE, cardinality_min=-1, cardinality_max=1)
        with self.assertRaises(ValueError):
            Slot(role=Role.CARB_BASE, portion_min_g=100.0, portion_max_g=50.0)
        with self.assertRaises(ValueError):
            Slot(role=Role.CARB_BASE, portion_min_g=-10.0, portion_max_g=50.0)

    def test_ingredient_candidate_display_conversions(self):
        # Grams (g)
        ing_g = IngredientCandidate(
            product_id=1, name="Spaghetti", role=Role.CARB_BASE, unit="g", resolved_quantity_g=85.0
        )
        self.assertEqual(ing_g.calculate_display_quantity(), (85.0, "g"))

        # Kilograms (kg)
        ing_kg = IngredientCandidate(
            product_id=1, name="Spaghetti", role=Role.CARB_BASE, unit="kg", resolved_quantity_g=85.0
        )
        self.assertEqual(ing_kg.calculate_display_quantity(), (0.085, "kg"))

        # Pieces (pcs) e.g. eggs 50g per piece
        ing_pcs = IngredientCandidate(
            product_id=2,
            name="Eggs",
            role=Role.PROTEIN_MAIN,
            unit="pcs",
            grams_per_piece=50.0,
            resolved_quantity_g=100.0,
        )
        self.assertEqual(ing_pcs.calculate_display_quantity(), (2.0, "pcs"))

        # Volume Liters (L) e.g. olive oil density 0.916
        ing_l = IngredientCandidate(
            product_id=105,
            name="EVO oil",
            role=Role.FAT_SEASONING,
            unit="L",
            density_kg_per_l=0.916,
            resolved_quantity_g=15.0,
        )
        qty, unit = ing_l.calculate_display_quantity()
        self.assertEqual(unit, "L")
        self.assertAlmostEqual(qty, 0.016, places=3)

        # Volume Milliliters (ml)
        ing_ml = IngredientCandidate(
            product_id=105,
            name="EVO oil",
            role=Role.FAT_SEASONING,
            unit="ml",
            density_kg_per_l=0.916,
            resolved_quantity_g=15.0,
        )
        qty_ml, unit_ml = ing_ml.calculate_display_quantity()
        self.assertEqual(unit_ml, "ml")
        self.assertAlmostEqual(qty_ml, 16.4, places=1)

        # Open remainder initializes is_open
        ing_open = IngredientCandidate(
            product_id=1,
            name="Penne",
            role=Role.CARB_BASE,
            unit="kg",
            open_remainder=0.35,
        )
        self.assertTrue(ing_open.is_open)

    def test_ingredient_candidate_macro_computation(self):
        ing = IngredientCandidate(
            product_id=108,
            name="Chicken breast",
            role=Role.PROTEIN_MAIN,
            kcal_100g=110.0,
            protein_100g=23.0,
            carbs_100g=0.0,
            fat_100g=1.5,
            resolved_quantity_g=200.0,
        )
        macros = ing.compute_macros_for_quantity()
        self.assertEqual(macros["Kcal"], 220.0)
        self.assertEqual(macros["Protein"], 46.0)
        self.assertEqual(macros["Carbs"], 0.0)
        self.assertEqual(macros["Fat"], 3.0)

        # Explicit grams override
        macros_150 = ing.compute_macros_for_quantity(150.0)
        self.assertEqual(macros_150["Kcal"], 165.0)
        self.assertEqual(macros_150["Protein"], 34.5)

    def test_recipe_candidate_to_create_recipe_args(self):
        ing = IngredientCandidate(
            product_id=1,
            name="Spaghetti",
            role=Role.CARB_BASE,
            unit="g",
            resolved_quantity_g=80.0,
        )
        rec = RecipeCandidate(
            title="Spaghetti al Pomodoro",
            archetype_name="Grain / Pasta Dish",
            meal_type="lunch",
            ingredients=[ing],
            instructions=["Cuocere la pasta in acqua bollente", "Condire con la passata"],
            target_kcal=600.0,
            total_kcal=580.0,
        )
        kwargs = rec.to_create_recipe_kwargs()
        self.assertEqual(kwargs["name"], "Spaghetti al Pomodoro")
        self.assertEqual(kwargs["meals"], ["lunch"])
        self.assertEqual(len(kwargs["ingredients"]), 1)
        self.assertEqual(kwargs["ingredients"][0]["product"], "Spaghetti")
        self.assertEqual(kwargs["ingredients"][0]["quantity"], 80.0)
        self.assertEqual(kwargs["ingredients"][0]["unit"], "g")
        self.assertIn("1. Cuocere la pasta in acqua bollente", kwargs["notes"])
        self.assertAlmostEqual(rec.caloric_delta_pct, 3.33, places=2)


class TestTaxonomyClassification(unittest.TestCase):
    """Verifies Tier A dictionary and Tier B macro fingerprinting."""

    def test_tier_a_canonical_foods(self):
        # Carbs
        self.assertIn(Role.CARB_BASE, classify_food("Spaghetti n.5", 355, 12, 72, 1.5))
        self.assertIn(Role.CARB_BASE, classify_food("Carnaroli rice", 350, 7, 78, 0.6))
        self.assertIn(Role.CARB_BASE, classify_food("Penne Rigate", 350, 12, 71, 1.5))
        self.assertIn(Role.CARB_BASE, classify_food("00 flour", 340, 11, 72, 1.0))
        self.assertIn(Role.CARB_BASE, classify_food("soup_pasta", 350, 12, 71, 1.5))

        # Protein
        self.assertIn(Role.PROTEIN_MAIN, classify_food("Chicken breast", 110, 23, 0, 1.5))
        self.assertIn(Role.PROTEIN_MAIN, classify_food("Petto di pollo", 110, 23, 0, 1.5))
        self.assertIn(Role.PROTEIN_MAIN, classify_food("Tonno al naturale", 100, 24, 0, 0.5))

        # Vegetables
        self.assertIn(Role.VEGETABLE, classify_food("Tomato puree", 25, 1.2, 4.0, 0.2))
        self.assertIn(Role.VEGETABLE, classify_food("Passata di pomodoro", 25, 1.2, 4.0, 0.2))
        self.assertIn(Role.VEGETABLE, classify_food("Zucchine", 15, 1.5, 3.0, 0.2))

        # Fat
        self.assertIn(Role.FAT_SEASONING, classify_food("Extra virgin olive oil", 884, 0, 0, 100))
        self.assertIn(Role.FAT_SEASONING, classify_food("EVO oil", 884, 0, 0, 100))

        # Dairy
        self.assertIn(Role.DAIRY_BASE, classify_food("Whole milk", 64, 3.3, 4.8, 3.6))
        self.assertIn(Role.DAIRY_BASE, classify_food("Plain yogurt", 61, 3.5, 4.7, 3.3))

        # Fruit
        self.assertIn(Role.FRUIT, classify_food("Apples", 52, 0.3, 14.0, 0.2, sugars_100g=10.0))
        self.assertIn(Role.FRUIT, classify_food("Banana", 89, 1.1, 23.0, 0.3, sugars_100g=12.0))

        # Breakfast cereal
        self.assertIn(Role.CEREAL_CARB, classify_food("biscotti", 450, 7.0, 68, 16))

    def test_tier_b_macro_fingerprinting_custom_foods(self):
        # Unknown high-protein food (e.g. Seitan)
        roles = classify_food("Custom Vegan Seitan", kcal_100g=120, protein_100g=24.0, carbs_100g=4.0, fat_100g=1.0)
        self.assertIn(Role.PROTEIN_MAIN, roles)

        # Unknown high-carb food (e.g. Quinoa Flour)
        roles = classify_food("Exotic Ancient Grain", kcal_100g=360, protein_100g=10.0, carbs_100g=68.0, fat_100g=3.0)
        self.assertIn(Role.CARB_BASE, roles)

        # Unknown pure oil
        roles = classify_food("Avocado Cold Pressed Oil", kcal_100g=880, protein_100g=0.0, carbs_100g=0.0, fat_100g=98.0)
        self.assertIn(Role.FAT_SEASONING, roles)

        # Unknown leafy vegetable
        roles = classify_food("Wild Green Leaf", kcal_100g=22, protein_100g=2.0, carbs_100g=2.5, fat_100g=0.3, sugars_100g=1.0)
        self.assertIn(Role.VEGETABLE, roles)

        # Unknown fresh fruit
        roles = classify_food("Exotic Tropical Berry", kcal_100g=48, protein_100g=0.8, carbs_100g=11.0, fat_100g=0.2, sugars_100g=9.5)
        self.assertIn(Role.FRUIT, roles)

    def test_tier_b_edge_cases_and_zero_division_protection(self):
        # Zero calorie spice / water / salt
        roles = classify_food("Sale Marino / Spices", kcal_100g=0.0, protein_100g=0.0, carbs_100g=0.0, fat_100g=0.0)
        self.assertIn(Role.AROMA_TASTE, roles)

        # Zero calorie with protein (e.g. hypothetical isolate with missing kcal)
        roles_prot_zero_kcal = fingerprint_macros(kcal_100g=0.0, protein_100g=20.0, carbs_100g=0.0, fat_100g=0.0)
        self.assertIn(Role.PROTEIN_MAIN, roles_prot_zero_kcal)

        # Missing sugars (None)
        roles = classify_food("Unsweetened Veg", kcal_100g=30, protein_100g=1.5, carbs_100g=4.0, fat_100g=0.5, sugars_100g=0.0)
        self.assertIn(Role.VEGETABLE, roles)

        # Negative inputs defensive protection
        roles_neg = fingerprint_macros(kcal_100g=-50.0, protein_100g=-10.0, carbs_100g=-5.0, fat_100g=-2.0)
        self.assertIn(Role.AROMA_TASTE, roles_neg)


class TestMultiRoleResolutionAndPortionBounds(unittest.TestCase):
    """Verifies dual-role candidates and context-sensitive physiological bounds."""

    def test_parmigiano_dual_role(self):
        roles = classify_food("Parmigiano Reggiano", kcal_100g=392, protein_100g=33, carbs_100g=0, fat_100g=28)
        self.assertIn(Role.AROMA_TASTE, roles)
        self.assertIn(Role.PROTEIN_MAIN, roles)

        # Context-sensitive portion bounds
        aroma_bounds = get_portion_bounds(Role.AROMA_TASTE, "Parmigiano Reggiano")
        self.assertEqual(aroma_bounds, (5.0, 20.0))

        protein_bounds = get_portion_bounds(Role.PROTEIN_MAIN, "Parmigiano Reggiano")
        self.assertEqual(protein_bounds, (40.0, 80.0))

        # English alias Parmesan
        self.assertEqual(get_portion_bounds(Role.AROMA_TASTE, "Parmesan cheese"), (5.0, 20.0))
        self.assertEqual(get_portion_bounds(Role.PROTEIN_MAIN, "Parmesan cheese"), (40.0, 80.0))

    def test_eggs_dual_role(self):
        roles = classify_food("Eggs", kcal_100g=143, protein_100g=12.6, carbs_100g=0.7, fat_100g=9.9)
        self.assertIn(Role.PROTEIN_MAIN, roles)
        self.assertIn(Role.PROTEIN_ACCENT, roles)

        main_bounds = get_portion_bounds(Role.PROTEIN_MAIN, "Eggs")
        self.assertEqual(main_bounds, (100.0, 200.0))

        accent_bounds = get_portion_bounds(Role.PROTEIN_ACCENT, "Eggs")
        self.assertEqual(accent_bounds, (50.0, 60.0))

    def test_default_portion_bounds(self):
        self.assertEqual(get_portion_bounds(Role.CARB_BASE), (60.0, 120.0))
        self.assertEqual(get_portion_bounds(Role.PROTEIN_MAIN), (100.0, 250.0))
        self.assertEqual(get_portion_bounds(Role.VEGETABLE), (100.0, 300.0))
        self.assertEqual(get_portion_bounds(Role.FAT_SEASONING), (5.0, 20.0))
        self.assertEqual(get_portion_bounds(Role.DAIRY_BASE), (100.0, 250.0))
        self.assertEqual(get_portion_bounds(Role.FRUIT), (50.0, 150.0))
        self.assertEqual(get_portion_bounds(Role.AROMA_TASTE), (5.0, 15.0))


class TestArchetypesAndSlots(unittest.TestCase):
    """Verifies 4 culinary archetypes, slot cardinalities, and meal mappings."""

    def test_four_archetypes_present(self):
        expected = [
            "Grain / Pasta Dish",
            "Main & Side Plate",
            "One-Pot Balanced Bowl",
            "Breakfast / Snack Bowl",
        ]
        self.assertEqual(len(ARCHETYPES), 4)
        for name in expected:
            self.assertIn(name, ARCHETYPES)
            arch = get_archetype(name)
            self.assertEqual(arch.name, name)

    def test_archetype_aliases(self):
        self.assertIs(get_archetype("pasta"), GRAIN_PASTA_DISH)
        self.assertIs(get_archetype("primi"), GRAIN_PASTA_DISH)
        self.assertIs(get_archetype("secondi"), MAIN_AND_SIDE_PLATE)
        self.assertIs(get_archetype("piatto unico"), ONE_POT_BALANCED_BOWL)
        self.assertIs(get_archetype("colazione"), BREAKFAST_SNACK_BOWL)
        self.assertIs(get_archetype("snack"), BREAKFAST_SNACK_BOWL)

        with self.assertRaises(ValueError):
            get_archetype("unknown_nonexistent_archetype")
        with self.assertRaises(ValueError):
            get_archetype("")

    def test_meal_filtering(self):
        lunch_archetypes = [a.name for a in get_archetypes_for_meal("lunch")]
        self.assertIn("Grain / Pasta Dish", lunch_archetypes)
        self.assertIn("One-Pot Balanced Bowl", lunch_archetypes)
        self.assertIn("Main & Side Plate", lunch_archetypes)

        dinner_archetypes = [a.name for a in get_archetypes_for_meal("dinner")]
        self.assertIn("Main & Side Plate", dinner_archetypes)
        self.assertIn("One-Pot Balanced Bowl", dinner_archetypes)

        breakfast_archetypes = [a.name for a in get_archetypes_for_meal("breakfast")]
        self.assertEqual(breakfast_archetypes, ["Breakfast / Snack Bowl"])

        # Defaults
        self.assertIs(get_default_archetype_for_meal("lunch"), GRAIN_PASTA_DISH)
        self.assertIs(get_default_archetype_for_meal("dinner"), MAIN_AND_SIDE_PLATE)
        self.assertIs(get_default_archetype_for_meal("breakfast"), BREAKFAST_SNACK_BOWL)

        with self.assertRaises(ValueError):
            get_archetypes_for_meal("midnight_snack_unknown")

    def test_grain_pasta_dish_structure(self):
        arch = get_archetype("Grain / Pasta Dish")
        self.assertEqual(arch.required_slots[Role.CARB_BASE], 1)
        self.assertEqual(arch.required_slots[Role.FAT_SEASONING], 1)
        self.assertEqual(len(arch.required_slots), 2)
        self.assertEqual(len(arch.optional_slots), 3)

        carb_slot = arch.get_slot(Role.CARB_BASE)
        self.assertIsNotNone(carb_slot)
        self.assertEqual(carb_slot.portion_min_g, 60.0)
        self.assertEqual(carb_slot.portion_max_g, 120.0)

        fat_slot = arch.get_slot(Role.FAT_SEASONING)
        self.assertIsNotNone(fat_slot)
        self.assertEqual(fat_slot.portion_min_g, 5.0)
        self.assertEqual(fat_slot.portion_max_g, 20.0)

    def test_main_and_side_plate_structure(self):
        arch = get_archetype("Main & Side Plate")
        self.assertEqual(arch.required_slots[Role.PROTEIN_MAIN], 1)
        self.assertEqual(arch.required_slots[Role.VEG_SIDE], 2)
        self.assertEqual(arch.required_slots[Role.FAT_SEASONING], 1)
        self.assertEqual(len(arch.required_slots), 3)
        self.assertEqual(len(arch.optional_slots), 2)

        veg_side = arch.get_slot(Role.VEG_SIDE)
        self.assertIsNotNone(veg_side)
        self.assertEqual(veg_side.cardinality_min, 1)
        self.assertEqual(veg_side.cardinality_max, 2)
        self.assertEqual(veg_side.portion_min_g, 120.0)
        self.assertEqual(veg_side.portion_max_g, 350.0)

    def test_one_pot_balanced_bowl_structure(self):
        arch = get_archetype("One-Pot Balanced Bowl")
        self.assertEqual(arch.required_slots[Role.CARB_BASE], 1)
        self.assertEqual(arch.required_slots[Role.PROTEIN_MAIN], 1)
        self.assertEqual(arch.required_slots[Role.VEGETABLE], 1)
        self.assertEqual(arch.required_slots[Role.FAT_SEASONING], 1)
        self.assertEqual(len(arch.required_slots), 4)
        self.assertEqual(len(arch.optional_slots), 1)

    def test_breakfast_snack_bowl_structure(self):
        arch = get_archetype("Breakfast / Snack Bowl")
        self.assertEqual(arch.required_slots[Role.DAIRY_BASE], 1)
        self.assertEqual(arch.required_slots[Role.CEREAL_CARB], 1)
        self.assertEqual(len(arch.required_slots), 2)
        self.assertEqual(len(arch.optional_slots), 2)

    def test_slot_filling_validation_dict(self):
        arch = get_archetype("Grain / Pasta Dish")

        # Valid pasta dish
        valid_dict = {
            Role.CARB_BASE: [{"id": 102, "name": "Spaghetti", "role": Role.CARB_BASE, "quantity": 80.0}],
            Role.FAT_SEASONING: [{"id": 105, "name": "EVO oil", "role": Role.FAT_SEASONING, "quantity": 10.0}],
            Role.VEG_SAUCE: [{"id": 103, "name": "Tomato puree", "role": Role.VEGETABLE, "quantity": 100.0}],
        }
        res_valid = validate_slot_filling(arch, valid_dict)
        self.assertTrue(res_valid.is_valid, f"Errors: {res_valid.errors}")
        self.assertEqual(len(res_valid.errors), 0)

        # Missing required CARB_BASE
        missing_carb = {
            Role.FAT_SEASONING: [{"id": 105, "name": "EVO oil", "role": Role.FAT_SEASONING, "quantity": 10.0}],
        }
        res_missing = validate_slot_filling(arch, missing_carb)
        self.assertFalse(res_missing.is_valid)
        self.assertTrue(any("CARB_BASE" in err for err in res_missing.errors))

        # Duplicate ingredient in multiple slots
        duplicate_dict = {
            Role.CARB_BASE: [{"id": 102, "name": "Spaghetti", "role": Role.CARB_BASE}],
            Role.FAT_SEASONING: [{"id": 102, "name": "Spaghetti", "role": Role.FAT_SEASONING}],
        }
        res_dup = validate_slot_filling(arch, duplicate_dict)
        self.assertFalse(res_dup.is_valid)
        self.assertTrue(any("Duplicate" in err for err in res_dup.errors))

    def test_slot_filling_validation_sequence(self):
        arch = get_archetype("Grain / Pasta Dish")

        # Valid list of candidate ingredients
        valid_selection = [
            IngredientCandidate(product_id=1, name="Spaghetti", role=Role.CARB_BASE, resolved_quantity_g=80.0),
            IngredientCandidate(product_id=105, name="EVO oil", role=Role.FAT_SEASONING, resolved_quantity_g=10.0),
        ]
        res = validate_slot_filling(arch, valid_selection)
        self.assertTrue(res.is_valid)
        self.assertEqual(len(res.errors), 0)

        # Missing required fat seasoning
        invalid_selection = [
            IngredientCandidate(product_id=1, name="Spaghetti", role=Role.CARB_BASE, resolved_quantity_g=80.0),
        ]
        res_invalid = validate_slot_filling(arch, invalid_selection)
        self.assertFalse(res_invalid.is_valid)
        self.assertTrue(any("FAT_SEASONING" in err for err in res_invalid.errors))

    def test_portion_clamping_helpers(self):
        arch = get_archetype("Grain / Pasta Dish")
        self.assertEqual(clamp_ingredient_portion(arch, Role.CARB_BASE, 30.0), 60.0)
        self.assertEqual(clamp_ingredient_portion(arch, Role.CARB_BASE, 80.0), 80.0)
        self.assertEqual(clamp_ingredient_portion(arch, Role.CARB_BASE, 200.0), 120.0)


class TestSlotRobustnessAndAdversarialInputs(unittest.TestCase):
    """Regression test suite for type robustness, role validation, and null safety.

    Covers Milestone 1 Iteration 2 fixes:
    - Bug 1: Graceful failure on unknown/invalid role strings without ValueError traceback.
    - Bug 2: Detection and rejection of null (None) items or roleless objects in dict assignments.
    - Bug 3: Graceful failure on non-string inputs to get_archetype, get_archetypes_for_meal, and is_eligible_for_meal.
    """

    def setUp(self):
        self.arch_pasta = get_archetype("Grain / Pasta Dish")
        self.arch_main = get_archetype("Main & Side Plate")
        self.arch_bowl = get_archetype("One-Pot Balanced Bowl")
        self.arch_bkt = get_archetype("Breakfast / Snack Bowl")

    # -----------------------------------------------------------------------
    # Bug 1 Tests: Unknown / Invalid Role Strings Fail Gracefully
    # -----------------------------------------------------------------------

    def test_bug1_invalid_role_string_in_sequence_fails_gracefully(self):
        """Sequence mode: ingredient with unknown string role must not crash with ValueError traceback."""
        invalid_item = {"id": 1, "name": "MysteryFood", "role": "TOTALLY_INVALID_ROLE_XYZ"}
        res = validate_slot_filling(self.arch_pasta, [invalid_item])

        self.assertFalse(res.is_valid, "Expected is_valid=False for unmapped role string.")
        self.assertGreater(len(res.errors), 0, "Expected at least one validation error.")
        self.assertTrue(
            any("TOTALLY_INVALID_ROLE_XYZ" in err or "does not fit into any slot" in err for err in res.errors),
            f"Error message should mention role or slot failure. Got: {res.errors}",
        )

    def test_bug1_invalid_role_string_in_dict_fails_gracefully(self):
        """Dict mode: ingredient with unknown string role must fail role compatibility cleanly."""
        assignments = {
            Role.CARB_BASE: [{"id": 101, "name": "BogusPasta", "role": "CORRUPT_ROLE_NAME"}],
            Role.FAT_SEASONING: [{"id": 105, "name": "EVO oil", "role": Role.FAT_SEASONING}],
        }
        res = validate_slot_filling(self.arch_pasta, assignments)

        self.assertFalse(res.is_valid, "Expected is_valid=False for invalid role in dict assignment.")
        self.assertGreater(len(res.errors), 0)
        self.assertTrue(
            any("incompatible" in err or "CORRUPT_ROLE_NAME" in err for err in res.errors),
            f"Error message should mention incompatibility. Got: {res.errors}",
        )

    def test_bug1_multiple_roles_with_invalid_string_entries(self):
        """Candidate with mixed valid and invalid string roles must handle invalid entries without traceback."""
        item_mixed = {
            "id": 201,
            "name": "CompositeFood",
            "roles": [Role.CARB_BASE, "UNKNOWN_ACCENT_STRING"],
        }
        # In sequence mode, should successfully fulfill CARB_BASE because it has a valid matching role
        res = validate_slot_filling(
            self.arch_pasta,
            [
                item_mixed,
                {"id": 202, "name": "Oil", "role": Role.FAT_SEASONING},
            ],
        )
        self.assertTrue(res.is_valid, f"Valid CARB_BASE should be matched despite invalid secondary role: {res.errors}")

    # -----------------------------------------------------------------------
    # Bug 2 Tests: Null Items and Roleless Objects in Dict Assignments
    # -----------------------------------------------------------------------

    def test_bug2_none_item_in_dict_assignment_rejected(self):
        """Assigning [None] to a required slot must fail validation (not silently pass)."""
        assignments = {
            Role.CARB_BASE: [None],
            Role.FAT_SEASONING: [{"id": 105, "name": "EVO oil", "role": Role.FAT_SEASONING}],
        }
        res = validate_slot_filling(self.arch_pasta, assignments)

        self.assertFalse(res.is_valid, "Slot filled with [None] must NOT be valid.")
        self.assertGreater(len(res.errors), 0)
        self.assertTrue(
            any("null" in err.lower() or "none" in err.lower() or "CARB_BASE" in err for err in res.errors),
            f"Expected error regarding null ingredient or missing required slot. Got: {res.errors}",
        )

    def test_bug2_roleless_dict_item_rejected(self):
        """Assigning an object or dict with no role attribute must not satisfy slot requirements."""
        assignments = {
            Role.CARB_BASE: [{"id": 999, "name": "NamelessIngredient", "quantity": 100.0}],
            Role.FAT_SEASONING: [{"id": 105, "name": "EVO oil", "role": Role.FAT_SEASONING}],
        }
        res = validate_slot_filling(self.arch_pasta, assignments)

        self.assertFalse(res.is_valid, "Item without a culinary role must fail validation.")
        self.assertGreater(len(res.errors), 0)
        self.assertTrue(
            any("no defined culinary role" in err or "incompatible" in err or "CARB_BASE" in err for err in res.errors),
            f"Expected role definition error. Got: {res.errors}",
        )

    def test_bug2_dict_with_none_value_or_empty_list(self):
        """Dict entries with None or empty list values must count as 0 fulfilled items."""
        assignments = {
            Role.CARB_BASE: [],
            Role.FAT_SEASONING: None,
        }
        res = validate_slot_filling(self.arch_pasta, assignments)

        self.assertFalse(res.is_valid)
        self.assertTrue(any("CARB_BASE" in err for err in res.errors))
        self.assertTrue(any("FAT_SEASONING" in err for err in res.errors))

    # -----------------------------------------------------------------------
    # Bug 3 Tests: Non-String Inputs to Retrieval Methods Fail Gracefully
    # -----------------------------------------------------------------------

    def test_bug3_get_archetype_non_string_inputs(self):
        """get_archetype must raise ValueError (not AttributeError) on non-string inputs."""
        non_string_inputs = [123, 45.67, True, False, [1, 2], {"name": "pasta"}, object()]
        for bad_input in non_string_inputs:
            with self.subTest(bad_input=bad_input):
                with self.assertRaises(ValueError) as ctx:
                    get_archetype(bad_input)
                self.assertTrue(
                    "Unknown archetype" in str(ctx.exception)
                    or "cannot be empty" in str(ctx.exception)
                    or "must be a string" in str(ctx.exception),
                    f"Unexpected error message for input {bad_input}: {ctx.exception}",
                )

        # Null and empty string inputs
        with self.assertRaises(ValueError):
            get_archetype(None)
        with self.assertRaises(ValueError):
            get_archetype("")
        with self.assertRaises(ValueError):
            get_archetype("   ")

    def test_bug3_get_archetypes_for_meal_non_string_inputs(self):
        """get_archetypes_for_meal must raise ValueError (not AttributeError) on non-string inputs."""
        non_string_inputs = [None, 123, 3.14, True, ["lunch"], object()]
        for bad_meal in non_string_inputs:
            with self.subTest(bad_meal=bad_meal):
                with self.assertRaises(ValueError) as ctx:
                    get_archetypes_for_meal(bad_meal)
                self.assertIn("Unknown meal type", str(ctx.exception))
                self.assertIn("Valid meal types", str(ctx.exception))

        # Also verify get_default_archetype_for_meal delegates cleanly
        with self.assertRaises(ValueError):
            get_default_archetype_for_meal(123)
        with self.assertRaises(ValueError):
            get_default_archetype_for_meal(None)

    def test_bug3_archetype_is_eligible_for_meal_non_string_inputs(self):
        """Archetype.is_eligible_for_meal must return False (not crash with AttributeError) on non-string inputs."""
        arch = self.arch_pasta
        self.assertFalse(arch.is_eligible_for_meal(123))
        self.assertFalse(arch.is_eligible_for_meal(None))
        self.assertFalse(arch.is_eligible_for_meal(3.14))
        self.assertFalse(arch.is_eligible_for_meal(["lunch"]))
        self.assertFalse(arch.is_eligible_for_meal(object()))
        # Verify legitimate meal still works
        self.assertTrue(arch.is_eligible_for_meal("lunch"))
        self.assertTrue(arch.is_eligible_for_meal("  LUNCH  "))

    # -----------------------------------------------------------------------
    # Milestone 1 Iteration 3 Adversarial Regression Tests
    # -----------------------------------------------------------------------

    def test_classify_food_non_string_names(self):
        """Passing non-string types (int, float, bool, list, dict, object) as food name

        must not raise AttributeError and must fall back gracefully to Tier B macro fingerprinting.
        """
        adversarial_names = [
            123,
            45.67,
            True,
            False,
            0,
            [1, 2],
            [],
            {"a": 1},
            {},
            object(),
        ]
        # Profile 1: High protein (120 kcal, 22g P, 2g C, 1.5g F) -> classifies to PROTEIN_MAIN
        for bad_name in adversarial_names:
            with self.subTest(bad_name=bad_name, name_type=type(bad_name).__name__):
                try:
                    roles = classify_food(
                        bad_name,
                        kcal_100g=120.0,
                        protein_100g=22.0,
                        carbs_100g=2.0,
                        fat_100g=1.5,
                    )
                except AttributeError as exc:
                    self.fail(f"classify_food raised AttributeError for {bad_name!r} ({type(bad_name).__name__}): {exc}")
                except Exception as exc:
                    self.fail(f"classify_food raised unexpected {type(exc).__name__} for {bad_name!r}: {exc}")

                self.assertIsInstance(roles, list)
                self.assertGreater(len(roles), 0)
                self.assertTrue(all(isinstance(r, Role) for r in roles))
                self.assertIn(
                    Role.PROTEIN_MAIN,
                    roles,
                    f"Expected Tier B fingerprinting to identify PROTEIN_MAIN for {bad_name!r}, got {roles}",
                )

        # Profile 2: High carbohydrate grain profile (350 kcal, 10g P, 75g C, 1.0g F) -> classifies to CARB_BASE
        roles_carb = classify_food([1, 2], kcal_100g=350.0, protein_100g=10.0, carbs_100g=75.0, fat_100g=1.0)
        self.assertIn(Role.CARB_BASE, roles_carb)

    def test_get_portion_bounds_unhashable_and_non_string(self):
        """get_portion_bounds must handle non-string food_name without AttributeError

        and unhashable/invalid role inputs without TypeError, always returning a valid float tuple.
        """
        # 1. Non-string food_name with valid role: returns standard role bounds
        adversarial_names = [123, 45.67, True, False, 0, [1, 2], {"foo": "bar"}, object()]
        for bad_name in adversarial_names:
            with self.subTest(case="bad_food_name", bad_name=bad_name):
                try:
                    bounds = get_portion_bounds(Role.CARB_BASE, bad_name)
                except AttributeError as exc:
                    self.fail(f"get_portion_bounds raised AttributeError for food_name={bad_name!r}: {exc}")
                except Exception as exc:
                    self.fail(f"get_portion_bounds raised unexpected {type(exc).__name__} for food_name={bad_name!r}: {exc}")

                self.assertIsInstance(bounds, tuple)
                self.assertEqual(len(bounds), 2)
                self.assertEqual(bounds, (60.0, 120.0))

        # 2. Unhashable and invalid role inputs: returns safe fallback bounds (10.0, 100.0)
        adversarial_roles = [
            [Role.CARB_BASE],
            {"role": "carb"},
            {"role": Role.CARB_BASE},
            set(),
            [1, 2],
            123,
            45.67,
            True,
            object(),
            None,
        ]
        for bad_role in adversarial_roles:
            with self.subTest(case="bad_role", bad_role=bad_role):
                try:
                    bounds = get_portion_bounds(bad_role, "pasta")
                except TypeError as exc:
                    self.fail(f"get_portion_bounds raised TypeError for role={bad_role!r}: {exc}")
                except Exception as exc:
                    self.fail(f"get_portion_bounds raised unexpected {type(exc).__name__} for role={bad_role!r}: {exc}")

                self.assertIsInstance(bounds, tuple)
                self.assertEqual(len(bounds), 2)
                self.assertIsInstance(bounds[0], (int, float))
                self.assertIsInstance(bounds[1], (int, float))
                self.assertGreaterEqual(bounds[0], 0.0)
                self.assertLessEqual(bounds[0], bounds[1])
                self.assertEqual(bounds, (10.0, 100.0))

        # 3. Both unhashable role and non-string food_name simultaneously
        bounds_both = get_portion_bounds([Role.CARB_BASE], 123)
        self.assertEqual(bounds_both, (10.0, 100.0))

    def test_validate_slot_filling_anonymous_items(self):
        """Anonymous ingredient items (dicts without 'id' or 'name') must not trigger

        false positive 'Duplicate ingredient None' errors across or within slots.
        """
        # 1. Multi-cardinality slot (VEG_SIDE cardinality_max=2 in Main & Side Plate)
        arch_main = get_archetype("Main & Side Plate")
        item_veg1 = {"role": Role.VEG_SIDE, "resolved_quantity_g": 150.0}
        item_veg2 = {"role": Role.VEG_SIDE, "resolved_quantity_g": 120.0}
        item_prot = {"role": Role.PROTEIN_MAIN, "resolved_quantity_g": 180.0}
        item_fat = {"role": Role.FAT_SEASONING, "resolved_quantity_g": 15.0}

        assignments_dict = {
            Role.PROTEIN_MAIN: [item_prot],
            Role.VEG_SIDE: [item_veg1, item_veg2],
            Role.FAT_SEASONING: [item_fat],
        }
        res_dict = validate_slot_filling(arch_main, assignments_dict)
        self.assertTrue(
            res_dict.is_valid,
            f"Validation failed for anonymous items in multi-cardinality slot: {res_dict.errors}",
        )
        self.assertFalse(
            any("Duplicate ingredient 'None'" in err for err in res_dict.errors),
            f"Encountered false-positive 'Duplicate ingredient None': {res_dict.errors}",
        )

        # 2. Sequence mode across different slots (Grain / Pasta Dish)
        arch_pasta = get_archetype("Grain / Pasta Dish")
        seq_items = [
            {"role": Role.CARB_BASE, "resolved_quantity_g": 80.0},
            {"role": Role.FAT_SEASONING, "resolved_quantity_g": 10.0},
        ]
        res_seq = validate_slot_filling(arch_pasta, seq_items)
        self.assertTrue(
            res_seq.is_valid,
            f"Validation failed for anonymous items in sequence mode: {res_seq.errors}",
        )
        self.assertFalse(
            any("None" in err for err in res_seq.errors),
            f"Encountered unexpected 'None' duplicate error: {res_seq.errors}",
        )

        # 3. Explicit duplicate detection verification: same dictionary reference in multiple slots must still be caught
        shared_item = {"role": Role.CARB_BASE, "resolved_quantity_g": 80.0}
        duplicate_assignments = {
            Role.CARB_BASE: [shared_item],
            Role.FAT_SEASONING: [shared_item],
        }
        res_dup = validate_slot_filling(arch_pasta, duplicate_assignments)
        self.assertFalse(res_dup.is_valid, "Same ingredient instance assigned to two slots must fail validation.")
        self.assertTrue(
            any("Duplicate ingredient" in err for err in res_dup.errors),
            f"Expected duplicate error message, got: {res_dup.errors}",
        )

    def test_role_compatibility_unhashable_inputs(self):
        """are_roles_compatible, is_role_compatible, and clamp_ingredient_portion

        must safely handle unhashable, non-iterable, or invalid inputs returning False
        or clamped bounds without TypeError traceback.
        """
        # 1. are_roles_compatible with unhashable candidate or required role
        unhashable_pairs = [
            ([Role.CARB_BASE], Role.CARB_BASE),
            (Role.CARB_BASE, [Role.CARB_BASE]),
            ({"a": 1}, Role.CARB_BASE),
            (Role.CARB_BASE, {"a": 1}),
            ([1, 2], [3, 4]),
            (None, Role.CARB_BASE),
            (Role.CARB_BASE, None),
            (123, 456),
            (True, False),
            (object(), Role.CARB_BASE),
        ]
        for cand, req in unhashable_pairs:
            with self.subTest(fn="are_roles_compatible", cand=cand, req=req):
                try:
                    res = are_roles_compatible(cand, req)
                except TypeError as exc:
                    self.fail(f"are_roles_compatible raised TypeError for ({cand!r}, {req!r}): {exc}")
                except Exception as exc:
                    self.fail(f"are_roles_compatible raised unexpected {type(exc).__name__} for ({cand!r}, {req!r}): {exc}")
                self.assertIs(res, False, f"Expected False for invalid/unhashable pair ({cand!r}, {req!r})")

        # 2. is_role_compatible with non-iterables, unhashable required_role, or unhashable candidates
        unhashable_candidates_tests = [
            # Non-iterable candidate_roles
            (Role.CARB_BASE, None),
            (Role.CARB_BASE, 123),
            (Role.CARB_BASE, True),
            (Role.CARB_BASE, object()),
            # Unhashable required_role
            ([Role.CARB_BASE], [Role.CARB_BASE]),
            ({"role": "carb"}, [Role.CARB_BASE]),
            ([1, 2], [Role.CARB_BASE]),
            # Unhashable elements inside candidate_roles
            (Role.CARB_BASE, [[Role.CARB_BASE]]),
            (Role.CARB_BASE, [{"role": "carb"}]),
            (Role.CARB_BASE, [[1, 2], {"a": 1}]),
        ]
        for req, cands in unhashable_candidates_tests:
            with self.subTest(fn="is_role_compatible", req=req, cands=cands):
                try:
                    res = is_role_compatible(req, cands)
                except TypeError as exc:
                    self.fail(f"is_role_compatible raised TypeError for ({req!r}, {cands!r}): {exc}")
                except Exception as exc:
                    self.fail(f"is_role_compatible raised unexpected {type(exc).__name__} for ({req!r}, {cands!r}): {exc}")
                self.assertIs(res, False, f"Expected False for ({req!r}, {cands!r})")

        # Verify legitimate compatibility is preserved
        self.assertTrue(are_roles_compatible(Role.CARB_BASE, Role.CARB_BASE))
        self.assertTrue(are_roles_compatible(Role.VEGETABLE, Role.VEG_SAUCE))
        self.assertTrue(is_role_compatible(Role.CARB_BASE, [Role.CARB_BASE]))
        self.assertTrue(is_role_compatible(Role.VEG_SAUCE, [Role.VEGETABLE]))

        # 3. clamp_ingredient_portion with unhashable role inputs
        arch = get_archetype("Grain / Pasta Dish")
        unhashable_roles = [[Role.CARB_BASE], {"role": "CARB_BASE"}, None, 123]
        for bad_role in unhashable_roles:
            with self.subTest(fn="clamp_ingredient_portion", bad_role=bad_role):
                try:
                    clamped = clamp_ingredient_portion(arch, bad_role, 50.0)
                except TypeError as exc:
                    self.fail(f"clamp_ingredient_portion raised TypeError for bad_role={bad_role!r}: {exc}")
                except Exception as exc:
                    self.fail(f"clamp_ingredient_portion raised unexpected {type(exc).__name__}: {exc}")
                self.assertIsInstance(clamped, float)
                self.assertGreaterEqual(clamped, 10.0)
                self.assertLessEqual(clamped, 250.0)


class TestBaselineInvariants(unittest.TestCase):
    """Verifies that the database baseline remains strictly intact."""

    def test_database_baseline_counts_unchanged(self):
        conn = sqlite3.connect(DB_PATH)
        try:
            counts = {
                t[0]: conn.execute(f"SELECT COUNT(*) FROM {t[0]}").fetchone()[0]
                for t in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            self.assertEqual(counts.get("INVENTORY"), 36)
            self.assertEqual(counts.get("PRODUCTS"), 14)
            self.assertEqual(counts.get("RECIPES"), 3)
            self.assertEqual(counts.get("ALIASES"), 30)
            self.assertEqual(counts.get("PROFILE"), 0)
            self.assertEqual(counts.get("APP_SETTINGS"), 3)
        finally:
            conn.close()


class TestFoodPairingEngine(unittest.TestCase):
    """Unit tests for the Food Pairing & Compatibility Engine (M2).

    Covers:
    - 17 hard mutual exclusion rules across culinary group tags.
    - Symmetric pairwise soft affinity scoring matrix with 4-tier fallback.
    - Multi-ingredient dish harmony score combination and order invariance.
    """

    def test_seventeen_hard_mutual_exclusions(self):
        # EX-01: Seafood + Sweet Dairy
        self.assertTrue(are_mutually_exclusive("tonno", None, "yogurt", None))
        self.assertTrue(are_mutually_exclusive("salmon", None, "whole milk", None))

        # EX-02: Seafood + Chocolate / Sweets
        self.assertTrue(are_mutually_exclusive("salmone", None, "cioccolato", None))
        self.assertTrue(are_mutually_exclusive("tuna", None, "honey", None))

        # EX-03: Seafood + Sweet Fruit
        self.assertTrue(are_mutually_exclusive("merluzzo", None, "mela", None))
        self.assertTrue(are_mutually_exclusive("cod", None, "banana", None))

        # EX-04: Pasta / Grain + Sweet Fruit
        self.assertTrue(are_mutually_exclusive("Spaghetti n.5", {Role.CARB_BASE}, "Apples", {Role.FRUIT}))
        self.assertTrue(are_mutually_exclusive("carnaroli rice", None, "banana", None))

        # EX-05: Pasta / Grain + Sweet Dairy
        self.assertTrue(are_mutually_exclusive("penne rigate", None, "latte intero", None))
        self.assertTrue(are_mutually_exclusive("spaghetti", None, "plain yogurt", None))

        # EX-06: Pasta / Grain + Chocolate / Sweets
        self.assertTrue(are_mutually_exclusive("fusilli", None, "biscotti", None))
        self.assertTrue(are_mutually_exclusive("pasta", None, "chocolate", None))

        # EX-07: Savory Meat + Sweet Dairy
        self.assertTrue(are_mutually_exclusive("chicken breast", None, "whole milk", None))
        self.assertTrue(are_mutually_exclusive("beef", None, "yogurt", None))

        # EX-08: Savory Meat + Chocolate / Sweets
        self.assertTrue(are_mutually_exclusive("petto di pollo", None, "cioccolato", None))
        self.assertTrue(are_mutually_exclusive("turkey", None, "biscotti", None))

        # EX-09: Savory Meat + Sweet Fruit
        self.assertTrue(are_mutually_exclusive("manzo", None, "banane", None))
        self.assertTrue(are_mutually_exclusive("chicken breast", None, "apples", None))

        # EX-10: Savory Sauce + Sweet Fruit
        self.assertTrue(are_mutually_exclusive("passata di pomodoro", None, "mela", None))
        self.assertTrue(are_mutually_exclusive("tomato puree", None, "berries", None))

        # EX-11: Savory Sauce + Chocolate / Sweets
        self.assertTrue(are_mutually_exclusive("tomato puree", None, "miele", None))
        self.assertTrue(are_mutually_exclusive("tomato passata", None, "chocolate", None))

        # EX-12: Savory Sauce + Sweet Dairy
        self.assertTrue(are_mutually_exclusive("tomato passata", None, "plain yogurt", None))
        self.assertTrue(are_mutually_exclusive("tomato puree", None, "latte", None))

        # EX-13: Pungent Aroma + Chocolate / Sweets
        self.assertTrue(are_mutually_exclusive("garlic", None, "chocolate", None))
        self.assertTrue(are_mutually_exclusive("cipolla", None, "honey", None))

        # EX-14: Pungent Aroma + Sweet Fruit
        self.assertTrue(are_mutually_exclusive("cipolla", None, "apple", None))
        self.assertTrue(are_mutually_exclusive("garlic", None, "banana", None))

        # EX-15: Pungent Aroma + Sweet Dairy
        self.assertTrue(are_mutually_exclusive("aglio", None, "milk", None))
        self.assertTrue(are_mutually_exclusive("onion", None, "yogurt", None))

        # EX-16: Raw Flour + Sweet Fruit
        self.assertTrue(are_mutually_exclusive("00 flour", None, "apple", None))
        self.assertTrue(are_mutually_exclusive("farina", None, "banana", None))

        # EX-17: Raw Flour + Sweet Dairy
        self.assertTrue(are_mutually_exclusive("00 flour", None, "plain yogurt", None))
        self.assertTrue(are_mutually_exclusive("farina", None, "whole milk", None))

    def test_compatible_pairings_return_false(self):
        self.assertFalse(are_mutually_exclusive("chicken breast", None, "Carnaroli rice", None))
        self.assertFalse(are_mutually_exclusive("Spaghetti n.5", None, "Tomato puree", None))
        self.assertFalse(are_mutually_exclusive("Spaghetti n.5", None, "Extra virgin olive oil", None))
        self.assertFalse(are_mutually_exclusive("plain yogurt", None, "apples", None))
        self.assertFalse(are_mutually_exclusive("whole milk", None, "oats", None))
        self.assertFalse(are_mutually_exclusive("salmone", None, "zucchine", None))
        self.assertFalse(are_mutually_exclusive("parmigiano reggiano", None, "spaghetti", None))

    def test_exclusion_edge_cases_and_symmetry(self):
        # Empty or None items return False
        self.assertFalse(are_mutually_exclusive("", None, "chicken", None))
        self.assertFalse(are_mutually_exclusive(None, None, "chicken", None))
        self.assertFalse(are_mutually_exclusive("chicken", None, "", None))

        # Self-pairing returns False
        self.assertFalse(are_mutually_exclusive("chicken", None, "chicken", None))
        self.assertFalse(are_mutually_exclusive("spaghetti", None, "spaghetti n.5", None))

        # Symmetry
        self.assertEqual(
            are_mutually_exclusive("tonno", None, "yogurt", None),
            are_mutually_exclusive("yogurt", None, "tonno", None),
        )
        self.assertEqual(
            are_mutually_exclusive("spaghetti", None, "tomato passata", None),
            are_mutually_exclusive("tomato passata", None, "spaghetti", None),
        )

        # Defensive handling of unusual role containers
        self.assertTrue(are_mutually_exclusive("pasta", [Role.CARB_BASE], "milk", {"invalid": 123}))
        self.assertFalse(are_mutually_exclusive("pasta", None, "rice", [1, 2, "random"]))

    def test_soft_affinity_scores_matrix(self):
        self.assertEqual(get_pairing_score("tomato passata", "basil"), 0.98)
        self.assertEqual(get_pairing_score("spaghetti", "tomato passata"), 0.95)
        self.assertEqual(get_pairing_score("chicken", "rice"), 0.92)
        self.assertEqual(get_pairing_score("oats", "milk"), 0.95)
        self.assertEqual(get_pairing_score("honey", "yogurt"), 0.95)
        self.assertEqual(get_pairing_score("parmigiano reggiano", "tuna"), 0.20)
        self.assertEqual(get_pairing_score("chicken", "pasta"), 0.65)

    def test_soft_affinity_fallbacks_and_bounds(self):
        # Excluded pairs return 0.0
        self.assertEqual(get_pairing_score("tonno", "plain yogurt"), 0.0)
        self.assertEqual(get_pairing_score("spaghetti", "apple"), 0.0)

        # Self-pair returns 1.0
        self.assertEqual(get_pairing_score("chicken", "chicken"), 1.0)

        # Empty strings return 0.0
        self.assertEqual(get_pairing_score("", "chicken"), 0.0)
        self.assertEqual(get_pairing_score(None, "chicken"), 0.0)

        # Role fallback: unknown savory vegetable + fat seasoning -> 0.70
        score_veg_fat = get_pairing_score("Zucchina Romanesca", "Extra virgin olive oil")
        self.assertEqual(score_veg_fat, 0.70)

        # Neutral default for unmapped pairs -> 0.50
        score_unmapped = get_pairing_score("unknown_product_a", "unknown_product_b")
        self.assertEqual(score_unmapped, 0.50)

        # Clamping invariant
        self.assertGreaterEqual(score_unmapped, 0.0)
        self.assertLessEqual(score_unmapped, 1.0)

        # Symmetry
        self.assertEqual(
            get_pairing_score("chicken", "rice"),
            get_pairing_score("rice", "chicken"),
        )

    def test_combination_scoring(self):
        # Classic pasta dish achieves high harmony (> 0.90)
        score_pasta = score_combination(["Spaghetti n.5", "Tomato puree", "EVO oil", "Parmigiano Reggiano"])
        self.assertGreater(score_pasta, 0.90)

        # Clashing combination returns 0.0
        score_clash = score_combination(["Spaghetti n.5", "Tomato puree", "Plain yogurt"])
        self.assertEqual(score_clash, 0.0)

        # Edge cases
        self.assertEqual(score_combination([]), 0.0)
        self.assertEqual(score_combination(["Spaghetti"]), 1.0)
        self.assertEqual(score_combination([None, "Spaghetti", ""]), 1.0)

        # Order invariance
        combo_a = ["spaghetti", "tomato passata", "evo oil"]
        combo_b = ["evo oil", "spaghetti", "tomato passata"]
        self.assertEqual(score_combination(combo_a), score_combination(combo_b))

    def test_regression_oats_compatibility_and_breakfast_harmony(self):
        """Regression test for Bug 1: Oats + honey/chocolate/sweets compatibility and breakfast bowl harmony > 0.80."""
        # Oats with sweets/chocolate must not be mutually exclusive
        self.assertFalse(are_mutually_exclusive("oats", None, "honey", None))
        self.assertFalse(are_mutually_exclusive("avena", None, "miele", None))
        self.assertFalse(are_mutually_exclusive("oats", None, "chocolate", None))
        self.assertFalse(are_mutually_exclusive("avena", None, "cioccolato", None))
        self.assertFalse(are_mutually_exclusive("oats", None, "cacao", None))
        self.assertFalse(are_mutually_exclusive("oats", None, "biscotti", None))

        # Soft affinity scores
        self.assertEqual(get_pairing_score("honey", "oats"), 0.92)
        self.assertEqual(get_pairing_score("apple", "oats"), 0.88)
        self.assertEqual(get_pairing_score("honey", "walnuts"), 0.90)

        # Standard breakfast bowl must achieve high harmony (> 0.80)
        breakfast_bowl = ["oats", "milk", "apple", "honey", "walnuts"]
        bowl_score = score_combination(breakfast_bowl)
        self.assertGreater(bowl_score, 0.80, f"Breakfast bowl score {bowl_score} should be > 0.80")

    def test_regression_fruit_inflection_leaks_plugged(self):
        """Regression test for Bug 2: Singular/plural fruit inflections are mutually exclusive with savory foods."""
        fruits = [
            "strawberries", "fragola", "blueberries", "mirtillo",
            "peaches", "pesche", "oranges", "arance",
            "pears", "pere", "apricots", "albicocche",
            "grapes", "uve", "bananas", "pineapples",
        ]
        savory_items = ["spaghetti", "penne rigate", "riso", "tonno", "salsa"]
        for fruit in fruits:
            for savory in savory_items:
                self.assertTrue(
                    are_mutually_exclusive(savory, None, fruit, None),
                    f"Fruit inflection leak detected: {savory} + {fruit}",
                )
                self.assertEqual(
                    score_combination([savory, fruit]),
                    0.0,
                    f"Combination did not collapse for {savory} + {fruit}",
                )

    def test_regression_role_preservation_in_score_combination(self):
        """Regression test for Bug 3: Candidate roles are preserved during score_combination evaluation."""
        # Custom item names that don't match keywords
        c_carb = IngredientCandidate(
            product_id=1,
            name="custom_grain",
            role=Role.CARB_BASE,
            all_eligible_roles=[Role.CARB_BASE],
        )
        c_fruit = IngredientCandidate(
            product_id=2,
            name="custom_berry",
            role=Role.FRUIT,
            all_eligible_roles=[Role.FRUIT],
        )
        # CARB_BASE + FRUIT is mutually exclusive at role level
        self.assertEqual(score_combination([c_carb, c_fruit]), 0.0)

        # Mutual breakfast items with custom names should pair harmoniously (0.80)
        c_cereal = IngredientCandidate(
            product_id=3,
            name="custom_flakes",
            role=Role.CEREAL_CARB,
            all_eligible_roles=[Role.CEREAL_CARB],
        )
        c_dairy = IngredientCandidate(
            product_id=4,
            name="custom_yogurt",
            role=Role.DAIRY_BASE,
            all_eligible_roles=[Role.DAIRY_BASE],
        )
        self.assertGreaterEqual(score_combination([c_cereal, c_dairy]), 0.80)

    def test_regression_savory_fat_vs_sweet_dairy_exclusions(self):
        """Regression test for Bug 4: Savory fats are mutually exclusive with sweet breakfast components."""
        fats = ["Extra virgin olive oil", "olio", "burro", "butter", "strutto", "lard"]
        sweets = ["Whole milk", "latte", "plain yogurt", "skyr", "honey", "miele", "cioccolato", "apples", "banane"]

        for fat in fats:
            for sweet in sweets:
                self.assertTrue(
                    are_mutually_exclusive(fat, None, sweet, None),
                    f"Expected {fat} + {sweet} to be mutually exclusive",
                )

        # FAT_SEASONING cannot satisfy NUTS_OR_SWEET slot
        self.assertFalse(are_roles_compatible(Role.FAT_SEASONING, Role.NUTS_OR_SWEET))
        self.assertFalse(is_role_compatible(Role.NUTS_OR_SWEET, [Role.FAT_SEASONING]))

        # Tree nuts remain compatible with dairy and do not inherit SAVORY_FAT exclusion
        self.assertFalse(are_mutually_exclusive("almonds", {Role.NUTS_OR_SWEET, Role.FAT_SEASONING}, "plain yogurt", {Role.DAIRY_BASE}))
        self.assertFalse(are_mutually_exclusive("walnuts", {Role.NUTS_OR_SWEET, Role.FAT_SEASONING}, "Whole milk", {Role.DAIRY_BASE}))
        self.assertFalse(are_mutually_exclusive("mandorle", {Role.NUTS_OR_SWEET, Role.FAT_SEASONING}, "latte", {Role.DAIRY_BASE}))
        self.assertFalse(are_mutually_exclusive("noci", {Role.NUTS_OR_SWEET, Role.FAT_SEASONING}, "yogurt", {Role.DAIRY_BASE}))
        self.assertEqual(get_pairing_score("almonds", "yogurt"), 0.90)
        self.assertEqual(get_pairing_score("walnuts", "yogurt"), 0.90)


class TestOptimizationSolver(unittest.TestCase):
    """Unit tests for SciPy HiGHS simplex optimization and knapsack fallback solver (M2)."""

    def test_compute_ingredient_cost(self):
        # Opened package bonus (-10.0)
        cand_open = IngredientCandidate(name="Penne", open_remainder=250.0, days_to_expiration=30.0, pairing_score=0.5)
        cand_sealed = IngredientCandidate(name="Penne", open_remainder=0.0, days_to_expiration=30.0, pairing_score=0.5)
        cost_open = compute_ingredient_cost(cand_open)
        cost_sealed = compute_ingredient_cost(cand_sealed)
        self.assertAlmostEqual(cost_open - cost_sealed, -10.0)

        # FIFO bonus (-5.0 / days)
        cand_urgent = IngredientCandidate(name="Meat", days_to_expiration=1.0, pairing_score=0.5)
        cand_distant = IngredientCandidate(name="Meat", days_to_expiration=50.0, pairing_score=0.5)
        self.assertLess(compute_ingredient_cost(cand_urgent), compute_ingredient_cost(cand_distant))

        # Pairing bonus (-2.0 * score)
        cand_high_pair = IngredientCandidate(name="Sauce", pairing_score=1.0)
        cand_low_pair = IngredientCandidate(name="Sauce", pairing_score=0.0)
        self.assertAlmostEqual(compute_ingredient_cost(cand_high_pair) - compute_ingredient_cost(cand_low_pair), -2.0)

        # Missing penalty (+5.0)
        cand_missing = IngredientCandidate(name="Item", is_missing=True, pairing_score=0.5)
        cand_pantry = IngredientCandidate(name="Item", is_missing=False, pairing_score=0.5)
        self.assertAlmostEqual(compute_ingredient_cost(cand_missing) - compute_ingredient_cost(cand_pantry), 5.0)

    def test_solver_convergence_caloric_targets(self):
        """Tests that the HiGHS simplex solver converges to exact target kcal within ±5%."""
        targets = [500.0, 700.0, 1000.0]

        for target in targets:
            with self.subTest(target_kcal=target):
                candidates = [
                    IngredientCandidate(
                        product_id=1,
                        name="Spaghetti",
                        role=Role.CARB_BASE,
                        kcal_100g=355.0,
                        protein_100g=12.0,
                        carbs_100g=72.0,
                        fat_100g=1.5,
                        portion_min_g=60.0,
                        portion_max_g=180.0,
                        available_stock_g=500.0,
                    ),
                    IngredientCandidate(
                        product_id=2,
                        name="Tomato puree",
                        role=Role.VEG_SAUCE,
                        kcal_100g=25.0,
                        protein_100g=1.2,
                        carbs_100g=4.0,
                        fat_100g=0.2,
                        portion_min_g=60.0,
                        portion_max_g=200.0,
                        available_stock_g=500.0,
                    ),
                    IngredientCandidate(
                        product_id=3,
                        name="EVO oil",
                        role=Role.FAT_SEASONING,
                        kcal_100g=884.0,
                        protein_100g=0.0,
                        carbs_100g=0.0,
                        fat_100g=100.0,
                        portion_min_g=5.0,
                        portion_max_g=20.0,
                        available_stock_g=500.0,
                    ),
                    IngredientCandidate(
                        product_id=4,
                        name="Parmigiano",
                        role=Role.AROMA_FINISHING,
                        kcal_100g=392.0,
                        protein_100g=33.0,
                        carbs_100g=0.0,
                        fat_100g=28.0,
                        portion_min_g=5.0,
                        portion_max_g=30.0,
                        available_stock_g=250.0,
                    ),
                ]

                res = solve_ingredient_quantities(candidates, target_kcal=target, tolerance=0.05)
                self.assertTrue(res.success, f"Solver failed for target {target}: {res.message}")
                delta_pct = abs(res.total_kcal - target) / target
                self.assertLessEqual(delta_pct, 0.05, f"Caloric delta {delta_pct:.3f} exceeded 5% for target {target}")
                self.assertIn("Spaghetti", res.quantities)
                self.assertIn(1, res.quantities_g)
                self.assertGreater(res.quantities["Spaghetti"], 0.0)

    def test_solver_fifo_and_opened_priority(self):
        """Verifies that an opened or expiring ingredient receives higher dosage than an identical sealed one."""
        cand_urgent = IngredientCandidate(
            product_id=1,
            name="Open Rice",
            role=Role.CARB_BASE,
            kcal_100g=350.0,
            portion_min_g=50.0,
            portion_max_g=150.0,
            available_stock_g=500.0,
            is_open=True,
            days_to_expiration=2.0,
        )
        cand_sealed = IngredientCandidate(
            product_id=2,
            name="Sealed Rice",
            role=Role.CARB_BASE,
            kcal_100g=350.0,
            portion_min_g=50.0,
            portion_max_g=150.0,
            available_stock_g=500.0,
            is_open=False,
            days_to_expiration=90.0,
        )
        cand_oil = IngredientCandidate(
            product_id=3,
            name="Oil",
            role=Role.FAT_SEASONING,
            kcal_100g=884.0,
            portion_min_g=10.0,
            portion_max_g=10.0,
            available_stock_g=100.0,
        )

        res = solve_ingredient_quantities([cand_urgent, cand_sealed, cand_oil], target_kcal=600.0)
        self.assertTrue(res.success)
        self.assertGreater(res.quantities["Open Rice"], res.quantities["Sealed Rice"])

    def test_solver_fallback_knapsack_mode(self):
        """Verifies pure-Python continuous knapsack fallback mode works and converges."""
        candidates = [
            IngredientCandidate(
                product_id=1,
                name="Spaghetti",
                role=Role.CARB_BASE,
                kcal_100g=355.0,
                portion_min_g=60.0,
                portion_max_g=150.0,
                available_stock_g=500.0,
            ),
            IngredientCandidate(
                product_id=2,
                name="Oil",
                role=Role.FAT_SEASONING,
                kcal_100g=884.0,
                portion_min_g=5.0,
                portion_max_g=15.0,
                available_stock_g=200.0,
            ),
        ]
        res = solve_ingredient_quantities(candidates, target_kcal=500.0, force_fallback=True)
        self.assertTrue(res.success)
        self.assertEqual(res.solver_name, "knapsack_fallback")
        delta_pct = abs(res.total_kcal - 500.0) / 500.0
        self.assertLessEqual(delta_pct, 0.05)

    def test_solver_proportional_scaling_on_overconstrained_bounds(self):
        """Verifies knapsack fallback scales portions proportionally when bounds are over/under-constrained."""
        # Case A: Min portions exceed target (Over-constrained high)
        huge_min_items = [
            IngredientCandidate(product_id=1, name="Item 1", kcal_100g=400.0, portion_min_g=100.0, portion_max_g=200.0, available_stock_g=500.0),
            IngredientCandidate(product_id=2, name="Item 2", kcal_100g=400.0, portion_min_g=100.0, portion_max_g=200.0, available_stock_g=500.0),
        ]  # min kcal = 800 kcal
        res_high = solve_ingredient_quantities(huge_min_items, target_kcal=400.0, force_fallback=True)
        self.assertTrue(res_high.success)
        self.assertIn("scaled down", res_high.message.lower())
        self.assertAlmostEqual(res_high.total_kcal, 400.0, delta=20.0)

        # Case B: Max portions below target (Over-constrained low)
        tiny_max_items = [
            IngredientCandidate(product_id=1, name="Item 1", kcal_100g=100.0, portion_min_g=10.0, portion_max_g=20.0, available_stock_g=20.0),
            IngredientCandidate(product_id=2, name="Item 2", kcal_100g=100.0, portion_min_g=10.0, portion_max_g=20.0, available_stock_g=20.0),
        ]  # max kcal = 40 kcal
        res_low = solve_ingredient_quantities(tiny_max_items, target_kcal=500.0, force_fallback=True)
        self.assertTrue(res_low.success)
        self.assertIn("scaled up", res_low.message.lower())

    def test_solver_error_cases(self):
        res_empty = solve_ingredient_quantities([], target_kcal=500.0)
        self.assertFalse(res_empty.success)
        self.assertIn("No ingredient candidates", res_empty.message)

        res_zero = solve_ingredient_quantities([IngredientCandidate(name="Food")], target_kcal=0.0)
        self.assertFalse(res_zero.success)
        self.assertIn("must be positive", res_zero.message)

    def test_regression_solver_zero_available_stock_bound_strictness(self):
        """Regression test for Bug 5: Ingredient with available_stock_g = 0.0 is strictly assigned 0.0g."""
        depleted = IngredientCandidate(
            product_id=1,
            name="Depleted Pasta",
            role=Role.CARB_BASE,
            kcal_100g=350.0,
            portion_min_g=60.0,
            portion_max_g=150.0,
            available_stock_g=0.0,
            is_missing=False,
        )
        available = IngredientCandidate(
            product_id=2,
            name="Available Rice",
            role=Role.CARB_BASE,
            kcal_100g=360.0,
            portion_min_g=60.0,
            portion_max_g=150.0,
            available_stock_g=500.0,
            is_missing=False,
        )
        oil = IngredientCandidate(
            product_id=3,
            name="Available Oil",
            role=Role.FAT_SEASONING,
            kcal_100g=884.0,
            portion_min_g=5.0,
            portion_max_g=15.0,
            available_stock_g=100.0,
            is_missing=False,
        )

        # HiGHS LP solver test
        res_highs = solve_ingredient_quantities([depleted, available, oil], target_kcal=500.0)
        self.assertTrue(res_highs.success)
        self.assertEqual(res_highs.quantities["Depleted Pasta"], 0.0)
        self.assertGreater(res_highs.quantities["Available Rice"], 0.0)

        # Knapsack fallback solver test
        res_knap = solve_ingredient_quantities([depleted, available, oil], target_kcal=500.0, force_fallback=True)
        self.assertTrue(res_knap.success)
        self.assertEqual(res_knap.quantities["Depleted Pasta"], 0.0)
        self.assertGreater(res_knap.quantities["Available Rice"], 0.0)

    def test_regression_solver_zero_portion_max_bound_strictness(self):
        """Regression test for Bug 6: Ingredient with portion_max_g = 0.0 is strictly assigned 0.0g."""
        forbidden = IngredientCandidate(
            product_id=1,
            name="Excluded Item",
            kcal_100g=350.0,
            portion_min_g=0.0,
            portion_max_g=0.0,
            available_stock_g=500.0,
        )
        normal = IngredientCandidate(
            product_id=2,
            name="Normal Item",
            kcal_100g=350.0,
            portion_min_g=50.0,
            portion_max_g=150.0,
            available_stock_g=500.0,
        )

        # HiGHS LP solver test
        res_highs = solve_ingredient_quantities([forbidden, normal], target_kcal=350.0)
        self.assertTrue(res_highs.success)
        self.assertEqual(res_highs.quantities["Excluded Item"], 0.0)
        self.assertGreater(res_highs.quantities["Normal Item"], 0.0)

        # Knapsack fallback solver test
        res_knap = solve_ingredient_quantities([forbidden, normal], target_kcal=350.0, force_fallback=True)
        self.assertTrue(res_knap.success)
        self.assertEqual(res_knap.quantities["Excluded Item"], 0.0)
        self.assertGreater(res_knap.quantities["Normal Item"], 0.0)

        # Knapsack Case B (over-constrained low: target 1000 kcal, normal max only 20g, forbidden has 500g stock)
        small_normal = IngredientCandidate(
            product_id=3,
            name="Small Normal",
            kcal_100g=100.0,
            portion_min_g=10.0,
            portion_max_g=20.0,
            available_stock_g=20.0,
        )
        res_case_b = solve_ingredient_quantities([forbidden, small_normal], target_kcal=1000.0, force_fallback=True)
        self.assertTrue(res_case_b.success)
        self.assertEqual(res_case_b.quantities["Excluded Item"], 0.0)
        self.assertGreater(res_case_b.quantities["Small Normal"], 0.0)


class TestRecipeGeneratorPipeline(unittest.TestCase):
    """Unit tests for the end-to-end RecipeGenerator orchestrator pipeline (M2)."""

    def test_pantry_candidates_extraction(self):
        conn = get_connection()
        try:
            candidates = extract_pantry_candidates(conn)
            self.assertGreater(len(candidates), 5)
            for c in candidates:
                self.assertGreater(c.product_id, 0)
                self.assertGreater(c.available_stock_g, 0.0)
                self.assertFalse(c.is_missing)
                self.assertGreater(len(c.all_eligible_roles), 0)

            # Check that opened items are sorted towards the beginning
            has_open_flags = [c.is_open for c in candidates]
            # If any open items exist, the first items must be open
            if any(has_open_flags):
                self.assertTrue(has_open_flags[0])
        finally:
            conn.close()

    def test_catalog_candidates_extraction(self):
        conn = get_connection()
        try:
            catalog = extract_catalog_candidates(conn)
            self.assertEqual(len(catalog), 14)  # 14 baseline products
            for c in catalog:
                self.assertTrue(c.is_missing)
                self.assertEqual(c.available_stock_g, 9999.0)
                self.assertEqual(c.cost_coefficient, 5.0)
        finally:
            conn.close()

    def test_title_synthesis(self):
        arch_pasta = get_archetype("Grain / Pasta Dish")
        arch_main = get_archetype("Main & Side Plate")
        arch_bkt = get_archetype("Breakfast / Snack Bowl")

        # Pasta with tomato and parmigiano
        ing_pasta = [
            IngredientCandidate(name="Spaghetti n.5", role=Role.CARB_BASE),
            IngredientCandidate(name="Tomato puree", role=Role.VEG_SAUCE),
            IngredientCandidate(name="Extra virgin olive oil", role=Role.FAT_SEASONING),
            IngredientCandidate(name="Parmigiano Reggiano", role=Role.AROMA_FINISHING),
        ]
        title_pasta = synthesize_recipe_title(arch_pasta, ing_pasta)
        self.assertEqual(title_pasta, "Spaghetti with Tomato Sauce and Parmigiano")

        # Main plate: chicken and zucchini
        ing_main = [
            IngredientCandidate(name="Chicken breast", role=Role.PROTEIN_MAIN),
            IngredientCandidate(name="Zucchine", role=Role.VEG_SIDE),
            IngredientCandidate(name="Extra virgin olive oil", role=Role.FAT_SEASONING),
        ]
        title_main = synthesize_recipe_title(arch_main, ing_main)
        self.assertEqual(title_main, "Chicken Breast with Zucchini")

        # Breakfast bowl: yogurt and biscuits
        ing_bkt = [
            IngredientCandidate(name="Plain yogurt", role=Role.DAIRY_BASE),
            IngredientCandidate(name="biscotti", role=Role.CEREAL_CARB),
            IngredientCandidate(name="Apples", role=Role.FRUIT),
        ]
        title_bkt = synthesize_recipe_title(arch_bkt, ing_bkt)
        self.assertEqual(title_bkt, "Yogurt Bowl with Biscuits and Apples")

    def test_cooking_instructions_synthesis(self):
        arch_pasta = get_archetype("Grain / Pasta Dish")
        ingredients = [
            IngredientCandidate(name="Spaghetti n.5", role=Role.CARB_BASE),
            IngredientCandidate(name="Tomato puree", role=Role.VEG_SAUCE),
            IngredientCandidate(name="Extra virgin olive oil", role=Role.FAT_SEASONING),
            IngredientCandidate(name="Parmigiano Reggiano", role=Role.AROMA_FINISHING),
        ]
        steps = synthesize_cooking_instructions(arch_pasta, ingredients)
        self.assertEqual(len(steps), 4)
        self.assertTrue(any(w in steps[0].lower() for w in ["weigh", "wash", "prepare", "arrange"]))
        self.assertTrue(any(w in steps[1].lower() for w in ["boil", "cook", "pour", "warm", "sauté", "sear"]))
        self.assertTrue(any(w in steps[2].lower() for w in ["drain", "toss", "plate", "top", "stir"]))
        self.assertTrue(any(w in steps[3].lower() for w in ["finish", "serve", "season"]))

    def test_end_to_end_generate_lunch(self):
        recipe = RecipeGenerator.generate(target_kcal=700.0, meal_type="lunch")
        self.assertIsInstance(recipe, RecipeCandidate)
        self.assertEqual(recipe.archetype_name, "Grain / Pasta Dish")
        self.assertEqual(recipe.meal_type, "lunch")
        delta_pct = abs(recipe.total_kcal - 700.0) / 700.0
        self.assertLessEqual(delta_pct, 0.05)
        self.assertGreaterEqual(len(recipe.ingredients), 2)
        for ing in recipe.ingredients:
            self.assertGreater(ing.resolved_quantity_g, 0.0)
        self.assertEqual(len(recipe.instructions), 4)

    def test_end_to_end_generate_dinner(self):
        recipe = RecipeGenerator.generate(target_kcal=650.0, meal_type="dinner")
        self.assertIsInstance(recipe, RecipeCandidate)
        self.assertIn(recipe.archetype_name, ["Main & Side Plate", "One-Pot Balanced Bowl"])
        delta_pct = abs(recipe.total_kcal - 650.0) / 650.0
        self.assertLessEqual(delta_pct, 0.05)

    def test_end_to_end_generate_breakfast(self):
        recipe = RecipeGenerator.generate(target_kcal=450.0, meal_type="breakfast")
        self.assertIsInstance(recipe, RecipeCandidate)
        self.assertEqual(recipe.archetype_name, "Breakfast / Snack Bowl")
        delta_pct = abs(recipe.total_kcal - 450.0) / 450.0
        self.assertLessEqual(delta_pct, 0.05)

    def test_create_recipe_kwargs_and_persistence_on_temp_db(self):
        """Verifies recipe candidate can be committed via create_recipe in an isolated temporary database copy."""
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_db = os.path.join(tmpdir, "test_inventory.db")
            shutil.copy(DB_PATH, temp_db)

            temp_conn = sqlite3.connect(temp_db)
            temp_conn.execute("PRAGMA foreign_keys = ON;")
            try:
                recipe = RecipeGenerator.generate(target_kcal=700.0, meal_type="lunch", db_path=temp_db)
                kwargs = recipe.to_create_recipe_kwargs()
                self.assertIn("name", kwargs)
                self.assertIn("meals", kwargs)
                self.assertIn("ingredients", kwargs)
                self.assertIn("notes", kwargs)

                saved = create_recipe(temp_conn, **kwargs)
                self.assertIn("Recipe_ID", saved)
                self.assertGreater(saved["Recipe_ID"], 0)

                # Verify recipe count in temp DB is now 4
                count = temp_conn.execute("SELECT COUNT(*) FROM RECIPES").fetchone()[0]
                self.assertEqual(count, 4)
            finally:
                temp_conn.close()

            # Master database remains untouched (3 recipes)
            master_conn = sqlite3.connect(DB_PATH)
            try:
                master_count = master_conn.execute("SELECT COUNT(*) FROM RECIPES").fetchone()[0]
                self.assertEqual(master_count, 3)
            finally:
                master_conn.close()

    def test_pantry_only_true_raises_error_when_slot_unfillable(self):
        """Verifies that if a required slot cannot be filled from pantry, ValueError is raised in pantry_only mode,

        and catalog items are used when pantry_only=False.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_db = os.path.join(tmpdir, "test_empty_fat.db")
            shutil.copy(DB_PATH, temp_db)

            temp_conn = sqlite3.connect(temp_db)
            try:
                # Delete all fats (EVO oil) from INVENTORY in temp DB
                temp_conn.execute("DELETE FROM INVENTORY WHERE ID = 105")
                temp_conn.commit()

                # pantry_only=True must raise ValueError
                gen = RecipeGenerator(db_path=temp_db)
                with self.assertRaises(ValueError) as ctx:
                    gen.generate_recipe(target_kcal=700.0, meal_type="lunch", pantry_only=True)
                self.assertIn("FAT_SEASONING", str(ctx.exception))

                # pantry_only=False must succeed by using catalog fallback
                recipe = gen.generate_recipe(target_kcal=700.0, meal_type="lunch", pantry_only=False)
                self.assertIsInstance(recipe, RecipeCandidate)
                missing_items = [ing for ing in recipe.ingredients if ing.is_missing]
                self.assertTrue(any("oil" in ing.name.lower() or "olio" in ing.name.lower() for ing in missing_items))
            finally:
                temp_conn.close()

    def test_regression_breakfast_bowl_never_contains_cooking_fats(self):
        """Regression test for Bug 4: Breakfast generator never recommends cooking oil/lard for cereal/dairy bowls."""
        gen = RecipeGenerator()
        for p_only in [True, False]:
            r = gen.generate_recipe(
                target_kcal=450,
                meal_type="breakfast",
                archetype_name="Breakfast / Snack Bowl",
                pantry_only=p_only,
            )
            self.assertIsNotNone(r)
            for ing in r.ingredients:
                self.assertNotEqual(ing.role, Role.FAT_SEASONING, f"Found FAT_SEASONING in breakfast: {ing.name}")
                lower = ing.name.lower()
                self.assertNotIn("olive oil", lower)
                self.assertNotIn("olio", lower)
                self.assertNotIn("burro", lower)
                self.assertNotIn("butter", lower)
                self.assertNotIn("strutto", lower)
                self.assertNotIn("lard", lower)


class TestRecipeGeneratorGUI(unittest.TestCase):
    """Milestone 3 GUI integration tests for RecipeGeneratorDialog and App toolbar button."""

    def test_app_button_and_open_generator_dialog(self):
        from meal_planner.gui.app import App
        from meal_planner.gui.dialogs import RecipeGeneratorDialog

        with tempfile.TemporaryDirectory() as tmpdir:
            temp_db = os.path.join(tmpdir, "test_gui_gen.db")
            shutil.copy(DB_PATH, temp_db)

            app = App()
            app.withdraw()
            try:
                app.db_path = temp_db
                self.assertTrue(hasattr(app, "btn_generate_recipe"))
                self.assertIn("Generate Recipe", app.btn_generate_recipe.cget("text"))

                dlg = app.open_recipe_generator_dialog(wait=False)
                self.assertIsInstance(dlg, RecipeGeneratorDialog)
                self.assertIsNotNone(dlg.current_candidate)
                self.assertTrue(dlg.v_title.get().strip())
                self.assertGreater(len(dlg.t_ingredients.get_children()), 0)
                dlg.cancel()
            finally:
                app.destroy()

    def test_generator_dialog_regenerate_and_save_workflow(self):
        import tkinter as tk
        from meal_planner.gui.dialogs import RecipeGeneratorDialog

        with tempfile.TemporaryDirectory() as tmpdir:
            temp_db = os.path.join(tmpdir, "test_gui_save.db")
            shutil.copy(DB_PATH, temp_db)

            root = tk.Tk()
            root.withdraw()
            saved_outcomes = []
            try:
                dlg = RecipeGeneratorDialog(
                    root,
                    db_path=temp_db,
                    wait=False,
                    on_save_callback=lambda out: saved_outcomes.append(out),
                )
                self.assertIsNotNone(dlg.current_candidate)

                # Test meal switching and regeneration
                dlg.v_meal.set("dinner")
                dlg._on_meal_changed()
                self.assertEqual(dlg.v_target_kcal.get(), "650")
                self.assertTrue(dlg.regenerate_proposal())
                self.assertEqual(dlg.current_candidate.meal_type, "dinner")

                # Customize title and save
                dlg.v_title.set("Generated Custom Dinner")
                self.assertTrue(dlg.save())
                self.assertEqual(len(saved_outcomes), 1)
                self.assertEqual(saved_outcomes[0]["Name"], "Generated Custom Dinner")

                # Verify persisted in temp DB
                conn = sqlite3.connect(temp_db)
                try:
                    row = conn.execute(
                        "SELECT Recipe_ID FROM RECIPES WHERE Name = 'Generated Custom Dinner'"
                    ).fetchone()
                    self.assertIsNotNone(row)
                finally:
                    conn.close()
            finally:
                root.destroy()


class TestGeneratorStressAndLogicalConsistency(unittest.TestCase):
    """Deep stress-testing and logical consistency verification suite."""

    def test_1_sql_view_macro_identity_across_meals_and_variations(self):
        """Verifies that RecipeCandidate macro totals match SQLite RECIPE_MACROS_VIEW after persistence."""
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_db = os.path.join(tmpdir, "test_macro_identity.db")
            shutil.copy(DB_PATH, temp_db)
            conn = sqlite3.connect(temp_db)
            conn.execute("PRAGMA foreign_keys = ON;")
            try:
                gen = RecipeGenerator(db_path=temp_db)
                idx = 0
                for meal, kcal in [("lunch", 700.0), ("dinner", 650.0), ("breakfast", 450.0)]:
                    for var_i in range(3):
                        cand = gen.generate_recipe(
                            target_kcal=kcal,
                            meal_type=meal,
                            pantry_only=False,
                            db_path=temp_db,
                            variation_index=var_i,
                        )
                        idx += 1
                        kwargs = cand.to_create_recipe_kwargs()
                        kwargs["name"] = f"{cand.title} #{idx}"
                        out = create_recipe(conn, **kwargs)
                        rid = out["Recipe_ID"]

                        row = conn.execute(
                            "SELECT Kcal, Protein, Carbs, Fat, Fiber FROM RECIPE_MACROS_VIEW WHERE Recipe_ID = ?",
                            (rid,),
                        ).fetchone()
                        self.assertIsNotNone(row)
                        db_kcal, db_prot, db_carb, db_fat, db_fib = row
                        self.assertAlmostEqual(cand.total_kcal, db_kcal, delta=0.5)
                        self.assertAlmostEqual(cand.total_protein, db_prot, delta=0.2)
                        self.assertAlmostEqual(cand.total_carbs, db_carb, delta=0.2)
                        self.assertAlmostEqual(cand.total_fat, db_fat, delta=0.2)
                        self.assertAlmostEqual(cand.total_fiber, db_fib, delta=0.2)
            finally:
                conn.close()

    def test_2_no_zero_gram_or_ghost_ingredients_in_generated_recipes(self):
        """Verifies no generated recipe ever includes an ingredient with 0.0g or <= 0 display quantity."""
        gen = RecipeGenerator()
        for meal, kcal in [("lunch", 700.0), ("dinner", 650.0), ("breakfast", 450.0)]:
            for var_i in range(5):
                cand = gen.generate_recipe(
                    target_kcal=kcal,
                    meal_type=meal,
                    pantry_only=False,
                    variation_index=var_i,
                )
                for ing in cand.ingredients:
                    self.assertGreater(
                        ing.resolved_quantity_g,
                        0.0,
                        f"Ghost 0g ingredient '{ing.name}' in '{cand.title}'",
                    )
                    qty, _ = ing.calculate_display_quantity()
                    self.assertGreater(qty, 0.0)

    def test_3_no_duplicate_canonical_ingredients_across_slots(self):
        """Verifies that two products with the same canonical identity (e.g. Penne and Spaghetti) never coexist in one recipe."""
        gen = RecipeGenerator()
        for arch in ["Grain / Pasta Dish", "Main & Side Plate", "One-Pot Balanced Bowl"]:
            for var_i in range(8):
                cand = gen.generate_recipe(
                    target_kcal=700.0,
                    meal_type="lunch",
                    archetype_name=arch,
                    pantry_only=False,
                    variation_index=var_i,
                )
                canons = [resolve_canonical(ing.name) for ing in cand.ingredients]
                self.assertEqual(
                    len(canons),
                    len(set(canons)),
                    f"Duplicate canonical ingredients {canons} in '{cand.title}'",
                )
                # Ensure dry pasta is never used as CARB_SIDE in Main & Side Plate
                if arch == "Main & Side Plate":
                    for ing in cand.ingredients:
                        if ing.role == Role.CARB_SIDE:
                            self.assertNotEqual(resolve_canonical(ing.name), "pasta")

    def test_4_shared_candidate_mutation_and_anonymous_id_isolation(self):
        """Verifies anonymous candidates (product_id=0) do not collide in quantities_by_idx."""
        anon_a = IngredientCandidate(
            product_id=0,
            name="Custom Grain A",
            role=Role.CARB_BASE,
            kcal_100g=350.0,
            portion_min_g=80.0,
            portion_max_g=80.0,
            available_stock_g=500.0,
        )
        anon_b = IngredientCandidate(
            product_id=0,
            name="Custom Oil B",
            role=Role.FAT_SEASONING,
            kcal_100g=884.0,
            portion_min_g=15.0,
            portion_max_g=15.0,
            available_stock_g=500.0,
        )
        for fallback in [False, True]:
            res = solve_ingredient_quantities(
                [anon_a, anon_b], target_kcal=412.6, force_fallback=fallback
            )
            self.assertTrue(res.success)
            self.assertEqual(len(res.quantities_by_idx), 2)
            self.assertAlmostEqual(res.quantities_by_idx[0], 80.0, delta=1.0)
            self.assertAlmostEqual(res.quantities_by_idx[1], 15.0, delta=1.0)

    def test_5_extreme_caloric_targets_and_macro_bounds_stress(self):
        """Stress-tests solver across 30 caloric targets (150..2800 kcal) and tight macro bounds."""
        base_cands = [
            IngredientCandidate(
                product_id=1, name="Rice", role=Role.CARB_BASE,
                kcal_100g=350.0, protein_100g=7.0, carbs_100g=78.0, fat_100g=0.6, fiber_100g=1.0,
                portion_min_g=50.0, portion_max_g=150.0, available_stock_g=1000.0,
            ),
            IngredientCandidate(
                product_id=2, name="Chicken", role=Role.PROTEIN_MAIN,
                kcal_100g=110.0, protein_100g=23.0, carbs_100g=0.0, fat_100g=1.5, fiber_100g=0.0,
                portion_min_g=80.0, portion_max_g=250.0, available_stock_g=1000.0,
            ),
            IngredientCandidate(
                product_id=3, name="EVO Oil", role=Role.FAT_SEASONING,
                kcal_100g=884.0, protein_100g=0.0, carbs_100g=0.0, fat_100g=100.0, fiber_100g=0.0,
                portion_min_g=5.0, portion_max_g=25.0, available_stock_g=500.0,
            ),
        ]
        for target in range(150, 2850, 90):
            res = solve_ingredient_quantities(
                base_cands,
                target_kcal=float(target),
                macro_bounds={"protein": (15.0, 90.0), "fat": (5.0, 45.0)},
            )
            self.assertTrue(res.success)
            self.assertTrue(math.isfinite(res.total_kcal))
            self.assertGreater(res.total_kcal, 0.0)
            for q in res.quantities_by_idx:
                self.assertTrue(math.isfinite(q))
                self.assertGreaterEqual(q, 0.0)

    def test_6_cooking_instructions_logical_coherence(self):
        """Verifies no 'Weigh out' when carb is absent, and liquid sauces are not chopped."""
        arch_main = get_archetype("Main & Side Plate")
        no_carb_ings = [
            IngredientCandidate(name="Chicken breast", role=Role.PROTEIN_MAIN),
            IngredientCandidate(name="Tomato puree", role=Role.VEG_SAUCE),
            IngredientCandidate(name="Extra virgin olive oil", role=Role.FAT_SEASONING),
        ]
        steps = synthesize_cooking_instructions(arch_main, no_carb_ings)
        full_text = " ".join(steps).lower()
        self.assertNotIn("weigh out", full_text)
        self.assertNotIn("cutting into even pieces", steps[0].lower())
        self.assertIn("sauce", steps[0].lower())

    def test_7_variation_index_cycling_determinism_and_diversity(self):
        """Verifies variation_index is deterministic for identical index and cycles combinations."""
        gen = RecipeGenerator()
        r0_a = gen.generate_recipe(target_kcal=700.0, meal_type="lunch", variation_index=0)
        r0_b = gen.generate_recipe(target_kcal=700.0, meal_type="lunch", variation_index=0)
        self.assertEqual(r0_a.title, r0_b.title)

        titles = {
            gen.generate_recipe(
                target_kcal=700.0, meal_type="lunch", pantry_only=False, variation_index=i
            ).title
            for i in range(6)
        }
        self.assertGreaterEqual(len(titles), 2)

    def test_8_high_volume_pantry_stress_and_exclusion_enforcement(self):
        """Seeds 80 pantry items (including expired and excluded items) and verifies speed and strict filtering."""
        import time
        with tempfile.TemporaryDirectory() as tmpdir:
            temp_db = os.path.join(tmpdir, "test_stress_80.db")
            shutil.copy(DB_PATH, temp_db)
            conn = sqlite3.connect(temp_db)
            try:
                for i in range(200, 280):
                    conn.execute(
                        "INSERT INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g, Fiber_100g) "
                        "VALUES (?, 'kg', ?, ?, ?, ?, 2.0)",
                        (i, 120.0 + (i % 250), 10.0 + (i % 20), 20.0 + (i % 50), 2.0 + (i % 15)),
                    )
                    conn.execute("INSERT INTO ALIASES (Name, ID) VALUES (?, ?)", (f"StressFood_{i}", i))
                    exp_date = "2020-01-01" if i % 10 == 0 else "2028-06-01"
                    conn.execute(
                        "INSERT INTO INVENTORY (ID, Unit_Weight, Intact_Items, Open_Remainder, Location, Expiration_Date) "
                        "VALUES (?, 1.0, 2, ?, 'pantry', ?)",
                        (i, 0.2 if i % 3 == 0 else 0.0, exp_date),
                    )
                    if i % 15 == 0:
                        conn.execute(
                            "INSERT OR IGNORE INTO EXCLUSION_LIST (ID, Ref_Name, Expiration_Date, Location) "
                            "VALUES (?, ?, '2025-01-01', 'pantry')",
                            (i, f"StressFood_{i}"),
                        )
                conn.commit()

                t0 = time.perf_counter()
                pantry_cands = extract_pantry_candidates(conn)
                cand = RecipeGenerator.generate(target_kcal=700.0, meal_type="lunch", db_path=temp_db)
                elapsed = time.perf_counter() - t0

                self.assertLess(elapsed, 1.0, f"High-volume generation took too long: {elapsed:.2f}s")
                self.assertIsNotNone(cand)
                pantry_ids = {c.product_id for c in pantry_cands}
                for i in range(200, 280):
                    if i % 10 == 0 or i % 15 == 0:
                        self.assertNotIn(i, pantry_ids, f"Expired/excluded item {i} leaked into pantry candidates")
            finally:
                conn.close()

    def test_9_gui_dialog_stress_rapid_regeneration_and_invalid_inputs(self):
        """Stress-tests RecipeGeneratorDialog with 15 rapid regenerations and adversarial invalid inputs."""
        import tkinter as tk
        from meal_planner.gui.dialogs import RecipeGeneratorDialog

        with tempfile.TemporaryDirectory() as tmpdir:
            temp_db = os.path.join(tmpdir, "test_gui_stress.db")
            shutil.copy(DB_PATH, temp_db)
            root = tk.Tk()
            root.withdraw()
            try:
                dlg = RecipeGeneratorDialog(root, db_path=temp_db, wait=False)
                for bad_kcal in ["-100", "0", "abc", "nan", "inf"]:
                    dlg.v_target_kcal.set(bad_kcal)
                    self.assertFalse(dlg.generate_proposal(show_errors=True))
                    self.assertIsNotNone(dlg.last_error)

                dlg.v_target_kcal.set("680")
                for i in range(15):
                    meal = ["lunch", "dinner", "breakfast"][i % 3]
                    dlg.v_meal.set(meal)
                    self.assertTrue(dlg.regenerate_proposal())
                    self.assertIsNotNone(dlg.current_candidate)
                    self.assertGreater(len(dlg.t_ingredients.get_children()), 0)
                dlg.cancel()
            finally:
                root.destroy()


if __name__ == "__main__":
    unittest.main()



