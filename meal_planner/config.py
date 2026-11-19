"""Application configuration stored in SQLite APP_SETTINGS (with JSON fallback for custom CONFIG_PATH)."""

from pathlib import Path
import json
import os
import threading

_CONFIG_LOCK = threading.Lock()
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


def _invalidate_cache() -> None:
    global _CACHED_CONFIG, _CACHED_DB_PATH
    with _CONFIG_LOCK:
        _CACHED_CONFIG = None
        _CACHED_DB_PATH = None


def _get_db_conn():
    from meal_planner.db.connection import get_connection
    conn = get_connection()
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='APP_SETTINGS'"
    ).fetchone()
    if not has_table:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS APP_SETTINGS ("
            "Key TEXT PRIMARY KEY, Value TEXT NOT NULL, Updated_At TEXT DEFAULT (datetime('now', 'localtime')))"
        )
    return conn


def _sync_theme(data: dict, prefer_default: bool = True) -> dict:
    if prefer_default and "default_theme" in data:
        data["theme"] = data["default_theme"]
    elif "theme" in data and "default_theme" not in data:
        data["default_theme"] = data["theme"]
    elif "default_theme" in data and "theme" not in data:
        data["theme"] = data["default_theme"]
    return data


def _upsert_settings(conn, items: dict) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO APP_SETTINGS (Key, Value, Updated_At) VALUES (?, ?, datetime('now', 'localtime'))",
        [(k, json.dumps(v)) for k, v in items.items()],
    )
    conn.commit()


def load_config() -> dict:
    """Loads configuration from APP_SETTINGS or custom JSON CONFIG_PATH."""
    data = dict(DEFAULT_CONFIG)
    raw_loaded = {}
    if CONFIG_PATH != DEFAULT_CONFIG_PATH:
        if CONFIG_PATH.exists():
            try:
                content = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                if isinstance(content, dict):
                    raw_loaded = content
            except (OSError, ValueError, AttributeError):
                pass
        data.update(_sync_theme(raw_loaded, prefer_default=True))
        return data

    try:
        conn = _get_db_conn()
        try:
            if DEFAULT_CONFIG_PATH.exists() and not os.environ.get("MEAL_PLANNER_CONFIG"):
                try:
                    content = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
                    if isinstance(content, dict):
                        _upsert_settings(conn, content)
                    DEFAULT_CONFIG_PATH.unlink(missing_ok=True)
                except Exception:
                    pass
            for k, val_str in conn.execute("SELECT Key, Value FROM APP_SETTINGS").fetchall():
                try:
                    raw_loaded[k] = json.loads(val_str)
                except Exception:
                    raw_loaded[k] = val_str
        finally:
            conn.close()
    except Exception:
        pass
    data.update(_sync_theme(raw_loaded, prefer_default=True))
    return data


def save_config(updates: dict) -> None:
    """Persists configuration updates and invalidates cache."""
    updates = _sync_theme(dict(updates), prefer_default=False)
    if CONFIG_PATH != DEFAULT_CONFIG_PATH:
        current = load_config()
        current.update(updates)
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(current, indent=2), encoding="utf-8")
    else:
        try:
            conn = _get_db_conn()
            try:
                _upsert_settings(conn, updates)
            finally:
                conn.close()
        except Exception:
            pass
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
    return default if default is not None else DEFAULT_CONFIG.get(key)


def set_key(key: str, value) -> None:
    """Sets a single configuration key and persists it."""
    save_config({key: value})
