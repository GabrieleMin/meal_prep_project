"""meal_planner.generator.slots

Culinary archetypes, slot definitions, cardinalities, and portion bounds.
Defines 4 core archetypes:
1. Grain / Pasta Dish (Primi Piatti)
2. Main & Side Plate (Secondi con Contorno)
3. One-Pot Balanced Bowl (Piatto Unico / Fitness Bowl)
4. Breakfast / Snack Bowl (Colazioni e Spuntini)
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple, Union

from meal_planner.generator.taxonomy import (
    DEFAULT_PORTION_BOUNDS,
    Role,
    ROLE_COMPATIBILITY,
    are_roles_compatible,
)


# Default fallback portions per role if not specified
DEFAULT_ROLE_PORTIONS: Dict[Role, Tuple[float, float]] = DEFAULT_PORTION_BOUNDS


@dataclass(frozen=True)
class Slot:
    """Represents a single grammatical slot within an archetype.

    Attributes:
        role: Primary Role required for this slot.
        cardinality_min: Minimum number of ingredients required (>= 0).
        cardinality_max: Maximum number of ingredients allowed (>= cardinality_min).
        is_optional: True if this slot can be omitted (cardinality_min == 0).
        portion_min_g: Minimum physiological portion in grams per ingredient.
        portion_max_g: Maximum physiological portion in grams per ingredient.
        name: Human-readable display label.
        compatible_roles: Roles accepted by this slot (defaults from ROLE_COMPATIBILITY).
    """

    role: Role
    cardinality_min: int = 1
    cardinality_max: int = 1
    is_optional: bool = False
    portion_min_g: float = 0.0
    portion_max_g: float = 0.0
    name: str = ""
    compatible_roles: Tuple[Role, ...] = field(default=())

    def __post_init__(self) -> None:
        if isinstance(self.role, str) and not isinstance(self.role, Role):
            object.__setattr__(self, "role", Role(self.role))
        if self.cardinality_min < 0:
            raise ValueError(f"cardinality_min must be >= 0, got {self.cardinality_min}")
        if self.cardinality_max < self.cardinality_min:
            raise ValueError(
                f"cardinality_max ({self.cardinality_max}) must be >= cardinality_min ({self.cardinality_min})"
            )
        if self.portion_min_g < 0:
            raise ValueError(f"portion_min_g must be >= 0, got {self.portion_min_g}")
        if self.portion_max_g < self.portion_min_g:
            raise ValueError(
                f"portion_max_g ({self.portion_max_g}) must be >= portion_min_g ({self.portion_min_g})"
            )

        # Set default name
        if not self.name:
            role_val = self.role.value if hasattr(self.role, "value") else str(self.role)
            object.__setattr__(self, "name", role_val.replace("_", " ").title())

        # Set default compatible roles
        if not self.compatible_roles:
            compat = ROLE_COMPATIBILITY.get(self.role, {self.role})
            object.__setattr__(self, "compatible_roles", tuple(compat))

    @property
    def bounds(self) -> Tuple[float, float]:
        """Returns (portion_min_g, portion_max_g)."""
        return (self.portion_min_g, self.portion_max_g)

    def accepts_role(self, role: Union[Role, str]) -> bool:
        """Returns True if this slot accepts the specified role."""
        if isinstance(role, str):
            try:
                role = Role(role)
            except ValueError:
                pass
        return role == self.role or role in self.compatible_roles

    def validate_cardinality(self, count: int) -> bool:
        """Returns True if the item count satisfies slot cardinality."""
        return self.cardinality_min <= count <= self.cardinality_max

    def validate_portion(self, grams: Any) -> bool:
        """Returns True if the portion in grams falls within bounds."""
        if isinstance(grams, bool) or grams is None:
            return False
        try:
            val = float(grams)
            if math.isnan(val):
                return False
            return self.portion_min_g <= val <= self.portion_max_g
        except (ValueError, TypeError):
            return False

    def clamp_portion(self, grams: Any) -> float:
        """Clamps portion weight to [portion_min_g, portion_max_g]."""
        if isinstance(grams, bool) or grams is None:
            val = self.portion_min_g
        else:
            try:
                val = float(grams)
                if math.isnan(val):
                    val = self.portion_min_g
            except (ValueError, TypeError):
                val = self.portion_min_g
        return max(self.portion_min_g, min(val, self.portion_max_g))


class SlotsDict(dict):
    """Dictionary mapping Role -> int, providing sequence and key-based access."""

    def __init__(self, slots: Sequence[Slot]):
        super().__init__({s.role: s.cardinality_max for s in slots})
        self._slots = tuple(slots)

    @property
    def slots(self) -> Tuple[Slot, ...]:
        return self._slots

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, str) and not isinstance(key, Role):
            try:
                key = Role(key)
            except ValueError:
                pass
        return super().__getitem__(key)

    def __iter__(self):
        return super().__iter__()

    def __len__(self) -> int:
        return len(self._slots)


@dataclass
class Archetype:
    """Represents a structured culinary meal archetype.

    Attributes:
        name: Canonical name of the archetype.
        slots: Sequence of Slot definitions making up the archetype.
        default_meal: Primary default meal type ('lunch', 'dinner', 'breakfast').
        allowed_meals: Tuple of allowed meal types.
        description: Informative description of the archetype style.
        required_slots: Mapping of required Role to max cardinality (SlotsDict).
        optional_slots: Mapping of optional Role to max cardinality (SlotsDict).
    """

    name: str
    slots: Tuple[Slot, ...] = field(default_factory=tuple)
    default_meal: str = "lunch"
    allowed_meals: Tuple[str, ...] = ("lunch", "dinner")
    description: str = ""
    required_slots: SlotsDict = field(init=False)
    optional_slots: SlotsDict = field(init=False)

    def __init__(
        self,
        name: str,
        slots: Optional[Sequence[Slot]] = None,
        required_slots: Optional[Dict[Role, int]] = None,
        optional_slots: Optional[Dict[Role, int]] = None,
        default_meal: str = "lunch",
        allowed_meals: Optional[Sequence[str]] = None,
        description: str = "",
    ):
        self.name = name
        self.default_meal = (default_meal or "lunch").lower()
        self.description = description or ""

        if slots is not None:
            self.slots = tuple(slots)
        elif required_slots is not None or optional_slots is not None:
            constructed: List[Slot] = []
            if required_slots:
                for role, count in required_slots.items():
                    r = Role(role) if isinstance(role, str) else role
                    p_min, p_max = DEFAULT_ROLE_PORTIONS.get(r, (50.0, 200.0))
                    constructed.append(
                        Slot(
                            role=r,
                            cardinality_min=1,
                            cardinality_max=count,
                            is_optional=False,
                            portion_min_g=p_min,
                            portion_max_g=p_max,
                        )
                    )
            if optional_slots:
                for role, count in optional_slots.items():
                    r = Role(role) if isinstance(role, str) else role
                    p_min, p_max = DEFAULT_ROLE_PORTIONS.get(r, (10.0, 100.0))
                    constructed.append(
                        Slot(
                            role=r,
                            cardinality_min=0,
                            cardinality_max=count,
                            is_optional=True,
                            portion_min_g=p_min,
                            portion_max_g=p_max,
                        )
                    )
            self.slots = tuple(constructed)
        else:
            self.slots = ()

        self.required_slots = SlotsDict([s for s in self.slots if not s.is_optional])
        self.optional_slots = SlotsDict([s for s in self.slots if s.is_optional])

        if allowed_meals is not None:
            self.allowed_meals = tuple(m.lower() for m in allowed_meals)
        else:
            if self.default_meal == "breakfast":
                self.allowed_meals = ("breakfast",)
            else:
                self.allowed_meals = ("lunch", "dinner")

    @property
    def all_roles(self) -> Set[Role]:
        """Returns set of all roles defined in this archetype's slots."""
        return {s.role for s in self.slots}

    def get_slot(self, role: Union[Role, str]) -> Optional[Slot]:
        """Finds the slot matching the given role or accepting it, or None if not found."""
        try:
            role_enum = Role(role) if isinstance(role, str) else role
        except ValueError:
            role_enum = None

        # First pass: exact role match
        for s in self.slots:
            if s.role == role_enum:
                return s

        # Second pass: compatible role match
        if role_enum is not None:
            for s in self.slots:
                if s.accepts_role(role_enum):
                    return s

        return None

    def is_eligible_for_meal(self, meal: str) -> bool:
        """Returns True if this archetype can be prepared for the specified meal."""
        if not isinstance(meal, str):
            return False
        return meal.strip().lower() in [m.lower() for m in self.allowed_meals]


