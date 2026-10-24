"""
pianificatore.py

Pianificatore giornaliero: sceglie una ricetta per pasto cercando il miglior
compromesso tra limiti nutrizionali e uso dell'aperto. Lavora su un inventario
virtuale (motore.py): scrive sul database solo con salva_piano().

Come sceglie (in ordine di importanza)
  1. Piano nei limiti (kcal e nutrienti, con le tolleranze del profilo). Lo
     sforamento extra (Extra_aperto_pct) e' ammesso solo se il piano finisce un
     residuo aperto. Se nessun piano rispetta i limiti vince quello con lo
     scarto relativo minore (la quantita' mancante e' riportata).
  2. Meno prodotti mancanti (finiscono nella lista della spesa del piano).
  3. Punteggio = PESO_APERTO * copertura dell'aperto
               - PESO_NUOVE  * confezioni nuove aperte
               - PESO_QUOTA  * scarto dalla ripartizione dei pasti (es. 25/40/35)
               - PENALITA_EXTRA se usa la tolleranza extra.

Regole
  - Ricette diverse nello stesso giorno e niente ricette cucinate negli ultimi
    Giorni_no_ripetizione giorni; se mancano alternative la ripetizione e' ammessa
    e segnalata in "avvisi". Le proposte salvate (Stato 'proposto') non vincolano i
    giorni vicini: contano solo cotture reali o confermate, cosi' ripianificare e'
    ripetibile.
  - I pasti gia' confermati/saltati/consumati nel DB, o passati con `fissi`,
    restano fermi e consumano le loro scorte.

Uso (dalla cartella del progetto):
    python pianificatore.py giorno --kcal 2000
    python pianificatore.py giorno --data 2026-10-24 --blocca pranzo="Pollo e riso" --salta cena --salva
    python pianificatore.py settimana --da 2026-10-26 --giorni 7 --salva
    python pianificatore.py spesa
Il target kcal e' quello del profilo (python motore.py profilo ...) se manca --kcal.
"""

import argparse
import sys
from collections import Counter
from datetime import date, timedelta

import motore as m
from crea_RELSCHEMA import get_connection, DB_PATH

PESO_APERTO, PESO_NUOVE, PESO_QUOTA, PENALITA_EXTRA = 3.0, 1.0, 5.0, 1.0
MAX_CANDIDATI = 12      # ricette valutate per pasto: le migliori sul punteggio singolo
STATI = {"ok": "nei limiti",
         "extra": "nei limiti con tolleranza extra (finisce un aperto)",
         "fuori_limite": "FUORI LIMITE (e' il piano piu' vicino ai limiti)"}


# Un avanzo gia' pronto: nessun ingrediente da consumare, conta come aperto al 100%.
SIM_AVANZO = {"dettaglio": [], "copertura_aperto": 1.0, "confezioni_nuove": 0, "mancanti": {},
              "fattibile": True, "dati_incompleti": False, "scelte_gruppo": {}}


def _punteggio(copertura, nuove, scarto_quota):
    return PESO_APERTO * copertura - PESO_NUOVE * nuove - PESO_QUOTA * scarto_quota


def _deviazione(verifica):
    """Somma degli scarti, ciascuno relativo al limite violato."""
    return sum(v["scarto"] / ((v["minimo"] if v["direzione"] == "sotto" else v["massimo"]) or 1.0)
               for v in verifica["voci"] if not v["ok"])


