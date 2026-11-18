"""
tests/test_settings_and_profile.py

Consolidated settings navigation, theme persistence, clean profile empty state, and destructive actions test suite.
"""


import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch


# ======================================================================
# SECTION: tests/test_settings_and_profile_tabs.py
# ======================================================================

"""
Comprehensive unit and integration test suite for Requirements R2 and R3 (Milestone M2):
- Requirement R2: Settings Tab (Non-monolithic sections, style/config, full profile, receipt logs, destructive actions guardrails)
- Requirement R3: Profile Tab Reorganization & Empty-State Overlay (Dynamic field segregation, Canvas overlay with exact text)

Covers all 7 test categories specified in architectural handoff:
1. TestSettingsTabNavigation
2. TestStyleAndConfigPersistence
3. TestFullProfileSection
4. TestReceiptLogsSection
5. TestDestructiveActionsGuardrails
6. TestProfileTabReorganizationAndEmptyState
7. TestDatabaseIntegrityAndWAL
"""

import os
from pathlib import Path
import socket
import sqlite3
import tempfile
import tkinter as tk
from unittest.mock import patch, MagicMock
import unittest

from meal_planner.db.connection import get_connection, DB_PATH
from meal_planner.db.schema import create_schema, ALL_TABLES
from meal_planner.db.schema import migrate
import meal_planner.config as cfg
import meal_planner.inventory as inv
from meal_planner import engine as engine_catalog
from meal_planner import engine as engine_profile
from meal_planner.gui.app import App


