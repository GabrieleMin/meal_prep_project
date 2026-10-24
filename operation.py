"""
operations.py

Implementa le due operazioni fondamentali sul database inventario:
    - aggiungi_item()
    - consuma_quantita()

Ogni operazione è eseguita in un'unica transazione (commit solo se tutti gli
step vanno a buon fine, rollback automatico in caso di errore), per garantire
che il database non resti mai in uno stato intermedio incoerente.
"""

import sqlite3
import math
from crea_RELSCHEMA import get_connection, DB_PATH


# ----------------------------------------------------------------------
# Eccezioni custom, per distinguere i casi di errore in modo esplicito
# quando queste funzioni verranno chiamate da uno strato superiore (API).
# ----------------------------------------------------------------------

class ConflittoAlias(Exception):
    """Il nome inserito esiste già in ALIAS ma collegato a un ID diverso."""
    pass


class IDNonValido(Exception):
    """L'ID fornito non è nel formato atteso (intero)."""
    pass


class ProdottoNonTrovato(Exception):
    """Nome non presente in ALIAS e nessun ID di fallback fornito, oppure
    ID di fallback che non risolve né a un prodotto esistente né a dati
    sufficienti per crearne uno nuovo."""
    pass


class InputNonValido(Exception):
    """Valori di input che violano le regole di business (es. quantità <= 0,
    quantità frazionaria per prodotti non divisibili)."""
    pass


# ----------------------------------------------------------------------
# Utility interne
# ----------------------------------------------------------------------

def _arrotonda_difetto_centesimo(valore: float) -> float:
    """Arrotonda per difetto al centesimo (0.01), es. 1.1111 -> 1.11."""
    return math.floor(round(valore * 100, 6)) / 100


def _risolvi_id_da_nome(cur: sqlite3.Cursor, nome: str):
    """Cerca il nome in ALIAS. Ritorna l'ID se trovato, altrimenti None."""
    cur.execute("SELECT ID FROM ALIAS WHERE Nome = ?", (nome,))
    row = cur.fetchone()
    return row[0] if row else None


def _id_esiste_in_nutrienti(cur: sqlite3.Cursor, id_prodotto: int) -> bool:
    cur.execute("SELECT 1 FROM NUTRIENTI WHERE ID = ?", (id_prodotto,))
    return cur.fetchone() is not None


def _primo_nome_per_id(cur: sqlite3.Cursor, id_prodotto: int) -> str:
    """Ritorna il primo alias storicamente inserito per un dato ID
    (usato come Nome_Riferimento in LISTA_SPESA). 'Primo' = rowid più basso,
    cioè il primo inserito in ordine cronologico."""
    cur.execute(
        "SELECT Nome FROM ALIAS WHERE ID = ? ORDER BY rowid ASC LIMIT 1",
        (id_prodotto,),
    )
    row = cur.fetchone()
    return row[0] if row else None


# ----------------------------------------------------------------------
# OPERAZIONE 1: AGGIUNGERE ITEM
# ----------------------------------------------------------------------