# ---------------------------------------------------------------------------
# Canonical Archetypes
# ---------------------------------------------------------------------------

GRAIN_PASTA_DISH = Archetype(
    name="Grain / Pasta Dish",
    slots=(
        Slot(
            role=Role.CARB_BASE,
            cardinality_min=1,
            cardinality_max=1,
            is_optional=False,
            portion_min_g=60.0,
            portion_max_g=120.0,
            name="Carbohydrate Base",
        ),
        Slot(
            role=Role.FAT_SEASONING,
            cardinality_min=1,
            cardinality_max=1,
            is_optional=False,
            portion_min_g=5.0,
            portion_max_g=20.0,
            name="Fat Seasoning",
        ),
        Slot(
            role=Role.VEG_SAUCE,
            cardinality_min=0,
            cardinality_max=2,
            is_optional=True,
            portion_min_g=50.0,
            portion_max_g=200.0,
            name="Vegetable Sauce",
        ),
        Slot(
            role=Role.PROTEIN_ACCENT,
            cardinality_min=0,
            cardinality_max=1,
            is_optional=True,
            portion_min_g=40.0,
            portion_max_g=120.0,
            name="Protein Accent",
        ),
        Slot(
            role=Role.AROMA_FINISHING,
            cardinality_min=0,
            cardinality_max=2,
            is_optional=True,
            portion_min_g=5.0,
            portion_max_g=15.0,
            name="Aroma Finishing",
        ),
    ),
    default_meal="lunch",
    allowed_meals=("lunch", "dinner"),
    description="Primi piatti tradizionali a base di pasta, riso o cereali con condimenti, salse di verdure e guarnizioni aromatiche.",
)

