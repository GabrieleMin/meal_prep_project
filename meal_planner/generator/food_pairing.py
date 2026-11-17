"""meal_planner.generator.food_pairing

Culinary Food Pairing Graph and Compatibility Engine.
- Hard mutual exclusions (prunes unpalatable or unsafe combinations).
- Soft affinity matrix and 4-tier normalization (scores flavor harmony w in [0.0, 1.0]).
- Combination scoring evaluating multi-ingredient dishes.
- Public API: are_mutually_exclusive, get_pairing_score, score_combination.
"""
from __future__ import annotations

from enum import Enum
import functools
import itertools
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union

from meal_planner.generator.taxonomy import Role, TIER_A_MAP, classify_food


# ---------------------------------------------------------------------------
# 1. Culinary Group Tags
# ---------------------------------------------------------------------------

class CulinaryTag(str, Enum):
    SEAFOOD = "SEAFOOD"
    SWEET_DAIRY = "SWEET_DAIRY"
    CHOCOLATE_SWEET = "CHOCOLATE_SWEET"
    PASTA_GRAIN = "PASTA_GRAIN"
    SAVORY_MEAT = "SAVORY_MEAT"
    SWEET_FRUIT = "SWEET_FRUIT"
    SAVORY_SAUCE = "SAVORY_SAUCE"
    PUNGENT_AROMA = "PUNGENT_AROMA"
    RAW_FLOUR = "RAW_FLOUR"
    HARD_CHEESE = "HARD_CHEESE"
    SAVORY_FAT = "SAVORY_FAT"
    EGGS = "EGGS"
    HERBS = "HERBS"


# Keyword to Tag mapping rules
TAG_KEYWORDS: List[Tuple[CulinaryTag, Tuple[str, ...]]] = [
    (
        CulinaryTag.SEAFOOD,
        (
            "tonno", "tuna", "salmone", "salmon", "merluzzo", "cod", "pesce", "fish",
            "gamberi", "shrimp", "prawn", "calamari", "squid", "acciughe", "anchovy",
            "cozze", "mussels", "vongole", "clams",
        ),
    ),
    (
        CulinaryTag.SWEET_DAIRY,
        (
            "latte", "milk", "yogurt", "skyr", "kefir", "whole milk", "latte intero",
            "panna", "cream", "ricotta",
        ),
    ),
    (
        CulinaryTag.CHOCOLATE_SWEET,
        (
            "cioccolato", "chocolate", "cacao", "cocoa", "miele", "honey", "zucchero",
            "sugar", "biscotti", "biscuit", "cookies", "muesli", "granola", "sciroppo",
            "syrup", "dolce",
        ),
    ),
    (
        CulinaryTag.PASTA_GRAIN,
        (
            "pasta", "spaghetti", "spaghetti n.5", "penne", "penne rigate", "fusilli",
            "rigatoni", "farfalle", "riso", "rice", "carnaroli", "basmati", "farro",
            "orzo", "soup_pasta", "long pasta", "noodles", "avena", "oats", "quinoa",
        ),
    ),
    (
        CulinaryTag.SAVORY_MEAT,
        (
            "pollo", "chicken", "chicken breast", "petto di pollo", "tacchino", "turkey",
            "manzo", "beef", "maiale", "pork", "vitello", "veal", "agnello", "lamb",
            "prosciutto", "pancetta", "bacon", "salsiccia", "sausage",
        ),
    ),
    (
        CulinaryTag.SWEET_FRUIT,
        (
            "mela", "mele", "apple", "apples", "banana", "banane", "bananas",
            "pera", "pere", "pear", "pears",
            "frutti di bosco", "berries", "fragole", "fragola", "strawberry", "strawberries",
            "mirtilli", "mirtillo", "blueberry", "blueberries",
            "arancia", "arance", "orange", "oranges",
            "pesca", "pesche", "peach", "peaches",
            "albicocca", "albicocche", "apricot", "apricots",
            "uva", "uve", "grape", "grapes",
            "kiwi", "ananas", "pineapple", "pineapples",
        ),
    ),
    (
        CulinaryTag.SAVORY_SAUCE,
        (
            "passata", "passata di pomodoro", "puree", "tomato puree", "tomato passata",
            "pomodoro", "tomato sauce", "pelati", "salsa",
        ),
    ),
    (
        CulinaryTag.PUNGENT_AROMA,
        (
            "aglio", "garlic", "cipolla", "onion", "scalogno", "shallot", "porro", "leek",
        ),
    ),
    (
        CulinaryTag.RAW_FLOUR,
        (
            "farina", "00 flour", "flour", "semola", "amido", "starch",
        ),
    ),
    (
        CulinaryTag.HARD_CHEESE,
        (
            "parmigiano", "parmigiano reggiano", "parmesan", "grana", "grana padano",
            "pecorino",
        ),
    ),
    (
        CulinaryTag.SAVORY_FAT,
        (
            "olio", "oil", "extra virgin olive oil", "evo oil", "olio evo",
            "olio extravergine", "olio di oliva", "olio di semi", "seed oil",
            "burro", "butter", "strutto", "lard", "margarina", "margarine",
        ),
    ),
    (
        CulinaryTag.EGGS,
        (
            "uova", "eggs", "uovo", "egg", "tuorlo", "albume", "egg white",
        ),
    ),
    (
        CulinaryTag.HERBS,
        (
            "basilico", "basil", "origano", "oregano", "rosmarino", "rosemary",
            "prezzemolo", "parsley", "timo", "thyme", "salvia", "sage",
        ),
    ),
]


