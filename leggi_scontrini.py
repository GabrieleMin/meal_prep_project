import warnings
from typing import List, Tuple
import cv2
import re
import numpy as np
import torch
import easyocr
from difflib import SequenceMatcher

# Silenzia i warning di PyTorch e gli overflow numerici interni di NumPy/EasyOCR
warnings.filterwarnings("ignore", category=UserWarning, module="torch")
warnings.filterwarnings("ignore", category=RuntimeWarning, module="easyocr")
np.seterr(over='ignore', invalid='ignore')


# =====================================================================
# RILEVAMENTO AUTOMATICO GPU
# =====================================================================
def configura_dispositivo_gpu() -> bool:
    """Rileva la presenza di GPU compatibili (CUDA o MPS) e ottimizza PyTorch."""
    if torch.cuda.is_available():
        num_gpu = torch.cuda.device_count()
        nome_gpu = torch.cuda.get_device_name(0)
        print(f"[HARDWARE] Trovata GPU NVIDIA ({num_gpu}x): {nome_gpu}")
        torch.backends.cudnn.benchmark = True
        return True
    elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        print("[HARDWARE] Trovata GPU Apple Silicon (MPS)")
        return True
    else:
        print("[HARDWARE] Nessuna GPU compatibile rilevata. Esecuzione su CPU.")
        return False


USA_GPU = configura_dispositivo_gpu()

# =====================================================================
# CONFIGURAZIONE TAGLI E PARAMETRI (Define)
# =====================================================================
TAGLIO_ALTO_RATIO = 0.15  # Taglia il primo 22% dall'alto
TAGLIO_DESTRA_RATIO = 0.68  # Mantiene l'85% sinistro (include colonna IVA)
FATTORE_INGRANDIMENTO = 2.0  # Ingrandimento 2.0x per bilanciare nitidezza e stabilità OCR
TOLLERANZA_Y_RIGA = 18  # Tolleranza in pixel (calibrata su zoom 2.0x)

FILE_DEBUG_FINALE = "debug_solo_spesa.jpg"

# Whitelist caratteri: impedisce all'OCR di trascrivere simboli inesistenti
CARATTERI_CONSENTITI = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789.,%€/-'() "

PAROLE_FINE_SPESA = [
    "SUBTOTALE",
    "TOTALE COMPLESSIVO",
    "COMPLESSIVO"
]
SOGLIA_SIMILARITA_OCR = 0.75

# Regex per intercettare le aliquote IVA (4%, 10%, 22%) e comuni errori di lettura del '%'
PATTERN_IVA = re.compile(r'\b(4|10|22)\s*(?:%|0/0|o/o|96|9/0|0/o)\b', re.IGNORECASE)

# Regex per rimuovere eventuali prezzi rimasti attaccati prima dell'IVA
PATTERN_PREZZI_FINALI = re.compile(r'[\d]+[.,][\d]{2}\s*€?\s*$')

# Regex per scartare righe descrittive di peso/quantità
PATTERN_MISURA_O_QTA = re.compile(
    r'(\d+[.,]?\d*\s*(kg|g|gr|l|ml|cl|cad|pz)\b|\b\d+\s*[xX]\b)',
    re.IGNORECASE
)
# =====================================================================

print("Caricamento modello OCR in memoria...")
OCR_READER = easyocr.Reader(['it'], gpu=USA_GPU, verbose=False)


def migliora_immagine_per_ocr(img_gray: np.ndarray) -> np.ndarray:
    """
    Pipeline calibrata per evitare artefatti sui bordi delle lettere:
    1. Upscaling Bicubico
    2. Rimozione ombre tramite divisione morfologica (sbianca lo sfondo)
    3. Filtro bilaterale (rimuove il rumore della carta mantenendo i bordi del testo taglienti)
    4. Normalizzazione del contrasto (rende il testo nero profondo su sfondo bianco)
    """
    # 1. Ingrandimento
    img_zoom = cv2.resize(
        img_gray, None,
        fx=FATTORE_INGRANDIMENTO, fy=FATTORE_INGRANDIMENTO,
        interpolation=cv2.INTER_CUBIC
    )

    # 2. Rimozione ombre e pieghe dello scontrino (porta lo sfondo a bianco uniforme)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (35, 35))
    sfondo = cv2.morphologyEx(img_zoom, cv2.MORPH_DILATE, kernel)
    img_senza_ombre = cv2.divide(img_zoom, sfondo, scale=255)

    # 3. Filtro Bilaterale: liscia la grana della carta termica senza intaccare i bordi delle lettere
    img_filtrata = cv2.bilateralFilter(img_senza_ombre, d=9, sigmaColor=75, sigmaSpace=75)

    # 4. Scurisce l'inchiostro sbiadito (Gamma & Contrast Stretching)
    # Tutto ciò che è quasi bianco (>220) diventa bianco puro (255), il testo viene scurito
    lut = np.arange(256, dtype=np.uint8)
    lut = np.clip((lut - 30) * (255.0 / (220 - 30)), 0, 255).astype(np.uint8)
    img_finale = cv2.LUT(img_filtrata, lut)

    return img_finale