MAIN_AND_SIDE_PLATE = Archetype(
    name="Main & Side Plate",
    slots=(
        Slot(
            role=Role.PROTEIN_MAIN,
            cardinality_min=1,
            cardinality_max=1,
            is_optional=False,
            portion_min_g=100.0,
            portion_max_g=250.0,
            name="Protein Main",
        ),
        Slot(
            role=Role.VEG_SIDE,
            cardinality_min=1,
            cardinality_max=2,
            is_optional=False,
            portion_min_g=120.0,
            portion_max_g=350.0,
            name="Vegetable Side",
        ),
        Slot(
            role=Role.FAT_SEASONING,
            cardinality_min=1,
            cardinality_max=1,
            is_optional=False,
            portion_min_g=5.0,
            portion_max_g=20.0,
            name="Fat Seasoning",
        ),
        Slot(
            role=Role.CARB_SIDE,
            cardinality_min=0,
            cardinality_max=1,
            is_optional=True,
            portion_min_g=40.0,
            portion_max_g=100.0,
            name="Carbohydrate Side",
        ),
        Slot(
            role=Role.AROMA_HERB,
            cardinality_min=0,
            cardinality_max=1,
            is_optional=True,
            portion_min_g=1.0,
            portion_max_g=5.0,
            name="Aroma Herb",
        ),
    ),
    default_meal="dinner",
    allowed_meals=("lunch", "dinner"),
    description="Secondi piatti con pietanza proteica centrale, ricco contorno di verdure e condimento aromatico.",
)

ONE_POT_BALANCED_BOWL = Archetype(
    name="One-Pot Balanced Bowl",
    slots=(
        Slot(
            role=Role.CARB_BASE,
            cardinality_min=1,
            cardinality_max=1,
            is_optional=False,
            portion_min_g=50.0,
            portion_max_g=90.0,
            name="Carbohydrate Base",
        ),
        Slot(
            role=Role.PROTEIN_MAIN,
            cardinality_min=1,
            cardinality_max=1,
            is_optional=False,
            portion_min_g=80.0,
            portion_max_g=180.0,
            name="Protein Main",
        ),
        Slot(
            role=Role.VEGETABLE,
            cardinality_min=1,
            cardinality_max=1,
            is_optional=False,
            portion_min_g=100.0,
            portion_max_g=250.0,
            name="Vegetable",
        ),
        Slot(
            role=Role.FAT_SEASONING,
            cardinality_min=1,
            cardinality_max=1,
            is_optional=False,
            portion_min_g=5.0,
            portion_max_g=15.0,
            name="Fat Seasoning",
        ),
        Slot(
            role=Role.AROMA_TASTE,
            cardinality_min=0,
            cardinality_max=1,
            is_optional=True,
            portion_min_g=5.0,
            portion_max_g=15.0,
            name="Aroma Taste",
        ),
    ),
    default_meal="lunch",
    allowed_meals=("lunch", "dinner"),
    description="Piatto unico bilanciato / fitness bowl con base di cereali, porzione proteica, verdura fresca e grassi buoni.",
)

