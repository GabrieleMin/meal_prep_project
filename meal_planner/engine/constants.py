"""Shared constants: macro names, energy factors, activity levels."""



MACROS = ["Kcal", "Protein", "Carbs", "Fat", "Sugars",
         "Saturated_fat", "Fiber", "Salt"]


LIMITED_NUTRIENTS = [m for m in MACROS if m != "Kcal"]


OPTIONAL = ("Sugars", "Saturated_fat", "Fiber", "Salt")


KCAL_PER_G = {"Protein": 4.0, "Carbs": 4.0, "Sugars": 4.0,
              "Fat": 9.0, "Saturated_fat": 9.0}


PAL = {"light": 1.4, "moderate": 1.6, "heavy": 1.75}


EPS = 1e-9
