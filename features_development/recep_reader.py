import os
import re
import warnings
from concurrent.futures import ThreadPoolExecutor
from typing import List, Tuple
import cv2
import numpy as np
import torch
import easyocr

# =====================================================================
# MODULI C/C++ E COMPILAZIONE JIT (CON FALLBACK AUTOMATICO)
# =====================================================================
try:
    # Libreria scritta in C++ per string matching ultra-veloce
    from rapidfuzz.fuzz import ratio as c_fuzzy_ratio

    USA_RAPIDFUZZ = True
except ImportError:
    from difflib import SequenceMatcher

    USA_RAPIDFUZZ = False

try:
    # Compilatore JIT LLVM per tradurre funzioni Python/NumPy in codice macchina C
    from numba import njit

    USA_NUMBA = True
except ImportError:
    USA_NUMBA = False


    def njit(*args, **kwargs):
        def decorator(func):
            return func

        return decorator

warnings.filterwarnings("ignore", category=UserWarning, module="torch")
warnings.filterwarnings("ignore", category=RuntimeWarning, module="easyocr")
np.seterr(over='ignore', invalid='ignore')

# =====================================================================
# OTTIMIZZAZIONE HARDWARE, MULTI-THREADING E GPU
# =====================================================================
NUM_CORES = os.cpu_count() or 4
cv2.setNumThreads(NUM_CORES)
torch.set_num_threads(NUM_CORES)


def configura_dispositivo_gpu() -> bool:
    if torch.cuda.is_available():
        torch.backends.cudnn.benchmark = True
        torch.backends.cuda.matmul.allow_tf32 = True
        return True
    elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        return True
    return False


USA_GPU = configura_dispositivo_gpu()

# Pool di thread persistente per operazioni I/O asincrone e pre-processing concorrente
IO_EXECUTOR = ThreadPoolExecutor(max_workers=NUM_CORES)

# =====================================================================
# CONFIGURAZIONE TAGLI E PARAMETRI
# =====================================================================
TAGLIO_ALTO_RATIO = 0.15  # Taglia l'intestazione superiore
TAGLIO_DESTRA_RATIO = 0.615  # Taglia i prezzi a destra
FATTORE_INGRANDIMENTO = 2.0
TOLLERANZA_Y_RIGA = 18

FILE_DEBUG_FINALE = "debug_solo_spesa.jpg"

PAROLE_FINE_SPESA = ("SUBTOTALE", "TOTALE COMPLESSIVO", "COMPLESSIVO")
SOGLIA_SIMILARITA_OCR = 75.0 if USA_RAPIDFUZZ else 0.75

# Regex pre-compilate in C tramite il motore 're' di Python
PATTERN_NON_ALFA = re.compile(r'[^A-Z\s]')
PATTERN_PULIZIA_IVA = re.compile(r'\s+\b(?:4|1[0O]|22)\s*(?:%|0/0|o/o|96|8)?\s*$', re.IGNORECASE)
PATTERN_PREZZI_FINALI = re.compile(r'\s+[\d]+[.,][\d]{2}\s*€?\s*$')
PATTERN_DETTAGLIO_CAD = re.compile(r'(?:\bCad\b|\bP[z2]\.?\s*\d+)', re.IGNORECASE)
PATTERN_DETTAGLIO_PESO = re.compile(r'([\dO]+[.,][\dO]+)\s*(kg|g|gr|l|ml|cl)\b', re.IGNORECASE)
PATTERN_PESO_INLINE = re.compile(r'\b(\d+[.,]?\d*)\s*(kg|g|gr|l|ml|cl)\b', re.IGNORECASE)
PATTERN_NUMERI_INTERI = re.compile(r'\b(\d+)\b')
PATTERN_BORDI_SPORCHI = re.compile(r'^[-.,:;€*\s]+|[-.,:;€*\s]+$')
# =====================================================================

print(f"Caricamento OCR (GPU={USA_GPU}, CPU Threads={NUM_CORES}, C++ Fuzz={USA_RAPIDFUZZ}, Numba JIT={USA_NUMBA})...")
OCR_READER = easyocr.Reader(['it'], gpu=USA_GPU, verbose=False)

# Pre-allocazione dell'oggetto C++ CLAHE (evita di ricrearlo ad ogni immagine)
CLAHE_ENGINE = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))

ItemSpesa = Tuple[str, float, str, int]


