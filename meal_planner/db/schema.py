"""Unified relational database schema for the Meal Planner application (Schema Version 1)."""

import os
import sqlite3

from meal_planner.db.connection import DB_PATH, get_connection

SCHEMA_VERSION = 1


class DatabaseIntegrityError(RuntimeError):
    """Raised when SQLite integrity_check or foreign_key_check fails at startup."""


def verify_integrity(conn: sqlite3.Connection) -> None:
    """Runs PRAGMA integrity_check and PRAGMA foreign_key_check, raising DatabaseIntegrityError on failure."""
    integrity_rows = conn.execute("PRAGMA integrity_check").fetchall()
    if integrity_rows != [("ok",)]:
        details = "; ".join(str(r[0]) for r in integrity_rows[:10]) or "unknown integrity error"
        raise DatabaseIntegrityError(f"Database integrity check failed: {details}")

    fk_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
    if fk_rows:
        details = "; ".join(
            f"table={r[0]}, rowid={r[1]}, parent={r[2]}, fkid={r[3]}" for r in fk_rows[:10]
        )
        raise DatabaseIntegrityError(f"Database foreign key check failed: {details}")

DEFAULT_MEALS = [("breakfast", 1, 25.0), ("lunch", 2, 40.0), ("dinner", 3, 35.0)]

DEFAULT_GOALS = [
    ("Protein", "per_kg", 0.9, 1.6),
    ("Carbs", "pct_energy", 45.0, 60.0),
    ("Fat", "pct_energy", 20.0, 35.0),
    ("Sugars", "pct_energy", None, 15.0),
    ("Saturated_fat", "pct_energy", None, 10.0),
    ("Fiber", "absolute", 25.0, None),
    ("Fiber", "per_1000kcal", 12.6, None),
    ("Salt", "absolute", None, 5.0),
]

DEFAULT_LOCATIONS = [("pantry",), ("fridge",), ("freezer",)]

ALL_TABLES = [
    "PRODUCTS", "ALIASES", "LOCATIONS", "INVENTORY", "SHOPPING_LIST", "EXCLUSION_LIST",
    "MEALS", "RECIPES", "RECIPE_MEALS", "SUBSTITUTE_GROUPS", "GROUP_MEMBERS",
    "RECIPE_INGREDIENTS", "COOKINGS", "PLAN", "PROFILE", "WEIGHT_LOG", "GOALS",
    "PLAN_SHOPPING_LIST", "RECEIPT_LOGS", "APP_SETTINGS",
]

_PROFILE_COLS = (
    ("Weight_kg", None), ("Height_cm", None), ("Age_years", None), ("Sex", None),
    ("Activity_Level", None), ("Goal", None), ("Kcal_target", None),
    ("Kcal_tolerance_pct", 5), ("Open_extra_pct", 5), ("No_repeat_days", 3),
    ("Target_weight_kg", None), ("Weekly_rate_kg", None),
    ("Maintenance_manual", None), ("Use_manual_maintenance", 0),
    ("Kcal_manual", None), ("Use_manual_kcal", 0), ("Default_servings", 1),
)

_EXP_CHECK = "TEXT CHECK (Expiration_Date IS NULL OR Expiration_Date GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]-[0-9][0-9]')"

