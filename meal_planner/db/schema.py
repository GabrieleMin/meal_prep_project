"""
meal_planner.db.schema

Creates the SQLite relational database that manages the food inventory.

Schema:
    ALIASES       (Name PK, ID)                         -- N names -> 1 product ID
    PRODUCTS   (ID PK, Unit, macros...)   -- 1 record per product
    INVENTORY  (Row_ID PK, ID FK, Unit_Weight,
                 Intact_Items, Open_Remainder, Location)
    SHOPPING_LIST (ID PK/FK, Reference_Name, Missing_Quantity)

Usage:
    python -m meal_planner.db.schema
    (creates the database file if missing; if it already exists it is NOT
     overwritten - see the main function)
"""

import os
import sqlite3

from meal_planner.db.connection import DB_PATH, get_connection


def create_schema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()

    # ------------------------------------------------------------------
    # PRODUCTS: created first because ALIASES and SHOPPING_LIST reference
    # its ID through a foreign key (the order in which tables are created
    # matters for FK constraints).
    # ------------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS PRODUCTS (
            ID                  INTEGER PRIMARY KEY,
            Unit     TEXT    NOT NULL CHECK (Unit IN ('kg', 'L', 'pcs')),
            Kcal_100g           REAL    NOT NULL CHECK (Kcal_100g >= 0),
            Protein_100g       REAL    NOT NULL CHECK (Protein_100g >= 0),
            Carbs_100g    REAL    NOT NULL CHECK (Carbs_100g >= 0),
            Sugars_100g       REAL    CHECK (Sugars_100g >= 0),
            Fat_100g         REAL    NOT NULL CHECK (Fat_100g >= 0),
            Saturated_fat_100g  REAL    CHECK (Saturated_fat_100g >= 0),
            Fiber_100g          REAL    CHECK (Fiber_100g >= 0),
            Salt_100g           REAL    CHECK (Salt_100g >= 0)
        );
    """)

    # ------------------------------------------------------------------
    # ALIASES: Name is the real lookup key (unique). ID is a grouping
    # attribute, repeatable across rows, and references
    # PRODUCTS.ID.
    # ------------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ALIASES (
            Name    TEXT    PRIMARY KEY,
            ID      INTEGER NOT NULL,
            FOREIGN KEY (ID) REFERENCES PRODUCTS(ID)
        );
    """)
    # Explicit index on ID to speed up "find all aliases of an ID"
    cur.execute("CREATE INDEX IF NOT EXISTS idx_alias_product ON ALIASES(ID);")

    # ------------------------------------------------------------------
    # INVENTORY: each row is a separate physical instance. Row_ID is the
    # auto-increment primary key of the single instance; ID is the
    # foreign key to the product.
    # ------------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS INVENTORY (
            Row_ID                INTEGER PRIMARY KEY AUTOINCREMENT,
            ID                     INTEGER NOT NULL,
            Unit_Weight          REAL    NOT NULL CHECK (Unit_Weight > 0),
            Intact_Items    INTEGER NOT NULL CHECK (Intact_Items >= 0),
            Open_Remainder    REAL    NOT NULL DEFAULT 0 CHECK (Open_Remainder >= 0),
            Location                  TEXT    NOT NULL,
            FOREIGN KEY (ID) REFERENCES PRODUCTS(ID)
        );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_inventory_product ON INVENTORY(ID);")

    # ------------------------------------------------------------------
    # SHOPPING_LIST: at most one row per product (1:0/1 with PRODUCTS).
    # ------------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS SHOPPING_LIST (
            ID                  INTEGER PRIMARY KEY,
            Reference_Name    TEXT    NOT NULL,
            Missing_Quantity   REAL    NOT NULL CHECK (Missing_Quantity > 0),
            FOREIGN KEY (ID) REFERENCES PRODUCTS(ID)
        );
    """)

    conn.commit()


def schema_exists(conn: sqlite3.Connection) -> bool:
    cur = conn.cursor()
    cur.execute("""
        SELECT name FROM sqlite_master
        WHERE type = 'table' AND name IN ('ALIASES', 'PRODUCTS', 'INVENTORY', 'SHOPPING_LIST');
    """)
    return len(cur.fetchall()) == 4


def main():
    first_time = not os.path.exists(DB_PATH)
    conn = get_connection()
    try:
        create_schema(conn)
        if schema_exists(conn):
            status = "created" if first_time else "verified (already existing)"
            print(f"Database {status} successfully: {DB_PATH}")
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type = 'table';")
            tables = [row[0] for row in cur.fetchall()]
            print("Tables present:", ", ".join(tables))
        else:
            print("ERROR: the schema does not appear to have been created correctly.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