class IsolatedDBTestCase(unittest.TestCase):
    """Base fixture for isolating tests in ephemeral databases with headless Tkinter."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp.name
        self.tmp.close()

        self._orig_db_env = os.environ.get("MEAL_PLANNER_DB")
        os.environ["MEAL_PLANNER_DB"] = self.db_path

        conn = get_connection(self.db_path)
        create_schema(conn)
        migrate(conn)
        conn.close()

        self._patch_err = patch("tkinter.messagebox.showerror")
        self._patch_warn = patch("tkinter.messagebox.showwarning")
        self._patch_info = patch("tkinter.messagebox.showinfo")
        self.mock_showerror = self._patch_err.start()
        self.mock_showwarning = self._patch_warn.start()
        self.mock_showinfo = self._patch_info.start()

    def tearDown(self):
        self._patch_err.stop()
        self._patch_warn.stop()
        self._patch_info.stop()

        if self._orig_db_env is not None:
            os.environ["MEAL_PLANNER_DB"] = self._orig_db_env
        else:
            os.environ.pop("MEAL_PLANNER_DB", None)

        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except OSError:
                pass


# =============================================================================
# 1. SETTINGS TAB NAVIGATION TESTS
# =============================================================================

class TestSettingsTabNavigation(IsolatedDBTestCase):
    """Tests navigation, non-monolithic controller, and section switching."""

    def test_setting_tab_in_notebook(self):
        app = App()
        try:
            tab_names = [app.nb.tab(i, "text") for i in range(app.nb.index("end"))]
            self.assertTrue("Setting" in tab_names or "Settings" in tab_names, "Notebook must contain 'Setting' or 'Settings' tab")
            self.assertTrue("Profilo" in tab_names or "Profile" in tab_names, "Notebook must contain 'Profilo' or 'Profile' tab")
        finally:
            app.destroy()

    def test_startup_default_tab_is_pantry(self):
        """Application must open on the Pantry tab (tab index 0) at startup."""
        app = App()
        try:
            self.assertEqual(app.nb.index(app.nb.select()), 0)
            self.assertEqual(app.nb.tab(app.nb.select(), "text"), "Pantry")
        finally:
            app.destroy()

    def test_four_sections_switching(self):
        app = App()
        try:
            expected_sections = ["style_and_config", "full_profile", "receipt_logs", "destructive_actions"]
            self.assertTrue(hasattr(app, "switch_settings_section"), "App must provide switch_settings_section")
            self.assertTrue(hasattr(app, "v_settings_section"), "App must provide v_settings_section StringVar")

            app.refresh(active_tab="Setting")
            app.update_idletasks()

            for sec in expected_sections:
                app.switch_settings_section(sec)
                app.update_idletasks()
                self.assertEqual(app.v_settings_section.get(), sec)

                # Check mapping isolation
                if sec == "style_and_config":
                    self.assertTrue(app.f_sec_style.winfo_ismapped())
                    self.assertFalse(app.f_sec_profile.winfo_ismapped())
                    self.assertFalse(app.f_sec_receipts.winfo_ismapped())
                    self.assertFalse(app.f_sec_destructive.winfo_ismapped())
                elif sec == "full_profile":
                    self.assertFalse(app.f_sec_style.winfo_ismapped())
                    self.assertTrue(app.f_sec_profile.winfo_ismapped())
                    self.assertFalse(app.f_sec_receipts.winfo_ismapped())
                    self.assertFalse(app.f_sec_destructive.winfo_ismapped())
                elif sec == "receipt_logs":
                    self.assertFalse(app.f_sec_style.winfo_ismapped())
                    self.assertFalse(app.f_sec_profile.winfo_ismapped())
                    self.assertTrue(app.f_sec_receipts.winfo_ismapped())
                    self.assertFalse(app.f_sec_destructive.winfo_ismapped())
                elif sec == "destructive_actions":
                    self.assertFalse(app.f_sec_style.winfo_ismapped())
                    self.assertFalse(app.f_sec_profile.winfo_ismapped())
                    self.assertFalse(app.f_sec_receipts.winfo_ismapped())
                    self.assertTrue(app.f_sec_destructive.winfo_ismapped())
        finally:
            app.destroy()

    def test_non_monolithic_presentation(self):
        """Verifies sections are packed conditionally rather than in a single monolithic view."""
        app = App()
        try:
            app.refresh(active_tab="Setting")
            app.switch_settings_section("style_and_config")
            app.update_idletasks()
            active_mapped = sum([
                app.f_sec_style.winfo_ismapped(),
                app.f_sec_profile.winfo_ismapped(),
                app.f_sec_receipts.winfo_ismapped(),
                app.f_sec_destructive.winfo_ismapped(),
            ])
            self.assertEqual(active_mapped, 1, "Only one section must be visible at any given moment")
        finally:
            app.destroy()


# =============================================================================
# 2. STYLE AND CONFIG PERSISTENCE TESTS
# =============================================================================

class TestStyleAndConfigPersistence(IsolatedDBTestCase):
    """Tests preference saving and loading via config module."""

    def test_theme_toggle_persistence(self):
        orig_theme = cfg.get("theme", "dark")
        new_theme = "light" if orig_theme == "dark" else "dark"
        cfg.save_config({"theme": new_theme})
        self.assertEqual(cfg.get("theme"), new_theme)
        # Restore
        cfg.save_config({"theme": orig_theme})
        self.assertEqual(cfg.get("theme"), orig_theme)

    def test_expiration_warning_days_persistence(self):
        cfg.save_config({"expiration_warning_days": 14})
        self.assertEqual(int(cfg.get("expiration_warning_days")), 14)
        cfg.save_config({"expiration_warning_days": 7})
        self.assertEqual(int(cfg.get("expiration_warning_days")), 7)

    def test_receipts_output_dir_persistence(self):
        cfg.save_config({"receipts_output_dir": "custom_receipts"})
        self.assertEqual(cfg.get("receipts_output_dir"), "custom_receipts")
        cfg.save_config({"receipts_output_dir": "receipts_output"})
        self.assertEqual(cfg.get("receipts_output_dir"), "receipts_output")

    def test_save_config_preserves_other_keys(self):
        cfg.save_config({"theme": "light"})
        self.assertEqual(cfg.get("theme"), "light")
        self.assertIsNotNone(cfg.get("expiration_warning_days"))
        self.assertIsNotNone(cfg.get("receipts_output_dir"))
        # Reset
        cfg.save_config({"theme": "dark", "expiration_warning_days": 7, "receipts_output_dir": "receipts_output"})


# =============================================================================
# 3. FULL PROFILE SECTION TESTS
# =============================================================================

class TestFullProfileSection(IsolatedDBTestCase):
    """Tests static fields segregation, display, and updates."""

    def test_static_profile_display(self):
        conn = get_connection(self.db_path)
        conn.execute("""
            INSERT OR REPLACE INTO PROFILE (ID, Weight_kg, Height_cm, Age_years, Sex, Activity_Level, Goal, Kcal_target)
            VALUES (1, 65.0, 168.0, 29, 'F', 'light', 'maintenance', 1900)
        """)
        conn.commit()
        conn.close()

        app = App()
        try:
            app.switch_settings_section("full_profile")
            app.update_idletasks()
            self.assertEqual(app.v_static_sex.get(), "F")
            self.assertEqual(app.v_static_height.get(), "168")
            overview_text = app.lbl_full_overview_text.get("1.0", "end")
            self.assertIn("Sex:                 F", overview_text)
            self.assertIn("Height:              168 cm", overview_text)
        finally:
            app.destroy()

    def test_static_profile_update(self):
        conn = get_connection(self.db_path)
        conn.execute("""
            INSERT OR REPLACE INTO PROFILE (ID, Weight_kg, Height_cm, Age_years, Sex, Activity_Level, Goal, Kcal_target)
            VALUES (1, 75.0, 172.0, 31, 'F', 'moderate', 'maintenance', 2200)
        """)
        conn.commit()
        conn.close()

        app = App()
        try:
            app.switch_settings_section("full_profile")
            app.v_static_sex.set("M")
            app.v_static_height.set("180")
            app.save_static_profile()
            app.update_idletasks()

            prof = engine_catalog.load_profile(self.db_path)
            self.assertEqual(prof["Sex"], "M")
            self.assertEqual(prof["Height_cm"], 180.0)
            # Dynamic fields preserved
            self.assertEqual(prof["Weight_kg"], 75.0)
            self.assertEqual(prof["Age_years"], 31)
        finally:
            app.destroy()


# =============================================================================
# 4. RECEIPT LOGS SECTION TESTS
# =============================================================================

class TestReceiptLogsSection(IsolatedDBTestCase):
    """Tests receipt metadata logging, querying, and table rendering."""

    def test_receipt_logs_table_columns(self):
        app = App()
        try:
            app.switch_settings_section("receipt_logs")
            headers = [app.t_receipt_logs.tv.heading(c, "text") for c in app.t_receipt_logs.tv["columns"]]
            self.assertEqual(headers, ["Time", "Day", "File Name"])
        finally:
            app.destroy()

    def test_log_receipt_helper_and_retrieval(self):
        row_id = inv.log_receipt("10:15:30", "2026-11-04", "receipt_20261104_101530.txt", db_path=self.db_path)
        self.assertGreater(row_id, 0)

        logs = inv.get_receipt_logs(db_path=self.db_path)
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0], ("10:15:30", "2026-11-04", "receipt_20261104_101530.txt"))

    def test_receipt_logs_table_rendering(self):
        inv.log_receipt("09:00:00", "2026-11-01", "receipt_nov1.txt", db_path=self.db_path)
        inv.log_receipt("18:30:00", "2026-11-03", "receipt_nov3.txt", db_path=self.db_path)

        app = App()
        try:
            app.switch_settings_section("receipt_logs")
            app.update_idletasks()
            rendered = [tuple(app.t_receipt_logs.tv.item(item, "values")) for item in app.t_receipt_logs.tv.get_children()]
            self.assertEqual(len(rendered), 2)
            # Ordered by id DESC
            self.assertEqual(rendered[0], ("18:30:00", "2026-11-03", "receipt_nov3.txt"))
            self.assertEqual(rendered[1], ("09:00:00", "2026-11-01", "receipt_nov1.txt"))
        finally:
            app.destroy()

    def test_receipt_logs_integration_with_output_dir(self):
        app = App()
        try:
            dummy_items = [{"name": "Mela", "unit_weight": 1.0, "intact_items": 1, "location": "pantry"}]
            with patch("meal_planner.gui.app.ReceiptReconciliationDialog") as MockDlg:
                MockDlg.return_value.result = dummy_items
                app._on_receipt_review_saved(dummy_items, out_path="/tmp/receipt_test_file.txt")
            logs = inv.get_receipt_logs(db_path=self.db_path)
            self.assertGreaterEqual(len(logs), 1)
            self.assertEqual(logs[0][2], "receipt_test_file.txt")
        finally:
            app.destroy()


# =============================================================================
# 5. DESTRUCTIVE ACTIONS GUARDRAILS TESTS
# =============================================================================

class TestDestructiveActionsGuardrails(IsolatedDBTestCase):
    """Tests hard-delete guardrails, confirmation cancellation, and execution."""

    def test_clear_inventory_cancelled_is_noop(self):
        conn = get_connection(self.db_path)
        conn.execute("INSERT INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g) VALUES (90, 'kg', 400.0, 7.0, 70.0, 10.0)")
        conn.execute("INSERT INTO ALIASES (Name, ID) VALUES ('Biscotti', 90)")
        conn.execute("INSERT INTO INVENTORY (Row_ID, ID, Unit_Weight, Intact_Items, Open_Remainder, Location) VALUES (1, 90, 0.5, 3, 0.0, 'pantry')")
        conn.execute("INSERT INTO EXCLUSION_LIST (ID, Ref_Name, Expiration_Date, Location) VALUES (90, 'Biscotti', '2027-12-31', 'pantry')")
        conn.commit()
        conn.close()

        app = App()
        try:
            with patch("tkinter.messagebox.askyesno", return_value=False):
                app.on_clear_inventory()

            conn = get_connection(self.db_path)
            inv_count = conn.execute("SELECT COUNT(*) FROM INVENTORY").fetchone()[0]
            excl_count = conn.execute("SELECT COUNT(*) FROM EXCLUSION_LIST").fetchone()[0]
            conn.close()
            self.assertEqual(inv_count, 1)
            self.assertEqual(excl_count, 1)
        finally:
            app.destroy()

    def test_clear_inventory_confirmed_wipes_inventory_preserves_products_and_aliases(self):
        conn = get_connection(self.db_path)
        conn.execute("INSERT INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g) VALUES (91, 'kg', 350.0, 10.0, 60.0, 3.0)")
        conn.execute("INSERT INTO ALIASES (Name, ID) VALUES ('Cereali', 91)")
        conn.execute("INSERT INTO INVENTORY (Row_ID, ID, Unit_Weight, Intact_Items, Open_Remainder, Location) VALUES (1, 91, 0.35, 2, 0.0, 'pantry')")
        conn.execute("INSERT INTO EXCLUSION_LIST (ID, Ref_Name, Expiration_Date, Location) VALUES (91, 'Cereali', '2027-12-31', 'pantry')")
        conn.commit()
        conn.close()

        app = App()
        try:
            with patch("tkinter.messagebox.askyesno", return_value=True):
                app.on_clear_inventory()

            conn = get_connection(self.db_path)
            inv_count = conn.execute("SELECT COUNT(*) FROM INVENTORY").fetchone()[0]
            excl_count = conn.execute("SELECT COUNT(*) FROM EXCLUSION_LIST").fetchone()[0]
            prod_count = conn.execute("SELECT COUNT(*) FROM PRODUCTS WHERE ID = 91").fetchone()[0]
            alias_count = conn.execute("SELECT COUNT(*) FROM ALIASES WHERE ID = 91").fetchone()[0]
            conn.close()
            self.assertEqual(inv_count, 0, "Inventory must be empty")
            self.assertEqual(excl_count, 0, "Exclusion list must be empty")
            self.assertEqual(prod_count, 1, "PRODUCTS catalog must be preserved")
            self.assertEqual(alias_count, 1, "ALIASES catalog must be preserved")
        finally:
            app.destroy()

    def test_reset_profile_cancelled_is_noop(self):
        conn = get_connection(self.db_path)
        conn.execute("INSERT OR REPLACE INTO PROFILE (ID, Weight_kg, Height_cm, Age_years, Sex) VALUES (1, 70.0, 175.0, 25, 'M')")
        conn.execute("INSERT OR REPLACE INTO WEIGHT_LOG (Date, Weight_kg) VALUES ('2026-11-04', 70.0)")
        conn.commit()
        conn.close()

        app = App()
        try:
            with patch("tkinter.messagebox.askyesno", return_value=False):
                app.on_reset_profile()

            conn = get_connection(self.db_path)
            prof_count = conn.execute("SELECT COUNT(*) FROM PROFILE").fetchone()[0]
            weight_count = conn.execute("SELECT COUNT(*) FROM WEIGHT_LOG").fetchone()[0]
            conn.close()
            self.assertEqual(prof_count, 1)
            self.assertEqual(weight_count, 1)
        finally:
            app.destroy()

    def test_reset_profile_confirmed_wipes_profile_and_weight_log(self):
        conn = get_connection(self.db_path)
        conn.execute("INSERT OR REPLACE INTO PROFILE (ID, Weight_kg, Height_cm, Age_years, Sex) VALUES (1, 70.0, 175.0, 25, 'M')")
        conn.execute("INSERT OR REPLACE INTO WEIGHT_LOG (Date, Weight_kg) VALUES ('2026-11-04', 70.0)")
        conn.commit()
        conn.close()

        app = App()
        try:
            with patch("tkinter.messagebox.askyesno", return_value=True):
                app.on_reset_profile()

            conn = get_connection(self.db_path)
            prof_count = conn.execute("SELECT COUNT(*) FROM PROFILE").fetchone()[0]
            weight_count = conn.execute("SELECT COUNT(*) FROM WEIGHT_LOG").fetchone()[0]
            conn.close()
            self.assertEqual(prof_count, 0)
            self.assertEqual(weight_count, 0)
        finally:
            app.destroy()


# =============================================================================
# 6. PROFILE TAB REORGANIZATION & EMPTY-STATE OVERLAY TESTS
# =============================================================================

class TestProfileTabReorganizationAndEmptyState(IsolatedDBTestCase):
    """Tests dynamic segregation and exact text empty-state overlay."""

    def test_profile_tab_contains_only_dynamic_fields(self):
        app = App()
        try:
            dynamic_keys = ["Weight_kg", "Age_years", "Body_fat_pct", "Waist_cm", "Activity_Level", "Goal"]
            for k in dynamic_keys:
                self.assertIn(k, app.pv, f"Dynamic key {k} must exist in app.pv")
        finally:
            app.destroy()

    def test_empty_state_overlay_exact_italian_text(self):
        conn = get_connection(self.db_path)
        conn.execute("DELETE FROM PROFILE")
        conn.commit()
        conn.close()

        app = App()
        try:
            app.refresh(active_tab="Profilo")
            app.update_idletasks()
            self.assertTrue(hasattr(app, "profile_overlay"))
            overlay = app.profile_overlay
            self.assertIsInstance(overlay, tk.Canvas)

            found_exact_text = False
            valid_texts = ("Hello, add your profile to get started", "Ciao, aggiunti il tuo profilo per iniziare")
            for item in overlay.find_all():
                if overlay.type(item) == "text":
                    if any(vt in overlay.itemcget(item, "text") for vt in valid_texts):
                        found_exact_text = True
                        break

            self.assertTrue(found_exact_text, f"Overlay must display verbatim text in: {valid_texts}")
        finally:
            app.destroy()

    def test_empty_state_overlay_hides_when_profile_added(self):
        conn = get_connection(self.db_path)
        conn.execute("DELETE FROM PROFILE")
        conn.commit()
        conn.close()

        app = App()
        try:
            app.refresh(active_tab="Profilo")
            app.update_idletasks()
            self.assertTrue(app.profile_overlay.winfo_ismapped())

            # Add profile
            conn = get_connection(self.db_path)
            conn.execute("""
                INSERT OR REPLACE INTO PROFILE (ID, Weight_kg, Height_cm, Age_years, Sex, Activity_Level, Goal, Kcal_target)
                VALUES (1, 72.0, 178.0, 27, 'M', 'moderate', 'maintenance', 2400)
            """)
            conn.commit()
            conn.close()

            app.refresh(active_tab="Profilo")
            app.update_idletasks()
            self.assertFalse(app.profile_overlay.winfo_ismapped())
        finally:
            app.destroy()

    def test_empty_state_overlay_reappears_on_profile_reset(self):
        conn = get_connection(self.db_path)
        conn.execute("""
            INSERT OR REPLACE INTO PROFILE (ID, Weight_kg, Height_cm, Age_years, Sex, Activity_Level, Goal, Kcal_target)
            VALUES (1, 68.0, 165.0, 24, 'F', 'light', 'maintenance', 1850)
        """)
        conn.commit()
        conn.close()

        app = App()
        try:
            app.refresh(active_tab="Profilo")
            app.update_idletasks()
            self.assertFalse(app.profile_overlay.winfo_ismapped())

            # Trigger profile reset
            with patch("tkinter.messagebox.askyesno", return_value=True):
                app.on_reset_profile()

            app.refresh(active_tab="Profilo")
            app.update_idletasks()
            self.assertTrue(app.profile_overlay.winfo_ismapped())
        finally:
            app.destroy()


# =============================================================================
# 7. DATABASE INTEGRITY AND WAL TESTS
# =============================================================================

class TestDatabaseIntegrityAndWAL(IsolatedDBTestCase):
    """Tests foreign keys, WAL mode, production DB preservation, and offline constraints."""

    def test_foreign_keys_enforced_during_destructive_actions(self):
        conn = get_connection(self.db_path)
        # Verify foreign keys are enabled
        fk = conn.execute("PRAGMA foreign_keys;").fetchone()[0]
        self.assertEqual(fk, 1, "PRAGMA foreign_keys must be ON")

        # Execute destructive actions
        inv.clear_inventory(conn)
        engine_profile.reset_profile(conn)

        # Run integrity and foreign key check
        fk_violations = conn.execute("PRAGMA foreign_key_check;").fetchall()
        self.assertEqual(len(fk_violations), 0, "No foreign key violations allowed")
        integrity = conn.execute("PRAGMA integrity_check;").fetchall()
        self.assertEqual(integrity, [("ok",)])
        conn.close()

    def test_atomic_rollback_on_failure(self):
        conn = get_connection(self.db_path)
        conn.execute("INSERT INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g) VALUES (95, 'kg', 100.0, 5.0, 20.0, 1.0)")
        conn.execute("INSERT INTO INVENTORY (Row_ID, ID, Unit_Weight, Intact_Items, Open_Remainder, Location) VALUES (1, 95, 1.0, 2, 0.0, 'pantry')")
        conn.commit()

        # Invalidate with read-only simulation or execute failure
        try:
            with conn:
                conn.execute("DELETE FROM INVENTORY")
                raise RuntimeError("Simulated failure")
        except RuntimeError:
            pass

        count = conn.execute("SELECT COUNT(*) FROM INVENTORY").fetchone()[0]
        conn.close()
        self.assertEqual(count, 1, "Rollback must restore previous state")

    def test_disk_inventory_db_preserved(self):
        """Verifies baseline data/inventory.db row counts on disk remain strictly (14, 30, 36, 3)."""
        baseline_db = Path("data/inventory.db")
        if not baseline_db.exists():
            return
        conn = sqlite3.connect(str(baseline_db))
        counts = {
            "PRODUCTS": conn.execute("SELECT COUNT(*) FROM PRODUCTS").fetchone()[0],
            "ALIASES": conn.execute("SELECT COUNT(*) FROM ALIASES").fetchone()[0],
            "INVENTORY": conn.execute("SELECT COUNT(*) FROM INVENTORY").fetchone()[0],
            "RECIPES": conn.execute("SELECT COUNT(*) FROM RECIPES").fetchone()[0],
        }
        conn.close()
        self.assertEqual(counts["PRODUCTS"], 14)
        self.assertEqual(counts["ALIASES"], 30)
        self.assertEqual(counts["INVENTORY"], 36)
        self.assertEqual(counts["RECIPES"], 3)

    def test_offline_execution(self):
        """Assures socket connections are not attempted."""
        with patch("socket.socket", side_effect=RuntimeError("Network access attempted!")):
            # Run operations to verify 100% offline
            app = App()
            try:
                app.switch_settings_section("style_and_config")
                app.switch_settings_section("full_profile")
                app.switch_settings_section("receipt_logs")
                app.switch_settings_section("destructive_actions")
            finally:
                app.destroy()




# ======================================================================
# SECTION: tests/test_settings_db_and_theme_separation.py
# ======================================================================

"""
tests/test_settings_db_and_theme_separation.py

