"""meal_planner.generator.domain - Taxonomy, archetypes/slots, food pairing, and core models."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import functools
import itertools
import math
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple, Union


def _clean_str(val: Any) -> str:
    return val.strip().lower() if isinstance(val, str) else ""

def _coerce_role(val: Any) -> Optional["Role"]:
    if isinstance(val, Role):
        return val
    if isinstance(val, str):
        try:
            return Role(val)
        except ValueError:
            return None
    return None

def _parse_float(val: Any) -> Optional[float]:
    if isinstance(val, bool) or val is None:
        return None
    try:
        f = float(val)
        return None if math.isnan(f) else f
    except (ValueError, TypeError):
        return None

def _safe_nonneg(val: Any) -> float:
    f = _parse_float(val)
    return max(0.0, f) if (f is not None and math.isfinite(f)) else 0.0

def _norm_pairing_text(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[_\-\.\(\)\,\/]", " ", name).lower().strip())

# --- 1. Culinary Roles & Taxonomy ---

class Role(str, Enum):
    CARB_BASE = "CARB_BASE"
    PROTEIN_MAIN = "PROTEIN_MAIN"
    VEGETABLE = "VEGETABLE"
    FAT_SEASONING = "FAT_SEASONING"
    DAIRY_BASE = "DAIRY_BASE"
    FRUIT = "FRUIT"
    AROMA_TASTE = "AROMA_TASTE"
    CEREAL_CARB = "CEREAL_CARB"
    NUTS_OR_SWEET = "NUTS_OR_SWEET"
    PROTEIN_ACCENT = "PROTEIN_ACCENT"
    BINDER_SAUCE = "BINDER_SAUCE"
    VEG_SAUCE = "VEG_SAUCE"
    VEG_SIDE = "VEG_SIDE"
    CARB_SIDE = "CARB_SIDE"
    AROMA_HERB = "AROMA_HERB"
    AROMA_FINISHING = "AROMA_FINISHING"
    def __str__(self) -> str: return self.value

_ROLE_COMPAT_EXTRAS: Dict[Role, Tuple[Role, ...]] = {
    Role.CARB_SIDE: (Role.CARB_BASE,), Role.CEREAL_CARB: (Role.CARB_BASE,),
    Role.PROTEIN_ACCENT: (Role.PROTEIN_MAIN,), Role.BINDER_SAUCE: (Role.PROTEIN_MAIN,),
    Role.VEGETABLE: (Role.VEG_SAUCE, Role.VEG_SIDE), Role.VEG_SAUCE: (Role.VEGETABLE,),
    Role.VEG_SIDE: (Role.VEGETABLE,), Role.AROMA_TASTE: (Role.AROMA_HERB, Role.AROMA_FINISHING),
    Role.AROMA_HERB: (Role.AROMA_TASTE,), Role.AROMA_FINISHING: (Role.AROMA_TASTE,),
}
ROLE_COMPATIBILITY: Dict[Role, Set[Role]] = {r: {r, *_ROLE_COMPAT_EXTRAS.get(r, ())} for r in Role}

def are_roles_compatible(candidate_role: Union[Role, str], required_slot_role: Union[Role, str]) -> bool:
    cand, req = _coerce_role(candidate_role), _coerce_role(required_slot_role)
    return cand is not None and req is not None and (cand == req or cand in ROLE_COMPATIBILITY.get(req, {req}))

def is_role_compatible(required_role: Union[Role, str], candidate_roles: Iterable[Union[Role, str]]) -> bool:
    req = _coerce_role(required_role)
    if req is None:
        return False
    if not isinstance(candidate_roles, Iterable) or isinstance(candidate_roles, (str, bytes)):
        return are_roles_compatible(candidate_roles, req) if isinstance(candidate_roles, (Role, str)) else False
    allowed = ROLE_COMPATIBILITY.get(req, {req})
    try:
        return any((c := _coerce_role(r)) is not None and c in allowed for r in candidate_roles)
    except (TypeError, ValueError):
        return False

# 30 contiguous runs preserved in exact source order (175 keys)
_TIER_A_GROUPS: Tuple[Tuple[Tuple[Role, ...], Tuple[str, ...]], ...] = (
    ((Role.CARB_BASE,), ("pasta", "spaghetti", "spaghetti n.5", "penne", "penne rigate", "rigatoni", "fusilli", "farfalle", "tagliatelle", "linguine", "riso", "rice", "carnaroli", "carnaroli rice", "basmati", "farro", "orzo", "quinoa", "couscous", "polenta")),
    ((Role.CARB_BASE, Role.CARB_SIDE), ("pane", "bread")),
    ((Role.CARB_SIDE, Role.CARB_BASE), ("patate", "potatoes")),
    ((Role.CARB_BASE,), ("flour", "00 flour", "farina", "long pasta", "soup_pasta")),
    ((Role.CEREAL_CARB, Role.CARB_BASE), ("avena", "oats", "rolled oats")),
    ((Role.CEREAL_CARB,), ("muesli", "granola")),
    ((Role.CEREAL_CARB, Role.CARB_BASE), ("biscotti",)),
    ((Role.PROTEIN_MAIN,), ("pollo", "chicken", "chicken breast", "petto di pollo", "tacchino", "turkey", "manzo", "beef", "vitello", "maiale", "pork")),
    ((Role.PROTEIN_MAIN, Role.PROTEIN_ACCENT), ("tonno", "tuna", "tonno al naturale", "salmone", "salmon")),
    ((Role.PROTEIN_MAIN,), ("merluzzo", "cod", "tofu", "seitan", "tempeh")),
    ((Role.PROTEIN_MAIN, Role.CARB_SIDE), ("ceci", "chickpeas", "fagioli", "beans", "lenticchie", "lentils")),
    ((Role.PROTEIN_MAIN, Role.PROTEIN_ACCENT), ("uova", "eggs", "uovo", "egg")),
    ((Role.AROMA_TASTE, Role.PROTEIN_MAIN, Role.AROMA_FINISHING), ("parmigiano", "parmigiano reggiano", "parmesan", "grana", "grana padano")),
    ((Role.AROMA_TASTE, Role.AROMA_FINISHING), ("pecorino",)),
    ((Role.PROTEIN_MAIN, Role.DAIRY_BASE), ("ricotta",)),
    ((Role.PROTEIN_MAIN, Role.PROTEIN_ACCENT), ("mozzarella", "feta")),
    ((Role.VEGETABLE, Role.VEG_SAUCE, Role.VEG_SIDE), ("pomodoro", "tomato", "tomato puree", "puree", "passata", "tomato passata", "passata di pomodoro")),
    ((Role.VEGETABLE, Role.VEG_SIDE, Role.VEG_SAUCE), ("zucchine", "zucchini")),
    ((Role.VEGETABLE, Role.VEG_SIDE), ("spinaci", "spinach", "broccoli", "carote", "carrots")),
    ((Role.VEGETABLE, Role.VEG_SAUCE, Role.VEG_SIDE), ("funghi", "mushrooms")),
    ((Role.VEGETABLE, Role.VEG_SIDE), ("peperoni", "peppers", "melanzane", "eggplant")),
    ((Role.VEGETABLE, Role.AROMA_TASTE), ("cipolla", "onion")),
    ((Role.FAT_SEASONING,), ("olio", "oil", "extra virgin olive oil", "evo oil", "olio evo", "olio extravergine", "olio di oliva", "burro", "butter", "olio di semi")),
    ((Role.DAIRY_BASE,), ("latte", "milk", "whole milk", "latte intero", "latte parzialmente scremato", "yogurt", "plain yogurt", "skyr", "kefir")),
    ((Role.FRUIT,), ("mela", "mele", "apple", "apples", "banana", "banane", "frutti di bosco", "berries", "mirtilli", "fragole", "pera", "pear", "arancia", "orange")),
    ((Role.AROMA_TASTE, Role.AROMA_HERB), ("basilico", "basil", "origano", "oregano", "rosmarino", "rosemary", "prezzemolo", "parsley")),
    ((Role.AROMA_TASTE,), ("pepe", "pepper", "sale", "salt", "sale marino", "aglio", "garlic")),
    ((Role.NUTS_OR_SWEET, Role.FAT_SEASONING), ("mandorle", "almonds", "noci", "walnuts", "nocciole", "hazelnuts", "pistacchi", "pistachios", "anacardi", "cashews", "pinoli", "pine nuts")),
    ((Role.NUTS_OR_SWEET,), ("semi di chia", "chia seeds", "semi di lino", "flax seeds", "semi di girasole", "sunflower seeds", "semi di zucca", "pumpkin seeds", "cacao", "cocoa", "sciroppo d'acero", "maple syrup", "miele", "honey", "cioccolato", "chocolate")),
    ((Role.NUTS_OR_SWEET, Role.AROMA_TASTE), ("sugar", "zucchero")),
)
TIER_A_MAP: Dict[str, List[Role]] = {k: list(roles) for roles, keys in _TIER_A_GROUPS for k in keys}

def _tier_a_lookup(clean_name: str) -> Optional[List[Role]]:
    if clean_name in TIER_A_MAP:
        return list(TIER_A_MAP[clean_name])
    if clean_name:
        for alias, roles in TIER_A_MAP.items():
            if re.search(r"(?:\b|_)" + re.escape(alias) + r"(?:\b|_)", clean_name):
                return list(roles)
    return None

def fingerprint_macros(kcal_100g: float, protein_100g: float, carbs_100g: float, fat_100g: float,
                       fiber_100g: float = 0.0, sugars_100g: float = 0.0) -> List[Role]:
    k, p, c, f, s = (_safe_nonneg(v) for v in (kcal_100g, protein_100g, carbs_100g, fat_100g, sugars_100g))
    if k <= 0.0:
        return [Role.PROTEIN_MAIN] if p >= 15.0 else [Role.AROMA_TASTE]
    if f >= 45.0:
        return [Role.NUTS_OR_SWEET, Role.FAT_SEASONING] if (p >= 8.0 and (c >= 4.0 or k >= 500.0)) else [Role.FAT_SEASONING]
    if (s >= 8.0 or (s == 0.0 and 8.0 <= c <= 25.0)) and p < 2.0 and f < 1.5:
        return [Role.FRUIT]
    if k <= 55.0 and f <= 2.0 and (s < 7.0 or s == 0.0) and p < 10.0:
        return [Role.VEGETABLE]
    per = (p * 4.0) / k
    if (per >= 0.35 and p >= 5.0) or p >= 15.0:
        return [Role.PROTEIN_MAIN]
    if c >= 50.0 and k >= 200.0:
        return [Role.CARB_BASE]
    if p >= 3.0 and 3.5 <= c <= 7.0 and k <= 150.0:
        return [Role.DAIRY_BASE]
    return [Role.PROTEIN_MAIN] if per >= 0.35 else [Role.AROMA_TASTE]

def classify_food(name: str, kcal_100g: float, protein_100g: float, carbs_100g: float, fat_100g: float,
                  fiber_100g: float = 0.0, sugars_100g: float = 0.0) -> List[Role]:
    found = _tier_a_lookup(_clean_str(name))
    return found if found is not None else fingerprint_macros(kcal_100g, protein_100g, carbs_100g, fat_100g, fiber_100g, sugars_100g)

DEFAULT_PORTION_BOUNDS: Dict[Role, Tuple[float, float]] = {
    Role.CARB_BASE: (60.0, 120.0), Role.CARB_SIDE: (40.0, 100.0), Role.CEREAL_CARB: (30.0, 80.0),
    Role.PROTEIN_MAIN: (100.0, 250.0), Role.PROTEIN_ACCENT: (40.0, 120.0), Role.BINDER_SAUCE: (30.0, 60.0),
    Role.VEGETABLE: (100.0, 300.0), Role.VEG_SAUCE: (50.0, 200.0), Role.VEG_SIDE: (120.0, 350.0),
    Role.FAT_SEASONING: (5.0, 20.0), Role.DAIRY_BASE: (100.0, 250.0), Role.FRUIT: (50.0, 150.0),
    Role.NUTS_OR_SWEET: (10.0, 25.0), Role.AROMA_TASTE: (5.0, 15.0), Role.AROMA_HERB: (1.0, 5.0),
    Role.AROMA_FINISHING: (5.0, 15.0),
}
DEFAULT_ROLE_PORTIONS: Dict[Role, Tuple[float, float]] = DEFAULT_PORTION_BOUNDS
SPECIFIC_PORTION_OVERRIDES: List[Tuple[str, Role, Tuple[float, float]]] = [(kw, r, bounds) for kws, rbs in (
    (("parmigiano", "parmesan", "grana"), ((Role.PROTEIN_MAIN, (40.0, 80.0)), (Role.AROMA_TASTE, (5.0, 20.0)), (Role.AROMA_FINISHING, (5.0, 20.0)))),
    (("pecorino",), ((Role.AROMA_TASTE, (5.0, 20.0)), (Role.AROMA_FINISHING, (5.0, 20.0)))),
    (("uova", "eggs", "egg", "uovo"), ((Role.PROTEIN_MAIN, (100.0, 200.0)), (Role.PROTEIN_ACCENT, (50.0, 60.0)))),
    (("pasta", "spaghetti", "penne"), ((Role.CARB_BASE, (60.0, 120.0)),)),
    (("riso", "rice"), ((Role.CARB_BASE, (50.0, 100.0)),)),
    (("passata", "puree", "pomodoro"), ((Role.VEG_SAUCE, (60.0, 200.0)),)),
    (("biscotti",), ((Role.CEREAL_CARB, (20.0, 50.0)),)),
    (("avena", "oats"), ((Role.CEREAL_CARB, (30.0, 80.0)),)),
) for kw in kws for r, bounds in rbs]

def get_portion_bounds(role: Union[Role, str], food_name: str = "") -> Tuple[float, float]:
    target_role = _coerce_role(role)
    if target_role is None:
        return (10.0, 100.0)
    if clean_name := _clean_str(food_name):
        for keyword, override_role, bounds in SPECIFIC_PORTION_OVERRIDES:
            if override_role == target_role and keyword in clean_name:
                return bounds
    return DEFAULT_PORTION_BOUNDS.get(target_role, (10.0, 100.0))

# --- 2. Slots & Culinary Archetypes ---

@dataclass(frozen=True)
class Slot:
    role: Role
    cardinality_min: int = 1
    cardinality_max: int = 1
    is_optional: bool = False
    portion_min_g: float = 0.0
    portion_max_g: float = 0.0
    name: str = ""
    compatible_roles: Tuple[Role, ...] = field(default=())

    def __post_init__(self) -> None:
        coerced_role = _coerce_role(self.role)
        if coerced_role is None:
            raise ValueError(f"Invalid slot role: {self.role!r}")
        if not isinstance(self.role, Role):
            object.__setattr__(self, "role", coerced_role)
        if isinstance(self.cardinality_min, bool) or isinstance(self.cardinality_max, bool) or self.cardinality_min < 0 or self.cardinality_max < self.cardinality_min:
            raise ValueError(f"Invalid cardinality range [{self.cardinality_min}, {self.cardinality_max}]")
        pmin, pmax = _parse_float(self.portion_min_g), _parse_float(self.portion_max_g)
        if pmin is None or pmax is None or not math.isfinite(pmin) or not math.isfinite(pmax) or pmin < 0 or pmax < pmin:
            raise ValueError(f"Invalid portion range [{self.portion_min_g}, {self.portion_max_g}]")
        if not self.name:
            object.__setattr__(self, "name", coerced_role.value.replace("_", " ").title())
        if not self.compatible_roles:
            object.__setattr__(self, "compatible_roles", tuple(ROLE_COMPATIBILITY.get(self.role, {self.role})))

    @property
    def bounds(self) -> Tuple[float, float]: return (self.portion_min_g, self.portion_max_g)
    def accepts_role(self, role: Union[Role, str]) -> bool:
        target = c if (c := _coerce_role(role)) is not None else role
        return target == self.role or target in self.compatible_roles
    def validate_cardinality(self, count: int) -> bool: return self.cardinality_min <= count <= self.cardinality_max
    def validate_portion(self, grams: Any) -> bool:
        return (val := _parse_float(grams)) is not None and self.portion_min_g <= val <= self.portion_max_g
    def clamp_portion(self, grams: Any) -> float:
        val = self.portion_min_g if (parsed := _parse_float(grams)) is None else parsed
        return max(self.portion_min_g, min(val, self.portion_max_g))

class SlotsDict(dict):
    def __init__(self, slots: Sequence[Slot]):
        super().__init__({s.role: s.cardinality_max for s in slots})
        self._slots = tuple(slots)
    @property
    def slots(self) -> Tuple[Slot, ...]: return self._slots
    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, str) and not isinstance(key, Role) and (coerced := _coerce_role(key)) is not None:
            key = coerced
        return super().__getitem__(key)
    def __len__(self) -> int: return len(self._slots)

@dataclass
class Archetype:
    name: str
    slots: Tuple[Slot, ...] = field(default_factory=tuple)
    default_meal: str = "lunch"
    allowed_meals: Tuple[str, ...] = ("lunch", "dinner")
    description: str = ""
    required_slots: SlotsDict = field(init=False)
    optional_slots: SlotsDict = field(init=False)

    def __init__(
        self, name: str, slots: Optional[Sequence[Slot]] = None,
        required_slots: Optional[Dict[Role, int]] = None, optional_slots: Optional[Dict[Role, int]] = None,
        default_meal: str = "lunch", allowed_meals: Optional[Sequence[str]] = None, description: str = "",
    ):
        self.name = name
        self.default_meal = (default_meal or "lunch").lower()
        self.description = description or ""
        if slots is not None:
            self.slots = tuple(slots)
        elif required_slots is not None or optional_slots is not None:
            constructed: List[Slot] = []
            for mapping, is_opt, c_min, fb in ((required_slots, False, 1, (50.0, 200.0)), (optional_slots, True, 0, (10.0, 100.0))):
                if mapping:
                    for role, count in mapping.items():
                        r = Role(role) if isinstance(role, str) else role
                        constructed.append(Slot(r, c_min, count, is_opt, *DEFAULT_ROLE_PORTIONS.get(r, fb)))
            self.slots = tuple(constructed)
        else:
            self.slots = ()
        self.required_slots = SlotsDict([s for s in self.slots if not s.is_optional])
        self.optional_slots = SlotsDict([s for s in self.slots if s.is_optional])
        self.allowed_meals = tuple(m.lower() for m in allowed_meals) if allowed_meals is not None else (
            ("breakfast",) if self.default_meal == "breakfast" else ("lunch", "dinner")
        )

    @property
    def all_roles(self) -> Set[Role]: return {s.role for s in self.slots}
    def get_slot(self, role: Union[Role, str]) -> Optional[Slot]:
        role_enum = _coerce_role(role)
        return next((s for s in self.slots if s.role == role_enum), None) or (
            next((s for s in self.slots if s.accepts_role(role_enum)), None) if role_enum is not None else None
        )
    def is_eligible_for_meal(self, meal: str) -> bool:
        return isinstance(meal, str) and meal.strip().lower() in [m.lower() for m in self.allowed_meals]

def _make_archetype(name: str, specs: Tuple[Tuple[Any, ...], ...], default_meal: str, allowed: Tuple[str, ...], desc: str) -> Archetype:
    return Archetype(name=name, slots=tuple(Slot(*sp) for sp in specs), default_meal=default_meal, allowed_meals=allowed, description=desc)

GRAIN_PASTA_DISH = _make_archetype("Grain / Pasta Dish", (
    (Role.CARB_BASE, 1, 1, False, 60.0, 120.0, "Carbohydrate Base"), (Role.FAT_SEASONING, 1, 1, False, 5.0, 20.0, "Fat Seasoning"),
    (Role.VEG_SAUCE, 0, 2, True, 50.0, 200.0, "Vegetable Sauce"), (Role.PROTEIN_ACCENT, 0, 1, True, 40.0, 120.0, "Protein Accent"),
    (Role.AROMA_FINISHING, 0, 2, True, 5.0, 15.0, "Aroma Finishing"),
), "lunch", ("lunch", "dinner"), "Primi piatti tradizionali a base di pasta, riso o cereali con condimenti, salse di verdure e guarnizioni aromatiche.")
MAIN_AND_SIDE_PLATE = _make_archetype("Main & Side Plate", (
    (Role.PROTEIN_MAIN, 1, 1, False, 100.0, 250.0, "Protein Main"), (Role.VEG_SIDE, 1, 2, False, 120.0, 350.0, "Vegetable Side"),
    (Role.FAT_SEASONING, 1, 1, False, 5.0, 20.0, "Fat Seasoning"), (Role.CARB_SIDE, 0, 1, True, 40.0, 100.0, "Carbohydrate Side"),
    (Role.AROMA_HERB, 0, 1, True, 1.0, 5.0, "Aroma Herb"),
), "dinner", ("lunch", "dinner"), "Secondi piatti con pietanza proteica centrale, ricco contorno di verdure e condimento aromatico.")
ONE_POT_BALANCED_BOWL = _make_archetype("One-Pot Balanced Bowl", (
    (Role.CARB_BASE, 1, 1, False, 50.0, 90.0, "Carbohydrate Base"), (Role.PROTEIN_MAIN, 1, 1, False, 80.0, 180.0, "Protein Main"),
    (Role.VEGETABLE, 1, 1, False, 100.0, 250.0, "Vegetable"), (Role.FAT_SEASONING, 1, 1, False, 5.0, 15.0, "Fat Seasoning"),
    (Role.AROMA_TASTE, 0, 1, True, 5.0, 15.0, "Aroma Taste"),
), "lunch", ("lunch", "dinner"), "Piatto unico bilanciato / fitness bowl con base di cereali, porzione proteica, verdura fresca e grassi buoni.")
BREAKFAST_SNACK_BOWL = _make_archetype("Breakfast / Snack Bowl", (
    (Role.DAIRY_BASE, 1, 1, False, 100.0, 250.0, "Dairy Base"), (Role.CEREAL_CARB, 1, 1, False, 30.0, 80.0, "Cereal Carb"),
    (Role.FRUIT, 0, 1, True, 50.0, 150.0, "Fruit"), (Role.NUTS_OR_SWEET, 0, 1, True, 10.0, 25.0, "Nuts or Sweet"),
), "breakfast", ("breakfast",), "Ciotola nutriente per colazione o spuntino a base di latte o yogurt, cereali integrali, frutta fresca e frutta secca.")

ARCHETYPES: Dict[str, Archetype] = {a.name: a for a in (GRAIN_PASTA_DISH, MAIN_AND_SIDE_PLATE, ONE_POT_BALANCED_BOWL, BREAKFAST_SNACK_BOWL)}
ARCHETYPE_ALIASES: Dict[str, str] = {alias: canon for canon, aliases in (
    ("Grain / Pasta Dish", ("grain / pasta dish", "grain and pasta dish", "grain", "pasta", "pasta dish", "primi", "primo", "primi piatti", "risotto")),
    ("Main & Side Plate", ("main & side plate", "main and side plate", "main & side", "main_side", "plate", "secondi", "secondo", "secondi piatti")),
    ("One-Pot Balanced Bowl", ("one-pot balanced bowl", "one pot balanced bowl", "one_pot", "bowl", "power bowl", "fitness bowl", "piatto unico", "piatto_unico")),
    ("Breakfast / Snack Bowl", ("breakfast / snack bowl", "breakfast and snack bowl", "breakfast", "breakfast bowl", "colazione", "snack", "spuntino", "yogurt bowl")),
) for alias in aliases}
MEAL_ARCHETYPES: Dict[str, Tuple[Archetype, ...]] = {
    "breakfast": (BREAKFAST_SNACK_BOWL,),
    "lunch": (GRAIN_PASTA_DISH, ONE_POT_BALANCED_BOWL, MAIN_AND_SIDE_PLATE),
    "dinner": (MAIN_AND_SIDE_PLATE, ONE_POT_BALANCED_BOWL, GRAIN_PASTA_DISH),
}

def get_archetype(name: str) -> Archetype:
    if not isinstance(name, str):
        if name is None:
            raise ValueError("Archetype name cannot be empty.")
        raise ValueError(f"Unknown archetype '{name}'. Archetype name must be a string, got {type(name).__name__}. Valid archetypes: {list(ARCHETYPES.keys())}")
    if not (clean := name.strip().lower()):
        raise ValueError("Archetype name cannot be empty.")
    if (canonical := ARCHETYPE_ALIASES.get(clean)) and canonical in ARCHETYPES:
        return ARCHETYPES[canonical]
    for key, arch in ARCHETYPES.items():
        if key.lower() == clean:
            return arch
    raise ValueError(f"Unknown archetype '{name}'. Valid archetypes: {list(ARCHETYPES.keys())}")

def get_archetypes_for_meal(meal: str) -> List[Archetype]:
    clean = meal.strip().lower() if isinstance(meal, str) else ""
    if clean not in MEAL_ARCHETYPES:
        raise ValueError(f"Unknown meal type '{meal}'. Valid meal types: {list(MEAL_ARCHETYPES.keys())}")
    return list(MEAL_ARCHETYPES[clean])

def get_default_archetype_for_meal(meal: str) -> Archetype: return get_archetypes_for_meal(meal)[0]
def get_slot_for_role(archetype: Archetype, role: Union[Role, str]) -> Optional[Slot]: return archetype.get_slot(role)

def clamp_ingredient_portion(archetype: Optional[Archetype], role: Any, grams: Any = 0.0) -> float:
    slot: Optional[Slot] = None
    if archetype is not None and hasattr(archetype, "get_slot"):
        try:
            slot = archetype.get_slot(role)
        except (TypeError, ValueError):
            slot = None
    if slot is not None:
        return slot.clamp_portion(grams)
    min_g, max_g = 10.0, 250.0
    role_enum = _coerce_role(role)
    try:
        min_g, max_g = DEFAULT_ROLE_PORTIONS.get(role_enum if role_enum is not None else role, (10.0, 250.0))
    except (TypeError, KeyError):
        min_g, max_g = 10.0, 250.0
    val = min_g if (parsed := _parse_float(grams)) is None else parsed
    return max(min_g, min(val, max_g))

@dataclass
class SlotValidationResult:
    is_valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

def _extract_item_roles(item: Any) -> List[Any]:
    if item is None:
        return []
    item_role = getattr(item, "role", None) or (item.get("role") if isinstance(item, dict) else None)
    item_roles = getattr(item, "all_eligible_roles", None) or getattr(item, "roles", None) or (
        (item.get("roles") or item.get("all_eligible_roles")) if isinstance(item, dict) else None
    )
    raw: List[Any] = [item_role] if item_role is not None else []
    if item_roles is not None:
        raw.extend(item_roles if isinstance(item_roles, (Sequence, set)) and not isinstance(item_roles, str) else [item_roles])
    resolved: List[Any] = []
    for r in raw:
        if (val := c if (c := _coerce_role(r)) is not None else r) not in resolved:
            resolved.append(val)
    return resolved

def _extract_item_id(item: Any) -> str:
    if item is not None:
        for fld in ("id", "ID", "food_id", "product_id", "name", "product"):
            val = item.get(fld) if isinstance(item, (dict, Mapping)) else None
            if val is None and hasattr(item, fld):
                try:
                    val = getattr(item, fld)
                except Exception:
                    val = None
            if val is not None and (s_val := str(val).strip()):
                return s_val
    return f"anonymous_{id(item)}"

def validate_slot_filling(archetype: Archetype, assignments: Union[Dict[Any, Sequence[Any]], Sequence[Any]]) -> SlotValidationResult:
    errors, warnings = [], []
    normalized: Dict[Slot, List[Any]] = {s: [] for s in archetype.slots}
    seen: Dict[str, str] = {}

    def _record(item: Any, slot: Slot) -> None:
        iid = _extract_item_id(item)
        if iid in seen:
            errors.append(f"Duplicate ingredient '{iid}' is assigned to multiple slots: '{seen[iid]}' and '{slot.name}'.")
        else:
            seen[iid] = slot.name

    if isinstance(assignments, (dict, Mapping)):
        for key, raw_items in assignments.items():
            slot = key if isinstance(key, Slot) else (archetype.get_slot(key) if isinstance(key, (Role, str)) else None)
            if slot is None or slot not in archetype.slots:
                errors.append(f"Assignment key '{key}' does not correspond to any slot in archetype '{archetype.name}'.")
                continue
            if raw_items is None:
                continue
            seq = [raw_items] if (isinstance(raw_items, (dict, Mapping, str, bytes)) or not isinstance(raw_items, (Sequence, set, frozenset))) else list(raw_items)
            for item in seq:
                if item is None:
                    errors.append(f"Slot '{slot.name}' contains a null (None) ingredient candidate.")
                    continue
                _record(item, slot)
                if not (c_roles := _extract_item_roles(item)):
                    errors.append(f"Item '{item}' assigned to slot '{slot.name}' has no defined culinary role.")
                elif not any(slot.accepts_role(r) for r in c_roles):
                    errors.append(f"Item '{item}' with role(s) {[str(r) for r in c_roles]} is incompatible with slot '{slot.name}' (accepts {[str(r) for r in slot.compatible_roles]}).")
                else:
                    normalized[slot].append(item)
    elif isinstance(assignments, Sequence) and not isinstance(assignments, (str, bytes)):
        passes = (
            lambda s: not s.is_optional and len(normalized[s]) < s.cardinality_max,
            lambda s: s.is_optional and len(normalized[s]) < s.cardinality_max,
            lambda s: True,
        )
        for item in assignments:
            if item is None:
                errors.append("Ingredient candidate sequence contains a null (None) item.")
                continue
            if not (c_roles := _extract_item_roles(item)):
                errors.append(f"Item '{item}' has no defined culinary role and does not fit into any slot of archetype '{archetype.name}'.")
                continue
            if (matched := next((s for p in passes for s in archetype.slots if p(s) and any(s.accepts_role(r) for r in c_roles)), None)) is not None:
                _record(item, matched)
                normalized[matched].append(item)
            else:
                errors.append(f"Item '{item}' with role(s) {[str(r) for r in c_roles]} does not fit into any slot of archetype '{archetype.name}'.")

    for slot in archetype.slots:
        count = len(normalized.get(slot, []))
        if not slot.is_optional and count < slot.cardinality_min:
            errors.append(f"Missing required slot '{slot.name}' ({slot.role.value}): requires at least {slot.cardinality_min} ingredient(s), but got {count}.")
        if count > slot.cardinality_max:
            errors.append(f"Slot '{slot.name}' ({slot.role.value}) exceeds maximum cardinality: allowed at most {slot.cardinality_max}, but got {count}.")
        for item in normalized[slot]:
            if isinstance(item, Mapping):
                qty = item["resolved_quantity_g"] if item.get("resolved_quantity_g") is not None else item.get("quantity")
            else:
                qty = getattr(item, "resolved_quantity_g", None)
                if qty is None:
                    qty = getattr(item, "quantity", None)
            if qty is not None and (qty_f := _parse_float(qty)) is not None and not slot.validate_portion(qty_f):
                warnings.append(f"Item '{item}' portion {qty_f}g is outside slot '{slot.name}' physiological bounds [{slot.portion_min_g}g, {slot.portion_max_g}g].")
    return SlotValidationResult(is_valid=(len(errors) == 0), errors=errors, warnings=warnings)

# --- 3. Food Pairing Graph & Harmony Engine ---

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

TAG_KEYWORDS: List[Tuple[CulinaryTag, Tuple[str, ...]]] = [
    (CulinaryTag.SEAFOOD, ("tonno", "tuna", "salmone", "salmon", "merluzzo", "cod", "pesce", "fish", "gamberi", "shrimp", "prawn", "calamari", "squid", "acciughe", "anchovy", "cozze", "mussels", "vongole", "clams")),
    (CulinaryTag.SWEET_DAIRY, ("latte", "milk", "yogurt", "skyr", "kefir", "whole milk", "latte intero", "panna", "cream", "ricotta")),
    (CulinaryTag.CHOCOLATE_SWEET, ("cioccolato", "chocolate", "cacao", "cocoa", "miele", "honey", "zucchero", "sugar", "biscotti", "biscuit", "cookies", "muesli", "granola", "sciroppo", "syrup", "dolce")),
    (CulinaryTag.PASTA_GRAIN, ("pasta", "spaghetti", "spaghetti n.5", "penne", "penne rigate", "fusilli", "rigatoni", "farfalle", "riso", "rice", "carnaroli", "basmati", "farro", "orzo", "soup_pasta", "long pasta", "noodles", "avena", "oats", "quinoa")),
    (CulinaryTag.SAVORY_MEAT, ("pollo", "chicken", "chicken breast", "petto di pollo", "tacchino", "turkey", "manzo", "beef", "maiale", "pork", "vitello", "veal", "agnello", "lamb", "prosciutto", "pancetta", "bacon", "salsiccia", "sausage")),
    (CulinaryTag.SWEET_FRUIT, ("mela", "mele", "apple", "apples", "banana", "banane", "bananas", "pera", "pere", "pear", "pears", "frutti di bosco", "berries", "fragole", "fragola", "strawberry", "strawberries", "mirtilli", "mirtillo", "blueberry", "blueberries", "arancia", "arance", "orange", "oranges", "pesca", "pesche", "peach", "peaches", "albicocca", "albicocche", "apricot", "apricots", "uva", "uve", "grape", "grapes", "kiwi", "ananas", "pineapple", "pineapples")),
    (CulinaryTag.SAVORY_SAUCE, ("passata", "passata di pomodoro", "puree", "tomato puree", "tomato passata", "pomodoro", "tomato sauce", "pelati", "salsa")),
    (CulinaryTag.PUNGENT_AROMA, ("aglio", "garlic", "cipolla", "onion", "scalogno", "shallot", "porro", "leek")),
    (CulinaryTag.RAW_FLOUR, ("farina", "00 flour", "flour", "semola", "amido", "starch")),
    (CulinaryTag.HARD_CHEESE, ("parmigiano", "parmigiano reggiano", "parmesan", "grana", "grana padano", "pecorino")),
    (CulinaryTag.SAVORY_FAT, ("olio", "oil", "extra virgin olive oil", "evo oil", "olio evo", "olio extravergine", "olio di oliva", "olio di semi", "seed oil", "burro", "butter", "strutto", "lard", "margarina", "margarine")),
    (CulinaryTag.EGGS, ("uova", "eggs", "uovo", "egg", "tuorlo", "albume", "egg white")),
    (CulinaryTag.HERBS, ("basilico", "basil", "origano", "oregano", "rosmarino", "rosemary", "prezzemolo", "parsley", "timo", "thyme", "salvia", "sage")),
]

CANONICAL_RESOLVER: List[Tuple[str, Tuple[str, ...]]] = [
    ("pasta", ("spaghetti", "penne", "fusilli", "rigatoni", "farfalle", "soup_pasta", "long pasta", "pasta")),
    ("rice", ("carnaroli", "basmati", "riso", "rice")),
    ("tomato passata", ("passata", "puree", "tomato puree", "tomato passata", "pomodoro")),
    ("evo oil", ("extra virgin olive oil", "evo oil", "olio evo", "olio", "oil")),
    ("parmigiano reggiano", ("parmigiano", "parmigiano reggiano", "parmesan", "grana", "pecorino")),
    ("chicken", ("chicken breast", "petto di pollo", "pollo", "chicken")),
    ("beef", ("manzo", "beef", "vitello", "veal")),
    ("tuna", ("tonno", "tuna")), ("salmon", ("salmone", "salmon")),
    ("eggs", ("uova", "eggs", "uovo", "egg")),
    ("milk", ("whole milk", "latte intero", "latte", "milk")),
    ("yogurt", ("plain yogurt", "yogurt", "skyr", "kefir")),
    ("apple", ("apples", "apple", "mela", "mele")), ("banana", ("banane", "banana")),
    ("berries", ("frutti di bosco", "berries", "mirtilli", "mirtillo", "fragole", "fragola", "strawberry", "strawberries", "blueberry", "blueberries")),
    ("biscotti", ("biscotti", "biscuit", "cookies")),
    ("oats", ("avena", "oats", "oatmeal", "porridge")),
    ("honey", ("miele", "honey")), ("chocolate", ("cioccolato", "chocolate", "cacao")),
    ("basil", ("basilico", "basil")), ("garlic", ("aglio", "garlic")),
    ("onion", ("cipolla", "onion", "scalogno")), ("oregano", ("origano", "oregano")),
    ("rosemary", ("rosmarino", "rosemary")),
    ("zucchini", ("zucchine", "zucchini", "zucchina", "courgette")),
    ("potatoes", ("patate", "potatoes", "potato")),
    ("almonds", ("mandorle", "almonds", "almond")), ("walnuts", ("noci", "walnuts", "walnut")),
    ("flour", ("00 flour", "farina", "flour")),
]

@functools.lru_cache(maxsize=1024)
def _resolve_canonical_str(name: str) -> str:
    clean = _norm_pairing_text(name)
    for canon, patterns in CANONICAL_RESOLVER:
        if any(re.search(r"\b" + re.escape(p) + r"\b", clean) for p in patterns):
            return canon
    return clean

def resolve_canonical(name: Any) -> str:
    return _resolve_canonical_str(name) if (name and isinstance(name, str)) else ""

@functools.lru_cache(maxsize=1024)
def _base_tags_for_name(name: str) -> frozenset:
    clean = _norm_pairing_text(name)
    return frozenset(tag for tag, pats in TAG_KEYWORDS if any(re.search(r"\b" + re.escape(p) + r"\b", clean) for p in pats))

def get_culinary_tags(name: Any, roles: Optional[Iterable[Any]] = None) -> Set[CulinaryTag]:
    if not name or not isinstance(name, str):
        return set()
    tags: Set[CulinaryTag] = set(_base_tags_for_name(name))
    role_seq: Iterable[Any] = (roles,) if isinstance(roles, Role) else (
        roles if (roles is not None and isinstance(roles, Iterable) and not isinstance(roles, (str, bytes, Mapping))) else ()
    )
    if coerced := {c for r in role_seq if (c := _coerce_role(r)) is not None}:
        if Role.FRUIT in coerced:
            tags.add(CulinaryTag.SWEET_FRUIT)
        if Role.FAT_SEASONING in coerced and Role.NUTS_OR_SWEET not in coerced and not tags:
            tags.add(CulinaryTag.SAVORY_FAT)
        if Role.DAIRY_BASE in coerced and not tags:
            tags.add(CulinaryTag.SWEET_DAIRY)
    return tags

HARD_EXCLUSION_PAIRS: Set[Tuple[CulinaryTag, CulinaryTag]] = set(itertools.product(
    (CulinaryTag.SEAFOOD, CulinaryTag.PASTA_GRAIN, CulinaryTag.SAVORY_MEAT, CulinaryTag.SAVORY_SAUCE, CulinaryTag.PUNGENT_AROMA, CulinaryTag.SAVORY_FAT),
    (CulinaryTag.SWEET_DAIRY, CulinaryTag.CHOCOLATE_SWEET, CulinaryTag.SWEET_FRUIT),
)) | {(CulinaryTag.RAW_FLOUR, CulinaryTag.SWEET_FRUIT), (CulinaryTag.RAW_FLOUR, CulinaryTag.SWEET_DAIRY)}

_TAG_ROLE_INFERENCE: Tuple[Tuple[Tuple[CulinaryTag, ...], Tuple[Role, ...]], ...] = (
    ((CulinaryTag.SAVORY_FAT,), (Role.FAT_SEASONING,)), ((CulinaryTag.SAVORY_MEAT, CulinaryTag.SEAFOOD), (Role.PROTEIN_MAIN,)),
    ((CulinaryTag.PASTA_GRAIN,), (Role.CARB_BASE,)), ((CulinaryTag.SAVORY_SAUCE,), (Role.VEG_SAUCE, Role.VEGETABLE)),
    ((CulinaryTag.SWEET_FRUIT,), (Role.FRUIT,)), ((CulinaryTag.SWEET_DAIRY,), (Role.DAIRY_BASE,)),
    ((CulinaryTag.HARD_CHEESE,), (Role.AROMA_TASTE, Role.AROMA_FINISHING)),
    ((CulinaryTag.PUNGENT_AROMA, CulinaryTag.HERBS), (Role.AROMA_HERB, Role.AROMA_TASTE)),
)

@functools.lru_cache(maxsize=1024)
def _infer_roles_from_name(item_name: str) -> frozenset:
    result: Set[Role] = set(_tier_a_lookup(item_name.strip().lower()) or ())
    if (canon := resolve_canonical(item_name)) == "zucchini":
        result.update((Role.VEGETABLE, Role.VEG_SIDE))
    elif canon == "potatoes":
        result.update((Role.CARB_SIDE, Role.CARB_BASE))
    tags = get_culinary_tags(item_name)
    for match_tags, add_roles in _TAG_ROLE_INFERENCE:
        if any(t in tags for t in match_tags):
            result.update(add_roles)
    return frozenset(result)

def _coerce_roles(item_name: str, roles: Optional[Any]) -> Set[Role]:
    result: Set[Role] = set()
    if roles is not None:
        seq = roles if isinstance(roles, (set, list, tuple)) else ((roles,) if isinstance(roles, (Role, str)) else ())
        result.update(c for r in seq if (c := _coerce_role(r)) is not None)
    if not result and item_name and isinstance(item_name, str):
        result.update(_infer_roles_from_name(item_name))
    return result

def are_mutually_exclusive(item_a: str, roles_a: Optional[Any] = None, item_b: str = "", roles_b: Optional[Any] = None) -> bool:
    if not item_a or not item_b or not isinstance(item_a, str) or not isinstance(item_b, str):
        return False
    canon_a, canon_b = resolve_canonical(item_a), resolve_canonical(item_b)
    if canon_a and canon_b and canon_a == canon_b:
        return False
    ca, cb = _coerce_roles(item_a, roles_a), _coerce_roles(item_b, roles_b)
    ta_set, tb_set = get_culinary_tags(item_a, ca), get_culinary_tags(item_b, cb)
    oats_ok = {CulinaryTag.SWEET_DAIRY, CulinaryTag.SWEET_FRUIT, CulinaryTag.CHOCOLATE_SWEET}
    for ta, tb in itertools.product(ta_set, tb_set):
        if ((ta, tb) in HARD_EXCLUSION_PAIRS or (tb, ta) in HARD_EXCLUSION_PAIRS) and not (
            (canon_a == "oats" and tb in oats_ok) or (canon_b == "oats" and ta in oats_ok)
        ):
            return True
    if (Role.FRUIT in ca and Role.CARB_BASE in cb and canon_b != "oats") or (Role.FRUIT in cb and Role.CARB_BASE in ca and canon_a != "oats"):
        return True
    if (Role.FRUIT in ca and Role.VEG_SAUCE in cb) or (Role.FRUIT in cb and Role.VEG_SAUCE in ca):
        return True
    savory_prot = (CulinaryTag.SAVORY_MEAT, CulinaryTag.SEAFOOD)
    return bool((Role.DAIRY_BASE in ca and Role.PROTEIN_MAIN in cb and any(t in tb_set for t in savory_prot)) or
                (Role.DAIRY_BASE in cb and Role.PROTEIN_MAIN in ca and any(t in ta_set for t in savory_prot)))

_AFFINITY_DATA: Dict[Tuple[str, str], float] = {
    ("basil", "tomato passata"): 0.98, ("evo oil", "tomato passata"): 0.95, ("pasta", "tomato passata"): 0.95,
    ("milk", "oats"): 0.95, ("honey", "yogurt"): 0.95, ("banana", "chocolate"): 0.95,
    ("basil", "evo oil"): 0.95, ("evo oil", "pasta"): 0.90, ("garlic", "pasta"): 0.88,
    ("parmigiano reggiano", "pasta"): 0.92, ("eggs", "pasta"): 0.90, ("parmigiano reggiano", "tomato passata"): 0.92,
    ("garlic", "tomato passata"): 0.92, ("oregano", "tomato passata"): 0.92, ("chicken", "rice"): 0.92,
    ("chicken", "rosemary"): 0.92, ("chicken", "evo oil"): 0.88, ("chicken", "zucchini"): 0.85,
    ("chicken", "potatoes"): 0.90, ("beef", "rosemary"): 0.92, ("beef", "potatoes"): 0.90,
    ("beef", "tomato passata"): 0.90, ("tuna", "tomato passata"): 0.88, ("tuna", "onion"): 0.85,
    ("salmon", "zucchini"): 0.85, ("eggs", "tomato passata"): 0.85, ("eggs", "parmigiano reggiano"): 0.88,
    ("biscotti", "milk"): 0.92, ("apple", "yogurt"): 0.90, ("berries", "yogurt"): 0.95,
    ("banana", "yogurt"): 0.90, ("oats", "yogurt"): 0.92, ("apple", "oats"): 0.88,
    ("honey", "oats"): 0.92, ("honey", "walnuts"): 0.90, ("apple", "walnuts"): 0.88,
    ("apple", "honey"): 0.88, ("oats", "walnuts"): 0.88, ("evo oil", "rosemary"): 0.88,
    ("potatoes", "rosemary"): 0.90, ("evo oil", "potatoes"): 0.85, ("almonds", "yogurt"): 0.90,
    ("walnuts", "yogurt"): 0.90, ("chicken", "pasta"): 0.65, ("parmigiano reggiano", "tuna"): 0.20,
    ("evo oil", "parmigiano reggiano"): 0.90, ("evo oil", "rice"): 0.88, ("garlic", "evo oil"): 0.92, ("onion", "evo oil"): 0.90,
}
AFFINITY_MATRIX: Dict[Tuple[str, str], float] = {(min(a, b), max(a, b)): s for (a, b), s in _AFFINITY_DATA.items()}
_BREAKFAST_ROLES = {Role.CEREAL_CARB, Role.FRUIT, Role.NUTS_OR_SWEET}
_SAVORY_ROLES = {Role.CARB_BASE, Role.PROTEIN_MAIN, Role.VEGETABLE, Role.VEG_SAUCE, Role.VEG_SIDE}
_TIER3_RULES: Tuple[Tuple[Set[Role], Set[Role], float], ...] = (
    ({Role.DAIRY_BASE}, _BREAKFAST_ROLES, 0.80), (_BREAKFAST_ROLES, _BREAKFAST_ROLES, 0.80),
    ({Role.FAT_SEASONING}, _SAVORY_ROLES, 0.70), ({Role.AROMA_TASTE, Role.AROMA_HERB, Role.AROMA_FINISHING}, _SAVORY_ROLES, 0.75),
    ({Role.VEGETABLE, Role.VEG_SAUCE, Role.VEG_SIDE}, {Role.CARB_BASE, Role.PROTEIN_MAIN}, 0.70),
)

def get_pairing_score(item_a: str, item_b: str, roles_a: Optional[Any] = None, roles_b: Optional[Any] = None) -> float:
    if not item_a or not item_b or not isinstance(item_a, str) or not isinstance(item_b, str):
        return 0.0
    canon_a, canon_b = resolve_canonical(item_a), resolve_canonical(item_b)
    if not canon_a or not canon_b:
        return 0.0
    if canon_a == canon_b:
        return 1.0
    if are_mutually_exclusive(item_a, roles_a, item_b, roles_b):
        return 0.0
    if (score := AFFINITY_MATRIX.get((min(canon_a, canon_b), max(canon_a, canon_b)))) is not None:
        return score
    ca, cb = _coerce_roles(item_a, roles_a), _coerce_roles(item_b, roles_b)
    if Role.CARB_BASE in ca and Role.CARB_BASE in cb:
        return 0.30
    if Role.PROTEIN_MAIN in ca and Role.PROTEIN_MAIN in cb:
        return 0.40
    return next((val for s1, s2, val in _TIER3_RULES if ((ca & s1) and (cb & s2)) or ((cb & s1) and (ca & s2))), 0.50)

def _extract_pairing_item(it: Any) -> Optional[Tuple[str, Optional[List[Any]]]]:
    if it is None:
        return None
    if isinstance(it, str):
        return (s, None) if (s := it.strip()) else None
    is_obj = hasattr(it, "name") and isinstance(it.name, str) and it.name.strip()
    is_map = isinstance(it, dict) and isinstance(it.get("name"), str) and it["name"].strip()
    if not is_obj and not is_map:
        return None
    roles: List[Any] = []
    for key in ("all_eligible_roles", "roles", "role"):
        if (val := getattr(it, key, None) if is_obj else it.get(key)) is not None:
            for elem in (val if isinstance(val, (list, tuple, set)) else (val,)):
                if elem not in roles:
                    roles.append(elem)
    return (it.name.strip() if is_obj else it["name"].strip(), roles or None)

def score_combination(items: Sequence[Any]) -> float:
    if not items or not isinstance(items, Iterable) or isinstance(items, (str, bytes, Mapping)):
        return 0.0
    clean = [pair for it in items if (pair := _extract_pairing_item(it)) is not None]
    if len(clean) <= 1:
        return float(len(clean))
    total_score, pairs_count = 0.0, 0
    for (ni, ri), (nj, rj) in itertools.combinations(clean, 2):
        if are_mutually_exclusive(ni, ri, nj, rj) or (score := get_pairing_score(ni, nj, ri, rj)) <= 0.0:
            return 0.0
        total_score += score
        pairs_count += 1
    return 1.0 if pairs_count == 0 else round(total_score / pairs_count, 3)

# --- 4. Core Generator Dataclasses ---

@dataclass
class IngredientCandidate:
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
    _alias_snapshot: Optional[Dict[str, Tuple[Any, Any]]] = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if isinstance(self.role, str) and not isinstance(self.role, Role) and (c := _coerce_role(self.role)) is not None:
            self.role = c
        snap = self._alias_snapshot
        new_snap: Dict[str, Tuple[Any, Any]] = {}
        for canon, alias, nullable in (
            ("portion_min_g", "min_g", False),
            ("portion_max_g", "max_g", False),
            ("available_stock_g", "available_g", False),
            ("days_to_expiration", "days_to_expiry", True),
        ):
            cur_c, cur_a = getattr(self, canon), getattr(self, alias)
            if isinstance(snap, dict) and canon in snap:
                prev_c, prev_a = snap[canon]
                if cur_a != prev_a and cur_c == prev_c:
                    raw = cur_a
                else:
                    raw = cur_c
            else:
                raw = cur_a if cur_a is not None else cur_c
            if nullable:
                val: Optional[float] = float(raw) if raw is not None else None
            else:
                val = float(raw) if raw is not None else 0.0
            setattr(self, canon, val)
            setattr(self, alias, val)
            new_snap[canon] = (val, val)
        self._alias_snapshot = new_snap
        if self.open_remainder > 0:
            self.is_open = True
        if not self.all_eligible_roles:
            self.all_eligible_roles = [self.role] if isinstance(self.role, Role) else []

    def _clean_unit(self) -> str:
        return self.unit.strip().lower() if isinstance(self.unit, str) and self.unit.strip() else "g"

    def _safe_density(self) -> float:
        d = _parse_float(self.density_kg_per_l)
        return d if (d is not None and math.isfinite(d) and d > 0.0) else 1.0

    def _safe_gpp(self) -> float:
        g = _parse_float(self.grams_per_piece)
        return g if (g is not None and math.isfinite(g) and g > 0.0) else 50.0

    def calculate_display_quantity(self) -> Tuple[float, str]:
        raw_g = self.resolved_quantity_g if self.resolved_quantity_g > 0 else (self.portion_min_g or 1.0)
        u, density = self._clean_unit(), self._safe_density()
        if u == "kg":
            return max(0.001, round(raw_g / 1000.0, 3)), "kg"
        if u == "g":
            return max(0.1, round(raw_g, 1)), "g"
        if u in ("pcs", "pz"):
            return max(1.0, float(round(raw_g / self._safe_gpp()))), "pcs"
        if u in ("l", "liter"):
            return max(0.001, round(raw_g / (1000.0 * density), 3)), "L"
        if u in ("ml", "cl"):
            return max(0.1, round(raw_g / density, 1)), "ml"
        if u == "tbsp":
            return max(0.1, round(raw_g / (15.0 * density), 1)), "tbsp"
        if u == "tsp":
            return max(0.1, round(raw_g / (5.0 * density), 1)), "tsp"
        return max(0.1, round(raw_g, 1)), (self.unit.strip() if isinstance(self.unit, str) and self.unit.strip() else "g")

    def quantized_grams(self) -> float:
        qty, unit = self.calculate_display_quantity()
        density, gpp = self._safe_density(), self._safe_gpp()
        factors = {
            "kg": 1000.0, "g": 1.0, "pcs": gpp, "pz": gpp,
            "l": 1000.0 * density, "liter": 1000.0 * density,
            "ml": density, "cl": 10.0 * density,
            "tbsp": 15.0 * density, "tsp": 5.0 * density,
        }
        return round(qty * factors.get(unit.strip().lower(), 1.0), 2)

    def compute_macros_for_quantity(self, grams: Optional[float] = None) -> Dict[str, float]:
        factor = max(0.0, self.resolved_quantity_g if grams is None else grams) / 100.0
        return {k: round(getattr(self, attr) * factor, 1) for k, attr in (
            ("Kcal", "kcal_100g"), ("Protein", "protein_100g"), ("Carbs", "carbs_100g"),
            ("Fat", "fat_100g"), ("Fiber", "fiber_100g"), ("Sugars", "sugars_100g"),
        )}

    def to_recipe_ingredient_dict(self) -> Dict[str, Any]:
        qty, unit = self.calculate_display_quantity()
        return {"product": self.name, "quantity": qty, "unit": unit, "to_taste": self.to_taste}

@dataclass
class SolverResult:
    success: bool
    quantities_g: Dict[int, float] = field(default_factory=dict)
    total_kcal: float = 0.0
    total_protein: float = 0.0
    total_carbs: float = 0.0
    total_fat: float = 0.0
    total_fiber: float = 0.0
    solver_name: str = "highs"
    message: str = ""
    iterations: int = 0
    quantities: Dict[str, float] = field(default_factory=dict)
    quantities_by_idx: List[float] = field(default_factory=list)

@dataclass
class RecipeCandidate:
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

    def _update_caloric_delta(self) -> None:
        if self.target_kcal > 0 and math.isfinite(self.target_kcal) and self.total_kcal >= 0 and math.isfinite(self.total_kcal):
            self.caloric_delta_pct = round(abs(self.total_kcal - self.target_kcal) / self.target_kcal * 100.0, 2)

    def __post_init__(self) -> None:
        if not self.notes and self.instructions:
            self.notes = "\n".join(f"{i + 1}. {step}" for i, step in enumerate(self.instructions))
        self._update_caloric_delta()

    def sync_macros_with_display_quantities(self) -> None:
        if not self.ingredients:
            return
        tot_kcal = tot_p = tot_c = tot_f = tot_fib = 0.0
        for ing in self.ingredients:
            qg = ing.quantized_grams()
            ing.resolved_quantity_g = qg
            factor = max(0.0, qg) / 100.0
            tot_kcal += ing.kcal_100g * factor
            tot_p += ing.protein_100g * factor
            tot_c += ing.carbs_100g * factor
            tot_f += ing.fat_100g * factor
            tot_fib += ing.fiber_100g * factor
        self.total_kcal, self.total_protein = round(tot_kcal, 1), round(tot_p, 1)
        self.total_carbs, self.total_fat, self.total_fiber = round(tot_c, 1), round(tot_f, 1), round(tot_fib, 1)
        self._update_caloric_delta()

    def to_create_recipe_kwargs(self) -> Dict[str, Any]:
        return {"name": self.title, "meals": [self.meal_type], "ingredients": [ing.to_recipe_ingredient_dict() for ing in self.ingredients], "notes": self.notes}
