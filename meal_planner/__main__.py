import os
import sys

from meal_planner.db.connection import DB_PATH
from meal_planner.gui import main

legacy = os.path.join(os.path.dirname(DB_PATH), "inventario.db")
if "MEAL_PLANNER_DB" not in os.environ and os.path.exists(legacy) and not os.path.exists(DB_PATH):
    print(f"Found a database from the earlier Italian-named version: {legacy}\n"
          f"To keep your data, convert it with:\n"
          f"    python -m scripts.migrate_legacy_db {legacy} {DB_PATH}\n", file=sys.stderr)

main()
