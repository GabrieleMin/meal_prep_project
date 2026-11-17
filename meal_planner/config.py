"""
meal_planner.config

Application configuration handling.
Configuration settings are stored centrally within the SQLite database (APP_SETTINGS table),
eliminating loose JSON files in the workspace.
If a custom CONFIG_PATH is specified (such as by unit test fixtures), JSON file fallback is supported.
"""

from pathlib import Path
import json
import os
import threading

_CONFIG_LOCK = threading.Lock()
_CACHED_CONFIG = None
_CACHED_DB_PATH = None


def _invalidate_cache() -> None:
    global _CACHED_CONFIG, _CACHED_DB_PATH
    with _CONFIG_LOCK:
        _CACHED_CONFIG = None
        _CACHED_DB_PATH = None

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.json"
CONFIG_PATH = Path(os.environ.get("MEAL_PLANNER_CONFIG") or DEFAULT_CONFIG_PATH)

DEFAULT_CONFIG = {
    "default_theme": "dark",
    "theme": "dark",
    "expiration_warning_days": 7,
    "receipts_output_dir": "receipts_output",
}


def _get_db_conn():
    from meal_planner.db.connection import get_connection
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS APP_SETTINGS (
            Key        TEXT PRIMARY KEY,
            Value      TEXT NOT NULL,
            Updated_At TEXT DEFAULT (datetime('now', 'localtime'))
        );
    """)
    return conn


def _migrate_legacy_json_to_db(conn) -> None:
    """Migrates any existing root config.json into APP_SETTINGS and cleans up config.json."""
    if DEFAULT_CONFIG_PATH.exists() and not os.environ.get("MEAL_PLANNER_CONFIG"):
        try:
            content = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(content, dict):
                for k, v in content.items():
                    val_str = json.dumps(v)
                    conn.execute(
                        "INSERT OR REPLACE INTO APP_SETTINGS (Key, Value, Updated_At) "
                        "VALUES (?, ?, datetime('now', 'localtime'))",
                        (k, val_str)
                    )
                conn.commit()
            DEFAULT_CONFIG_PATH.unlink(missing_ok=True)
        except Exception:
            pass


def _load_from_db() -> dict:
    data = dict(DEFAULT_CONFIG)
    try:
        conn = _get_db_conn()
        try:
            _migrate_legacy_json_to_db(conn)
            cur = conn.execute("SELECT Key, Value FROM APP_SETTINGS")
            for k, val_str in cur.fetchall():
                try:
                    val = json.loads(val_str)
                except Exception:
                    val = val_str
                data[k] = val
        finally:
            conn.close()
    except Exception:
        pass

    # Ensure theme and default_theme stay synchronized
    if "default_theme" in data:
        data["theme"] = data["default_theme"]
    elif "theme" in data:
        data["default_theme"] = data["theme"]
    return data


def _save_to_db(updates: dict) -> None:
    try:
        conn = _get_db_conn()
        try:
            for k, v in updates.items():
                val_str = json.dumps(v)
                conn.execute(
                    "INSERT OR REPLACE INTO APP_SETTINGS (Key, Value, Updated_At) "
                    "VALUES (?, ?, datetime('now', 'localtime'))",
                    (k, val_str)
                )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def load_config() -> dict:
    """
    Loads application configuration.
    Uses SQLite APP_SETTINGS table by default.
    Falls back to JSON file if CONFIG_PATH was overridden (e.g. in test fixtures).
    """
    if CONFIG_PATH != DEFAULT_CONFIG_PATH:
        data = dict(DEFAULT_CONFIG)
        if CONFIG_PATH.exists():
            try:
                content = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                if isinstance(content, dict):
                    data.update(content)
            except (OSError, ValueError, AttributeError):
                pass
        if "default_theme" in data:
            data["theme"] = data["default_theme"]
        elif "theme" in data:
            data["default_theme"] = data["theme"]
        return data

    return _load_from_db()


def save_config(updates: dict) -> None:
    """
    Saves configuration updates to SQLite APP_SETTINGS table (or JSON if overridden).
    Synchronizes 'theme' and 'default_theme'.
    """
    if "theme" in updates and "default_theme" not in updates:
        updates["default_theme"] = updates["theme"]
    elif "default_theme" in updates and "theme" not in updates:
        updates["theme"] = updates["default_theme"]

    if CONFIG_PATH != DEFAULT_CONFIG_PATH:
        current = load_config()
        current.update(updates)
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(current, indent=2), encoding="utf-8")
        _invalidate_cache()
        return

    _save_to_db(updates)
    _invalidate_cache()


def get(key: str, default=None):
    """Retrieves a configuration value."""
    global _CACHED_CONFIG, _CACHED_DB_PATH
    current_db = os.environ.get("MEAL_PLANNER_DB")
    with _CONFIG_LOCK:
        if _CACHED_CONFIG is None or _CACHED_DB_PATH != current_db:
            _CACHED_CONFIG = load_config()
            _CACHED_DB_PATH = current_db
        cfg = _CACHED_CONFIG
    val = cfg.get(key)
    if val is not None:
        return val
    if default is not None:
        return default
    return DEFAULT_CONFIG.get(key)


def set_key(key: str, value) -> None:
    """Sets a single configuration key and persists it."""
    save_config({key: value})
