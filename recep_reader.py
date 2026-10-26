import warnings
from typing import List, Tuple
import cv2
import re
import numpy as np
import torch
import easyocr
from difflib import SequenceMatcher

warnings.filterwarnings("ignore", category=UserWarning, module="torch")
warnings.filterwarnings("ignore", category=RuntimeWarning, module="easyocr")
np.seterr(over='ignore', invalid='ignore')


def configura_dispositivo_gpu() -> bool:
    if torch.cuda.is_available():
        return True
    elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        return True
    return False


USA_GPU = configura_dispositivo_gpu()

# =====================================================================
# CONFIGURAZIONE TAGLI E PARAMETRI (Aggiornati empiricamente)
# =====================================================================
TAGLIO_ALTO_RATIO = 0.15  # Taglia l'intestazione superiore
TAGLIO_DESTRA_RATIO = 0.615  # Taglia i prezzi a destra
FATTORE_INGRANDIMENTO = 2.0
TOLLERANZA_Y_RIGA = 4.5

FILE_DEBUG_FINALE = "debug_solo_spesa.jpg"

PAROLE_FINE_SPESA = ["SUBTOTALE", "TOTALE COMPLESSIVO", "COMPLESSIVO"]
SOGLIA_SIMILARITA_OCR = 0.75

PATTERN_PULIZIA_IVA = re.compile(r'\s+\b(?:4|1[0O]|22)\s*(?:%|0/0|o/o|96|8)?\s*$', re.IGNORECASE)
PATTERN_PREZZI_FINALI = re.compile(r'\s+[\d]+[.,][\d]{2}\s*€?\s*$')

PATTERN_DETTAGLIO_CAD = re.compile(r'(?:\bCad\b|\bP[z2]\.?\s*\d+)', re.IGNORECASE)
PATTERN_DETTAGLIO_PESO = re.compile(r'([\dO]+[.,][\dO]+)\s*(kg|g|gr|l|ml|cl)\b', re.IGNORECASE)
PATTERN_PESO_INLINE = re.compile(r'\b(\d+[.,]?\d*)\s*(kg|g|gr|l|ml|cl)\b', re.IGNORECASE)
# =====================================================================

print("Caricamento modello OCR in memoria...")
OCR_READER = easyocr.Reader(['it'], gpu=USA_GPU, verbose=False)

ItemSpesa = Tuple[str, float, str, int]


def migliora_immagine_per_ocr(img_gray: np.ndarray) -> np.ndarray:
    img_zoom = cv2.resize(
        img_gray, None,
        fx=FATTORE_INGRANDIMENTO, fy=FATTORE_INGRANDIMENTO,
        interpolation=cv2.INTER_CUBIC
    )
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    img_contrasto = clahe.apply(img_zoom)
    return cv2.GaussianBlur(img_contrasto, (3, 3), 0)


def is_fine_spesa(testo_riga: str) -> bool:
    testo_pulito = re.sub(r'[^A-Z\s]', '', testo_riga.upper()).strip()
    if not testo_pulito:
        return False
    for target in PAROLE_FINE_SPESA:
        if target in testo_pulito or SequenceMatcher(None, testo_pulito, target).ratio() >= SOGLIA_SIMILARITA_OCR:
            return True
        for parola in testo_pulito.split():
            if len(parola) >= 5 and SequenceMatcher(None, parola, target).ratio() >= SOGLIA_SIMILARITA_OCR:
                return True
    return False


def ricostruisci_righe_orizzontali(risultati_ocr: list) -> List[Tuple[int, str]]:
    if not risultati_ocr:
        return []
    blocchi = []
    for (bbox, testo, conf) in risultati_ocr:
        y_top = int(bbox[0][1])
        y_bottom = int(bbox[2][1])
        y_centro = (y_top + y_bottom) // 2
        x_sinistra = int(bbox[0][0])
        blocchi.append((y_centro, x_sinistra, y_top, testo.strip()))
    blocchi.sort(key=lambda b: b[0])

    righe_raggruppate, riga_corrente = [], [blocchi[0]]
    for blocco in blocchi[1:]:
        if abs(blocco[0] - riga_corrente[-1][0]) <= TOLLERANZA_Y_RIGA:
            riga_corrente.append(blocco)
        else:
            righe_raggruppate.append(riga_corrente)
            riga_corrente = [blocco]
    righe_raggruppate.append(riga_corrente)

    righe_finali = []
    for gruppo in righe_raggruppate:
        gruppo.sort(key=lambda b: b[1])
        y_riferimento = min(b[2] for b in gruppo)
        testo_completo = " ".join(b[3] for b in gruppo if b[3])
        righe_finali.append((y_riferimento, testo_completo))
    return righe_finali


def estrai_tuple_da_immagine(percorso_immagine: str) -> List[ItemSpesa]:
    img = cv2.imread(percorso_immagine)
    if img is None:
        print(f"Errore: Impossibile caricare l'immagine {percorso_immagine}")
        return []

    img_gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, w = img_gray.shape[:2]

    if "debug" in percorso_immagine.lower():
        img_pre_tagliata = img_gray
    else:
        img_pre_tagliata = img_gray[int(h * TAGLIO_ALTO_RATIO):, :int(w * TAGLIO_DESTRA_RATIO)]

    img_ottimizzata = migliora_immagine_per_ocr(img_pre_tagliata)

    risultati_ocr = OCR_READER.readtext(img_ottimizzata)
    righe_complete = ricostruisci_righe_orizzontali(risultati_ocr)

    y_fine_dinamico = img_ottimizzata.shape[0]
    righe_spesa_grezze = []
    for (y_top, testo_riga) in righe_complete:
        if is_fine_spesa(testo_riga):
            y_fine_dinamico = y_top
            break
        righe_spesa_grezze.append(testo_riga)

    img_solo_spesa = img_ottimizzata[:y_fine_dinamico, :]
    if img_solo_spesa.size > 0 and "debug" not in percorso_immagine.lower():
        cv2.imwrite(FILE_DEBUG_FINALE, img_solo_spesa)

    lista_items: List[ItemSpesa] = []

    for riga in righe_spesa_grezze:
        riga_pulita = PATTERN_PULIZIA_IVA.sub('', riga).strip()
        riga_pulita = PATTERN_PREZZI_FINALI.sub('', riga_pulita).strip()

        match_peso_dettaglio = PATTERN_DETTAGLIO_PESO.search(riga_pulita)
        is_riga_cad = PATTERN_DETTAGLIO_CAD.search(riga_pulita) is not None

        if lista_items and (is_riga_cad or (match_peso_dettaglio and (
                "x" in riga_pulita.lower() or "eur" in riga_pulita.lower() or riga_pulita[0].isdigit()))):
            nome_prec, qta_prec, unita_prec, conf_prec = lista_items[-1]

            if is_riga_cad:
                numeri = re.findall(r'\b(\d+)\b', riga_pulita)
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

        nome_finale = re.sub(r'^[-.,:;€*\s]+|[-.,:;€*\s]+$', '', riga_pulita)
        if len(nome_finale) >= 2:
            lista_items.append((nome_finale, qta_default, unita_default, 1))

    return lista_items