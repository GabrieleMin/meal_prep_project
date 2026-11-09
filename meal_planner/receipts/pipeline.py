"""
meal_planner.receipts.pipeline - revisione manuale scontrino -> out.txt
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from meal_planner.receipts import reader as ls

UNITA_COMUNI = ("pz", "kg", "g", "l", "ml", "cl")
SOGLIA_BASSA_CONFIDENZA = 0.5
COLORE_ATTENZIONE = "#f5a524"      # testo dei nomi letti con bassa confidenza (leggibile su tema chiaro e scuro)
ROSSO, VERDE = "#e5484d", "#30a46c"  # stessi colori di meal_planner.gui.support (errore / ok)


# =====================================================================
# LOGICA (senza gui: testabile ovunque)
# =====================================================================
@dataclass
class Voce:
    """una riga della finestra, con i campi come testo (cosi' come li vede l'utente)."""
    nome: str = ""
    qta: str = "1"
    unita: str = "pz"
    confezioni: str = "1"
    conf_ocr: float = 1.0


def leggi_voci(percorso: str, profilo: ls.Profilo = ls.PROFILO_DEFAULT,
               catalogo: Sequence[str] = ()) -> List[Voce]:
    """legge lo scontrino e propone tutto in minuscolo."""
    r = ls.leggi_scontrino(percorso, profilo, catalogo)
    if r.errore:
        raise RuntimeError(r.errore.lower())
    return [Voce(a.nome.lower(), f"{a.qta:g}", a.unita.lower(), str(a.confezioni), a.conf_ocr)
            for a in r.articoli]


def valida_voci(voci: Sequence[Voce]) -> Tuple[List[ls.ItemSpesa], List[str]]:
    """converte le voci in tuple (nome, qta, unita, confezioni). le righe con nome vuoto
    vengono ignorate. ritorna (tuple valide, messaggi di errore in minuscolo)."""
    import math

    if voci is None:
        return [], []

    items: List[ls.ItemSpesa] = []
    errori: List[str] = []
    for i, v in enumerate(voci, 1):
        if v is None:
            continue
        raw_name = str(v.nome) if v.nome is not None else ""
        nome = " ".join(raw_name.lower().replace(";", " ").split())
        if not nome:
            continue
        try:
            raw_qta = str(v.qta) if v.qta is not None else ""
            qta = float(raw_qta.strip().replace(",", "."))
            if not math.isfinite(qta) or qta <= 0:
                raise ValueError
        except (ValueError, TypeError):
            errori.append(f"row {i} ({nome}): invalid quantity")
            continue
        raw_unita = str(v.unita) if v.unita is not None else ""
        unita = raw_unita.strip().lower().replace(";", "")
        if not unita:
            errori.append(f"row {i} ({nome}): missing unit")
            continue
        try:
            raw_conf = str(v.confezioni) if v.confezioni is not None else ""
            conf_f = float(raw_conf.strip().replace(",", "."))
            if not math.isfinite(conf_f) or not conf_f.is_integer() or conf_f < 1:
                raise ValueError
            conf = int(conf_f)
        except (ValueError, TypeError, OverflowError):
            errori.append(f"row {i} ({nome}): invalid packages (integer >= 1)")
            continue
        items.append((nome, qta, unita, conf))
    return items, errori


def scrivi_out(items: Sequence[ls.ItemSpesa], percorso: Path) -> None:
    testo = "".join(f"{n.lower()};{q:g};{u.lower()};{c}\n" for n, q, u, c in items)
    Path(percorso).write_text(testo, encoding="utf-8")


# =====================================================================
# FINESTRA (tkinter)
# =====================================================================
def applica_tema_app(root) -> str:
    """stesso tema (sv-ttk, chiaro/scuro da config.json) dell'app principale meal_planner."""
    try:
        from meal_planner.gui import theme
        nome = theme.load()
        theme.apply(nome)
        return nome
    except Exception:                   # sv-ttk assente: resta il tema ttk di default
        return "light"


