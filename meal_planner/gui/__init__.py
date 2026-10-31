"""
Meal Planner desktop interface (Tkinter, no extra installation).
Start:  python -m meal_planner

Tabs: Pantry (availability, add, consume) | Shopping | Recipes | Plan | Profile.
Calls the project functions directly: no server or browser needed.
"""


from meal_planner.gui.app import main

__all__ = ["main"]
