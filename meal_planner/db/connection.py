"""Database location and connection (override the path with MEAL_PLANNER_DB)."""

import os
import shutil
import sqlite3

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(ROOT_DIR, "data")
DEFAULT_DB_PATH = os.path.join(DATA_DIR, "inventory.db")
LEGACY_DB_PATH = os.path.join(ROOT_DIR, "inventory.db")


def _ensure_db_location():
    """Ensures data/ directory exists and inventory.db is located exclusively in data/."""
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.islink(LEGACY_DB_PATH):
        try:
            os.unlink(LEGACY_DB_PATH)
        except OSError:
            pass
    elif os.path.exists(LEGACY_DB_PATH) and not os.path.exists(DEFAULT_DB_PATH):
        for ext in ("", "-wal", "-shm"):
            if os.path.exists(LEGACY_DB_PATH + ext):
                shutil.move(LEGACY_DB_PATH + ext, DEFAULT_DB_PATH + ext)


_ensure_db_location()

DB_PATH = os.environ.get("MEAL_PLANNER_DB") or DEFAULT_DB_PATH
_VERIFIED_DIRS = set()
_WAL_CONFIGURED_PATHS = set()


def get_connection(db_path: str = None) -> sqlite3.Connection:
    """Opens a SQLite connection with FK constraints, WAL mode, and NORMAL sync."""
    if db_path is None:
        db_path = os.environ.get("MEAL_PLANNER_DB") or DB_PATH

    dir_path = os.path.dirname(os.path.abspath(db_path))
    if dir_path and dir_path not in _VERIFIED_DIRS:
        if not os.path.isdir(dir_path):
            os.makedirs(dir_path, exist_ok=True)
        _VERIFIED_DIRS.add(dir_path)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")

    norm_path = os.path.abspath(db_path) if db_path != ":memory:" else ":memory:"
    if norm_path not in _WAL_CONFIGURED_PATHS:
        if conn.execute("PRAGMA journal_mode;").fetchone()[0].lower() != "wal":
            conn.execute("PRAGMA journal_mode = WAL;")
        _WAL_CONFIGURED_PATHS.add(norm_path)

    conn.execute("PRAGMA synchronous = NORMAL;")
    return conn
