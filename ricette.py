"""
ricette.py - Inserimento, import ed eliminazione delle ricette (quantita' per UNA porzione).

Uso da riga di comando:
    python ricette.py importa ricette.json
    python ricette.py importa ricette.csv

Formato JSON: [{"nome": "...", "pasti": ["pranzo", "cena"], "note": "...",
                "ingredienti": [{"prodotto": "Riso", "quantita": 100, "unita": "g"},
                                {"gruppo": "Cereale", "quantita": 80, "unita": "g", "quanto_basta": false}]}]
Formato CSV (una riga per ingrediente, separatore ';' o ','): colonne
    ricetta;pasti;prodotto;quantita;unita;quanto_basta      (pasti separati da '|', es. pranzo|cena)
Prodotti e gruppi si indicano per nome (anche alias, senza distinzione tra maiuscole e minuscole).
"""

import argparse
import csv
import json
import os
import sqlite3
import sys

import motore as m
from crea_RELSCHEMA import get_connection, DB_PATH

UNITA = ("g", "kg", "ml", "L", "pz", "cucchiaio", "cucchiaino")


def _id(conn, tabella, colonna, nome):
    r = conn.execute(f"SELECT {colonna} FROM {tabella} WHERE Nome = ? COLLATE NOCASE", (nome,)).fetchone()
    return r[0] if r else None


def _si(valore):
    return str(valore).strip().lower() in ("1", "si", "sì", "true", "x")


def crea_ricetta(conn, nome, pasti, ingredienti, note=None) -> dict:
    """Crea una ricetta in modo atomico. `ingredienti` = [(nome, quantita, unita)] oppure dict con
    'prodotto' o 'gruppo', 'quantita', 'unita', 'quanto_basta'. ValueError elenca tutti i problemi."""
    nome, errori, righe = (nome or "").strip(), [], []
    if not nome:
        errori.append("manca il nome")
    validi = {r[0] for r in conn.execute("SELECT Nome FROM PASTI")}
    errori += [f"pasto sconosciuto '{p}' (validi: {', '.join(sorted(validi))})" for p in pasti if p not in validi]
    if not pasti:
        errori.append("indica almeno un pasto")
    for ing in ingredienti:
        d = ing if isinstance(ing, dict) else dict(zip(("prodotto", "quantita", "unita"), ing))
        voce = d.get("prodotto") or d.get("gruppo")
        id_p, id_g = _id(conn, "ALIAS", "ID", voce), None
        if id_p is None:
            id_g = _id(conn, "GRUPPI_SOSTITUIBILI", "ID_gruppo", voce)
        try:
            q = float(str(d.get("quantita")).replace(",", "."))
        except ValueError:
            q = 0
        if id_p is None and id_g is None:
            errori.append(f"ingrediente sconosciuto '{voce}'")
        if q <= 0:
            errori.append(f"quantita' non valida per '{voce}'")
        if d.get("unita") not in UNITA:
            errori.append(f"unita' non valida per '{voce}' (valide: {', '.join(UNITA)})")
        righe.append((id_p, id_g, q, d.get("unita"), int(_si(d.get("quanto_basta", 0)))))
    if not ingredienti:
        errori.append("indica almeno un ingrediente")
    if errori:
        raise ValueError(f"Ricetta '{nome}' non valida: " + "; ".join(errori))
    try:
        rid = conn.execute("INSERT INTO RICETTE (Nome, Note) VALUES (?, ?)", (nome, note)).lastrowid
    except sqlite3.IntegrityError:
        raise ValueError(f"Esiste gia' una ricetta chiamata '{nome}'.")
    conn.executemany("INSERT INTO RICETTA_PASTI (ID_ricetta, Pasto) VALUES (?, ?)", [(rid, p) for p in pasti])
    conn.executemany("INSERT INTO RICETTA_INGREDIENTI (ID_ricetta, ID, ID_gruppo, Quantita, Unita, Quanto_basta) "
                     "VALUES (?, ?, ?, ?, ?, ?)", [(rid, *r) for r in righe])
    conn.commit()
    avvisi = []
    if m.macro_ricetta(conn, rid)["Dati_incompleti"]:
        avvisi.append("mancano dati di conversione (g per pezzo, densita') o nutrienti opzionali: "
                      "i macro possono essere sottostimati")
    return {"ID_ricetta": rid, "avvisi": avvisi}


def elimina_ricetta(conn, nome) -> None:
    rid = _id(conn, "RICETTE", "ID_ricetta", nome)
    if rid is None:
        raise ValueError(f"Ricetta '{nome}' non trovata.")
    if conn.execute("SELECT 1 FROM COTTURE WHERE ID_ricetta = ?", (rid,)).fetchone():
        raise ValueError(f"'{nome}' e' usata in piani o cotture: non si puo' eliminare.")
    conn.execute("DELETE FROM RICETTE WHERE ID_ricetta = ?", (rid,))
    conn.commit()


def _da_csv(percorso) -> list:
    with open(percorso, newline="", encoding="utf-8-sig") as f:
        testo = f.read()
    separatore = ";" if ";" in testo.splitlines()[0] else ","
    ricette = {}
    for r in csv.DictReader(testo.splitlines(), delimiter=separatore):
        voce = ricette.setdefault(r["ricetta"], {"nome": r["ricetta"], "ingredienti": [],
                                                 "pasti": [p.strip() for p in r["pasti"].split("|") if p.strip()]})
        voce["ingredienti"].append({"prodotto": r["prodotto"], "quantita": r["quantita"], "unita": r["unita"],
                                    "quanto_basta": r.get("quanto_basta", "")})
    return list(ricette.values())


def importa(conn, percorso):
    """Importa un file .json o .csv. Ogni ricetta e' atomica: le non valide si saltano e si riportano.
    Restituisce (ricette create, elenco errori)."""
    try:
        if os.path.splitext(percorso)[1].lower() == ".json":
            with open(percorso, encoding="utf-8") as f:
                elenco = json.load(f)
            if not isinstance(elenco, list):
                raise ValueError("il JSON deve essere un elenco di ricette")
        else:
            elenco = _da_csv(percorso)
    except (OSError, ValueError, KeyError) as e:
        raise ValueError(f"File non leggibile: {e}")
    creati, errori = 0, []
    for r in elenco:
        try:
            crea_ricetta(conn, r.get("nome"), r.get("pasti", []), r.get("ingredienti", []), r.get("note"))
            creati += 1
        except ValueError as e:
            errori.append(str(e))
    return creati, errori


def main():
    parser = argparse.ArgumentParser(description="Gestione ricette.")
    parser.add_argument("--db", default=DB_PATH)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("importa", help="importa ricette da .json o .csv").add_argument("file")
    args = parser.parse_args()
    conn = get_connection(args.db)
    try:
        creati, errori = importa(conn, args.file)
    except ValueError as e:
        sys.exit(str(e))
    finally:
        conn.close()
    print(f"Ricette importate: {creati}")
    for e in errori:
        print("Saltata:", e)


if __name__ == "__main__":
    main()