_TABLES = (
    """CREATE TABLE IF NOT EXISTS PRODUCTS (
        ID INTEGER PRIMARY KEY, Unit TEXT NOT NULL CHECK (Unit IN ('kg', 'L', 'pcs')),
        Kcal_100g REAL NOT NULL CHECK (Kcal_100g >= 0), Protein_100g REAL NOT NULL CHECK (Protein_100g >= 0),
        Carbs_100g REAL NOT NULL CHECK (Carbs_100g >= 0), Sugars_100g REAL CHECK (Sugars_100g >= 0),
        Fat_100g REAL NOT NULL CHECK (Fat_100g >= 0), Saturated_fat_100g REAL CHECK (Saturated_fat_100g >= 0),
        Fiber_100g REAL CHECK (Fiber_100g >= 0), Salt_100g REAL CHECK (Salt_100g >= 0),
        Grams_per_piece REAL CHECK (Grams_per_piece > 0), Density_kg_per_L REAL CHECK (Density_kg_per_L > 0))""",
    "CREATE TABLE IF NOT EXISTS ALIASES (Name TEXT PRIMARY KEY, ID INTEGER NOT NULL, FOREIGN KEY (ID) REFERENCES PRODUCTS(ID))",
    "CREATE TABLE IF NOT EXISTS LOCATIONS (Name TEXT PRIMARY KEY)",
    f"""CREATE TABLE IF NOT EXISTS INVENTORY (
        Row_ID INTEGER PRIMARY KEY AUTOINCREMENT, ID INTEGER NOT NULL, Unit_Weight REAL NOT NULL CHECK (Unit_Weight > 0),
        Intact_Items INTEGER NOT NULL CHECK (Intact_Items >= 0), Open_Remainder REAL NOT NULL DEFAULT 0 CHECK (Open_Remainder >= 0),
        Location TEXT NOT NULL, Expiration_Date {_EXP_CHECK}, FOREIGN KEY (ID) REFERENCES PRODUCTS(ID))""",
    "CREATE TABLE IF NOT EXISTS SHOPPING_LIST (ID INTEGER PRIMARY KEY, Reference_Name TEXT NOT NULL, Missing_Quantity REAL NOT NULL CHECK (Missing_Quantity > 0), FOREIGN KEY (ID) REFERENCES PRODUCTS(ID))",
    "CREATE TABLE IF NOT EXISTS EXCLUSION_LIST (ID INTEGER NOT NULL, Ref_Name TEXT NOT NULL, Expiration_Date TEXT NOT NULL, Location TEXT NOT NULL, PRIMARY KEY (ID, Location))",
    "CREATE TABLE IF NOT EXISTS MEALS (Name TEXT PRIMARY KEY, Sort_Order INTEGER NOT NULL UNIQUE, Energy_Share_pct REAL NOT NULL CHECK (Energy_Share_pct > 0 AND Energy_Share_pct <= 100))",
    "CREATE TABLE IF NOT EXISTS RECIPES (Recipe_ID INTEGER PRIMARY KEY AUTOINCREMENT, Name TEXT NOT NULL UNIQUE COLLATE NOCASE, Notes TEXT)",
    "CREATE TABLE IF NOT EXISTS RECIPE_MEALS (Recipe_ID INTEGER NOT NULL, Meal TEXT NOT NULL, PRIMARY KEY (Recipe_ID, Meal), FOREIGN KEY (Recipe_ID) REFERENCES RECIPES(Recipe_ID) ON DELETE CASCADE, FOREIGN KEY (Meal) REFERENCES MEALS(Name))",
    "CREATE TABLE IF NOT EXISTS SUBSTITUTE_GROUPS (Group_ID INTEGER PRIMARY KEY AUTOINCREMENT, Name TEXT NOT NULL UNIQUE COLLATE NOCASE)",
    "CREATE TABLE IF NOT EXISTS GROUP_MEMBERS (Group_ID INTEGER NOT NULL, ID INTEGER NOT NULL, PRIMARY KEY (Group_ID, ID), FOREIGN KEY (Group_ID) REFERENCES SUBSTITUTE_GROUPS(Group_ID) ON DELETE CASCADE, FOREIGN KEY (ID) REFERENCES PRODUCTS(ID))",
    """CREATE TABLE IF NOT EXISTS RECIPE_INGREDIENTS (
        Row_ID INTEGER PRIMARY KEY AUTOINCREMENT, Recipe_ID INTEGER NOT NULL, ID INTEGER, Group_ID INTEGER,
        Quantity REAL NOT NULL CHECK (Quantity > 0), Unit TEXT NOT NULL CHECK (Unit IN ('g','kg','ml','L','pcs','tbsp','tsp')),
        To_Taste INTEGER NOT NULL DEFAULT 0 CHECK (To_Taste IN (0, 1)),
        FOREIGN KEY (Recipe_ID) REFERENCES RECIPES(Recipe_ID) ON DELETE CASCADE,
        FOREIGN KEY (ID) REFERENCES PRODUCTS(ID), FOREIGN KEY (Group_ID) REFERENCES SUBSTITUTE_GROUPS(Group_ID),
        CHECK ((ID IS NOT NULL) + (Group_ID IS NOT NULL) = 1))""",
    """CREATE TABLE IF NOT EXISTS COOKINGS (
        Cooking_ID INTEGER PRIMARY KEY AUTOINCREMENT, Recipe_ID INTEGER NOT NULL, Cooking_Date TEXT NOT NULL,
        Total_Servings INTEGER NOT NULL CHECK (Total_Servings > 0),
        Remaining_Servings INTEGER NOT NULL CHECK (Remaining_Servings >= 0 AND Remaining_Servings <= Total_Servings),
        Status TEXT NOT NULL DEFAULT 'planned' CHECK (Status IN ('planned', 'cooked')),
        FOREIGN KEY (Recipe_ID) REFERENCES RECIPES(Recipe_ID))""",
    """CREATE TABLE IF NOT EXISTS PLAN (
        Entry_ID INTEGER PRIMARY KEY AUTOINCREMENT, Date TEXT NOT NULL, Meal TEXT NOT NULL, Cooking_ID INTEGER,
        Status TEXT NOT NULL DEFAULT 'proposed' CHECK (Status IN ('proposed', 'confirmed', 'consumed', 'skipped')),
        UNIQUE (Date, Meal), FOREIGN KEY (Meal) REFERENCES MEALS(Name), FOREIGN KEY (Cooking_ID) REFERENCES COOKINGS(Cooking_ID),
        CHECK ((Status = 'skipped') = (Cooking_ID IS NULL)))""",
    """CREATE TABLE IF NOT EXISTS PROFILE (
        ID INTEGER PRIMARY KEY CHECK (ID = 1), Weight_kg REAL CHECK (Weight_kg > 0), Height_cm REAL CHECK (Height_cm > 0),
        Age_years INTEGER CHECK (Age_years > 0), Sex TEXT CHECK (Sex IN ('M', 'F')),
        Activity_Level TEXT CHECK (Activity_Level IN ('light', 'moderate', 'heavy')),
        Goal TEXT CHECK (Goal IN ('maintenance', 'weight_loss', 'weight_gain')), Kcal_target REAL CHECK (Kcal_target > 0),
        Kcal_tolerance_pct REAL NOT NULL DEFAULT 5 CHECK (Kcal_tolerance_pct >= 0),
        Open_extra_pct REAL NOT NULL DEFAULT 5 CHECK (Open_extra_pct >= 0),
        No_repeat_days INTEGER NOT NULL DEFAULT 3 CHECK (No_repeat_days >= 0),
        Target_weight_kg REAL CHECK (Target_weight_kg > 0), Weekly_rate_kg REAL CHECK (Weekly_rate_kg > 0),
        Maintenance_manual REAL CHECK (Maintenance_manual > 0), Use_manual_maintenance INTEGER NOT NULL DEFAULT 0,
        Kcal_manual REAL CHECK (Kcal_manual > 0), Use_manual_kcal INTEGER NOT NULL DEFAULT 0,
        Default_servings INTEGER NOT NULL DEFAULT 1 CHECK (Default_servings BETWEEN 1 AND 20))""",
    "CREATE TABLE IF NOT EXISTS WEIGHT_LOG (Date TEXT PRIMARY KEY, Weight_kg REAL NOT NULL CHECK (Weight_kg > 0), Body_fat_pct REAL CHECK (Body_fat_pct >= 0), Waist_cm REAL CHECK (Waist_cm > 0))",
    """CREATE TABLE IF NOT EXISTS GOALS (
        Nutrient TEXT NOT NULL CHECK (Nutrient IN ('Protein','Carbs','Fat','Sugars','Saturated_fat','Fiber','Salt')),
        Type TEXT NOT NULL CHECK (Type IN ('absolute','pct_energy','per_1000kcal','per_kg')),
        Min_Value REAL CHECK (Min_Value >= 0), Max_Value REAL CHECK (Max_Value >= 0), PRIMARY KEY (Nutrient, Type),
        CHECK (Min_Value IS NOT NULL OR Max_Value IS NOT NULL),
        CHECK (Min_Value IS NULL OR Max_Value IS NULL OR Min_Value <= Max_Value))""",
    "CREATE TABLE IF NOT EXISTS PLAN_SHOPPING_LIST (ID INTEGER PRIMARY KEY, Missing_Quantity REAL NOT NULL CHECK (Missing_Quantity > 0), FOREIGN KEY (ID) REFERENCES PRODUCTS(ID))",
    "CREATE TABLE IF NOT EXISTS RECEIPT_LOGS (id INTEGER PRIMARY KEY AUTOINCREMENT, time TEXT NOT NULL, day TEXT NOT NULL, file_name TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS APP_SETTINGS (Key TEXT PRIMARY KEY, Value TEXT NOT NULL, Updated_At TEXT DEFAULT (datetime('now', 'localtime')))",
)