# ---------------------------------------------------------------------------
# 2. Canonical Food Resolver
# ---------------------------------------------------------------------------

# Normalized keywords -> canonical base tokens
CANONICAL_RESOLVER: List[Tuple[str, Tuple[str, ...]]] = [
    ("pasta", ("spaghetti", "penne", "fusilli", "rigatoni", "farfalle", "soup_pasta", "long pasta", "pasta")),
    ("rice", ("carnaroli", "basmati", "riso", "rice")),
    ("tomato passata", ("passata", "puree", "tomato puree", "tomato passata", "pomodoro")),
    ("evo oil", ("extra virgin olive oil", "evo oil", "olio evo", "olio", "oil")),
    ("parmigiano reggiano", ("parmigiano", "parmigiano reggiano", "parmesan", "grana", "pecorino")),
    ("chicken", ("chicken breast", "petto di pollo", "pollo", "chicken")),
    ("beef", ("manzo", "beef", "vitello", "veal")),
    ("tuna", ("tonno", "tuna")),
    ("salmon", ("salmone", "salmon")),
    ("eggs", ("uova", "eggs", "uovo", "egg")),
    ("milk", ("whole milk", "latte intero", "latte", "milk")),
    ("yogurt", ("plain yogurt", "yogurt", "skyr", "kefir")),
    ("apple", ("apples", "apple", "mela", "mele")),
    ("banana", ("banane", "banana")),
    ("berries", ("frutti di bosco", "berries", "mirtilli", "mirtillo", "fragole", "fragola", "strawberry", "strawberries", "blueberry", "blueberries")),
    ("biscotti", ("biscotti", "biscuit", "cookies")),
    ("oats", ("avena", "oats", "oatmeal", "porridge")),
    ("honey", ("miele", "honey")),
    ("chocolate", ("cioccolato", "chocolate", "cacao")),
    ("basil", ("basilico", "basil")),
    ("garlic", ("aglio", "garlic")),
    ("onion", ("cipolla", "onion", "scalogno")),
    ("oregano", ("origano", "oregano")),
    ("rosemary", ("rosmarino", "rosemary")),
    ("zucchini", ("zucchine", "zucchini", "zucchina", "courgette")),
    ("potatoes", ("patate", "potatoes", "potato")),
    ("almonds", ("mandorle", "almonds", "almond")),
    ("walnuts", ("noci", "walnuts", "walnut")),
    ("flour", ("00 flour", "farina", "flour")),
]


@functools.lru_cache(maxsize=1024)
def _resolve_canonical_str(name: str) -> str:
    clean = re.sub(r"[_\-\.\(\)\,\/]", " ", name).lower().strip()
    clean = re.sub(r"\s+", " ", clean)

    for canon, patterns in CANONICAL_RESOLVER:
        for p in patterns:
            if re.search(r"\b" + re.escape(p) + r"\b", clean):
                return canon
    return clean


