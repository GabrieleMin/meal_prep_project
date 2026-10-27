"""Main window."""

from datetime import date
from tkinter import filedialog, messagebox, simpledialog, ttk
import contextlib
import io
import tkinter as tk

from meal_planner import engine as m
from meal_planner import planner as pf
from meal_planner import recipes as rc
from meal_planner.db.recipe_schema import migrate
from meal_planner.gui.dialogs import ProductDialog, ChoiceDialog
from meal_planner.gui.support import RED, GREEN, db, number, safe
from meal_planner.gui.widgets import Form, Table
from meal_planner.inventory import add_item, consume_quantity, get_available_inventory, ProductNotFound


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Meal Planner")
        self.geometry("1140x700")
        self.minsize(920, 580)
        ttk.Style(self).theme_use("clam")
        ttk.Style(self).configure("Treeview", rowheight=24)
        with db() as c:
            migrate(c)                                     # makes sure the schema exists
        self.plans, self.draft, self.fixed = [], [], {}
        self.status = tk.StringVar(value="Ready.")
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=8, pady=(8, 0))
        for title, build in (("Pantry", self.tab_pantry), ("Shopping", self.tab_shopping),
                                   ("Recipes", self.tab_recipes), ("Plan", self.tab_plan),
                                   ("Profile", self.tab_profile)):
            tab = ttk.Frame(self.nb, padding=10)
            self.nb.add(tab, text=title)
            build(tab)
        self.bar = ttk.Label(self, textvariable=self.status, anchor="w", padding=6)
        self.bar.pack(fill="x")
        self.nb.bind("<<NotebookTabChanged>>", lambda e: self.refresh())
        self.refresh()

    def msg(self, text, error=False):
        self.status.set(text)
        self.bar.configure(foreground=RED if error else GREEN)

    # ---------------- tabs ----------------

    def tab_pantry(self, f):
        f.columnconfigure(0, weight=1)
        f.rowconfigure(0, weight=1)
        self.t_disp = Table(f, [("n", "Product", 240), ("q", "Available", 100), ("u", "Unit", 60),
                                  ("l", "Location", 120)], 18)
        self.t_disp.grid(row=0, column=0, sticky="nsew")
        side = ttk.Frame(f)
        side.grid(row=0, column=1, sticky="n", padx=(12, 0))
        locations = ["pantry", "fridge", "freezer"]
        self.f_add = Form(side, "Add to inventory",
                          [("name", "Product", []), ("weight", "Package weight/volume", None),
                           ("num", "No. of packages", None), ("location", "Location", locations)], "Add", self.add)
        self.f_use = Form(side, "Consume", [("name", "Product", []), ("q", "Quantity", None)],
                          "Consume", self.consume)
        self.f_add.pack(fill="x")
        self.f_use.pack(fill="x", pady=(10, 0))

    def tab_shopping(self, f):
        ttk.Label(f, text="What is missing: consumption beyond availability and plan ingredients.").pack(anchor="w")
        self.t_shopping = Table(f, [("n", "Product", 320), ("q", "Missing quantity", 140), ("u", "Unit", 80)], 20)
        self.t_shopping.pack(fill="both", expand=True, pady=8)

    def tab_recipes(self, f):
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)
        ttk.Label(f, text="Macros per serving and feasibility with the current inventory "
                          "('Open' = share taken from packages already open).").grid(row=0, column=0, sticky="w")
        self.t_ric = Table(f, [("r", "Recipe", 200), ("k", "kcal", 50), ("p", "Prot", 50), ("c", "Carb", 50),
                                 ("g", "Fat", 50), ("fi", "Fiber", 50), ("a", "Open", 55),
                                 ("e", "Outcome", 220)], 18)
        self.t_ric.grid(row=1, column=0, sticky="nsew", pady=8)
        bar = ttk.Frame(f)
        bar.grid(row=2, column=0, sticky="w")
        ttk.Button(bar, text="Import from file (.json / .csv)...", command=self.import_recipes).pack(side="left")
        ttk.Button(bar, text="Delete selected recipe", command=self.delete_recipe).pack(side="left", padx=6)

        new = ttk.LabelFrame(f, text="New recipe (quantities for ONE serving)", padding=10)
        new.grid(row=0, column=1, rowspan=3, sticky="n", padx=(12, 0))
        self.v_rnome = tk.StringVar()
        ttk.Label(new, text="Name").grid(row=0, column=0, sticky="w")
        ttk.Entry(new, textvariable=self.v_rnome, width=30).grid(row=0, column=1, sticky="ew", padx=(8, 0))
        with db() as c:
            self.v_meals = {n: tk.BooleanVar() for (n,) in c.execute("SELECT Name FROM MEALS ORDER BY Sort_Order")}
        checkboxes = ttk.Frame(new)
        checkboxes.grid(row=1, column=0, columnspan=2, sticky="w", pady=4)
        ttk.Label(checkboxes, text="Meals:").pack(side="left", padx=(0, 6))
        for name, var in self.v_meals.items():
            ttk.Checkbutton(checkboxes, text=name, variable=var).pack(side="left", padx=(0, 6))
        self.f_ing = Form(new, "Ingredient", [("p", "Product or group", []), ("q", "Quantity", None),
                                                 ("u", "Unit", list(rc.UNITS))],
                          "Add ingredient", self.add_ingredient)
        self.f_ing.grid(row=2, column=0, columnspan=2, sticky="ew")
        self.t_draft = Table(new, [("p", "Recipe ingredients", 160), ("q", "Quantity", 70),
                                       ("u", "Unit", 70)], 5)
        self.t_draft.grid(row=3, column=0, columnspan=2, pady=6)
        ttk.Button(new, text="Remove selected ingredient",
                   command=self.remove_ingredient).grid(row=4, column=0, columnspan=2, sticky="ew")
        ttk.Button(new, text="Save recipe", command=self.save_recipe).grid(
            row=5, column=0, columnspan=2, sticky="ew", pady=(6, 0))

    def tab_plan(self, f):
        top = ttk.Frame(f)
        top.pack(fill="x")
        self.v_date, self.v_days, self.v_kcal = (tk.StringVar(value=date.today().isoformat()),
                                                   tk.StringVar(value="7"), tk.StringVar())
        for label, var, width in (("Start date", self.v_date, 12), ("Days", self.v_days, 4),
                                          ("Kcal (empty = profile)", self.v_kcal, 8)):
            ttk.Label(top, text=label).pack(side="left", padx=(0, 4))
            ttk.Entry(top, textvariable=var, width=width).pack(side="left", padx=(0, 12))
        for text, command in (("Propose plan", self.propose), ("Save plan", self.save),
                               ("Mark as cooked", self.cooked)):
            ttk.Button(top, text=text, command=command).pack(side="left", padx=3)
        middle = ttk.Frame(f)
        middle.pack(fill="x", pady=(6, 0))
        ttk.Label(middle, text="On the selected meal:").pack(side="left", padx=(0, 6))
        for text, command in (("Lock recipe...", self.lock), ("Skip meal", self.skip),
                               ("Unlock all", self.unlock)):
            ttk.Button(middle, text=text, command=command).pack(side="left", padx=3)
        self.t_plan = Table(f, [("d", "Date", 95), ("p", "Meal", 90), ("r", "Recipe", 290),
                                   ("k", "kcal", 60), ("s", "Status", 360)], 9)
        self.t_plan.pack(fill="both", expand=True, pady=8)
        self.text = tk.Text(f, height=9, wrap="word", state="disabled", relief="flat", background="#f6f7f9")
        self.text.pack(fill="x")

    def tab_profile(self, f):
        self.profile_label = ttk.Label(f, justify="left")
        self.profile_label.pack(anchor="w", pady=(0, 10))
        self.f_prof = Form(f, "Set the profile",
                           [("sex", "Sex", ["M", "F"]), ("att", "Activity", sorted(m.PAL)),
                            ("ob", "Goal", ["maintenance", "weight_loss", "weight_gain"]),
                            ("kcal", "Kcal/day (required unless maintenance)", None)],
                           "Save profile", self.save_profile)
        self.f_prof.pack(anchor="w")

    # ---------------- refresh ----------------

    def refresh(self):
        with db() as c:
            names = [r[0] for r in c.execute("SELECT Name FROM ALIASES ORDER BY Name")]
            groups = [r[0] for r in c.execute("SELECT Name FROM SUBSTITUTE_GROUPS ORDER BY Name")]
            shopping = c.execute("SELECT Name, Missing_Quantity, Unit FROM SHOPPING_LIST_VIEW "
                              "ORDER BY Name").fetchall()
            recipes = self.recipe_rows(c)
            p = m.load_profile(c)
        for form in (self.f_add, self.f_use):
            form.w["name"]["values"] = names
        self.f_ing.w["p"]["values"] = names + groups
        self.t_disp.fill([((r["Name"], f"{r['Total_Available_Weight']:g}", r["Unit"], r["Location"]), None)
                            for r in get_available_inventory()])
        self.t_shopping.fill([((n, f"{q:.2f}", u), None) for n, q, u in shopping])
        self.t_ric.fill(recipes)
        kcal = f"{p['Kcal_target']:.0f} kcal/day" if p["Kcal_target"] else "not defined (set the profile)"
        self.profile_label.configure(text=(
            f"Weight {p['Weight_kg']:g} kg, height {p['Height_cm']:g} cm, age {p['Age_years']}\n"
            f"Sex: {p['Sex'] or '-'}   Activity: {p['Activity_Level'] or '-'}   "
            f"Goal: {p['Goal'] or '-'}\nTarget: {kcal}"))

    @staticmethod
    def recipe_rows(c):
        products, names, inv = m.load_products(c), m.product_names(c), m.VirtualInventory.from_db(c)
        rows = []
        for recipe_id, name in c.execute("SELECT Recipe_ID, Name FROM RECIPES ORDER BY Name").fetchall():
            sim = m.simulate_recipe(c, inv.copy(), recipe_id, products)
            mac = m.recipe_macros(c, recipe_id, products, sim["group_choices"])
            rows.append(((name, f"{mac['Kcal']:.0f}", f"{mac['Protein']:.1f}", f"{mac['Carbs']:.1f}",
                           f"{mac['Fat']:.1f}", f"{mac['Fiber']:.1f}", f"{sim['open_coverage'] * 100:.0f}%",
                           m.outcome(sim, products, names)), None if sim["feasible"] else "ko"))
        return rows

    # ---------------- actions ----------------

    @safe
    def add(self):
        v = self.f_add.read()
        weight, num = number(v["weight"], "weight"), int(number(v["num"], "packages"))
        try:
            add_item(v["name"], weight, num, v["location"])
        except ProductNotFound:
            with db() as c:
                suggested = c.execute("SELECT COALESCE(MAX(ID), 0) + 1 FROM PRODUCTS").fetchone()[0]
            extra = ProductDialog(self, v["name"], suggested).result
            if extra is None:
                return
            add_item(v["name"], weight, num, v["location"], **extra)
        self.msg(f"Added: {v['name']} ({num} x {weight:g}) in {v['location']}.")
        self.f_add.clear()
        self.refresh()

    @safe
    def consume(self):
        v = self.f_use.read()
        r = consume_quantity(v["name"], number(v["q"], "quantity"))
        text = f"Consumed {r['consumed']:g} of {v['name']}."
        if r["insufficient_availability"]:
            text += f" Insufficient availability: {r['shortfall']:g} missing, added to the shopping list."
        self.msg(text, r["insufficient_availability"])
        self.f_use.clear()
        self.refresh()

    def plan_params(self):
        date_str = self.v_date.get().strip()
        try:
            date.fromisoformat(date_str)
        except ValueError:
            raise ValueError("Invalid date: use the YYYY-MM-DD format.")
        days = int(number(self.v_days.get(), "days"))
        if not 1 <= days <= 31:
            raise ValueError("Days must be between 1 and 31.")
        kcal = number(self.v_kcal.get(), "kcal") if self.v_kcal.get().strip() else None
        return date_str, days, kcal

    def write_text(self, text):
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("end", text)
        self.text.configure(state="disabled")

    @safe
    def propose(self):
        date_str, days, kcal = self.plan_params()
        with db() as c:
            self.plans = pf.plan_week(c, date_str, days, kcal, self.fixed or None)
            products, names = m.load_products(c), m.product_names(c)
            summary = io.StringIO()
            with contextlib.redirect_stdout(summary):
                pf.print_week(c, self.plans)
        rows = []
        for p in self.plans:
            for v in p["meals"]:
                if v["Recipe_ID"] is None:
                    rows.append(((p["date_str"], v["meal"], "(skipped)", "", "skipped"), None))
                else:
                    ok = v["sim"]["feasible"]
                    rows.append(((p["date_str"], v["meal"], v["recipe"], f"{v['macro']['Kcal']:.0f}",
                                   "proposta: " + ("uses a leftover" if v["leftover"] else m.outcome(v["sim"], products, names))), None if ok else "ko"))
        self.t_plan.fill(rows)
        self.write_text(summary.getvalue())
        self.msg("Plan proposed (not saved yet).")

    @safe
    def save(self):
        if not self.plans:
            raise ValueError("Propose a plan first.")
        with db() as c:
            pf.save_week(c, self.plans)
        self.msg("Plan saved. Shopping list updated.")
        self.show_saved()
        self.refresh()

    def show_saved(self):
        date_str, days, _ = self.plan_params()
        with db() as c:
            rows = c.execute(
                "SELECT p.Date, p.Meal, COALESCE(r.Name, '(skipped)'), COALESCE(CAST(ROUND(v.Kcal) AS INTEGER), ''), "
                "p.Status FROM PLAN p LEFT JOIN COOKINGS k USING (Cooking_ID) "
                "LEFT JOIN RECIPES r ON r.Recipe_ID = k.Recipe_ID "
                "LEFT JOIN RECIPE_MACROS_VIEW v ON v.Recipe_ID = r.Recipe_ID "
                "WHERE p.Date >= ? AND p.Date < date(?, '+' || ? || ' day') "
                "ORDER BY p.Date, (SELECT Sort_Order FROM MEALS WHERE Name = p.Meal)", (date_str, date_str, days)).fetchall()
        self.t_plan.fill([(r, None) for r in rows])

    @safe
    def cooked(self):
        selection = self.t_plan.tv.selection()
        if not selection:
            raise ValueError("Select a meal in the table (after saving the plan).")
        date_str, meal = self.t_plan.tv.item(selection[0], "values")[:2]
        n = simpledialog.askinteger(
            "Cooked", f"How many servings are you cooking now for '{meal}' on {date_str}?\n"
                        "1 = this meal only; more = the others become leftovers for later meals.\n"
                        "(If the meal uses a ready leftover, keep 1.)",
            parent=self, minvalue=1, maxvalue=20, initialvalue=1)
        if n is None:
            return
        with db() as c:
            pf.mark_cooked(c, date_str, meal, n)
        self.msg(f"Done: {meal} on {date_str} cooked, inventory updated"
                 + (f" ({n - 1} leftovers available)." if n > 1 else "."))
        self.show_saved()
        self.refresh()

    @safe
    def save_profile(self):
        v = self.f_prof.read()
        if not (v["sex"] and v["att"] and v["ob"]):
            raise ValueError("Fill in sex, activity and goal.")
        with db() as c:
            kcal = m.set_profile(c, v["sex"], v["att"], v["ob"], number(v["kcal"], "kcal") if v["kcal"] else None)
        self.msg(f"Profile saved: target {kcal:.0f} kcal/day.")
        self.refresh()

    @safe
    def add_ingredient(self):
        v = self.f_ing.read()
        if not v["p"] or not v["u"]:
            raise ValueError("Specify product (or group), quantity and unit.")
        self.draft.append((v["p"], number(v["q"], "quantity"), v["u"]))
        self.f_ing.clear()
        self.show_draft()

    def show_draft(self):
        self.t_draft.fill([((p, f"{q:g}", u), None) for p, q, u in self.draft])

    @safe
    def remove_ingredient(self):
        selection = self.t_draft.tv.selection()
        if selection:
            del self.draft[self.t_draft.tv.index(selection[0])]
            self.show_draft()

    @safe
    def save_recipe(self):
        name = self.v_rnome.get().strip()
        meals = [n for n, var in self.v_meals.items() if var.get()]
        with db() as c:
            outcome = rc.create_recipe(c, name, meals, self.draft)
        self.draft = []
        self.v_rnome.set("")
        for var in self.v_meals.values():
            var.set(False)
        self.show_draft()
        warning = " Warning: " + outcome["warnings"][0] if outcome["warnings"] else ""
        self.msg(f"Recipe '{name}' saved." + warning, bool(warning))
        self.refresh()

    @safe
    def import_recipes(self):
        path = filedialog.askopenfilename(title="Import recipes",
                                              filetypes=[("Recipes", "*.json *.csv"), ("All files", "*.*")])
        if not path:
            return
        with db() as c:
            created, errors = rc.import_file(c, path)
        self.msg(f"Imported {created} recipes" + (f", {len(errors)} skipped." if errors else "."), bool(errors))
        if errors:
            messagebox.showwarning("Skipped recipes", "\n".join(errors[:15]))
        self.refresh()

    @safe
    def delete_recipe(self):
        selection = self.t_ric.tv.selection()
        if not selection:
            raise ValueError("Select a recipe in the table.")
        name = self.t_ric.tv.item(selection[0], "values")[0]
        if not messagebox.askyesno("Delete", f"Delete the recipe '{name}'?"):
            return
        with db() as c:
            rc.delete_recipe(c, name)
        self.msg(f"Recipe '{name}' deleted.")
        self.refresh()

    def selected_meal(self):
        selection = self.t_plan.tv.selection()
        if not selection:
            raise ValueError("Select a meal in the table.")
        return self.t_plan.tv.item(selection[0], "values")[:2]

    @safe
    def lock(self):
        date_str, meal = self.selected_meal()
        with db() as c:
            names = [r[0] for r in c.execute("SELECT r.Name FROM RECIPES r JOIN RECIPE_MEALS p USING (Recipe_ID) "
                                            "WHERE p.Meal = ? ORDER BY r.Name", (meal,))]
        if not names:
            raise ValueError(f"No recipe allowed for {meal}.")
        choice = ChoiceDialog(self, f"Recipe for {meal} on {date_str}", names).result
        if choice is None:
            return
        with db() as c:
            self.fixed.setdefault(date_str, {})[meal] = m.recipe_id_from_name(c, choice)
        self.propose()
        self.msg(f"{meal} on {date_str} fixed to '{choice}': save the plan to confirm it.")

    @safe
    def skip(self):
        date_str, meal = self.selected_meal()
        self.fixed.setdefault(date_str, {})[meal] = None
        self.propose()
        self.msg(f"{meal} on {date_str} skipped: save the plan to record it.")

    @safe
    def unlock(self):
        self.fixed = {}
        self.propose()


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
