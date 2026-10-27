"""User profile: estimated maintenance energy and profile update."""

import math

from meal_planner.engine.catalog import load_profile
from meal_planner.engine.constants import PAL


def estimate_maintenance(weight_kg, height_cm, age, sex, activity) -> int:
    """Mifflin-St Jeor x activity level (1.4 / 1.6 / 1.75)."""
    bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age + (5 if sex == "M" else -161)
    return int(math.floor(bmr * PAL[activity] + 0.5))   # round half up (Python's round() goes to the even number)


def set_profile(conn, sex, activity, goal, kcal_target=None):
    p = load_profile(conn)
    if kcal_target is None:
        if goal != "maintenance":
            raise ValueError("For weight_loss or weight_gain pass --kcal: "
                             "no automatic deficit or surplus is applied.")
        kcal_target = estimate_maintenance(p["Weight_kg"], p["Height_cm"], p["Age_years"],
                                         sex, activity)
    conn.execute("UPDATE PROFILE SET Sex = ?, Activity_Level = ?, Goal = ?, "
                 "Kcal_target = ? WHERE ID = 1", (sex, activity, goal, kcal_target))
    conn.commit()
    return kcal_target
