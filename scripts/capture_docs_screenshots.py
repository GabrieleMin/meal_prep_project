"""Script to capture all documentation screenshots automatically via scrot."""

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from meal_planner.db.connection import get_connection
from meal_planner.gui.app import App
from meal_planner.gui.dialogs import (
    AddRecipeDialog, InventoryItemEditDialog, MacroPromptDialog, QuickProductDialog, ReceiptReconciliationDialog,
)

IMG_DIR = ROOT / "docs" / "images"
IMG_DIR.mkdir(parents=True, exist_ok=True)


def capture_window(widget, filename):
    widget.update_idletasks()
    widget.update()
    time.sleep(0.3)
    x, y = max(0, widget.winfo_rootx()), max(0, widget.winfo_rooty())
    w, h = widget.winfo_width(), widget.winfo_height()
    subprocess.run(["scrot", "-a", f"{x},{y},{w},{h}", str(IMG_DIR / filename)], check=True)
    print(f"[OK] Captured: {filename} ({w}x{h})")


def main():
    app = App()
    app.geometry("1100x750+50+50")
    app.update_idletasks()
    app.update()

    app.refresh(active_tab="Pantry")
    capture_window(app, "01_pantry_tab.png")

    mock_items = [
        ("viaemilia 120 bologna", 1.0, "pcs", 1), ("penne rigate", 10.0, "kg", 1),
        ("latteintero", 10.0, "L", 1), ("banane", 0.9, "kg", 1),
        ("uova 6 pz", 1.0, "pcs", 1), ("yogurt bianco", 0.5, "kg", 1),
    ]
    for dlg, geom, name in (
        (InventoryItemEditDialog(app, row_id=1, wait=False), "700x650+120+60", "02_item_edit_dialog.png"),
        (MacroPromptDialog(app, product_name="Penne Rigate", wait=False), "620x340+180+150", "03_macro_propagation_prompt.png"),
        (ReceiptReconciliationDialog(app, items=mock_items, wait=False), "1050x580+80+60", "04_receipt_reconciliation.png"),
    ):
        dlg.geometry(geom)
        capture_window(dlg, name)
        dlg.destroy()

    app.refresh(active_tab="Recipes")
    capture_window(app, "05_recipes_tab.png")

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

    qprod_dlg = QuickProductDialog(recipe_dlg, name="Farro Integrale Bio", suggested_unit="g", wait=False)
    qprod_dlg.geometry("480x420+200+120")
    capture_window(qprod_dlg, "07_on_the_fly_product.png")
    qprod_dlg.destroy()
    recipe_dlg.destroy()

    for tab, fname in (("Plan", "08_plan_tab.png"), ("Shopping", "09_shopping_tab.png")):
        app.refresh(active_tab=tab)
        capture_window(app, fname)

    with get_connection() as c:
        c.execute("DELETE FROM PROFILE")
        c.commit()
    app.refresh(active_tab="Profile")
    capture_window(app, "10_profile_empty_overlay.png")

    with get_connection() as c:
        c.execute("INSERT OR REPLACE INTO PROFILE (ID, Weight_kg, Height_cm, Age_years, Sex, Activity_Level, Goal, Kcal_target) "
                  "VALUES (1, 74.5, 178.0, 28, 'M', 'moderate', 'maintenance', 2450)")
        for dt, wt in (("2026-11-01", 75.5), ("2026-11-05", 75.0), ("2026-11-09", 74.5)):
            c.execute("INSERT OR REPLACE INTO WEIGHT_LOG (Date, Weight_kg) VALUES (?, ?)", (dt, wt))
        c.commit()
    app.refresh(active_tab="Profile")
    capture_window(app, "11_profile_configured.png")

    app.refresh(active_tab="Settings")
    for sec, fname in (("style_and_config", "12_settings_style.png"), ("full_profile", "13_settings_profile.png"),
                       ("receipt_logs", "14_settings_receipt_logs.png"), ("destructive_actions", "15_settings_destructive.png")):
        app.switch_settings_section(sec)
        capture_window(app, fname)

    app.set_theme("light")
    app.refresh(active_tab="Pantry")
    capture_window(app, "16_light_mode.png")

    app.set_theme("dark")
    with get_connection() as c:
        c.execute("DELETE FROM PROFILE")
        c.execute("DELETE FROM WEIGHT_LOG")
        c.commit()
    app.destroy()
    print("[ALL DONE] All 16 screenshots generated successfully in docs/images/.")


if __name__ == "__main__":
    main()
