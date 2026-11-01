"""
recepiet_reader.py - Lettura scontrini da immagine, 100% locale e gratuito (EasyOCR + OpenCV).

Novità rispetto alla versione precedente
-----------------------------------------
* Caricamento lazy di torch/easyocr: l'import è istantaneo e il parser è testabile senza OCR.
* Scala adattiva (EasyOCR ridimensiona comunque a canvas_size=2560: ingrandire sempre x2 era spreco).
* Raddrizzamento automatico (deskew) per foto scattate non perfettamente dritte.
* allowlist dei caratteri + uso della confidenza OCR (prima veniva scartata).
* Seconda passata automatica con binarizzazione adattiva se la prima è poco affidabile.
* Raggruppamento righe con tolleranza proporzionale all'altezza del testo (non più 18 px fissi).
* Parser più robusto (quantità "Cad" corretta, righe di dettaglio meno ambigue).
* Catalogo prodotti opzionale con correzione fuzzy: il sistema migliora man mano che lo usi.
* CLI: file singoli, cartelle, export JSON/CSV, immagini di debug opzionali.
* Rimosso Numba: per ~50-200 box il tempo di compilazione JIT supera di molto il risparmio.
* Fix thread-safety (CLAHE condiviso tra thread), niente scritture su disco involontarie,
  niente cudnn.benchmark (rallenta con input di dimensioni sempre diverse).

* Cronometri per fase (RisultatoScontrino.tempi) e righe OCR grezze conservate per il debug.

L'API precedente resta valida: estrai_tuple_da_immagine() e estrai_tuple_batch_concorrente().
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import json
import os
import re
import sys
import time
import warnings
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from statistics import median
from typing import Deque, Dict, Iterable, List, Optional, Sequence, Tuple

import cv2
import numpy as np

try:
    from rapidfuzz import fuzz as _rf_fuzz

    USA_RAPIDFUZZ = True
except ImportError:  # fallback stdlib
    from difflib import SequenceMatcher

    USA_RAPIDFUZZ = False

ItemSpesa = Tuple[str, float, str, int]
NUM_CORES = os.cpu_count() or 4
ESTENSIONI = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
ALLOWLIST = ("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
             "ÀÈÉÌÒÙàèéìòù0123456789 .,%'-/*€:+&()")


def similarita(a: str, b: str) -> float:
    """Similarità 0-100 (RapidFuzz in C++ se disponibile, altrimenti difflib)."""
    if USA_RAPIDFUZZ:
        return float(_rf_fuzz.ratio(a, b))
    return SequenceMatcher(None, a, b).ratio() * 100.0


# Fasi cronometrate, nell'ordine in cui avvengono
FASI = ("caricamento_modello", "decodifica", "ritaglio_scala", "raddrizzamento", "variante",
        "ocr", "righe", "parsing", "catalogo", "salvataggio_debug")


@contextlib.contextmanager
def _fase(tempi: Optional[Dict[str, float]], nome: str):
    """Somma in tempi[nome] i secondi trascorsi nel blocco (no-op se tempi è None)."""
    t0 = time.perf_counter()
    try:
        yield
    finally:
        if tempi is not None:
            tempi[nome] = tempi.get(nome, 0.0) + (time.perf_counter() - t0)


# =====================================================================
# CONFIGURAZIONE (un Profilo per ogni formato di scontrino/supermercato)
# =====================================================================
@dataclass(frozen=True)
class Profilo:
    taglio_alto: float = 0.15          # frazione di altezza da tagliare (intestazione)
    taglio_destra: float = 0.615       # frazione di larghezza da tenere (esclude prezzi)
    larghezza_target: int = 1500       # larghezza (px) dell'immagine data all'OCR
    scala_min: float = 0.5
    scala_max: float = 3.0
    raddrizza: bool = True
    tolleranza_riga: float = 0.6       # x altezza mediana delle parole
    soglia_fine: float = 75.0
    parole_fine: Tuple[str, ...] = ("SUBTOTALE", "TOTALE COMPLESSIVO", "COMPLESSIVO")
    marcatori_inizio: Tuple[str, ...] = ()   # es. ("DESCRIZIONE",): scarta tutto ciò che precede
    parole_ignora: Tuple[str, ...] = ()      # righe da saltare se contengono queste parole
    usa_allowlist: bool = True
    soglia_retry_conf: float = 0.55    # sotto questa confidenza media parte la 2a passata
    soglia_catalogo: float = 88.0


PROFILO_DEFAULT = Profilo()

# Regex pre-compilate
P_NON_ALFA = re.compile(r'[^A-Z\s]')
P_IVA = re.compile(r'\s+\b(?:4|1[0O]|22)\s*(?:%|0/0|o/o|96|8)?\s*$', re.I)
P_PREZZO = re.compile(r'\s+\d+[.,]\d{2}\s*€?\s*$')
P_CAD = re.compile(r'\bCad\b|\bP[z2]\.?\s*\d+', re.I)
P_DETT_PESO = re.compile(r'([\dO]+[.,][\dO]+)\s*(kg|gr|g|ml|cl|l)\b', re.I)
P_PESO_INLINE = re.compile(r'\b(\d+[.,]?\d*)\s*(kg|gr|g|ml|cl|l)\b', re.I)
P_MARCATORE_DETT = re.compile(r'\bx\b|×|\beur', re.I)
P_CONF_A = re.compile(r'(\d+)\s*(?:cad|p[z2])\b', re.I)
P_CONF_B = re.compile(r'\bp[z2]\.?\s*(\d+)', re.I)
P_INTERI = re.compile(r'\b\d+\b')
P_BORDI = re.compile(r'^[-.,:;€*\s]+|[-.,:;€*\s]+$')


# =====================================================================
# MODELLO OCR (lazy, singleton)
# =====================================================================
@lru_cache(maxsize=1)
def _get_reader():
    import torch
    import easyocr

    warnings.filterwarnings("ignore", category=UserWarning, module="torch")
    warnings.filterwarnings("ignore", category=RuntimeWarning, module="easyocr")
    torch.set_num_threads(NUM_CORES)
    gpu = False
    if torch.cuda.is_available():
        torch.backends.cuda.matmul.allow_tf32 = True
        gpu = True  # cudnn.benchmark volutamente OFF: input di forma variabile = ri-tuning continuo
    elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        gpu = True
    print(f"Caricamento OCR (GPU={gpu}, threads={NUM_CORES}, RapidFuzz={USA_RAPIDFUZZ})...",
          file=sys.stderr)
    return easyocr.Reader(["it"], gpu=gpu, verbose=False)


def carica_modello() -> float:
    """Carica (una sola volta) il modello OCR e restituisce i secondi impiegati."""
    t0 = time.perf_counter()
    _get_reader()
    return time.perf_counter() - t0


def _esegui_ocr(img: np.ndarray, profilo: Profilo, tempi: Optional[Dict[str, float]] = None) -> list:
    import torch

    with _fase(tempi, "caricamento_modello"):  # ~0 se già caricato
        reader = _get_reader()
    kw = dict(batch_size=8, workers=0, paragraph=False)
    if profilo.usa_allowlist:
        kw["allowlist"] = ALLOWLIST
    with _fase(tempi, "ocr"), torch.inference_mode():
        return reader.readtext(img, **kw)


# =====================================================================
# PRE-PROCESSING IMMAGINE
# =====================================================================
def _leggi_grigio(percorso: str) -> Optional[np.ndarray]:
    """imread che funziona anche con percorsi non-ASCII (Windows)."""
    try:
        dati = np.fromfile(percorso, dtype=np.uint8)
    except OSError:
        return None
    if dati.size == 0:
        return None
    return cv2.imdecode(dati, cv2.IMREAD_GRAYSCALE)


def _stima_inclinazione(gray: np.ndarray, max_deg: float = 6.0, step: float = 0.5) -> float:
    """Profilo di proiezione: l'angolo che massimizza la varianza delle righe di testo."""
    h, w = gray.shape[:2]
    if w > 400:
        gray = cv2.resize(gray, None, fx=400.0 / w, fy=400.0 / w, interpolation=cv2.INTER_AREA)
    bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]
    hh, ww = bw.shape

    def punteggio(ang: float) -> float:
        M = cv2.getRotationMatrix2D((ww / 2, hh / 2), ang, 1.0)
        r = cv2.warpAffine(bw, M, (ww, hh), flags=cv2.INTER_NEAREST)
        return float(np.var(r.sum(axis=1, dtype=np.float32)))

    migliore, p_max = 0.0, punteggio(0.0) * 1.03  # serve un guadagno reale per ruotare
    for ang in np.arange(-max_deg, max_deg + step / 2, step):
        if abs(ang) < 1e-9:
            continue
        p = punteggio(float(ang))
        if p > p_max:
            migliore, p_max = float(ang), p
    return migliore


