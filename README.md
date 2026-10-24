# Meal Prep Project

## MEAL PLANNER - Pantry, recipes, and meal plan (Desktop program)

**Requirements:** Python 3.10 or higher. No external libraries required (only the standard library, **Tkinter** included; on Linux, if it's missing, install the `python3-tk` system package).

### Startup

To start the program, run:

```bash
python gui.py
```

> *Note: On the first run, the program will automatically create the `inventario.db` database in the same folder.*

### How to Get Started (Window tabs)

1. **Profile** - Set gender, activity level, and goal: required to calculate daily kcal.

2. **Pantry** - Add products (the first time you enter a new name, you will be asked for the unit of measurement and nutritional values per 100 g).

3. **Recipes** - Enter recipes manually using the form, or import them from `.json` or `.csv` files.

4. **Plan** - Click "Propose plan", then "Save plan". When you cook, use "Mark as cooked" (indicate how many portions you prepared: the extra ones will become leftovers). You can also lock or skip a meal.

5. **Shopping** - Shows what is missing (calculating consumption that exceeds availability and ingredients needed for future plans).

### Recipe Import

The quantities indicated must be for **ONE** portion.

**Supported units:** `g`, `kg`, `ml`, `L`, `pcs`, `tbsp`, `tsp`.

#### JSON Format

*(Note: JSON keys are kept in Italian assuming your Python code expects them).*

```json
[
  {
    "nome": "Rice and chicken", 
    "pasti": ["pranzo", "cena"],
    "ingredienti": [
      {
        "prodotto": "Rice", 
        "quantita": 100, 
        "unita": "g"
      }
    ]
  }
]
```

#### CSV Format

Use `;` or `,` as a separator. Multiple meals must be separated by the `|` character.

```csv
ricetta;pasti;prodotto;quantita;unita;quanto_basta
```

### Command Line (Optional)

You can also interact with the program via the terminal:

```bash
python motore.py ricette | giorno ... | profilo ...
python pianificatore.py giorno | settimana | spesa
python ricette.py importa file.json
```