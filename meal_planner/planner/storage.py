"""Saving plans, derived shopping list and marking meals as cooked."""

from meal_planner import engine as m


def _apply_plan(conn, inv, products, before=None):
    """Consumes from `inv` (modifying it), in date order, the plan recipes not yet
    consumed (proposed or confirmed) with Date < before (all if None).
    Returns the missing items {ID: quantity}."""
    missing = {}
    entries = conn.execute(
        "SELECT c.Recipe_ID FROM PLAN p JOIN COOKINGS c USING (Cooking_ID) "
        "WHERE p.Status IN ('proposed', 'confirmed') AND c.Status = 'planned' AND (? IS NULL OR p.Date < ?) "
        "ORDER BY p.Date, (SELECT Sort_Order FROM MEALS WHERE Name = p.Meal)", (before, before)).fetchall()
    for (rid,) in entries:
        for i, q in m.simulate_recipe(conn, inv, rid, products)["missing"].items():
            missing[i] = missing.get(i, 0.0) + q
    return missing


def update_plan_shopping_list(conn):
    """Recomputes PLAN_SHOPPING_LIST from scratch with the missing items of the whole plan (proposed or confirmed)."""
    missing = _apply_plan(conn, m.VirtualInventory.from_db(conn), m.load_products(conn))
    conn.execute("DELETE FROM PLAN_SHOPPING_LIST")
    conn.executemany("INSERT INTO PLAN_SHOPPING_LIST (ID, Missing_Quantity) VALUES (?, ?)",
                     [(i, q) for i, q in missing.items() if q > 0])
    conn.commit()


def save_plan(conn, plan, update_shopping=True):
    """Writes the plan: replaces the proposals of the day and the requested meals, leaves
    untouched those already in the DB (confirmed/consumed/skipped). With
    update_shopping=False it does not recompute the shopping list and does NOT commit."""
    d = plan["date_str"]
    new = [v for v in plan["meals"] if v["origin"] != "db"]
    placeholders = ",".join("?" * len(new))
    old = conn.execute(
        f"SELECT Entry_ID, Cooking_ID FROM PLAN WHERE Date = ? AND Status != 'consumed' "
        f"AND (Status = 'proposed' OR Meal IN ({placeholders}))", [d, *[v["meal"] for v in new]]).fetchall()
    conn.executemany("DELETE FROM PLAN WHERE Entry_ID = ?", [(i,) for i, _ in old])
    conn.executemany("DELETE FROM COOKINGS WHERE Cooking_ID = ? AND Status = 'planned'",
                     [(c,) for _, c in old if c])
    for v in new:
        cooking = v["Cooking_ID"] if v["Recipe_ID"] else None
        if v["Recipe_ID"] and cooking is None:
            cooking = conn.execute(
                "INSERT INTO COOKINGS (Recipe_ID, Cooking_Date, Total_Servings, Remaining_Servings) "
                "VALUES (?, ?, 1, 1)", (v["Recipe_ID"], d)).lastrowid
        status = "skipped" if cooking is None else ("proposed" if v["origin"] == "planned" else "confirmed")
        conn.execute("INSERT INTO PLAN (Date, Meal, Cooking_ID, Status) VALUES (?, ?, ?, ?)",
                     (d, v["meal"], cooking, status))
    if update_shopping:
        update_plan_shopping_list(conn)


def mark_cooked(conn, date_str, meal, servings=1):
    """Marks the meal as consumed. If the recipe has to be cooked it consumes the ingredients (x `servings`)
    from the real inventory in a single transaction; with servings > 1 the others become leftovers
    (Remaining_Servings) that can be eaten at later meals without consuming anything else. If the meal
    uses a leftover it only consumes one remaining serving. Refuses, writing nothing, if that is not possible."""
    r = conn.execute("SELECT p.Cooking_ID, c.Recipe_ID, p.Status, c.Status, c.Remaining_Servings FROM PLAN p "
                     "LEFT JOIN COOKINGS c USING (Cooking_ID) WHERE p.Date = ? AND p.Meal = ?",
                     (date_str, meal)).fetchone()
    if r is None or r[2] in ("consumed", "skipped"):
        raise ValueError(f"No meal to cook for {meal} on {date_str}.")
    cooking_id, rid, _, cooking_status, remaining = r
    if servings < 1:
        raise ValueError("Servings must be at least 1.")
    if cooking_status == "cooked":                       # a ready leftover
        if servings != 1 or remaining < 1:
            raise ValueError("This meal uses a leftover: there are no other servings to cook "
                             "or none remain.")
        conn.execute("UPDATE COOKINGS SET Remaining_Servings = Remaining_Servings - 1 WHERE Cooking_ID = ?", (cooking_id,))
    else:
        products, inv = m.load_products(conn), m.VirtualInventory.from_db(conn)
        sim = m.simulate_recipe(conn, inv, rid, products, servings=servings)
        if not sim["feasible"]:
            names = m.product_names(conn)
            raise ValueError("Ingredients missing: " + ", ".join(
                f"{names[i]} {q:.2f} {products[i]['unit']}" for i, q in sim["missing"].items()))
        inv.write(conn, {d["ID"] for d in sim["detail"]})
        conn.execute("UPDATE COOKINGS SET Status = 'cooked', Total_Servings = ?, Remaining_Servings = ? "
                     "WHERE Cooking_ID = ?", (servings, servings - 1, cooking_id))
    conn.execute("UPDATE PLAN SET Status = 'consumed' WHERE Date = ? AND Meal = ?", (date_str, meal))
    update_plan_shopping_list(conn)