BREAKFAST_SNACK_BOWL = Archetype(
    name="Breakfast / Snack Bowl",
    slots=(
        Slot(
            role=Role.DAIRY_BASE,
            cardinality_min=1,
            cardinality_max=1,
            is_optional=False,
            portion_min_g=100.0,
            portion_max_g=250.0,
            name="Dairy Base",
        ),
        Slot(
            role=Role.CEREAL_CARB,
            cardinality_min=1,
            cardinality_max=1,
            is_optional=False,
            portion_min_g=30.0,
            portion_max_g=80.0,
            name="Cereal Carb",
        ),
        Slot(
            role=Role.FRUIT,
            cardinality_min=0,
            cardinality_max=1,
            is_optional=True,
            portion_min_g=50.0,
            portion_max_g=150.0,
            name="Fruit",
        ),
        Slot(
            role=Role.NUTS_OR_SWEET,
            cardinality_min=0,
            cardinality_max=1,
            is_optional=True,
            portion_min_g=10.0,
            portion_max_g=25.0,
            name="Nuts or Sweet",
        ),
    ),
    default_meal="breakfast",
    allowed_meals=("breakfast",),
    description="Ciotola nutriente per colazione o spuntino a base di latte o yogurt, cereali integrali, frutta fresca e frutta secca.",
)

ARCHETYPES: Dict[str, Archetype] = {
    "Grain / Pasta Dish": GRAIN_PASTA_DISH,
    "Main & Side Plate": MAIN_AND_SIDE_PLATE,
    "One-Pot Balanced Bowl": ONE_POT_BALANCED_BOWL,
    "Breakfast / Snack Bowl": BREAKFAST_SNACK_BOWL,
}

ARCHETYPE_ALIASES: Dict[str, str] = {
    # Grain / Pasta
    "grain / pasta dish": "Grain / Pasta Dish",
    "grain and pasta dish": "Grain / Pasta Dish",
    "grain": "Grain / Pasta Dish",
    "pasta": "Grain / Pasta Dish",
    "pasta dish": "Grain / Pasta Dish",
    "primi": "Grain / Pasta Dish",
    "primo": "Grain / Pasta Dish",
    "primi piatti": "Grain / Pasta Dish",
    "risotto": "Grain / Pasta Dish",

    # Main & Side
    "main & side plate": "Main & Side Plate",
    "main and side plate": "Main & Side Plate",
    "main & side": "Main & Side Plate",
    "main_side": "Main & Side Plate",
    "plate": "Main & Side Plate",
    "secondi": "Main & Side Plate",
    "secondo": "Main & Side Plate",
    "secondi piatti": "Main & Side Plate",

    # One-Pot Balanced Bowl
    "one-pot balanced bowl": "One-Pot Balanced Bowl",
    "one pot balanced bowl": "One-Pot Balanced Bowl",
    "one_pot": "One-Pot Balanced Bowl",
    "bowl": "One-Pot Balanced Bowl",
    "power bowl": "One-Pot Balanced Bowl",
    "fitness bowl": "One-Pot Balanced Bowl",
    "piatto unico": "One-Pot Balanced Bowl",
    "piatto_unico": "One-Pot Balanced Bowl",

    # Breakfast / Snack
    "breakfast / snack bowl": "Breakfast / Snack Bowl",
    "breakfast and snack bowl": "Breakfast / Snack Bowl",
    "breakfast": "Breakfast / Snack Bowl",
    "breakfast bowl": "Breakfast / Snack Bowl",
    "colazione": "Breakfast / Snack Bowl",
    "snack": "Breakfast / Snack Bowl",
    "spuntino": "Breakfast / Snack Bowl",
    "yogurt bowl": "Breakfast / Snack Bowl",
}

