"""In-memory inventory and recipe simulation (never writes to the database on its own)."""

from meal_planner.engine.catalog import load_products, recipe_ingredients, group_members
from meal_planner.engine.nutrition import inventory_quantity
from meal_planner.inventory import _round_down_to_hundredth as _arr


class VirtualInventory:
    """In-memory copy of INVENTORY. consume() mirrors consume_quantity():
    open remainders first, then new units are opened in FIFO order."""

    def __init__(self, rows: dict, unit: dict):
        self.rows = rows    # {id: [[row_id, unit_weight, items, remainder], ...]}
        self.unit = unit    # {id: 'kg' | 'L' | 'pcs'}

    @classmethod
    def from_db(cls, conn):
        rows = {}
        for row_id, product_id, weight, number, remainder in conn.execute(
                "SELECT Row_ID, ID, Unit_Weight, Intact_Items, Open_Remainder "
                "FROM INVENTORY ORDER BY ID, Row_ID"):
            rows.setdefault(product_id, []).append([row_id, weight, number, remainder])
        unit = dict(conn.execute("SELECT ID, Unit FROM PRODUCTS").fetchall())
        return cls(rows, unit)

    def copy(self):
        return VirtualInventory({k: [r[:] for r in v] for k, v in self.rows.items()}, self.unit)

    def availability(self, product_id: int) -> float:
        rows = self.rows.get(product_id, [])
        if self.unit[product_id] == "pcs":
            return sum(r[2] for r in rows)
        return sum(r[2] * r[1] + r[3] for r in rows)

    def open_remainder(self, product_id: int) -> float:
        return sum(r[3] for r in self.rows.get(product_id, []))

    def preview(self, product_id: int, quantity: float) -> dict:
        """Outcome of a consumption without modifying the inventory."""
        trial = VirtualInventory({product_id: [r[:] for r in self.rows.get(product_id, [])]}, self.unit)
        return trial.consume(product_id, quantity)

    def write(self, conn, ids) -> None:
        """Writes the state of products `ids` to the DB (updates or deletes rows). Does not commit."""
        for product_id in ids:
            new = {r[0]: r for r in self.rows.get(product_id, [])}
            for (row_id,) in conn.execute("SELECT Row_ID FROM INVENTORY WHERE ID = ?", (product_id,)).fetchall():
                if row_id not in new:
                    conn.execute("DELETE FROM INVENTORY WHERE Row_ID = ?", (row_id,))
            for row_id, (_, _, number, remainder) in new.items():
                conn.execute("UPDATE INVENTORY SET Intact_Items = ?, Open_Remainder = ? "
                             "WHERE Row_ID = ?", (number, remainder, row_id))

    def consume(self, product_id: int, quantity: float) -> dict:
        rows = self.rows.get(product_id, [])
        pz = self.unit[product_id] == "pcs"
        disp = self.availability(product_id)
        shortfall = 0.0
        to_cover = quantity
        if disp < quantity:
            shortfall = (quantity - disp) if pz else _arr(quantity - disp)
            to_cover = disp
        remaining = to_cover
        from_open = 0.0
        open = 0
        if pz:
            for r in rows:
                if remaining <= 0:
                    break
                withdrawal = min(r[2], remaining)
                if withdrawal > 0:
                    r[2] -= withdrawal
                    remaining -= withdrawal
        else:
            for r in rows:                      # phase 1: already-open remainders
                if remaining <= 0:
                    break
                if r[3] > 0:
                    withdrawal = min(r[3], remaining)
                    r[3] = _arr(r[3] - withdrawal)
                    remaining -= withdrawal
                    from_open += withdrawal
            if remaining > 0:                        # phase 2: new units, FIFO
                for r in rows:
                    while remaining > 0 and r[2] > 0:
                        r[2] -= 1
                        open += 1
                        if remaining >= r[1]:
                            remaining -= r[1]
                            r[3] = 0.0
                        else:
                            r[3] = _arr(r[1] - remaining)
                            remaining = 0.0
                    if remaining <= 0:
                        break
        self.rows[product_id] = [r for r in rows if not (r[2] == 0 and r[3] == 0)]
        return {"consumed": to_cover, "shortfall": shortfall,
                "from_open": from_open, "packages_opened": open}


def _choose_member(conn, inv, products, group_id, quantity, unit):
    """Group member to use: feasible first, then most open, then fewest
    new packages, then lowest ID."""
    best = None
    for product_id in group_members(conn, group_id):
        q = inventory_quantity(quantity, unit, products[product_id])
        if q is None:
            continue
        trial = inv.preview(product_id, q)
        key = (trial["shortfall"] > 0, -min(1.0, trial["from_open"] / q),
                  trial["packages_opened"], product_id)
        if best is None or key < best[0]:
            best = (key, product_id)
    return best[1] if best else None


def simulate_recipe(conn, inv: VirtualInventory, recipe_id: int, products: dict = None,
                   servings: int = 1, choices: dict = None, ingredients: list = None) -> dict:
    """Consumes the recipe from `inv` (which is MODIFIED: pass inv.copy() for
    a separate trial). Returns open coverage, new packages, missing items
    and the per-ingredient detail."""
    products = products if products is not None else load_products(conn)
    choices = dict(choices or {})
    detail, missing, coverages = [], {}, []
    new = 0
    incomplete = False
    for ing in (ingredients if ingredients is not None else recipe_ingredients(conn, recipe_id)):
        qty = ing["Quantity"] * servings
        product_id = ing["ID"]
        if product_id is None:
            gid = ing["Group_ID"]
            if gid not in choices:
                choices[gid] = _choose_member(conn, inv, products, gid, qty, ing["Unit"])
            product_id = choices[gid]
        if product_id is None:
            incomplete = True
            continue
        p = products[product_id]
        q = inventory_quantity(qty, ing["Unit"], p)
        if q is None:
            incomplete = True
            continue
        r = inv.consume(product_id, q)
        coverages.append(min(1.0, r["from_open"] / q) if q > 0 else 0.0)
        new += r["packages_opened"]
        if r["shortfall"] > 0:
            missing[product_id] = missing.get(product_id, 0.0) + r["shortfall"]
        detail.append({"ID": product_id, "requested": q, "unit": p["unit"],
                          "from_open": r["from_open"],
                          "packages_opened": r["packages_opened"],
                          "shortfall": r["shortfall"]})
    return {"detail": detail,
            "open_coverage": sum(coverages) / len(coverages) if coverages else 0.0,
            "new_packages": new,
            "missing": missing,
            "feasible": not missing and not incomplete,
            "incomplete_data": incomplete,
            "group_choices": choices}
