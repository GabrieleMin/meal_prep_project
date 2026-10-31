"""
meal_planner.db.recipe_schema

Extends the database with the tables for recipes, cookings, the meal plan and
nutritional goals. It is an ADDITIVE and IDEMPOTENT migration: it can be re-run
at any time and never modifies or deletes existing data.

Usage (from the project root):
    python -m meal_planner.db.recipe_schema

Project conventions
-------------------
- The quantities in RECIPE_INGREDIENTS refer to ONE serving.
  Servings are fixed (not scalable); for different sizes, add separate
  recipes (e.g. "Tomato pasta M" / "... L").
- Macros in PRODUCTS are always per 100 g. To convert:
    pcs -> grams: PRODUCTS.Grams_per_piece
    L / ml / tablespoons -> grams: PRODUCTS.Density_kg_per_L (= g/ml)
  If the conversion data is missing, the ingredient is not summed and the
  recipe is flagged with Incomplete_Data = 1 (no invented values).
- An optional nutrient that is NULL (sugars, saturated fat, fiber, salt) counts
  as 0 and also sets Incomplete_Data = 1.
- An ingredient is a product (ID) OR a substitutable group (Group_ID):
  groups model alternatives (rice or barley) and are resolved by the
  planner, not by the SQL views.
- COOKINGS also covers "ready servings": a cooking with status 'cooked' and
  Remaining_Servings > 0 holds leftovers that can be eaten at later meals.
  The rule "do not repeat the same recipe for N days" applies per
  cooking, not per single meal.
- Shopping list: SHOPPING_LIST (consumption origin) is unchanged; the entries
  derived from the plan live in PLAN_SHOPPING_LIST, which can be recomputed
  from scratch. SHOPPING_LIST_VIEW adds them together.
"""

import sqlite3

from meal_planner.db.connection import get_connection, DB_PATH
from meal_planner.db.schema import create_schema

SCHEMA_VERSION = 1  # PRAGMA user_version after this migration

DEFAULT_MEALS = [  # (Name, Sort_Order, Energy_Share_pct)
    ("breakfast", 1, 25.0),
    ("lunch", 2, 40.0),
    ("dinner", 3, 35.0),
]

# (Nutrient, Type, Min_Value, Max_Value). Several rules for the same nutrient all
# apply together (the intersection holds). Reference values from the Italian LARN;
# the 1.6 g/kg protein cap is a project choice, not a LARN value.
DEFAULT_GOALS = [
    ("Protein",      "per_kg",       0.9,  1.6),
    ("Carbs",   "pct_energy",  45.0, 60.0),
    ("Fat",        "pct_energy",  20.0, 35.0),
    ("Sugars",      "pct_energy",  None, 15.0),
    ("Saturated_fat", "pct_energy",  None, 10.0),
    ("Fiber",         "absolute",     25.0, None),
    ("Fiber",         "per_1000kcal", 12.6, None),
    ("Salt",          "absolute",     None, 5.0),
]

# Profile data provided. Sex, activity, goal and Kcal_target stay
# NULL until they are decided.
DEFAULT_PROFILE = (1, 87.0, 176.0, 23)  # ID, Weight_kg, Height_cm, Age_years

NEW_TABLES = [
    "MEALS", "RECIPES", "RECIPE_MEALS", "SUBSTITUTE_GROUPS", "GROUP_MEMBERS",
    "RECIPE_INGREDIENTS", "COOKINGS", "PLAN", "PROFILE", "GOALS",
    "PLAN_SHOPPING_LIST",
]


def _add_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> bool:
    present = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    if column in present:
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    return True