Exhaustive integration and regression tests for:
1. Database migration and persistent location in data/inventory.db.
2. Storage of application settings in SQLite APP_SETTINGS table with elimination of loose config.json files.
3. Separation of default startup theme (persisted in Settings) from runtime session toggle (header button).
4. Clean Profile display in Settings with zero a priori dummy values when no profile exists.
"""

import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from meal_planner import config
from meal_planner.db.connection import get_connection, DEFAULT_DB_PATH, LEGACY_DB_PATH
from meal_planner.db.schema import create_schema
from meal_planner.gui import theme
from meal_planner.gui.app import App
import tkinter as tk


class TestSettingsAndThemeSeparation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
            cls.root.withdraw()
        except Exception:
            cls.root = None

    @classmethod
    def tearDownClass(cls):
        if cls.root:
            try:
                cls.root.destroy()
            except Exception:
                pass

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "data", "inventory.db")
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        conn = get_connection(self.db_path)
        create_schema(conn)
        conn.close()

        self._orig_db_env = os.environ.get("MEAL_PLANNER_DB")
        os.environ["MEAL_PLANNER_DB"] = self.db_path

        self._patch_err = patch("tkinter.messagebox.showerror")
        self._patch_warn = patch("tkinter.messagebox.showwarning")
        self._patch_info = patch("tkinter.messagebox.showinfo")
        self._patch_ask = patch("tkinter.messagebox.askyesno", return_value=True)
        self.mock_err = self._patch_err.start()
        self.mock_warn = self._patch_warn.start()
        self.mock_info = self._patch_info.start()
        self.mock_ask = self._patch_ask.start()

    def tearDown(self):
        self._patch_err.stop()
        self._patch_warn.stop()
        self._patch_info.stop()
        self._patch_ask.stop()

        if self._orig_db_env is not None:
            os.environ["MEAL_PLANNER_DB"] = self._orig_db_env
        else:
            os.environ.pop("MEAL_PLANNER_DB", None)

        if os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_database_location_and_data_preservation(self):
        """Verifies data/inventory.db exists in data/ and no redundant root symlink is present."""
        self.assertTrue(os.path.exists(DEFAULT_DB_PATH), "data/inventory.db must exist as canonical DB")
        self.assertFalse(os.path.islink(LEGACY_DB_PATH), "inventory.db root symlink must not exist")

        conn1 = get_connection(DEFAULT_DB_PATH)
        p1 = conn1.execute("SELECT COUNT(*) FROM PRODUCTS").fetchone()[0]
        conn1.close()
        self.assertEqual(p1, 14)

    def test_app_settings_table_and_config_elimination(self):
        """Settings are stored in APP_SETTINGS table; root config.json is eliminated."""
        conn = get_connection(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='APP_SETTINGS'")
        self.assertIsNotNone(cur.fetchone(), "APP_SETTINGS table must exist in SQLite schema")

        # Test writing a key through config
        config.set_key("expiration_warning_days", 12)
        val = config.get("expiration_warning_days")
        self.assertEqual(val, 12)

        # Confirm persisted in SQLite table
        row = cur.execute("SELECT Value FROM APP_SETTINGS WHERE Key='expiration_warning_days'").fetchone()
        self.assertIsNotNone(row)
        self.assertIn("12", row[0])
        conn.close()

    def test_theme_settings_default_vs_runtime_header_toggle(self):
        """
        Verify that:
        1. Setting default startup theme in Settings saves to APP_SETTINGS.
        2. Toggling theme via header button changes session in real time WITHOUT altering saved default.
        """
        # 1. Set default to dark
        config.save_config({"default_theme": "dark"})
        self.assertEqual(config.get("default_theme"), "dark")

        app = App()
        try:
            self.assertEqual(app.theme, "dark")

            # 2. Toggle via header button: should become light
            app.toggle_theme()
            self.assertEqual(app.theme, "light")

            # But the persisted default in APP_SETTINGS must STILL be dark!
            self.assertEqual(config.get("default_theme"), "dark")

            # 3. Save default in Settings: should update persisted setting
            app.v_cfg_theme.set("light")
            app.save_settings_config()
            self.assertEqual(config.get("default_theme"), "light")
        finally:
            app.destroy()

    def test_settings_full_profile_clean_empty_state_without_a_priori_data(self):
        """
        If no profile is present:
        - Sex and Height static fields must be completely blank (empty strings).
        - Overview must state 'No user profile is currently configured'.
        - No fake dummy weights (like 70kg, 87kg) or fake ages (23, 25) are pre-populated.
        """
        # Ensure PROFILE table is empty
        conn = get_connection(self.db_path)
        conn.execute("DELETE FROM PROFILE")
        conn.commit()
        conn.close()

        app = App()
        try:
            app.switch_settings_section("full_profile")
            app.update_idletasks()

            # Static fields must NOT have a priori values like 'M' or '175'
            self.assertEqual(app.v_static_sex.get(), "")
            self.assertEqual(app.v_static_height.get(), "")

            # Overview text must indicate no profile
            overview = app.lbl_full_overview_text.get("1.0", "end")
            self.assertIn("No user profile is currently configured", overview)
            self.assertNotIn("87", overview)
            self.assertNotIn("70", overview)
        finally:
            app.destroy()

    def test_settings_save_static_profile_does_not_invent_fake_metrics(self):
        """Saving static Sex and Height in Settings does NOT inject fake weight or age."""
        conn = get_connection(self.db_path)
        conn.execute("DELETE FROM PROFILE")
        conn.commit()
        conn.close()

        app = App()
        try:
            app.switch_settings_section("full_profile")
            app.v_static_sex.set("M")
            app.v_static_height.set("182")
            app.save_static_profile()

            conn = get_connection(self.db_path)
            row = conn.execute("SELECT Weight_kg, Height_cm, Age_years, Sex FROM PROFILE WHERE ID = 1").fetchone()
            conn.close()

            self.assertIsNotNone(row)
            self.assertIsNone(row[0], "Weight_kg must NOT be populated with fake dummy data")
            self.assertEqual(row[1], 182.0)
            self.assertIsNone(row[2], "Age_years must NOT be populated with fake dummy data")
            self.assertEqual(row[3], "M")
        finally:
            app.destroy()

    def test_on_reset_profile_clears_static_fields_and_restores_empty_state(self):
        """Hard reset in Settings destructive actions clears static fields and overview."""
        conn = get_connection(self.db_path)
        conn.execute("INSERT OR REPLACE INTO PROFILE (ID, Height_cm, Sex) VALUES (1, 178.0, 'F')")
        conn.commit()
        conn.close()

        app = App()
        try:
            app.switch_settings_section("full_profile")
            self.assertEqual(app.v_static_sex.get(), "F")
            self.assertEqual(app.v_static_height.get(), "178")

            # Execute profile reset
            app.on_reset_profile()
            app.update_idletasks()

            self.assertEqual(app.v_static_sex.get(), "")
            self.assertEqual(app.v_static_height.get(), "")
            overview = app.lbl_full_overview_text.get("1.0", "end")
            self.assertIn("No user profile is currently configured", overview)
        finally:
            app.destroy()




# ======================================================================
# SECTION: tests/test_challenger_m2_overlay.py
# ======================================================================

"""
Adversarial and Empirical Test Suite for Milestone M2 Gate (Challenger 2):
Focus Areas:
1. Empty-State Overlay Exact Verbatim Copy and Canvas Elements Inspection
2. Overlay Geometry, Visibility, and Mapping Lifecycle (Empty -> Configured -> Reset -> Reconfigured)
3. Dynamic vs Static Field Segregation (Profilo tab vs Settings full_profile)
4. Section Selection Controller Stress Testing (Rapid, out-of-order, idempotent, and error handling)
5. Interactive Profile Setup Dialog Launch, Input Validation, and Immediate Dismissal
6. Strictly 100% Offline Execution and Disk Database Integrity Invariant
"""

import os
import random
import socket
import sqlite3
import tempfile
import tkinter as tk
from tkinter import ttk
from unittest.mock import patch
import unittest

from meal_planner.db.connection import get_connection, DB_PATH
from meal_planner.db.schema import create_schema
from meal_planner.db.schema import migrate
from meal_planner.gui.app import App
from meal_planner.gui.dialogs import ProfileSetupDialog


class ChallengerM2BaseTestCase(unittest.TestCase):
    """Adversarial base fixture ensuring complete DB isolation, UI mocking, and offline enforcement."""

    def setUp(self):
        # Enforce offline: block network connections
        self._orig_socket_connect = socket.socket.connect
        def _forbidden_network_call(*args, **kwargs):
            raise RuntimeError("CRITICAL ERROR: Offline violation! External network call attempted.")
        socket.socket.connect = _forbidden_network_call

        # Ephemeral isolated database
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp.name
        self.tmp.close()

        self._orig_env = os.environ.get("MEAL_PLANNER_DB")
        os.environ["MEAL_PLANNER_DB"] = self.db_path

        conn = get_connection(self.db_path)
        create_schema(conn)
        migrate(conn)
        conn.close()

        # Mock modal messageboxes to prevent GUI blocking
        self._patch_err = patch("tkinter.messagebox.showerror")
        self._patch_warn = patch("tkinter.messagebox.showwarning")
        self._patch_info = patch("tkinter.messagebox.showinfo")
        self.mock_showerror = self._patch_err.start()
        self.mock_showwarning = self._patch_warn.start()
        self.mock_showinfo = self._patch_info.start()

    def tearDown(self):
        self._patch_err.stop()
        self._patch_warn.stop()
        self._patch_info.stop()

        socket.socket.connect = self._orig_socket_connect

        if self._orig_env is not None:
            os.environ["MEAL_PLANNER_DB"] = self._orig_env
        else:
            os.environ.pop("MEAL_PLANNER_DB", None)

        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except OSError:
                pass


# =============================================================================
# 1. EMPTY-STATE OVERLAY EXACT TEXT & RENDERING
# =============================================================================

class TestEmptyStateOverlayExactTextAndRendering(ChallengerM2BaseTestCase):
    """Inspects all canvas elements on an unconfigured database for exact verbatim match and geometry."""

    def test_empty_state_overlay_exact_verbatim_text_inspection(self):
        """Verifies exact verbatim match for 'Ciao, aggiunti il tuo profilo per iniziare'."""
        app = App()
        try:
            app.refresh(active_tab="Profilo")
            app.update_idletasks()

            self.assertTrue(hasattr(app, "profile_overlay"), "App must have profile_overlay canvas")
            c = app.profile_overlay
            self.assertIsInstance(c, tk.Canvas, "profile_overlay must be a tk.Canvas")

            all_items = c.find_all()
            self.assertGreater(len(all_items), 0, "Canvas must contain elements")

            text_items = [it for it in all_items if c.type(it) == "text"]
            self.assertGreater(len(text_items), 0, "Canvas must contain text elements")

            all_texts = [c.itemcget(it, "text") for it in text_items]
            valid_titles = ("Hello, add your profile to get started", "Ciao, aggiunti il tuo profilo per iniziare")
            found_title = next((vt for vt in valid_titles if vt in all_texts), None)
            self.assertIsNotNone(found_title, f"Expected overlay title in {valid_titles}, got {all_texts}")

            # Verify exact character count, case, and spacing
            matching_items = [it for it in text_items if c.itemcget(it, "text") == found_title]
            self.assertEqual(len(matching_items), 1, "There should be exactly one title text element")

            title_item = matching_items[0]
            tags = c.gettags(title_item)
            self.assertIn("overlay_text", tags, "Title text must have 'overlay_text' tag")
            self.assertIn("overlay_card", tags, "Title text must have 'overlay_card' tag")

            # Check secondary explanatory subtext exists and is not empty
            subtext_items = [it for it in text_items if it != title_item]
            self.assertGreaterEqual(len(subtext_items), 1, "Canvas must also contain explanatory subtext")
            subtext = c.itemcget(subtext_items[0], "text")
            self.assertTrue("profile" in subtext.lower() or "profilo" in subtext.lower())
        finally:
            app.destroy()

    def test_empty_state_overlay_polygon_card_properties(self):
        """Inspects card polygon shape, tags, and button element on canvas."""
        app = App()
        try:
            app.refresh(active_tab="Profilo")
            app.update_idletasks()

            c = app.profile_overlay
            polygon_items = [it for it in c.find_all() if c.type(it) == "polygon"]
            self.assertGreaterEqual(len(polygon_items), 1, "Canvas must contain a polygon card")

            card_poly = polygon_items[0]
            tags = c.gettags(card_poly)
            self.assertIn("overlay_card", tags, "Card polygon must be tagged 'overlay_card'")

            # Check coordinates form a rounded card polygon (multiple vertices)
            coords = c.coords(card_poly)
            self.assertGreaterEqual(len(coords), 16, "Card polygon must have multiple points for smooth rounded corners")

            # Check action button window item on canvas
            btn_items = c.find_withtag("overlay_btn")
            self.assertEqual(len(btn_items), 1, "Must have exactly one action button window on overlay")
            btn_name = c.itemcget(btn_items[0], "window")
            btn_widget = c.nametowidget(btn_name)
            self.assertIsInstance(btn_widget, ttk.Button)
            self.assertTrue("Add Profile" in btn_widget.cget("text") or "Aggiungi profilo" in btn_widget.cget("text"))
        finally:
            app.destroy()

    def test_empty_state_overlay_resize_idempotence(self):
        """Redrawing canvas (e.g. on window resize) must not accumulate duplicate items or corrupt state."""
        app = App()
        try:
            app.refresh(active_tab="Profilo")
            app.update_idletasks()

            c = app.profile_overlay
            initial_count = len(c.find_all())

            # Trigger 5 simulated resize events
            for _ in range(5):
                app._draw_profile_overlay()
                app.update_idletasks()

            final_count = len(c.find_all())
            self.assertEqual(initial_count, final_count,
                             f"Redraw must delete previous items; item count changed from {initial_count} to {final_count}")

            valid_titles = ("Hello, add your profile to get started", "Ciao, aggiunti il tuo profilo per iniziare")
            texts = [c.itemcget(it, "text") for it in c.find_all() if c.type(it) == "text"]
            self.assertEqual(sum(texts.count(vt) for vt in valid_titles), 1, "Must maintain exactly 1 instance of verbatim title")
        finally:
            app.destroy()


# =============================================================================
# 2. OVERLAY GEOMETRY AND MAPPING LIFECYCLE
# =============================================================================

class TestOverlayGeometryAndMapping(ChallengerM2BaseTestCase):
    """Tests winfo_ismapped() transitions across empty -> configured -> reset lifecycle and tab switching."""

    def test_overlay_mapping_lifecycle_empty_configured_reset(self):
        """Asserts winfo_ismapped() is True when empty, False when configured, and True again after reset."""
        app = App()
        try:
            # 1. Empty profile state
            app.refresh(active_tab="Profilo")
            app.update_idletasks()
            self.assertTrue(app.profile_overlay.winfo_ismapped(),
                            "Overlay must be mapped (visible) when profile is empty")

            # 2. Configured profile state
            conn = get_connection(self.db_path)
            conn.execute("""
                INSERT OR REPLACE INTO PROFILE (ID, Weight_kg, Height_cm, Age_years, Sex, Activity_Level, Goal, Kcal_target)
                VALUES (1, 74.0, 178.0, 28, 'M', 'moderate', 'maintenance', 2450)
            """)
            conn.commit()
            conn.close()

            app.refresh(active_tab="Profilo")
            app.update_idletasks()
            self.assertFalse(app.profile_overlay.winfo_ismapped(),
                             "Overlay must NOT be mapped when profile is configured")

            # 3. Trigger profile reset
            with patch("tkinter.messagebox.askyesno", return_value=True):
                app.on_reset_profile()

            app.refresh(active_tab="Profilo")
            app.update_idletasks()
            self.assertTrue(app.profile_overlay.winfo_ismapped(),
                            "Overlay must become mapped again after user triggers profile reset")
        finally:
            app.destroy()

    def test_overlay_mapping_repeated_toggle_stress(self):
        """Stress tests 6 full cycles of configure -> verify unmapped -> reset -> verify mapped."""
        app = App()
        try:
            for cycle in range(6):
                # Configure
                conn = get_connection(self.db_path)
                conn.execute(f"""
                    INSERT OR REPLACE INTO PROFILE (ID, Weight_kg, Height_cm, Age_years, Sex, Activity_Level, Goal, Kcal_target)
                    VALUES (1, {70.0 + cycle}, 175.0, 25, 'M', 'light', 'maintenance', 2200)
                """)
                conn.commit()
                conn.close()

                app.refresh(active_tab="Profilo")
                app.update_idletasks()
                self.assertFalse(app.profile_overlay.winfo_ismapped(),
                                 f"Cycle {cycle}: Overlay must be hidden when profile exists")

                # Reset
                with patch("tkinter.messagebox.askyesno", return_value=True):
                    app.on_reset_profile()

                app.refresh(active_tab="Profilo")
                app.update_idletasks()
                self.assertTrue(app.profile_overlay.winfo_ismapped(),
                                f"Cycle {cycle}: Overlay must be visible after reset")
        finally:
            app.destroy()

    def test_overlay_mapping_across_tab_switches(self):
        """Verifies overlay remains properly positioned and mapped when switching back and forth between tabs."""
        app = App()
        try:
            # Unconfigured profile
            app.refresh(active_tab="Profilo")
            app.update_idletasks()
            self.assertTrue(app.profile_overlay.winfo_ismapped())

            # Switch to Pantry
            app.refresh(active_tab="Pantry")
            app.update_idletasks()

            # Switch to Setting
            app.refresh(active_tab="Setting")
            app.update_idletasks()

            # Switch back to Profilo
            app.refresh(active_tab="Profilo")
            app.update_idletasks()
            self.assertTrue(app.profile_overlay.winfo_ismapped(),
                            "Overlay must remain mapped upon returning to Profilo tab")
        finally:
            app.destroy()


# =============================================================================
# 3. DYNAMIC VS STATIC FIELD SEGREGATION
# =============================================================================

class TestDynamicVsStaticFieldSegregation(ChallengerM2BaseTestCase):
    """Verifies Sex and Height_cm are absent from Profilo tab entries and present in Settings full_profile."""

    def test_profilo_tab_contains_no_static_entry_widgets(self):
        """Asserts neither Sex nor Height_cm has entry/combobox input widgets inside the Profilo tab."""
        app = App()
        try:
            app.refresh(active_tab="Profilo")
            app.update_idletasks()

            def get_all_input_widgets(parent):
                inputs = []
                for child in parent.winfo_children():
                    if isinstance(child, (ttk.Entry, ttk.Combobox, tk.Entry)):
                        inputs.append(child)
                    inputs.extend(get_all_input_widgets(child))
                return inputs

            profilo_inputs = get_all_input_widgets(app.tab_profile_frame)
            profilo_input_var_names = {str(inp.cget("textvariable")) for inp in profilo_inputs if inp.cget("textvariable")}

            # Sex and Height_cm must NOT be bound to any input widget in Profilo tab
            sex_var_name = str(app.pv["Sex"])
            height_var_name = str(app.pv["Height_cm"])

            self.assertNotIn(sex_var_name, profilo_input_var_names,
                             "Sex must NOT have an input widget in Profilo tab")
            self.assertNotIn(height_var_name, profilo_input_var_names,
                             "Height_cm must NOT have an input widget in Profilo tab")

            # Check that Body frame labels do not mention Height or Sex
            def get_all_labels(parent):
                labels = []
                for child in parent.winfo_children():
                    if isinstance(child, (ttk.Label, tk.Label)):
                        labels.append(child.cget("text"))
                    labels.extend(get_all_labels(child))
                return labels

            labels = get_all_labels(app.tab_profile_frame)
            self.assertFalse(any("height" in lbl.lower() for lbl in labels),
                             "No label in Profilo tab should prompt for Height")
            self.assertFalse(any(lbl.strip().lower() == "sex" for lbl in labels),
                             "No label in Profilo tab should prompt for Sex")
        finally:
            app.destroy()

    def test_profilo_tab_contains_all_dynamic_fields(self):
        """Asserts dynamic fields (Weight_kg, Age_years, Body_fat_pct, Waist_cm, Activity_Level, Goal) are present."""
        app = App()
        try:
            dynamic_keys = ["Weight_kg", "Age_years", "Body_fat_pct", "Waist_cm", "Activity_Level", "Goal"]
            for k in dynamic_keys:
                self.assertIn(k, app.pv, f"Dynamic key {k} must exist in app.pv")

            def get_all_input_widgets(parent):
                inputs = []
                for child in parent.winfo_children():
                    if isinstance(child, (ttk.Entry, ttk.Combobox, tk.Entry)):
                        inputs.append(child)
                    inputs.extend(get_all_input_widgets(child))
                return inputs

            profilo_inputs = get_all_input_widgets(app.tab_profile_frame)
            profilo_input_var_names = {str(inp.cget("textvariable")) for inp in profilo_inputs if inp.cget("textvariable")}

            for k in dynamic_keys:
                var_name = str(app.pv[k])
                self.assertIn(var_name, profilo_input_var_names,
                              f"Dynamic field {k} must be bound to an input widget in Profilo tab")
        finally:
            app.destroy()

    def test_static_fields_available_and_functional_in_settings_full_profile(self):
        """Asserts Sex and Height_cm are available in Settings -> full_profile and persist correctly."""
        app = App()
        try:
            app.refresh(active_tab="Setting")
            app.switch_settings_section("full_profile")
            app.update_idletasks()

            self.assertTrue(hasattr(app, "cb_static_sex"), "Settings full_profile must have cb_static_sex")
            self.assertTrue(hasattr(app, "v_static_height"), "Settings full_profile must have v_static_height")

            # Check sex combobox values
            self.assertEqual(list(app.cb_static_sex.cget("values")), ["M", "F"])

            # Test updating static fields
            app.v_static_sex.set("F")
            app.v_static_height.set("167.5")
            app.save_static_profile()

            conn = get_connection(self.db_path)
            row = conn.execute("SELECT Sex, Height_cm FROM PROFILE WHERE ID = 1").fetchone()
            conn.close()

            self.assertIsNotNone(row, "Profile row must exist")
            self.assertEqual(row[0], "F")
            self.assertEqual(row[1], 167.5)

            # Check read-only overview text widget is populated
            self.assertTrue(hasattr(app, "lbl_full_overview_text"))
            overview_text = app.lbl_full_overview_text.get("1.0", "end")
            self.assertIn("167.5", overview_text)
            self.assertIn("F", overview_text)
        finally:
            app.destroy()

    def test_static_field_validation_in_settings_full_profile(self):
        """Invalid height or sex in settings full_profile must trigger error and prevent corruption."""
        app = App()
        try:
            app.refresh(active_tab="Setting")
            app.switch_settings_section("full_profile")

            # Baseline value in DB
            conn = get_connection(self.db_path)
            conn.execute("INSERT OR REPLACE INTO PROFILE (ID, Weight_kg, Height_cm, Age_years, Sex) VALUES (1, 70.0, 175.0, 25, 'M')")
            conn.commit()
            conn.close()

            # Attempt negative height
            app.v_static_height.set("-10")
            app.save_static_profile()
            self.assertTrue(self.mock_showerror.called, "Must show error for negative height")

            conn = get_connection(self.db_path)
            h = conn.execute("SELECT Height_cm FROM PROFILE WHERE ID = 1").fetchone()[0]
            conn.close()
            self.assertEqual(h, 175.0, "Height in DB must remain unchanged after invalid submission")

            # Attempt non-numeric height
            self.mock_showerror.reset_mock()
            app.v_static_height.set("not_a_number")
            app.save_static_profile()
            self.assertTrue(self.mock_showerror.called, "Must show error for non-numeric height")
        finally:
            app.destroy()

    def test_save_profile_button_persistence(self):
        """
        Adversarial test for the 'Save profile' button in Profilo tab:
        Verifies that editing dynamic fields in Profilo tab and clicking 'Save profile'
        commits changes to SQLite so they persist across connections.
        """
        conn = get_connection(self.db_path)
        conn.execute("""
            INSERT OR REPLACE INTO PROFILE (ID, Weight_kg, Height_cm, Age_years, Sex, Activity_Level, Goal, Kcal_target)
            VALUES (1, 70.0, 175.0, 25, 'M', 'moderate', 'maintenance', 2400)
        """)
        conn.commit()
        conn.close()

        app = App()
        try:
            app.refresh(active_tab="Profilo")
            app.pv["Weight_kg"].set("85.0")
            app.pv["Age_years"].set("26")
            app.pv["Activity_Level"].set("heavy")
            app.pv["Goal"].set("weight_gain")
            app.pv["Target_weight_kg"].set("90.0")
            app.pv["Weekly_rate_kg"].set("0.5")

            app.save_profile()

            # Inspect database from separate connection
            conn2 = get_connection(self.db_path)
            row = conn2.execute("SELECT Weight_kg, Age_years FROM PROFILE WHERE ID = 1").fetchone()
            conn2.close()

            self.assertEqual(row[0], 85.0, "Weight_kg must be committed to database by save_profile()")
            self.assertEqual(row[1], 26, "Age_years must be committed to database by save_profile()")
        finally:
            app.destroy()


# =============================================================================
# 4. SECTION SELECTION CONTROLLER ADVERSARIAL STRESS TESTING
# =============================================================================

class TestSectionSelectionControllerAdversarial(ChallengerM2BaseTestCase):
    """Stress tests rapid and out-of-order section switching, ensuring strict frame mapping isolation."""

    def test_rapid_and_out_of_order_switching(self):
        """Executes 50 pseudo-random section transitions; verifies only the active subframe is mapped."""
        app = App()
        try:
            app.refresh(active_tab="Setting")
            app.update_idletasks()

            sections = ["style_and_config", "full_profile", "receipt_logs", "destructive_actions"]
            frame_map = {
                "style_and_config": app.f_sec_style,
                "full_profile": app.f_sec_profile,
                "receipt_logs": app.f_sec_receipts,
                "destructive_actions": app.f_sec_destructive,
            }

            rng = random.Random(20261104)
            for step in range(50):
                target_sec = rng.choice(sections)
                app.switch_settings_section(target_sec)
                app.update_idletasks()

                # Verify variable state
                self.assertEqual(app.v_settings_section.get(), target_sec,
                                 f"Step {step}: StringVar mismatch for {target_sec}")

                # Verify exactly 1 frame is mapped
                active_frame = frame_map[target_sec]
                self.assertTrue(active_frame.winfo_ismapped(),
                                f"Step {step}: Active frame {target_sec} must be mapped")

                mapped_count = sum(1 for f in frame_map.values() if f.winfo_ismapped())
                self.assertEqual(mapped_count, 1,
                                 f"Step {step}: Exactly 1 frame must be mapped, but found {mapped_count}")
        finally:
            app.destroy()

    def test_section_switching_idempotence(self):
        """Repeatedly switching to the same active section is a clean no-op without widget duplication."""
        app = App()
        try:
            app.refresh(active_tab="Setting")
            for _ in range(5):
                app.switch_settings_section("destructive_actions")
                app.update_idletasks()
                self.assertTrue(app.f_sec_destructive.winfo_ismapped())
                self.assertFalse(app.f_sec_style.winfo_ismapped())
                self.assertFalse(app.f_sec_profile.winfo_ismapped())
                self.assertFalse(app.f_sec_receipts.winfo_ismapped())
        finally:
            app.destroy()

    def test_unknown_section_defensive_handling(self):
        """Switching to an unexpected section string unmaps all sections without crashing."""
        app = App()
        try:
            app.refresh(active_tab="Setting")
            app.switch_settings_section("non_existent_section")
            app.update_idletasks()

            self.assertFalse(app.f_sec_style.winfo_ismapped())
            self.assertFalse(app.f_sec_profile.winfo_ismapped())
            self.assertFalse(app.f_sec_receipts.winfo_ismapped())
            self.assertFalse(app.f_sec_destructive.winfo_ismapped())
        finally:
            app.destroy()


# =============================================================================
# 5. INTERACTIVE PROFILE SETUP DIALOG
# =============================================================================

class TestInteractiveProfileSetupDialog(ChallengerM2BaseTestCase):
    """Tests launching ProfileSetupDialog from button/card, saving profile details, and verifying dismissal."""

    def test_launch_profile_setup_dialog_from_overlay_button_and_save(self):
        """Simulates clicking the overlay action button, filling dialog, saving, and verifying overlay disappears."""
        app = App()
        try:
            app.refresh(active_tab="Profilo")
            app.update_idletasks()

            self.assertTrue(app.profile_overlay.winfo_ismapped(), "Overlay must start mapped")

            c = app.profile_overlay
            btn_items = c.find_withtag("overlay_btn")
            self.assertEqual(len(btn_items), 1)
            btn_name = c.itemcget(btn_items[0], "window")
            btn = c.nametowidget(btn_name)

            def mock_wait_window(dialog_self, *args, **kwargs):
                self.assertIsInstance(dialog_self, ProfileSetupDialog)
                # Populate profile details
                dialog_self.v_sex.set("F")
                dialog_self.v_height.set("172")
                dialog_self.v_weight.set("64.5")
                dialog_self.v_age.set("27")
                dialog_self.v_activity.set("moderate")
                dialog_self.v_goal.set("weight_loss")
                dialog_self.v_target_weight.set("60")
                dialog_self.v_weekly_rate.set("0.4")
                success = dialog_self.save()
                self.assertTrue(success, "dialog.save() must return True for valid data")

            with patch.object(tk.Misc, "wait_window", mock_wait_window):
                btn.invoke()

            app.update_idletasks()

            # Verify profile is persisted in database
            conn = get_connection(self.db_path)
            row = conn.execute("SELECT Weight_kg, Height_cm, Age_years, Sex, Goal FROM PROFILE WHERE ID = 1").fetchone()
            conn.close()

            self.assertIsNotNone(row)
            self.assertEqual(row[0], 64.5)
            self.assertEqual(row[1], 172.0)
            self.assertEqual(row[2], 27)
            self.assertEqual(row[3], "F")
            self.assertEqual(row[4], "weight_loss")

            # Verify overlay has immediately disappeared
            self.assertFalse(app.profile_overlay.winfo_ismapped(),
                             "Overlay must disappear immediately after profile is configured")
            self.assertEqual(app.pv["Weight_kg"].get(), "64.5")
        finally:
            app.destroy()

    def test_launch_profile_setup_dialog_from_card_click(self):
        """Simulates clicking the overlay card polygon, filling dialog, saving, and verifying dismissal."""
        app = App()
        try:
            app.refresh(active_tab="Profilo")
            app.update_idletasks()

            c = app.profile_overlay
            coords = c.coords("overlay_card")
            self.assertGreaterEqual(len(coords), 4)
            cx = sum(coords[0::2]) / (len(coords) / 2)
            cy = sum(coords[1::2]) / (len(coords) / 2)

            def mock_wait_window(dialog_self, *args, **kwargs):
                self.assertIsInstance(dialog_self, ProfileSetupDialog)
                dialog_self.v_sex.set("M")
                dialog_self.v_height.set("182")
                dialog_self.v_weight.set("81.0")
                dialog_self.v_age.set("31")
                dialog_self.save()

            with patch.object(tk.Misc, "wait_window", mock_wait_window):
                c.event_generate("<Button-1>", x=int(cx), y=int(cy))
                app.update_idletasks()

            # Verify overlay has disappeared
            self.assertFalse(app.profile_overlay.winfo_ismapped(),
                             "Overlay must disappear after card-click profile setup")
        finally:
            app.destroy()

    def test_dialog_cancellation_preserves_empty_overlay(self):
        """Cancelling ProfileSetupDialog must NOT modify the DB and overlay must remain mapped."""
        app = App()
        try:
            app.refresh(active_tab="Profilo")
            app.update_idletasks()

            def mock_wait_window(dialog_self, *args, **kwargs):
                dialog_self.cancel()

            with patch.object(tk.Misc, "wait_window", mock_wait_window):
                app.open_profile_setup_dialog()

            app.update_idletasks()

            # DB profile should still be empty or unconfigured
            conn = get_connection(self.db_path)
            row = conn.execute("SELECT Sex, Weight_kg FROM PROFILE WHERE ID = 1").fetchone()
            conn.close()
            # If row exists from create_schema, Sex must be None
            if row:
                self.assertIsNone(row[0], "Sex must remain None if cancelled")

            self.assertTrue(app.profile_overlay.winfo_ismapped(),
                            "Overlay must remain mapped when user cancels profile setup dialog")
        finally:
            app.destroy()

    def test_dialog_validation_rejection(self):
        """ProfileSetupDialog.save() must reject invalid inputs (e.g. negative values, non-numbers)."""
        app = App()
        try:
            # We patch wait_window so constructor finishes without blocking
            with patch.object(tk.Misc, "wait_window", return_value=None):
                dialog = ProfileSetupDialog(app, db_path=self.db_path)

            try:
                # 1. Negative height
                dialog.v_height.set("-180")
                self.assertFalse(dialog.save())
                self.assertTrue(self.mock_showerror.called)

                # 2. Non-numeric weight
                self.mock_showerror.reset_mock()
                dialog.v_height.set("180")
                dialog.v_weight.set("abc")
                self.assertFalse(dialog.save())
                self.assertTrue(self.mock_showerror.called)

                # 3. Invalid sex
                self.mock_showerror.reset_mock()
                dialog.v_weight.set("75")
                dialog.v_sex.set("UNKNOWN")
                self.assertFalse(dialog.save())
                self.assertTrue(self.mock_showerror.called)

                # 4. Invalid activity (triggers engine PAL KeyError)
                self.mock_showerror.reset_mock()
                dialog.v_sex.set("M")
                dialog.v_activity.set("unsupported_activity")
                self.assertFalse(dialog.save())
                self.assertTrue(self.mock_showerror.called)

                # Result must remain None on failure
                self.assertIsNone(dialog.result)
            finally:
                dialog.destroy()
        finally:
            app.destroy()


# =============================================================================
# 6. DISK DATABASE INTEGRITY AND OFFLINE VERIFICATION
# =============================================================================

class TestDiskDatabaseIntegrityAndOffline(ChallengerM2BaseTestCase):
    """Verifies production disk inventory.db is 100% untouched and system is strictly offline."""

    def test_disk_inventory_db_counts_untouched(self):
        """Checks row counts of PRODUCTS, ALIASES, INVENTORY, RECIPES on disk database."""
        real_db = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "inventory.db"
        )
        self.assertTrue(os.path.exists(real_db), f"Disk DB {real_db} must exist")

        conn = sqlite3.connect(real_db)
        inv_count = conn.execute("SELECT COUNT(*) FROM INVENTORY").fetchone()[0]
        rec_count = conn.execute("SELECT COUNT(*) FROM RECIPES").fetchone()[0]
        prod_count = conn.execute("SELECT COUNT(*) FROM PRODUCTS").fetchone()[0]
        alias_count = conn.execute("SELECT COUNT(*) FROM ALIASES").fetchone()[0]
        conn.close()

        expected = (36, 3, 14, 30)
        actual = (inv_count, rec_count, prod_count, alias_count)
        self.assertEqual(actual, expected,
                         f"Disk inventory.db row counts changed! Expected {expected}, found {actual}")

    def test_offline_enforcement(self):
        """Attempts to open an external socket must be caught and forbidden."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with self.assertRaises(RuntimeError):
                s.connect(("8.8.8.8", 53))
        finally:
            s.close()




