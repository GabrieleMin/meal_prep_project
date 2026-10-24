"""
create_RELSCHEMA.py

Crea il database relazionale SQLite per la gestione dell'inventario alimentare.

Schema:
    ALIAS       (Nome PK, ID)                         -- N nomi -> 1 ID prodotto
    NUTRIENTI   (ID PK, Unita_di_misura, macro...)     -- 1 record per prodotto
    INVENTARIO  (ID_riga PK, ID FK, Peso_Unitario,
                 Numero_Item_Integri, Peso_Residuo_Aperto, Luogo)
    LISTA_SPESA (ID PK/FK, Nome_Riferimento, Quantita_Mancante)

Uso:
    python3 create_schema.py
    (ricrea il file inventario.db nella stessa cartella; se esiste già,
     NON lo sovrascrive automaticamente - vedi funzione main)
"""

import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "inventario.db")


def get_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    """
    Apre una connessione al database con i controlli di integrità referenziale
    attivi. SQLite li ha disattivati di default per compatibilità storica,
    vanno riattivati esplicitamente ad ogni connessione.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()

    # ------------------------------------------------------------------
    # NUTRIENTI: creata per prima perché ALIAS e LISTA_SPESA referenziano
    # il suo ID tramite foreign key (l'ordine di creazione delle tabelle
    # conta per i vincoli FK).
    # ------------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS NUTRIENTI (
            ID                  INTEGER PRIMARY KEY,
            Unita_di_misura     TEXT    NOT NULL CHECK (Unita_di_misura IN ('kg', 'L', 'pz')),
            Kcal_100g           REAL    NOT NULL CHECK (Kcal_100g >= 0),
            Proteine_100g       REAL    NOT NULL CHECK (Proteine_100g >= 0),
            Carboidrati_100g    REAL    NOT NULL CHECK (Carboidrati_100g >= 0),
            Zuccheri_100g       REAL    CHECK (Zuccheri_100g >= 0),
            Grassi_100g         REAL    NOT NULL CHECK (Grassi_100g >= 0),
            Grassi_saturi_100g  REAL    CHECK (Grassi_saturi_100g >= 0),
            Fibre_100g          REAL    CHECK (Fibre_100g >= 0),
            Sale_100g           REAL    CHECK (Sale_100g >= 0)
        );
    """)

    # ------------------------------------------------------------------
    # ALIAS: Nome è la vera chiave di ricerca (univoca). ID è un
    # attributo di raggruppamento, ripetibile su più righe, e referenzia
    # NUTRIENTI.ID.
    # ------------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS ALIAS (
            Nome    TEXT    PRIMARY KEY,
            ID      INTEGER NOT NULL,
            FOREIGN KEY (ID) REFERENCES NUTRIENTI(ID)
        );
    """)
    # Indice esplicito su ID per velocizzare "trova tutti gli alias di un ID"
    cur.execute("CREATE INDEX IF NOT EXISTS idx_alias_id ON ALIAS(ID);")

    # ------------------------------------------------------------------
    # INVENTARIO: ogni riga è un'istanza fisica separata. ID_riga è la
    # chiave primaria autoincrementale della singola istanza; ID è la
    # foreign key verso il prodotto.
    # ------------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS INVENTARIO (
            ID_riga                INTEGER PRIMARY KEY AUTOINCREMENT,
            ID                     INTEGER NOT NULL,
            Peso_Unitario          REAL    NOT NULL CHECK (Peso_Unitario > 0),
            Numero_Item_Integri    INTEGER NOT NULL CHECK (Numero_Item_Integri >= 0),
            Peso_Residuo_Aperto    REAL    NOT NULL DEFAULT 0 CHECK (Peso_Residuo_Aperto >= 0),
            Luogo                  TEXT    NOT NULL,
            FOREIGN KEY (ID) REFERENCES NUTRIENTI(ID)
        );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_inventario_id ON INVENTARIO(ID);")

    # ------------------------------------------------------------------
    # LISTA_SPESA: al massimo una riga per prodotto (1:0/1 con NUTRIENTI).
    # ------------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS LISTA_SPESA (
            ID                  INTEGER PRIMARY KEY,
            Nome_Riferimento    TEXT    NOT NULL,
            Quantita_Mancante   REAL    NOT NULL CHECK (Quantita_Mancante > 0),
            FOREIGN KEY (ID) REFERENCES NUTRIENTI(ID)
        );
    """)

    conn.commit()


def schema_exists(conn: sqlite3.Connection) -> bool:
    cur = conn.cursor()
    cur.execute("""
        SELECT name FROM sqlite_master
        WHERE type = 'table' AND name IN ('ALIAS', 'NUTRIENTI', 'INVENTARIO', 'LISTA_SPESA');
    """)
    return len(cur.fetchall()) == 4


def main():
    first_time = not os.path.exists(DB_PATH)
    conn = get_connection()
    try:
        create_schema(conn)
        if schema_exists(conn):
            status = "creato" if first_time else "verificato (già esistente)"
            print(f"Database {status} correttamente: {DB_PATH}")
            cur = conn.cursor()
            cur.execute("SELECT name FROM sqlite_master WHERE type = 'table';")
            tabelle = [row[0] for row in cur.fetchall()]
            print("Tabelle presenti:", ", ".join(tabelle))
        else:
            print("ERRORE: lo schema non risulta creato correttamente.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