def resolve_canonical(name: Any) -> str:
    """Normalizes an ingredient name string into a canonical token."""
    if not name or not isinstance(name, str):
        return ""
    return _resolve_canonical_str(name)


@functools.lru_cache(maxsize=1024)
def _base_tags_for_name(name: str) -> frozenset:
    clean = re.sub(r"[_\-\.\(\)\,\/]", " ", name).lower().strip()
    clean = re.sub(r"\s+", " ", clean)
    found = set()
    for tag, patterns in TAG_KEYWORDS:
        for p in patterns:
            if re.search(r"\b" + re.escape(p) + r"\b", clean):
                found.add(tag)
                break
    return frozenset(found)


def get_culinary_tags(name: Any, roles: Optional[Iterable[Any]] = None) -> Set[CulinaryTag]:
    """Identifies all applicable CulinaryTags for an ingredient by name and roles."""
    if not name or not isinstance(name, str):
        return set()

    tags: Set[CulinaryTag] = set(_base_tags_for_name(name))

    # Infer from roles if tags are incomplete
    if roles:
        coerced_roles: Set[Role] = set()
        for r in roles:
            if isinstance(r, Role):
                coerced_roles.add(r)
            elif isinstance(r, str):
                try:
                    coerced_roles.add(Role(r))
                except ValueError:
                    pass

        if Role.FRUIT in coerced_roles:
            tags.add(CulinaryTag.SWEET_FRUIT)
        if Role.FAT_SEASONING in coerced_roles and Role.NUTS_OR_SWEET not in coerced_roles and not tags:
            tags.add(CulinaryTag.SAVORY_FAT)
        if Role.DAIRY_BASE in coerced_roles and not tags:
            tags.add(CulinaryTag.SWEET_DAIRY)

    return tags


# ---------------------------------------------------------------------------
# 3. 17 Hard Mutual Exclusion Rules
# ---------------------------------------------------------------------------

HARD_EXCLUSION_PAIRS: Set[Tuple[CulinaryTag, CulinaryTag]] = {
    # EX-01: Seafood + Sweet Dairy
    (CulinaryTag.SEAFOOD, CulinaryTag.SWEET_DAIRY),
    # EX-02: Seafood + Chocolate / Sweets
    (CulinaryTag.SEAFOOD, CulinaryTag.CHOCOLATE_SWEET),
    # EX-03: Seafood + Sweet Fruit
    (CulinaryTag.SEAFOOD, CulinaryTag.SWEET_FRUIT),
    # EX-04: Pasta / Grain + Sweet Fruit
    (CulinaryTag.PASTA_GRAIN, CulinaryTag.SWEET_FRUIT),
    # EX-05: Pasta / Grain + Sweet Dairy
    (CulinaryTag.PASTA_GRAIN, CulinaryTag.SWEET_DAIRY),
    # EX-06: Pasta / Grain + Chocolate / Sweets
    (CulinaryTag.PASTA_GRAIN, CulinaryTag.CHOCOLATE_SWEET),
    # EX-07: Savory Meat + Sweet Dairy
    (CulinaryTag.SAVORY_MEAT, CulinaryTag.SWEET_DAIRY),
    # EX-08: Savory Meat + Chocolate / Sweets
    (CulinaryTag.SAVORY_MEAT, CulinaryTag.CHOCOLATE_SWEET),
    # EX-09: Savory Meat + Sweet Fruit
    (CulinaryTag.SAVORY_MEAT, CulinaryTag.SWEET_FRUIT),
    # EX-10: Savory Sauce + Sweet Fruit
    (CulinaryTag.SAVORY_SAUCE, CulinaryTag.SWEET_FRUIT),
    # EX-11: Savory Sauce + Chocolate / Sweets
    (CulinaryTag.SAVORY_SAUCE, CulinaryTag.CHOCOLATE_SWEET),
    # EX-12: Savory Sauce + Sweet Dairy
    (CulinaryTag.SAVORY_SAUCE, CulinaryTag.SWEET_DAIRY),
    # EX-13: Pungent Aroma (Garlic/Onion) + Chocolate / Sweets
    (CulinaryTag.PUNGENT_AROMA, CulinaryTag.CHOCOLATE_SWEET),
    # EX-14: Pungent Aroma (Garlic/Onion) + Sweet Fruit
    (CulinaryTag.PUNGENT_AROMA, CulinaryTag.SWEET_FRUIT),
    # EX-15: Pungent Aroma (Garlic/Onion) + Sweet Dairy
    (CulinaryTag.PUNGENT_AROMA, CulinaryTag.SWEET_DAIRY),
    # EX-16: Raw Flour + Sweet Fruit
    (CulinaryTag.RAW_FLOUR, CulinaryTag.SWEET_FRUIT),
    # EX-17: Raw Flour + Sweet Dairy
    (CulinaryTag.RAW_FLOUR, CulinaryTag.SWEET_DAIRY),
    # EX-18: Savory Fat + Sweet Dairy
    (CulinaryTag.SAVORY_FAT, CulinaryTag.SWEET_DAIRY),
    # EX-19: Savory Fat + Chocolate / Sweets
    (CulinaryTag.SAVORY_FAT, CulinaryTag.CHOCOLATE_SWEET),
    # EX-20: Savory Fat + Sweet Fruit
    (CulinaryTag.SAVORY_FAT, CulinaryTag.SWEET_FRUIT),
}


