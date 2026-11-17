"""meal_planner.generator.taxonomy

Two-tier food classification engine and physiological portion bounds:
- Tier A: Semantic taxonomy dictionary mapping known ingredients and aliases to culinary roles.
- Tier B: Automated macro fingerprinting heuristics classifying any food from 100g nutritional profile.
- Context-sensitive portion bounds [min_g, max_g] for primary and multi-role ingredients.
"""
from __future__ import annotations

from enum import Enum
import math
import re
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union


class Role(str, Enum):
    """Culinary grammatical roles used to classify foods and define archetype slots."""

    # 7 Core culinary categories
    CARB_BASE = "CARB_BASE"
    PROTEIN_MAIN = "PROTEIN_MAIN"
    VEGETABLE = "VEGETABLE"
    FAT_SEASONING = "FAT_SEASONING"
    DAIRY_BASE = "DAIRY_BASE"
    FRUIT = "FRUIT"
    AROMA_TASTE = "AROMA_TASTE"

    # Breakfast & snack specific roles
    CEREAL_CARB = "CEREAL_CARB"
    NUTS_OR_SWEET = "NUTS_OR_SWEET"

    # Contextual sub-roles used by archetype slots
    PROTEIN_ACCENT = "PROTEIN_ACCENT"
    BINDER_SAUCE = "BINDER_SAUCE"
    VEG_SAUCE = "VEG_SAUCE"
    VEG_SIDE = "VEG_SIDE"
    CARB_SIDE = "CARB_SIDE"
    AROMA_HERB = "AROMA_HERB"
    AROMA_FINISHING = "AROMA_FINISHING"

    def __str__(self) -> str:
        return self.value


# Mapping of which candidate roles can fulfill specific required slot roles
ROLE_COMPATIBILITY: Dict[Role, Set[Role]] = {
    Role.CARB_BASE: {Role.CARB_BASE},
    Role.CARB_SIDE: {Role.CARB_SIDE, Role.CARB_BASE},
    Role.CEREAL_CARB: {Role.CEREAL_CARB, Role.CARB_BASE},
    Role.PROTEIN_MAIN: {Role.PROTEIN_MAIN},
    Role.PROTEIN_ACCENT: {Role.PROTEIN_ACCENT, Role.PROTEIN_MAIN},
    Role.BINDER_SAUCE: {Role.BINDER_SAUCE, Role.PROTEIN_MAIN},
    Role.VEGETABLE: {Role.VEGETABLE, Role.VEG_SAUCE, Role.VEG_SIDE},
    Role.VEG_SAUCE: {Role.VEG_SAUCE, Role.VEGETABLE},
    Role.VEG_SIDE: {Role.VEG_SIDE, Role.VEGETABLE},
    Role.FAT_SEASONING: {Role.FAT_SEASONING},
    Role.DAIRY_BASE: {Role.DAIRY_BASE},
    Role.FRUIT: {Role.FRUIT},
    Role.NUTS_OR_SWEET: {Role.NUTS_OR_SWEET},
    Role.AROMA_TASTE: {Role.AROMA_TASTE, Role.AROMA_HERB, Role.AROMA_FINISHING},
    Role.AROMA_HERB: {Role.AROMA_HERB, Role.AROMA_TASTE},
    Role.AROMA_FINISHING: {Role.AROMA_FINISHING, Role.AROMA_TASTE},
}


def are_roles_compatible(
    candidate_role: Union[Role, str], required_slot_role: Union[Role, str]
) -> bool:
    """Returns True if candidate_role can satisfy the required_slot_role."""
    try:
        cand = candidate_role if isinstance(candidate_role, Role) else Role(candidate_role)
        req = required_slot_role if isinstance(required_slot_role, Role) else Role(required_slot_role)
    except (ValueError, TypeError):
        return False
    if cand == req:
        return True
    try:
        allowed = ROLE_COMPATIBILITY.get(req, {req})
        return cand in allowed
    except (TypeError, KeyError):
        return False


