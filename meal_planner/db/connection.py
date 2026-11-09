"""Database location and connection (override the path with MEAL_PLANNER_DB)."""

import os
import shutil
import sqlite3

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(ROOT_DIR, "data")
DEFAULT_DB_PATH = os.path.join(DATA_DIR, "inventory.db")
LEGACY_DB_PATH = os.path.join(ROOT_DIR, "inventory.db")


def _ensure_db_location():
    """Ensures data/ directory exists and inventory.db is located in data/ with compatibility link."""
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(LEGACY_DB_PATH) and not os.path.islink(LEGACY_DB_PATH):
        if not os.path.exists(DEFAULT_DB_PATH):
            shutil.move(LEGACY_DB_PATH, DEFAULT_DB_PATH)
            for ext in ("-wal", "-shm"):
                wal_legacy = LEGACY_DB_PATH + ext
                if os.path.exists(wal_legacy):
                    shutil.move(wal_legacy, DEFAULT_DB_PATH + ext)
    if not os.path.exists(LEGACY_DB_PATH) and os.path.exists(DEFAULT_DB_PATH):
        try:
            rel_target = os.path.relpath(DEFAULT_DB_PATH, ROOT_DIR)
            os.symlink(rel_target, LEGACY_DB_PATH)
        except OSError:
            pass


_ensure_db_location()

DB_PATH = os.environ.get("MEAL_PLANNER_DB") or DEFAULT_DB_PATH


def get_connection(db_path: str = None) -> sqlite3.Connection:
    """
    Opens a database connection with referential integrity checks
    enabled. SQLite disables them by default for historical compatibility,
    so they must be re-enabled explicitly on every connection.
    Also enables WAL journal mode and normal sync for performance.
    """
    if db_path is None:
        db_path = os.environ.get("MEAL_PLANNER_DB") or DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    return conn
