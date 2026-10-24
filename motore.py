"""
motore.py

Motore di calcolo per ricette e piano dei pasti. Non scrive MAI sull'inventario
reale: lavora su una copia in memoria (InventarioVirtuale). L'unica scrittura
e' il comando 'profilo', che aggiorna la tabella PROFILO.

Contenuto
- macro_ricetta():     macro per porzione, con conversione pz/L/ml/cucchiai -> grammi
- InventarioVirtuale:  stessa logica di consuma_quantita() (prima il residuo
                       aperto, poi nuove confezioni in ordine FIFO) ma in memoria
- simula_ricetta():    copertura dell'aperto, confezioni nuove, mancanti
                       (sceglie da solo il membro di un gruppo sostituibile)
- verifica_giorno():   totali del giorno contro kcal e limiti nutrizionali
- stima_mantenimento() fabbisogno stimato (Mifflin-St Jeor)

Regole adottate
- I limiti in % dell'energia e "per 1000 kcal" si valutano sulle kcal REALI del
  giorno; i limiti "per kg" sul peso del profilo. Piu' regole sullo stesso
  nutriente valgono insieme (intersezione). Le kcal si confrontano con
  Kcal_target +- Tolleranza_kcal_pct.
- extra_pct (default 0) allarga tutti i limiti: il pianificatore lo usera'
  (Extra_aperto_pct del profilo) solo se cosi' finisce un residuo aperto.
- Copertura dell'aperto di un ingrediente = quota della quantita' richiesta
  presa da residui gia' aperti (0..1); per i 'pz' vale sempre 0 (Q19).
  Copertura della ricetta = media sugli ingredienti.
- Prodotti 'pz': solo pezzi interi; una quantita' in g/ml si converte in pezzi
  arrotondando per eccesso.

Uso da riga di comando (le ricette con spazi vanno tra virgolette):
    python motore.py ricette
    python motore.py giorno "Yogurt, mela e latte" "Spaghetti al pomodoro" "Pollo e riso" --kcal 2500
    python motore.py profilo --sesso M --attivita moderata --obiettivo mantenimento
    python motore.py profilo --sesso M --attivita moderata --obiettivo dimagrimento --kcal 2400
"""

import argparse
import math
import sys

from crea_RELSCHEMA import get_connection, DB_PATH
from operation import _arrotonda_difetto_centesimo as _arr

MACRO = ["Kcal", "Proteine", "Carboidrati", "Grassi", "Zuccheri",
         "Grassi_saturi", "Fibre", "Sale"]
NUTRIENTI_LIMITATI = [m for m in MACRO if m != "Kcal"]
OPZIONALI = ("Zuccheri", "Grassi_saturi", "Fibre", "Sale")
KCAL_PER_G = {"Proteine": 4.0, "Carboidrati": 4.0, "Zuccheri": 4.0,
              "Grassi": 9.0, "Grassi_saturi": 9.0}
PAL = {"leggera": 1.4, "moderata": 1.6, "intensa": 1.75}
EPS = 1e-9


# ----------------------------------------------------------------------
# Lettura dati
# ----------------------------------------------------------------------

def carica_prodotti(conn) -> dict:
    righe = conn.execute(
        "SELECT ID, Unita_di_misura, Grammi_per_pezzo, Densita_kg_per_L, "
        "Kcal_100g, Proteine_100g, Carboidrati_100g, Grassi_100g, Zuccheri_100g, "
        "Grassi_saturi_100g, Fibre_100g, Sale_100g FROM NUTRIENTI").fetchall()
    return {r[0]: {"ID": r[0], "unita": r[1], "g_pezzo": r[2], "densita": r[3],
                   "macro": dict(zip(MACRO, r[4:]))} for r in righe}


def nomi_prodotti(conn) -> dict:
    nomi = {}
    for id_p, nome in conn.execute("SELECT ID, Nome FROM ALIAS ORDER BY rowid"):
        nomi.setdefault(id_p, nome)
    return nomi


def id_ricetta_da_nome(conn, nome: str):
    r = conn.execute("SELECT ID_ricetta FROM RICETTE WHERE Nome = ?", (nome,)).fetchone()
    return r[0] if r else None