def is_role_compatible(
    required_role: Union[Role, str], candidate_roles: Iterable[Union[Role, str]]
) -> bool:
    """Returns True if any of the candidate's roles satisfies the required slot role."""
    try:
        req = required_role if isinstance(required_role, Role) else Role(required_role)
    except (ValueError, TypeError):
        return False

    if not isinstance(candidate_roles, Iterable) or isinstance(candidate_roles, (str, bytes)):
        if isinstance(candidate_roles, (Role, str)):
            return are_roles_compatible(candidate_roles, req)
        return False

    try:
        allowed = ROLE_COMPATIBILITY.get(req, {req})
    except (TypeError, KeyError):
        allowed = {req}

    try:
        for r in candidate_roles:
            try:
                cand = r if isinstance(r, Role) else Role(r)
                if cand in allowed:
                    return True
            except (ValueError, TypeError):
                continue
    except (TypeError, ValueError):
        return False
    return False


# ---------------------------------------------------------------------------
# Tier A Semantic Taxonomy Dictionary
# ---------------------------------------------------------------------------

TIER_A_MAP: Dict[str, List[Role]] = {
    # 1. Carbohydrate bases (Pasta, Rice, Grains, Bread, Flour)
    "pasta": [Role.CARB_BASE],
    "spaghetti": [Role.CARB_BASE],
    "spaghetti n.5": [Role.CARB_BASE],
    "penne": [Role.CARB_BASE],
    "penne rigate": [Role.CARB_BASE],
    "rigatoni": [Role.CARB_BASE],
    "fusilli": [Role.CARB_BASE],
    "farfalle": [Role.CARB_BASE],
    "tagliatelle": [Role.CARB_BASE],
    "linguine": [Role.CARB_BASE],
    "riso": [Role.CARB_BASE],
    "rice": [Role.CARB_BASE],
    "carnaroli": [Role.CARB_BASE],
    "carnaroli rice": [Role.CARB_BASE],
    "basmati": [Role.CARB_BASE],
    "farro": [Role.CARB_BASE],
    "orzo": [Role.CARB_BASE],
    "quinoa": [Role.CARB_BASE],
    "couscous": [Role.CARB_BASE],
    "polenta": [Role.CARB_BASE],
    "pane": [Role.CARB_BASE, Role.CARB_SIDE],
    "bread": [Role.CARB_BASE, Role.CARB_SIDE],
    "patate": [Role.CARB_SIDE, Role.CARB_BASE],
    "potatoes": [Role.CARB_SIDE, Role.CARB_BASE],
    "flour": [Role.CARB_BASE],
    "00 flour": [Role.CARB_BASE],
    "farina": [Role.CARB_BASE],
    "long pasta": [Role.CARB_BASE],
    "soup_pasta": [Role.CARB_BASE],

    # 2. Breakfast Cereals & Baked Goods
    "avena": [Role.CEREAL_CARB, Role.CARB_BASE],
    "oats": [Role.CEREAL_CARB, Role.CARB_BASE],
    "rolled oats": [Role.CEREAL_CARB, Role.CARB_BASE],
    "muesli": [Role.CEREAL_CARB],
    "granola": [Role.CEREAL_CARB],
    "biscotti": [Role.CEREAL_CARB, Role.CARB_BASE],

    # 3. Main Proteins & Multi-Role Proteins
    "pollo": [Role.PROTEIN_MAIN],
    "chicken": [Role.PROTEIN_MAIN],
    "chicken breast": [Role.PROTEIN_MAIN],
    "petto di pollo": [Role.PROTEIN_MAIN],
    "tacchino": [Role.PROTEIN_MAIN],
    "turkey": [Role.PROTEIN_MAIN],
    "manzo": [Role.PROTEIN_MAIN],
    "beef": [Role.PROTEIN_MAIN],
    "vitello": [Role.PROTEIN_MAIN],
    "maiale": [Role.PROTEIN_MAIN],
    "pork": [Role.PROTEIN_MAIN],
    "tonno": [Role.PROTEIN_MAIN, Role.PROTEIN_ACCENT],
    "tuna": [Role.PROTEIN_MAIN, Role.PROTEIN_ACCENT],
    "tonno al naturale": [Role.PROTEIN_MAIN, Role.PROTEIN_ACCENT],
    "salmone": [Role.PROTEIN_MAIN, Role.PROTEIN_ACCENT],
    "salmon": [Role.PROTEIN_MAIN, Role.PROTEIN_ACCENT],
    "merluzzo": [Role.PROTEIN_MAIN],
    "cod": [Role.PROTEIN_MAIN],
    "tofu": [Role.PROTEIN_MAIN],
    "seitan": [Role.PROTEIN_MAIN],
    "tempeh": [Role.PROTEIN_MAIN],
    "ceci": [Role.PROTEIN_MAIN, Role.CARB_SIDE],
    "chickpeas": [Role.PROTEIN_MAIN, Role.CARB_SIDE],
    "fagioli": [Role.PROTEIN_MAIN, Role.CARB_SIDE],
    "beans": [Role.PROTEIN_MAIN, Role.CARB_SIDE],
    "lenticchie": [Role.PROTEIN_MAIN, Role.CARB_SIDE],
    "lentils": [Role.PROTEIN_MAIN, Role.CARB_SIDE],

    # Multi-Role: Eggs
    "uova": [Role.PROTEIN_MAIN, Role.PROTEIN_ACCENT],
    "eggs": [Role.PROTEIN_MAIN, Role.PROTEIN_ACCENT],
    "uovo": [Role.PROTEIN_MAIN, Role.PROTEIN_ACCENT],
    "egg": [Role.PROTEIN_MAIN, Role.PROTEIN_ACCENT],

    # Multi-Role: Parmigiano & Cheeses
    "parmigiano": [Role.AROMA_TASTE, Role.PROTEIN_MAIN, Role.AROMA_FINISHING],
    "parmigiano reggiano": [Role.AROMA_TASTE, Role.PROTEIN_MAIN, Role.AROMA_FINISHING],
    "parmesan": [Role.AROMA_TASTE, Role.PROTEIN_MAIN, Role.AROMA_FINISHING],
    "grana": [Role.AROMA_TASTE, Role.PROTEIN_MAIN, Role.AROMA_FINISHING],
    "grana padano": [Role.AROMA_TASTE, Role.PROTEIN_MAIN, Role.AROMA_FINISHING],
    "pecorino": [Role.AROMA_TASTE, Role.AROMA_FINISHING],
    "ricotta": [Role.PROTEIN_MAIN, Role.DAIRY_BASE],
    "mozzarella": [Role.PROTEIN_MAIN, Role.PROTEIN_ACCENT],
    "feta": [Role.PROTEIN_MAIN, Role.PROTEIN_ACCENT],

    # 4. Vegetables (Sauce, Side, and Generic)
    "pomodoro": [Role.VEGETABLE, Role.VEG_SAUCE, Role.VEG_SIDE],
    "tomato": [Role.VEGETABLE, Role.VEG_SAUCE, Role.VEG_SIDE],
    "tomato puree": [Role.VEGETABLE, Role.VEG_SAUCE, Role.VEG_SIDE],
    "puree": [Role.VEGETABLE, Role.VEG_SAUCE, Role.VEG_SIDE],
    "passata": [Role.VEGETABLE, Role.VEG_SAUCE, Role.VEG_SIDE],
    "tomato passata": [Role.VEGETABLE, Role.VEG_SAUCE, Role.VEG_SIDE],
    "passata di pomodoro": [Role.VEGETABLE, Role.VEG_SAUCE, Role.VEG_SIDE],
    "zucchine": [Role.VEGETABLE, Role.VEG_SIDE, Role.VEG_SAUCE],
    "zucchini": [Role.VEGETABLE, Role.VEG_SIDE, Role.VEG_SAUCE],
    "spinaci": [Role.VEGETABLE, Role.VEG_SIDE],
    "spinach": [Role.VEGETABLE, Role.VEG_SIDE],
    "broccoli": [Role.VEGETABLE, Role.VEG_SIDE],
    "carote": [Role.VEGETABLE, Role.VEG_SIDE],
    "carrots": [Role.VEGETABLE, Role.VEG_SIDE],
    "funghi": [Role.VEGETABLE, Role.VEG_SAUCE, Role.VEG_SIDE],
    "mushrooms": [Role.VEGETABLE, Role.VEG_SAUCE, Role.VEG_SIDE],
    "peperoni": [Role.VEGETABLE, Role.VEG_SIDE],
    "peppers": [Role.VEGETABLE, Role.VEG_SIDE],
    "melanzane": [Role.VEGETABLE, Role.VEG_SIDE],
    "eggplant": [Role.VEGETABLE, Role.VEG_SIDE],
    "cipolla": [Role.VEGETABLE, Role.AROMA_TASTE],
    "onion": [Role.VEGETABLE, Role.AROMA_TASTE],

    # 5. Fats & Seasonings
    "olio": [Role.FAT_SEASONING],
    "oil": [Role.FAT_SEASONING],
    "extra virgin olive oil": [Role.FAT_SEASONING],
    "evo oil": [Role.FAT_SEASONING],
    "olio evo": [Role.FAT_SEASONING],
    "olio extravergine": [Role.FAT_SEASONING],
    "olio di oliva": [Role.FAT_SEASONING],
    "burro": [Role.FAT_SEASONING],
    "butter": [Role.FAT_SEASONING],
    "olio di semi": [Role.FAT_SEASONING],

    # 6. Dairy Bases
    "latte": [Role.DAIRY_BASE],
    "milk": [Role.DAIRY_BASE],
    "whole milk": [Role.DAIRY_BASE],
    "latte intero": [Role.DAIRY_BASE],
    "latte parzialmente scremato": [Role.DAIRY_BASE],
    "yogurt": [Role.DAIRY_BASE],
    "plain yogurt": [Role.DAIRY_BASE],
    "skyr": [Role.DAIRY_BASE],
    "kefir": [Role.DAIRY_BASE],

    # 7. Fruits
    "mela": [Role.FRUIT],
    "mele": [Role.FRUIT],
    "apple": [Role.FRUIT],
    "apples": [Role.FRUIT],
    "banana": [Role.FRUIT],
    "banane": [Role.FRUIT],
    "frutti di bosco": [Role.FRUIT],
    "berries": [Role.FRUIT],
    "mirtilli": [Role.FRUIT],
    "fragole": [Role.FRUIT],
    "pera": [Role.FRUIT],
    "pear": [Role.FRUIT],
    "arancia": [Role.FRUIT],
    "orange": [Role.FRUIT],

    # 8. Aromas, Herbs & Condiments
    "basilico": [Role.AROMA_TASTE, Role.AROMA_HERB],
    "basil": [Role.AROMA_TASTE, Role.AROMA_HERB],
    "origano": [Role.AROMA_TASTE, Role.AROMA_HERB],
    "oregano": [Role.AROMA_TASTE, Role.AROMA_HERB],
    "rosmarino": [Role.AROMA_TASTE, Role.AROMA_HERB],
    "rosemary": [Role.AROMA_TASTE, Role.AROMA_HERB],
    "prezzemolo": [Role.AROMA_TASTE, Role.AROMA_HERB],
    "parsley": [Role.AROMA_TASTE, Role.AROMA_HERB],
    "pepe": [Role.AROMA_TASTE],
    "pepper": [Role.AROMA_TASTE],
    "sale": [Role.AROMA_TASTE],
    "salt": [Role.AROMA_TASTE],
    "sale marino": [Role.AROMA_TASTE],
    "aglio": [Role.AROMA_TASTE],
    "garlic": [Role.AROMA_TASTE],

    # 9. Nuts & Sweets
    "mandorle": [Role.NUTS_OR_SWEET, Role.FAT_SEASONING],
    "almonds": [Role.NUTS_OR_SWEET, Role.FAT_SEASONING],
    "noci": [Role.NUTS_OR_SWEET, Role.FAT_SEASONING],
    "walnuts": [Role.NUTS_OR_SWEET, Role.FAT_SEASONING],
    "nocciole": [Role.NUTS_OR_SWEET, Role.FAT_SEASONING],
    "hazelnuts": [Role.NUTS_OR_SWEET, Role.FAT_SEASONING],
    "pistacchi": [Role.NUTS_OR_SWEET, Role.FAT_SEASONING],
    "pistachios": [Role.NUTS_OR_SWEET, Role.FAT_SEASONING],
    "anacardi": [Role.NUTS_OR_SWEET, Role.FAT_SEASONING],
    "cashews": [Role.NUTS_OR_SWEET, Role.FAT_SEASONING],
    "pinoli": [Role.NUTS_OR_SWEET, Role.FAT_SEASONING],
    "pine nuts": [Role.NUTS_OR_SWEET, Role.FAT_SEASONING],
    "semi di chia": [Role.NUTS_OR_SWEET],
    "chia seeds": [Role.NUTS_OR_SWEET],
    "semi di lino": [Role.NUTS_OR_SWEET],
    "flax seeds": [Role.NUTS_OR_SWEET],
    "semi di girasole": [Role.NUTS_OR_SWEET],
    "sunflower seeds": [Role.NUTS_OR_SWEET],
    "semi di zucca": [Role.NUTS_OR_SWEET],
    "pumpkin seeds": [Role.NUTS_OR_SWEET],
    "cacao": [Role.NUTS_OR_SWEET],
    "cocoa": [Role.NUTS_OR_SWEET],
    "sciroppo d'acero": [Role.NUTS_OR_SWEET],
    "maple syrup": [Role.NUTS_OR_SWEET],
    "miele": [Role.NUTS_OR_SWEET],
    "honey": [Role.NUTS_OR_SWEET],
    "cioccolato": [Role.NUTS_OR_SWEET],
    "chocolate": [Role.NUTS_OR_SWEET],
    "sugar": [Role.NUTS_OR_SWEET, Role.AROMA_TASTE],
    "zucchero": [Role.NUTS_OR_SWEET, Role.AROMA_TASTE],
}


