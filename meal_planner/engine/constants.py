"""Shared constants: macro names, energy factors, activity levels."""



MACROS = ["Kcal", "Protein", "Carbs", "Fat", "Sugars",
         "Saturated_fat", "Fiber", "Salt"]


LIMITED_NUTRIENTS = [m for m in MACROS if m != "Kcal"]


OPTIONAL = ("Sugars", "Saturated_fat", "Fiber", "Salt")


KCAL_PER_G = {"Protein": 4.0, "Carbs": 4.0, "Sugars": 4.0,
              "Fat": 9.0, "Saturated_fat": 9.0}


PAL = {"light": 1.4, "moderate": 1.6, "heavy": 1.75}


KCAL_PER_KG = 7700      # energy of 1 kg of body weight, used for the weekly weight-change rate


EPS = 1e-9