# =====================================================================
# FUNZIONE COMPILATA IN C TRAMITE NUMBA JIT
# =====================================================================
@njit(fastmath=True, cache=True)
def _raggruppa_righe_numba(coords: np.ndarray, tolleranza_y: int) -> Tuple[np.ndarray, np.ndarray]:
    """
    Esegue in codice macchina C l'ordinamento e il clustering spaziale 2D delle parole.
    coords: array (N, 3) contenente [y_centro, x_sinistra, y_top]
    Restituisce gli indici ordinati e l'ID di riga per ogni blocco.
    """
    n = coords.shape[0]
    indici_y = np.argsort(coords[:, 0])
    id_righe = np.zeros(n, dtype=np.int32)

    riga_attuale = 0
    y_prec = coords[indici_y[0], 0]

    for i in range(1, n):
        idx = indici_y[i]
        y_curr = coords[idx, 0]
        if abs(y_curr - y_prec) <= tolleranza_y:
            id_righe[i] = riga_attuale
        else:
            riga_attuale += 1
            id_righe[i] = riga_attuale
        y_prec = y_curr

    return indici_y, id_righe


def migliora_immagine_per_ocr(img_gray: np.ndarray) -> np.ndarray:
    """Esegue la pipeline OpenCV sfruttando buffer C++ contigui."""
    img_zoom = cv2.resize(
        img_gray, None,
        fx=FATTORE_INGRANDIMENTO, fy=FATTORE_INGRANDIMENTO,
        interpolation=cv2.INTER_CUBIC
    )
    img_contrasto = CLAHE_ENGINE.apply(img_zoom)
    return cv2.GaussianBlur(img_contrasto, (3, 3), 0)


def is_fine_spesa(testo_riga: str) -> bool:
    """Usa RapidFuzz in C++ (se presente) per il confronto stringhe ad alta velocità."""
    testo_pulito = PATTERN_NON_ALFA.sub('', testo_riga.upper()).strip()
    if not testo_pulito:
        return False

    parole = testo_pulito.split()
    for target in PAROLE_FINE_SPESA:
        if target in testo_pulito:
            return True
        if USA_RAPIDFUZZ:
            if c_fuzzy_ratio(testo_pulito, target) >= SOGLIA_SIMILARITA_OCR:
                return True
            for p in parole:
                if len(p) >= 5 and c_fuzzy_ratio(p, target) >= SOGLIA_SIMILARITA_OCR:
                    return True
        else:
            if SequenceMatcher(None, testo_pulito, target).ratio() >= SOGLIA_SIMILARITA_OCR:
                return True
            for p in parole:
                if len(p) >= 5 and SequenceMatcher(None, p, target).ratio() >= SOGLIA_SIMILARITA_OCR:
                    return True
    return False


def ricostruisci_righe_orizzontali(risultati_ocr: list) -> List[Tuple[int, str]]:
    if not risultati_ocr:
        return []

    n = len(risultati_ocr)
    coords = np.empty((n, 3), dtype=np.int32)
    testi = []

    for i, (bbox, testo, _) in enumerate(risultati_ocr):
        y_top = int(bbox[0][1])
        y_bottom = int(bbox[2][1])
        coords[i, 0] = (y_top + y_bottom) >> 1  # Bit-shift veloce per divisione intera per 2
        coords[i, 1] = int(bbox[0][0])
        coords[i, 2] = y_top
        testi.append(testo.strip())

    # Clustering spaziale in C tramite Numba
    indici_y, id_righe = _raggruppa_righe_numba(coords, TOLLERANZA_Y_RIGA)

    righe_finali = []
    num_gruppi = id_righe[-1] + 1

    for g in range(num_gruppi):
        mask = (id_righe == g)
        idx_gruppo = indici_y[mask]

        # Ordina per coordinata X (colonna 1)
        ordine_x = np.argsort(coords[idx_gruppo, 1])
        idx_ordinati = idx_gruppo[ordine_x]

        y_riferimento = int(np.min(coords[idx_ordinati, 2]))
        testo_completo = " ".join(testi[i] for i in idx_ordinati if testi[i])
        righe_finali.append((y_riferimento, testo_completo))

    return righe_finali


def _carica_e_prepara_immagine(percorso_immagine: str) -> Tuple[np.ndarray, bool]:
    """Funzione isolata thread-safe per caricamento e pre-processing parallelo."""
    img = cv2.imread(percorso_immagine, cv2.IMREAD_GRAYSCALE)
    if img is None:
        return np.empty(0), False

    is_debug = "debug" in percorso_immagine.lower()
    if not is_debug:
        h, w = img.shape[:2]
        img = img[int(h * TAGLIO_ALTO_RATIO):, :int(w * TAGLIO_DESTRA_RATIO)]

    return migliora_immagine_per_ocr(img), is_debug