def prepara_base(percorso: str, profilo: Profilo = PROFILO_DEFAULT,
                 gia_ritagliata: Optional[bool] = None,
                 tempi: Optional[Dict[str, float]] = None) -> Optional[np.ndarray]:
    """Carica, ritaglia, scala e raddrizza. Thread-safe (nessuno stato condiviso)."""
    with _fase(tempi, "decodifica"):
        img = _leggi_grigio(percorso)
    if img is None:
        return None
    if gia_ritagliata is None:
        gia_ritagliata = Path(percorso).name.lower().startswith("debug")

    with _fase(tempi, "ritaglio_scala"):
        if not gia_ritagliata:
            h, w = img.shape[:2]
            img = img[int(h * profilo.taglio_alto):, :int(w * profilo.taglio_destra)]
        if img.size == 0:
            return None
        scala = profilo.larghezza_target / img.shape[1]
        scala = min(max(scala, profilo.scala_min), profilo.scala_max)
        interp = cv2.INTER_CUBIC if scala > 1 else cv2.INTER_AREA
        img = cv2.resize(img, None, fx=scala, fy=scala, interpolation=interp)

    if profilo.raddrizza:
        with _fase(tempi, "raddrizzamento"):
            ang = _stima_inclinazione(img)
            if abs(ang) >= 0.5:
                h, w = img.shape[:2]
                M = cv2.getRotationMatrix2D((w / 2, h / 2), ang, 1.0)
                img = cv2.warpAffine(img, M, (w, h), flags=cv2.INTER_CUBIC,
                                     borderMode=cv2.BORDER_REPLICATE)
    return np.ascontiguousarray(img)