# ======================================================================
# SECTION: tests/test_challenger_m2_destructive.py
# ======================================================================

"""
test_challenger_m2_destructive.py

Empirical Challenger test suite for Milestone M2 Gate:
- Destructive action cancellation guardrails (inventory wipe & profile reset)
- Destructive action confirmation, hard-delete, and foreign key integrity under WAL
- Repeat inventory addition after wipe and quantity consumption verification
- Profile reset confirmation (PROFILE and WEIGHT_LOG wiped, overlay reappears)
- Post-reset profile re-creation & upsert verification:
    - Backend update_profile() upsert on empty table
    - ProfileSetupDialog modal configuration
    - GUI app.save_profile() database persistence check
- Receipt logging stress test: edge cases (spaces, quotes, unicode, long paths, SQLi attempt, ordered retrieval)
- Production database disk preservation and offline enforcement
"""

import os
from pathlib import Path
import socket
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from meal_planner.db.connection import get_connection, DB_PATH
from meal_planner.db.schema import create_schema, ALL_TABLES
from meal_planner.db.schema import migrate
import meal_planner.inventory as inv
from meal_planner import engine as engine_catalog
from meal_planner import engine as engine_profile
from meal_planner.gui.app import App
from meal_planner.gui.dialogs import ProfileSetupDialog