MEAL_ARCHETYPES: Dict[str, Tuple[Archetype, ...]] = {
    "breakfast": (BREAKFAST_SNACK_BOWL,),
    "lunch": (GRAIN_PASTA_DISH, ONE_POT_BALANCED_BOWL, MAIN_AND_SIDE_PLATE),
    "dinner": (MAIN_AND_SIDE_PLATE, ONE_POT_BALANCED_BOWL, GRAIN_PASTA_DISH),
}


# ---------------------------------------------------------------------------
# Retrieval & Query Helpers
# ---------------------------------------------------------------------------

def get_archetype(name: str) -> Archetype:
    """Retrieves an archetype by name or alias (case-insensitive)."""
    if not isinstance(name, str):
        if name is None:
            raise ValueError("Archetype name cannot be empty.")
        raise ValueError(
            f"Unknown archetype '{name}'. Archetype name must be a string, got {type(name).__name__}. "
            f"Valid archetypes: {list(ARCHETYPES.keys())}"
        )

    clean = name.strip().lower()
    if not clean:
        raise ValueError("Archetype name cannot be empty.")

    # 1. Alias lookup
    canonical = ARCHETYPE_ALIASES.get(clean)
    if canonical and canonical in ARCHETYPES:
        return ARCHETYPES[canonical]

    # 2. Case-insensitive key lookup
    for key, arch in ARCHETYPES.items():
        if key.lower() == clean:
            return arch

    raise ValueError(
        f"Unknown archetype '{name}'. Valid archetypes: {list(ARCHETYPES.keys())}"
    )


def get_archetypes_for_meal(meal: str) -> List[Archetype]:
    """Returns the ordered list of archetypes suitable for the given meal type."""
    if not isinstance(meal, str):
        clean = ""
    else:
        clean = meal.strip().lower()

    if clean not in MEAL_ARCHETYPES:
        raise ValueError(
            f"Unknown meal type '{meal}'. Valid meal types: {list(MEAL_ARCHETYPES.keys())}"
        )
    return list(MEAL_ARCHETYPES[clean])


def get_default_archetype_for_meal(meal: str) -> Archetype:
    """Returns the primary default archetype for the given meal type."""
    archetypes = get_archetypes_for_meal(meal)
    return archetypes[0]


def get_slot_for_role(archetype: Archetype, role: Union[Role, str]) -> Optional[Slot]:
    """Finds the slot in the given archetype that accepts or matches the specified role."""
    return archetype.get_slot(role)


def clamp_ingredient_portion(
    archetype: Optional[Archetype],
    role: Any,
    grams: Any = 0.0,
) -> float:
    """Clamps a proposed ingredient weight in grams to the slot's bounds.

    Defensively handles:
    - Non-numeric, unparseable, or NaN grams (falls back to minimum bound).
    - Numeric string grams (coerces to float).
    - Unhashable or non-Role role types (falls back to default portion bounds).
    - None or invalid archetype objects.
    """
    min_g, max_g = 10.0, 250.0

    slot: Optional[Slot] = None
    if archetype is not None and hasattr(archetype, "get_slot"):
        try:
            slot = archetype.get_slot(role)
        except (TypeError, ValueError):
            slot = None

    if slot is not None:
        return slot.clamp_portion(grams)

    # Fallback to role-based default portions
    role_enum = None
    if isinstance(role, Role):
        role_enum = role
    elif isinstance(role, str):
        try:
            role_enum = Role(role)
        except (ValueError, TypeError):
            role_enum = None

    if role_enum is not None:
        try:
            min_g, max_g = DEFAULT_ROLE_PORTIONS.get(role_enum, (10.0, 250.0))
        except (TypeError, KeyError):
            min_g, max_g = 10.0, 250.0
    else:
        try:
            min_g, max_g = DEFAULT_ROLE_PORTIONS.get(role, (10.0, 250.0))
        except (TypeError, KeyError):
            min_g, max_g = 10.0, 250.0

    if isinstance(grams, bool) or grams is None:
        val = min_g
    else:
        try:
            val = float(grams)
            if math.isnan(val):
                val = min_g
        except (ValueError, TypeError):
            val = min_g

    return max(min_g, min(val, max_g))


# ---------------------------------------------------------------------------
# Slot Validation Engine
# ---------------------------------------------------------------------------

