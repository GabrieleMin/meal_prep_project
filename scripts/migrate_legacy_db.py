"""
scripts.migrate_legacy_db

Converts a database created by the earlier, Italian-named version of this project (tables such as
NUTRIENTI, INVENTARIO, PIANO; usually the file inventario.db) into a NEW database with the current
English schema. The old file is only read, never modified.

Usage (from the project root):
    python -m scripts.migrate_legacy_db inventario.db inventory.db

Table and column names are renamed, and the stored status/unit/meal values are translated
(e.g. 'pranzo' -> 'lunch', 'proposto' -> 'proposed', 'pz' -> 'pcs'). Free text that you typed
(product names, recipe names, locations other than the old defaults) is copied unchanged.
"""

import os
import sqlite3
import sys

from meal_planner.db.connection import get_connection
from meal_planner.db.recipe_schema import migrate as migrate_recipe_schema
from meal_planner.db.schema import create_schema

# (legacy table, current table), in dependency order
TABLES = [('NUTRIENTI', 'PRODUCTS'),
 ('ALIAS', 'ALIASES'),
 ('INVENTARIO', 'INVENTORY'),
 ('LISTA_SPESA', 'SHOPPING_LIST'),
 ('PASTI', 'MEALS'),
 ('RICETTE', 'RECIPES'),
 ('RICETTA_PASTI', 'RECIPE_MEALS'),
 ('GRUPPI_SOSTITUIBILI', 'SUBSTITUTE_GROUPS'),
 ('GRUPPO_MEMBRI', 'GROUP_MEMBERS'),
 ('RICETTA_INGREDIENTI', 'RECIPE_INGREDIENTS'),
 ('COTTURE', 'COOKINGS'),
 ('PIANO', 'PLAN'),
 ('PROFILO', 'PROFILE'),
 ('OBIETTIVI', 'GOALS'),
 ('LISTA_SPESA_PIANO', 'PLAN_SHOPPING_LIST')]

COLUMNS = {'Nome': 'Name',
 'Unita_di_misura': 'Unit',
 'Proteine_100g': 'Protein_100g',
 'Carboidrati_100g': 'Carbs_100g',
 'Zuccheri_100g': 'Sugars_100g',
 'Grassi_100g': 'Fat_100g',
 'Grassi_saturi_100g': 'Saturated_fat_100g',
 'Fibre_100g': 'Fiber_100g',
 'Sale_100g': 'Salt_100g',
 'Grammi_per_pezzo': 'Grams_per_piece',
 'Densita_kg_per_L': 'Density_kg_per_L',
 'ID_riga': 'Row_ID',
 'Peso_Unitario': 'Unit_Weight',
 'Numero_Item_Integri': 'Intact_Items',
 'Peso_Residuo_Aperto': 'Open_Remainder',
 'Luogo': 'Location',
 'Nome_Riferimento': 'Reference_Name',
 'Quantita_Mancante': 'Missing_Quantity',
 'Ordine': 'Sort_Order',
 'Quota_energia_pct': 'Energy_Share_pct',
 'ID_ricetta': 'Recipe_ID',
 'Note': 'Notes',
 'Pasto': 'Meal',
 'ID_gruppo': 'Group_ID',
 'Quantita': 'Quantity',
 'Unita': 'Unit',
 'Quanto_basta': 'To_Taste',
 'ID_cottura': 'Cooking_ID',
 'Data_Cottura': 'Cooking_Date',
 'Porzioni_totali': 'Total_Servings',
 'Porzioni_residue': 'Remaining_Servings',
 'Stato': 'Status',
 'ID_voce': 'Entry_ID',
 'Data': 'Date',
 'Peso_kg': 'Weight_kg',
 'Altezza_cm': 'Height_cm',
 'Eta_anni': 'Age_years',
 'Sesso': 'Sex',
 'Livello_attivita': 'Activity_Level',
 'Obiettivo': 'Goal',
 'Tolleranza_kcal_pct': 'Kcal_tolerance_pct',
 'Extra_aperto_pct': 'Open_extra_pct',
 'Giorni_no_ripetizione': 'No_repeat_days',
 'Nutriente': 'Nutrient',
 'Tipo': 'Type',
 'Minimo': 'Min_Value',
 'Massimo': 'Max_Value',
 'Proteine': 'Protein',
 'Carboidrati': 'Carbs',
 'Grassi': 'Fat',
 'Zuccheri': 'Sugars',
 'Grassi_saturi': 'Saturated_fat',
 'Fibre': 'Fiber',
 'Sale': 'Salt',
 'Dati_incompleti': 'Incomplete_Data',
 'Grammi': 'Grams',
 'Peso_Totale_Disponibile': 'Total_Available_Weight'}

