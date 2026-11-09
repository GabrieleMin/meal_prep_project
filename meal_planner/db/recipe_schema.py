"""
meal_planner.db.recipe_schema

Backwards compatibility facade delegating all schema operations to meal_planner.db.schema.
Consolidated schema definition, migrations, views, and defaults live in meal_planner.db.schema.
"""

from meal_planner.db.connection import DB_PATH, get_connection
from meal_planner.db.schema import (
    ALL_TABLES,
    DEFAULT_GOALS,
    DEFAULT_LOCATIONS,
    DEFAULT_MEALS,
    DEFAULT_PROFILE,
    NEW_TABLES,
    SCHEMA_VERSION,
    _add_column,
    _add_table,
    create_schema,
    migrate,
    schema_exists,
)


def main():
    conn = get_connection()
    try:
        migrate(conn)
        present = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        missing = [t for t in NEW_TABLES if t not in present]
        if missing:
            print("ERROR: tables not created:", ", ".join(missing))
            return
        print(f"Recipe schema ready (version {SCHEMA_VERSION}): {DB_PATH}")
        print("New tables:", ", ".join(NEW_TABLES))
        print("Views: INGREDIENT_GRAMS_VIEW, RECIPE_MACROS_VIEW, SHOPPING_LIST_VIEW")
        p = conn.execute("SELECT Weight_kg, Height_cm, Age_years, Sex, Activity_Level, "
                         "Goal, Kcal_target FROM PROFILE").fetchone()
        print("Profile:", p, "(sex, activity, goal and kcal_target still to be defined)")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
