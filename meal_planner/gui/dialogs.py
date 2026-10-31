"""Modal dialogs."""

from tkinter import messagebox, ttk
import tkinter as tk

from meal_planner.gui.support import number
from meal_planner.gui.widgets import Form


class ProductDialog(tk.Toplevel):
    """Asks for the data of a product not yet in the archive."""

    def __init__(self, master, name, suggested_id):
        super().__init__(master)
        self.title("New product")
        self.transient(master)
        self.result = None
        ttk.Label(self, wraplength=380, padding=10, text=(
            f"'{name}' is not in the archive yet.\nEnter the ID: if it already exists the name becomes an alias "
            "of that product; if it is new, also fill in unit and values per 100 g.")).pack()
        self.form = Form(self, "", [("id", "Product ID", None), ("unit", "Unit", ["kg", "L", "pcs"]),
                                    ("kcal", "Kcal", None), ("prot", "Protein (g)", None),
                                    ("carb", "Carbs (g)", None), ("fat", "Fat (g)", None)],
                         "Confirm", self.confirm)
        self.form.pack(padx=10, pady=(0, 10))
        self.form.v["id"].set(suggested_id)
        self.grab_set()
        self.wait_window()

    def confirm(self):
        v = self.form.read()
        try:
            r = {"fallback_id": int(v["id"])}
            if v["unit"]:
                r["new_product_unit"] = v["unit"]
            if all(v[k] for k in ("kcal", "prot", "carb", "fat")):
                r["new_product_macros"] = {"Kcal_100g": number(v["kcal"]), "Protein_100g": number(v["prot"]),
                                             "Carbs_100g": number(v["carb"]), "Fat_100g": number(v["fat"])}
        except ValueError as e:
            messagebox.showerror("Invalid data", str(e), parent=self)
            return
        self.result = r
        self.destroy()


class ChoiceDialog(tk.Toplevel):
    """Asks the user to pick a value from a list."""

    def __init__(self, master, title, values):
        super().__init__(master)
        self.title(title)
        self.transient(master)
        self.result = None
        self.v = tk.StringVar()
        ttk.Label(self, text=title, padding=10).pack()
        ttk.Combobox(self, textvariable=self.v, values=values, state="readonly", width=34).pack(padx=10)
        ttk.Button(self, text="Confirm", command=self.confirm).pack(pady=10)
        self.grab_set()
        self.wait_window()

    def confirm(self):
        self.result = self.v.get() or None
        self.destroy()