_MIGRATIONS = (
    ("PRODUCTS", "Grams_per_piece", "REAL CHECK (Grams_per_piece > 0)"),
    ("PRODUCTS", "Density_kg_per_L", "REAL CHECK (Density_kg_per_L > 0)"),
    ("INVENTORY", "Expiration_Date", _EXP_CHECK),
    ("PROFILE", "Target_weight_kg", "REAL CHECK (Target_weight_kg > 0)"),
    ("PROFILE", "Weekly_rate_kg", "REAL CHECK (Weekly_rate_kg > 0)"),
    ("PROFILE", "Maintenance_manual", "REAL CHECK (Maintenance_manual > 0)"),
    ("PROFILE", "Use_manual_maintenance", "INTEGER NOT NULL DEFAULT 0"),
    ("PROFILE", "Kcal_manual", "REAL CHECK (Kcal_manual > 0)"),
    ("PROFILE", "Use_manual_kcal", "INTEGER NOT NULL DEFAULT 0"),
    ("PROFILE", "Default_servings", "INTEGER NOT NULL DEFAULT 1 CHECK (Default_servings BETWEEN 1 AND 20)"),
)

_INDICES = (
    "CREATE INDEX IF NOT EXISTS idx_alias_product ON ALIASES(ID)",
    "CREATE INDEX IF NOT EXISTS idx_inventory_product ON INVENTORY(ID)",
    "CREATE INDEX IF NOT EXISTS idx_inventory_expiration ON INVENTORY(Expiration_Date)",
    "CREATE INDEX IF NOT EXISTS idx_inventory_product_loc ON INVENTORY(ID, Location)",
    "CREATE INDEX IF NOT EXISTS idx_inventory_location ON INVENTORY(Location)",
    "CREATE INDEX IF NOT EXISTS idx_recipe_ingredients_recipe ON RECIPE_INGREDIENTS(Recipe_ID)",
    "CREATE INDEX IF NOT EXISTS idx_cookings_status ON COOKINGS(Status, Remaining_Servings)",
    "CREATE INDEX IF NOT EXISTS idx_cookings_date ON COOKINGS(Cooking_Date)",
    "CREATE INDEX IF NOT EXISTS idx_plan_cooking ON PLAN(Cooking_ID)",
    "CREATE INDEX IF NOT EXISTS idx_plan_date_status ON PLAN(Date, Status)",
    "CREATE INDEX IF NOT EXISTS idx_receipt_logs_day_time ON RECEIPT_LOGS(day, time)",
)