def aggiungi_item(
    nome: str,
    peso_unitario: float,
    numero_item_integri: int,
    luogo: str,
    id_fallback: int = None,
    unita_misura_nuovo_prodotto: str = None,
    macro_nuovo_prodotto: dict = None,
    db_path: str = DB_PATH,
) -> dict:
    """
    Aggiunge una nuova istanza di un prodotto all'inventario.

    Parametri
    ---------
    nome : nome scritto dall'utente (es. "Penne Rigate")
    peso_unitario : peso/volume di una singola confezione (> 0)
    numero_item_integri : quante confezioni intere si stanno aggiungendo (> 0)
    luogo : luogo fisico di conservazione
    id_fallback : richiesto SOLO se 'nome' non è già presente in ALIAS
                  (Caso B). Se l'ID esiste già in NUTRIENTI -> Caso B1
                  (nuovo alias per prodotto esistente). Se non esiste ->
                  Caso B2 (nuovo prodotto, richiede unita_misura_nuovo_prodotto
                  e macro_nuovo_prodotto).
    unita_misura_nuovo_prodotto : 'kg' | 'L' | 'pz', richiesto solo in Caso B2
    macro_nuovo_prodotto : dict con chiavi Kcal_100g, Proteine_100g,
                            Carboidrati_100g, Grassi_100g (obbligatorie) ed
                            eventualmente Zuccheri_100g, Grassi_saturi_100g,
                            Fibre_100g, Sale_100g. Richiesto solo in Caso B2.

    Ritorna un dizionario con l'esito dell'operazione.
    """
    if peso_unitario is None or peso_unitario <= 0:
        raise InputNonValido("Peso_Unitario deve essere maggiore di 0.")
    if numero_item_integri is None or numero_item_integri <= 0:
        raise InputNonValido(
            "Numero_Item_Integri deve essere maggiore di 0 "
            "(un'aggiunta di 0 item non ha senso)."
        )
    if not luogo:
        raise InputNonValido("Luogo è obbligatorio.")

    conn = get_connection(db_path)
    try:
        cur = conn.cursor()

        # ---- Step 1: risoluzione nome ----
        id_prodotto = _risolvi_id_da_nome(cur, nome)
        caso = None

        if id_prodotto is not None:
            # Caso A: nome trovato
            caso = "A"
        else:
            # Caso B: nome non trovato, serve id_fallback
            if id_fallback is None:
                raise ProdottoNonTrovato(
                    f"Nome '{nome}' non presente in ALIAS. "
                    f"Fornire id_fallback per procedere (Caso B)."
                )
            if not isinstance(id_fallback, int):
                raise IDNonValido("id_fallback deve essere un intero.")

            if _id_esiste_in_nutrienti(cur, id_fallback):
                # Caso B1: ID esiste già -> nuovo alias per prodotto esistente
                caso = "B1"
                id_prodotto = id_fallback
                cur.execute(
                    "INSERT INTO ALIAS (Nome, ID) VALUES (?, ?)",
                    (nome, id_prodotto),
                )
            else:
                # Caso B2: ID non esiste -> nuovo prodotto
                caso = "B2"
                id_prodotto = id_fallback

                if unita_misura_nuovo_prodotto not in ("kg", "L", "pz"):
                    raise InputNonValido(
                        "unita_misura_nuovo_prodotto deve essere 'kg', 'L' o 'pz'."
                    )
                if not macro_nuovo_prodotto:
                    raise InputNonValido(
                        "macro_nuovo_prodotto è obbligatorio per un nuovo prodotto (Caso B2)."
                    )
                obbligatori = ["Kcal_100g", "Proteine_100g", "Carboidrati_100g", "Grassi_100g"]
                mancanti = [k for k in obbligatori if k not in macro_nuovo_prodotto]
                if mancanti:
                    raise InputNonValido(
                        f"macro_nuovo_prodotto: campi obbligatori mancanti: {mancanti}"
                    )

                cur.execute(
                    """
                    INSERT INTO NUTRIENTI
                        (ID, Unita_di_misura, Kcal_100g, Proteine_100g,
                         Carboidrati_100g, Zuccheri_100g, Grassi_100g,
                         Grassi_saturi_100g, Fibre_100g, Sale_100g)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        id_prodotto,
                        unita_misura_nuovo_prodotto,
                        macro_nuovo_prodotto["Kcal_100g"],
                        macro_nuovo_prodotto["Proteine_100g"],
                        macro_nuovo_prodotto["Carboidrati_100g"],
                        macro_nuovo_prodotto.get("Zuccheri_100g"),
                        macro_nuovo_prodotto["Grassi_100g"],
                        macro_nuovo_prodotto.get("Grassi_saturi_100g"),
                        macro_nuovo_prodotto.get("Fibre_100g"),
                        macro_nuovo_prodotto.get("Sale_100g"),
                    ),
                )
                cur.execute(
                    "INSERT INTO ALIAS (Nome, ID) VALUES (?, ?)",
                    (nome, id_prodotto),
                )

        # ---- Step 2: scrittura inventario (sempre nuova riga, mai merge) ----
        cur.execute(
            """
            INSERT INTO INVENTARIO
                (ID, Peso_Unitario, Numero_Item_Integri, Peso_Residuo_Aperto, Luogo)
            VALUES (?, ?, ?, 0, ?)
            """,
            (id_prodotto, peso_unitario, numero_item_integri, luogo),
        )
        id_riga_creata = cur.lastrowid

        conn.commit()
        return {
            "esito": "ok",
            "caso": caso,
            "ID": id_prodotto,
            "ID_riga_inventario": id_riga_creata,
        }

    except sqlite3.IntegrityError as e:
        conn.rollback()
        # Tipicamente scatta se si tenta di inserire in ALIAS un Nome
        # già esistente (vincolo PRIMARY KEY su Nome) collegato a un ID
        # diverso: il conflitto descritto nella specifica.
        raise ConflittoAlias(
            f"Conflitto di integrità durante l'inserimento: {e}"
        )
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ----------------------------------------------------------------------
# OPERAZIONE 2: CONSUMARE QUANTITA
# ----------------------------------------------------------------------

def consuma_quantita(nome: str, quantita_da_consumare: float, db_path: str = DB_PATH) -> dict:
    """
    Consuma una quantità di un prodotto dall'inventario, seguendo la
    priorità: prima unità già aperte (Peso_Residuo_Aperto), poi apertura
    di nuove unità integre in ordine FIFO (ID_riga crescente).

    Se la disponibilità totale è insufficiente, consuma tutto il
    disponibile e registra (sommandola se già presente) la quantità
    mancante in LISTA_SPESA. L'operazione non si blocca mai in questo caso.

    Per prodotti con Unita_di_misura = 'pz', la quantità deve essere un
    intero: si decrementa direttamente Numero_Item_Integri, senza mai
    generare o usare Peso_Residuo_Aperto.

    Ritorna un dizionario con il dettaglio dell'esito.
    """
    if quantita_da_consumare is None or quantita_da_consumare <= 0:
        raise InputNonValido("Quantità da consumare deve essere maggiore di 0.")

    conn = get_connection(db_path)
    try:
        cur = conn.cursor()

        # ---- Step 1: risoluzione nome -> ID ----
        id_prodotto = _risolvi_id_da_nome(cur, nome)
        if id_prodotto is None:
            raise ProdottoNonTrovato(f"Nome '{nome}' non presente in ALIAS.")

        cur.execute("SELECT Unita_di_misura FROM NUTRIENTI WHERE ID = ?", (id_prodotto,))
        row = cur.fetchone()
        if row is None:
            # Non dovrebbe accadere se i vincoli FK sono rispettati, ma
            # lo trattiamo comunque per robustezza.
            raise ProdottoNonTrovato(f"Nessuna scheda NUTRIENTI per ID {id_prodotto}.")
        unita_misura = row[0]

        # Vincolo prodotti non divisibili: quantità intera obbligatoria
        if unita_misura == "pz":
            if quantita_da_consumare != int(quantita_da_consumare):
                raise InputNonValido(
                    "Prodotto non divisibile (unità 'pz'): la quantità richiesta "
                    "deve essere un numero intero di item."
                )
            quantita_da_consumare = int(quantita_da_consumare)

        # ---- Step 2: recupero righe inventario e calcolo disponibilità ----
        cur.execute(
            """
            SELECT ID_riga, Peso_Unitario, Numero_Item_Integri, Peso_Residuo_Aperto
            FROM INVENTARIO
            WHERE ID = ?
            ORDER BY ID_riga ASC
            """,
            (id_prodotto,),
        )
        righe = cur.fetchall()  # lista di tuple (ID_riga, Peso_Unitario, Numero_Item_Integri, Peso_Residuo_Aperto)

        disponibilita_totale = sum(
            r[2] if unita_misura == "pz" else (r[2] * r[1]) + r[3] for r in righe
        )

        quantita_mancante = 0.0
        quantita_da_coprire = quantita_da_consumare

        if disponibilita_totale < quantita_da_consumare:
            # Disponibilità insufficiente: si consuma tutto, si registra il resto mancante
            quantita_mancante = _arrotonda_difetto_centesimo(
                quantita_da_consumare - disponibilita_totale
            ) if unita_misura != "pz" else (quantita_da_consumare - disponibilita_totale)
            quantita_da_coprire = disponibilita_totale  # consumiamo al massimo il disponibile

        # ---- Step 3/4: consumo effettivo, riga per riga, con priorità ----
        residuo_da_consumare = quantita_da_coprire

        if unita_misura == "pz":
            # Nessun residuo aperto per i pezzi: si decrementa direttamente
            # Numero_Item_Integri, FIFO tra le righe.
            for id_riga, peso_unitario, numero_item, _peso_res in righe:
                if residuo_da_consumare <= 0:
                    break
                prelievo = min(numero_item, residuo_da_consumare)
                if prelievo > 0:
                    nuovo_numero = numero_item - prelievo
                    cur.execute(
                        "UPDATE INVENTARIO SET Numero_Item_Integri = ? WHERE ID_riga = ?",
                        (nuovo_numero, id_riga),
                    )
                    residuo_da_consumare -= prelievo
        else:
            # Fase 1: consuma prima dai residui già aperti
            for id_riga, peso_unitario, numero_item, peso_residuo in righe:
                if residuo_da_consumare <= 0:
                    break
                if peso_residuo > 0:
                    prelievo = min(peso_residuo, residuo_da_consumare)
                    nuovo_residuo = _arrotonda_difetto_centesimo(peso_residuo - prelievo)
                    cur.execute(
                        "UPDATE INVENTARIO SET Peso_Residuo_Aperto = ? WHERE ID_riga = ?",
                        (nuovo_residuo, id_riga),
                    )
                    residuo_da_consumare -= prelievo
                    # aggiorniamo il valore locale per la fase 2, se serve rileggere
                    righe = [
                        (r[0], r[1], r[2], nuovo_residuo) if r[0] == id_riga else r
                        for r in righe
                    ]

            # Fase 2: apri nuove unità integre, FIFO, finché serve
            if residuo_da_consumare > 0:
                for idx, (id_riga, peso_unitario, numero_item, peso_residuo) in enumerate(righe):
                    while residuo_da_consumare > 0 and numero_item > 0:
                        numero_item -= 1
                        if residuo_da_consumare >= peso_unitario:
                            # l'intera unità aperta viene consumata subito
                            residuo_da_consumare -= peso_unitario
                            nuovo_residuo_riga = 0.0
                        else:
                            nuovo_residuo_riga = _arrotonda_difetto_centesimo(
                                peso_unitario - residuo_da_consumare
                            )
                            residuo_da_consumare = 0.0

                        cur.execute(
                            "UPDATE INVENTARIO SET Numero_Item_Integri = ?, Peso_Residuo_Aperto = ? WHERE ID_riga = ?",
                            (numero_item, nuovo_residuo_riga, id_riga),
                        )
                    if residuo_da_consumare <= 0:
                        break

        # ---- Step 6: pulizia righe azzerate ----
        cur.execute(
            """
            DELETE FROM INVENTARIO
            WHERE ID = ? AND Numero_Item_Integri = 0 AND Peso_Residuo_Aperto = 0
            """,
            (id_prodotto,),
        )

        # ---- Upsert LISTA_SPESA se quantità mancante ----
        if quantita_mancante > 0:
            cur.execute("SELECT Quantita_Mancante FROM LISTA_SPESA WHERE ID = ?", (id_prodotto,))
            esistente = cur.fetchone()
            if esistente is not None:
                nuovo_totale = esistente[0] + quantita_mancante
                if unita_misura != "pz":
                    nuovo_totale = _arrotonda_difetto_centesimo(nuovo_totale)
                cur.execute(
                    "UPDATE LISTA_SPESA SET Quantita_Mancante = ? WHERE ID = ?",
                    (nuovo_totale, id_prodotto),
                )
            else:
                nome_riferimento = _primo_nome_per_id(cur, id_prodotto)
                cur.execute(
                    "INSERT INTO LISTA_SPESA (ID, Nome_Riferimento, Quantita_Mancante) VALUES (?, ?, ?)",
                    (id_prodotto, nome_riferimento, quantita_mancante),
                )

        conn.commit()

        return {
            "esito": "ok",
            "ID": id_prodotto,
            "richiesto": quantita_da_consumare,
            "consumato": quantita_da_coprire,
            "mancante": quantita_mancante,
            "disponibilita_insufficiente": quantita_mancante > 0,
        }

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ----------------------------------------------------------------------
# LETTURA 1: LISTA DELLA SPESA
# ----------------------------------------------------------------------

def ottieni_lista_spesa(db_path: str = DB_PATH) -> list:
    """
    Ritorna il contenuto corrente di LISTA_SPESA: tutti i prodotti per cui
    almeno una richiesta di consumo non è stata soddisfatta interamente,
    con la quantità mancante accumulata nel tempo.

    Ritorna una lista di dizionari:
        [{"ID": ..., "Nome": ..., "Quantita_Mancante": ...}, ...]
    """
    conn = get_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT ID, Nome_Riferimento, Quantita_Mancante FROM LISTA_SPESA ORDER BY ID"
        )
        righe = cur.fetchall()
        return [
            {"ID": id_prodotto, "Nome": nome_riferimento, "Quantita_Mancante": quantita}
            for id_prodotto, nome_riferimento, quantita in righe
        ]
    finally:
        conn.close()


# ----------------------------------------------------------------------
# LETTURA 2: VISTA DISPONIBILITA' (calcolata al volo ad ogni chiamata,
# non persistita: riflette sempre lo stato più aggiornato di INVENTARIO)
# ----------------------------------------------------------------------

def ottieni_inventario_disponibile(db_path: str = DB_PATH) -> list:
    """
    Aggrega INVENTARIO per (ID, Luogo) e calcola, per ciascuna combinazione,
    la disponibilità totale con la stessa formula già usata in
    consuma_quantita():
        (Numero_Item_Integri * Peso_Unitario) + Peso_Residuo_Aperto
    (per i prodotti 'pz': solo Numero_Item_Integri) sommata su tutte le righe che condividono lo stesso prodotto e lo
    stesso luogo.

    Ritorna una lista di dizionari, una riga per ogni coppia
    (prodotto, luogo) effettivamente presente in INVENTARIO:
        [{
            "ID": ...,
            "Nome": ...,                       # primo alias storico per l'ID
            "Peso_Totale_Disponibile": ...,
            "Unita_di_misura": ...,             # 'kg' | 'L' | 'pz'
            "Luogo": ...
        }, ...]
    """
    conn = get_connection(db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                INVENTARIO.ID,
                INVENTARIO.Luogo,
                SUM(CASE WHEN NUTRIENTI.Unita_di_misura = 'pz'
                         THEN INVENTARIO.Numero_Item_Integri
                         ELSE INVENTARIO.Numero_Item_Integri * INVENTARIO.Peso_Unitario
                              + INVENTARIO.Peso_Residuo_Aperto END) AS Totale,
                NUTRIENTI.Unita_di_misura
            FROM INVENTARIO
            JOIN NUTRIENTI ON NUTRIENTI.ID = INVENTARIO.ID
            GROUP BY INVENTARIO.ID, INVENTARIO.Luogo
            ORDER BY INVENTARIO.ID, INVENTARIO.Luogo
            """
        )
        righe = cur.fetchall()

        risultato = []
        for id_prodotto, luogo, totale, unita_misura in righe:
            nome = _primo_nome_per_id(cur, id_prodotto)
            valore_totale = (
                totale if unita_misura == "pz" else _arrotonda_difetto_centesimo(totale)
            )
            risultato.append(
                {
                    "ID": id_prodotto,
                    "Nome": nome,
                    "Peso_Totale_Disponibile": valore_totale,
                    "Unita_di_misura": unita_misura,
                    "Luogo": luogo,
                }
            )
        return risultato
    finally:
        conn.close()