@functools.lru_cache(maxsize=1024)
def _infer_roles_from_name(item_name: str) -> frozenset:
    result: Set[Role] = set()
    clean_name = item_name.strip().lower()
    if clean_name in TIER_A_MAP:
        result.update(TIER_A_MAP[clean_name])
    else:
        for alias, mapped_roles in TIER_A_MAP.items():
            pattern = r"(?:\b|_)" + re.escape(alias) + r"(?:\b|_)"
            if re.search(pattern, clean_name):
                result.update(mapped_roles)
                break

    canon = resolve_canonical(item_name)
    if canon == "zucchini":
        result.add(Role.VEGETABLE)
        result.add(Role.VEG_SIDE)
    elif canon == "potatoes":
        result.add(Role.CARB_SIDE)
        result.add(Role.CARB_BASE)

    tags = get_culinary_tags(item_name)
    if CulinaryTag.SAVORY_FAT in tags:
        result.add(Role.FAT_SEASONING)
    if CulinaryTag.SAVORY_MEAT in tags or CulinaryTag.SEAFOOD in tags:
        result.add(Role.PROTEIN_MAIN)
    if CulinaryTag.PASTA_GRAIN in tags:
        result.add(Role.CARB_BASE)
    if CulinaryTag.SAVORY_SAUCE in tags:
        result.add(Role.VEG_SAUCE)
        result.add(Role.VEGETABLE)
    if CulinaryTag.SWEET_FRUIT in tags:
        result.add(Role.FRUIT)
    if CulinaryTag.SWEET_DAIRY in tags:
        result.add(Role.DAIRY_BASE)
    if CulinaryTag.HARD_CHEESE in tags:
        result.add(Role.AROMA_TASTE)
        result.add(Role.AROMA_FINISHING)
    if CulinaryTag.PUNGENT_AROMA in tags or CulinaryTag.HERBS in tags:
        result.add(Role.AROMA_HERB)
        result.add(Role.AROMA_TASTE)
    return frozenset(result)


def _coerce_roles(item_name: str, roles: Optional[Any]) -> Set[Role]:
    """Safely coerces role collections or infers them via classify_food."""
    result: Set[Role] = set()
    if roles is not None:
        if isinstance(roles, (set, list, tuple)):
            for r in roles:
                if isinstance(r, Role):
                    result.add(r)
                elif isinstance(r, str):
                    try:
                        result.add(Role(r))
                    except ValueError:
                        pass
        elif isinstance(roles, Role):
            result.add(roles)
        elif isinstance(roles, str):
            try:
                result.add(Role(roles))
            except ValueError:
                pass

    if not result and item_name and isinstance(item_name, str):
        result.update(_infer_roles_from_name(item_name))

    return result