# ---------------------------------------------------------------------------
# Tier B Macro Fingerprinting Heuristics
# ---------------------------------------------------------------------------

def fingerprint_macros(
    kcal_100g: float,
    protein_100g: float,
    carbs_100g: float,
    fat_100g: float,
    fiber_100g: float = 0.0,
    sugars_100g: float = 0.0,
) -> List[Role]:
    """Deterministic mathematical classifier determining culinary roles from 100g nutritional profile."""
    # Defensive numerical coercion
    k = max(0.0, float(kcal_100g or 0.0))
    p = max(0.0, float(protein_100g or 0.0))
    c = max(0.0, float(carbs_100g or 0.0))
    f = max(0.0, float(fat_100g or 0.0))
    s = max(0.0, float(sugars_100g or 0.0))

    # Guard zero-calorie items (pure spices, salt, water)
    if k <= 0.0:
        if p >= 15.0:
            return [Role.PROTEIN_MAIN]
        return [Role.AROMA_TASTE]

    # Rule 1: High Fat Seasoning / Condiment vs Nuts & Seeds
    if f >= 45.0:
        if p >= 8.0 and (c >= 4.0 or k >= 500.0):
            return [Role.NUTS_OR_SWEET, Role.FAT_SEASONING]
        return [Role.FAT_SEASONING]

    # Rule 2: Fruit (high sugars or fresh fruit carb profile, low protein, low fat)
    if (s >= 8.0 or (s == 0.0 and 8.0 <= c <= 25.0)) and p < 2.0 and f < 1.5:
        return [Role.FRUIT]

    # Rule 3: Vegetables (low calorie density, low fat, low sugars, non-protein)
    if k <= 55.0 and f <= 2.0 and (s < 7.0 or s == 0.0) and p < 10.0:
        return [Role.VEGETABLE]

    # Ratio of protein calories to total energy
    protein_energy_ratio = (p * 4.0) / k

    # Rule 4: Protein Main (meat, poultry, fish, tofu, eggs, protein supplements)
    if (protein_energy_ratio >= 0.35 and p >= 5.0) or p >= 15.0:
        return [Role.PROTEIN_MAIN]

    # Rule 5: Carbohydrate Base (pasta, rice, grains, flour, bread)
    if c >= 50.0 and k >= 200.0:
        return [Role.CARB_BASE]

    # Rule 6: Dairy Base (milk, yogurt, skyr - liquid/semiliquid protein + lactose carbs)
    if p >= 3.0 and (3.5 <= c <= 7.0) and k <= 150.0:
        return [Role.DAIRY_BASE]

    # Rule 7: Remaining high protein-to-energy items
    if protein_energy_ratio >= 0.35:
        return [Role.PROTEIN_MAIN]

    # Default fallback: Seasoning / Aroma / Condiment
    return [Role.AROMA_TASTE]


