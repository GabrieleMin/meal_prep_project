"""
gui.py - Meal Planner, interfaccia desktop (Tkinter, nessuna installazione extra).
Avvio:  python gui.py

Schede: Dispensa (disponibilita', aggiunta, consumo) | Spesa | Ricette | Piano | Profilo.
Chiama direttamente le funzioni del progetto: non serve il server ne' il browser.
"""

import contextlib
import io
import tkinter as tk
from datetime import date
from tkinter import filedialog, messagebox, simpledialog, ttk

import motore as m
import pianificatore as pf
import ricette as rc
from crea_RELSCHEMA import get_connection
from crea_RICETTE import migra
from operation import aggiungi_item, consuma_quantita, ottieni_inventario_disponibile, ProdottoNonTrovato

ROSSO, VERDE = "#b42318", "#067647"


@contextlib.contextmanager
def db():
    conn = get_connection()
    try:
        yield conn
    finally:
        conn.close()


def numero(testo, nome="valore"):
    try:
        return float(str(testo).replace(",", "."))
    except ValueError:
        raise ValueError(f"'{testo}' non e' un numero valido ({nome}).")


def sicuro(funzione):
    """Mostra l'errore all'utente invece di far chiudere la finestra."""
    def interno(self, *args):
        try:
            return funzione(self, *args)
        except Exception as e:
            self.msg(str(e), True)
            messagebox.showerror("Attenzione", str(e))
    return interno


class Tabella(ttk.Frame):
    def __init__(self, master, colonne, altezza=14):
        super().__init__(master)
        self.tv = ttk.Treeview(self, columns=[c for c, _, _ in colonne], show="headings", height=altezza)
        for chiave, titolo, larghezza in colonne:
            self.tv.heading(chiave, text=titolo)
            self.tv.column(chiave, width=larghezza, anchor="w")
        barra = ttk.Scrollbar(self, command=self.tv.yview)
        self.tv.configure(yscrollcommand=barra.set)
        self.tv.tag_configure("ko", foreground=ROSSO)
        self.tv.pack(side="left", fill="both", expand=True)
        barra.pack(side="right", fill="y")

    def riempi(self, righe):
        """righe = [(valori, tag), ...]"""
        self.tv.delete(*self.tv.get_children())
        for valori, tag in righe:
            self.tv.insert("", "end", values=valori, tags=(tag,) if tag else ())


class Form(ttk.LabelFrame):
    def __init__(self, master, titolo, campi, testo_bottone, comando):
        super().__init__(master, text=titolo, padding=10)
        self.v, self.w = {}, {}
        for r, (chiave, etichetta, valori) in enumerate(campi):
            ttk.Label(self, text=etichetta).grid(row=r, column=0, sticky="w", pady=3)
            self.v[chiave] = tk.StringVar()
            cls = ttk.Combobox if valori is not None else ttk.Entry
            self.w[chiave] = cls(self, textvariable=self.v[chiave], width=24,
                                 **({"values": valori} if valori is not None else {}))
            self.w[chiave].grid(row=r, column=1, padx=(8, 0), pady=3)
        ttk.Button(self, text=testo_bottone, command=comando).grid(
            row=len(campi), column=0, columnspan=2, sticky="ew", pady=(8, 0))

    def leggi(self):
        return {k: v.get().strip() for k, v in self.v.items()}

    def pulisci(self):
        for v in self.v.values():
            v.set("")


class DialogoProdotto(tk.Toplevel):
    """Chiede i dati di un prodotto non ancora in archivio."""

    def __init__(self, master, nome, id_suggerito):
        super().__init__(master)
        self.title("Prodotto nuovo")
        self.transient(master)
        self.risultato = None
        ttk.Label(self, wraplength=380, padding=10, text=(
            f"'{nome}' non e' ancora in archivio.\nIndica l'ID: se esiste gia' il nome diventa un alias "
            "di quel prodotto; se e' nuovo compila anche unita' e valori per 100 g.")).pack()
        self.form = Form(self, "", [("id", "ID prodotto", None), ("unita", "Unita'", ["kg", "L", "pz"]),
                                    ("kcal", "Kcal", None), ("prot", "Proteine (g)", None),
                                    ("carb", "Carboidrati (g)", None), ("gras", "Grassi (g)", None)],
                         "Conferma", self.conferma)
        self.form.pack(padx=10, pady=(0, 10))
        self.form.v["id"].set(id_suggerito)
        self.grab_set()
        self.wait_window()

    def conferma(self):
        v = self.form.leggi()
        try:
            r = {"id_fallback": int(v["id"])}
            if v["unita"]:
                r["unita_misura_nuovo_prodotto"] = v["unita"]
            if all(v[k] for k in ("kcal", "prot", "carb", "gras")):
                r["macro_nuovo_prodotto"] = {"Kcal_100g": numero(v["kcal"]), "Proteine_100g": numero(v["prot"]),
                                             "Carboidrati_100g": numero(v["carb"]), "Grassi_100g": numero(v["gras"])}
        except ValueError as e:
            messagebox.showerror("Dati non validi", str(e), parent=self)
            return
        self.risultato = r
        self.destroy()