_VIEWS = (
    """CREATE VIEW IF NOT EXISTS SHOPPING_LIST_VIEW AS
       SELECT t.ID AS ID, (SELECT a.Name FROM ALIASES a WHERE a.ID = t.ID ORDER BY a.rowid LIMIT 1) AS Name,
              SUM(t.Q) AS Missing_Quantity, n.Unit AS Unit
       FROM (SELECT ID, Missing_Quantity AS Q FROM SHOPPING_LIST UNION ALL SELECT ID, Missing_Quantity FROM PLAN_SHOPPING_LIST) t
       JOIN PRODUCTS n ON n.ID = t.ID GROUP BY t.ID""",
    """CREATE VIEW IF NOT EXISTS INGREDIENT_GRAMS_VIEW AS
       SELECT ri.Row_ID, ri.Recipe_ID, ri.ID, ri.To_Taste,
              CASE ri.Unit WHEN 'g' THEN ri.Quantity WHEN 'kg' THEN ri.Quantity * 1000.0
                   WHEN 'pcs' THEN ri.Quantity * n.Grams_per_piece WHEN 'ml' THEN ri.Quantity * n.Density_kg_per_L
                   WHEN 'L' THEN ri.Quantity * 1000.0 * n.Density_kg_per_L
                   WHEN 'tbsp' THEN ri.Quantity * 15.0 * n.Density_kg_per_L
                   WHEN 'tsp' THEN ri.Quantity * 5.0 * n.Density_kg_per_L END AS Grams
       FROM RECIPE_INGREDIENTS ri JOIN PRODUCTS n ON n.ID = ri.ID""",
    """CREATE VIEW IF NOT EXISTS RECIPE_MACROS_VIEW AS
       SELECT r.Recipe_ID, r.Name, SUM(g.Grams / 100.0 * n.Kcal_100g) AS Kcal,
              SUM(g.Grams / 100.0 * n.Protein_100g) AS Protein, SUM(g.Grams / 100.0 * n.Carbs_100g) AS Carbs,
              SUM(g.Grams / 100.0 * n.Fat_100g) AS Fat, SUM(g.Grams / 100.0 * COALESCE(n.Sugars_100g, 0)) AS Sugars,
              SUM(g.Grams / 100.0 * COALESCE(n.Saturated_fat_100g, 0)) AS Saturated_fat,
              SUM(g.Grams / 100.0 * COALESCE(n.Fiber_100g, 0)) AS Fiber, SUM(g.Grams / 100.0 * COALESCE(n.Salt_100g, 0)) AS Salt,
              MAX(CASE WHEN g.Grams IS NULL OR n.Sugars_100g IS NULL OR n.Saturated_fat_100g IS NULL
                            OR n.Fiber_100g IS NULL OR n.Salt_100g IS NULL THEN 1 ELSE 0 END) AS Incomplete_Data
       FROM RECIPES r JOIN INGREDIENT_GRAMS_VIEW g ON g.Recipe_ID = r.Recipe_ID JOIN PRODUCTS n ON n.ID = g.ID GROUP BY r.Recipe_ID""",
)