# ---------------------------------------------------------------------------
# Classification Engine Entry Point
# ---------------------------------------------------------------------------

def classify_food(
    name: str,
    kcal_100g: float,
    protein_100g: float,
    carbs_100g: float,
    fat_100g: float,
    fiber_100g: float = 0.0,
    sugars_100g: float = 0.0,
) -> List[Role]:
    """Classifies a food into eligible culinary roles using Tier A semantic lookup
    with automated Tier B macro fingerprinting fallback."""
    try:
        clean_name = (
            name.strip().lower()
            if isinstance(name, str)
            else ("" if name is None else str(name).strip().lower())
        )
    except Exception:
        clean_name = ""

    # Tier A - Step 1: Exact match
    if clean_name in TIER_A_MAP:
        return list(TIER_A_MAP[clean_name])

    # Tier A - Step 2: Whole-word / token containment match
    if clean_name:
        for alias, roles in TIER_A_MAP.items():
            pattern = r"(?:\b|_)" + re.escape(alias) + r"(?:\b|_)"
            if re.search(pattern, clean_name):
                return list(roles)

    # Tier B - Fallback to automated macro fingerprinting
    return fingerprint_macros(
        kcal_100g=kcal_100g,
        protein_100g=protein_100g,
        carbs_100g=carbs_100g,
        fat_100g=fat_100g,
        fiber_100g=fiber_100g,
        sugars_100g=sugars_100g,
    )


