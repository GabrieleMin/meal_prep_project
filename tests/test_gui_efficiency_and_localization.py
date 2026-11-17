"""
tests/test_gui_efficiency_and_localization.py

Focused test suite for Milestone 3 GUI efficiency optimizations and English UI localization:
1. LineChart ISO date ordinal precomputation in plot() and debounced redraw on <Configure>.
2. ReceiptReconciliationDialog 26 pulse-flash ttk style caching across dialog sessions and themes.
3. ReceiptReconciliationDialog container-level mousewheel binding (elimination of per-row bindings).
4. AddRecipeDialog consolidated SQLite connection usage during init and ingredient entry.
5. App recipe tab refresh deferral when user is on other tabs (Pantry, Shopping).
6. App receipt log buffering via list in _receipt_log_write.
7. App and dialogs English UI localization:
   - "Unable to load image" in messagebox.showerror when OCR fails with Italian image loading error.
   - "Profile" active_tab refresh in open_profile_setup_dialog.
   - "Product " fallback label in RecipeDetailsDialog SQL query.
"""

from datetime import date
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import tkinter as tk
from tkinter import ttk

from meal_planner.db.connection import get_connection
from meal_planner.db.schema import create_schema, migrate
import meal_planner.config as cfg
from meal_planner.gui.widgets import LineChart
from meal_planner.gui.dialogs import (
    ReceiptReconciliationDialog,
    AddRecipeDialog,
    RecipeDetailsDialog,
    _RECONCILIATION_FLASH_STYLES,
)
from meal_planner.gui.app import App