def applica_variante(base: np.ndarray, nome: str) -> np.ndarray:
    if nome == "adattiva":
        sfocata = cv2.GaussianBlur(base, (3, 3), 0)
        return cv2.adaptiveThreshold(sfocata, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                     cv2.THRESH_BINARY, 41, 12)
    # CLAHE creato per chiamata: l'oggetto cv2 non è thread-safe se condiviso
    return cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(base)


# =====================================================================
# RICOSTRUZIONE RIGHE
# =====================================================================
@dataclass
class Riga:
    y_top: int
    testo: str
    conf: float


def ricostruisci_righe(risultati_ocr: Sequence, tolleranza_rel: float = 0.6) -> List[Riga]:
    parole = []  # (x0, y_centro, y_top, altezza, testo, conf)
    for bbox, testo, conf in risultati_ocr:
        testo = str(testo).strip()
        if not testo:
            continue
        xs = [p[0] for p in bbox]
        ys = [p[1] for p in bbox]
        parole.append((min(xs), (min(ys) + max(ys)) / 2.0, min(ys), max(ys) - min(ys),
                       testo, float(conf)))
    if not parole:
        return []

    tol = max(6.0, tolleranza_rel * median(p[3] for p in parole))
    parole.sort(key=lambda p: p[1])
    gruppi: List[list] = [[parole[0]]]
    for p in parole[1:]:
        media_y = sum(q[1] for q in gruppi[-1]) / len(gruppi[-1])
        if abs(p[1] - media_y) <= tol:
            gruppi[-1].append(p)
        else:
            gruppi.append([p])

    righe = []
    for g in gruppi:
        g.sort(key=lambda p: p[0])
        righe.append(Riga(int(min(p[2] for p in g)),
                          " ".join(p[4] for p in g),
                          sum(p[5] for p in g) / len(g)))
    righe.sort(key=lambda r: r.y_top)
    return righe


def is_fine_spesa(testo_riga: str, parole_fine: Sequence[str] = PROFILO_DEFAULT.parole_fine,
                  soglia: float = PROFILO_DEFAULT.soglia_fine) -> bool:
    pulito = P_NON_ALFA.sub('', testo_riga.upper()).strip()
    if not pulito:
        return False
    lunghe = [p for p in pulito.split() if len(p) >= 5]
    for target in parole_fine:
        if target in pulito or similarita(pulito, target) >= soglia:
            return True
        if any(similarita(p, target) >= soglia for p in lunghe):
            return True
    return False


# =====================================================================
# PARSER
# =====================================================================
@dataclass
class Articolo:
    nome: str
    qta: float = 1.0
    unita: str = "pz"
    confezioni: int = 1
    conf_ocr: float = 0.0
    riga_ocr: str = ""

    def tupla(self) -> ItemSpesa:
        return (self.nome, self.qta, self.unita, self.confezioni)