def pianifica_giorno(conn, data, kcal=None, inv=None, fissi=None, cotte=None, avanzi_usati=None):
    """Piano di `data` (YYYY-MM-DD). `fissi` = {pasto: ID_ricetta, oppure None per saltare}.
    `inv` (InventarioVirtuale) permette di concatenare piu' giorni; non viene modificato.
    `cotte` = [(ID_ricetta, data)] gia' pianificate in memoria: contano per le ripetizioni."""
    prodotti, obiettivi, profilo = m.carica_prodotti(conn), m.carica_obiettivi(conn), m.carica_profilo(conn)
    if kcal:
        profilo["Kcal_target"] = kcal
    target = profilo["Kcal_target"]
    if not target:
        raise ValueError("Target kcal non definito: usa --kcal oppure 'python motore.py profilo ...'.")
    pasti = conn.execute("SELECT Nome, Quota_energia_pct FROM PASTI ORDER BY Ordine").fetchall()
    ordine = {nome: i for i, (nome, _) in enumerate(pasti)}
    quote = {nome: q / 100.0 for nome, q in pasti}
    nome_ric = dict(conn.execute("SELECT ID_ricetta, Nome FROM RICETTE").fetchall())
    ing = {rid: m.ingredienti_ricetta(conn, rid) for rid in nome_ric}
    inv = (inv if inv is not None else m.InventarioVirtuale.da_db(conn)).copia()
    aperti_prima = {i for i in inv.righe if inv.residuo_aperto(i) > 0}
    avvisi = []

    def voce(pasto, rid, sim, origine, id_cott=None):
        mac = m.macro_ricetta(conn, rid, prodotti, sim["scelte_gruppo"], ingredienti=ing[rid]) if rid else None
        return {"pasto": pasto, "ID_ricetta": rid, "ricetta": nome_ric.get(rid), "origine": origine,
                "sim": sim, "macro": mac, "ID_cottura": id_cott, "avanzo": bool(id_cott) and origine == "pianificato"}

    # --- pasti fermi: gia' decisi nel DB (non 'proposto') o richiesti dal chiamante ---
    fissi = fissi or {}
    for pasto in fissi:
        if pasto not in quote:
            raise ValueError(f"Pasto sconosciuto: {pasto}")
    # un pasto del DB gia' cucinato (o che mangia un avanzo) non consuma di nuovo gli ingredienti
    nel_db = {pasto: (rid, "db", id_c, stato_c == "pianificata") for pasto, rid, id_c, stato_c in conn.execute(
        "SELECT p.Pasto, c.ID_ricetta, p.ID_cottura, c.Stato FROM PIANO p LEFT JOIN COTTURE c USING (ID_cottura) "
        "WHERE p.Data = ? AND p.Stato != 'proposto'", (data,)).fetchall()}
    bloccati = {**nel_db, **{p: (r, "richiesto", None, True) for p, r in fissi.items()}}
    fisse = {}
    for pasto, _ in pasti:
        if pasto in bloccati:
            rid, origine, id_c, da_cucinare = bloccati[pasto]
            sim = (m.simula_ricetta(conn, inv, rid, prodotti, ingredienti=ing[rid]) if da_cucinare
                   else SIM_AVANZO) if rid else None
            fisse[pasto] = voce(pasto, rid, sim, origine, id_c)

    # --- candidati per i pasti liberi ---
    usati = {v["ID_ricetta"] for v in fisse.values() if v["ID_ricetta"]}
    giorni_no_rip = profilo["Giorni_no_ripetizione"]
    recenti = {r[0] for r in conn.execute(
        "SELECT DISTINCT c.ID_ricetta FROM COTTURE c LEFT JOIN PIANO p USING (ID_cottura) "
        "WHERE c.Data_Cottura != ? AND ABS(julianday(c.Data_Cottura) - julianday(?)) < ? "
        "AND COALESCE(p.Stato, '') != 'proposto'", (data, data, giorni_no_rip))}
    recenti |= {rid for rid, d in (cotte or []) if d != data
                and abs((date.fromisoformat(d) - date.fromisoformat(data)).days) < giorni_no_rip}

    # avanzi: porzioni gia' cucinate e non ancora assegnate a pasti confermati ne' usate in questa esecuzione
    avanzi = {}
    for id_c, rid, residue, riservate in conn.execute(
            "SELECT c.ID_cottura, c.ID_ricetta, c.Porzioni_residue, (SELECT COUNT(*) FROM PIANO p "
            "WHERE p.ID_cottura = c.ID_cottura AND p.Stato = 'confermato') FROM COTTURE c "
            "WHERE c.Stato = 'cucinata' AND c.Porzioni_residue > 0"):
        if residue - riservate - (avanzi_usati or {}).get(id_c, 0) > 0:
            avanzi[id_c] = (rid, residue - riservate - (avanzi_usati or {}).get(id_c, 0))

    def preselezione(rid, id_c, pasto):
        sim = SIM_AVANZO if id_c else m.simula_ricetta(conn, inv.copia(), rid, prodotti, ingredienti=ing[rid])
        kcal_r = m.macro_ricetta(conn, rid, prodotti, sim["scelte_gruppo"], ingredienti=ing[rid])["Kcal"]
        scarto = abs(kcal_r / target - quote[pasto])
        return (len(sim["mancanti"]) + sim["dati_incompleti"],
                -_punteggio(sim["copertura_aperto"], sim["confezioni_nuove"], scarto))

    liberi, cands = [], {}
    for pasto, _ in pasti:
        if pasto in fisse:
            continue
        per_pasto = [r for (r,) in conn.execute("SELECT ID_ricetta FROM RICETTA_PASTI WHERE Pasto = ? "
                                                "ORDER BY ID_ricetta", (pasto,))]
        tutte = [r for r in per_pasto if r not in usati]
        ammesse = [r for r in tutte if r not in recenti]
        con_avanzo = [(rid, id_c) for id_c, (rid, _) in avanzi.items() if rid in per_pasto]
        if tutte and not ammesse and not con_avanzo:
            avvisi.append(f"{pasto}: ricette usate di recente, ripetizione ammessa")
        pool = [(r, None) for r in (ammesse or ([] if con_avanzo else tutte))] + con_avanzo
        if not pool:
            avvisi.append(f"{pasto}: nessuna ricetta disponibile")
            continue
        cands[pasto] = sorted(pool, key=lambda c: preselezione(c[0], c[1], pasto))[:MAX_CANDIDATI]
        liberi.append(pasto)

    # --- ricerca: ogni combinazione di candidati, con le scorte condivise tra i pasti ---
    def esplora(i, inv_i, scelti, distinte):
        if i == len(liberi):
            yield scelti, inv_i
            return
        pasto = liberi[i]
        for rid, id_c in cands[pasto]:
            if id_c is None and distinte and rid in {s["ID_ricetta"] for s in scelti if not s["ID_cottura"]}:
                continue
            if id_c and sum(1 for s in scelti if s["ID_cottura"] == id_c) >= avanzi[id_c][1]:
                continue                                  # porzioni dell'avanzo gia' tutte assegnate
            inv_n = inv_i.copia()
            sim = SIM_AVANZO if id_c else m.simula_ricetta(conn, inv_n, rid, prodotti, ingredienti=ing[rid])
            yield from esplora(i + 1, inv_n, scelti + [voce(pasto, rid, sim, "pianificato", id_c)], distinte)

    migliore = None
    for distinte in (True, False):          # se non basta, si ammettono ricette ripetute nel giorno
        for scelti, inv_fin in esplora(0, inv, [], distinte):
            voci = sorted([*fisse.values(), *scelti], key=lambda v: ordine[v["pasto"]])
            presenti = [v for v in voci if v["ID_ricetta"]]
            sims = [v["sim"] for v in presenti]
            totali = m.somma_macro([v["macro"] for v in presenti])
            v0 = m.verifica_giorno(totali, profilo, obiettivi)
            vx = m.verifica_giorno(totali, profilo, obiettivi, profilo["Extra_aperto_pct"])
            finisce_aperto = any(inv_fin.residuo_aperto(i) == 0 for i in aperti_prima)
            stato = "ok" if v0["ok"] else "extra" if vx["ok"] and finisce_aperto else "fuori_limite"
            q_tot = sum(quote[v["pasto"]] for v in presenti) or 1.0
            scarto_q = sum(abs(v["macro"]["Kcal"] / (totali["Kcal"] or 1.0) - quote[v["pasto"]] / q_tot)
                           for v in presenti)
            punteggio = _punteggio(sum(s["copertura_aperto"] for s in sims),
                                   sum(s["confezioni_nuove"] for s in sims), scarto_q)
            punteggio -= PENALITA_EXTRA if stato == "extra" else 0.0
            mancanti = {}
            for s in sims:
                for i, q in s["mancanti"].items():
                    mancanti[i] = mancanti.get(i, 0.0) + q
            problemi = len(mancanti) + sum(s["dati_incompleti"] for s in sims)
            fuori = stato == "fuori_limite"
            chiave = (fuori, _deviazione(v0) if fuori else 0.0, problemi, -punteggio)
            if migliore is None or chiave < migliore[0]:
                migliore = (chiave, {"data": data, "pasti": voci, "totali": totali, "verifica": v0,
                                     "stato": stato, "mancanti": mancanti, "punteggio": punteggio,
                                     "kcal_target": target, "inventario": inv_fin})
        if migliore:
            if not distinte:
                avvisi.append("ricette ripetute nello stesso giorno: non ci sono abbastanza alternative")
            break
    piano = migliore[1]
    piano["avvisi"] = avvisi
    return piano


