"""Unified launcher script for Meal Planner receipt demonstrations."""

import argparse
from datetime import datetime
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from meal_planner.gui.app import App


class MockDropEvent:
    def __init__(self, data: str):
        self.data = data


def main():
    parser = argparse.ArgumentParser(description="Meal Planner Receipt Demonstration Launcher")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--drop", action="store_true", help="Simulate drag & drop of mock receipt onto pantry drop zone (default)")
    group.add_argument("--reconcile", action="store_true", help="Directly launch reconciliation dialog with mock parsed items")
    args = parser.parse_args()

    app = App()
    if args.reconcile:
        mock_items = [
            ("viaemilia 120 bologna", 1.0, "pcs", 1), ("penne rigate", 10.0, "kg", 1),
            ("latteintero", 10.0, "L", 1), ("banane", 0.9, "kg", 1),
            ("uova 6 pz", 1.0, "pcs", 1), ("yogurt bianco", 0.5, "kg", 1), ("totaleeuro", 1.0, "pcs", 1),
        ]
        app._last_receipt_var.set(f"Last receipt loaded: {datetime.now():%Y-%m-%d %H:%M:%S}")
        app.after(500, lambda: app._on_receipt_review_saved(mock_items))
    else:
        mock_path = str(ROOT / "mock_data" / "mock_receipt.png")
        def simulate_drop():
            app._receipt_log_write(f"[DEMO EVENT] Simulating Drag & Drop of: {Path(mock_path).name}\n")
            app._on_receipt_drop(MockDropEvent(mock_path))
        app.after(1200, simulate_drop)
    app.mainloop()


if __name__ == "__main__":
    main()
