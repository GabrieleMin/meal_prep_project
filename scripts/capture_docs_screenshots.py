"""
Script to capture all documentation screenshots automatically.
Uses scrot to capture each view, dialog, and section cleanly.
"""

import os
import subprocess
import sys
import time
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from meal_planner.gui.app import App
from meal_planner.gui.dialogs import (
    InventoryItemEditDialog,
    MacroPromptDialog,
    ReceiptReconciliationDialog,
    QuickProductDialog,
    AddRecipeDialog,
)
from meal_planner.db.connection import get_connection

IMG_DIR = ROOT / "docs" / "images"
IMG_DIR.mkdir(parents=True, exist_ok=True)


def capture_window(widget, filename):
    widget.update_idletasks()
    widget.update()
    time.sleep(0.3)
    x = max(0, widget.winfo_rootx())
    y = max(0, widget.winfo_rooty())
    w = widget.winfo_width()
    h = widget.winfo_height()
    out_path = IMG_DIR / filename
    cmd = ["scrot", "-a", f"{x},{y},{w},{h}", str(out_path)]
    subprocess.run(cmd, check=True)
    print(f"[OK] Captured: {filename} ({w}x{h})")


def main():
    app = App()
    app.geometry("1100x750+50+50")
    app.update_idletasks()
    app.update()

    # 1. Pantry Tab (Dark Mode)
    app.refresh(active_tab="Pantry")
    capture_window(app, "01_pantry_tab.png")

    # 2. Item Edit Dialog
    edit_dlg = InventoryItemEditDialog(app, row_id=1, wait=False)
    edit_dlg.geometry("700x650+120+60")
    capture_window(edit_dlg, "02_item_edit_dialog.png")
    edit_dlg.destroy()

    # 3. Macro Propagation Prompt Dialog
    prop_dlg = MacroPromptDialog(app, product_name="Penne Rigate", wait=False)
    prop_dlg.geometry("620x340+180+150")
    capture_window(prop_dlg, "03_macro_propagation_prompt.png")
    prop_dlg.destroy()

    # 4. Receipt Reconciliation Dialog
    mock_items = [
        ("viaemilia 120 bologna", 1.0, "pcs", 1),
        ("penne rigate", 10.0, "kg", 1),
        ("latteintero", 10.0, "L", 1),
        ("banane", 0.9, "kg", 1),
        ("uova 6 pz", 1.0, "pcs", 1),
        ("yogurt bianco", 0.5, "kg", 1),
    ]
    rec_dlg = ReceiptReconciliationDialog(app, items=mock_items, wait=False)
    rec_dlg.geometry("1050x580+80+60")
    capture_window(rec_dlg, "04_receipt_reconciliation.png")
    rec_dlg.destroy()

    # 5. Recipes Tab
    app.refresh(active_tab="Recipes")
    capture_window(app, "05_recipes_tab.png")

    # 6. Add Recipe Dialog
    recipe_dlg = AddRecipeDialog(app, on_save_callback=lambda *a: None, wait=False)
    recipe_dlg.geometry("900x640+100+40")
    recipe_dlg.v_name.set("Pasta al Pomodoro e Basilico")
    recipe_dlg.v_meals["lunch"].set(True)
    recipe_dlg.v_meals["dinner"].set(True)
    recipe_dlg.v_ingredient_name.set("Penne Rigate")
    recipe_dlg.v_quantity.set("100")
    recipe_dlg.v_unit.set("g")
    recipe_dlg.add_ingredient()
    capture_window(recipe_dlg, "06_add_recipe_dialog.png")

    # 7. Quick Product Dialog (On the fly product creation)
    qprod_dlg = QuickProductDialog(recipe_dlg, name="Farro Integrale Bio", suggested_unit="g", wait=False)
    qprod_dlg.geometry("480x420+200+120")
    capture_window(qprod_dlg, "07_on_the_fly_product.png")
    qprod_dlg.destroy()
    recipe_dlg.destroy()

    # 8. Plan Tab
    app.refresh(active_tab="Plan")
    capture_window(app, "08_plan_tab.png")

    # 9. Shopping Tab
    app.refresh(active_tab="Shopping")
    capture_window(app, "09_shopping_tab.png")

    # 10. Profile Tab (Empty State Overlay)
    with get_connection() as c:
        c.execute("DELETE FROM PROFILE")
        c.commit()
    app.refresh(active_tab="Profilo")
    capture_window(app, "10_profile_empty_overlay.png")

    # 11. Profile Tab (Configured State)
    with get_connection() as c:
        c.execute("""
            INSERT OR REPLACE INTO PROFILE (
                ID, Weight_kg, Height_cm, Age_years, Sex, Activity_Level, Goal, Kcal_target
            ) VALUES (1, 74.5, 178.0, 28, 'M', 'moderate', 'maintenance', 2450)
        """)
        c.execute("INSERT OR REPLACE INTO WEIGHT_LOG (Date, Weight_kg) VALUES ('2026-11-01', 75.5)")
        c.execute("INSERT OR REPLACE INTO WEIGHT_LOG (Date, Weight_kg) VALUES ('2026-11-05', 75.0)")
        c.execute("INSERT OR REPLACE INTO WEIGHT_LOG (Date, Weight_kg) VALUES ('2026-11-09', 74.5)")
        c.commit()
    app.refresh(active_tab="Profilo")
    capture_window(app, "11_profile_configured.png")

    # 12. Settings Tab - Style & Config
    app.refresh(active_tab="Setting")
    app.switch_settings_section("style_and_config")
    capture_window(app, "12_settings_style.png")

    # 13. Settings Tab - Full Profile
    app.switch_settings_section("full_profile")
    capture_window(app, "13_settings_profile.png")

    # 14. Settings Tab - Receipt Logs
    app.switch_settings_section("receipt_logs")
    capture_window(app, "14_settings_receipt_logs.png")

    # 15. Settings Tab - Destructive Actions
    app.switch_settings_section("destructive_actions")
    capture_window(app, "15_settings_destructive.png")

    # 16. Light Mode Toggle
    app.set_theme("light")
    app.refresh(active_tab="Pantry")
    capture_window(app, "16_light_mode.png")

    # Restore clean state
    app.set_theme("dark")
    with get_connection() as c:
        c.execute("DELETE FROM PROFILE")
        c.execute("DELETE FROM WEIGHT_LOG")
        c.commit()

    app.destroy()
    print("[ALL DONE] All 16 screenshots generated successfully in docs/images/.")


if __name__ == "__main__":
    main()