# ----------------------------------------------------------------------
# Salvataggio e lista della spesa derivata dal piano
# ----------------------------------------------------------------------

def _applica_piano(conn, inv, prodotti, prima_di=None):
    """Consuma da `inv` (modificandolo), in ordine di data, le ricette del piano non ancora
    consumate (proposto o confermato) con Data < prima_di (tutte se None).
    Restituisce i mancanti {ID: quantita}."""
    mancanti = {}
    voci = conn.execute(
        "SELECT c.ID_ricetta FROM PIANO p JOIN COTTURE c USING (ID_cottura) "
        "WHERE p.Stato IN ('proposto', 'confermato') AND c.Stato = 'pianificata' AND (? IS NULL OR p.Data < ?) "
        "ORDER BY p.Data, (SELECT Ordine FROM PASTI WHERE Nome = p.Pasto)", (prima_di, prima_di)).fetchall()
    for (rid,) in voci:
        for i, q in m.simula_ricetta(conn, inv, rid, prodotti)["mancanti"].items():
            mancanti[i] = mancanti.get(i, 0.0) + q
    return mancanti


def aggiorna_lista_spesa_piano(conn):
    """Ricalcola LISTA_SPESA_PIANO da zero con i mancanti dell'intero piano (proposto o confermato)."""
    mancanti = _applica_piano(conn, m.InventarioVirtuale.da_db(conn), m.carica_prodotti(conn))
    conn.execute("DELETE FROM LISTA_SPESA_PIANO")
    conn.executemany("INSERT INTO LISTA_SPESA_PIANO (ID, Quantita_Mancante) VALUES (?, ?)",
                     [(i, q) for i, q in mancanti.items() if q > 0])
    conn.commit()