@dataclass
class SlotValidationResult:
    """Result of slot assignment validation."""

    is_valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def _extract_item_roles(item: Any) -> List[Any]:
    """Extracts and normalizes culinary roles from an ingredient candidate or dict.

    Safely handles string roles and invalid roles without raising unhandled exceptions.
    """
    if item is None:
        return []

    item_role = getattr(item, "role", None) or (
        item.get("role") if isinstance(item, dict) else None
    )
    item_roles = (
        getattr(item, "all_eligible_roles", None)
        or getattr(item, "roles", None)
        or (item.get("roles") if isinstance(item, dict) else None)
        or (item.get("all_eligible_roles") if isinstance(item, dict) else None)
    )

    raw_roles: List[Any] = []
    if item_role is not None:
        raw_roles.append(item_role)
    if item_roles is not None:
        if isinstance(item_roles, str):
            raw_roles.append(item_roles)
        elif isinstance(item_roles, (Sequence, set)):
            raw_roles.extend(item_roles)
        else:
            raw_roles.append(item_roles)

    resolved: List[Any] = []
    for r in raw_roles:
        if isinstance(r, Role):
            if r not in resolved:
                resolved.append(r)
        elif isinstance(r, str):
            try:
                role_enum = Role(r)
                if role_enum not in resolved:
                    resolved.append(role_enum)
            except (ValueError, TypeError):
                if r not in resolved:
                    resolved.append(r)
        else:
            if r not in resolved:
                resolved.append(r)
    return resolved


def _extract_item_id(item: Any) -> str:
    """Extracts a unique ingredient identifier for duplicate detection.

    Checks in priority order:
    1. Explicit non-None, non-empty identifier from attributes or dict keys
       ('id', 'ID', 'food_id', 'product_id').
    2. Explicit non-None, non-empty name from attributes or dict keys
       ('name', 'product').
    3. Fallback to unique object identity `f"anonymous_{id(item)}"` so anonymous items
       do not trigger false-positive collisions with each other, while reusing the
       exact same object instance across slots will still be caught as a duplicate.
    """
    if item is None:
        return f"anonymous_{id(item)}"

    search_fields = (
        ("id", "ID", "food_id", "product_id"),
        ("name", "product"),
    )

    for field_group in search_fields:
        for field in field_group:
            val = None
            if isinstance(item, (dict, Mapping)):
                val = item.get(field)
            if val is None and hasattr(item, field):
                try:
                    val = getattr(item, field)
                except Exception:
                    val = None

            if val is not None:
                s_val = str(val).strip()
                if s_val:
                    return s_val

    return f"anonymous_{id(item)}"