def are_mutually_exclusive(
    item_a: str,
    roles_a: Optional[Any] = None,
    item_b: str = "",
    roles_b: Optional[Any] = None,
) -> bool:
    """Returns True if item_a and item_b are culinary-wise incompatible / mutually exclusive.

    Args:
        item_a: Name, alias, or token of the first ingredient.
        roles_a: Optional collection of Role enums associated with item_a.
        item_b: Name, alias, or token of the second ingredient.
        roles_b: Optional collection of Role enums associated with item_b.

    Returns:
        bool: True if incompatible, False if compatible.
    """
    if not item_a or not item_b or not isinstance(item_a, str) or not isinstance(item_b, str):
        return False

    canon_a = resolve_canonical(item_a)
    canon_b = resolve_canonical(item_b)

    # Identical foods are never mutually exclusive with themselves
    if canon_a and canon_b and canon_a == canon_b:
        return False

    coerced_a = _coerce_roles(item_a, roles_a)
    coerced_b = _coerce_roles(item_b, roles_b)

    tags_a = get_culinary_tags(item_a, coerced_a)
    tags_b = get_culinary_tags(item_b, coerced_b)

    # Check 17 hard tag-pair exclusions
    for ta in tags_a:
        for tb in tags_b:
            if (ta, tb) in HARD_EXCLUSION_PAIRS or (tb, ta) in HARD_EXCLUSION_PAIRS:
                # Exception: Oats in Breakfast / Snack Bowl naturally pairs with sweet dairy, fruit, and sweets
                if canon_a == "oats" or canon_b == "oats":
                    other_tag = tb if canon_a == "oats" else ta
                    if other_tag in (
                        CulinaryTag.SWEET_DAIRY,
                        CulinaryTag.SWEET_FRUIT,
                        CulinaryTag.CHOCOLATE_SWEET,
                    ):
                        continue
                return True

    # Role-level exclusions
    # Role.FRUIT with CARB_BASE (savory pasta/grains)
    if Role.FRUIT in coerced_a and Role.CARB_BASE in coerced_b:
        if canon_b != "oats":
            return True
    if Role.FRUIT in coerced_b and Role.CARB_BASE in coerced_a:
        if canon_a != "oats":
            return True

    # Role.FRUIT with VEG_SAUCE
    if (Role.FRUIT in coerced_a and Role.VEG_SAUCE in coerced_b) or (Role.FRUIT in coerced_b and Role.VEG_SAUCE in coerced_a):
        return True

    # Role.DAIRY_BASE with savory meat or seafood PROTEIN_MAIN
    if Role.DAIRY_BASE in coerced_a and Role.PROTEIN_MAIN in coerced_b:
        if any(t in tags_b for t in (CulinaryTag.SAVORY_MEAT, CulinaryTag.SEAFOOD)):
            return True
    if Role.DAIRY_BASE in coerced_b and Role.PROTEIN_MAIN in coerced_a:
        if any(t in tags_a for t in (CulinaryTag.SAVORY_MEAT, CulinaryTag.SEAFOOD)):
            return True

    return False


# ---------------------------------------------------------------------------
# 4. Soft Affinity Matrix
# ---------------------------------------------------------------------------

_AFFINITY_DATA: Dict[Tuple[str, str], float] = {
    ("basil", "tomato passata"): 0.98,
    ("evo oil", "tomato passata"): 0.95,
    ("pasta", "tomato passata"): 0.95,
    ("milk", "oats"): 0.95,
    ("honey", "yogurt"): 0.95,
    ("banana", "chocolate"): 0.95,
    ("basil", "evo oil"): 0.95,
    ("evo oil", "pasta"): 0.90,
    ("garlic", "pasta"): 0.88,
    ("parmigiano reggiano", "pasta"): 0.92,
    ("eggs", "pasta"): 0.90,
    ("parmigiano reggiano", "tomato passata"): 0.92,
    ("garlic", "tomato passata"): 0.92,
    ("oregano", "tomato passata"): 0.92,
    ("chicken", "rice"): 0.92,
    ("chicken", "rosemary"): 0.92,
    ("chicken", "evo oil"): 0.88,
    ("chicken", "zucchini"): 0.85,
    ("chicken", "potatoes"): 0.90,
    ("beef", "rosemary"): 0.92,
    ("beef", "potatoes"): 0.90,
    ("beef", "tomato passata"): 0.90,
    ("tuna", "tomato passata"): 0.88,
    ("tuna", "onion"): 0.85,
    ("salmon", "zucchini"): 0.85,
    ("eggs", "tomato passata"): 0.85,
    ("eggs", "parmigiano reggiano"): 0.88,
    ("biscotti", "milk"): 0.92,
    ("apple", "yogurt"): 0.90,
    ("berries", "yogurt"): 0.95,
    ("banana", "yogurt"): 0.90,
    ("oats", "yogurt"): 0.92,
    ("apple", "oats"): 0.88,
    ("honey", "oats"): 0.92,
    ("honey", "walnuts"): 0.90,
    ("apple", "walnuts"): 0.88,
    ("apple", "honey"): 0.88,
    ("oats", "walnuts"): 0.88,
    ("evo oil", "rosemary"): 0.88,
    ("potatoes", "rosemary"): 0.90,
    ("evo oil", "potatoes"): 0.85,
    ("almonds", "yogurt"): 0.90,
    ("walnuts", "yogurt"): 0.90,
    ("chicken", "pasta"): 0.65,
    ("parmigiano reggiano", "tuna"): 0.20,
    ("evo oil", "parmigiano reggiano"): 0.90,
    ("evo oil", "rice"): 0.88,
    ("garlic", "evo oil"): 0.92,
    ("onion", "evo oil"): 0.90,
}