class DialogoScelta(tk.Toplevel):
    """Chiede di scegliere un valore da un elenco."""

    def __init__(self, master, titolo, valori):
        super().__init__(master)
        self.title(titolo)
        self.transient(master)
        self.risultato = None
        self.v = tk.StringVar()
        ttk.Label(self, text=titolo, padding=10).pack()
        ttk.Combobox(self, textvariable=self.v, values=valori, state="readonly", width=34).pack(padx=10)
        ttk.Button(self, text="Conferma", command=self.conferma).pack(pady=10)
        self.grab_set()
        self.wait_window()

    def conferma(self):
        self.risultato = self.v.get() or None
        self.destroy()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Meal Planner")
        self.geometry("1140x700")
        self.minsize(920, 580)
        ttk.Style(self).theme_use("clam")
        ttk.Style(self).configure("Treeview", rowheight=24)
        with db() as c:
            migra(c)                                     # garantisce che lo schema esista
        self.piani, self.bozza, self.fissi = [], [], {}
        self.stato = tk.StringVar(value="Pronto.")
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=8, pady=(8, 0))
        for titolo, costruisci in (("Dispensa", self.tab_dispensa), ("Spesa", self.tab_spesa),
                                   ("Ricette", self.tab_ricette), ("Piano", self.tab_piano),
                                   ("Profilo", self.tab_profilo)):
            scheda = ttk.Frame(self.nb, padding=10)
            self.nb.add(scheda, text=titolo)
            costruisci(scheda)
        self.barra = ttk.Label(self, textvariable=self.stato, anchor="w", padding=6)
        self.barra.pack(fill="x")
        self.nb.bind("<<NotebookTabChanged>>", lambda e: self.aggiorna())
        self.aggiorna()

    def msg(self, testo, errore=False):
        self.stato.set(testo)
        self.barra.configure(foreground=ROSSO if errore else VERDE)

    # ---------------- schede ----------------

    def tab_dispensa(self, f):
        f.columnconfigure(0, weight=1)
        f.rowconfigure(0, weight=1)
        self.t_disp = Tabella(f, [("n", "Prodotto", 240), ("q", "Disponibile", 100), ("u", "Unita'", 60),
                                  ("l", "Luogo", 120)], 18)
        self.t_disp.grid(row=0, column=0, sticky="nsew")
        lato = ttk.Frame(f)
        lato.grid(row=0, column=1, sticky="n", padx=(12, 0))
        luoghi = ["dispensa", "frigo", "freezer"]
        self.f_add = Form(lato, "Aggiungi all'inventario",
                          [("nome", "Prodotto", []), ("peso", "Peso/volume confezione", None),
                           ("num", "N. confezioni", None), ("luogo", "Luogo", luoghi)], "Aggiungi", self.aggiungi)
        self.f_use = Form(lato, "Consuma", [("nome", "Prodotto", []), ("q", "Quantita'", None)],
                          "Consuma", self.consuma)
        self.f_add.pack(fill="x")
        self.f_use.pack(fill="x", pady=(10, 0))

    def tab_spesa(self, f):
        ttk.Label(f, text="Cosa manca: consumi oltre la disponibilita' e ingredienti dei piani.").pack(anchor="w")
        self.t_spesa = Tabella(f, [("n", "Prodotto", 320), ("q", "Quantita' mancante", 140), ("u", "Unita'", 80)], 20)
        self.t_spesa.pack(fill="both", expand=True, pady=8)

    def tab_ricette(self, f):
        f.columnconfigure(0, weight=1)
        f.rowconfigure(1, weight=1)
        ttk.Label(f, text="Macro per porzione e fattibilita' con l'inventario attuale "
                          "('Aperto' = quota presa da confezioni gia' aperte).").grid(row=0, column=0, sticky="w")
        self.t_ric = Tabella(f, [("r", "Ricetta", 200), ("k", "kcal", 50), ("p", "Prot", 50), ("c", "Carb", 50),
                                 ("g", "Grassi", 50), ("fi", "Fibre", 50), ("a", "Aperto", 55),
                                 ("e", "Esito", 220)], 18)
        self.t_ric.grid(row=1, column=0, sticky="nsew", pady=8)
        barra = ttk.Frame(f)
        barra.grid(row=2, column=0, sticky="w")
        ttk.Button(barra, text="Importa da file (.json / .csv)...", command=self.importa_ricette).pack(side="left")
        ttk.Button(barra, text="Elimina ricetta selezionata", command=self.elimina_ricetta).pack(side="left", padx=6)

        nuova = ttk.LabelFrame(f, text="Nuova ricetta (quantita' per UNA porzione)", padding=10)
        nuova.grid(row=0, column=1, rowspan=3, sticky="n", padx=(12, 0))
        self.v_rnome = tk.StringVar()
        ttk.Label(nuova, text="Nome").grid(row=0, column=0, sticky="w")
        ttk.Entry(nuova, textvariable=self.v_rnome, width=30).grid(row=0, column=1, sticky="ew", padx=(8, 0))
        with db() as c:
            self.v_pasti = {n: tk.BooleanVar() for (n,) in c.execute("SELECT Nome FROM PASTI ORDER BY Ordine")}
        caselle = ttk.Frame(nuova)
        caselle.grid(row=1, column=0, columnspan=2, sticky="w", pady=4)
        ttk.Label(caselle, text="Pasti:").pack(side="left", padx=(0, 6))
        for nome, var in self.v_pasti.items():
            ttk.Checkbutton(caselle, text=nome, variable=var).pack(side="left", padx=(0, 6))
        self.f_ing = Form(nuova, "Ingrediente", [("p", "Prodotto o gruppo", []), ("q", "Quantita'", None),
                                                 ("u", "Unita'", list(rc.UNITA))],
                          "Aggiungi ingrediente", self.aggiungi_ingrediente)
        self.f_ing.grid(row=2, column=0, columnspan=2, sticky="ew")
        self.t_bozza = Tabella(nuova, [("p", "Ingredienti della ricetta", 160), ("q", "Quantita'", 70),
                                       ("u", "Unita'", 70)], 5)
        self.t_bozza.grid(row=3, column=0, columnspan=2, pady=6)
        ttk.Button(nuova, text="Rimuovi ingrediente selezionato",
                   command=self.rimuovi_ingrediente).grid(row=4, column=0, columnspan=2, sticky="ew")
        ttk.Button(nuova, text="Salva ricetta", command=self.salva_ricetta).grid(
            row=5, column=0, columnspan=2, sticky="ew", pady=(6, 0))

    def tab_piano(self, f):
        alto = ttk.Frame(f)
        alto.pack(fill="x")
        self.v_data, self.v_giorni, self.v_kcal = (tk.StringVar(value=date.today().isoformat()),
                                                   tk.StringVar(value="7"), tk.StringVar())
        for etichetta, var, larghezza in (("Data inizio", self.v_data, 12), ("Giorni", self.v_giorni, 4),
                                          ("Kcal (vuoto = profilo)", self.v_kcal, 8)):
            ttk.Label(alto, text=etichetta).pack(side="left", padx=(0, 4))
            ttk.Entry(alto, textvariable=var, width=larghezza).pack(side="left", padx=(0, 12))
        for testo, comando in (("Proponi piano", self.proponi), ("Salva piano", self.salva),
                               ("Segna come cucinato", self.cucinato)):
            ttk.Button(alto, text=testo, command=comando).pack(side="left", padx=3)
        medio = ttk.Frame(f)
        medio.pack(fill="x", pady=(6, 0))
        ttk.Label(medio, text="Sul pasto selezionato:").pack(side="left", padx=(0, 6))
        for testo, comando in (("Blocca ricetta...", self.blocca), ("Salta pasto", self.salta),
                               ("Sblocca tutti", self.sblocca)):
            ttk.Button(medio, text=testo, command=comando).pack(side="left", padx=3)
        self.t_piano = Tabella(f, [("d", "Data", 95), ("p", "Pasto", 90), ("r", "Ricetta", 290),
                                   ("k", "kcal", 60), ("s", "Stato", 360)], 9)
        self.t_piano.pack(fill="both", expand=True, pady=8)
        self.testo = tk.Text(f, height=9, wrap="word", state="disabled", relief="flat", background="#f6f7f9")
        self.testo.pack(fill="x")

    def tab_profilo(self, f):
        self.l_profilo = ttk.Label(f, justify="left")
        self.l_profilo.pack(anchor="w", pady=(0, 10))
        self.f_prof = Form(f, "Imposta il profilo",
                           [("sesso", "Sesso", ["M", "F"]), ("att", "Attivita'", sorted(m.PAL)),
                            ("ob", "Obiettivo", ["mantenimento", "dimagrimento", "aumento"]),
                            ("kcal", "Kcal/giorno (obbligatorio se non mantenimento)", None)],
                           "Salva profilo", self.salva_profilo)
        self.f_prof.pack(anchor="w")

    # ---------------- aggiornamento ----------------

    def aggiorna(self):
        with db() as c:
            nomi = [r[0] for r in c.execute("SELECT Nome FROM ALIAS ORDER BY Nome")]
            gruppi = [r[0] for r in c.execute("SELECT Nome FROM GRUPPI_SOSTITUIBILI ORDER BY Nome")]
            spesa = c.execute("SELECT Nome, Quantita_Mancante, Unita_di_misura FROM VISTA_LISTA_SPESA "
                              "ORDER BY Nome").fetchall()
            ricette = self.righe_ricette(c)
            p = m.carica_profilo(c)
        for form in (self.f_add, self.f_use):
            form.w["nome"]["values"] = nomi
        self.f_ing.w["p"]["values"] = nomi + gruppi
        self.t_disp.riempi([((r["Nome"], f"{r['Peso_Totale_Disponibile']:g}", r["Unita_di_misura"], r["Luogo"]), None)
                            for r in ottieni_inventario_disponibile()])
        self.t_spesa.riempi([((n, f"{q:.2f}", u), None) for n, q, u in spesa])
        self.t_ric.riempi(ricette)
        kcal = f"{p['Kcal_target']:.0f} kcal/giorno" if p["Kcal_target"] else "non definito (imposta il profilo)"
        self.l_profilo.configure(text=(
            f"Peso {p['Peso_kg']:g} kg, altezza {p['Altezza_cm']:g} cm, eta' {p['Eta_anni']}\n"
            f"Sesso: {p['Sesso'] or '-'}   Attivita': {p['Livello_attivita'] or '-'}   "
            f"Obiettivo: {p['Obiettivo'] or '-'}\nTarget: {kcal}"))

    @staticmethod
    def righe_ricette(c):
        prodotti, nomi, inv = m.carica_prodotti(c), m.nomi_prodotti(c), m.InventarioVirtuale.da_db(c)
        righe = []
        for id_r, nome in c.execute("SELECT ID_ricetta, Nome FROM RICETTE ORDER BY Nome").fetchall():
            sim = m.simula_ricetta(c, inv.copia(), id_r, prodotti)
            mac = m.macro_ricetta(c, id_r, prodotti, sim["scelte_gruppo"])
            righe.append(((nome, f"{mac['Kcal']:.0f}", f"{mac['Proteine']:.1f}", f"{mac['Carboidrati']:.1f}",
                           f"{mac['Grassi']:.1f}", f"{mac['Fibre']:.1f}", f"{sim['copertura_aperto'] * 100:.0f}%",
                           m.esito(sim, prodotti, nomi)), None if sim["fattibile"] else "ko"))
        return righe

    # ---------------- azioni ----------------

    @sicuro
    def aggiungi(self):
        v = self.f_add.leggi()
        peso, num = numero(v["peso"], "peso"), int(numero(v["num"], "confezioni"))
        try:
            aggiungi_item(v["nome"], peso, num, v["luogo"])
        except ProdottoNonTrovato:
            with db() as c:
                suggerito = c.execute("SELECT COALESCE(MAX(ID), 0) + 1 FROM NUTRIENTI").fetchone()[0]
            extra = DialogoProdotto(self, v["nome"], suggerito).risultato
            if extra is None:
                return
            aggiungi_item(v["nome"], peso, num, v["luogo"], **extra)
        self.msg(f"Aggiunto: {v['nome']} ({num} x {peso:g}) in {v['luogo']}.")
        self.f_add.pulisci()
        self.aggiorna()

    @sicuro
    def consuma(self):
        v = self.f_use.leggi()
        r = consuma_quantita(v["nome"], numero(v["q"], "quantita'"))
        testo = f"Consumati {r['consumato']:g} di {v['nome']}."
        if r["disponibilita_insufficiente"]:
            testo += f" Disponibilita' insufficiente: mancano {r['mancante']:g}, aggiunti alla spesa."
        self.msg(testo, r["disponibilita_insufficiente"])
        self.f_use.pulisci()
        self.aggiorna()

    def parametri_piano(self):
        data = self.v_data.get().strip()
        try:
            date.fromisoformat(data)
        except ValueError:
            raise ValueError("Data non valida: usa il formato AAAA-MM-GG.")
        giorni = int(numero(self.v_giorni.get(), "giorni"))
        if not 1 <= giorni <= 31:
            raise ValueError("I giorni devono essere tra 1 e 31.")
        kcal = numero(self.v_kcal.get(), "kcal") if self.v_kcal.get().strip() else None
        return data, giorni, kcal

    def scrivi_testo(self, testo):
        self.testo.configure(state="normal")
        self.testo.delete("1.0", "end")
        self.testo.insert("end", testo)
        self.testo.configure(state="disabled")

    @sicuro
    def proponi(self):
        data, giorni, kcal = self.parametri_piano()
        with db() as c:
            self.piani = pf.pianifica_settimana(c, data, giorni, kcal, self.fissi or None)
            prodotti, nomi = m.carica_prodotti(c), m.nomi_prodotti(c)
            riepilogo = io.StringIO()
            with contextlib.redirect_stdout(riepilogo):
                pf.stampa_settimana(c, self.piani)
        righe = []
        for p in self.piani:
            for v in p["pasti"]:
                if v["ID_ricetta"] is None:
                    righe.append(((p["data"], v["pasto"], "(saltato)", "", "saltato"), None))
                else:
                    ok = v["sim"]["fattibile"]
                    righe.append(((p["data"], v["pasto"], v["ricetta"], f"{v['macro']['Kcal']:.0f}",
                                   "proposta: " + ("usa un avanzo" if v["avanzo"] else m.esito(v["sim"], prodotti, nomi))), None if ok else "ko"))
        self.t_piano.riempi(righe)
        self.scrivi_testo(riepilogo.getvalue())
        self.msg("Piano proposto (non ancora salvato).")

    @sicuro
    def salva(self):
        if not self.piani:
            raise ValueError("Prima proponi un piano.")
        with db() as c:
            pf.salva_settimana(c, self.piani)
        self.msg("Piano salvato. Lista della spesa aggiornata.")
        self.mostra_salvato()
        self.aggiorna()

    def mostra_salvato(self):
        data, giorni, _ = self.parametri_piano()
        with db() as c:
            righe = c.execute(
                "SELECT p.Data, p.Pasto, COALESCE(r.Nome, '(saltato)'), COALESCE(CAST(ROUND(v.Kcal) AS INTEGER), ''), "
                "p.Stato FROM PIANO p LEFT JOIN COTTURE k USING (ID_cottura) "
                "LEFT JOIN RICETTE r ON r.ID_ricetta = k.ID_ricetta "
                "LEFT JOIN VISTA_MACRO_RICETTA v ON v.ID_ricetta = r.ID_ricetta "
                "WHERE p.Data >= ? AND p.Data < date(?, '+' || ? || ' day') "
                "ORDER BY p.Data, (SELECT Ordine FROM PASTI WHERE Nome = p.Pasto)", (data, data, giorni)).fetchall()
        self.t_piano.riempi([(r, None) for r in righe])

    @sicuro
    def cucinato(self):
        selezione = self.t_piano.tv.selection()
        if not selezione:
            raise ValueError("Seleziona un pasto nella tabella (dopo aver salvato il piano).")
        data, pasto = self.t_piano.tv.item(selezione[0], "values")[:2]
        n = simpledialog.askinteger(
            "Cucinato", f"Quante porzioni cucini ora per '{pasto}' del {data}?\n"
                        "1 = solo questo pasto; di piu' = le altre diventano avanzi per i pasti successivi.\n"
                        "(Se il pasto usa un avanzo gia' pronto, lascia 1.)",
            parent=self, minvalue=1, maxvalue=20, initialvalue=1)
        if n is None:
            return
        with db() as c:
            pf.segna_cucinato(c, data, pasto, n)
        self.msg(f"Fatto: {pasto} del {data} cucinato, inventario aggiornato"
                 + (f" ({n - 1} avanzi disponibili)." if n > 1 else "."))
        self.mostra_salvato()
        self.aggiorna()

    @sicuro
    def salva_profilo(self):
        v = self.f_prof.leggi()
        if not (v["sesso"] and v["att"] and v["ob"]):
            raise ValueError("Compila sesso, attivita' e obiettivo.")
        with db() as c:
            kcal = m.imposta_profilo(c, v["sesso"], v["att"], v["ob"], numero(v["kcal"], "kcal") if v["kcal"] else None)
        self.msg(f"Profilo salvato: target {kcal:.0f} kcal/giorno.")
        self.aggiorna()

    @sicuro
    def aggiungi_ingrediente(self):
        v = self.f_ing.leggi()
        if not v["p"] or not v["u"]:
            raise ValueError("Indica prodotto (o gruppo), quantita' e unita'.")
        self.bozza.append((v["p"], numero(v["q"], "quantita'"), v["u"]))
        self.f_ing.pulisci()
        self.mostra_bozza()

    def mostra_bozza(self):
        self.t_bozza.riempi([((p, f"{q:g}", u), None) for p, q, u in self.bozza])

    @sicuro
    def rimuovi_ingrediente(self):
        selezione = self.t_bozza.tv.selection()
        if selezione:
            del self.bozza[self.t_bozza.tv.index(selezione[0])]
            self.mostra_bozza()

    @sicuro
    def salva_ricetta(self):
        nome = self.v_rnome.get().strip()
        pasti = [n for n, var in self.v_pasti.items() if var.get()]
        with db() as c:
            esito = rc.crea_ricetta(c, nome, pasti, self.bozza)
        self.bozza = []
        self.v_rnome.set("")
        for var in self.v_pasti.values():
            var.set(False)
        self.mostra_bozza()
        avviso = " Attenzione: " + esito["avvisi"][0] if esito["avvisi"] else ""
        self.msg(f"Ricetta '{nome}' salvata." + avviso, bool(avviso))
        self.aggiorna()

    @sicuro
    def importa_ricette(self):
        percorso = filedialog.askopenfilename(title="Importa ricette",
                                              filetypes=[("Ricette", "*.json *.csv"), ("Tutti i file", "*.*")])
        if not percorso:
            return
        with db() as c:
            creati, errori = rc.importa(c, percorso)
        self.msg(f"Importate {creati} ricette" + (f", {len(errori)} saltate." if errori else "."), bool(errori))
        if errori:
            messagebox.showwarning("Ricette saltate", "\n".join(errori[:15]))
        self.aggiorna()

    @sicuro
    def elimina_ricetta(self):
        selezione = self.t_ric.tv.selection()
        if not selezione:
            raise ValueError("Seleziona una ricetta nella tabella.")
        nome = self.t_ric.tv.item(selezione[0], "values")[0]
        if not messagebox.askyesno("Elimina", f"Eliminare la ricetta '{nome}'?"):
            return
        with db() as c:
            rc.elimina_ricetta(c, nome)
        self.msg(f"Ricetta '{nome}' eliminata.")
        self.aggiorna()

    def pasto_selezionato(self):
        selezione = self.t_piano.tv.selection()
        if not selezione:
            raise ValueError("Seleziona un pasto nella tabella.")
        return self.t_piano.tv.item(selezione[0], "values")[:2]

    @sicuro
    def blocca(self):
        data, pasto = self.pasto_selezionato()
        with db() as c:
            nomi = [r[0] for r in c.execute("SELECT r.Nome FROM RICETTE r JOIN RICETTA_PASTI p USING (ID_ricetta) "
                                            "WHERE p.Pasto = ? ORDER BY r.Nome", (pasto,))]
        if not nomi:
            raise ValueError(f"Nessuna ricetta ammessa per {pasto}.")
        scelta = DialogoScelta(self, f"Ricetta per {pasto} del {data}", nomi).risultato
        if scelta is None:
            return
        with db() as c:
            self.fissi.setdefault(data, {})[pasto] = m.id_ricetta_da_nome(c, scelta)
        self.proponi()
        self.msg(f"{pasto} del {data} fissato su '{scelta}': salva il piano per confermarlo.")

    @sicuro
    def salta(self):
        data, pasto = self.pasto_selezionato()
        self.fissi.setdefault(data, {})[pasto] = None
        self.proponi()
        self.msg(f"{pasto} del {data} saltato: salva il piano per registrarlo.")

    @sicuro
    def sblocca(self):
        self.fissi = {}
        self.proponi()


if __name__ == "__main__":
    App().mainloop()