def salva_piano(conn, piano, aggiorna_spesa=True):
    """Scrive il piano: sostituisce le proposte del giorno e i pasti richiesti, lascia
    intatti quelli gia' presenti nel DB (confermati/consumati/saltati). Con
    aggiorna_spesa=False non ricalcola la lista della spesa e NON fa commit."""
    d = piano["data"]
    nuovi = [v for v in piano["pasti"] if v["origine"] != "db"]
    marcatori = ",".join("?" * len(nuovi))
    vecchie = conn.execute(
        f"SELECT ID_voce, ID_cottura FROM PIANO WHERE Data = ? AND Stato != 'consumato' "
        f"AND (Stato = 'proposto' OR Pasto IN ({marcatori}))", [d, *[v["pasto"] for v in nuovi]]).fetchall()
    conn.executemany("DELETE FROM PIANO WHERE ID_voce = ?", [(i,) for i, _ in vecchie])
    conn.executemany("DELETE FROM COTTURE WHERE ID_cottura = ? AND Stato = 'pianificata'",
                     [(c,) for _, c in vecchie if c])
    for v in nuovi:
        cottura = v["ID_cottura"] if v["ID_ricetta"] else None
        if v["ID_ricetta"] and cottura is None:
            cottura = conn.execute(
                "INSERT INTO COTTURE (ID_ricetta, Data_Cottura, Porzioni_totali, Porzioni_residue) "
                "VALUES (?, ?, 1, 1)", (v["ID_ricetta"], d)).lastrowid
        stato = "saltato" if cottura is None else ("proposto" if v["origine"] == "pianificato" else "confermato")
        conn.execute("INSERT INTO PIANO (Data, Pasto, ID_cottura, Stato) VALUES (?, ?, ?, ?)",
                     (d, v["pasto"], cottura, stato))
    if aggiorna_spesa:
        aggiorna_lista_spesa_piano(conn)