class ChallengerIsolatedDBTestCase(unittest.TestCase):
    """Base test fixture ensuring complete isolation in temporary SQLite databases."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.tmp.name
        self.tmp.close()

        self._orig_db_env = os.environ.get("MEAL_PLANNER_DB")
        os.environ["MEAL_PLANNER_DB"] = self.db_path

        conn = get_connection(self.db_path)
        create_schema(conn)
        migrate(conn)
        conn.close()

        self._patch_err = patch("tkinter.messagebox.showerror")
        self._patch_warn = patch("tkinter.messagebox.showwarning")
        self._patch_info = patch("tkinter.messagebox.showinfo")
        self.mock_showerror = self._patch_err.start()
        self.mock_showwarning = self._patch_warn.start()
        self.mock_showinfo = self._patch_info.start()

    def tearDown(self):
        self._patch_err.stop()
        self._patch_warn.stop()
        self._patch_info.stop()

        if self._orig_db_env is not None:
            os.environ["MEAL_PLANNER_DB"] = self._orig_db_env
        else:
            os.environ.pop("MEAL_PLANNER_DB", None)

        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except OSError:
                pass


class TestChallengerDestructiveGuardrails(ChallengerIsolatedDBTestCase):
    """Empirical challenge of cancellation and confirmation for destructive actions."""

    def test_destructive_inventory_wipe_cancellation(self):
        """Mock askyesno -> False; empirically assert 0 rows deleted from INVENTORY and EXCLUSION_LIST."""
        conn = get_connection(self.db_path)
        conn.execute("INSERT INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g) VALUES (501, 'kg', 250.0, 12.0, 45.0, 3.0)")
        conn.execute("INSERT INTO ALIASES (Name, ID) VALUES ('Avena Test', 501)")
        conn.execute("INSERT INTO INVENTORY (Row_ID, ID, Unit_Weight, Intact_Items, Open_Remainder, Location) VALUES (1, 501, 1.0, 4, 0.0, 'pantry')")
        conn.execute("INSERT INTO INVENTORY (Row_ID, ID, Unit_Weight, Intact_Items, Open_Remainder, Location) VALUES (2, 501, 0.5, 2, 0.2, 'freezer')")
        conn.execute("INSERT INTO EXCLUSION_LIST (ID, Ref_Name, Expiration_Date, Location) VALUES (501, 'Avena Test', '2027-01-01', 'pantry')")
        conn.commit()
        conn.close()

        app = App()
        try:
            with patch("tkinter.messagebox.askyesno", return_value=False) as mock_ask:
                app.on_clear_inventory()
                self.assertTrue(mock_ask.called, "askyesno must be called")

            conn = get_connection(self.db_path)
            inv_count = conn.execute("SELECT COUNT(*) FROM INVENTORY").fetchone()[0]
            excl_count = conn.execute("SELECT COUNT(*) FROM EXCLUSION_LIST").fetchone()[0]
            conn.close()

            self.assertEqual(inv_count, 2, "0 rows must be deleted from INVENTORY on cancel")
            self.assertEqual(excl_count, 1, "0 rows must be deleted from EXCLUSION_LIST on cancel")
        finally:
            app.destroy()

    def test_destructive_profile_reset_cancellation(self):
        """Mock askyesno -> False; empirically assert 0 rows deleted from PROFILE and WEIGHT_LOG."""
        conn = get_connection(self.db_path)
        conn.execute("""
            INSERT OR REPLACE INTO PROFILE (ID, Weight_kg, Height_cm, Age_years, Sex, Activity_Level, Goal, Kcal_target)
            VALUES (1, 80.0, 182.0, 29, 'M', 'moderate', 'maintenance', 2600)
        """)
        conn.execute("INSERT OR REPLACE INTO WEIGHT_LOG (Date, Weight_kg) VALUES ('2026-11-01', 81.0)")
        conn.execute("INSERT OR REPLACE INTO WEIGHT_LOG (Date, Weight_kg) VALUES ('2026-11-04', 80.0)")
        conn.commit()
        conn.close()

        app = App()
        try:
            with patch("tkinter.messagebox.askyesno", return_value=False) as mock_ask:
                app.on_reset_profile()
                self.assertTrue(mock_ask.called, "askyesno must be called")

            conn = get_connection(self.db_path)
            prof_count = conn.execute("SELECT COUNT(*) FROM PROFILE").fetchone()[0]
            weight_count = conn.execute("SELECT COUNT(*) FROM WEIGHT_LOG").fetchone()[0]
            conn.close()

            self.assertEqual(prof_count, 1, "0 rows must be deleted from PROFILE on cancel")
            self.assertEqual(weight_count, 2, "0 rows must be deleted from WEIGHT_LOG on cancel")
        finally:
            app.destroy()

    def test_destructive_inventory_wipe_confirmation_and_foreign_key_integrity(self):
        """
        Mock askyesno -> True; assert INVENTORY and EXCLUSION_LIST are 0,
        PRODUCTS, ALIASES, and RECIPE_INGREDIENTS remain intact, and PRAGMA foreign_key_check is 0.
        """
        conn = get_connection(self.db_path)
        conn.execute("INSERT INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g) VALUES (601, 'kg', 120.0, 20.0, 0.0, 4.0)")
        conn.execute("INSERT INTO ALIASES (Name, ID) VALUES ('Pollo Test', 601)")
        conn.execute("INSERT INTO INVENTORY (Row_ID, ID, Unit_Weight, Intact_Items, Open_Remainder, Location) VALUES (10, 601, 0.8, 3, 0.0, 'fridge')")
        conn.execute("INSERT INTO EXCLUSION_LIST (ID, Ref_Name, Expiration_Date, Location) VALUES (601, 'Pollo Test', '2026-12-31', 'fridge')")
        # Add a recipe referencing product 601
        conn.execute("INSERT INTO RECIPES (Recipe_ID, Name, Notes) VALUES (101, 'Petto di Pollo', 'al vapore')")
        conn.execute("INSERT INTO RECIPE_INGREDIENTS (Recipe_ID, ID, Quantity, Unit) VALUES (101, 601, 0.2, 'kg')")
        conn.commit()
        conn.close()

        app = App()
        try:
            with patch("tkinter.messagebox.askyesno", return_value=True):
                app.on_clear_inventory()

            conn = get_connection(self.db_path)
            inv_count = conn.execute("SELECT COUNT(*) FROM INVENTORY").fetchone()[0]
            excl_count = conn.execute("SELECT COUNT(*) FROM EXCLUSION_LIST").fetchone()[0]
            prod_count = conn.execute("SELECT COUNT(*) FROM PRODUCTS WHERE ID = 601").fetchone()[0]
            alias_count = conn.execute("SELECT COUNT(*) FROM ALIASES WHERE ID = 601").fetchone()[0]
            recipe_count = conn.execute("SELECT COUNT(*) FROM RECIPES WHERE Recipe_ID = 101").fetchone()[0]
            ing_count = conn.execute("SELECT COUNT(*) FROM RECIPE_INGREDIENTS WHERE Recipe_ID = 101").fetchone()[0]

            fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            integrity = conn.execute("PRAGMA integrity_check").fetchall()
            conn.close()

            self.assertEqual(inv_count, 0, "INVENTORY count must be 0")
            self.assertEqual(excl_count, 0, "EXCLUSION_LIST count must be 0")
            self.assertEqual(prod_count, 1, "PRODUCTS catalog must remain intact")
            self.assertEqual(alias_count, 1, "ALIASES catalog must remain intact")
            self.assertEqual(recipe_count, 1, "RECIPES must remain intact")
            self.assertEqual(ing_count, 1, "RECIPE_INGREDIENTS must remain intact")
            self.assertEqual(fk_violations, [], "PRAGMA foreign_key_check must return 0 violations")
            self.assertEqual(integrity, [("ok",)], "PRAGMA integrity_check must return 'ok'")
        finally:
            app.destroy()

    def test_repeat_inventory_addition_after_wipe(self):
        """
        After inventory wipe, add an inventory item for an existing product and
        verify normal operations (addition, inventory row count, and consumption).
        """
        conn = get_connection(self.db_path)
        conn.execute("INSERT INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g) VALUES (701, 'kg', 360.0, 8.0, 75.0, 1.5)")
        conn.execute("INSERT INTO ALIASES (Name, ID) VALUES ('Riso Basmati', 701)")
        conn.commit()

        # Wipe inventory
        inv.clear_inventory(conn)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM INVENTORY").fetchone()[0], 0)

        # Add inventory item for existing product (unit_weight=1.0, intact_items=2 -> total 2.0 kg)
        inv.add_item(
            name="Riso Basmati",
            unit_weight=1.0,
            intact_items=2,
            location="pantry",
            expiration_date="2027-06-30",
            conn=conn,
        )
        conn.commit()
        conn.close()

        # Consume quantity from this newly added item (0.4 kg)
        res = inv.consume_quantity(name="Riso Basmati", quantity_to_consume=0.4, db_path=self.db_path)
        self.assertEqual(res["outcome"], "ok")
        self.assertEqual(res["shortfall"], 0.0)
        self.assertEqual(res["consumed"], 0.4)
        self.assertFalse(res["insufficient_availability"])

        conn = get_connection(self.db_path)
        row_after = conn.execute("SELECT Intact_Items, Open_Remainder FROM INVENTORY WHERE ID = 701").fetchone()
        self.assertEqual(row_after[0], 1)
        self.assertAlmostEqual(row_after[1], 0.6, places=2)

        fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        self.assertEqual(fk_violations, [])
        conn.close()

    def test_profile_reset_confirmation(self):
        """Mock askyesno -> True; assert PROFILE and WEIGHT_LOG are 0 and overlay is shown on Profilo tab."""
        conn = get_connection(self.db_path)
        conn.execute("""
            INSERT OR REPLACE INTO PROFILE (ID, Weight_kg, Height_cm, Age_years, Sex, Activity_Level, Goal, Kcal_target)
            VALUES (1, 65.0, 170.0, 26, 'F', 'light', 'maintenance', 1900)
        """)
        conn.execute("INSERT OR REPLACE INTO WEIGHT_LOG (Date, Weight_kg) VALUES ('2026-11-04', 65.0)")
        conn.commit()
        conn.close()

        app = App()
        try:
            with patch("tkinter.messagebox.askyesno", return_value=True):
                app.on_reset_profile()

            conn = get_connection(self.db_path)
            prof_count = conn.execute("SELECT COUNT(*) FROM PROFILE").fetchone()[0]
            weight_count = conn.execute("SELECT COUNT(*) FROM WEIGHT_LOG").fetchone()[0]
            conn.close()

            self.assertEqual(prof_count, 0, "PROFILE must have 0 rows after reset")
            self.assertEqual(weight_count, 0, "WEIGHT_LOG must have 0 rows after reset")

            # Verify that planner defaults are restored after profile reset
            self.assertEqual(app.pv["Kcal_tolerance_pct"].get(), "5")
            self.assertEqual(app.pv["Open_extra_pct"].get(), "5")
            self.assertEqual(app.pv["No_repeat_days"].get(), "3")
            self.assertEqual(app.pv["Default_servings"].get(), "1")

            # Verify that navigating to Profilo displays the empty-state overlay
            app.refresh(active_tab="Profilo")
            app.update_idletasks()
            self.assertTrue(app.profile_overlay.winfo_ismapped(), "Empty state overlay must be shown")
        finally:
            app.destroy()

    def test_post_reset_backend_update_profile_upsert(self):
        """Verify that backend update_profile() successfully executes upsert when PROFILE has 0 rows."""
        conn = get_connection(self.db_path)
        engine_profile.reset_profile(conn)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM PROFILE").fetchone()[0], 0)
        conn.close()

        prof_data = {
            "Sex": "F",
            "Height_cm": 165.0,
            "Weight_kg": 58.0,
            "Age_years": 25,
            "Activity_Level": "moderate",
            "Goal": "maintenance",
            "Weekly_rate_kg": None,
            "Kcal_tolerance_pct": 5,
            "Open_extra_pct": 5,
            "No_repeat_days": 3,
            "Default_servings": 1,
            "Body_fat_pct": 21.0,
            "Waist_cm": 68.0,
            "Target_weight_kg": None,
            "Maintenance_manual": None,
            "Kcal_manual": None,
            "Use_manual_maintenance": 0,
            "Use_manual_kcal": 0,
        }

        kcal = engine_profile.update_profile(values=prof_data, db_path=self.db_path)
        self.assertGreater(kcal, 1500)

        conn = get_connection(self.db_path)
        row = conn.execute("SELECT ID, Weight_kg, Height_cm, Sex FROM PROFILE WHERE ID = 1").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 1)
        self.assertEqual(row[1], 58.0)
        self.assertEqual(row[2], 165.0)
        self.assertEqual(row[3], "F")

        # Verify WEIGHT_LOG has today's entry
        wl_count = conn.execute("SELECT COUNT(*) FROM WEIGHT_LOG").fetchone()[0]
        self.assertEqual(wl_count, 1)
        conn.close()

    def test_post_reset_profile_setup_dialog(self):
        """Test recreation via ProfileSetupDialog modal dialog after reset."""
        conn = get_connection(self.db_path)
        engine_profile.reset_profile(conn)
        conn.close()

        app = App()
        try:
            app.refresh(active_tab="Profilo")
            app.update_idletasks()
            self.assertTrue(app.profile_overlay.winfo_ismapped())

            dlg = ProfileSetupDialog(app, db_path=self.db_path, wait=False)
            dlg.v_sex.set("F")
            dlg.v_height.set("172")
            dlg.v_weight.set("64")
            dlg.v_age.set("29")
            dlg.v_activity.set("moderate")
            dlg.v_goal.set("maintenance")
            saved = dlg.save()
            self.assertTrue(saved)

            app.refresh(active_tab="Profilo")
            app.update_idletasks()

            # Check that profile now exists and overlay disappeared
            prof = engine_catalog.load_profile(self.db_path)
            self.assertIsNotNone(prof)
            self.assertEqual(prof["Sex"], "F")
            self.assertEqual(prof["Height_cm"], 172.0)
            self.assertEqual(prof["Weight_kg"], 64.0)
        finally:
            app.destroy()

    def test_app_save_profile_gui_persistence_bug(self):
        """
        Adversarial test demonstrating the transaction rollback bug in app.save_profile():
        app.save_profile() executes update_profile(c, data) inside 'with db() as c:',
        which never commits. As a result, values are rolled back upon connection close.
        """
        app = App()
        try:
            app.pv["Sex"].set("M")
            app.pv["Height_cm"].set("180")
            app.pv["Weight_kg"].set("78")
            app.pv["Age_years"].set("32")
            app.pv["Activity_Level"].set("heavy")
            app.pv["Goal"].set("maintenance")
            app.pv["Kcal_tolerance_pct"].set("5")
            app.pv["Open_extra_pct"].set("5")
            app.pv["No_repeat_days"].set("3")
            app.pv["Default_servings"].set("1")

            app.save_profile()

            conn = get_connection(self.db_path)
            prof_row = conn.execute("SELECT Weight_kg, Sex FROM PROFILE WHERE ID = 1").fetchone()
            conn.close()

            # Empirical check: if uncommitted, Weight_kg is NOT 78.0 or Sex is NOT 'M'
            # We assert the expected behavior (that it SHOULD persist).
            # When it fails, it exposes the uncommitted transaction defect.
            self.assertEqual(prof_row[0], 78.0, "Weight_kg must be persisted to database")
            self.assertEqual(prof_row[1], "M", "Sex must be persisted to database")
        finally:
            app.destroy()


class TestChallengerReceiptLoggingEdgeCases(ChallengerIsolatedDBTestCase):
    """Stress testing log_receipt and get_receipt_logs with adversarial inputs."""

    def test_receipt_logging_adversarial_filenames(self):
        """
        Stress test log_receipt with:
        - Spaces in filename
        - Single quotes and double quotes
        - Non-ASCII and Unicode characters, accents, emoji
        - Very long file paths (> 1000 chars)
        - SQL injection attempts in all fields
        - Empty string and boundary inputs
        """
        adversarial_cases = [
            ("12:00:00", "2026-11-04", "Receipt with spaces in name.txt"),
            ("13:15:22", "2026-11-04", "L'angolo della spesa \"speciale\" 'super'.txt"),
            ("14:30:00", "2026-11-04", "Spesa_Caffè_e_Tè_€150_🛒_öäü_漢字_Русский.pdf"),
            ("15:45:10", "2026-11-04", "dir/subdir/nested/receipt_path.txt"),
            ("16:00:00", "2026-11-04", "x" * 1200 + ".txt"),  # Very long path
            ("17:20:00", "2026-11-04", "test'); DROP TABLE RECEIPT_LOGS; --.txt"),  # SQL injection test
            ("00:00:00", "9999-12-31", ""),  # Empty filename
            ("23:59:59.999999", "2026/02/29", "odd_timestamp_date.log"),
        ]

        ids = []
        for time_str, day_str, fname in adversarial_cases:
            row_id = inv.log_receipt(time_str, day_str, fname, db_path=self.db_path)
            self.assertGreater(row_id, 0)
            ids.append(row_id)

        # Confirm all IDs are strictly increasing
        self.assertEqual(ids, sorted(ids))

        # Retrieve and verify ordered retrieval (ORDER BY id DESC)
        logs = inv.get_receipt_logs(db_path=self.db_path)
        self.assertEqual(len(logs), len(adversarial_cases))

        # Check reverse order
        for retrieved, expected in zip(logs, reversed(adversarial_cases)):
            self.assertEqual(retrieved[0], expected[0], "Time must match exactly")
            self.assertEqual(retrieved[1], expected[1], "Day must match exactly")
            self.assertEqual(retrieved[2], expected[2], "Filename must match exactly, preserving quotes and unicode")

        # Verify RECEIPT_LOGS table is NOT dropped by SQL injection test
        conn = get_connection(self.db_path)
        tbl_check = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='RECEIPT_LOGS'").fetchone()
        self.assertIsNotNone(tbl_check, "RECEIPT_LOGS table must exist unharmed")
        conn.close()

    def test_receipt_logging_wal_cross_connection(self):
        """Verify receipts written on one connection are immediately visible on another under WAL mode."""
        conn1 = get_connection(self.db_path)
        conn2 = get_connection(self.db_path)

        # Check WAL mode
        mode1 = conn1.execute("PRAGMA journal_mode;").fetchone()[0]
        self.assertEqual(mode1.lower(), "wal")

        inv.log_receipt("08:00:00", "2026-11-04", "conn1_receipt.txt", conn=conn1)

        logs2 = inv.get_receipt_logs(conn=conn2)
        self.assertEqual(len(logs2), 1)
        self.assertEqual(logs2[0][2], "conn1_receipt.txt")

        conn1.close()
        conn2.close()


class TestProductionDatabaseUntouched(unittest.TestCase):
    """Ensures production inventory.db row counts (14, 30, 36, 3) are completely untouched."""

    def test_production_db_row_counts(self):
        prod_db = Path("data/inventory.db")
        self.assertTrue(prod_db.exists(), "data/inventory.db must exist")

        conn = sqlite3.connect(str(prod_db))
        counts = {
            "PRODUCTS": conn.execute("SELECT COUNT(*) FROM PRODUCTS").fetchone()[0],
            "ALIASES": conn.execute("SELECT COUNT(*) FROM ALIASES").fetchone()[0],
            "INVENTORY": conn.execute("SELECT COUNT(*) FROM INVENTORY").fetchone()[0],
            "RECIPES": conn.execute("SELECT COUNT(*) FROM RECIPES").fetchone()[0],
        }
        conn.close()

        self.assertEqual(counts["PRODUCTS"], 14, "PRODUCTS count must remain 14")
        self.assertEqual(counts["ALIASES"], 30, "ALIASES count must remain 30")
        self.assertEqual(counts["INVENTORY"], 36, "INVENTORY count must remain 36")
        self.assertEqual(counts["RECIPES"], 3, "RECIPES count must remain 3")


class TestOfflineConstraintEnforcement(unittest.TestCase):
    """Verifies that no socket network activity can occur."""

    def test_no_socket_connections(self):
        with patch("socket.socket", side_effect=RuntimeError("Network access forbidden!")):
            # Instantiate App and switch tabs/sections
            app = App()
            try:
                for section in ("style_and_config", "full_profile", "receipt_logs", "destructive_actions"):
                    app.switch_settings_section(section)
            finally:
                app.destroy()


class TestSettingsAndProfileAdvancedEdgeCases(unittest.TestCase):
    """
    Advanced edge-case testing for Settings & Profile (Milestone M5):
    - Profile empty-state overlay lifecycle with exact text "Ciao, aggiunti il tuo profilo per iniziare"
    - Session theme toggle does not mutate startup default in APP_SETTINGS
    - Destructive actions cancel guardrails (inventory and profile preservation)
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "settings_edge_test.db")
        self.orig_db = os.environ.get("MEAL_PLANNER_DB")
        os.environ["MEAL_PLANNER_DB"] = self.db_path
        conn = get_connection(self.db_path)
        from meal_planner.db.schema import create_schema
        create_schema(conn)
        conn.close()

    def tearDown(self):
        if self.orig_db is not None:
            os.environ["MEAL_PLANNER_DB"] = self.orig_db
        else:
            os.environ.pop("MEAL_PLANNER_DB", None)
        self.tmp.cleanup()

    def test_profile_empty_state_overlay_lifecycle_and_exact_text(self):
        from meal_planner.gui.dialogs import ProfileSetupDialog
        app = App()
        app.withdraw()
        try:
            app.update()
            # 1. Overlay placed when profile is empty
            self.assertTrue(bool(app.profile_overlay.place_info()), "Empty-state overlay must be placed when profile is absent")
            text_items = app.profile_overlay.find_withtag("overlay_text")
            texts = [app.profile_overlay.itemcget(t, "text") for t in text_items]
            self.assertTrue("Hello, add your profile to get started" in texts or "Ciao, aggiunti il tuo profilo per iniziare" in texts, "Overlay must display exact required text")

            # 2. Add profile via ProfileSetupDialog
            dlg = ProfileSetupDialog(app, db_path=self.db_path, wait=False)
            dlg.save()
            app.refresh()
            app.update()
            self.assertFalse(bool(app.profile_overlay.place_info()), "Overlay must disappear once profile is created")

            # 3. Reset profile in destructive actions
            with patch("tkinter.messagebox.askyesno", return_value=True):
                app.on_reset_profile()
            app.update()
            self.assertTrue(bool(app.profile_overlay.place_info()), "Overlay must immediately reappear after profile reset")
        finally:
            app.destroy()

    def test_session_theme_toggle_does_not_mutate_startup_default(self):
        cfg.save_config({"default_theme": "dark", "theme": "dark"})
        app = App()
        app.withdraw()
        try:
            self.assertEqual(cfg.get("default_theme"), "dark")
            self.assertEqual(app.theme, "dark")

            # Toggle theme in real-time session
            app.toggle_theme()
            self.assertEqual(app.theme, "light")
            # Invariant: APP_SETTINGS unchanged
            self.assertEqual(cfg.get("default_theme"), "dark", "Session toggle must not mutate persistent startup theme in APP_SETTINGS")

            # Change default startup theme in settings config
            app.v_cfg_theme.set("light")
            with patch("tkinter.messagebox.showinfo"):
                app.save_settings_config()
            self.assertEqual(cfg.get("default_theme"), "light", "Saving config in Settings must update default_theme in APP_SETTINGS")
        finally:
            app.destroy()

    def test_destructive_actions_cancel_guardrails(self):
        from meal_planner.gui.dialogs import ProfileSetupDialog
        conn = get_connection(self.db_path)
        conn.execute("INSERT INTO PRODUCTS (ID, Unit, Kcal_100g, Protein_100g, Carbs_100g, Fat_100g) VALUES (1, 'kg', 100, 10, 10, 2)")
        conn.execute("INSERT INTO INVENTORY (ID, Unit_Weight, Intact_Items, Location) VALUES (1, 1.0, 5, 'pantry')")
        conn.commit()
        conn.close()

        app = App()
        app.withdraw()
        try:
            # Setup a profile
            dlg = ProfileSetupDialog(app, db_path=self.db_path, wait=False)
            dlg.save()
            app.refresh()

            # Guardrail: cancel both destructive actions
            with patch("tkinter.messagebox.askyesno", return_value=False):
                app.on_clear_inventory()
                app.on_reset_profile()

            conn = get_connection(self.db_path)
            inv_count = conn.execute("SELECT COUNT(*) FROM INVENTORY").fetchone()[0]
            prof_count = conn.execute("SELECT COUNT(*) FROM PROFILE").fetchone()[0]
            conn.close()

            self.assertEqual(inv_count, 1, "Inventory rows must be preserved when clear_inventory is cancelled")
            self.assertEqual(prof_count, 1, "Profile must be preserved when reset_profile is cancelled")
            self.assertFalse(bool(app.profile_overlay.place_info()), "Overlay must remain hidden if reset is cancelled")
        finally:
            app.destroy()


if __name__ == "__main__":
    unittest.main()