def ingredienti_ricetta(conn, id_ricetta: int) -> list:
    righe = conn.execute(
        "SELECT ID, ID_gruppo, Quantita, Unita, Quanto_basta FROM RICETTA_INGREDIENTI "
        "WHERE ID_ricetta = ? ORDER BY ID_riga", (id_ricetta,)).fetchall()
    return [{"ID": r[0], "ID_gruppo": r[1], "Quantita": r[2], "Unita": r[3],
             "Quanto_basta": r[4]} for r in righe]


def membri_gruppo(conn, id_gruppo: int) -> list:
    return [r[0] for r in conn.execute(
        "SELECT ID FROM GRUPPO_MEMBRI WHERE ID_gruppo = ? ORDER BY ID", (id_gruppo,))]


def carica_profilo(conn) -> dict:
    cur = conn.execute(
        "SELECT Peso_kg, Altezza_cm, Eta_anni, Sesso, Livello_attivita, Obiettivo, "
        "Kcal_target, Tolleranza_kcal_pct, Extra_aperto_pct, Giorni_no_ripetizione "
        "FROM PROFILO WHERE ID = 1")
    r = cur.fetchone()
    if r is None:
        raise RuntimeError("Profilo mancante: esegui prima crea_RICETTE.py")
    return dict(zip([d[0] for d in cur.description], r))


def carica_obiettivi(conn) -> list:
    return [{"Nutriente": r[0], "Tipo": r[1], "Minimo": r[2], "Massimo": r[3]}
            for r in conn.execute("SELECT Nutriente, Tipo, Minimo, Massimo FROM OBIETTIVI")]


# ----------------------------------------------------------------------
# Conversioni e macro
# ----------------------------------------------------------------------

def grammi(quantita: float, unita: str, prod: dict):
    """Quantita' di ricetta -> grammi (stessa logica di VISTA_INGREDIENTI_GRAMMI).
    None se manca il dato di conversione."""
    if unita == "g":
        return quantita
    if unita == "kg":
        return quantita * 1000.0
    if unita == "pz":
        return quantita * prod["g_pezzo"] if prod["g_pezzo"] else None
    if not prod["densita"]:
        return None
    fattori = {"ml": 1.0, "L": 1000.0, "cucchiaio": 15.0, "cucchiaino": 5.0}
    return quantita * fattori[unita] * prod["densita"]


def quantita_inventario(quantita: float, unita: str, prod: dict):
    """Quantita' di ricetta -> unita' di inventario del prodotto (kg, L o pz).
    None se non convertibile."""
    u = prod["unita"]
    if u == "pz":
        if unita == "pz":
            return math.ceil(round(quantita, 6))
        g = grammi(quantita, unita, prod)
        if g is None or not prod["g_pezzo"]:
            return None
        return math.ceil(round(g / prod["g_pezzo"], 6))
    if u == "L" and unita in ("ml", "L", "cucchiaio", "cucchiaino"):
        return quantita * {"ml": 0.001, "L": 1.0, "cucchiaio": 0.015, "cucchiaino": 0.005}[unita]
    g = grammi(quantita, unita, prod)
    if g is None:
        return None
    if u == "kg":
        return g / 1000.0
    return g / (1000.0 * prod["densita"]) if prod["densita"] else None


def macro_ricetta(conn, id_ricetta: int, prodotti: dict = None, scelte: dict = None,
                  porzioni: int = 1, ingredienti: list = None) -> dict:
    """Macro di `porzioni` porzioni. Un ingrediente di gruppo senza scelta in
    `scelte` ({id_gruppo: id_prodotto}) non si somma e segnala Dati_incompleti."""
    prodotti = prodotti if prodotti is not None else carica_prodotti(conn)
    scelte = scelte or {}
    tot = {m: 0.0 for m in MACRO}
    incompleti = False
    for ing in (ingredienti if ingredienti is not None else ingredienti_ricetta(conn, id_ricetta)):
        id_p = ing["ID"] if ing["ID"] is not None else scelte.get(ing["ID_gruppo"])
        if id_p is None:
            incompleti = True
            continue
        p = prodotti[id_p]
        g = grammi(ing["Quantita"] * porzioni, ing["Unita"], p)
        if g is None:
            incompleti = True
            continue
        for m in MACRO:
            v = p["macro"][m]
            if v is None:          # nutriente opzionale mancante: conta 0 e segnala
                incompleti = True
                v = 0.0
            tot[m] += g / 100.0 * v
    tot["Dati_incompleti"] = incompleti
    return tot