# ---------------------------------------------------------------------------
# Physiological Portion Bounds
# ---------------------------------------------------------------------------

DEFAULT_PORTION_BOUNDS: Dict[Role, Tuple[float, float]] = {
    Role.CARB_BASE: (60.0, 120.0),
    Role.CARB_SIDE: (40.0, 100.0),
    Role.CEREAL_CARB: (30.0, 80.0),
    Role.PROTEIN_MAIN: (100.0, 250.0),
    Role.PROTEIN_ACCENT: (40.0, 120.0),
    Role.BINDER_SAUCE: (30.0, 60.0),
    Role.VEGETABLE: (100.0, 300.0),
    Role.VEG_SAUCE: (50.0, 200.0),
    Role.VEG_SIDE: (120.0, 350.0),
    Role.FAT_SEASONING: (5.0, 20.0),
    Role.DAIRY_BASE: (100.0, 250.0),
    Role.FRUIT: (50.0, 150.0),
    Role.NUTS_OR_SWEET: (10.0, 25.0),
    Role.AROMA_TASTE: (5.0, 15.0),
    Role.AROMA_HERB: (1.0, 5.0),
    Role.AROMA_FINISHING: (5.0, 15.0),
}

# (keyword, target_role) -> (min_g, max_g) overrides
SPECIFIC_PORTION_OVERRIDES: List[Tuple[str, Role, Tuple[float, float]]] = [
    # Parmigiano / Hard cheeses
    ("parmigiano", Role.PROTEIN_MAIN, (40.0, 80.0)),
    ("parmigiano", Role.AROMA_TASTE, (5.0, 20.0)),
    ("parmigiano", Role.AROMA_FINISHING, (5.0, 20.0)),
    ("parmesan", Role.PROTEIN_MAIN, (40.0, 80.0)),
    ("parmesan", Role.AROMA_TASTE, (5.0, 20.0)),
    ("parmesan", Role.AROMA_FINISHING, (5.0, 20.0)),
    ("grana", Role.PROTEIN_MAIN, (40.0, 80.0)),
    ("grana", Role.AROMA_TASTE, (5.0, 20.0)),
    ("grana", Role.AROMA_FINISHING, (5.0, 20.0)),
    ("pecorino", Role.AROMA_TASTE, (5.0, 20.0)),
    ("pecorino", Role.AROMA_FINISHING, (5.0, 20.0)),

    # Eggs (1 pc = ~50g, 2-3 pcs = 100-150g)
    ("uova", Role.PROTEIN_MAIN, (100.0, 200.0)),
    ("uova", Role.PROTEIN_ACCENT, (50.0, 60.0)),
    ("eggs", Role.PROTEIN_MAIN, (100.0, 200.0)),
    ("eggs", Role.PROTEIN_ACCENT, (50.0, 60.0)),
    ("egg", Role.PROTEIN_MAIN, (100.0, 200.0)),
    ("egg", Role.PROTEIN_ACCENT, (50.0, 60.0)),
    ("uovo", Role.PROTEIN_MAIN, (100.0, 200.0)),
    ("uovo", Role.PROTEIN_ACCENT, (50.0, 60.0)),

    # Grains & Pasta fine-tuning
    ("pasta", Role.CARB_BASE, (60.0, 120.0)),
    ("spaghetti", Role.CARB_BASE, (60.0, 120.0)),
    ("penne", Role.CARB_BASE, (60.0, 120.0)),
    ("riso", Role.CARB_BASE, (50.0, 100.0)),
    ("rice", Role.CARB_BASE, (50.0, 100.0)),

    # Tomato sauce / puree
    ("passata", Role.VEG_SAUCE, (60.0, 200.0)),
    ("puree", Role.VEG_SAUCE, (60.0, 200.0)),
    ("pomodoro", Role.VEG_SAUCE, (60.0, 200.0)),

    # Breakfast Cereals & Biscotti
    ("biscotti", Role.CEREAL_CARB, (20.0, 50.0)),
    ("avena", Role.CEREAL_CARB, (30.0, 80.0)),
    ("oats", Role.CEREAL_CARB, (30.0, 80.0)),
]


def get_portion_bounds(role: Union[Role, str], food_name: str = "") -> Tuple[float, float]:
    """Returns the physiological portion weight bounds [min_g, max_g] for the specified
    culinary role and optional specific food name."""
    try:
        target_role = role if isinstance(role, Role) else Role(role)
    except (ValueError, TypeError):
        return (10.0, 100.0)

    try:
        clean_name = (
            food_name.strip().lower()
            if isinstance(food_name, str)
            else ("" if food_name is None else str(food_name).strip().lower())
        )
    except Exception:
        clean_name = ""

    if clean_name:
        for keyword, override_role, bounds in SPECIFIC_PORTION_OVERRIDES:
            if override_role == target_role and keyword in clean_name:
                return bounds

    try:
        return DEFAULT_PORTION_BOUNDS.get(target_role, (10.0, 100.0))
    except (TypeError, KeyError):
        return (10.0, 100.0)
