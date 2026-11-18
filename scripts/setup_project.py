"""
Project setup: create the database, load demo data, check integrity, run the tests.

Usage:
    python -m scripts.setup_project              # first-time setup
    python -m scripts.setup_project --test       # + run test suites
    python -m scripts.setup_project --reset -y   # wipe & reload demo data
    python -m scripts.setup_project --launch     # open the GUI at the end
"""

import argparse
import os
import sqlite3
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from meal_planner.db.connection import DB_PATH  # noqa: E402
from scripts import seed_inventory, seed_recipes  # noqa: E402

TEST_MODULES = (
    "tests.test_domain_core",
    "tests.test_receipts_and_reconciliation",
    "tests.test_inventory_management",
    "tests.test_recipe_management",
    "tests.test_settings_and_profile",
    "tests.test_e2e_and_invariants",
    "tests.test_recipe_generator",
)


def run(module, *args):
    return subprocess.run([sys.executable, "-m", module, *args], cwd=ROOT).returncode == 0


def _count(table):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except sqlite3.OperationalError:
        return 0


def demo_recipes_possible():
    names = set(seed_recipes.CONVERSIONS) | {p for _, _, ings in seed_recipes.RECIPES for p, _, _ in ings}
    with sqlite3.connect(DB_PATH) as conn:
        found = conn.execute(f"SELECT COUNT(*) FROM ALIASES WHERE Name IN ({','.join('?' * len(names))})", list(names)).fetchone()[0]
        return found == len(names)


def check_database():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        print(f"Database integrity: {integrity} | foreign key violations: {len(violations)}")
        for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
            print(f"  {table:<22}{conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]:>5} rows")
        return integrity == "ok" and not violations
    finally:
        conn.close()


def run_tests():
    all_ok = True
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    for module in TEST_MODULES:
        r = subprocess.run([sys.executable, "-m", module], cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
        out = r.stdout + r.stderr
        ok = r.returncode == 0 and "Traceback" not in out and "FAILED (" not in out and "ERROR:" not in out
        summary = [ln for ln in out.splitlines() if "tests passed" in ln or ln.startswith("Ran ")]
        print(f"{module}: {summary[-1] if summary else 'completed'} | {'ok' if ok else 'FAILED'}")
        if not ok:
            print(out[-3000:])
        all_ok = all_ok and ok
    return all_ok


def _seed_step(fn, reset):
    try:
        fn(DB_PATH, reset)
        return True
    except SystemExit as exc:
        return exc.code in (0, None)


def main() -> int:
    parser = argparse.ArgumentParser(description="Set up the project.")
    parser.add_argument("--reset", action="store_true", help="wipe products, inventory and recipes, then reload demo data")
    parser.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation with --reset")
    parser.add_argument("--test", action="store_true", help="run all test suites")
    parser.add_argument("--launch", action="store_true", help="open the application window at the end")
    args = parser.parse_args()
    if args.reset and not args.yes and input("--reset deletes ALL products, inventory and recipes. Type 'yes' to continue: ").strip().lower() != "yes":
        print("Cancelled.")
        return 1

    results = []
    def step(label, ok, notes=""):
        results.append((label, "OK" if ok is True else ok or "ERROR", notes))
        return ok is not False

    def finish():
        print("\n" + "-" * 60 + "\nSUMMARY")
        for label, status, notes in results:
            print(f"  [{status:<8}] {label}" + (f"  ({notes})" if notes else ""))
        failed = any(status == "ERROR" for _, status, _ in results)
        print("\n" + ("Setup stopped because of an error." if failed else f"Project ready. Database: {DB_PATH}"))
        return 1 if failed else 0

    if not step("Database schema", run("meal_planner.db.schema")):
        return finish()
    existing = _count("PRODUCTS")
    if args.reset:
        if not (step("Demo products (reset)", _seed_step(seed_inventory.seed, True)) and
                step("Demo recipes (reset)", _seed_step(seed_recipes.seed, True))):
            return finish()
    else:
        if existing:
            step("Demo products", "SKIPPED", f"{existing} products already present")
        elif not step("Demo products", _seed_step(seed_inventory.seed, False)):
            return finish()
        if _count("RECIPES"):
            step("Demo recipes", "SKIPPED", "recipes already present")
        elif not demo_recipes_possible():
            step("Demo recipes", "SKIPPED", "demo products missing")
        elif not step("Demo recipes", _seed_step(seed_recipes.seed, False)):
            return finish()

    if not step("Database check", check_database()):
        return finish()
    if args.test:
        step("Tests", run_tests())
    code = finish()
    if args.launch and code == 0:
        print("\nOpening the window (close it to exit)...")
        run("meal_planner")
    return code


if __name__ == "__main__":
    sys.exit(main())