def validate_slot_filling(
    archetype: Archetype,
    assignments: Union[Dict[Any, Sequence[Any]], Sequence[Any]],
) -> SlotValidationResult:
    """Validates that a collection or mapping of slot assignments satisfies an archetype's grammar.

    Accepts either:
    - A dictionary mapping Slot, Role, or role name to assigned ingredient candidates.
    - A sequence (list) of ingredient candidates, automatically mapped to slots.

    Checks:
    1. Every required slot in the archetype has count >= cardinality_min.
    2. No slot in the archetype exceeds cardinality_max.
    3. Assigned items do not contain duplicate ingredient IDs across different slots.
    4. Assigned items match slot role compatibility.
    5. Portion bounds are within physiological limits (warns if breached).
    """
    errors: List[str] = []
    warnings: List[str] = []

    normalized_assignments: Dict[Slot, List[Any]] = {s: [] for s in archetype.slots}
    seen_ingredients: Dict[str, str] = {}

    if isinstance(assignments, (dict, Mapping)):
        for key, raw_items in assignments.items():
            slot: Optional[Slot] = None
            if isinstance(key, Slot):
                slot = key
            elif isinstance(key, (Role, str)):
                slot = archetype.get_slot(key)

            if slot is None or slot not in archetype.slots:
                errors.append(
                    f"Assignment key '{key}' does not correspond to any slot in archetype '{archetype.name}'."
                )
                continue

            if raw_items is None:
                continue

            if isinstance(raw_items, (dict, Mapping)) or not isinstance(raw_items, (Sequence, set)):
                items_seq = [raw_items]
            else:
                items_seq = list(raw_items)

            for item in items_seq:
                if item is None:
                    errors.append(
                        f"Slot '{slot.name}' contains a null (None) ingredient candidate."
                    )
                    continue

                item_id = _extract_item_id(item)
                if item_id in seen_ingredients:
                    errors.append(
                        f"Duplicate ingredient '{item_id}' is assigned to multiple slots: "
                        f"'{seen_ingredients[item_id]}' and '{slot.name}'."
                    )
                else:
                    seen_ingredients[item_id] = slot.name

                candidate_roles = _extract_item_roles(item)
                if not candidate_roles:
                    errors.append(
                        f"Item '{item}' assigned to slot '{slot.name}' has no defined culinary role."
                    )
                    continue

                if not any(slot.accepts_role(r) for r in candidate_roles):
                    errors.append(
                        f"Item '{item}' with role(s) {[str(r) for r in candidate_roles]} "
                        f"is incompatible with slot '{slot.name}' (accepts {[str(r) for r in slot.compatible_roles]})."
                    )
                    continue

                normalized_assignments[slot].append(item)

    elif isinstance(assignments, Sequence):
        # Sequence of ingredient candidate items: assign each item to an appropriate slot
        for item in assignments:
            if item is None:
                errors.append("Ingredient candidate sequence contains a null (None) item.")
                continue

            candidates = _extract_item_roles(item)
            if not candidates:
                errors.append(
                    f"Item '{item}' has no defined culinary role and does not fit into any slot of archetype '{archetype.name}'."
                )
                continue

            matched_slot: Optional[Slot] = None

            # Pass 1: Try required slots that still need items
            for s in archetype.slots:
                if not s.is_optional and len(normalized_assignments[s]) < s.cardinality_max:
                    if any(s.accepts_role(r) for r in candidates):
                        matched_slot = s
                        break

            # Pass 2: Try optional slots that still have room
            if matched_slot is None:
                for s in archetype.slots:
                    if s.is_optional and len(normalized_assignments[s]) < s.cardinality_max:
                        if any(s.accepts_role(r) for r in candidates):
                            matched_slot = s
                            break

            # Pass 3: Fallback to any slot that accepts the role
            if matched_slot is None:
                for s in archetype.slots:
                    if any(s.accepts_role(r) for r in candidates):
                        matched_slot = s
                        break

            if matched_slot is not None:
                item_id = _extract_item_id(item)
                if item_id in seen_ingredients:
                    errors.append(
                        f"Duplicate ingredient '{item_id}' is assigned to multiple slots: "
                        f"'{seen_ingredients[item_id]}' and '{matched_slot.name}'."
                    )
                else:
                    seen_ingredients[item_id] = matched_slot.name
                normalized_assignments[matched_slot].append(item)
            else:
                errors.append(
                    f"Item '{item}' with role(s) {[str(r) for r in candidates]} "
                    f"does not fit into any slot of archetype '{archetype.name}'."
                )

    # 1. Check all required slots are present and satisfy minimum cardinality
    for slot in archetype.slots:
        if not slot.is_optional:
            assigned = normalized_assignments.get(slot, [])
            count = len(assigned)
            if count < slot.cardinality_min:
                errors.append(
                    f"Missing required slot '{slot.name}' ({slot.role.value}): requires at least "
                    f"{slot.cardinality_min} ingredient(s), but got {count}."
                )

    # 2. Check maximum cardinalities for all assigned slots
    for slot, assigned in normalized_assignments.items():
        count = len(assigned)
        if count > slot.cardinality_max:
            errors.append(
                f"Slot '{slot.name}' ({slot.role.value}) exceeds maximum cardinality: "
                f"allowed at most {slot.cardinality_max}, but got {count}."
            )

    # 3. Check physiological portion bounds
    for slot, assigned in normalized_assignments.items():
        for item in assigned:
            qty = getattr(item, "resolved_quantity_g", None) or getattr(item, "quantity", None)
            if qty is None and isinstance(item, dict):
                qty = item.get("resolved_quantity_g") or item.get("quantity")

            if qty is not None:
                try:
                    qty_f = float(qty)
                    if not slot.validate_portion(qty_f):
                        warnings.append(
                            f"Item '{item}' portion {qty_f}g is outside slot '{slot.name}' "
                            f"physiological bounds [{slot.portion_min_g}g, {slot.portion_max_g}g]."
                        )
                except (ValueError, TypeError):
                    pass

    return SlotValidationResult(
        is_valid=(len(errors) == 0),
        errors=errors,
        warnings=warnings,
    )