def segna_cucinato(conn, data, pasto, porzioni=1):
    """Segna il pasto come consumato. Se la ricetta va cucinata consuma gli ingredienti (x `porzioni`)
    dall'inventario reale in una sola transazione; con porzioni > 1 le altre diventano avanzi
    (Porzioni_residue) mangiabili nei pasti successivi senza consumare altro. Se il pasto usa un
    avanzo consuma solo una porzione residua. Rifiuta, senza scrivere nulla, se non e' possibile."""
    r = conn.execute("SELECT p.ID_cottura, c.ID_ricetta, p.Stato, c.Stato, c.Porzioni_residue FROM PIANO p "
                     "LEFT JOIN COTTURE c USING (ID_cottura) WHERE p.Data = ? AND p.Pasto = ?",
                     (data, pasto)).fetchone()
    if r is None or r[2] in ("consumato", "saltato"):
        raise ValueError(f"Nessun pasto da cucinare per {pasto} del {data}.")
    id_cott, rid, _, stato_cottura, residue = r
    if porzioni < 1:
        raise ValueError("Le porzioni devono essere almeno 1.")
    if stato_cottura == "cucinata":                       # avanzo gia' pronto
        if porzioni != 1 or residue < 1:
            raise ValueError("Questo pasto usa un avanzo: non ci sono altre porzioni da cucinare "
                             "oppure non ne restano.")
        conn.execute("UPDATE COTTURE SET Porzioni_residue = Porzioni_residue - 1 WHERE ID_cottura = ?", (id_cott,))
    else:
        prodotti, inv = m.carica_prodotti(conn), m.InventarioVirtuale.da_db(conn)
        sim = m.simula_ricetta(conn, inv, rid, prodotti, porzioni=porzioni)
        if not sim["fattibile"]:
            nomi = m.nomi_prodotti(conn)
            raise ValueError("Ingredienti mancanti: " + ", ".join(
                f"{nomi[i]} {q:.2f} {prodotti[i]['unita']}" for i, q in sim["mancanti"].items()))
        inv.scrivi(conn, {d["ID"] for d in sim["dettaglio"]})
        conn.execute("UPDATE COTTURE SET Stato = 'cucinata', Porzioni_totali = ?, Porzioni_residue = ? "
                     "WHERE ID_cottura = ?", (porzioni, porzioni - 1, id_cott))
    conn.execute("UPDATE PIANO SET Stato = 'consumato' WHERE Data = ? AND Pasto = ?", (data, pasto))
    aggiorna_lista_spesa_piano(conn)

# ----------------------------------------------------------------------
# Piu' giorni
# ----------------------------------------------------------------------