def somma_macro(lista: list) -> dict:
    return {m: sum(x[m] for x in lista) for m in MACRO}


# ----------------------------------------------------------------------
# Inventario virtuale
# ----------------------------------------------------------------------

class InventarioVirtuale:
    """Copia in memoria di INVENTARIO. consuma() replica consuma_quantita():
    prima i residui aperti, poi apertura di nuove unita' in ordine FIFO."""

    def __init__(self, righe: dict, unita: dict):
        self.righe = righe    # {id: [[id_riga, peso_unitario, numero, residuo], ...]}
        self.unita = unita    # {id: 'kg' | 'L' | 'pz'}

    @classmethod
    def da_db(cls, conn):
        righe = {}
        for id_riga, id_p, peso, numero, residuo in conn.execute(
                "SELECT ID_riga, ID, Peso_Unitario, Numero_Item_Integri, Peso_Residuo_Aperto "
                "FROM INVENTARIO ORDER BY ID, ID_riga"):
            righe.setdefault(id_p, []).append([id_riga, peso, numero, residuo])
        unita = dict(conn.execute("SELECT ID, Unita_di_misura FROM NUTRIENTI").fetchall())
        return cls(righe, unita)

    def copia(self):
        return InventarioVirtuale({k: [r[:] for r in v] for k, v in self.righe.items()}, self.unita)

    def disponibilita(self, id_p: int) -> float:
        righe = self.righe.get(id_p, [])
        if self.unita[id_p] == "pz":
            return sum(r[2] for r in righe)
        return sum(r[2] * r[1] + r[3] for r in righe)

    def residuo_aperto(self, id_p: int) -> float:
        return sum(r[3] for r in self.righe.get(id_p, []))

    def anteprima(self, id_p: int, quantita: float) -> dict:
        """Esito di un consumo senza modificare l'inventario."""
        prova = InventarioVirtuale({id_p: [r[:] for r in self.righe.get(id_p, [])]}, self.unita)
        return prova.consuma(id_p, quantita)

    def scrivi(self, conn, ids) -> None:
        """Porta sul DB lo stato dei prodotti `ids` (aggiorna o elimina le righe). Non fa commit."""
        for id_p in ids:
            nuove = {r[0]: r for r in self.righe.get(id_p, [])}
            for (id_riga,) in conn.execute("SELECT ID_riga FROM INVENTARIO WHERE ID = ?", (id_p,)).fetchall():
                if id_riga not in nuove:
                    conn.execute("DELETE FROM INVENTARIO WHERE ID_riga = ?", (id_riga,))
            for id_riga, (_, _, numero, residuo) in nuove.items():
                conn.execute("UPDATE INVENTARIO SET Numero_Item_Integri = ?, Peso_Residuo_Aperto = ? "
                             "WHERE ID_riga = ?", (numero, residuo, id_riga))

    def consuma(self, id_p: int, quantita: float) -> dict:
        righe = self.righe.get(id_p, [])
        pz = self.unita[id_p] == "pz"
        disp = self.disponibilita(id_p)
        mancante = 0.0
        da_coprire = quantita
        if disp < quantita:
            mancante = (quantita - disp) if pz else _arr(quantita - disp)
            da_coprire = disp
        resto = da_coprire
        da_aperto = 0.0
        aperte = 0
        if pz:
            for r in righe:
                if resto <= 0:
                    break
                prelievo = min(r[2], resto)
                if prelievo > 0:
                    r[2] -= prelievo
                    resto -= prelievo
        else:
            for r in righe:                      # fase 1: residui gia' aperti
                if resto <= 0:
                    break
                if r[3] > 0:
                    prelievo = min(r[3], resto)
                    r[3] = _arr(r[3] - prelievo)
                    resto -= prelievo
                    da_aperto += prelievo
            if resto > 0:                        # fase 2: nuove unita', FIFO
                for r in righe:
                    while resto > 0 and r[2] > 0:
                        r[2] -= 1
                        aperte += 1
                        if resto >= r[1]:
                            resto -= r[1]
                            r[3] = 0.0
                        else:
                            r[3] = _arr(r[1] - resto)
                            resto = 0.0
                    if resto <= 0:
                        break
        self.righe[id_p] = [r for r in righe if not (r[2] == 0 and r[3] == 0)]
        return {"consumato": da_coprire, "mancante": mancante,
                "da_aperto": da_aperto, "confezioni_aperte": aperte}


