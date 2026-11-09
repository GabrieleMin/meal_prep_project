# Storico Dettagliato di Tutte le Modifiche del Progetto

Questo documento descrive dettagliatamente e minuziosamente l'evoluzione architetturale, le feature sviluppate, le ottimizzazioni delle performance e le operazioni di refactoring implementate nel progetto **Meal Planner** dall'inizio ad oggi.

---

## Indice delle Fasi Evolutive
1. [Stato Iniziale e Problematiche Rilevate](#1-stato-iniziale-e-problematiche-rilevate)
2. [Fase 1: Bonifica Architetturale, Schema SQLite Unificato e Isolamento Offline](#fase-1-bonifica-architetturale-schema-sqlite-unificato-e-isolamento-offline)
3. [Fase 2: Pipeline di Acquisizione Scontrini e Riconciliazione Catalogo](#fase-2-pipeline-di-acquisizione-scontrini-e-riconciliazione-catalogo)
4. [Fase 3: Modifica Interattiva Alimenti e Disambiguazione Macro (Task 1)](#fase-3-modifica-interattiva-alimenti-e-disambiguazione-macro-task-1)
5. [Fase 4: Pulsante di Eliminazione Alimento Personalizzato (Task 2)](#fase-4-pulsante-di-eliminazione-alimento-personalizzato-task-2)
6. [Fase 5: Nuova Scheda Setting Modulare a 4 Sezioni (Task 3)](#fase-5-nuova-scheda-setting-modulare-a-4-sezioni-task-3)
7. [Fase 6: Riorganizzazione Scheda Profilo e Overlay Stato Vuoto (Task 4)](#fase-6-riorganizzazione-scheda-profilo-e-overlay-stato-vuoto-task-4)
8. [Fase 7: Ridisegno Creazione Ricette e Registrazione Prodotti al Volo (Task 5)](#fase-7-ridisegno-creazione-ricette-e-registrazione-prodotti-al-volo-task-5)
9. [Fase 8: Separazione Tema Default vs Runtime](#fase-8-separazione-tema-default-vs-runtime)
10. [Fase 9: Pulizia Radicale Dati Fittizi a Priori](#fase-9-pulizia-radicale-dati-fittizi-a-priori)
11. [Fase 10: Centralizzazione Configurazioni in SQLite e Directory data/](#fase-10-centralizzazione-configurazioni-in-sqlite-e-directory-data)
12. [Fase 11: Ristrutturazione Cartella e Consolidamento Test](#fase-11-ristrutturazione-cartella-e-consolidamento-test)

---

## 1. Stato Iniziale e Problematiche Rilevate

Nelle fasi embrionali dell'applicazione:
- Il codice era frammentato tra moduli interni al package `meal_planner/` e una cartella esterna non standard `features_development/` caricata con modifiche invasive a `sys.path`.
- Esistevano script di migrazione storici (`migrate_legacy_db.py`) legati al vecchio formato `inventario.db`, con controlli di compatibilità sparsi in `__main__.py`.
- Le definizioni dello schema SQLite erano suddivise in modo non sincronizzato tra `schema.py` e `recipe_schema.py`.
- L'inventario della dispensa era puramente testuale e in sola lettura diretta: per modificare un prodotto o consumarlo era necessario digitare a mano il nome senza poter modificare le caratteristiche intrinseche dell'alimento.
- Le ricette potevano essere aggiunte solo tramite importazione file JSON/CSV o da CLI.
- Le configurazioni applicative risiedevano in un file JSON sfuso (`config.json`) nella radice del progetto, mentre il database risiedeva anch'esso nella radice.
- Lo schema del database pre-popolava a priori un profilo utente fittizio (87 kg, 176 cm, 23 anni), costringendo l'interfaccia a mostrare dati simulati anche quando l'utente non aveva mai inserito il proprio profilo.

---

## 2. Fase 1: Bonifica Architetturale, Schema SQLite Unificato e Isolamento Offline

### Azioni Implementate:
1. **Eliminazione Cartella Esterna `features_development/`**:
   - Spostati e rifattorizzati i moduli di lettura e riconciliazione scontrini all'interno del package primario [`meal_planner/receipts/`](file:///home/gabriele/Desktop/meal_prep/meal_prep_project/meal_planner/receipts/).
   - Rimossi tutti i blocchi di codice con `sys.path.insert(0, ...)` per garantire importazioni pulite e conformi a PEP-8.
2. **Rimozione del Codice Legacy `inventario.db`**:
   - Eliminato definitivamente lo script `scripts/migrate_legacy_db.py`.
   - Rimossi controlli e ramificazioni su vecchi database non relazionali.
3. **Consolidamento dello Schema Database in [`meal_planner/db/schema.py`](file:///home/gabriele/Desktop/meal_prep/meal_prep_project/meal_planner/db/schema.py)**:
   - Definizione unificata, idempotente e strutturata di tutte le tabelle relazionali: `PRODUCTS`, `ALIASES`, `LOCATIONS`, `INVENTORY`, `SHOPPING_LIST`, `EXCLUSION_LIST`, `MEALS`, `RECIPES`, `RECIPE_MEALS`, `SUBSTITUTE_GROUPS`, `GROUP_MEMBERS`, `RECIPE_INGREDIENTS`, `COOKINGS`, `PLAN`, `PROFILE`, `WEIGHT_LOG`, `GOALS`, `PLAN_SHOPPING_LIST`, `RECEIPT_LOGS`, `APP_SETTINGS`.
   - Creazione di viste relazionali indicizzate: `SHOPPING_LIST_VIEW`, `INGREDIENT_GRAMS_VIEW`, `RECIPE_MACROS_VIEW`.
   - Trigger SQLite `INSTEAD OF` per garantire l'aggiornamento sicuro di viste profilo e alias.
4. **Vincoli di Integrità e Concorrenza**:
   - Attivazione su ogni connessione di `PRAGMA foreign_keys = ON;`, `PRAGMA journal_mode = WAL;` e `PRAGMA synchronous = NORMAL;`.
   - Garanzia di funzionamento al **100% offline**: nessuna connessione a socket remoti, chiamate HTTP o CDN esterne.

---

## 3. Fase 2: Pipeline di Acquisizione Scontrini e Riconciliazione Catalogo

### Azioni Implementate:
1. **Modulo di Lettura Scontrini ([`meal_planner/receipts/reader.py`](file:///home/gabriele/Desktop/meal_prep/meal_prep_project/meal_planner/receipts/reader.py))**:
   - Algoritmo di parsing per estrarre descrizioni prodotto, quantità, prezzi e date da scontrini formattati o grezzi.
   - Pulizia euristica del rumore OCR e tokenizzazione intelligente.
2. **Normalizzazione e Conversione Unità ([`meal_planner/receipts/pipeline.py`](file:///home/gabriele/Desktop/meal_prep/meal_prep_project/meal_planner/receipts/pipeline.py))**:
   - Riconoscimento delle unità dimensionali canoniche: massa (`kg`, `g`), volume (`L`, `ml`) e conteggio (`pcs`).
   - Conversione accurata basata sulla densità e sul peso medio per pezzo (`Grams_per_piece`, `Density_kg_per_L`).
   - Blocco rigido su incompatibilità dimensionali (es. conversione da litri a chilogrammi senza densità).
3. **Interfaccia di Riconciliazione ([`ReceiptReconciliationDialog`](file:///home/gabriele/Desktop/meal_prep/meal_prep_project/meal_planner/gui/dialogs.py))**:
   - Finestra interattiva con visualizzazione tabellare delle righe scontrino.
   - Indicatori visivi di confidenza: associazione automatica ad alias già noti o richiesta di disambiguazione.
   - Possibilità di modificare quantità, confezioni, data di scadenza e locazione di stoccaggio (`pantry`, `fridge`, `freezer`).
   - Commit atomico in una singola transazione SQLite per evitare scritture parziali in caso di errore.
4. **Tracciamento Storico**:
   - Tabella SQLite `RECEIPT_LOGS` con campi `time`, `day`, `file_name` per audit completo di tutti gli scontrini importati.

---

## 4. Fase 3: Modifica Interattiva Alimenti e Disambiguazione Macro (Task 1)

### Azioni Implementate:
1. **Righe Inventario Cliccabili ([`meal_planner/gui/app.py`](file:///home/gabriele/Desktop/meal_prep/meal_prep_project/meal_planner/gui/app.py))**:
   - Binding del doppio clic e selezione sulla tabella alimenti della Dispensa (`t_disp`).
   - Apertura automatica di [`InventoryItemEditDialog`](file:///home/gabriele/Desktop/meal_prep/meal_prep_project/meal_planner/gui/dialogs.py) precompilata con tutte le caratteristiche della riga selezionata.
2. **Modifica Completa delle Caratteristiche**:
   - Dati di inventario: Nome, Peso unitario, Confezioni integre, Rimanenza aperta, Locazione, Data di scadenza.
   - Dati di catalogo e prodotto: Unità, Grammi per pezzo, Densità.
   - Profilo nutrizionale (su 100g): Calorie (Kcal), Proteine, Carboidrati, Grassi, Zuccheri, Grassi Saturi, Fibre, Sale.
3. **Prompt di Disambiguazione Macro ([`MacroPromptDialog`](file:///home/gabriele/Desktop/meal_prep/meal_prep_project/meal_planner/gui/dialogs.py))**:
   - Quando l'utente modifica i macronutrienti di un alimento collegato ad altri alias o altre istanze in inventario/ricette, il sistema intercetta la modifica e richiede esplicitamente la strategia desiderata:
     - **Opzione 1 (Modifica solo questo alimento specifico)**: Il sistema genera un nuovo `Product_ID` nel catalogo e ri-associa la sola riga d'inventario modificata al nuovo prodotto, preservando intatti tutti gli altri alimenti collegati al vecchio alias.
     - **Opzione 2 (Propaga la modifica a ritroso)**: Il sistema aggiorna direttamente la riga del prodotto in `PRODUCTS`, propagando i nuovi valori nutrizionali su tutti gli alias, le ricette e le righe inventario collegate.
     - **Annulla**: Ripristina i valori originari senza alterare il database.

---

## 5. Fase 4: Pulsante di Eliminazione Alimento Personalizzato (Task 2)

### Azioni Implementate:
1. **Pulsante Dedicato su Ogni Riga**:
   - Aggiunta di un pulsante di cancellazione per ciascun singolo item presente nella tabella dell'inventario.
2. **Specifiche Grafiche Personalizzate**:
   - Disegnato come **croce rosso scuro racchiusa in un quadrato stondato** (`#8b0000` / `#a91d22`), conforme sia al tema Dark che al tema Light.
   - Evidenziazione visiva al passaggio del mouse (hover).
3. **Sicurezza ed Eliminazione Atomica**:
   - Cliccando il pulsante, l'applicazione richiede conferma modale prima di eseguire la cancellazione.
   - L'eliminazione rimuove la riga selezionata da `INVENTORY`, lasciando intatto il catalogo generale dei prodotti e preservando la coerenza delle ricette.

---

## 6. Fase 5: Nuova Scheda Setting Modulare a 4 Sezioni (Task 3)

### Azioni Implementate:
1. **Architettura Non-Monolitica**:
   - Creata una nuova scheda dedicata **"Setting"** distinta e complementare alla scheda **"Profilo"**.
   - Per evitare schermate sovraffollate, la visualizzazione è governata da una barra di navigazione secondaria a pulsanti che permette di passare tra 4 sezioni isolate:
     - `style_and_config` (Stile & Configurazione)
     - `full_profile` (Profilo Completo)
     - `receipt_logs` (Registro Scontrini)
     - `destructive_actions` (Azioni Distruttive)
2. **Sezione 1: Style & Configuration**:
   - Selezione del tema di default all'avvio (`dark` / `light`).
   - Configurazione della soglia di avviso scadenza alimenti (in giorni).
   - Selezione della cartella di destinazione dei report e scontrini con file dialog interattivo.
3. **Sezione 2: Full Profile**:
   - Modifica delle caratteristiche statiche dell'utente che non cambiano frequentemente nel tempo: **Sesso** e **Altezza**.
   - Pannello di riepilogo in sola lettura formattato con tutte le metriche statiche, dinamiche, obiettivi calorici e parametri di pianificazione.
4. **Sezione 3: Receipt Logs**:
   - Tabella interattiva per consultare la cronologia di tutti gli scontrini importati, con colonne: **Orario (Time)**, **Giorno (Day)** e **Nome File (File Name)**.
5. **Sezione 4: Destructive Actions**:
   - Pannello ad alto livello di sicurezza ("Danger Zone") per operazioni distruttive irreversibili:
     - *Eliminazione completa dell'inventario*: cancellazione di tutte le scorte fisiche preservando ricette e catalogo.
     - *Reset del profilo utente*: cancellazione integrale del profilo e dello storico delle pesate.
   - Entrambe le azioni sono protette da finestre di conferma modale (`askyesno`) per prevenire perdite accidentali di dati.

---

## 7. Fase 6: Riorganizzazione Scheda Profilo e Overlay Stato Vuoto (Task 4)

### Azioni Implementate:
1. **Segregazione dei Dati Statici e Dinamici**:
   - Le informazioni statiche (Sesso, Altezza) sono state trasferite nella scheda Setting.
   - La scheda "Profilo" è stata dedicata unicamente alle grandezze dinamiche che variano nel tempo: Peso corporeo attuale, Livello di attività, Obiettivo nutrizionale (mantenimento, perdita o aumento peso), Tolleranza calorica, e Storico delle pesate.
2. **Overlay per lo Stato Vuoto (Empty-State)**:
   - Se nel database non è presente alcun profilo utente, l'intera schermata della scheda Profilo viene coperta da un grande rettangolo su canvas con angoli smussati.
   - Il rettangolo mostra al centro il testo esatto:
     > *"Ciao, aggiunti il tuo profilo per iniziare"*
     e presenta un comodo pulsante "Aggiungi profilo" per avviare la procedura guidata.
3. **Transizione Automatica**:
   - Non appena l'utente compila e salva il profilo, il canvas overlay scompare istantaneamente (`place_forget`), rivelando tutti i campi, il grafico dell'andamento ponderale e il calcolo automatico del BMI e delle calorie di mantenimento.
   - Se l'utente resetta il profilo da Setting, l'overlay ricompare automaticamente.

---

## 8. Fase 7: Ridisegno Creazione Ricette e Registrazione Prodotti al Volo (Task 5)

### Azioni Implementate:
1. **Nuovo Form Interattivo ([`AddRecipeDialog`](file:///home/gabriele/Desktop/meal_prep/meal_prep_project/meal_planner/gui/dialogs.py))**:
   - Sostituito il vecchio sistema rudimentale con un builder visivo moderno.
   - Campi strutturati per: Nome della ricetta, Selezione dei pasti abilitati (`colazione`, `pranzo`, `cena`), Note di preparazione, Porzioni di default.
   - Tabella dinamica degli ingredienti con possibilità di aggiungere, modificare e rimuovere ingredienti in tempo reale.
2. **Calcolo Macronutrienti in Tempo Reale**:
   - A ogni ingrediente inserito, il dialog ricalcola all'istante le calorie, proteine, carboidrati e grassi complessivi e per porzione, mostrando l'anteprima nutrizionale prima del salvataggio.
3. **Registrazione Prodotti al Volo ([`QuickProductDialog`](file:///home/gabriele/Desktop/meal_prep/meal_prep_project/meal_planner/gui/dialogs.py))**:
   - Se l'utente inserisce un ingrediente che non esiste ancora nel catalogo, si apre automaticamente un popup snello per registrare il prodotto:
     - Unità di misura predefinita (`g`, `kg`, `ml`, `L`, `pcs`).
     - Macronutrienti su 100g (Kcal, Proteine, Carboidrati, Grassi, Fibre).
   - Il prodotto viene inserito atomicamente in `PRODUCTS` e `ALIASES` con ID generato automaticamente da SQLite (`lastrowid`), consentendo il salvataggio immediato della ricetta senza bloccare l'utente.
4. **Mantenimento Retrocompatibile Import CSV/JSON**:
   - Preservata integralmente la funzionalità di import massivo da file `.csv` e `.json` esistente.

---

## 9. Fase 8: Separazione Tema Default vs Runtime

### Azioni Implementate:
1. **Disaccoppiamento tra Avvio e Sessione Corrente**:
   - Precedentemente, cliccare il pulsante rapido nell'header sovrascriveva permanentemente il file di configurazione, eliminando la distinzione tra preferenza di default e cambio temporaneo.
2. **Tema di Default Persistito nei Setting**:
   - Nella scheda Setting (sezione *Style & Configuration*), l'utente definisce `"Default Startup Theme"` (`dark` o `light`). Questo valore viene salvato nel database ed è il tema caricato a ogni nuovo avvio dell'applicazione.
3. **Toggle Rapido nell'Header per la Sessione**:
   - Il pulsante nell'header in alto a destra applica il tema graficamente all'istante (`sv_ttk.set_theme`) per la sessione di lavoro attiva, **senza** modificare né sovrascrivere il default salvato nel database. Al riavvio successivo l'app riparte sempre dalla preferenza di default.

---

## 10. Fase 9: Pulizia Radicale Dati Fittizi a Priori

### Azioni Implementate:
1. **Rimozione del Seed Fittizio in [`meal_planner/db/schema.py`](file:///home/gabriele/Desktop/meal_prep/meal_prep_project/meal_planner/db/schema.py)**:
   - Eliminato `DEFAULT_PROFILE = (1, 87.0, 176.0, 23)` dall'inizializzazione del database. Un database vuoto o resettato ha ora esattamente **0 righe** nella tabella `PROFILE`.
2. **Campi Vuoti di Default nei Setting**:
   - Rimossa l'impostazione fittizia a priori di `"M"` per il Sesso e `"175"` per l'Altezza. Se non esiste un profilo, i campi rimangono rigorosamente vuoti (`""`).
   - Il pannello riepilogativo mostra chiaramente:
     > *"No user profile is currently configured. No profile metrics to display. Please configure your profile in the Profilo tab."*
3. **Nessun Inserimento Dummy**:
   - Rimosso l'inserimento arbitrario di `70.0 kg` e `25 anni` nel salvataggio del profilo statico. La tabella `PROFILE` supporta campi parziali o nulli per evitare qualsiasi contaminazione con dati finti.

---

## 11. Fase 10: Centralizzazione Configurazioni in SQLite e Directory data/

### Azioni Implementate:
1. **Creazione Cartella Dedicata `data/`**:
   - Il database primario dell'applicazione è stato riallocato in [`data/inventory.db`](file:///home/gabriele/Desktop/meal_prep/meal_prep_project/data/inventory.db).
   - In [`meal_planner/db/connection.py`](file:///home/gabriele/Desktop/meal_prep/meal_prep_project/meal_planner/db/connection.py) è implementata la migrazione automatica da root a `data/` e il mantenimento di un symlink relativo trasparente `inventory.db -> data/inventory.db`, garantendo compatibilità al 100% con script esterni e test suite.
2. **Tabella `APP_SETTINGS` in SQLite**:
   - Creata la tabella relazionale:
     ```sql
     CREATE TABLE IF NOT EXISTS APP_SETTINGS (
         Key        TEXT PRIMARY KEY,
         Value      TEXT NOT NULL,
         Updated_At TEXT DEFAULT (datetime('now', 'localtime'))
     );
     ```
3. **Eliminazione di `config.json`**:
   - Il modulo [`meal_planner/config.py`](file:///home/gabriele/Desktop/meal_prep/meal_prep_project/meal_planner/config.py) è stato riprogrammato per leggere e scrivere direttamente su `APP_SETTINGS`.
   - I valori del vecchio `config.json` sono stati importati nel database e il file JSON rimosso, centralizzando tutti i dati in un unico file SQLite robusto.

---

## 12. Fase 11: Ristrutturazione Cartella e Consolidamento Test

### Azioni Implementate:
1. **Ristrutturazione Directory**:
   - Creazione della cartella [`docs/`](file:///home/gabriele/Desktop/meal_prep/meal_prep_project/docs/) e della sottocartella [`docs/images/`](file:///home/gabriele/Desktop/meal_prep/meal_prep_project/docs/images/) con tutti gli screenshot dell'applicazione.
   - Eliminazione dei file temporanei di checklist in root (`TEST_INFRA.md`, `TEST_READY.md`).
   - Pulizia dei file temporanei in `receipts_output/`.
   - Unificazione degli script di demo in [`scripts/demo_receipt.py`](file:///home/gabriele/Desktop/meal_prep/meal_prep_project/scripts/demo_receipt.py) con eliminazione dei due vecchi launcher ridondanti.
   - Semplificazione di `recipe_schema.py` in un facade trasparente di 4 righe.
2. **Accorpamento delle 30 Suite di Test in 6 Moduli Tematici Coesi**:
   - [`tests/test_domain_core.py`](file:///home/gabriele/Desktop/meal_prep/meal_prep_project/tests/test_domain_core.py)
   - [`tests/test_receipts_and_reconciliation.py`](file:///home/gabriele/Desktop/meal_prep/meal_prep_project/tests/test_receipts_and_reconciliation.py)
   - [`tests/test_inventory_management.py`](file:///home/gabriele/Desktop/meal_prep/meal_prep_project/tests/test_inventory_management.py)
   - [`tests/test_recipe_management.py`](file:///home/gabriele/Desktop/meal_prep/meal_prep_project/tests/test_recipe_management.py)
   - [`tests/test_settings_and_profile.py`](file:///home/gabriele/Desktop/meal_prep/meal_prep_project/tests/test_settings_and_profile.py)
   - [`tests/test_e2e_and_invariants.py`](file:///home/gabriele/Desktop/meal_prep/meal_prep_project/tests/test_e2e_and_invariants.py)
   - Tutti i 399+ casi di test sono stati preservati al 100%, con tempi di esecuzione e discovery notevolmente accelerati.

---

## 13. Fase 12: Hardening Test Suite Consolidate, Risoluzione Edge Cases e Allineamento Documentazione (Gen 4)

### Obiettivo della Fase:
Verificare, testare e consolidare le 6 suite di test tematiche dell'applicazione, estendendo la copertura dei casi limite più critici (float non finiti, fallimenti atomici, porzioni estreme, ciclo di vita dell'overlay profilo, guardrail di cancellazione e vincoli relazionali offline) e allineando la documentazione di progetto (`README.md`, `docs/GUIDA_FUNZIONAMENTO.md`).

### Azioni Implementate:
1. **Estensione delle 6 Suite di Test Tematiche con Classi Dedicate ai Casi Limite**:
   - `tests/test_domain_core.py`: stabilizzazione del micro-benchmark `test_c_round_down_speedup` (warmup e best-of-3 runs) e aggiunta della classe `TestDomainBoundaryEdgeCases` per validare il rifiuto di float non finiti e booleani in `consume_quantity`, conversioni unità senza fattori e collisioni CRUD su location.
   - `tests/test_receipts_and_reconciliation.py`: aggiunta della classe `TestReceiptEdgeCasesAndCorruptFiles` per validare correzione fuzzy con cataloghi vuoti o tie-break, file immagine scontrino a 0-byte o corrotti, salvataggio recensione vuoto e righe dettaglio senza articolo padre.
   - `tests/test_inventory_management.py`: aggiunta della classe `TestInventoryAdvancedEdgeCases` per validare consumo FIFO su righe inventario concorrenti dopo eliminazione parziale, resilienza su inventario vuoto, decoupling prodotto in opzione 1 vs propagazione in opzione 2, ed eliminazione righe con resto aperto.
   - `tests/test_recipe_management.py`: aggiunta della classe `TestRecipeManagementAdvancedEdgeCases` per validare:
     - Scaling estremo delle porzioni (`servings = 1000`) e rifiuto validato di input nulli, negativi o non numerici (`0`, `-1`, `NaN`, `Inf`, `"abc"`).
     - Atomicità del rollback in `create_recipe`: verifica che ingredienti sconosciuti o duplicati sollevino `ValueError` senza lasciare record orfani in `RECIPES` o `RECIPE_MEALS`.
     - Persistenza verbatim di note multilinea con caratteri unicode ed emoji (`🌿`, `🍝`) e flag `To_Taste` a livello di ingrediente.
     - Validazione avversariale in `register_product_on_the_fly` contro float non finiti, booleani e collisioni con gruppi sostitutivi.
   - `tests/test_settings_and_profile.py`: aggiunta della classe `TestSettingsAndProfileAdvancedEdgeCases` per validare:
     - Ciclo di vita completo dell'overlay empty-state con testo verbatim *"Ciao, aggiunti il tuo profilo per iniziare"*.
     - Decoupling del toggle tema della sessione runtime rispetto alla preferenza di default salvata in `APP_SETTINGS`.
     - Guardrail di cancellazione delle azioni distruttive (annullamento di `clear_inventory` e `reset_profile` preserva intatti i dati).
   - `tests/test_e2e_and_invariants.py`: aggiunta della classe `TestDatabaseConstraintsAndOfflineWorkflow` per validare:
     - Integrità referenziale strict con `PRAGMA foreign_keys = ON` e vincoli `NOT NULL` / `CHECK` su `INVENTORY` e `RECIPE_INGREDIENTS`.
     - Workflow end-to-end completo (aggiunta inventario, log scontrino, registrazione prodotto on-the-fly, creazione ricetta, calcolo macro e salvataggio impostazioni) con socket di rete rigorosamente bloccati.

2. **Perfezionamento Import, Tooling e Script**:
   - Sostituzione di tutti i 17 import residui di `from meal_planner.db.recipe_schema import migrate` nei file di test, indirizzandoli direttamente al modulo canonico `meal_planner.db.schema`.
   - Rimozione di `default=True` dal flag mutualmente esclusivo `--drop` in `scripts/demo_receipt.py`.
   - Configurazione di `[tool.pytest.ini_options]` in `pyproject.toml` per esecuzione diretta di `pytest` senza necessità di manipolare `PYTHONPATH`.
   - Hardening di `consume_quantity` in `meal_planner/inventory.py` contro `None`, `bool`, tipi non numerici, float non finiti (`NaN`, `Inf`) e valori non positivi.

3. **Allineamento della Documentazione**:
   - Aggiornato `README.md` includendo `scripts/demo_receipt.py` nell'albero di progetto, referenziando le 6 suite consolidate e correggendo il riferimento ai benchmark in `test_domain_core.py`.
   - Estesa la guida tecnica `docs/GUIDA_FUNZIONAMENTO.md` con la nuova Sezione 12 dedicata a `scripts/setup_project.py` e `scripts/demo_receipt.py`.