class TestGuiEfficiencyAndLocalization(unittest.TestCase):
    """Unit and integration tests for Milestone 3 GUI efficiency and localization."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp.name
        self.tmp.close()

        self._orig_db_env = os.environ.get("MEAL_PLANNER_DB")
        os.environ["MEAL_PLANNER_DB"] = self.db_path

        conn = get_connection(self.db_path)
        create_schema(conn)
        migrate(conn)
        # Seed basic catalog
        conn.execute("INSERT OR REPLACE INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g) VALUES (1, 'kg', 350, 12, 70, 2)")
        conn.execute("INSERT OR REPLACE INTO ALIASES (Name, ID) VALUES ('Test Pasta', 1)")
        conn.execute("INSERT OR REPLACE INTO RECIPES (Recipe_ID, Name, Notes) VALUES (1, 'Test Recipe', 'Notes')")
        conn.execute("INSERT OR REPLACE INTO RECIPE_MEALS (Recipe_ID, Meal) VALUES (1, 'lunch')")
        conn.execute("INSERT OR REPLACE INTO RECIPE_INGREDIENTS (Row_ID, Recipe_ID, ID, Quantity, Unit, To_Taste) VALUES (1, 1, 1, 0.1, 'kg', 0)")
        conn.commit()
        conn.close()

        self.root = tk.Tk()
        self.root.withdraw()

        self._patch_err = patch("tkinter.messagebox.showerror")
        self._patch_info = patch("tkinter.messagebox.showinfo")
        self._patch_warn = patch("tkinter.messagebox.showwarning")
        self.mock_showerror = self._patch_err.start()
        self.mock_showinfo = self._patch_info.start()
        self.mock_warn = self._patch_warn.start()

    def tearDown(self):
        self._patch_err.stop()
        self._patch_info.stop()
        self._patch_warn.stop()

        try:
            self.root.destroy()
        except Exception:
            pass

        if self._orig_db_env is not None:
            os.environ["MEAL_PLANNER_DB"] = self._orig_db_env
        else:
            os.environ.pop("MEAL_PLANNER_DB", None)

        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except OSError:
                pass
        for extra in [self.db_path + "-wal", self.db_path + "-shm"]:
            if os.path.exists(extra):
                try:
                    os.remove(extra)
                except OSError:
                    pass

    # ==================================================================
    # 1. LineChart Optimizations
    # ==================================================================

    def test_line_chart_precomputes_ordinals_in_plot(self):
        """LineChart.plot() pre-computes date ordinals so draw() does not re-parse on resize."""
        chart = LineChart(self.root, height=120)
        points = [
            ("2026-01-01", 75.0),
            ("2026-01-02", 74.8),
            ("2026-01-03", 74.5),
        ]
        chart.plot(points, background="#222222", foreground="#ffffff")

        # Verify ordinals precomputed
        expected_ordinals = [date.fromisoformat(d).toordinal() for d, _ in points]
        self.assertEqual(chart._ordinals, expected_ordinals)

        # In draw(), verify date.fromisoformat is NOT called when _ordinals are present
        mock_date = MagicMock(wraps=date)
        with patch("meal_planner.gui.widgets.date", mock_date):
            chart.draw()
            self.assertEqual(mock_date.fromisoformat.call_count, 0, "draw() must not re-parse dates when ordinals are precomputed")

    def test_line_chart_debounces_configure_events(self):
        """Rapid <Configure> resize events are debounced with after_idle() rather than re-drawing synchronously."""
        chart = LineChart(self.root, height=120)
        points = [("2026-01-01", 70.0), ("2026-01-02", 69.5)]
        chart.plot(points, background="#111111", foreground="#ffffff")

        with patch.object(chart, "draw") as mock_draw:
            # Simulate 10 rapid configure events
            for _ in range(10):
                chart._on_configure()
            # Before idle processing, draw should NOT have been called 10 times synchronously
            self.assertEqual(mock_draw.call_count, 0)

            # Process pending idle callbacks
            chart.update_idletasks()
            self.assertEqual(mock_draw.call_count, 1, "Debounced draw should execute exactly once after idle tasks")

    # ==================================================================
    # 2. ReceiptReconciliationDialog Style Caching & Mousewheel
    # ==================================================================

    def test_receipt_reconciliation_dialog_caches_pulse_flash_styles(self):
        """ReceiptReconciliationDialog reuses cached PhotoImages and styles on second open."""
        items = [("Test Pasta", 0.5, "kg", 1)]

        # Clear cache for isolated test
        cache_key = (id(self.root.tk), True)
        if cache_key in _RECONCILIATION_FLASH_STYLES:
            del _RECONCILIATION_FLASH_STYLES[cache_key]

        # First dialog open
        dlg1 = ReceiptReconciliationDialog(self.root, items, db_path=self.db_path, wait=False)
        self.assertIn(cache_key, _RECONCILIATION_FLASH_STYLES)
        photos_first = _RECONCILIATION_FLASH_STYLES[cache_key]
        self.assertEqual(len(photos_first), 26)
        dlg1.destroy()

        # Second dialog open: PIL Image creation should not be triggered again
        with patch("PIL.Image.new") as mock_img_new:
            dlg2 = ReceiptReconciliationDialog(self.root, items, db_path=self.db_path, wait=False)
            self.assertEqual(mock_img_new.call_count, 0, "Reopening dialog must reuse cached styles without creating new PIL images")
            self.assertIs(dlg2._style_photos, photos_first, "Reopened dialog should reference cached photos")
            dlg2.destroy()

    def test_receipt_reconciliation_dialog_mousewheel_container_binding(self):
        """Mousewheel is bound at container/canvas level rather than 12 individual bindings per row."""
        items = [("Item 1", 1, "kg", 1), ("Item 2", 2, "kg", 1)]
        dlg = ReceiptReconciliationDialog(self.root, items, db_path=self.db_path, wait=False)
        try:
            # Check container, canvas, and table_frame have mousewheel
            self.assertTrue(dlg.canvas.bind("<MouseWheel>"))

            # Check that row entry widgets do NOT have redundant direct <MouseWheel> bindings
            for r in dlg.rows:
                ent = r["ent_weight"]
                # In standard Tkinter, unbound widget returns empty string for widget.bind('<MouseWheel>')
                mw_binding = ent.bind("<MouseWheel>")
                self.assertFalse(mw_binding, "Row widgets should not have redundant per-widget mousewheel bindings")
        finally:
            dlg.destroy()

    # ==================================================================
    # 3. AddRecipeDialog Consolidated Connection Usage
    # ==================================================================

    def test_add_recipe_dialog_consolidated_connections_on_init_and_add(self):
        """AddRecipeDialog consolidates SQLite connections during init and ingredient addition."""
        # Trace connection opens
        conn_open_count = 0
        from meal_planner.db import connection as db_conn
        orig_get_conn = db_conn.get_connection

        def counting_get_conn(*args, **kwargs):
            nonlocal conn_open_count
            conn_open_count += 1
            return orig_get_conn(*args, **kwargs)

        with patch("meal_planner.db.connection.get_connection", side_effect=counting_get_conn):
            dlg = AddRecipeDialog(self.root, db_path=self.db_path, wait=False)
            try:
                # Initialization should open connection only once to load meals and known ingredients
                self.assertEqual(conn_open_count, 1, "AddRecipeDialog init should use exactly 1 DB connection")

                # Adding an existing ingredient should consolidate check and macros calculation into 1 connection
                conn_open_count = 0
                dlg.v_ingredient_name.set("Test Pasta")
                dlg.v_quantity.set("200")
                dlg.v_unit.set("g")
                dlg.add_ingredient()
                self.assertEqual(conn_open_count, 1, "Adding known ingredient should use exactly 1 DB connection")
                self.assertEqual(len(dlg.ingredients), 1)
            finally:
                dlg.destroy()

    # ==================================================================
    # 4. App GUI Optimization & Recipe Tab Simulation Deferral
    # ==================================================================

    def test_app_defers_recipe_simulation_on_pantry_tab(self):
        """app.refresh() on Pantry tab defers full recipe simulation until Recipes tab is selected."""
        app = App()
        app.withdraw()
        app.db_path = self.db_path
        app.update_idletasks()

        try:
            # Initial launch is on Pantry tab (index 0)
            self.assertEqual(app.nb.tab(app.nb.select(), "text"), "Pantry")

            # Patch recipe_rows to verify it is NOT called during Pantry refresh
            with patch.object(app, "recipe_rows") as mock_recipe_rows:
                app.refresh(active_tab="Pantry")
                self.assertEqual(mock_recipe_rows.call_count, 0, "refresh('Pantry') must not simulate recipes")
                self.assertTrue(getattr(app, "_recipes_dirty", False))

                # Also generic app.refresh() while on Pantry tab must defer
                app.refresh()
                self.assertEqual(mock_recipe_rows.call_count, 0, "refresh() on Pantry tab must not simulate recipes")

            # Switching to Recipes tab should trigger the deferred simulation
            with patch.object(app, "recipe_rows", wraps=app.recipe_rows) as mock_recipe_rows:
                app.refresh(active_tab="Recipes")
                self.assertEqual(mock_recipe_rows.call_count, 1, "Switching to Recipes tab must execute simulation")
                self.assertFalse(getattr(app, "_recipes_dirty", False))
        finally:
            app.destroy()

    def test_app_receipt_log_buffered_in_list(self):
        """_receipt_log_write buffers entries in a list and property returns combined string."""
        app = App()
        app.withdraw()
        try:
            app._receipt_log_write("Line 1\n")
            app._receipt_log_write("Line 2\n")

            self.assertIsInstance(app._receipt_log_buffer, list)
            self.assertEqual(app._receipt_log_buffer, ["Line 1\n", "Line 2\n"])
            self.assertEqual(app._receipt_log_text, "Line 1\nLine 2\n")
            self.assertEqual(app._receipt_log.get(), "Line 1\nLine 2\n")
        finally:
            app.destroy()

    # ==================================================================
    # 5. Localization Verifications
    # ==================================================================

    def test_receipt_ocr_error_localized_to_english(self):
        """_on_receipt_read_completed translates Italian image loading error to 'Unable to load image'."""
        app = App()
        app.withdraw()
        try:
            app._on_receipt_read_completed(
                "receipt.jpg",
                [],
                RuntimeError("impossibile caricare l'immagine"),
            )
            self.mock_showerror.assert_called_with("Receipt Error", "Unable to load image")

            # Exact Italian title case error
            app._on_receipt_read_completed(
                "receipt.jpg",
                [],
                "Impossibile caricare l'immagine",
            )
            self.mock_showerror.assert_called_with("Receipt Error", "Unable to load image")
        finally:
            app.destroy()

    def test_profile_setup_dialog_refreshes_profile_in_english(self):
        """open_profile_setup_dialog invokes self.refresh(active_tab='Profile')."""
        app = App()
        app.withdraw()
        try:
            with patch("meal_planner.gui.app.ProfileSetupDialog") as MockDlg, \
                 patch.object(app, "refresh") as mock_refresh:
                mock_dlg_instance = MagicMock()
                mock_dlg_instance.result = {"Weight_kg": 70.0}
                MockDlg.return_value = mock_dlg_instance

                app.open_profile_setup_dialog()
                mock_refresh.assert_called_with(active_tab="Profile")
        finally:
            app.destroy()

    def test_recipe_details_dialog_sql_uses_english_product_fallback(self):
        """RecipeDetailsDialog uses English 'Product ' in ingredient fallback coalesce."""
        import inspect
        source = inspect.getsource(RecipeDetailsDialog._init_ui)
        self.assertIn("'Product ' || ri.ID", source)
        self.assertNotIn("'Prodotto ' || ri.ID", source)


if __name__ == "__main__":
    unittest.main()