MEAL_VALUES = {'colazione': 'breakfast', 'pranzo': 'lunch', 'cena': 'dinner'}
PLAN_STATUS_VALUES = {'proposto': 'proposed', 'confermato': 'confirmed', 'consumato': 'consumed', 'saltato': 'skipped'}
COOKING_STATUS_VALUES = {"pianificata": "planned", "cucinata": "cooked"}
ACTIVITY_VALUES = {"leggera": "light", "moderata": "moderate", "intensa": "heavy"}
GOAL_VALUES = {"mantenimento": "maintenance", "dimagrimento": "weight_loss", "aumento": "weight_gain"}
NUTRIENT_VALUES = {'Proteine': 'Protein',
 'Carboidrati': 'Carbs',
 'Grassi': 'Fat',
 'Zuccheri': 'Sugars',
 'Grassi_saturi': 'Saturated_fat',
 'Fibre': 'Fiber',
 'Sale': 'Salt'}
RULE_TYPE_VALUES = {"assoluto": "absolute", "pct_energia": "pct_energy"}
UNIT_VALUES = {'pz': 'pcs', 'cucchiaio': 'tbsp', 'cucchiaino': 'tsp'}
LOCATION_VALUES = {"dispensa": "pantry", "frigo": "fridge"}

# (current table, current column) -> {legacy value: current value}
VALUE_COLUMNS = {
    ("MEALS", "Name"): MEAL_VALUES, ("RECIPE_MEALS", "Meal"): MEAL_VALUES, ("PLAN", "Meal"): MEAL_VALUES,
    ("PLAN", "Status"): PLAN_STATUS_VALUES, ("COOKINGS", "Status"): COOKING_STATUS_VALUES,
    ("PROFILE", "Activity_Level"): ACTIVITY_VALUES, ("PROFILE", "Goal"): GOAL_VALUES,
    ("GOALS", "Nutrient"): NUTRIENT_VALUES, ("GOALS", "Type"): RULE_TYPE_VALUES,
    ("PRODUCTS", "Unit"): UNIT_VALUES, ("RECIPE_INGREDIENTS", "Unit"): UNIT_VALUES,
    ("INVENTORY", "Location"): LOCATION_VALUES,
}
CONFIG_TABLES = {"MEALS", "PROFILE", "GOALS"}   # replace the defaults created with the new schema


def migrate(old_path, new_path):
    """Creates `new_path` from the legacy database. Returns {table: rows copied}."""
    if not os.path.exists(old_path):
        raise FileNotFoundError(old_path)
    if os.path.exists(new_path):
        raise FileExistsError(f"{new_path} already exists: choose a new file name")
    new = get_connection(new_path)
    old = sqlite3.connect(f"file:{old_path}?mode=ro", uri=True)
    copied = {}
    try:
        create_schema(new)
        migrate_recipe_schema(new)
        present = {r[0] for r in old.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        for legacy, current in TABLES:
            if legacy not in present:
                continue
            if current in CONFIG_TABLES:
                new.execute(f"DELETE FROM {current}")
            columns = [r[1] for r in old.execute(f"PRAGMA table_info({legacy})")]
            names = [COLUMNS.get(c, c) for c in columns]
            maps = [VALUE_COLUMNS.get((current, n), {}) for n in names]
            rows = [[m.get(v, v) for m, v in zip(maps, row)] for row in old.execute(f"SELECT * FROM {legacy}")]
            new.executemany(f"INSERT INTO {current} ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})", rows)
            copied[current] = len(rows)
        new.commit()
        if new.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or new.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("the migrated database failed the integrity check")
    except Exception:
        new.close()
        old.close()
        os.remove(new_path)
        raise
    new.close()
    old.close()
    return copied


def main():
    if len(sys.argv) != 3:
        sys.exit("Usage: python -m scripts.migrate_legacy_db OLD_DB NEW_DB")
    try:
        copied = migrate(sys.argv[1], sys.argv[2])
    except (OSError, RuntimeError, sqlite3.Error) as e:
        sys.exit(f"Migration failed: {e}")
    print(f"Migrated {sys.argv[1]} -> {sys.argv[2]}")
    for table, n in copied.items():
        print(f"  {table:<22}{n:>5} rows")


if __name__ == "__main__":
    main()