def _user_profile_statements() -> tuple[str, ...]:
    cols = ", ".join(c for c, _ in _PROFILE_COLS)
    sets = ", ".join(f"{c}=NEW.{c}" for c, _ in _PROFILE_COLS)
    vals = ", ".join(
        ["COALESCE(NEW.Profile_ID,NEW.ID,1)"]
        + [f"COALESCE(NEW.{c},{d})" if d is not None else f"NEW.{c}" for c, d in _PROFILE_COLS]
    )
    return (
        f"CREATE VIEW IF NOT EXISTS USER_PROFILE AS SELECT ID AS Profile_ID, ID, {cols} FROM PROFILE",
        f"CREATE TRIGGER IF NOT EXISTS trg_user_profile_update INSTEAD OF UPDATE ON USER_PROFILE BEGIN UPDATE PROFILE SET {sets} WHERE ID=COALESCE(OLD.Profile_ID,OLD.ID,1); END",
        f"CREATE TRIGGER IF NOT EXISTS trg_user_profile_insert INSTEAD OF INSERT ON USER_PROFILE BEGIN INSERT OR REPLACE INTO PROFILE (ID, {cols}) VALUES ({vals}); END",
        "CREATE TRIGGER IF NOT EXISTS trg_user_profile_delete INSTEAD OF DELETE ON USER_PROFILE BEGIN DELETE FROM PROFILE WHERE ID=COALESCE(OLD.Profile_ID,OLD.ID,1); END",
    )


def _add_column(conn: sqlite3.Connection, table: str, column: str, ddl: str, existing_cols: set = None) -> bool:
    if existing_cols is None:
        existing_cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column in existing_cols:
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    existing_cols.add(column)
    return True


def create_schema(conn: sqlite3.Connection) -> None:
    """Idempotently creates all tables, indices, views, migrations, and initial data."""
    row = conn.execute("PRAGMA user_version").fetchone()
    current_version = row[0] if row else 0
    if current_version >= SCHEMA_VERSION and schema_exists(conn):
        return

    cur = conn.cursor()
    for stmt in _TABLES:
        cur.execute(stmt)
    cols_cache: dict[str, set] = {}
    for tbl, col, ddl in _MIGRATIONS:
        cols = cols_cache.setdefault(tbl, {r[1] for r in conn.execute(f"PRAGMA table_info({tbl})")})
        _add_column(conn, tbl, col, ddl, existing_cols=cols)
    for stmt in (*_INDICES, *_VIEWS, *_user_profile_statements()):
        cur.execute(stmt)
    cur.executemany("INSERT OR IGNORE INTO LOCATIONS (Name) VALUES (?)", DEFAULT_LOCATIONS)
    cur.execute("INSERT OR IGNORE INTO LOCATIONS (Name) SELECT DISTINCT Location FROM INVENTORY WHERE Location IS NOT NULL")
    cur.executemany("INSERT OR IGNORE INTO MEALS (Name, Sort_Order, Energy_Share_pct) VALUES (?, ?, ?)", DEFAULT_MEALS)
    cur.executemany("INSERT OR IGNORE INTO GOALS (Nutrient, Type, Min_Value, Max_Value) VALUES (?, ?, ?, ?)", DEFAULT_GOALS)
    cur.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def migrate(conn: sqlite3.Connection) -> None:
    """Alias for create_schema, maintaining full compatibility with callers of migrate()."""
    create_schema(conn)


def schema_exists(conn: sqlite3.Connection) -> bool:
    """Checks whether all expected tables exist in the database."""
    placeholders = ",".join("?" for _ in ALL_TABLES)
    rows = conn.execute(f"SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ({placeholders})", ALL_TABLES).fetchall()
    return len(rows) == len(ALL_TABLES)


def main():
    first_time = not os.path.exists(DB_PATH)
    conn = get_connection()
    try:
        create_schema(conn)
        if schema_exists(conn):
            status = "created" if first_time else "verified (already existing)"
            print(f"Database {status} successfully: {DB_PATH}")
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            print("Tables present:", ", ".join(tables))
            print("Views: INGREDIENT_GRAMS_VIEW, RECIPE_MACROS_VIEW, SHOPPING_LIST_VIEW")
        else:
            print("ERROR: the schema does not appear to have been created correctly.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