# ----------------------------------------------------------------------
# Simulazione di una ricetta
# ----------------------------------------------------------------------

def _scegli_membro(conn, inv, prodotti, id_gruppo, quantita, unita):
    """Membro del gruppo da usare: fattibile, poi piu' aperto, poi meno
    confezioni nuove, poi ID piu' basso."""
    migliore = None
    for id_p in membri_gruppo(conn, id_gruppo):
        q = quantita_inventario(quantita, unita, prodotti[id_p])
        if q is None:
            continue
        prova = inv.anteprima(id_p, q)
        chiave = (prova["mancante"] > 0, -min(1.0, prova["da_aperto"] / q),
                  prova["confezioni_aperte"], id_p)
        if migliore is None or chiave < migliore[0]:
            migliore = (chiave, id_p)
    return migliore[1] if migliore else None


def simula_ricetta(conn, inv: InventarioVirtuale, id_ricetta: int, prodotti: dict = None,
                   porzioni: int = 1, scelte: dict = None, ingredienti: list = None) -> dict:
    """Consuma la ricetta da `inv` (che viene MODIFICATO: passare inv.copia() per
    una prova a se'). Restituisce copertura dell'aperto, confezioni nuove,
    mancanti e il dettaglio per ingrediente."""
    prodotti = prodotti if prodotti is not None else carica_prodotti(conn)
    scelte = dict(scelte or {})
    dettaglio, mancanti, coperture = [], {}, []
    nuove = 0
    incompleto = False
    for ing in (ingredienti if ingredienti is not None else ingredienti_ricetta(conn, id_ricetta)):
        qta = ing["Quantita"] * porzioni
        id_p = ing["ID"]
        if id_p is None:
            gid = ing["ID_gruppo"]
            if gid not in scelte:
                scelte[gid] = _scegli_membro(conn, inv, prodotti, gid, qta, ing["Unita"])
            id_p = scelte[gid]
        if id_p is None:
            incompleto = True
            continue
        p = prodotti[id_p]
        q = quantita_inventario(qta, ing["Unita"], p)
        if q is None:
            incompleto = True
            continue
        r = inv.consuma(id_p, q)
        coperture.append(min(1.0, r["da_aperto"] / q) if q > 0 else 0.0)
        nuove += r["confezioni_aperte"]
        if r["mancante"] > 0:
            mancanti[id_p] = mancanti.get(id_p, 0.0) + r["mancante"]
        dettaglio.append({"ID": id_p, "richiesto": q, "unita": p["unita"],
                          "da_aperto": r["da_aperto"],
                          "confezioni_aperte": r["confezioni_aperte"],
                          "mancante": r["mancante"]})
    return {"dettaglio": dettaglio,
            "copertura_aperto": sum(coperture) / len(coperture) if coperture else 0.0,
            "confezioni_nuove": nuove,
            "mancanti": mancanti,
            "fattibile": not mancanti and not incompleto,
            "dati_incompleti": incompleto,
            "scelte_gruppo": scelte}


# ----------------------------------------------------------------------
# Verifica dei limiti
# ----------------------------------------------------------------------

def _converti(tipo, nutriente, valore, kcal, peso_kg):
    if valore is None:
        return None
    if tipo == "assoluto":
        return valore
    if tipo == "per_kg":
        return valore * peso_kg
    if tipo == "per_1000kcal":
        return valore * kcal / 1000.0
    if tipo == "pct_energia":
        if nutriente not in KCAL_PER_G:
            raise ValueError(f"pct_energia non applicabile a {nutriente}")
        return valore / 100.0 * kcal / KCAL_PER_G[nutriente]
    raise ValueError(f"Tipo di obiettivo sconosciuto: {tipo}")


def limiti_in_grammi(obiettivi: list, kcal_giorno: float, peso_kg: float) -> dict:
    """{nutriente: (minimo_g, massimo_g)}: le regole dello stesso nutriente si
    intersecano (vale il minimo piu' alto e il massimo piu' basso)."""
    limiti = {}
    for o in obiettivi:
        n = o["Nutriente"]
        mn = _converti(o["Tipo"], n, o["Minimo"], kcal_giorno, peso_kg)
        mx = _converti(o["Tipo"], n, o["Massimo"], kcal_giorno, peso_kg)
        cur_mn, cur_mx = limiti.get(n, (None, None))
        if mn is not None:
            cur_mn = mn if cur_mn is None else max(cur_mn, mn)
        if mx is not None:
            cur_mx = mx if cur_mx is None else min(cur_mx, mx)
        limiti[n] = (cur_mn, cur_mx)
    return limiti