def _norm_unita(u: str) -> str:
    u = u.lower()
    return "g" if u == "gr" else u


def _estrai_confezioni(testo: str) -> Optional[int]:
    m = P_CONF_A.search(testo) or P_CONF_B.search(testo)
    if m:
        n = int(m.group(1))
    else:
        interi = P_INTERI.findall(testo)
        n = int(interi[0]) if interi else 0  # il PRIMO intero: gli ultimi sono spesso prezzi
    return n if 0 < n < 100 else None


def parsa_righe(righe: Sequence[Riga], profilo: Profilo = PROFILO_DEFAULT) -> List[Articolo]:
    articoli: List[Articolo] = []
    for riga in righe:
        testo = P_IVA.sub('', riga.testo).strip()
        testo = P_PREZZO.sub('', testo).strip()
        if not testo:
            continue
        if profilo.parole_ignora and any(w in testo.upper() for w in profilo.parole_ignora):
            continue

        m_peso = P_DETT_PESO.search(testo)
        is_cad = P_CAD.search(testo) is not None
        is_dettaglio = is_cad or (m_peso is not None and (
            testo[0].isdigit() or P_MARCATORE_DETT.search(testo) is not None))

        if is_dettaglio:
            if not articoli:
                continue  # riga di dettaglio senza articolo a cui legarla
            ultimo = articoli[-1]
            if is_cad:
                n = _estrai_confezioni(testo)
                if n:
                    ultimo.confezioni = n
            elif m_peso:
                try:
                    ultimo.qta = float(m_peso.group(1).upper().replace('O', '0').replace(',', '.'))
                    ultimo.unita = _norm_unita(m_peso.group(2))
                except ValueError:
                    pass
            continue

        qta, unita = 1.0, "pz"
        m_in = P_PESO_INLINE.search(testo)
        if m_in:
            try:
                qta = float(m_in.group(1).replace(',', '.'))
                unita = _norm_unita(m_in.group(2))
                testo = (testo[:m_in.start()] + testo[m_in.end():]).strip()
            except ValueError:
                pass

        nome = P_BORDI.sub('', testo)
        if sum(c.isalpha() for c in nome) >= 2:
            articoli.append(Articolo(nome, qta, unita, 1, riga.conf, riga.testo))
    return articoli


# =====================================================================
# CATALOGO PRODOTTI (apprendimento locale, gratuito)
# =====================================================================
def _norm_nome(s: str) -> str:
    return re.sub(r'[^A-Z0-9 ]', '', s.upper()).strip()


def carica_catalogo(percorso: Optional[str]) -> List[str]:
    if not percorso or not Path(percorso).exists():
        return []
    righe = Path(percorso).read_text(encoding="utf-8").splitlines()
    return [r.strip() for r in righe if r.strip()]


def aggiungi_al_catalogo(percorso: str, nomi: Iterable[str]) -> int:
    esistenti = {_norm_nome(n) for n in carica_catalogo(percorso)}
    nuovi = [n for n in dict.fromkeys(nomi) if _norm_nome(n) not in esistenti]
    if nuovi:
        with open(percorso, "a", encoding="utf-8") as f:
            f.write("\n".join(nuovi) + "\n")
    return len(nuovi)


def correggi_con_catalogo(nome: str, catalogo: Sequence[str], soglia: float = 88.0) -> str:
    n = _norm_nome(nome)
    migliore, p_max = nome, soglia
    for voce in catalogo:
        s = similarita(n, _norm_nome(voce))
        if s >= p_max:
            migliore, p_max = voce, s
    return migliore


# =====================================================================
# PIPELINE
# =====================================================================
@dataclass
class RisultatoScontrino:
    percorso: str
    articoli: List[Articolo] = field(default_factory=list)
    conf_media: float = 0.0
    strategia: str = ""
    secondi: float = 0.0                       # somma dei tempi delle fasi
    errore: str = ""
    tempi: Dict[str, float] = field(default_factory=dict)   # secondi per fase (vedi FASI)
    n_passate: int = 0                         # 2 = è servita la passata di rinforzo
    # righe OCR grezze della passata scelta: (testo, confidenza, considerata_nella_spesa)
    righe_ocr: List[Tuple[str, float, bool]] = field(default_factory=list)

    @property
    def tuple_items(self) -> List[ItemSpesa]:
        return [a.tupla() for a in self.articoli]


@dataclass
class _EsitoPassata:
    articoli: List[Articolo]
    conf: float
    img: np.ndarray
    y_fine: int
    righe: List[Tuple[str, float, bool]]


def _passata(base: np.ndarray, profilo: Profilo, strategia: str,
             tempi: Optional[Dict[str, float]] = None) -> _EsitoPassata:
    with _fase(tempi, "variante"):
        img = applica_variante(base, strategia)
    risultati = _esegui_ocr(img, profilo, tempi)
    with _fase(tempi, "righe"):
        righe = ricostruisci_righe(risultati, profilo.tolleranza_riga)

    with _fase(tempi, "parsing"):
        inizio = 0
        if profilo.marcatori_inizio:
            for i, r in enumerate(righe):
                if any(m in r.testo.upper() for m in profilo.marcatori_inizio):
                    inizio = i + 1
                    break

        spesa, y_fine, fine_idx = [], img.shape[0], len(righe)
        for i in range(inizio, len(righe)):
            if is_fine_spesa(righe[i].testo, profilo.parole_fine, profilo.soglia_fine):
                y_fine, fine_idx = righe[i].y_top, i
                break
            spesa.append(righe[i])

        articoli = parsa_righe(spesa, profilo)
    conf = sum(a.conf_ocr for a in articoli) / len(articoli) if articoli else 0.0
    info = [(r.testo, r.conf, inizio <= i < fine_idx) for i, r in enumerate(righe)]
    return _EsitoPassata(articoli, conf, img, y_fine, info)


def _leggi_da_base(percorso: str, base: Optional[np.ndarray], profilo: Profilo,
                   catalogo: Sequence[str], debug_dir: Optional[str],
                   tempi: Optional[Dict[str, float]] = None) -> RisultatoScontrino:
    tempi = {} if tempi is None else tempi
    if base is None:
        return RisultatoScontrino(percorso, errore="Impossibile caricare l'immagine",
                                  tempi=tempi, secondi=sum(tempi.values()))

    migliore, migliore_strategia, n_passate = None, "", 0
    for strategia in ("clahe", "adattiva"):
        esito = _passata(base, profilo, strategia, tempi)
        n_passate += 1
        punteggio = sum(a.conf_ocr for a in esito.articoli)  # più articoli affidabili = meglio
        if migliore is None or punteggio > sum(a.conf_ocr for a in migliore.articoli):
            migliore, migliore_strategia = esito, strategia
        if esito.articoli and esito.conf >= profilo.soglia_retry_conf:
            break  # prima passata già affidabile: niente costo extra

    if catalogo:
        with _fase(tempi, "catalogo"):
            for a in migliore.articoli:
                a.nome = correggi_con_catalogo(a.nome, catalogo, profilo.soglia_catalogo)

    if debug_dir:
        with _fase(tempi, "salvataggio_debug"):
            Path(debug_dir).mkdir(parents=True, exist_ok=True)
            ok, buf = cv2.imencode(".jpg", migliore.img[:max(migliore.y_fine, 1), :])
            if ok:
                buf.tofile(str(Path(debug_dir) / f"{Path(percorso).stem}_solo_spesa.jpg"))

    return RisultatoScontrino(percorso, migliore.articoli, migliore.conf, migliore_strategia,
                              sum(tempi.values()), "", tempi, n_passate, migliore.righe)


def leggi_scontrino(percorso: str, profilo: Profilo = PROFILO_DEFAULT,
                    catalogo: Sequence[str] = (), debug_dir: Optional[str] = None,
                    gia_ritagliata: Optional[bool] = None) -> RisultatoScontrino:
    tempi: Dict[str, float] = {}
    base = prepara_base(percorso, profilo, gia_ritagliata, tempi)
    return _leggi_da_base(percorso, base, profilo, catalogo, debug_dir, tempi)


def leggi_batch(percorsi: Iterable[str], profilo: Profilo = PROFILO_DEFAULT,
                catalogo: Sequence[str] = (), debug_dir: Optional[str] = None,
                prefetch: int = 4) -> List[RisultatoScontrino]:
    """Pre-processing OpenCV in parallelo (finestra scorrevole: memoria limitata)
    mentre l'OCR lavora in sequenza sul main thread."""
    it = iter(list(percorsi))
    coda: Deque = deque()
    risultati: List[RisultatoScontrino] = []
    with ThreadPoolExecutor(max_workers=min(4, NUM_CORES)) as ex:
        def alimenta():
            while len(coda) < prefetch:
                p = next(it, None)
                if p is None:
                    return
                t: Dict[str, float] = {}
                coda.append((p, t, ex.submit(prepara_base, p, profilo, None, t)))

        alimenta()
        while coda:
            p, t, fut = coda.popleft()
            alimenta()
            try:
                base = fut.result()
            except Exception as e:  # un file rotto non deve fermare il batch
                risultati.append(RisultatoScontrino(p, errore=str(e), tempi=t))
                continue
            risultati.append(_leggi_da_base(p, base, profilo, catalogo, debug_dir, t))
    return risultati


# ---- API retro-compatibile -------------------------------------------------
def estrai_tuple_da_immagine(percorso_immagine: str, **kwargs) -> List[ItemSpesa]:
    r = leggi_scontrino(percorso_immagine, **kwargs)
    if r.errore:
        print(f"Errore: {r.errore} ({percorso_immagine})", file=sys.stderr)
        return []
    return r.tuple_items


def estrai_tuple_batch_concorrente(percorsi_immagini: List[str], **kwargs) -> List[List[ItemSpesa]]:
    return [r.tuple_items for r in leggi_batch(percorsi_immagini, **kwargs)]


# =====================================================================
# CLI
# =====================================================================
def _raccogli_immagini(percorsi: Sequence[str]) -> List[str]:
    out: List[str] = []
    for p in percorsi:
        path = Path(p)
        if path.is_dir():
            out += sorted(str(f) for f in path.iterdir() if f.suffix.lower() in ESTENSIONI)
        else:
            out.append(str(path))
    return out


def _salva_json(risultati: List[RisultatoScontrino], percorso: str) -> None:
    Path(percorso).write_text(json.dumps([asdict(r) for r in risultati], ensure_ascii=False,
                                         indent=2), encoding="utf-8")


def _salva_csv(risultati: List[RisultatoScontrino], percorso: str) -> None:
    with open(percorso, "w", newline="", encoding="utf-8-sig") as f:  # ';' + BOM: Excel italiano
        w = csv.writer(f, delimiter=";")
        w.writerow(["file", "nome", "quantita", "unita", "confezioni", "confidenza_ocr"])
        for r in risultati:
            for a in r.articoli:
                w.writerow([Path(r.percorso).name, a.nome, a.qta, a.unita, a.confezioni,
                            f"{a.conf_ocr:.2f}"])


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Legge scontrini (OCR locale, gratuito).")
    ap.add_argument("input", nargs="+", help="immagini e/o cartelle")
    ap.add_argument("--json")
    ap.add_argument("--csv")
    ap.add_argument("--catalogo", help="file .txt con un prodotto per riga")
    ap.add_argument("--impara", action="store_true", help="aggiunge i nomi letti al catalogo")
    ap.add_argument("--debug-dir")
    ap.add_argument("--taglio-alto", type=float, default=PROFILO_DEFAULT.taglio_alto)
    ap.add_argument("--taglio-destra", type=float, default=PROFILO_DEFAULT.taglio_destra)
    ap.add_argument("--no-raddrizza", action="store_true")
    a = ap.parse_args(argv)

    profilo = replace(PROFILO_DEFAULT, taglio_alto=a.taglio_alto, taglio_destra=a.taglio_destra,
                      raddrizza=not a.no_raddrizza)
    immagini = _raccogli_immagini(a.input)
    catalogo = carica_catalogo(a.catalogo)
    risultati = leggi_batch(immagini, profilo, catalogo, a.debug_dir)

    for r in risultati:
        print(f"\n== {r.percorso}  [{r.strategia or '-'} | conf {r.conf_media:.2f} | {r.secondi:.1f}s]")
        if r.errore:
            print(f"   ERRORE: {r.errore}")
        for art in r.articoli:
            flag = "  ?" if art.conf_ocr < 0.5 else ""
            print(f"   {art.nome:<32} {art.qta:>7g} {art.unita:<3} x{art.confezioni}{flag}")
    if a.json:
        _salva_json(risultati, a.json)
    if a.csv:
        _salva_csv(risultati, a.csv)
    if a.catalogo and a.impara:
        n = aggiungi_al_catalogo(a.catalogo, (x.nome for r in risultati for x in r.articoli))
        print(f"\nCatalogo aggiornato: +{n} prodotti")
    return 0


if __name__ == "__main__":
    sys.exit(main())