def pianifica_settimana(conn, inizio, giorni=7, kcal=None, fissi=None):
    """Piani di `giorni` giorni consecutivi da `inizio`. Parte dall'inventario meno i pasti gia'
    pianificati prima di `inizio` (le scorte restano prenotate), poi passa le scorte da un
    giorno al successivo. `fissi` = {data: {pasto: ID_ricetta | None}}. Non scrive nel DB."""
    inv = m.InventarioVirtuale.da_db(conn)
    _applica_piano(conn, inv, m.carica_prodotti(conn), prima_di=inizio)
    piani, cotte, avanzi_usati = [], [], {}
    for k in range(giorni):
        d = (date.fromisoformat(inizio) + timedelta(days=k)).isoformat()
        p = pianifica_giorno(conn, d, kcal, inv, (fissi or {}).get(d), cotte, avanzi_usati)
        cotte += [(v["ID_ricetta"], d) for v in p["pasti"] if v["ID_ricetta"] and v["origine"] != "db"]
        for v in p["pasti"]:
            if v["avanzo"]:
                avanzi_usati[v["ID_cottura"]] = avanzi_usati.get(v["ID_cottura"], 0) + 1
        inv = p["inventario"]
        piani.append(p)
    return piani


def salva_settimana(conn, piani):
    """Salva tutti i giorni in un'unica transazione e ricalcola la lista della spesa una volta."""
    for p in piani:
        salva_piano(conn, p, aggiorna_spesa=False)
    aggiorna_lista_spesa_piano(conn)


# ----------------------------------------------------------------------
# Riga di comando
# ----------------------------------------------------------------------

def stampa_piano(conn, p):
    prodotti, nomi = m.carica_prodotti(conn), m.nomi_prodotti(conn)
    print(f"Piano del {p['data']}: {STATI[p['stato']]}  (target {p['kcal_target']:.0f} kcal)")
    print(m.INTESTAZIONE)
    for v in p["pasti"]:
        if v["ID_ricetta"] is None:
            print(f"{v['pasto']}: (saltato)")
        else:
            m.riga_ricetta(f"{v['pasto']}: {v['ricetta']}" + (" (avanzo)" if v["avanzo"] else ""), v["macro"], v["sim"], prodotti, nomi)
    fermi = [f"{v['pasto']} ({'gia nel piano' if v['origine'] == 'db' else 'richiesto'})"
             for v in p["pasti"] if v["origine"] != "pianificato"]
    if fermi:
        print("Pasti fermi: " + ", ".join(fermi))
    print("\nVerifica del giorno")
    m.stampa_verifica(p["verifica"])
    if p["mancanti"]:
        print("\nDa comprare: " + ", ".join(f"{nomi[i]} {q:.2f} {prodotti[i]['unita']}"
                                             for i, q in p["mancanti"].items()))
    for a in p["avvisi"]:
        print("Attenzione:", a)


def stampa_settimana(conn, piani):
    prodotti, nomi = m.carica_prodotti(conn), m.nomi_prodotti(conn)
    print(f"{'Data':<12}{'Stato':<14}{'kcal':>6}  Pasti")
    mancanti, avvisi = {}, Counter()
    for p in piani:
        pasti = " / ".join((v["ricetta"] + (" (avanzo)" if v["avanzo"] else "")) if v["ricetta"] else "(saltato)"
                           for v in p["pasti"])
        print(f"{p['data']:<12}{p['stato']:<14}{p['totali']['Kcal']:>6.0f}  {pasti}")
        fuori = "; ".join(f"{v['nutriente']} {v['direzione']} di {v['scarto']:.1f}"
                          for v in p["verifica"]["voci"] if not v["ok"])
        if fuori:
            print(f"{'':<12}scostamenti: {fuori}")
        for i, q in p["mancanti"].items():
            mancanti[i] = mancanti.get(i, 0.0) + q
        avvisi.update(p["avvisi"])
    stati = Counter(p["stato"] for p in piani)
    print(f"\nGiorni: {stati['ok']} ok, {stati['extra']} con tolleranza extra, "
          f"{stati['fuori_limite']} fuori limite")
    if mancanti:
        print("Da comprare: " + ", ".join(f"{nomi[i]} {q:.2f} {prodotti[i]['unita']}"
                                          for i, q in mancanti.items()))
    for testo, n in avvisi.items():
        print(f"Attenzione: {testo}" + (f" (x{n})" if n > 1 else ""))


