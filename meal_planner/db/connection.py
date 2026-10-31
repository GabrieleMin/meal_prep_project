"""Database location and connection (override the path with MEAL_PLANNER_DB)."""

import os
import sqlite3


DB_PATH = os.environ.get("MEAL_PLANNER_DB") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "inventory.db")


def get_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    """
    Opens a database connection with referential integrity checks
    enabled. SQLite disables them by default for historical compatibility,
    so they must be re-enabled explicitly on every connection.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn
