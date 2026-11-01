"""
test_performance_recepiet_reader.py - Test e collaudo di recepiet_reader.py (un solo file).

MODALITÀ 1 - Collaudo su cartella (default): legge in sequenza tutte le immagini della cartella
`scontrini/` (stesso livello di questo file), scrive `<nome>_lettura.txt` per ciascuna in
`output_letture/` e un `RIEPILOGO_tempi.txt`, cronometrando ogni fase principale.

    python test_performance_recepiet_reader.py
    python test_performance_recepiet_reader.py --cartella altri_scontrini --out risultati
    python test_performance_recepiet_reader.py --catalogo catalogo.txt --min-f1 0.8

Ground truth opzionale: se accanto a `foto.jpg` esiste `foto_truth.json`
(lista di [nome, quantita, unita, confezioni]) il report include il confronto
(precision / recall / F1). Per `material_fro_leggi_scontrini` è già incorporata.

MODALITÀ 2 - Unit test veloci (nessun modello OCR, nessuna immagine reale):

    python test_performance_recepiet_reader.py --unit        # oppure: pytest -v test_performance_recepiet_reader.py
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from dataclasses import replace
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np

import recepiet_reader as ls
from recepiet_reader import Riga

ItemSpesa = Tuple[str, float, str, int]

QUI = Path(__file__).resolve().parent
CARTELLA_DEFAULT = QUI / "scontrini"
OUT_DEFAULT = QUI / "output_letture"

GROUND_TRUTH_INCORPORATE: Dict[str, List[ItemSpesa]] = {
    "material_fro_leggi_scontrini": [
        ("LENTICCHIE", 1.0, "pz", 1),
        ("NISSIN CUP NOODLES", 1.0, "pz", 1),
        ("TOFU BIO NATURALE", 1.0, "pz", 2),
        ("TAVOL.VITAMINA C LIM", 1.0, "pz", 1),
        ("UOVA ALL.TERRA", 1.0, "pz", 1),
        ("FAGIOLI ROSSI", 1.0, "pz", 1),
        ("FIOCCHI D'AVENA", 1.0, "pz", 1),
        ("COLLUTORIO ALLIN1", 1.0, "pz", 1),
        ("YOGURT GRECO 0%", 1.0, "pz", 1),
        ("CAR.EUC.MENT.SEN.ZUC", 1.0, "pz", 1),
        ("LUNETTE GORGONZOLA", 1.0, "pz", 1),
        ("ASIAGO PRESSATO DOP", 1.0, "pz", 1),
        ("BANANE", 0.838, "kg", 1),
        ("PERE", 1.0, "kg", 1),
        ("SEITAN BIO", 1.0, "pz", 2),
    ]
}


# =====================================================================
# CONFRONTO CON GROUND TRUTH
# =====================================================================
def _norm(s: str) -> str:
    return "".join(c for c in s.upper() if c.isalnum())


def abbina(attesi: Sequence[ItemSpesa], estratti: Sequence[ItemSpesa],
           soglia: float = 0.60) -> Dict[int, Tuple[int, float]]:
    """Abbinamento greedy uno-a-uno per similarità decrescente: {idx_atteso: (idx_estratto, sim)}."""
    coppie = []
    for i, a in enumerate(attesi):
        na = _norm(a[0])
        for j, e in enumerate(estratti):
            sim = SequenceMatcher(None, _norm(e[0]), na).ratio()
            if sim >= soglia:
                coppie.append((sim, i, j))
    coppie.sort(reverse=True)
    usati_i, usati_j, out = set(), set(), {}
    for sim, i, j in coppie:
        if i not in usati_i and j not in usati_j:
            usati_i.add(i)
            usati_j.add(j)
            out[i] = (j, sim)
    return out


def carica_ground_truth(immagine: Path) -> Optional[List[ItemSpesa]]:
    sidecar = immagine.with_name(f"{immagine.stem}_truth.json")
    if sidecar.exists():
        dati = json.loads(sidecar.read_text(encoding="utf-8"))
        return [(str(n), float(q), str(u), int(c)) for n, q, u, c in dati]
    return GROUND_TRUTH_INCORPORATE.get(immagine.stem.lower())


def confronta(attesi: Sequence[ItemSpesa], estratti: Sequence[ItemSpesa],
              soglia: float = 0.60) -> Tuple[List[str], Dict[str, float]]:
    abbinati = abbina(attesi, estratti, soglia)
    righe: List[str] = []
    qta_ok_n = conf_ok_n = perfette = 0
    for i, atteso in enumerate(attesi):
        if i not in abbinati:
            righe.append(f"{i + 1:02d}. [NON TROVATO]       | Atteso:   {atteso}")
            continue
        j, sim = abbinati[i]
        est = estratti[j]
        qta_ok = abs(est[1] - atteso[1]) < 0.01 and est[2].lower() == atteso[2].lower()
        conf_ok = est[3] == atteso[3]
        qta_ok_n += qta_ok
        conf_ok_n += conf_ok
        perfette += qta_ok and conf_ok
        if qta_ok and conf_ok:
            esito = "[OK COMPLETO]"
        else:
            motivi = [m for m, ok in (("QTA/UNITA", qta_ok), ("CONFEZIONI", conf_ok)) if not ok]
            esito = "[ERR " + "+".join(motivi) + "]"
        righe.append(f"{i + 1:02d}. {esito:19}| Atteso:   {atteso}")
        righe.append(f"    {'':19}| Estratto: {est} (sim. nome {sim * 100:.0f}%)")

    usati = {v[0] for v in abbinati.values()}
    falsi = [e for j, e in enumerate(estratti) if j not in usati]
    if falsi:
        righe.append("")
        righe.append("Falsi positivi (estratti senza corrispondenza attesa):")
        righe += [f"   - {e}" for e in falsi]

    n_att, n_est, n_trov = len(attesi), len(estratti), len(abbinati)
    recall = n_trov / n_att if n_att else 0.0
    precision = n_trov / n_est if n_est else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    righe += ["", f"Nomi trovati (recall):      {n_trov}/{n_att} ({recall * 100:.1f}%)",
              f"Precisione:                 {n_trov}/{n_est} ({precision * 100:.1f}%)",
              f"F1:                         {f1 * 100:.1f}%",
              f"Quantita+unita corrette:    {qta_ok_n}/{n_trov}",
              f"Confezioni corrette:        {conf_ok_n}/{n_trov}",
              f"Tuple perfette:             {perfette}/{n_att} "
              f"({(perfette / n_att * 100) if n_att else 0:.1f}%)"]
    return righe, {"recall": recall, "precision": precision, "f1": f1, "perfette": perfette,
                   "attesi": n_att}


# =====================================================================
# REPORT PER SINGOLO SCONTRINO
# =====================================================================
LINEA = "=" * 78


def tabella_tempi(tempi: Dict[str, float], wall: Optional[float] = None) -> List[str]:
    somma = sum(tempi.values())
    out = [f"  {'fase':<22}{'secondi':>10}{'%':>8}"]
    for fase in ls.FASI:
        v = tempi.get(fase, 0.0)
        out.append(f"  {fase:<22}{v:>10.3f}{(v / somma * 100 if somma else 0):>7.1f}%")
    out.append(f"  {'-' * 40}")
    out.append(f"  {'SOMMA FASI':<22}{somma:>10.3f}")
    if wall is not None:
        out.append(f"  {'TEMPO REALE (wall)':<22}{wall:>10.3f}")
    return out


def costruisci_report(nome_file: str, r: ls.RisultatoScontrino, wall: float,
                      ground_truth: Optional[Sequence[ItemSpesa]], soglia_nome: float
                      ) -> Tuple[str, Optional[Dict[str, float]]]:
    L = [LINEA, f"LETTURA SCONTRINO: {nome_file}",
         f"Eseguita il: {datetime.now():%Y-%m-%d %H:%M:%S}", LINEA]
    if r.errore:
        L += [f"ERRORE: {r.errore}", ""]
    else:
        L += [f"Strategia scelta:      {r.strategia}   (passate OCR eseguite: {r.n_passate})",
              f"Confidenza OCR media:  {r.conf_media:.2f}",
              f"Articoli estratti:     {len(r.articoli)}", "",
              "ARTICOLI ESTRATTI  (nome | quantita unita | confezioni | confidenza OCR)",
              "-" * 78]
        for i, a in enumerate(r.articoli, 1):
            flag = "  <-- bassa confidenza" if a.conf_ocr < 0.5 else ""
            L.append(f"{i:02d}. {a.nome:<30} | {a.qta:g} {a.unita:<3} | x{a.confezioni} | "
                     f"{a.conf_ocr:.2f}{flag}")
        L += ["", "TUPLE (formato ItemSpesa)", "-" * 78]
        L += [f"  {t}" for t in r.tuple_items]
        L += ["", "RIGHE OCR GREZZE  (>> = usata come spesa, .. = scartata/fine scontrino)",
              "-" * 78]
        L += [f" {'>>' if uso else '..'} {c:.2f} | {t}" for t, c, uso in r.righe_ocr]

    L += ["", "TEMPI PER FASE", "-" * 78] + tabella_tempi(r.tempi, wall)

    metriche = None
    if ground_truth and not r.errore:
        righe_cmp, metriche = confronta(ground_truth, r.tuple_items, soglia_nome)
        L += ["", "CONFRONTO CON GROUND TRUTH", "-" * 78] + righe_cmp
    L.append("")
    return "\n".join(L), metriche


# =====================================================================
# COLLAUDO SU CARTELLA
# =====================================================================
def elenca_immagini(cartella: Path) -> List[Path]:
    return sorted((p for p in cartella.iterdir()
                   if p.is_file() and p.suffix.lower() in ls.ESTENSIONI),
                  key=lambda p: p.name.lower())


def esegui_cartella(cartella: Path, out: Path, profilo: ls.Profilo = ls.PROFILO_DEFAULT,
                    catalogo: Sequence[str] = (), min_f1: float = 0.0,
                    soglia_nome: float = 0.60) -> int:
    if not cartella.is_dir():
        print(f"ERRORE: cartella non trovata: {cartella}\n"
              f"Creala accanto a questo file e inseriscici gli scontrini.")
        return 2
    immagini = elenca_immagini(cartella)
    if not immagini:
        print(f"ERRORE: nessuna immagine ({', '.join(sorted(ls.ESTENSIONI))}) in {cartella}")
        return 2
    out.mkdir(parents=True, exist_ok=True)

    print(f"Cartella: {cartella}  |  Immagini: {len(immagini)}  |  Output: {out}")
    print("Caricamento modello OCR...")
    t_modello = ls.carica_modello()
    print(f"Modello caricato in {t_modello:.2f}s\n")

    righe_riepilogo = []     # (nome, esito, n_art, conf, passate, wall, t_ocr, f1)
    somme_fasi: Dict[str, float] = {}
    n_ok = 0
    f1_sotto_soglia = False
    t_totale0 = time.perf_counter()

    for k, img in enumerate(immagini, 1):
        t0 = time.perf_counter()
        try:
            r = ls.leggi_scontrino(str(img), profilo, catalogo)
        except Exception:                                   # un errore non ferma il collaudo
            r = ls.RisultatoScontrino(str(img), errore="Eccezione:\n" + traceback.format_exc())
        wall = time.perf_counter() - t0

        testo, met = costruisci_report(img.name, r, wall, carica_ground_truth(img), soglia_nome)
        (out / f"{img.stem}_lettura.txt").write_text(testo, encoding="utf-8")

        if r.errore:
            print(f"[{k}/{len(immagini)}] {img.name:<40} ERRORE ({r.errore.splitlines()[0]})")
            righe_riepilogo.append((img.name, "ERRORE", 0, 0.0, 0, wall, 0.0, None))
            continue
        n_ok += 1
        for fase, v in r.tempi.items():
            somme_fasi[fase] = somme_fasi.get(fase, 0.0) + v
        f1 = met["f1"] if met else None
        if f1 is not None and f1 < min_f1:
            f1_sotto_soglia = True
        extra = f"  F1 {f1 * 100:.0f}%" if f1 is not None else ""
        print(f"[{k}/{len(immagini)}] {img.name:<40} {len(r.articoli):>3} art  "
              f"conf {r.conf_media:.2f}  {wall:6.2f}s  (ocr {r.tempi.get('ocr', 0):.2f}s){extra}")
        righe_riepilogo.append((img.name, "ok", len(r.articoli), r.conf_media, r.n_passate,
                                wall, r.tempi.get("ocr", 0.0), f1))

    t_totale = time.perf_counter() - t_totale0

    # ---- riepilogo ----
    R = [LINEA, "RIEPILOGO COLLAUDO", f"Eseguito il: {datetime.now():%Y-%m-%d %H:%M:%S}", LINEA,
         f"Cartella:               {cartella}",
         f"Scontrini letti:        {n_ok}/{len(immagini)}",
         f"Caricamento modello:    {t_modello:.3f} s (una sola volta)",
         f"Tempo totale lettura:   {t_totale:.3f} s", ""]
    R += [f"{'file':<38}{'esito':<8}{'art':>4}{'conf':>6}{'pass':>5}{'wall s':>9}{'ocr s':>8}{'F1':>7}",
          "-" * 85]
    for nome, esito, n, conf, pas, wall, t_ocr, f1 in righe_riepilogo:
        f1s = f"{f1 * 100:.0f}%" if f1 is not None else "-"
        R.append(f"{nome[:37]:<38}{esito:<8}{n:>4}{conf:>6.2f}{pas:>5}{wall:>9.2f}{t_ocr:>8.2f}{f1s:>7}")
    if n_ok:
        medie = {f: v / n_ok for f, v in somme_fasi.items()}
        R += ["", "TEMPO MEDIO PER FASE (per scontrino)", "-" * 85]
        R += tabella_tempi(medie)
    (out / "RIEPILOGO_tempi.txt").write_text("\n".join(R) + "\n", encoding="utf-8")

    print(f"\nFatto: {n_ok}/{len(immagini)} letti in {t_totale:.1f}s. Risultati in: {out}")
    return 1 if (f1_sotto_soglia or n_ok < len(immagini)) else 0


# =====================================================================
# UNIT TEST (pytest) - nessun modello OCR necessario
# =====================================================================
def _righe(*testi):
    return [Riga(i * 40, t, 0.9) for i, t in enumerate(testi)]


def _box(x, y, w=100, h=30):
    return [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]


def test_righe_ordinate_per_x_e_y():
    ocr = [(_box(300, 105), "NATURALE", 0.9), (_box(10, 100), "TOFU BIO", 0.9),
           (_box(10, 200), "UOVA", 0.8)]
    assert [x.testo for x in ls.ricostruisci_righe(ocr)] == ["TOFU BIO NATURALE", "UOVA"]


def test_righe_vuote():
    assert ls.ricostruisci_righe([]) == []


def test_fine_spesa_esatta_e_fuzzy():
    assert ls.is_fine_spesa("SUBTOTALE")
    assert ls.is_fine_spesa("SUBT0TALE 45")
    assert ls.is_fine_spesa("TOTALE COMPLESSIVO")
    assert not ls.is_fine_spesa("YOGURT GRECO")


def test_articolo_semplice():
    assert ls.parsa_righe(_righe("LENTICCHIE"))[0].tupla() == ("LENTICCHIE", 1.0, "pz", 1)


def test_riga_cad_non_prende_il_prezzo():
    assert ls.parsa_righe(_righe("TOFU BIO NATURALE", "2 Cad x 1,99"))[0].confezioni == 2


def test_peso_su_riga_dettaglio():
    a = ls.parsa_righe(_righe("BANANE", "0,838 kg x 1,99 EUR/kg"))
    assert a[0].tupla() == ("BANANE", 0.838, "kg", 1)


def test_nome_con_x_non_e_dettaglio():
    assert len(ls.parsa_righe(_righe("LENTICCHIE", "RISO EXTRA 1,0 kg"))) == 2


def test_dettaglio_senza_articolo_ignorato():
    assert ls.parsa_righe(_righe("2 Cad")) == []


def test_peso_inline_e_unita_normalizzata():
    a = ls.parsa_righe(_righe("FARINA 500 gr"))
    assert (a[0].nome, a[0].qta, a[0].unita) == ("FARINA", 500.0, "g")


def test_rimozione_iva_e_prezzo():
    assert ls.parsa_righe(_righe("UOVA ALL.TERRA 1,79 4"))[0].nome == "UOVA ALL.TERRA"


def test_rumore_scartato():
    assert ls.parsa_righe(_righe("- . -", "*", "12")) == []


def test_catalogo_corregge_ocr():
    cat = ["YOGURT GRECO 0%", "FIOCCHI D'AVENA"]
    assert ls.correggi_con_catalogo("YOGURT GRECO O%", cat, 85) == "YOGURT GRECO 0%"
    assert ls.correggi_con_catalogo("PERE", cat, 85) == "PERE"


def test_deskew_recupera_inclinazione():
    img = np.full((600, 800), 255, np.uint8)
    for y in range(80, 540, 40):
        cv2.putText(img, "LENTICCHIE BIO 1 PZ " * 2, (20, y), cv2.FONT_HERSHEY_SIMPLEX, 0.9, 0, 2)
    M = cv2.getRotationMatrix2D((400, 300), 3.0, 1.0)
    storta = cv2.warpAffine(img, M, (800, 600), borderValue=255)
    assert -4.0 <= ls._stima_inclinazione(storta) <= -2.0   # ruotata di +3: correzione -3
    assert ls._stima_inclinazione(img) == 0.0


def test_abbinamento_uno_a_uno():
    attesi = [("TOFU BIO", 1.0, "pz", 1), ("TOFU BIO NATURALE", 1.0, "pz", 2)]
    estratti = [("TOFU BIO NATURALE", 1.0, "pz", 2)]
    ab = abbina(attesi, estratti)
    assert len(ab) == 1 and 1 in ab          # l'unico estratto non può coprire due attesi


def _scrivi_png(percorso: Path):
    cv2.imencode(".png", np.full((1000, 1000), 255, np.uint8))[1].tofile(str(percorso))


_OCR_FINTO = [(_box(10, 100), "LENTICCHIE", 0.95), (_box(10, 150), "TOFU BIO", 0.9),
              (_box(10, 190), "2 Cad", 0.9), (_box(10, 250), "BANANE", 0.9),
              (_box(10, 290), "0,838 kg x 1,99", 0.9), (_box(10, 350), "SUBTOTALE", 0.9),
              (_box(10, 400), "NON DEVE APPARIRE", 0.9)]


def _ocr_finto(img, profilo, tempi=None):
    with ls._fase(tempi, "ocr"):
        return _OCR_FINTO


def test_pipeline_con_ocr_finto(monkeypatch, tmp_path):
    monkeypatch.setattr(ls, "_esegui_ocr", _ocr_finto)
    p = tmp_path / "scontrino.png"
    _scrivi_png(p)
    r = ls.leggi_scontrino(str(p), debug_dir=str(tmp_path / "dbg"))
    assert r.tuple_items == [("LENTICCHIE", 1.0, "pz", 1), ("TOFU BIO", 1.0, "pz", 2),
                             ("BANANE", 0.838, "kg", 1)]
    assert r.strategia == "clahe" and r.n_passate == 1
    for fase in ("decodifica", "ritaglio_scala", "raddrizzamento", "variante", "ocr",
                 "righe", "parsing", "salvataggio_debug"):
        assert fase in r.tempi and r.tempi[fase] >= 0, fase
    assert abs(r.secondi - sum(r.tempi.values())) < 1e-9
    assert [uso for _, _, uso in r.righe_ocr] == [True] * 5 + [False] * 2
    assert list((tmp_path / "dbg").glob("*.jpg"))


def test_seconda_passata_se_confidenza_bassa(monkeypatch, tmp_path):
    bassa = [(b, t, 0.2) for b, t, _ in _OCR_FINTO]
    monkeypatch.setattr(ls, "_esegui_ocr", lambda img, pr, tempi=None: bassa)
    p = tmp_path / "s.png"
    _scrivi_png(p)
    assert ls.leggi_scontrino(str(p)).n_passate == 2


def test_file_inesistente():
    assert ls.estrai_tuple_da_immagine("non_esiste.jpg") == []


def test_esecuzione_cartella_con_ocr_finto(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(ls, "_esegui_ocr", _ocr_finto)
    monkeypatch.setattr(ls, "carica_modello", lambda: 0.0)
    cartella, out = tmp_path / "scontrini", tmp_path / "out"
    cartella.mkdir()
    _scrivi_png(cartella / "a_scontrino.png")
    _scrivi_png(cartella / "b_scontrino.png")
    (cartella / "b_scontrino_truth.json").write_text(json.dumps(
        [["LENTICCHIE", 1, "pz", 1], ["TOFU BIO", 1, "pz", 2], ["BANANE", 0.838, "kg", 1]]))
    (cartella / "note.txt").write_text("ignorato")

    assert esegui_cartella(cartella, out, min_f1=0.9) == 0
    a = (out / "a_scontrino_lettura.txt").read_text(encoding="utf-8")
    b = (out / "b_scontrino_lettura.txt").read_text(encoding="utf-8")
    assert "LENTICCHIE" in a and "TEMPI PER FASE" in a and "GROUND TRUTH" not in a
    assert "GROUND TRUTH" in b and "F1:                         100.0%" in b
    riepilogo = (out / "RIEPILOGO_tempi.txt").read_text(encoding="utf-8")
    assert "Scontrini letti:        2/2" in riepilogo
    assert not (out / "note_lettura.txt").exists()


def test_cartella_mancante(tmp_path):
    assert esegui_cartella(tmp_path / "non_esiste", tmp_path / "out") == 2


# =====================================================================
# ENTRY POINT
# =====================================================================
def main() -> int:
    ap = argparse.ArgumentParser(description="Collaudo di recepiet_reader.py")
    ap.add_argument("--unit", action="store_true", help="esegue solo gli unit test (pytest)")
    ap.add_argument("--cartella", type=Path, default=CARTELLA_DEFAULT)
    ap.add_argument("--out", type=Path, default=OUT_DEFAULT)
    ap.add_argument("--catalogo", default=None)
    ap.add_argument("--soglia", type=float, default=0.60, help="similarità minima del nome")
    ap.add_argument("--min-f1", type=float, default=0.0)
    ap.add_argument("--taglio-alto", type=float, default=ls.PROFILO_DEFAULT.taglio_alto)
    ap.add_argument("--taglio-destra", type=float, default=ls.PROFILO_DEFAULT.taglio_destra)
    ap.add_argument("--no-raddrizza", action="store_true")
    a = ap.parse_args()

    if a.unit:
        import pytest
        return int(pytest.main(["-v", __file__]))

    profilo = replace(ls.PROFILO_DEFAULT, taglio_alto=a.taglio_alto,
                      taglio_destra=a.taglio_destra, raddrizza=not a.no_raddrizza)
    return esegui_cartella(a.cartella, a.out, profilo, ls.carica_catalogo(a.catalogo),
                           a.min_f1, a.soglia)


if __name__ == "__main__":
    sys.exit(main())