# Construct normalized symmetric lookup table
AFFINITY_MATRIX: Dict[Tuple[str, str], float] = {}
for (item1, item2), score in _AFFINITY_DATA.items():
    pair_key = tuple(sorted([item1, item2]))
    AFFINITY_MATRIX[pair_key] = score  # type: ignore[index]


def get_pairing_score(
    item_a: str,
    item_b: str,
    roles_a: Optional[Any] = None,
    roles_b: Optional[Any] = None,
) -> float:
    """Computes the pairwise flavor affinity score between two ingredients.

    Evaluated via 4 cascading tiers:
    1. Hard Exclusion Check: are_mutually_exclusive(a, roles_a, b, roles_b) -> 0.0
    2. Canonical Pair Lookup in AFFINITY_MATRIX
    3. Role / Category Fallback Heuristic
    4. Neutral Default: 0.50

    Returns:
        float: Normalized affinity score strictly in [0.0, 1.0].
    """
    if not item_a or not item_b or not isinstance(item_a, str) or not isinstance(item_b, str):
        return 0.0

    canon_a = resolve_canonical(item_a)
    canon_b = resolve_canonical(item_b)

    if not canon_a or not canon_b:
        return 0.0

    # Self-pairing returns 1.0
    if canon_a == canon_b:
        return 1.0

    # Tier 1: Hard Exclusion Check
    if are_mutually_exclusive(item_a, roles_a, item_b, roles_b):
        return 0.0

    # Tier 2: Canonical Pair Lookup
    lookup_key = tuple(sorted([canon_a, canon_b]))
    if lookup_key in AFFINITY_MATRIX:
        return AFFINITY_MATRIX[lookup_key]

    # Tier 3: Role / Category Fallback Heuristic
    coerced_a = _coerce_roles(item_a, roles_a)
    coerced_b = _coerce_roles(item_b, roles_b)

    # Double Carb
    if Role.CARB_BASE in coerced_a and Role.CARB_BASE in coerced_b:
        return 0.30

    # Double Meat
    if Role.PROTEIN_MAIN in coerced_a and Role.PROTEIN_MAIN in coerced_b:
        return 0.40

    # Dairy Base + Breakfast Items
    breakfast_roles = {Role.CEREAL_CARB, Role.FRUIT, Role.NUTS_OR_SWEET}
    if (Role.DAIRY_BASE in coerced_a and any(r in breakfast_roles for r in coerced_b)) or (
        Role.DAIRY_BASE in coerced_b and any(r in breakfast_roles for r in coerced_a)
    ):
        return 0.80

    # Mutual Breakfast Items (Cereal Carb, Fruit, Nuts/Sweet)
    if (
        any(r in breakfast_roles for r in coerced_a)
        and any(r in breakfast_roles for r in coerced_b)
    ):
        return 0.80

    # Fat Seasoning + Savory food
    savory_roles = {Role.CARB_BASE, Role.PROTEIN_MAIN, Role.VEGETABLE, Role.VEG_SAUCE, Role.VEG_SIDE}
    if (Role.FAT_SEASONING in coerced_a and any(r in savory_roles for r in coerced_b)) or (
        Role.FAT_SEASONING in coerced_b and any(r in savory_roles for r in coerced_a)
    ):
        return 0.70

    # Aroma / Herb + Savory food
    aroma_roles = {Role.AROMA_TASTE, Role.AROMA_HERB, Role.AROMA_FINISHING}
    if (any(r in aroma_roles for r in coerced_a) and any(r in savory_roles for r in coerced_b)) or (
        any(r in aroma_roles for r in coerced_b) and any(r in savory_roles for r in coerced_a)
    ):
        return 0.75

    # Veg Sauce / Vegetable + Carb Base or Protein Main
    veg_roles = {Role.VEGETABLE, Role.VEG_SAUCE, Role.VEG_SIDE}
    main_roles = {Role.CARB_BASE, Role.PROTEIN_MAIN}
    if (any(r in veg_roles for r in coerced_a) and any(r in main_roles for r in coerced_b)) or (
        any(r in veg_roles for r in coerced_b) and any(r in main_roles for r in coerced_a)
    ):
        return 0.70

    # Tier 4: Neutral Default
    return 0.50