def verifica_giorno(totali: dict, profilo: dict, obiettivi: list, extra_pct: float = 0.0) -> dict:
    """Confronta i totali del giorno con kcal e limiti. Ogni voce riporta valore,
    limiti (gia' allargati di extra_pct), scarto (>= 0) e direzione."""
    f_min, f_max = 1.0 - extra_pct / 100.0, 1.0 + extra_pct / 100.0
    voci = []

    def aggiungi(nome, unita, valore, mn, mx):
        mn = None if mn is None else mn * f_min
        mx = None if mx is None else mx * f_max
        sotto = (mn - valore) if mn is not None and valore < mn else 0.0
        sopra = (valore - mx) if mx is not None and valore > mx else 0.0
        scarto = max(sotto, sopra)
        voci.append({"nutriente": nome, "unita": unita, "valore": valore,
                     "minimo": mn, "massimo": mx, "scarto": scarto,
                     "direzione": "sotto" if sotto > 0 else ("sopra" if sopra > 0 else None),
                     "ok": scarto <= EPS})

    kt = profilo.get("Kcal_target")
    if kt:
        tol = profilo["Tolleranza_kcal_pct"] / 100.0
        aggiungi("Kcal", "kcal", totali["Kcal"], kt * (1 - tol), kt * (1 + tol))
    limiti = limiti_in_grammi(obiettivi, totali["Kcal"], profilo["Peso_kg"])
    for n in NUTRIENTI_LIMITATI:
        if n in limiti:
            aggiungi(n, "g", totali[n], *limiti[n])
    return {"voci": voci, "ok": all(v["ok"] for v in voci), "kcal_target_definito": bool(kt)}


# ----------------------------------------------------------------------
# Profilo
# ----------------------------------------------------------------------

def stima_mantenimento(peso_kg, altezza_cm, eta, sesso, attivita) -> int:
    """Mifflin-St Jeor x livello di attivita' (1.4 / 1.6 / 1.75)."""
    bmr = 10 * peso_kg + 6.25 * altezza_cm - 5 * eta + (5 if sesso == "M" else -161)
    return int(math.floor(bmr * PAL[attivita] + 0.5))   # metà verso l'alto (round() va al pari)


def imposta_profilo(conn, sesso, attivita, obiettivo, kcal_target=None):
    p = carica_profilo(conn)
    if kcal_target is None:
        if obiettivo != "mantenimento":
            raise ValueError("Per dimagrimento o aumento indica --kcal: "
                             "non applico deficit o surplus automatici.")
        kcal_target = stima_mantenimento(p["Peso_kg"], p["Altezza_cm"], p["Eta_anni"],
                                         sesso, attivita)
    conn.execute("UPDATE PROFILO SET Sesso = ?, Livello_attivita = ?, Obiettivo = ?, "
                 "Kcal_target = ? WHERE ID = 1", (sesso, attivita, obiettivo, kcal_target))
    conn.commit()
    return kcal_target


# ----------------------------------------------------------------------
# Riga di comando
# ----------------------------------------------------------------------

def _fmt(v):
    return "-" if v is None else f"{v:.1f}"


def esito(sim, prodotti, nomi):
    if sim["fattibile"]:
        return "fattibile"
    parti = [f"{nomi[i]} {q:.2f} {prodotti[i]['unita']}" for i, q in sim["mancanti"].items()]
    if sim["dati_incompleti"]:
        parti.append("dati incompleti")
    return "mancano: " + ", ".join(parti)


def riga_ricetta(nome, mac, sim, prodotti, nomi):
    print(f"{nome:<34}{mac['Kcal']:>6.0f}{mac['Proteine']:>7.1f}{mac['Carboidrati']:>7.1f}"
          f"{mac['Grassi']:>8.1f}{mac['Fibre']:>7.1f}{sim['copertura_aperto'] * 100:>7.0f}%"
          f"{sim['confezioni_nuove']:>6}  {esito(sim, prodotti, nomi)}")


