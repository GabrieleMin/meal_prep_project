"""Read-only access to products, recipes, profile and nutritional goals."""

from meal_planner.engine.constants import MACROS


def load_products(conn) -> dict:
    rows = conn.execute(
        "SELECT ID, Unit, Grams_per_piece, Density_kg_per_L, "
        "Kcal_100g, Protein_100g, Carbs_100g, Fat_100g, Sugars_100g, "
        "Saturated_fat_100g, Fiber_100g, Salt_100g FROM PRODUCTS").fetchall()
    return {r[0]: {"ID": r[0], "unit": r[1], "g_piece": r[2], "density": r[3],
                   "macro": dict(zip(MACROS, r[4:]))} for r in rows}


def product_names(conn) -> dict:
    names = {}
    for product_id, name in conn.execute("SELECT ID, Name FROM ALIASES ORDER BY rowid"):
        names.setdefault(product_id, name)
    return names


def recipe_id_from_name(conn, name: str):
    r = conn.execute("SELECT Recipe_ID FROM RECIPES WHERE Name = ?", (name,)).fetchone()
    return r[0] if r else None


def recipe_ingredients(conn, recipe_id: int) -> list:
    rows = conn.execute(
        "SELECT ID, Group_ID, Quantity, Unit, To_Taste FROM RECIPE_INGREDIENTS "
        "WHERE Recipe_ID = ? ORDER BY Row_ID", (recipe_id,)).fetchall()
    return [{"ID": r[0], "Group_ID": r[1], "Quantity": r[2], "Unit": r[3],
             "To_Taste": r[4]} for r in rows]


def group_members(conn, group_id: int) -> list:
    return [r[0] for r in conn.execute(
        "SELECT ID FROM GROUP_MEMBERS WHERE Group_ID = ? ORDER BY ID", (group_id,))]


def load_profile(conn) -> dict:
    cur = conn.execute(
        "SELECT Weight_kg, Height_cm, Age_years, Sex, Activity_Level, Goal, "
        "Kcal_target, Kcal_tolerance_pct, Open_extra_pct, No_repeat_days, Target_weight_kg, "
        "Weekly_rate_kg, Maintenance_manual, Use_manual_maintenance, Kcal_manual, Use_manual_kcal, "
        "Default_servings FROM PROFILE WHERE ID = 1")
    r = cur.fetchone()
    if r is None:
        raise RuntimeError("Profile missing: run meal_planner.db.recipe_schema first")
    return dict(zip([d[0] for d in cur.description], r))


def load_goals(conn) -> list:
    return [{"Nutrient": r[0], "Type": r[1], "Min_Value": r[2], "Max_Value": r[3]}
            for r in conn.execute("SELECT Nutrient, Type, Min_Value, Max_Value FROM GOALS")]