def _parsa_righe_testo(righe_spesa_grezze: List[str]) -> List[ItemSpesa]:
    """Estrae le tuple applicando le espressioni regolari pre-compilate."""
    lista_items: List[ItemSpesa] = []

    for riga in righe_spesa_grezze:
        riga_pulita = PATTERN_PULIZIA_IVA.sub('', riga).strip()
        riga_pulita = PATTERN_PREZZI_FINALI.sub('', riga_pulita).strip()
        if not riga_pulita:
            continue

        match_peso_dettaglio = PATTERN_DETTAGLIO_PESO.search(riga_pulita)
        is_riga_cad = PATTERN_DETTAGLIO_CAD.search(riga_pulita) is not None
        riga_lower = riga_pulita.lower()

        if lista_items and (is_riga_cad or (
                match_peso_dettaglio and ("x" in riga_lower or "eur" in riga_lower or riga_pulita[0].isdigit()))):
            nome_prec, qta_prec, unita_prec, conf_prec = lista_items[-1]

            if is_riga_cad:
                numeri = PATTERN_NUMERI_INTERI.findall(riga_pulita)
                if numeri:
                    conf_prec = int(numeri[-1])
            elif match_peso_dettaglio:
                val_str = match_peso_dettaglio.group(1).upper().replace('O', '0').replace(',', '.')
                try:
                    qta_prec = float(val_str)
                    unita_prec = match_peso_dettaglio.group(2).lower()
                except ValueError:
                    pass

            lista_items[-1] = (nome_prec, qta_prec, unita_prec, conf_prec)
            continue

        qta_default = 1.0
        unita_default = "pz"

        match_inline = PATTERN_PESO_INLINE.search(riga_pulita)
        if match_inline:
            val_str = match_inline.group(1).replace(',', '.')
            try:
                qta_default = float(val_str)
                unita_default = match_inline.group(2).lower()
                riga_pulita = (riga_pulita[:match_inline.start()] + riga_pulita[match_inline.end():]).strip()
            except ValueError:
                pass

        nome_finale = PATTERN_BORDI_SPORCHI.sub('', riga_pulita)
        if len(nome_finale) >= 2:
            lista_items.append((nome_finale, qta_default, unita_default, 1))

    return lista_items


def estrai_tuple_da_immagine(percorso_immagine: str) -> List[ItemSpesa]:
    img_ottimizzata, is_debug = _carica_e_prepara_immagine(percorso_immagine)
    if img_ottimizzata.size == 0:
        print(f"Errore: Impossibile caricare l'immagine {percorso_immagine}")
        return []

    # Disabilita il calcolo dei gradienti PyTorch e usa batching interno per velocizzare l'inferenza
    with torch.inference_mode():
        risultati_ocr = OCR_READER.readtext(
            img_ottimizzata,
            batch_size=8,
            workers=0  # Evita overhead di sotto-processi su singola immagine
        )

    righe_complete = ricostruisci_righe_orizzontali(risultati_ocr)

    y_fine_dinamico = img_ottimizzata.shape[0]
    righe_spesa_grezze = []
    for (y_top, testo_riga) in righe_complete:
        if is_fine_spesa(testo_riga):
            y_fine_dinamico = y_top
            break
        righe_spesa_grezze.append(testo_riga)

    # Salvataggio immagine su disco in modo ASINCRONO (non blocca l'esecuzione del programma)
    img_solo_spesa = img_ottimizzata[:y_fine_dinamico, :]
    if img_solo_spesa.size > 0 and not is_debug:
        IO_EXECUTOR.submit(cv2.imwrite, FILE_DEBUG_FINALE, img_solo_spesa.copy())

    return _parsa_righe_testo(righe_spesa_grezze)


def estrai_tuple_batch_concorrente(percorsi_immagini: List[str]) -> List[List[ItemSpesa]]:
    """
    Elabora una lista di scontrini sfruttando il multi-threading per il pre-processing OpenCV
    in parallelo su tutti i core della CPU.
    """
    # 1. Pre-processing OpenCV eseguito in parallelo su tutti i core (OpenCV rilascia il GIL)
    immagini_pronte = list(IO_EXECUTOR.map(_carica_e_prepara_immagine, percorsi_immagini))

    risultati_batch = []
    with torch.inference_mode():
        for img_ottimizzata, _ in immagini_pronte:
            if img_ottimizzata.size == 0:
                risultati_batch.append([])
                continue
            risultati_ocr = OCR_READER.readtext(img_ottimizzata, batch_size=16)
            righe_complete = ricostruisci_righe_orizzontali(risultati_ocr)
            righe_grezze = []
            for (_, testo_riga) in righe_complete:
                if is_fine_spesa(testo_riga):
                    break
                righe_grezze.append(testo_riga)
            risultati_batch.append(_parsa_righe_testo(righe_grezze))

    return risultati_batch