def _data(testo):
    try:
        return date.fromisoformat(testo).isoformat()
    except ValueError:
        sys.exit("Data non valida: usa il formato YYYY-MM-DD.")


def _fissi_da_argomenti(conn, bloccati, saltati):
    fissi = {}
    for arg in bloccati or []:
        pasto, _, nome = arg.partition("=")
        rid = m.id_ricetta_da_nome(conn, nome)
        if rid is None:
            sys.exit(f"--blocca {arg!r}: usa PASTO=\"Nome ricetta\" con una ricetta esistente")
        fissi[pasto] = rid
    return {**fissi, **{pasto: None for pasto in saltati or []}}


def main():
    parser = argparse.ArgumentParser(description="Pianificatore giornaliero.")
    parser.add_argument("--db", default=DB_PATH, help="percorso del database")
    sub = parser.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("giorno", help="propone il piano di un giorno")
    g.add_argument("--data", default=date.today().isoformat(), help="YYYY-MM-DD (default: oggi)")
    g.add_argument("--kcal", type=float, help="target kcal (altrimenti quello del profilo)")
    g.add_argument("--blocca", action="append", metavar='PASTO="RICETTA"', help="fissa la ricetta di un pasto")
    g.add_argument("--salta", action="append", metavar="PASTO", help="salta un pasto")
    g.add_argument("--salva", action="store_true", help="salva il piano e aggiorna la lista della spesa")
    w = sub.add_parser("settimana", help="propone il piano di piu' giorni consecutivi")
    w.add_argument("--da", default=date.today().isoformat(), help="primo giorno YYYY-MM-DD (default: oggi)")
    w.add_argument("--giorni", type=int, default=7, help="quanti giorni, da 1 a 31 (default 7)")
    w.add_argument("--kcal", type=float, help="target kcal (altrimenti quello del profilo)")
    w.add_argument("--salva", action="store_true", help="salva i piani e aggiorna la lista della spesa")
    sub.add_parser("spesa", help="ricalcola e mostra la lista della spesa (consumi + piano)")
    args = parser.parse_args()

    conn = get_connection(args.db)
    try:
        if args.cmd == "giorno":
            piano = pianifica_giorno(conn, _data(args.data), args.kcal,
                                     fissi=_fissi_da_argomenti(conn, args.blocca, args.salta))
            stampa_piano(conn, piano)
            if args.salva:
                salva_piano(conn, piano)
                print("\nPiano salvato. Lista della spesa del piano aggiornata.")
        elif args.cmd == "settimana":
            if not 1 <= args.giorni <= 31:
                sys.exit("--giorni deve essere tra 1 e 31.")
            piani = pianifica_settimana(conn, _data(args.da), args.giorni, args.kcal)
            stampa_settimana(conn, piani)
            if args.salva:
                salva_settimana(conn, piani)
                print("\nPiani salvati. Lista della spesa del piano aggiornata.")
        else:
            aggiorna_lista_spesa_piano(conn)
            print("Lista della spesa (consumi + piano):")
            for i, nome, q, u in conn.execute(
                    "SELECT ID, Nome, Quantita_Mancante, Unita_di_misura FROM VISTA_LISTA_SPESA ORDER BY ID"):
                print(f"  [{i}] {nome:<28}{q:>8.2f} {u}")
    except ValueError as e:
        sys.exit(str(e))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