# ---------------------------------------------------------------------------
# 5. Multi-Ingredient Combination Harmony Evaluator
# ---------------------------------------------------------------------------

def score_combination(items: Sequence[Any]) -> float:
    """Evaluates the overall culinary harmony of an ingredient combination.

    Args:
        items: Sequence of ingredient names, tokens, or dict/candidate objects.

    Returns:
        float: Aggregated culinary harmony score strictly in [0.0, 1.0].
               Returns 0.0 if any pairwise mutual exclusion exists.
               Returns 1.0 for a valid single ingredient.
               Returns 0.0 for empty sequences.
    """
    if not items:
        return 0.0

    # Extract valid string names and preserve candidate roles
    clean_items: List[Tuple[str, Optional[List[Any]]]] = []
    for it in items:
        if it is None:
            continue
        if isinstance(it, str):
            s = it.strip()
            if s:
                clean_items.append((s, None))
        elif hasattr(it, "name") and isinstance(it.name, str) and it.name.strip():
            s = it.name.strip()
            roles: List[Any] = []
            if hasattr(it, "all_eligible_roles") and it.all_eligible_roles:
                if isinstance(it.all_eligible_roles, (list, tuple, set)):
                    roles.extend(it.all_eligible_roles)
                else:
                    roles.append(it.all_eligible_roles)
            if hasattr(it, "roles") and it.roles:
                if isinstance(it.roles, (list, tuple, set)):
                    roles.extend(it.roles)
                else:
                    roles.append(it.roles)
            if hasattr(it, "role") and it.role is not None:
                if it.role not in roles:
                    roles.append(it.role)
            clean_items.append((s, roles if roles else None))
        elif isinstance(it, dict) and "name" in it and isinstance(it["name"], str) and it["name"].strip():
            s = it["name"].strip()
            roles: List[Any] = []
            for rkey in ("all_eligible_roles", "roles", "role"):
                val = it.get(rkey)
                if val is not None:
                    if isinstance(val, (list, tuple, set)):
                        for elem in val:
                            if elem not in roles:
                                roles.append(elem)
                    elif val not in roles:
                        roles.append(val)
            clean_items.append((s, roles if roles else None))

    n = len(clean_items)
    if n == 0:
        return 0.0
    if n == 1:
        return 1.0

    # Check for any hard exclusions first
    for i in range(n):
        name_i, roles_i = clean_items[i]
        for j in range(i + 1, n):
            name_j, roles_j = clean_items[j]
            if are_mutually_exclusive(name_i, roles_i, name_j, roles_j):
                return 0.0

    # Arithmetic mean of all pairwise affinity scores
    total_score = 0.0
    pairs_count = 0
    for i in range(n):
        name_i, roles_i = clean_items[i]
        for j in range(i + 1, n):
            name_j, roles_j = clean_items[j]
            score = get_pairing_score(name_i, name_j, roles_i, roles_j)
            if score <= 0.0:
                return 0.0
            total_score += score
            pairs_count += 1

    if pairs_count == 0:
        return 1.0

    return round(total_score / pairs_count, 3)
