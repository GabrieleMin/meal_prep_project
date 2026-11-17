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
12. [Fase 11: Ristrutturazione Cartella e Consolidamento Test](#12-fase-11-ristrutturazione-cartella-e-consolidamento-test)
13. [Fase 12: Hardening Test Suite Consolidate, Risoluzione Edge Cases e Allineamento Documentazione (Gen 4)](#13-fase-12-hardening-test-suite-consolidate-risoluzione-edge-cases-e-allineamento-documentazione-gen-4)
14. [Fase 13: Eliminazione Ridondanza Tab Ricette, RecipeDetailsDialog e UX Inserimento Rapido](#14-fase-13-eliminazione-ridondanza-tab-ricette-recipedetailsdialog-e-ux-inserimento-rapido)
15. [Fase 14: Risoluzione UX Eventi Ricette, Centratura Modali, Avvio su Dispensa, Chiarimento "Q.b." e Traduzione Completa in Inglese](#15-fase-14-risoluzione-ux-eventi-ricette-centratura-modali-avvio-su-dispensa-chiarimento-qb-e-traduzione-completa-in-inglese)
16. [Fase 15: Risoluzione Bug Toggle Light Mode "X", Restyling Soft-Badge Antialiased, Ottimizzazioni Sistematiche di Efficienza e Localizzazione UI 100% Inglese](#16-fase-15-risoluzione-bug-toggle-light-mode-x-restyling-soft-badge-antialiased-ottimizzazioni-sistematiche-di-efficienza-e-localizzazione-ui-100-inglese)

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

---

## 14. Fase 13: Eliminazione Ridondanza Tab Ricette, RecipeDetailsDialog e UX Inserimento Rapido

### Obiettivo della Fase:
Eliminare la ridondanza ergonomica nella scheda Ricette (presenza contemporanea di un form laterale statico obsoleto a sinistra e del pulsante modale `Nuova ricetta...` in basso), espandere a tutta larghezza la tabella di consultazione ricette, introdurre un visualizzatore di dettaglio non distruttivo (`RecipeDetailsDialog`), velocizzare l'inserimento rapido tramite tastiera e risolvere problemi di firma nel decoratore `@safe`.

### Azioni Implementate:
1. **Rimozione del Form Laterale Statico e Layout Tabella a Tutta Larghezza ([`meal_planner/gui/app.py`](file:///home/gabriele/Desktop/meal_prep/meal_prep_project/meal_planner/gui/app.py))**:
   - Rimosso il pannello laterale `self.f_ing` / `self.t_draft`, residuo delle prime versioni dell'applicazione privo di supporto a porzioni scalabili, note di preparazione e calcolo live dei macronutrienti.
   - Espansa la tabella ricette `self.t_ric` al 100% della larghezza della scheda con colonne dimensionate generosamente (`Ricetta`: 240px, `kcal`, `Prot`, `Carb`, `Fat`, `Fiber`: 65px cad., `Aperto`: 65px, `Disponibilità / Outcome`: 320px), eliminando qualsiasi troncamento orizzontale.
   - Aggiunta una barra degli strumenti dedicata in basso con pulsanti chiari:
     - `➕ Nuova ricetta...` (apre `AddRecipeDialog`)
     - `👁️ Dettagli ricetta...` (apre `RecipeDetailsDialog`)
     - `📂 Importa da file (.json / .csv)...`
     - `🗑️ Elimina ricetta selezionata`
   - Configurato il binding `<Double-1>` su `self.t_ric` per aprire istantaneamente la vista di dettaglio della ricetta con doppio clic.
   - Resi resilienti i metodi legacy `add_ingredient`, `remove_ingredient`, `save_recipe`, `show_draft` e `refresh` con guardie `hasattr(self, 'f_ing')`.

2. **Nuova Finestra Modale di Dettaglio Ricetta ([`RecipeDetailsDialog` in `meal_planner/gui/dialogs.py`](file:///home/gabriele/Desktop/meal_prep/meal_prep_project/meal_planner/gui/dialogs.py))**:
   - Finestra di consultazione pulita e non distruttiva che visualizza:
     - Nome ricetta e pasti associati (`Colazione`, `Pranzo`, `Cena`, `Spuntino`).
     - Note di preparazione complete.
     - Tabella dettagliata degli ingredienti (Nome, Quantità, Unità, Indicatore `Q.b.`).
     - Riepilogo macronutrienti calcolati per porzione (`kcal`, proteine, carboidrati, grassi, fibre) tramite query su `RECIPE_MACROS_VIEW`.
   - Supporto headless e non-blocking (`wait: bool = True`).

3. **Flusso di Inserimento Tastiera ad Alta Ergonomia ([`AddRecipeDialog` in `meal_planner/gui/dialogs.py`](file:///home/gabriele/Desktop/meal_prep/meal_prep_project/meal_planner/gui/dialogs.py))**:
   - Focus iniziale automatico sul campo nome ricetta `e_name`.
   - `Invio` su `e_name` sposta direttamente il focus sulla combobox degli ingredienti `cb_ingredient`.
   - `Invio` su `cb_ingredient` o `e_quantity` aggiunge immediatamente l'ingrediente alla ricetta senza richiedere click del mouse.
   - Dopo l'aggiunta di un ingrediente, i campi quantità e spunta "Q.b." si azzerano e il focus ritorna automaticamente sulla combobox ingredienti per digitare in sequenza.
   - Tasti `Canc` e `Backspace` sulla tabella ingredienti rimuovono l'ingrediente selezionato.
   - Scorciatoia globale `Ctrl+Invio` o `Ctrl+S` per salvare immediatamente la ricetta.
   - Gestione smart "Quanto basta": se "Q.b." è spuntato e la quantità è vuota, assegna automaticamente `1.0` come valore di default senza mostrare errori.

4. **Correzione del Decoratore `@safe` ([`meal_planner/gui/support.py`](file:///home/gabriele/Desktop/meal_prep/meal_prep_project/meal_planner/gui/support.py))**:
   - Aggiornato `def safe(function)` per accettare `*args, **kwargs` all'interno della closure `inner(self, *args, **kwargs)`.
   - Risolto il `TypeError` che si verificava quando metodi decorati venivano invocati con parametri con nome (es. `wait=False`).

5. **Estensione Test Suite e Rigenerazione Screenshot**:
   - Aggiunti test mirati in `tests/test_recipe_management.py`:
     - `test_app_recipe_details_and_full_width_table`
     - `test_add_recipe_dialog_keyboard_shortcuts_and_refocus`
   - Totale test: **416 / 416 passati** su tutte le 6 suite tematiche (100% offline).
   - Rigenerati tutti i 16 screenshot in `docs/images/` via `scripts/capture_docs_screenshots.py`, aggiornando la vista di `05_recipes_tab.png`.

---

## 15. Fase 14: Risoluzione UX Eventi Ricette, Centratura Modali, Avvio su Dispensa, Chiarimento "Q.b." e Traduzione Completa in Inglese

### Problematiche Affrontate:
1. **Pulsanti Ricette Percepiti Disconnessi**:
   - `open_add_recipe_dialog` non era decorato con `@safe`, rischiando eccezioni silenziose.
   - I dialoghi `AddRecipeDialog` e `RecipeDetailsDialog` non chiamavano `self.lift()`, `self.focus_force()` né centravano la geometria rispetto alla finestra principale, posizionandosi dietro la finestra principale su alcuni window manager sotto Linux e intrappolando gli eventi con `grab_set()`.
   - `open_recipe_details` mostrava un popup di avviso bloccante quando nessun elemento era pre-selezionato nella tabella.
   - Il timer `after(100, self._set_initial_focus)` in `AddRecipeDialog` non veniva cancellato alla chiusura, producendo errori nel terminale.
2. **Scheda di Avvio Iniziale Errata**:
   - Durante `App.__init__`, la costruzione di `tab_settings()` invocava `self.switch_settings_section("style_and_config")`, che a sua volta selezionava la scheda delle impostazioni (`Setting` / `Settings`), sottraendo il focus iniziale alla scheda della dispensa (`Pantry`).
3. **Mancanza di Chiarezza sul Termine Culinario "Q.b."**:
   - La spunta e l'intestazione colonna recavano unicamente la sigla "Q.b.", incomprensibile per utenti non a conoscenza della dicitura "quanto basta" e del suo utilizzo per ingredienti a dosaggio discrezionale.
4. **Richiesta di Traduzione Completa in Inglese ("Traduci tutto in inglese")**:
   - Tutti gli elementi dell'interfaccia grafica (schede notebook, pulsanti, titoli dialoghi, intestazioni tabelle, etichette form, messaggi di alert/conferma e overlay stato vuoto profilo) sono stati tradotti in lingua inglese.

### Azioni Implementate:
1. **Correzione Eventi e Ciclo di Vita Dialoghi Ricette (`meal_planner/gui/app.py` e `dialogs.py`)**:
   - Decorato `open_add_recipe_dialog` con `@safe`.
   - In `open_recipe_details`: se nessuna riga è selezionata, seleziona automaticamente la prima ricetta disponibile nella tabella e procede con l'apertura; se la tabella è vuota, notifica l'utente in modo chiaro.
   - Collegato il doppio clic della tabella `Table(..., on_double_click=self._on_recipe_row_double_click)`.
   - Introdotta la funzione `_center_and_lift(dialog, master)` che calcola le coordinate esatte per centrare la finestra modale rispetto al master (o allo schermo), applicando `self.lift()` e `self.focus_force()`.
   - Tracciamento e cancellazione pulita del timer `_focus_timer` nei metodi `destroy()` e `_on_cancel()` di `AddRecipeDialog`.
2. **Selezione Predefinita della Scheda Dispensa (`Pantry`) all'Avvio**:
   - Aggiunto il parametro `select_tab: bool = True` a `switch_settings_section(self, section: str, select_tab: bool = True)`.
   - Durante la costruzione interna in `tab_settings()`, invocato `self.switch_settings_section("style_and_config", select_tab=False)`, evitando il cambio anticipato di scheda.
   - Forzata esplicitamente la selezione di `self.nb.select(0)` al termine dell'inizializzazione di `App.__init__`.
3. **Chiarimento Esplicito di "To taste (q.b.)"**:
   - In `AddRecipeDialog`: casella di spunta rinominata in `"To taste (q.b.)"`, aggiunta nota di guida sottostante per ingredienti discrezionali (sale, pepe, spezie, erbe), colonna tabella `"To taste"`, valori `"Yes"` / `"No"`.
   - In `RecipeDetailsDialog`: colonna tabella `"To taste (q.b.)"`, valori `"Yes"` / `"No"`, e nota a piè di tabella:
     `"ℹ️ 'To taste' (q.b. / quanto basta): ingredients added as needed to taste without fixed nutritional weight (e.g. salt, pepper, spices)."`
4. **Localizzazione Completa della GUI in Lingua Inglese**:
   - Schede notebook: `"Pantry"`, `"Shopping"`, `"Recipes"`, `"Plan"`, `"Profile"`, `"Settings"` (con piena retrocompatibilità per lookup e chiamate a `refresh(active_tab=...)`).
   - Pulsanti e barre degli strumenti: `"➕ New Recipe..."`, `"👁️ Recipe Details..."`, `"📂 Import from file (.json / .csv)..."`, `"🗑️ Delete Selected Recipe"`, `"➕ Add Profile"`, `"Cancel"`, `"Save Recipe"`, `"Close"`.
   - Intestazioni tabelle e form: `"Recipe"`, `"Feasibility / Outcome"`, `"Ingredient"`, `"Quantity"`, `"Unit"`, `"To taste"`, `"Fat (g)"`.
   - Overlay stato vuoto profilo: `"Hello, add your profile to get started"`, `"Set up your profile to calculate caloric needs and start planning meals."`, `"➕ Add Profile"`.
   - Dialoghi `ProfileSetupDialog` e `QuickProductDialog`: titoli, etichette campi e messaggi di validazione/errore completamente in inglese.
5. **Allineamento Suite di Test**:
   - Aggiornate le asserzioni in `test_settings_and_profile.py`, `test_e2e_and_invariants.py` e `test_recipe_management.py` per validare le stringhe in inglese o supportare in modo bilingue le versioni legacy.
   - Aggiunti 5 nuovi casi di test verificanti:
     - Apertura predefinita all'avvio sulla scheda 0 (`"Pantry"`).
     - Apertura affidabile, centratura, focus e distruzione pulita di `AddRecipeDialog` e `RecipeDetailsDialog`.
     - Presenza e chiarezza della spiegazione `"To taste (q.b.)"` in entrambi i dialoghi.
     - Auto-selezione della prima riga e doppio clic su riga ricetta.

---

## 16. Fase 15: Risoluzione Bug Toggle Light Mode "X", Restyling Soft-Badge Antialiased, Ottimizzazioni Sistematiche di Efficienza e Localizzazione UI 100% Inglese

Questa fase ha affrontato le due richieste chiave della sessione: la risoluzione del bug visivo dei pulsanti di eliminazione "X" e il loro ammodernamento estetico (Attività 1), seguita da un audit esteso e approfondito dell'intera architettura del software per implementare ottimizzazioni di efficienza senza alcuna alterazione funzionale e completare la traduzione al 100% in lingua inglese (Attività 2).

### 1. Risoluzione Bug "X" e Restyling Grafico Antialiased (Milestone M1)

#### Problematiche Rilevate:
1. **Scomparsa Visiva dei Pulsanti "X" al Cambio Tema**:
   - Nella tabella della dispensa (`Table` in `meal_planner/gui/widgets.py`), il metodo `update_delete_icon(self, is_dark=False)` rigenerava una nuova istanza di `PhotoImage` assegnandola a `self._delete_icon`. Tuttavia, l'immagine non veniva propagata alle righe esistenti della `ttk.Treeview`.
   - Quando il garbage collector di Python distruggeva il vecchio oggetto `PhotoImage`, Tkinter inviava il comando Tcl `image delete <nome_immagine>`. Le righe della tabella mantenevano un puntatore pendente all'identificativo Tcl distrutto, rendendo l'icona invisibile pur lasciando intatti i click handler.
2. **Disallineamento Iniziale del Tema**:
   - `Table.__init__` chiamava `update_delete_icon()` senza passare lo stato del tema, assumendo `is_dark=False` anche se l'applicazione si avviava in modalità scura (`dark`).
3. **Estetica Datata e Non Antialiased**:
   - Le icone "X" precedenti venivano tracciate pixel per pixel senza anti-aliasing con bordi rigidi e colori rosso acceso sgradevoli (`#991b1b`, `#7f1d1d`, `#b91c1c`, `#ef4444`) che stridevano con il look moderno di `sv-ttk`.

#### Azioni Implementate:
1. **Propagazione Dinamica alle Righe Esistenti**:
   - In `Table.update_delete_icon`: dopo la creazione del nuovo `PhotoImage`, il metodo itera attraverso tutti gli elementi correnti della tabella via `self.tv.get_children("")` e aggiorna ciascuna riga invocando `self.tv.item(child, image=self._delete_icon)`.
   - Garantita la persistenza visiva e la validità dell'icona Tcl durante qualsiasi transizione di tema (`dark` ↔ `light`).
2. **Rilevamento Automatico del Tema Attivo**:
   - `Table.__init__` accetta ora `is_dark: bool | None = None` e verifica `sv_ttk.get_theme() == "dark"` (con fallback su `theme.load()`), garantendo che fin dal primo rendering la tabella utilizzi la palette corretta.
   - In `Table.fill()` e `_ensure_delete_icon`, il tema attivo viene mantenuto coerentemente.
3. **Anti-Aliasing con Supersampling 4x e Palette Soft Coral/Rose**:
   - La funzione `create_delete_icon` in `meal_planner/gui/widgets.py` è stata riscritta utilizzando un rendering con **supersampling 4x**: l'icona viene disegnata su una superficie Pillow a 4x di risoluzione (80x80 pixel per un target di 20x20) e successivamente riscalata con filtro `Image.Resampling.LANCZOS`.
   - Adottata una raffinata palette "soft coral/rose" che si armonizza naturalmente con entrambi i temi:
     - **Dark Mode**: sfondo badge `#2d1f22`, bordo `#52272e`, croce `#f87171`.
     - **Light Mode**: sfondo badge `#fbebec`, bordo `#f0b8bd`, croce `#c94a53`.
   - Il risultato è un badge moderno con angoli stondati perfettamente levigati e tratti diagonali privi di artefatti o scalettature.

---

### 2. Ottimizzazioni Sistematiche di Efficienza Backend e Core (Milestone M2)

È stata condotta un'analisi sistematica "componente per componente" su tutto il codice di backend (`config`, `db`, `engine`, `inventory`, `recipes`, `planner`, `receipts`), ponendosi la domanda: *"È possibile implementare questo meccanismo in modo più efficiente senza modificarne assolutamente la funzionalità?"*.

#### Azioni Implementate:
1. **In-Memory Configuration Caching (`meal_planner/config.py`)**:
   - Le letture tramite `config.get(key, default)` eseguivano ripetutamente una connessione SQLite, un `CREATE TABLE IF NOT EXISTS` e una query `SELECT`.
   - Introdotta una cache thread-safe in memoria (`_CONFIG_LOCK`, `_CACHED_CONFIG`, `_CACHED_DB_PATH`). Le letture successive sono trasformate in lookup O(1) in memoria, riducendo drasticamente I/O e roundtrip. L'invalidazione della cache avviene automaticamente in caso di scrittura (`save_config`, `set_key`).
2. **Ottimizzazione Connessioni SQLite (`meal_planner/db/connection.py`)**:
   - Evitati controlli I/O ridondanti su filesystem: la verifica e creazione della directory del database (`os.path.isdir`, `os.makedirs`) viene tracciata in un set in memoria (`_VERIFIED_DIRS`) per non ripeterla ad ogni singola connessione.
   - Tracciamento dei percorsi con WAL abilitato (`_WAL_CONFIGURED_PATHS`): `PRAGMA journal_mode = WAL;` viene eseguito solo quando necessario e non a ogni apertura.
3. **Ispezione Batch dello Schema (`meal_planner/db/schema.py`)**:
   - La creazione dello schema (`create_schema`) eseguiva 10 chiamate individuali a `PRAGMA table_info` per verificare l'esistenza di singole colonne.
   - Ottimizzato raggruppando l'ispezione delle colonne esistenti in una sola query per tabella (`PRODUCTS`, `INVENTORY`, `PROFILE`), riducendo le query da 10 a 3.
4. **Indicizzazione Diretta Catalogo Prodotti (`meal_planner/engine/catalog.py`)**:
   - In `load_products`: eliminata la chiamata ripetuta a `dict(zip(MACROS, r[4:]))` per ogni alimento, sostituita con costruzione diretta tramite indici numerici della tupla SQLite, riducendo l'overhead di allocazione e iterazione.
5. **Fattorizzazione Divisioni Nutrizionali (`meal_planner/engine/nutrition.py`)**:
   - Nel calcolo dei macronutrienti di una ricetta (`recipe_macros`), la divisione per 100g (`g / 100.0`) veniva ripetuta 8 volte all'interno del loop dei macro per ciascun ingrediente.
   - Fattorizzata la divisione: `factor = g / 100.0` calcolato una sola volta per ingrediente ed applicato nel ciclo interno, eliminando 7 divisioni in virgola mobile per alimento.
6. **Scritture Batch in Inventario Virtuale (`meal_planner/engine/virtual_inventory.py`)**:
   - In `VirtualInventory.write`: le cancellazioni e gli aggiornamenti delle righe d'inventario venivano eseguiti con chiamate singole `conn.execute(...)`.
   - Raggruppate le operazioni in liste ed eseguite in batch con `conn.executemany`, riducendo le transazioni e i cambi di contesto SQLite.
7. **Aggiornamento In-Place nel Consumo Scorte (`meal_planner/inventory.py`)**:
   - In `consume_quantity`: ad ogni passaggio di consumo veniva riallocata una nuova lista tramite list comprehension per aggiornare la tupla della riga. Sostituita con modifica in-place `rows[idx] = (...)`.
8. **Scansione Singolo Passaggio per Riconciliazione Catalogo (`meal_planner/inventory.py`)**:
   - In `get_catalog_for_reconciliation`: era presente una subquery correlata O(N²) `WHERE a.rowid = (SELECT MIN(...) FROM ALIASES ...)` e una query ridondante `PRAGMA table_info(PRODUCTS)`.
   - Sostituita con una singola scansione indicizzata `SELECT ID, Name FROM ALIASES ORDER BY rowid ASC` con memorizzazione O(1) tramite `canonical_names.setdefault(pid, name)`.
9. **Query UNION ALL per Verifica Ingredienti Noti (`meal_planner/recipes.py`)**:
   - `is_ingredient_known` eseguiva due distinte query sequenziali su `ALIASES` e `SUBSTITUTE_GROUPS`.
   - Consolidate in una singola query con `UNION ALL ... LIMIT 1`.
10. **Pre-Caching nell'Importazione Massiva Ricette (`meal_planner/recipes.py`)**:
    - In `import_file`: durante l'importazione di ricette con centinaia di ingredienti, venivano effettuate query ripetute per ciascun ingrediente.
    - Implementato il pre-caching di alias e gruppi sostitutivi in dizionari in memoria passati a `create_recipe`, rendendo l'importazione quasi istantanea.
11. **Bulk Query Ingredienti nel Planner Giornaliero e Settimanale (`meal_planner/planner/day.py`, `storage.py`)**:
    - Risolto il classico problema N+1 in `plan_day` e `_apply_plan`, dove gli ingredienti di ogni ricetta venivano estratti con query individuali in loop.
    - Introdotta un'unica query bulk `SELECT Recipe_ID, ID, Group_ID, Quantity, Unit, To_Taste FROM RECIPE_INGREDIENTS ...` con raggruppamento in memoria per `Recipe_ID`.
12. **Pre-Compilazione Regex e Pre-Normalizzazione Catalogo OCR (`meal_planner/receipts/reader.py`)**:
    - Pre-compilato a livello di modulo il pattern regex di normalizzazione caratteri `P_NORM_NOME = re.compile(r'[^A-Z0-9 ]')`.
    - In `correggi_con_catalogo`: il catalogo delle voci note viene pre-normalizzato una sola volta all'avvio della scansione anziché ripetere la normalizzazione su ogni articolo rilevato nello scontrino.

---

### 3. Ottimizzazioni di Efficienza GUI e Localizzazione 100% Inglese (Milestone M3)

#### Ottimizzazioni GUI:
1. **Caching degli Stili e Icone nella Riconciliazione Scontrini (`ReceiptReconciliationDialog` in `meal_planner/gui/dialogs.py`)**:
   - Ad ogni apertura del dialogo venivano generate 26 immagini PIL e rispettivi `PhotoImage` per gli stili di evidenziazione lampeggiante (`_init_rounded_styles`), per poi distruggerli alla chiusura.
   - Introdotti stili condivisi memorizzati in `_RECONCILIATION_FLASH_STYLES` indicizzati per istanza Tkinter e tema (`(id(self.tk), self.is_dark)`), eliminando completamente la ri-generazione grafica ad ogni apertura.
2. **Binding Mousewheel a Livello di Contenitore (`ReceiptReconciliationDialog`)**:
   - In precedenza venivano registrati eventi di scroll `<MouseWheel>`, `<Button-4>`, `<Button-5>` su 12 singoli widget per ogni riga della tabella scontrino.
   - Sostituito legando gli eventi di scroll unicamente a livello di container (`self`, `container`, `canvas`, `table_frame`), sfruttando la naturale propagazione degli eventi Tkinter e riducendo decine di handler ridondanti.
3. **Consolidamento Connessioni SQLite in `AddRecipeDialog` (`meal_planner/gui/dialogs.py`)**:
   - I metodi `_load_meals`, `_load_known_ingredients` e il calcolo dei macro aprivano e chiudevano singole connessioni separate durante l'inizializzazione.
   - Parametrizzate le funzioni per accettare una connessione aperta preesistente (`conn=conn`).
4. **Debouncing e Pre-Calcolo Ordinali in `LineChart` (`meal_planner/gui/widgets.py`)**:
   - Ad ogni ridisegno del grafico del peso, le stringhe data ISO venivano re-parsate con `date.fromisoformat(d).toordinal()`.
   - Pre-calcolati gli ordinali una sola volta in `plot()` e riutilizzati in `draw()`.
   - L'evento di ridimensionamento finestra `<Configure>` è stato disaccoppiato tramite debouncing con `self.after_idle(self._debounced_draw)` e cancellazione dei job pendenti (`after_cancel`), eliminando qualsiasi lag durante il resizing della finestra.
5. **Simulazione Differita delle Ricette al Refresh (`meal_planner/gui/app.py`)**:
   - In precedenza, ogni chiamata a `app.refresh()` (es. aggiunta/consumo scorte in dispensa, aggiornamento lista spesa, modifica profilo) eseguiva la simulazione completa di tutte le ricette con `self.recipe_rows(c)`.
   - Introdotto il flag `self._recipes_dirty`: se l'utente si trova su schede diverse da "Recipes" (es. "Pantry", "Shopping", "Profile"), la simulazione pesante viene differita; il ricalcolo effettivo scatta solo quando l'utente passa alla scheda "Recipes".
6. **Bufferizzazione dei Log Scontrini (`meal_planner/gui/app.py`)**:
   - Sostituita la concatenazione continua di stringhe `self._receipt_log_text += text` con un buffer a lista `self._receipt_log_buffer.append(text)`, esponendo `_receipt_log_text` come `@property` per retrocompatibilità.

#### Localizzazione al 100% in Inglese:
1. **Fallback Ingrediente Ricetta**:
   - In `meal_planner/gui/dialogs.py` (`RecipeDetailsDialog`), corretto `'Prodotto ' || ri.ID` in `'Product ' || ri.ID`.
2. **Label Stato Nutrizionale**:
   - In `meal_planner/planner/cli.py`, corretto `STATUS_LABELS["ok"] = "within limits"` (era `"nei limiti"`).
3. **Log di Avviso Planner**:
   - In `meal_planner/planner/cli.py`, corretto `print("Warning:", ...)` (era `"Attenzione:"`).
4. **Navigazione Scheda Profilo**:
   - In `meal_planner/gui/app.py` (`open_profile_setup_dialog`), corretto `self.refresh(active_tab="Profile")` (era `"Profilo"`).
5. **Messaggio di Errore OCR Immagine Scontrino**:
   - In `meal_planner/gui/app.py` (`_on_receipt_read_completed`), mappato il messaggio visualizzato a video da `"Impossibile caricare l'immagine"` a `"Unable to load image"`.

---

### 4. Test Consolidati, Invarianti e Risultati di Verifica (Milestone M4)

- **Test Suite Consolidate**:
  - Esecuzione completa senza errori di tutte le suite di test con `scripts/setup_project.py --test` e `.venv/bin/python -m unittest`.
  - Nuova suite mirata `tests/test_e2e_theme_and_efficiency.py` (32 test passati, 0 skipped).
  - Nuova suite mirata `tests/test_gui_efficiency_and_localization.py` (10 test passati).
  - Totale complessivo: **472 test passati su 472**, 0 errori, 0 fallimenti, 0 skipped, 100% offline.
- **Invarianza Database Golden Baseline (`data/inventory.db`)**:
  - `INVENTORY`: 36 righe
  - `PRODUCTS`: 14 righe
  - `RECIPES`: 3 righe
  - `ALIASES`: 30 righe
  - `PROFILE`: 0 righe
  - `APP_SETTINGS`: 3 righe
  - `PRAGMA integrity_check`: `ok`
  - `PRAGMA foreign_key_check`: 0 violazioni.

---

## 17. Fase 16: Pipeline Ibrida di Generazione Ricette (Slot + Food Pairing + Ottimizzatore ILP/CSP) e Integrazione GUI

### Obiettivo della Fase:
Progettare e implementare un sistema completo e 100% offline per la generazione intelligente di ricette (`meal_planner/generator/`) basato sulla sinergia tra grammatica culinaria a slot, grafo di Food Pairing e programmazione lineare vincolata (`scipy.optimize.linprog`), integrandolo nella scheda Recipes tramite la finestra modale `RecipeGeneratorDialog`.

### Azioni Implementate:
1. **Motore di Tassonomia e Macro Fingerprinting (`meal_planner/generator/taxonomy.py`)**:
   - Classificazione ibrida a 2 livelli: dizionario semantico canonico (Livello A) + classificatore matematico automatico sui macronutrienti per 100g (Livello B) per qualsiasi alimento personalizzato aggiunto dall'utente.
   - Supporto multi-ruolo contestuale e limiti fisiologici di porzione per ogni categoria gastronomica.
2. **Architettura a Slot e Archetipi Culinari (`meal_planner/generator/slots.py`)**:
   - Definiti 4 archetipi strutturati (`Grain / Pasta Dish`, `Main & Side Plate`, `One-Pot Balanced Bowl`, `Breakfast / Snack Bowl`) con slot obbligatori e opzionali, cardinalità e validazione rigorosa dei bound.
3. **Grafo di Food Pairing ed Esclusioni Rigide (`meal_planner/generator/food_pairing.py`)**:
   - Implementate 17 regole di esclusione mutua rigida (es. incompatibilità tra pesce e latticini dolci, esclusione di grassi da cucina salati nelle colazioni dolci, farina cruda in preparazioni a freddo).
   - Matrice di affinità aromatica a 4 livelli per ordinare e premiare le combinazioni di ingredienti più armoniose.
4. **Risolutore Matematico ILP/CSP (`meal_planner/generator/solver.py`)**:
   - Risoluzione esatta delle grammature tramite `scipy.optimize.linprog(method='highs')` con convergenza entro $\pm 5\%$ del target calorico e fallback knapsack deterministico in puro Python.
   - Funzione obiettivo calibrata per massimizzare il consumo di confezioni già aperte (`Open_Remainder > 0`) e prodotti in scadenza (FIFO), penalizzando gli ingredienti mancanti.
5. **Orchestratore Pipeline e Sintesi Culinaria (`meal_planner/generator/pipeline.py`)**:
   - Estrazione candidati dalla dispensa SQLite e dal catalogo prodotti, generazione combinazioni, sintesi automatica del titolo e delle istruzioni di cottura in 4 passaggi, e supporto alla rotazione delle varianti (`variation_index`).
6. **Integrazione GUI (`meal_planner/gui/app.py`, `meal_planner/gui/dialogs.py`)**:
   - Aggiunto il pulsante `✨ Generate Recipe...` nella barra degli strumenti della scheda Recipes.
   - Creata la finestra modale `RecipeGeneratorDialog` con selettori di pasto, archetipo, target kcal, modalità Pantry Only, pulsante `🎲 Regenerate / Try Another`, anteprima ingredienti/macro e salvataggio diretto in SQLite tramite `create_recipe`.
7. **Suite di Test Dedicata e Stress-Testing di Consistenza Logica (`tests/test_recipe_generator.py`)**:
   - **75 test** unitari, avversariali, di stress e di coerenza logica end-to-end (`TestGeneratorStressAndLogicalConsistency`), registrati anche in `scripts/setup_project.py`.
   - Risolte e verificate 5 sottili inconsistenze logiche:
     1. Allineamento post-quantizzazione (`quantized_grams()` e `sync_macros_with_display_quantities()`) per garantire identità matematica tra i macro di `RecipeCandidate` e la vista SQLite `RECIPE_MACROS_VIEW`.
     2. Rispetto del limite inferiore `min_g` nel solutore anche per gli ingredienti `to_taste` selezionati nello slot, eliminando ingredienti con peso `0.0g`.
     3. Isolamento tramite clonazione dei candidati nel loop combinatorio e indicizzazione posizionale `quantities_by_idx` per evitare collisioni su `product_id=0`.
     4. Controllo di unicità canonica (`resolve_canonical`) tra slot diversi della stessa ricetta (impedendo formati multipli dello stesso ingrediente canonico nello stesso piatto).
     5. Coerenza semantica delle istruzioni di cottura per piatti privi di carboidrati e distinzione tra salse liquide e verdure fresche da tagliare.
   - Tutti i **547 test** complessivi del progetto passano al 100% offline mantenendo intatta la baseline del database `data/inventory.db`.



