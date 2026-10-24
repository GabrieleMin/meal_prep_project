"""
crea_RICETTE.py

Estende inventario.db con le tabelle per ricette, cotture, piano dei pasti e
obiettivi nutrizionali. E' una migrazione ADDITIVA e IDEMPOTENTE: si puo'
rilanciare quando si vuole, non modifica ne' cancella i dati esistenti.

Uso (nella cartella del progetto):
    python crea_RICETTE.py

Convenzioni di progetto
-----------------------
- Le quantita' di RICETTA_INGREDIENTI sono riferite a UNA porzione.
  Le porzioni sono fisse (non scalabili); per taglie diverse si inseriscono
  ricette distinte (es. "Pasta al pomodoro M" / "... L").
- I macro in NUTRIENTI restano sempre per 100 g. Per convertire:
    pz -> grammi: NUTRIENTI.Grammi_per_pezzo
    L / ml / cucchiai -> grammi: NUTRIENTI.Densita_kg_per_L (= g/ml)
  Se il dato di conversione manca, l'ingrediente non viene sommato e la
  ricetta e' segnalata con Dati_incompleti = 1 (nessun valore inventato).
- Un nutriente opzionale NULL (zuccheri, saturi, fibre, sale) conta come 0
  e segnala anch'esso Dati_incompleti = 1.
- Un ingrediente e' un prodotto (ID) OPPURE un gruppo sostituibile
  (ID_gruppo): i gruppi servono per le alternative (riso o orzo) e li
  risolve il pianificatore, non le viste SQL.
- COTTURE copre anche le "porzioni pronte": una cottura 'cucinata' con
  Porzioni_residue > 0 contiene avanzi mangiabili nei pasti successivi.
  La regola "non ripetere la stessa ricetta per N giorni" si applica per
  cottura, non per singolo pasto.
- Lista della spesa: LISTA_SPESA (origine consumo) resta com'e'; le voci
  derivate dal piano stanno in LISTA_SPESA_PIANO, ricalcolabile da zero.
  VISTA_LISTA_SPESA le somma.
"""

import sqlite3

from crea_RELSCHEMA import get_connection, create_schema, DB_PATH

VERSIONE_SCHEMA = 1  # PRAGMA user_version dopo questa migrazione

PASTI_DEFAULT = [  # (Nome, Ordine, Quota_energia_pct)
    ("colazione", 1, 25.0),
    ("pranzo", 2, 40.0),
    ("cena", 3, 35.0),
]

# (Nutriente, Tipo, Minimo, Massimo). Piu' regole per lo stesso nutriente si
# applicano tutte insieme (vale l'intersezione). Valori di riferimento LARN;
# il tetto proteico 1.6 g/kg e' una scelta di progetto, non dei LARN.
OBIETTIVI_DEFAULT = [
    ("Proteine",      "per_kg",       0.9,  1.6),
    ("Carboidrati",   "pct_energia",  45.0, 60.0),
    ("Grassi",        "pct_energia",  20.0, 35.0),
    ("Zuccheri",      "pct_energia",  None, 15.0),
    ("Grassi_saturi", "pct_energia",  None, 10.0),
    ("Fibre",         "assoluto",     25.0, None),
    ("Fibre",         "per_1000kcal", 12.6, None),
    ("Sale",          "assoluto",     None, 5.0),
]

# Dati del profilo forniti. Sesso, attivita', obiettivo e Kcal_target restano
# NULL finche' non vengono decisi.
PROFILO_DEFAULT = (1, 87.0, 176.0, 23)  # ID, Peso_kg, Altezza_cm, Eta_anni

TABELLE_NUOVE = [
    "PASTI", "RICETTE", "RICETTA_PASTI", "GRUPPI_SOSTITUIBILI", "GRUPPO_MEMBRI",
    "RICETTA_INGREDIENTI", "COTTURE", "PIANO", "PROFILO", "OBIETTIVI",
    "LISTA_SPESA_PIANO",
]


def _aggiungi_colonna(conn: sqlite3.Connection, tabella: str, colonna: str, ddl: str) -> bool:
    presenti = [r[1] for r in conn.execute(f"PRAGMA table_info({tabella})")]
    if colonna in presenti:
        return False
    conn.execute(f"ALTER TABLE {tabella} ADD COLUMN {colonna} {ddl}")
    return True