def is_fine_spesa(testo_riga: str) -> bool:
    """Verifica se la riga corrisponde a 'subtotale' o 'TOTALE COMPLESSIVO'."""
    testo_pulito = re.sub(r'[^A-Z\s]', '', testo_riga.upper()).strip()
    if not testo_pulito:
        return False

    for target in PAROLE_FINE_SPESA:
        if target in testo_pulito:
            return True
        if SequenceMatcher(None, testo_pulito, target).ratio() >= SOGLIA_SIMILARITA_OCR:
            return True
        for parola in testo_pulito.split():
            if len(parola) >= 5 and SequenceMatcher(None, parola, target).ratio() >= SOGLIA_SIMILARITA_OCR:
                return True
    return False


def ricostruisci_righe_orizzontali(risultati_ocr: list) -> List[Tuple[int, str]]:
    """
    Raggruppa i frammenti di testo rilevati da EasyOCR che si trovano
    sulla stessa riga orizzontale e li unisce ordinandoli da sinistra a destra.
    """
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

    righe_raggruppate = []
    riga_corrente = [blocchi[0]]

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


def estrai_nomi_da_immagine(percorso_immagine: str) -> List[str]:
    img = cv2.imread(percorso_immagine)
    if img is None:
        print(f"Errore: Impossibile caricare l'immagine {percorso_immagine}")
        return []

    img_gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    altezza_orig, larghezza_orig = img_gray.shape[:2]

    # 1. Taglio dall'alto e taglio colonna destra
    y_inizio = int(altezza_orig * TAGLIO_ALTO_RATIO)
    x_fine = int(larghezza_orig * TAGLIO_DESTRA_RATIO)
    img_pre_tagliata = img_gray[y_inizio:, :x_fine]

    # 2. Pre-processing per scontrini
    print("Pre-processing dell'immagine in corso...")
    img_ottimizzata = migliora_immagine_per_ocr(img_pre_tagliata)

    # 3. Lettura OCR con decoder 'greedy' (evita l'overflow scalare di beamsearch)
    print("Analisi OCR dell'immagine in corso...")
    risultati_ocr = OCR_READER.readtext(
        img_ottimizzata,
        decoder='greedy',  # Risolve il bug di overflow in easyocr/utils.py
        allowlist=CARATTERI_CONSENTITI,  # Filtra caratteri spazzatura
        mag_ratio=1.5,  # Aiuta il rilevamento dei caratteri piccoli
        contrast_ths=0.1,
        adjust_contrast=0.5,
        width_ths=0.3  # Evita di fondere insieme colonne troppo distanti
    )

    righe_complete = ricostruisci_righe_orizzontali(risultati_ocr)

    y_fine_dinamico = img_ottimizzata.shape[0]
    righe_spesa_grezze = []

    # 4. Individuazione fine spesa ("SUBTOTALE" / "TOTALE COMPLESSIVO")
    for (y_top, testo_riga) in righe_complete:
        if is_fine_spesa(testo_riga):
            print(f"[DEBUG] Fine spesa rilevata alla riga: '{testo_riga}' (Y={y_top})")
            y_fine_dinamico = y_top
            break
        righe_spesa_grezze.append(testo_riga)

    # Salvataggio nell'ambiente del ritaglio finale ottimizzato
    img_solo_spesa = img_ottimizzata[:y_fine_dinamico, :]
    if img_solo_spesa.size > 0:
        cv2.imwrite(FILE_DEBUG_FINALE, img_solo_spesa)
        print(f"[DEBUG] Immagine ritagliata salvata in: {FILE_DEBUG_FINALE}")

    # 5. Divisione ed estrazione nomi tramite ancora IVA (4%, 10%, 22%)
    nomi_estratti = []
    for riga in righe_spesa_grezze:
        print(f"[OCR RIGA]: {riga}")

        matches_iva = list(PATTERN_IVA.finditer(riga))

        if matches_iva:
            inizio_segmento = 0
            for match in matches_iva:
                porzione_prodotto = riga[inizio_segmento:match.start()].strip()
                inizio_segmento = match.end()

                nome_pulito = PATTERN_PREZZI_FINALI.sub('', porzione_prodotto).strip()
                nome_pulito = re.sub(r'[-.,:;€*]+$', '', nome_pulito).strip()

                if len(nome_pulito) >= 2 and not PATTERN_MISURA_O_QTA.search(nome_pulito):
                    nomi_estratti.append(nome_pulito)

    return nomi_estratti


def main():
    immagine_test = "material_fro_leggi_scontrini.jpeg"
    nomi_prodotti = estrai_nomi_da_immagine(immagine_test)

    print("\n--- Prodotti Estratti (Validati con IVA 4%, 10%, 22%) ---")
    for nome in nomi_prodotti:
        print(f"- {nome}")


if __name__ == "__main__":
    main()