class FinestraModifica:
    """tabella editabile degli articoli. tutti i testi sono in minuscolo."""

    def __init__(self, root, voci: Sequence[Voce], percorso_out: Path, titolo: str,
                 on_save: Optional[Callable[[List[ls.ItemSpesa]], None]] = None):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk = tk, ttk
        self.root = root
        self.percorso_out = Path(percorso_out)
        self.on_save = on_save
        self.righe: List[dict] = []

        root.title(titolo.lower())
        root.geometry("820x560")
        root.minsize(640, 360)
        self.tema = applica_tema_app(root)
        ttk.Style(root).configure("Attenzione.TEntry", foreground=COLORE_ATTENZIONE)

        intestazione = ttk.Frame(root, padding=(14, 12, 14, 0))
        intestazione.pack(fill="x")
        ttk.Label(intestazione, text=titolo.lower(), anchor="w",
                  font=("TkDefaultFont", 11, "bold")).pack(fill="x")
        ttk.Label(intestazione, anchor="w", justify="left", wraplength=780,
                  text="Check and correct the fields below. An orange name marks an uncertain "
                       "reading. Rows with an empty name are ignored.").pack(fill="x", pady=(2, 6))

        # ---- area scorrevole ----
        zona = ttk.Frame(root, style="Card.TFrame", padding=6)
        zona.pack(fill="both", expand=True, padx=12, pady=4)
        self.canvas = tk.Canvas(zona, highlightthickness=0, background=self._sfondo())
        barra = ttk.Scrollbar(zona, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=barra.set)
        barra.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.interno = ttk.Frame(self.canvas)
        self.canvas.create_window((0, 0), window=self.interno, anchor="nw")
        self.interno.bind("<Configure>", lambda e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        def _scroll_canvas(d):
            try:
                if self.canvas.winfo_exists():
                    self.canvas.yview_scroll(d, "units")
            except Exception:
                pass

        for w in (self.canvas, self.interno):
            for evento, delta in (("<Button-4>", -1), ("<Button-5>", 1)):
                w.bind(evento, lambda e, d=delta: _scroll_canvas(d), add="+")
            w.bind("<MouseWheel>", lambda e: _scroll_canvas(-1 if e.delta > 0 else 1), add="+")

        def _cleanup_scroll(event=None):
            if event is None or event.widget == root:
                for ev in ("<Button-4>", "<Button-5>", "<MouseWheel>"):
                    try:
                        root.unbind_all(ev)
                    except Exception:
                        pass

        root.bind("<Destroy>", _cleanup_scroll, add="+")

        for col, (testo, larg) in enumerate((("Item Name", 38), ("Quantity", 9), ("Unit", 7),
                                             ("Packages", 10))):
            ttk.Label(self.interno, text=testo, anchor="w", font=("TkDefaultFont", 9, "bold")).grid(
                row=0, column=col, sticky="w", padx=4, pady=(2, 6))

        # ---- barra inferiore ----
        self.stato = tk.StringVar(value="")
        self.etichetta_stato = ttk.Label(root, textvariable=self.stato, anchor="w", padding=(14, 6))
        self.etichetta_stato.pack(fill="x", padx=6, pady=(4, 0))
        barra_bottoni = ttk.Frame(root, padding=(14, 10))
        barra_bottoni.pack(fill="x", side="bottom")
        ttk.Button(barra_bottoni, text="+ Add Item", command=self.aggiungi_riga).pack(side="left")
        ttk.Button(barra_bottoni, text="Cancel", command=root.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(barra_bottoni, text="Save & Reconcile", style="Accent.TButton", command=self.salva, width=16).pack(side="right")
        root.bind("<Control-s>", lambda e: self.salva())

        for v in voci:
            self.aggiungi_riga(v)
        if not voci:
            self.aggiungi_riga()
            self._messaggio("no items read: you can enter them by hand", errore=False)

    # ---- helpers ----
    def _forza_minuscolo(self, var, widget) -> None:
        def cb(*_):
            valore = var.get()
            if valore != valore.lower():
                pos = widget.index("insert")
                var.set(valore.lower())
                widget.icursor(pos)
        var.trace_add("write", cb)

    def _sfondo(self) -> str:
        """sfondo del canvas = sfondo del tema (il canvas non e' un widget ttk)."""
        return self.ttk.Style(self.root).lookup("TFrame", "background") or (
            "#1c1c1c" if self.tema == "dark" else "#fafafa")

    def _messaggio(self, testo: str, errore: bool) -> None:
        self.stato.set(testo.lower())
        self.etichetta_stato.configure(foreground=ROSSO if errore else VERDE)

    # ---- righe ----
    def aggiungi_riga(self, voce: Optional[Voce] = None) -> None:
        tk, ttk = self.tk, self.ttk
        manuale = voce is None            # riga aggiunta dall'utente: scorre in fondo
        voce = voce or Voce()
        r = self.interno.grid_size()[1]   # prossima riga libera (0 = intestazione)
        vars_ = {k: tk.StringVar(value=getattr(voce, k).lower())
                 for k in ("nome", "qta", "unita", "confezioni")}
        sospetta = voce.conf_ocr < SOGLIA_BASSA_CONFIDENZA

        e_nome = ttk.Entry(self.interno, textvariable=vars_["nome"], width=38)
        if sospetta:
            e_nome.configure(style="Attenzione.TEntry")
        e_qta = ttk.Entry(self.interno, textvariable=vars_["qta"], width=9)
        e_unita = ttk.Combobox(self.interno, textvariable=vars_["unita"], width=6,
                               values=UNITA_COMUNI)
        e_conf = ttk.Entry(self.interno, textvariable=vars_["confezioni"], width=10)
        for col, w in enumerate((e_nome, e_qta, e_unita, e_conf)):
            w.grid(row=r, column=col, padx=3, pady=2, sticky="w")
        for chiave, w in zip(("nome", "qta", "unita", "confezioni"), (e_nome, e_qta, e_unita, e_conf)):
            self._forza_minuscolo(vars_[chiave], w)

        riga = {"vars": vars_, "conf_ocr": voce.conf_ocr, "widgets": [e_nome, e_qta, e_unita, e_conf]}
        btn = ttk.Button(self.interno, text="✕", style="Toolbutton", width=2,
                        command=lambda: self.rimuovi_riga(riga))
        btn.grid(row=r, column=4, padx=3)
        riga["widgets"].append(btn)
        self.righe.append(riga)
        self.root.update_idletasks()
        if manuale:
            self.canvas.yview_moveto(1.0)

    def rimuovi_riga(self, riga: dict) -> None:
        for w in riga["widgets"]:
            w.destroy()
        self.righe.remove(riga)

    def raccogli(self) -> List[Voce]:
        return [Voce(r["vars"]["nome"].get(), r["vars"]["qta"].get(), r["vars"]["unita"].get(),
                     r["vars"]["confezioni"].get(), r["conf_ocr"]) for r in self.righe]

    def salva(self) -> bool:
        items, errori = valida_voci(self.raccogli())
        if errori:
            self._messaggio("not saved. " + " | ".join(errori), errore=True)
            return False
        try:
            scrivi_out(items, self.percorso_out)
        except OSError as e:
            self._messaggio(f"not saved: {e}", errore=True)
            return False
        self._messaggio(f"saved: {len(items)} rows in {self.percorso_out.name}", errore=False)
        if self.on_save:
            self.on_save(items)
        try:
            self.root.destroy()
        except Exception:
            pass
        return True


def apri_finestra(voci: Sequence[Voce], percorso_out: Path, titolo: str,
                  on_save: Optional[Callable[[List[ls.ItemSpesa]], None]] = None) -> None:
    import tkinter as tk
    root = tk.Tk()
    FinestraModifica(root, voci, percorso_out, titolo, on_save=on_save)
    root.mainloop()


# =====================================================================
# ENTRY POINT
# =====================================================================
def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="legge uno scontrino, lo fa rivedere e salva out.txt")
    ap.add_argument("scontrino", help="percorso dell'immagine dello scontrino")
    ap.add_argument("--out", default="out.txt", help="file di output (default: out.txt)")
    ap.add_argument("--catalogo", default=None)
    ap.add_argument("--taglio-alto", type=float, default=ls.PROFILO_DEFAULT.taglio_alto)
    ap.add_argument("--taglio-destra", type=float, default=ls.PROFILO_DEFAULT.taglio_destra)
    ap.add_argument("--senza-finestra", action="store_true",
                    help="salva subito il risultato dell'ocr, senza aprire la finestra")
    a = ap.parse_args(argv)

    if not Path(a.scontrino).is_file():
        print(f"errore: file non trovato: {a.scontrino}".lower(), file=sys.stderr)
        return 2

    profilo = replace(ls.PROFILO_DEFAULT, taglio_alto=a.taglio_alto, taglio_destra=a.taglio_destra)
    print("lettura in corso (al primo avvio il caricamento del modello richiede qualche secondo)...")
    try:
        voci = leggi_voci(a.scontrino, profilo, ls.carica_catalogo(a.catalogo))
    except RuntimeError as e:
        print(f"errore: {e}", file=sys.stderr)
        return 2
    print(f"trovati {len(voci)} articoli")

    if a.senza_finestra:
        items, errori = valida_voci(voci)
        for e in errori:
            print(f"attenzione: {e}", file=sys.stderr)
        scrivi_out(items, Path(a.out))
        print(f"salvato: {len(items)} righe in {a.out}")
        return 1 if errori else 0

    apri_finestra(voci, Path(a.out), f"receipt - {Path(a.scontrino).name}")
    return 0


# =====================================================================
# TEST (pytest) - nessun modello ocr necessario
# =====================================================================
def _box(x, y, w=100, h=30):
    return [[x, y], [x + w, y], [x + w, y + h], [x, y + h]]


_OCR_FINTO = [(_box(10, 100), "LENTICCHIE", 0.95), (_box(10, 150), "TOFU BIO", 0.3),
              (_box(10, 190), "2 Cad", 0.9), (_box(10, 250), "BANANE", 0.9),
              (_box(10, 290), "0,838 kg x 1,99", 0.9), (_box(10, 350), "SUBTOTALE", 0.9)]


def _immagine_vuota(percorso: Path) -> None:
    import cv2
    import numpy as np
    cv2.imencode(".png", np.full((1000, 1000), 255, np.uint8))[1].tofile(str(percorso))


def test_leggi_voci_tutto_minuscolo(monkeypatch, tmp_path):
    monkeypatch.setattr(ls, "_esegui_ocr", lambda img, pr, tempi=None: _OCR_FINTO)
    p = tmp_path / "s.png"
    _immagine_vuota(p)
    voci = leggi_voci(str(p))
    assert [(v.nome, v.qta, v.unita, v.confezioni) for v in voci] == [
        ("lenticchie", "1", "pz", "1"), ("tofu bio", "1", "pz", "2"), ("banane", "0.838", "kg", "1")]
    assert all(v.nome == v.nome.lower() for v in voci)
    assert voci[1].conf_ocr < SOGLIA_BASSA_CONFIDENZA


def test_leggi_voci_file_inesistente():
    import pytest
    with pytest.raises(RuntimeError):
        leggi_voci("non_esiste.jpg")


def test_validazione_ok_virgola_e_maiuscole():
    items, errori = valida_voci([Voce("  LATTE   Intero ", "1,5", "L", "2"), Voce("", "x", "", "")])
    assert errori == []
    assert items == [("latte intero", 1.5, "l", 2)]       # riga vuota ignorata


def test_validazione_errori():
    _, errori = valida_voci([Voce("a", "abc"), Voce("b", "0"), Voce("c", "1", ""),
                             Voce("d", "1", "pz", "0"), Voce("e", "1", "pz", "x")])
    assert len(errori) == 5 and all(e == e.lower() for e in errori)


def test_scrittura_out_senza_maiuscole(tmp_path):
    out = tmp_path / "out.txt"
    scrivi_out([("Banane; Bio", 0.838, "KG", 1), ("uova", 6.0, "pz", 2)], out)
    testo = out.read_text(encoding="utf-8")
    assert testo == testo.lower()
    assert testo.splitlines()[1] == "uova;6;pz;2"


def test_scrittura_out_separatore_nel_nome_sostituito(tmp_path):
    items, _ = valida_voci([Voce("banane; bio", "1")])
    scrivi_out(items, tmp_path / "o.txt")
    assert (tmp_path / "o.txt").read_text() == "banane bio;1;pz;1\n"


def test_main_senza_finestra(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(ls, "_esegui_ocr", lambda img, pr, tempi=None: _OCR_FINTO)
    p, out = tmp_path / "s.png", tmp_path / "out.txt"
    _immagine_vuota(p)
    assert main([str(p), "--out", str(out), "--senza-finestra"]) == 0
    assert out.read_text(encoding="utf-8") == "lenticchie;1;pz;1\ntofu bio;1;pz;2\nbanane;0.838;kg;1\n"


def test_main_file_mancante(capsys):
    assert main(["non_esiste.png"]) == 2


def _root_o_skip():
    import pytest
    tk = pytest.importorskip("tkinter")
    try:
        return tk.Tk()
    except tk.TclError:
        pytest.skip("nessun display disponibile")


def test_finestra_forza_minuscolo_e_salva(tmp_path):
    root = _root_o_skip()
    try:
        out = tmp_path / "out.txt"
        f = FinestraModifica(root, [Voce("pere", "1", "kg", "1", 0.2)], out, "Titolo Con Maiuscole")
        assert root.title() == "titolo con maiuscole"
        f.righe[0]["vars"]["nome"].set("PERE Abate")          # l'utente scrive maiuscole
        assert f.righe[0]["vars"]["nome"].get() == "pere abate"
        f.righe[0]["vars"]["qta"].set("0,5")

        # Test validazione fallita prima del salvataggio definitivo
        f.aggiungi_riga(Voce("riso", "boh"))                   # dato non valido: non salva
        assert f.salva() is False
        assert "invalid quantity" in f.stato.get()
        f.rimuovi_riga(f.righe[-1])

        assert f.salva() is True
        assert out.read_text() == "pere abate;0.5;kg;1\n"
        assert f.stato.get() == f.stato.get().lower()
    finally:
        try:
            root.destroy()
        except Exception:
            pass


def test_finestra_senza_articoli_ha_una_riga_vuota(tmp_path):
    root = _root_o_skip()
    try:
        f = FinestraModifica(root, [], tmp_path / "out.txt", "x")
        assert len(f.righe) == 1
        assert f.salva() is True and (tmp_path / "out.txt").read_text() == ""
    finally:
        try:
            root.destroy()
        except Exception:
            pass


def test_finestra_on_save_callback_e_chiusura(tmp_path):
    root = _root_o_skip()
    chiamate = []
    try:
        out = tmp_path / "out.txt"
        voci = [Voce("latte", "1,5", "l", "2")]
        f = FinestraModifica(root, voci, out, "test", on_save=lambda items: chiamate.append(items))

        # on_save is NOT called if validation fails
        f.aggiungi_riga(Voce("pane", "non-valido"))
        assert f.salva() is False
        assert len(chiamate) == 0
        try:
            ancora_viva = bool(root.winfo_exists())
        except Exception:
            ancora_viva = False
        assert ancora_viva

        # on_save receives list of items and window is destroyed on successful save
        f.rimuovi_riga(f.righe[-1])
        assert f.salva() is True
        assert chiamate == [[("latte", 1.5, "l", 2)]]
        assert out.read_text() == "latte;1.5;l;2\n"
        try:
            ancora_viva = bool(root.winfo_exists())
        except Exception:
            ancora_viva = False
        assert not ancora_viva
    finally:
        try:
            root.destroy()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
