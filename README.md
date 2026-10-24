# Meal Prep Project

## MEAL PLANNER - Dispensa, ricette e piano dei pasti (Programma desktop)

**Requisiti:** Python 3.10 o superiore. Nessuna libreria esterna (solo libreria standard, **Tkinter** incluso; su Linux, se manca, installa il pacchetto di sistema `python3-tk`).

### Avvio

Per avviare il programma, esegui:

```bash
python gui.py
```

> *Nota: Al primo avvio, il programma creerà automaticamente il database `inventario.db` nella stessa cartella.*

---

### Come Iniziare (Schede della finestra)

1. **Profilo** - Imposta sesso, attività e obiettivo: serve per calcolare le kcal giornaliere.
2. **Dispensa** - Aggiungi i prodotti (la prima volta che inserisci un nome nuovo ti verranno chieste l'unità di misura e i valori nutrizionali per 100 g).
3. **Ricette** - Inserisci le ricette manualmente dal modulo oppure importale da file `.json` o `.csv`.
4. **Piano** - Clicca su "Proponi piano", poi "Salva piano". Quando cucini, usa "Segna come cucinato" (indica quante porzioni hai preparato: quelle in più diventeranno avanzi). Puoi anche bloccare o saltare un pasto.
5. **Spesa** - Mostra cosa manca (calcolando i consumi che superano la disponibilità e gli ingredienti necessari per i piani futuri).

---

### Import Ricette

Le quantità indicate devono essere per **UNA** porzione.  
**Unità supportate:** `g`, `kg`, `ml`, `L`, `pz`, `cucchiaio`, `cucchiaino`.

#### Formato JSON
```json
[
  {
    "nome": "Riso e pollo", 
    "pasti": ["pranzo", "cena"],
    "ingredienti": [
      {
        "prodotto": "Riso", 
        "quantita": 100, 
        "unita": "g"
      }
    ]
  }
]
```

#### Formato CSV
Utilizza come separatore `;` oppure `,`. I pasti multipli vanno separati dal carattere `|`.
```csv
ricetta;pasti;prodotto;quantita;unita;quanto_basta
```

---

### Riga di Comando (Facoltativa)

Puoi interagire con il programma anche da terminale:

```bash
python motore.py ricette | giorno ... | profilo ...
python pianificatore.py giorno | settimana | spesa
python ricette.py importa file.json
```