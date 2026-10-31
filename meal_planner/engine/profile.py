"""User profile: estimated maintenance energy and profile update."""

import math

from meal_planner.engine.catalog import load_profile
from meal_planner.engine.constants import KCAL_PER_KG, PAL


def estimate_maintenance(weight_kg, height_cm, age, sex, activity) -> int:
    """Mifflin-St Jeor x activity level (1.4 / 1.6 / 1.75)."""
    bmr = 10 * weight_kg + 6.25 * height_cm - 5 * age + (5 if sex == "M" else -161)
    return int(math.floor(bmr * PAL[activity] + 0.5))   # round half up (Python's round() goes to the even number)


def bmi(weight_kg, height_cm):
    value = weight_kg / (height_cm / 100) ** 2
    category = ("underweight" if value < 18.5 else "normal" if value < 25
                else "overweight" if value < 30 else "obese")
    return value, category


def estimates(p):
    """-> (calculated maintenance, calculated target, target in use) from a profile dict.
    Manual values replace the calculated ones when their 'Use_manual_*' flag is set."""
    calc = None
    if p["Sex"] and p["Activity_Level"]:
        calc = estimate_maintenance(p["Weight_kg"], p["Height_cm"], p["Age_years"],
                                    p["Sex"], p["Activity_Level"])
    maintenance = p["Maintenance_manual"] if p["Use_manual_maintenance"] else calc
    target = None
    if maintenance:
        if p["Goal"] == "maintenance":
            target = maintenance
        elif p["Goal"] and p["Weekly_rate_kg"]:
            daily = p["Weekly_rate_kg"] * KCAL_PER_KG / 7
            target = int(math.floor(maintenance + (daily if p["Goal"] == "weight_gain" else -daily) + 0.5))
    return calc, target, (p["Kcal_manual"] if p["Use_manual_kcal"] else target)


def update_profile(conn, values):
    """Saves the whole profile form and logs today's measurements; returns the target in use."""
    v = dict(values)
    log = (v.pop("Body_fat_pct"), v.pop("Waist_cm"))
    if (v["Use_manual_maintenance"] and v["Maintenance_manual"] is None) or \
            (v["Use_manual_kcal"] and v["Kcal_manual"] is None):
        raise ValueError("Enter the manual value or untick 'Use manual'.")
    v["Kcal_target"] = estimates(v)[2]
    if v["Kcal_target"] is None:
        raise ValueError("For weight_loss or weight_gain set the weekly rate (or a manual kcal/day).")
    conn.execute("UPDATE PROFILE SET " + ", ".join(f"{k} = ?" for k in v) + " WHERE ID = 1", list(v.values()))
    conn.execute("INSERT OR REPLACE INTO WEIGHT_LOG (Date, Weight_kg, Body_fat_pct, Waist_cm) "
                 "VALUES (date('now', 'localtime'), ?, ?, ?)", (v["Weight_kg"], *log))
    conn.commit()
    return v["Kcal_target"]


def set_meal_shares(conn, shares):
    total = sum(shares.values())
    if abs(total - 100) > 0.01:
        raise ValueError(f"Meal energy shares must add up to 100% (now {total:g}%).")
    conn.executemany("UPDATE MEALS SET Energy_Share_pct = ? WHERE Name = ?", [(s, n) for n, s in shares.items()])
    conn.commit()


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
