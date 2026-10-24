# meal_prep_project
# MEAL PLANNER - pantry, recipes, and meal plan (desktop program)

Requirements: Python 3.10 or higher. No external libraries (standard library only, including Tkinter; on Linux, if missing, install the `python3-tk` system package).

STARTUP: `python gui.py`
On the first run, it creates the `inventario.db` database in the same folder.

HOW TO GET STARTED (window tabs)
1. **Profile** - set gender, activity level, and goal: required for daily kcal.
2. **Pantry** - add products (the first time you enter a new name, it asks for the unit and nutritional values per 100 g).
3. **Recipes** - enter recipes manually from the form or import them from .json / .csv files.
4. **Plan** - "Propose plan", then "Save plan"; when you cook, "Mark as cooked" (indicate how many portions: the rest become leftovers). You can lock or skip a meal.
5. **Shopping** - shows what is missing (consumption exceeding availability and plan ingredients).

RECIPE IMPORT (quantities are for ONE portion; units: g, kg, ml, L, pcs, tbsp, tsp)
JSON: `[{"nome": "Riso e pollo", "pasti": ["pranzo", "cena"], "ingredienti": [{"prodotto": "Riso", "quantita": 100, "unita": "g"}]}]`
CSV (separator ; or ,): `ricetta;pasti;prodotto;quantita;unita;quanto_basta` (meals separated by |)

COMMAND LINE (optional)
`python motore.py ricette | giorno ... | profilo ...`
`python pianificatore.py giorno | settimana | spesa`
`python ricette.py importa file.json`