def migrate(conn: sqlite3.Connection) -> None:
    create_schema(conn)  # guarantees the 4 base tables
    cur = conn.cursor()

    # --- New conversion fields in PRODUCTS ---------------------------
    _add_column(conn, "PRODUCTS", "Grams_per_piece",
                      "REAL CHECK (Grams_per_piece > 0)")
    _add_column(conn, "PRODUCTS", "Density_kg_per_L",
                      "REAL CHECK (Density_kg_per_L > 0)")

    # --- Meals (configurable) ---------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS MEALS (
            Name               TEXT    PRIMARY KEY,
            Sort_Order             INTEGER NOT NULL UNIQUE,
            Energy_Share_pct  REAL    NOT NULL
                               CHECK (Energy_Share_pct > 0 AND Energy_Share_pct <= 100)
        );
    """)

    # --- Recipes -----------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS RECIPES (
            Recipe_ID  INTEGER PRIMARY KEY AUTOINCREMENT,
            Name        TEXT    NOT NULL UNIQUE COLLATE NOCASE,
            Notes        TEXT
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS RECIPE_MEALS (
            Recipe_ID  INTEGER NOT NULL,
            Meal       TEXT    NOT NULL,
            PRIMARY KEY (Recipe_ID, Meal),
            FOREIGN KEY (Recipe_ID) REFERENCES RECIPES(Recipe_ID) ON DELETE CASCADE,
            FOREIGN KEY (Meal)      REFERENCES MEALS(Name)
        );
    """)

    # --- Substitutable groups (alternatives: rice or barley) --------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS SUBSTITUTE_GROUPS (
            Group_ID  INTEGER PRIMARY KEY AUTOINCREMENT,
            Name       TEXT    NOT NULL UNIQUE COLLATE NOCASE
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS GROUP_MEMBERS (
            Group_ID  INTEGER NOT NULL,
            ID         INTEGER NOT NULL,
            PRIMARY KEY (Group_ID, ID),
            FOREIGN KEY (Group_ID) REFERENCES SUBSTITUTE_GROUPS(Group_ID) ON DELETE CASCADE,
            FOREIGN KEY (ID)        REFERENCES PRODUCTS(ID)
        );
    """)

    # --- Ingredients (per ONE serving) ------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS RECIPE_INGREDIENTS (
            Row_ID       INTEGER PRIMARY KEY AUTOINCREMENT,
            Recipe_ID    INTEGER NOT NULL,
            ID            INTEGER,
            Group_ID     INTEGER,
            Quantity      REAL    NOT NULL CHECK (Quantity > 0),
            Unit         TEXT    NOT NULL
                          CHECK (Unit IN ('g','kg','ml','L','pcs','tbsp','tsp')),
            To_Taste  INTEGER NOT NULL DEFAULT 0 CHECK (To_Taste IN (0, 1)),
            FOREIGN KEY (Recipe_ID) REFERENCES RECIPES(Recipe_ID) ON DELETE CASCADE,
            FOREIGN KEY (ID)         REFERENCES PRODUCTS(ID),
            FOREIGN KEY (Group_ID)  REFERENCES SUBSTITUTE_GROUPS(Group_ID),
            CHECK ((ID IS NOT NULL) + (Group_ID IS NOT NULL) = 1)
        );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_recipe_ingredients_recipe ON RECIPE_INGREDIENTS(Recipe_ID);")

    # --- Cookings (also covers ready-made servings) -----------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS COOKINGS (
            Cooking_ID        INTEGER PRIMARY KEY AUTOINCREMENT,
            Recipe_ID        INTEGER NOT NULL,
            Cooking_Date      TEXT    NOT NULL,
            Total_Servings   INTEGER NOT NULL CHECK (Total_Servings > 0),
            Remaining_Servings  INTEGER NOT NULL
                              CHECK (Remaining_Servings >= 0 AND Remaining_Servings <= Total_Servings),
            Status             TEXT    NOT NULL DEFAULT 'planned'
                              CHECK (Status IN ('planned', 'cooked')),
            FOREIGN KEY (Recipe_ID) REFERENCES RECIPES(Recipe_ID)
        );
    """)

    # --- Meal plan ---------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS PLAN (
            Entry_ID     INTEGER PRIMARY KEY AUTOINCREMENT,
            Date        TEXT    NOT NULL,
            Meal       TEXT    NOT NULL,
            Cooking_ID  INTEGER,
            Status       TEXT    NOT NULL DEFAULT 'proposed'
                        CHECK (Status IN ('proposed', 'confirmed', 'consumed', 'skipped')),
            UNIQUE (Date, Meal),
            FOREIGN KEY (Meal)      REFERENCES MEALS(Name),
            FOREIGN KEY (Cooking_ID) REFERENCES COOKINGS(Cooking_ID),
            CHECK ((Status = 'skipped') = (Cooking_ID IS NULL))
        );
    """)

    # --- Profile (a single row) and goals ----------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS PROFILE (
            ID                     INTEGER PRIMARY KEY CHECK (ID = 1),
            Weight_kg                REAL    NOT NULL CHECK (Weight_kg > 0),
            Height_cm             REAL    NOT NULL CHECK (Height_cm > 0),
            Age_years               INTEGER NOT NULL CHECK (Age_years > 0),
            Sex                  TEXT    CHECK (Sex IN ('M', 'F')),
            Activity_Level       TEXT    CHECK (Activity_Level IN ('light', 'moderate', 'heavy')),
            Goal              TEXT    CHECK (Goal IN ('maintenance', 'weight_loss', 'weight_gain')),
            Kcal_target            REAL    CHECK (Kcal_target > 0),
            Kcal_tolerance_pct    REAL    NOT NULL DEFAULT 5 CHECK (Kcal_tolerance_pct >= 0),
            Open_extra_pct       REAL    NOT NULL DEFAULT 5 CHECK (Open_extra_pct >= 0),
            No_repeat_days  INTEGER NOT NULL DEFAULT 3 CHECK (No_repeat_days >= 0)
        );
    """)
    for column, ddl in (("Target_weight_kg", "REAL CHECK (Target_weight_kg > 0)"),
                        ("Weekly_rate_kg", "REAL CHECK (Weekly_rate_kg > 0)"),
                        ("Maintenance_manual", "REAL CHECK (Maintenance_manual > 0)"),
                        ("Use_manual_maintenance", "INTEGER NOT NULL DEFAULT 0"),
                        ("Kcal_manual", "REAL CHECK (Kcal_manual > 0)"),
                        ("Use_manual_kcal", "INTEGER NOT NULL DEFAULT 0"),
                        ("Default_servings", "INTEGER NOT NULL DEFAULT 1 CHECK (Default_servings BETWEEN 1 AND 20)")):
        _add_column(conn, "PROFILE", column, ddl)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS WEIGHT_LOG (
            Date          TEXT PRIMARY KEY,
            Weight_kg     REAL NOT NULL CHECK (Weight_kg > 0),
            Body_fat_pct  REAL CHECK (Body_fat_pct >= 0),
            Waist_cm      REAL CHECK (Waist_cm > 0)
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS GOALS (
            Nutrient  TEXT NOT NULL
                       CHECK (Nutrient IN ('Protein','Carbs','Fat','Sugars',
                                            'Saturated_fat','Fiber','Salt')),
            Type       TEXT NOT NULL
                       CHECK (Type IN ('absolute','pct_energy','per_1000kcal','per_kg')),
            Min_Value     REAL CHECK (Min_Value >= 0),
            Max_Value    REAL CHECK (Max_Value >= 0),
            PRIMARY KEY (Nutrient, Type),
            CHECK (Min_Value IS NOT NULL OR Max_Value IS NOT NULL),
            CHECK (Min_Value IS NULL OR Max_Value IS NULL OR Min_Value <= Max_Value)
        );
    """)

    # --- Shopping entries derived from the plan ----------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS PLAN_SHOPPING_LIST (
            ID                 INTEGER PRIMARY KEY,
            Missing_Quantity  REAL    NOT NULL CHECK (Missing_Quantity > 0),
            FOREIGN KEY (ID) REFERENCES PRODUCTS(ID)
        );
    """)

    # --- Views -------------------------------------------------------------
    cur.execute("""
        CREATE VIEW IF NOT EXISTS SHOPPING_LIST_VIEW AS
        SELECT t.ID                          AS ID,
               (SELECT a.Name FROM ALIASES a WHERE a.ID = t.ID ORDER BY a.rowid LIMIT 1) AS Name,
               SUM(t.Q)                      AS Missing_Quantity,
               n.Unit             AS Unit
        FROM (SELECT ID, Missing_Quantity AS Q FROM SHOPPING_LIST
              UNION ALL
              SELECT ID, Missing_Quantity     FROM PLAN_SHOPPING_LIST) t
        JOIN PRODUCTS n ON n.ID = t.ID
        GROUP BY t.ID;
    """)
    cur.execute("""
        CREATE VIEW IF NOT EXISTS INGREDIENT_GRAMS_VIEW AS
        SELECT ri.Row_ID, ri.Recipe_ID, ri.ID, ri.To_Taste,
               CASE ri.Unit
                   WHEN 'g'          THEN ri.Quantity
                   WHEN 'kg'         THEN ri.Quantity * 1000.0
                   WHEN 'pcs'         THEN ri.Quantity * n.Grams_per_piece
                   WHEN 'ml'         THEN ri.Quantity * n.Density_kg_per_L
                   WHEN 'L'          THEN ri.Quantity * 1000.0 * n.Density_kg_per_L
                   WHEN 'tbsp'  THEN ri.Quantity * 15.0 * n.Density_kg_per_L
                   WHEN 'tsp' THEN ri.Quantity * 5.0  * n.Density_kg_per_L
               END AS Grams
        FROM RECIPE_INGREDIENTS ri
        JOIN PRODUCTS n ON n.ID = ri.ID;   -- only ingredients with a specific product
    """)
    cur.execute("""
        CREATE VIEW IF NOT EXISTS RECIPE_MACROS_VIEW AS
        SELECT r.Recipe_ID, r.Name,
               SUM(g.Grams / 100.0 * n.Kcal_100g)                          AS Kcal,
               SUM(g.Grams / 100.0 * n.Protein_100g)                      AS Protein,
               SUM(g.Grams / 100.0 * n.Carbs_100g)                   AS Carbs,
               SUM(g.Grams / 100.0 * n.Fat_100g)                        AS Fat,
               SUM(g.Grams / 100.0 * COALESCE(n.Sugars_100g, 0))         AS Sugars,
               SUM(g.Grams / 100.0 * COALESCE(n.Saturated_fat_100g, 0))    AS Saturated_fat,
               SUM(g.Grams / 100.0 * COALESCE(n.Fiber_100g, 0))            AS Fiber,
               SUM(g.Grams / 100.0 * COALESCE(n.Salt_100g, 0))             AS Salt,
               MAX(CASE WHEN g.Grams IS NULL
                          OR n.Sugars_100g IS NULL OR n.Saturated_fat_100g IS NULL
                          OR n.Fiber_100g IS NULL    OR n.Salt_100g IS NULL
                        THEN 1 ELSE 0 END)                                  AS Incomplete_Data
        FROM RECIPES r
        JOIN INGREDIENT_GRAMS_VIEW g ON g.Recipe_ID = r.Recipe_ID
        JOIN PRODUCTS n               ON n.ID = g.ID
        GROUP BY r.Recipe_ID;
    """)

    # --- Initial configuration data (do not overwrite edits) ---------------
    cur.executemany("INSERT OR IGNORE INTO MEALS (Name, Sort_Order, Energy_Share_pct) VALUES (?, ?, ?)",
                    DEFAULT_MEALS)
    cur.executemany(
        "INSERT OR IGNORE INTO GOALS (Nutrient, Type, Min_Value, Max_Value) VALUES (?, ?, ?, ?)",
        DEFAULT_GOALS)
    cur.execute("INSERT OR IGNORE INTO PROFILE (ID, Weight_kg, Height_cm, Age_years) VALUES (?, ?, ?, ?)",
                DEFAULT_PROFILE)

    cur.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


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