def migra(conn: sqlite3.Connection) -> None:
    create_schema(conn)  # garantisce le 4 tabelle di base
    cur = conn.cursor()

    # --- Nuovi campi di conversione in NUTRIENTI ---------------------------
    _aggiungi_colonna(conn, "NUTRIENTI", "Grammi_per_pezzo",
                      "REAL CHECK (Grammi_per_pezzo > 0)")
    _aggiungi_colonna(conn, "NUTRIENTI", "Densita_kg_per_L",
                      "REAL CHECK (Densita_kg_per_L > 0)")

    # --- Pasti (configurabili) ---------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS PASTI (
            Nome               TEXT    PRIMARY KEY,
            Ordine             INTEGER NOT NULL UNIQUE,
            Quota_energia_pct  REAL    NOT NULL
                               CHECK (Quota_energia_pct > 0 AND Quota_energia_pct <= 100)
        );
    """)

    # --- Ricette -----------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS RICETTE (
            ID_ricetta  INTEGER PRIMARY KEY AUTOINCREMENT,
            Nome        TEXT    NOT NULL UNIQUE COLLATE NOCASE,
            Note        TEXT
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS RICETTA_PASTI (
            ID_ricetta  INTEGER NOT NULL,
            Pasto       TEXT    NOT NULL,
            PRIMARY KEY (ID_ricetta, Pasto),
            FOREIGN KEY (ID_ricetta) REFERENCES RICETTE(ID_ricetta) ON DELETE CASCADE,
            FOREIGN KEY (Pasto)      REFERENCES PASTI(Nome)
        );
    """)

    # --- Gruppi sostituibili (alternative: riso o orzo) --------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS GRUPPI_SOSTITUIBILI (
            ID_gruppo  INTEGER PRIMARY KEY AUTOINCREMENT,
            Nome       TEXT    NOT NULL UNIQUE COLLATE NOCASE
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS GRUPPO_MEMBRI (
            ID_gruppo  INTEGER NOT NULL,
            ID         INTEGER NOT NULL,
            PRIMARY KEY (ID_gruppo, ID),
            FOREIGN KEY (ID_gruppo) REFERENCES GRUPPI_SOSTITUIBILI(ID_gruppo) ON DELETE CASCADE,
            FOREIGN KEY (ID)        REFERENCES NUTRIENTI(ID)
        );
    """)

    # --- Ingredienti (per UNA porzione) ------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS RICETTA_INGREDIENTI (
            ID_riga       INTEGER PRIMARY KEY AUTOINCREMENT,
            ID_ricetta    INTEGER NOT NULL,
            ID            INTEGER,
            ID_gruppo     INTEGER,
            Quantita      REAL    NOT NULL CHECK (Quantita > 0),
            Unita         TEXT    NOT NULL
                          CHECK (Unita IN ('g','kg','ml','L','pz','cucchiaio','cucchiaino')),
            Quanto_basta  INTEGER NOT NULL DEFAULT 0 CHECK (Quanto_basta IN (0, 1)),
            FOREIGN KEY (ID_ricetta) REFERENCES RICETTE(ID_ricetta) ON DELETE CASCADE,
            FOREIGN KEY (ID)         REFERENCES NUTRIENTI(ID),
            FOREIGN KEY (ID_gruppo)  REFERENCES GRUPPI_SOSTITUIBILI(ID_gruppo),
            CHECK ((ID IS NOT NULL) + (ID_gruppo IS NOT NULL) = 1)
        );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_ricing_ricetta ON RICETTA_INGREDIENTI(ID_ricetta);")

    # --- Cotture (comprende le porzioni pronte) ----------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS COTTURE (
            ID_cottura        INTEGER PRIMARY KEY AUTOINCREMENT,
            ID_ricetta        INTEGER NOT NULL,
            Data_Cottura      TEXT    NOT NULL,
            Porzioni_totali   INTEGER NOT NULL CHECK (Porzioni_totali > 0),
            Porzioni_residue  INTEGER NOT NULL
                              CHECK (Porzioni_residue >= 0 AND Porzioni_residue <= Porzioni_totali),
            Stato             TEXT    NOT NULL DEFAULT 'pianificata'
                              CHECK (Stato IN ('pianificata', 'cucinata')),
            FOREIGN KEY (ID_ricetta) REFERENCES RICETTE(ID_ricetta)
        );
    """)

    # --- Piano dei pasti ---------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS PIANO (
            ID_voce     INTEGER PRIMARY KEY AUTOINCREMENT,
            Data        TEXT    NOT NULL,
            Pasto       TEXT    NOT NULL,
            ID_cottura  INTEGER,
            Stato       TEXT    NOT NULL DEFAULT 'proposto'
                        CHECK (Stato IN ('proposto', 'confermato', 'consumato', 'saltato')),
            UNIQUE (Data, Pasto),
            FOREIGN KEY (Pasto)      REFERENCES PASTI(Nome),
            FOREIGN KEY (ID_cottura) REFERENCES COTTURE(ID_cottura),
            CHECK ((Stato = 'saltato') = (ID_cottura IS NULL))
        );
    """)

    # --- Profilo (una sola riga) e obiettivi -------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS PROFILO (
            ID                     INTEGER PRIMARY KEY CHECK (ID = 1),
            Peso_kg                REAL    NOT NULL CHECK (Peso_kg > 0),
            Altezza_cm             REAL    NOT NULL CHECK (Altezza_cm > 0),
            Eta_anni               INTEGER NOT NULL CHECK (Eta_anni > 0),
            Sesso                  TEXT    CHECK (Sesso IN ('M', 'F')),
            Livello_attivita       TEXT    CHECK (Livello_attivita IN ('leggera', 'moderata', 'intensa')),
            Obiettivo              TEXT    CHECK (Obiettivo IN ('mantenimento', 'dimagrimento', 'aumento')),
            Kcal_target            REAL    CHECK (Kcal_target > 0),
            Tolleranza_kcal_pct    REAL    NOT NULL DEFAULT 5 CHECK (Tolleranza_kcal_pct >= 0),
            Extra_aperto_pct       REAL    NOT NULL DEFAULT 5 CHECK (Extra_aperto_pct >= 0),
            Giorni_no_ripetizione  INTEGER NOT NULL DEFAULT 3 CHECK (Giorni_no_ripetizione >= 0)
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS OBIETTIVI (
            Nutriente  TEXT NOT NULL
                       CHECK (Nutriente IN ('Proteine','Carboidrati','Grassi','Zuccheri',
                                            'Grassi_saturi','Fibre','Sale')),
            Tipo       TEXT NOT NULL
                       CHECK (Tipo IN ('assoluto','pct_energia','per_1000kcal','per_kg')),
            Minimo     REAL CHECK (Minimo >= 0),
            Massimo    REAL CHECK (Massimo >= 0),
            PRIMARY KEY (Nutriente, Tipo),
            CHECK (Minimo IS NOT NULL OR Massimo IS NOT NULL),
            CHECK (Minimo IS NULL OR Massimo IS NULL OR Minimo <= Massimo)
        );
    """)

    # --- Voci di spesa derivate dal piano ----------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS LISTA_SPESA_PIANO (
            ID                 INTEGER PRIMARY KEY,
            Quantita_Mancante  REAL    NOT NULL CHECK (Quantita_Mancante > 0),
            FOREIGN KEY (ID) REFERENCES NUTRIENTI(ID)
        );
    """)

    # --- Viste -------------------------------------------------------------
    cur.execute("""
        CREATE VIEW IF NOT EXISTS VISTA_LISTA_SPESA AS
        SELECT t.ID                          AS ID,
               (SELECT a.Nome FROM ALIAS a WHERE a.ID = t.ID ORDER BY a.rowid LIMIT 1) AS Nome,
               SUM(t.Q)                      AS Quantita_Mancante,
               n.Unita_di_misura             AS Unita_di_misura
        FROM (SELECT ID, Quantita_Mancante AS Q FROM LISTA_SPESA
              UNION ALL
              SELECT ID, Quantita_Mancante     FROM LISTA_SPESA_PIANO) t
        JOIN NUTRIENTI n ON n.ID = t.ID
        GROUP BY t.ID;
    """)
    cur.execute("""
        CREATE VIEW IF NOT EXISTS VISTA_INGREDIENTI_GRAMMI AS
        SELECT ri.ID_riga, ri.ID_ricetta, ri.ID, ri.Quanto_basta,
               CASE ri.Unita
                   WHEN 'g'          THEN ri.Quantita
                   WHEN 'kg'         THEN ri.Quantita * 1000.0
                   WHEN 'pz'         THEN ri.Quantita * n.Grammi_per_pezzo
                   WHEN 'ml'         THEN ri.Quantita * n.Densita_kg_per_L
                   WHEN 'L'          THEN ri.Quantita * 1000.0 * n.Densita_kg_per_L
                   WHEN 'cucchiaio'  THEN ri.Quantita * 15.0 * n.Densita_kg_per_L
                   WHEN 'cucchiaino' THEN ri.Quantita * 5.0  * n.Densita_kg_per_L
               END AS Grammi
        FROM RICETTA_INGREDIENTI ri
        JOIN NUTRIENTI n ON n.ID = ri.ID;   -- solo ingredienti con prodotto specifico
    """)
    cur.execute("""
        CREATE VIEW IF NOT EXISTS VISTA_MACRO_RICETTA AS
        SELECT r.ID_ricetta, r.Nome,
               SUM(g.Grammi / 100.0 * n.Kcal_100g)                          AS Kcal,
               SUM(g.Grammi / 100.0 * n.Proteine_100g)                      AS Proteine,
               SUM(g.Grammi / 100.0 * n.Carboidrati_100g)                   AS Carboidrati,
               SUM(g.Grammi / 100.0 * n.Grassi_100g)                        AS Grassi,
               SUM(g.Grammi / 100.0 * COALESCE(n.Zuccheri_100g, 0))         AS Zuccheri,
               SUM(g.Grammi / 100.0 * COALESCE(n.Grassi_saturi_100g, 0))    AS Grassi_saturi,
               SUM(g.Grammi / 100.0 * COALESCE(n.Fibre_100g, 0))            AS Fibre,
               SUM(g.Grammi / 100.0 * COALESCE(n.Sale_100g, 0))             AS Sale,
               MAX(CASE WHEN g.Grammi IS NULL
                          OR n.Zuccheri_100g IS NULL OR n.Grassi_saturi_100g IS NULL
                          OR n.Fibre_100g IS NULL    OR n.Sale_100g IS NULL
                        THEN 1 ELSE 0 END)                                  AS Dati_incompleti
        FROM RICETTE r
        JOIN VISTA_INGREDIENTI_GRAMMI g ON g.ID_ricetta = r.ID_ricetta
        JOIN NUTRIENTI n               ON n.ID = g.ID
        GROUP BY r.ID_ricetta;
    """)

    # --- Dati di configurazione iniziali (non sovrascrivono modifiche) ------
    cur.executemany("INSERT OR IGNORE INTO PASTI (Nome, Ordine, Quota_energia_pct) VALUES (?, ?, ?)",
                    PASTI_DEFAULT)
    cur.executemany(
        "INSERT OR IGNORE INTO OBIETTIVI (Nutriente, Tipo, Minimo, Massimo) VALUES (?, ?, ?, ?)",
        OBIETTIVI_DEFAULT)
    cur.execute("INSERT OR IGNORE INTO PROFILO (ID, Peso_kg, Altezza_cm, Eta_anni) VALUES (?, ?, ?, ?)",
                PROFILO_DEFAULT)

    cur.execute(f"PRAGMA user_version = {VERSIONE_SCHEMA}")
    conn.commit()


def main():
    conn = get_connection()
    try:
        migra(conn)
        presenti = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        mancanti = [t for t in TABELLE_NUOVE if t not in presenti]
        if mancanti:
            print("ERRORE: tabelle non create:", ", ".join(mancanti))
            return
        print(f"Schema ricette pronto (versione {VERSIONE_SCHEMA}): {DB_PATH}")
        print("Tabelle nuove:", ", ".join(TABELLE_NUOVE))
        print("Viste: VISTA_INGREDIENTI_GRAMMI, VISTA_MACRO_RICETTA, VISTA_LISTA_SPESA")
        p = conn.execute("SELECT Peso_kg, Altezza_cm, Eta_anni, Sesso, Livello_attivita, "
                         "Obiettivo, Kcal_target FROM PROFILO").fetchone()
        print("Profilo:", p, "(sesso, attivita', obiettivo e kcal_target da definire)")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