INTESTAZIONE = (f"{'Ricetta':<34}{'kcal':>6}{'Prot':>7}{'Carb':>7}{'Grassi':>8}"
                f"{'Fibre':>7}{'Aperto':>8}{'Nuove':>6}  Esito")


def stampa_verifica(res):
    for v in res["voci"]:
        stato = "ok" if v["ok"] else f"{v['direzione'].upper()} di {v['scarto']:.1f}"
        print(f"  {v['nutriente']:<14}{v['valore']:>9.1f} {v['unita']:<5}"
              f"[{_fmt(v['minimo'])} - {_fmt(v['massimo'])}]  {stato}")


def cmd_ricette(conn):
    prodotti, nomi = carica_prodotti(conn), nomi_prodotti(conn)
    inv = InventarioVirtuale.da_db(conn)
    print("Ogni ricetta e' simulata sull'inventario attuale, indipendentemente dalle altre.")
    print(INTESTAZIONE)
    for id_r, nome in conn.execute("SELECT ID_ricetta, Nome FROM RICETTE ORDER BY ID_ricetta").fetchall():
        sim = simula_ricetta(conn, inv.copia(), id_r, prodotti)
        mac = macro_ricetta(conn, id_r, prodotti, sim["scelte_gruppo"])
        riga_ricetta(nome, mac, sim, prodotti, nomi)


def cmd_giorno(conn, nomi_ricette, kcal):
    prodotti, nomi = carica_prodotti(conn), nomi_prodotti(conn)
    profilo = carica_profilo(conn)
    if kcal:
        profilo["Kcal_target"] = kcal
    inv = InventarioVirtuale.da_db(conn)
    print("Le ricette consumano in sequenza le stesse scorte (inventario virtuale).")
    print(INTESTAZIONE)
    macros = []
    for nome in nomi_ricette:
        id_r = id_ricetta_da_nome(conn, nome)
        if id_r is None:
            sys.exit(f"Ricetta '{nome}' non trovata.")
        sim = simula_ricetta(conn, inv, id_r, prodotti)
        mac = macro_ricetta(conn, id_r, prodotti, sim["scelte_gruppo"])
        macros.append(mac)
        riga_ricetta(nome, mac, sim, prodotti, nomi)
    totali = somma_macro(macros)
    res = verifica_giorno(totali, profilo, carica_obiettivi(conn))
    kt = profilo.get("Kcal_target")
    print("\nVerifica del giorno" + (f" (target {kt:.0f} kcal +-{profilo['Tolleranza_kcal_pct']:.0f}%)" if kt else ""))
    stampa_verifica(res)
    if not res["kcal_target_definito"]:
        print("  Kcal: target non definito, controllo energetico saltato "
              "(usa --kcal oppure il comando 'profilo').")
    print("\nEsito complessivo:", "nei limiti" if res["ok"] else "FUORI LIMITE")


def cmd_profilo(conn, args):
    kcal = imposta_profilo(conn, args.sesso, args.attivita, args.obiettivo, args.kcal)
    print(f"Profilo aggiornato: sesso {args.sesso}, attivita' {args.attivita}, "
          f"obiettivo {args.obiettivo}, target {kcal:.0f} kcal/giorno.")


def main():
    parser = argparse.ArgumentParser(description="Motore di calcolo ricette e piano.")
    parser.add_argument("--db", default=DB_PATH, help="percorso del database")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ricette", help="macro e fattibilita' di ogni ricetta")
    g = sub.add_parser("giorno", help="verifica un giorno: una ricetta per pasto")
    g.add_argument("ricette", nargs="+")
    g.add_argument("--kcal", type=float, help="target kcal (altrimenti quello del profilo)")
    p = sub.add_parser("profilo", help="imposta sesso, attivita', obiettivo e target kcal")
    p.add_argument("--sesso", required=True, choices=["M", "F"])
    p.add_argument("--attivita", required=True, choices=sorted(PAL))
    p.add_argument("--obiettivo", required=True, choices=["mantenimento", "dimagrimento", "aumento"])
    p.add_argument("--kcal", type=float, help="obbligatorio per dimagrimento e aumento")
    args = parser.parse_args()

    conn = get_connection(args.db)
    try:
        if args.cmd == "ricette":
            cmd_ricette(conn)
        elif args.cmd == "giorno":
            cmd_giorno(conn, args.ricette, args.kcal)
        else:
            cmd_profilo(conn, args)
    except ValueError as e:
        sys.exit(str(e))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
