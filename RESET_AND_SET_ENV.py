import ast
import os
import re
import sys
import shutil
import subprocess
from pathlib import Path
from datetime import datetime

# =====================================================================
# CONFIGURAZIONE PERCORSI
# =====================================================================
PROJECT_DIR = Path(__file__).resolve().parent
VENV_DIR = PROJECT_DIR / ".venv"
LOG_FILE = PROJECT_DIR / "setup_environment.log"
REQ_FILE = PROJECT_DIR / "requirements.txt"

# Dizionario di traduzione standard tra il nome usato nell'import Python e il nome reale su PyPI (pip).
# Non è una lista di pacchetti da installare: viene consultato SOLO se il codice importa quella libreria.
MAPPA_IMPORT_TO_PYPI = {
    "cv2": "opencv-python",
    "PIL": "Pillow",
    "sklearn": "scikit-learn",
    "yaml": "PyYAML",
    "bs4": "beautifulsoup4",
    "dotenv": "python-dotenv",
}


# =====================================================================


def log(messaggio: str):
    """Scrive il progresso a schermo e nel file di log."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    riga = f"[{timestamp}] {messaggio}"
    print(riga)
    with open(LOG_FILE, mode="a", encoding="utf-8") as f:
        f.write(riga + "\n")


def trova_python_di_sistema() -> Path:
    """Individua l'eseguibile Python principale del sistema fuori dal .venv."""
    cfg_file = VENV_DIR / "pyvenv.cfg"
    if cfg_file.exists():
        for line in cfg_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.strip().startswith("home"):
                home_dir = Path(line.split("=", 1)[1].strip())
                candidato = home_dir / "python.exe"
                if candidato.exists():
                    return candidato

    base_exec = getattr(sys, "_base_executable", None)
    if base_exec and Path(base_exec).exists() and VENV_DIR not in Path(base_exec).parents:
        return Path(base_exec)

    candidato_prefix = Path(sys.base_prefix) / "python.exe"
    if candidato_prefix.exists():
        return candidato_prefix

    raise RuntimeError("Impossibile localizzare l'interprete Python di sistema fuori dal .venv.")


def sgancia_da_venv_se_attivo():
    """Se lo script è stato avviato usando il Python del .venv, si rilancia col Python di sistema."""
    eseguibile_corrente = Path(sys.executable).resolve()
    if VENV_DIR.resolve() in eseguibile_corrente.parents:
        python_sistema = trova_python_di_sistema()
        print(f"[AUTO-DEACTIVATE] Sgancio da .venv in corso. Rilancio con: {python_sistema}\n")
        env_pulito = os.environ.copy()
        env_pulito.pop("VIRTUAL_ENV", None)
        risultato = subprocess.run([str(python_sistema), str(Path(__file__).resolve())], env=env_pulito)
        sys.exit(risultato.returncode)


def esegui_comando(comando: list, descrizione: str, blocca_su_errore: bool = True) -> bool:
    """Esegue un comando registrando stdout e stderr nel file di log."""
    log(f"Inizio: {descrizione}...")
    processo = subprocess.run(
        comando,
        cwd=str(PROJECT_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace"
    )

    with open(LOG_FILE, mode="a", encoding="utf-8") as f:
        if processo.stdout:
            for linea in processo.stdout.strip().splitlines():
                f.write(f"    [OUT] {linea}\n")
        if processo.stderr:
            for linea in processo.stderr.strip().splitlines():
                f.write(f"    [ERR] {linea}\n")

    if processo.returncode != 0:
        log(f"ERRORE durante '{descrizione}' (Codice: {processo.returncode}).")
        if blocca_su_errore:
            sys.exit(processo.returncode)
        return False

    log(f"Completato: {descrizione}.")
    return True


def scansiona_import_ast_progetto() -> set:
    """
    Analizza attivamente l'albero sintattico (AST) di tutti i file .py del progetto
    (inclusi i blocchi try/except) per trovare tutti i moduli esterni importati.
    """
    moduli_stdlib = set(getattr(sys, "stdlib_module_names", ())) | {"__future__", "typing"}

    # Individua i nomi dei file .py locali del progetto per non scambiarli per librerie pip
    moduli_locali = {
        p.stem for p in PROJECT_DIR.rglob("*.py")
        if VENV_DIR not in p.parents
    }

    moduli_esterni_trovati = set()

    for py_file in PROJECT_DIR.rglob("*.py"):
        if VENV_DIR in py_file.parents or py_file.resolve() == Path(__file__).resolve():
            continue

        try:
            codice = py_file.read_text(encoding="utf-8", errors="ignore")
            albero = ast.parse(codice, filename=str(py_file))
        except SyntaxError as e:
            log(f"Attenzione: errore di sintassi in {py_file.name}, saltato ({e}).")
            continue

        for nodo in ast.walk(albero):
            if isinstance(nodo, ast.Import):
                for alias in nodo.names:
                    radice = alias.name.split(".")[0]
                    moduli_esterni_trovati.add(radice)
            elif isinstance(nodo, ast.ImportFrom) and nodo.module and nodo.level == 0:
                radice = nodo.module.split(".")[0]
                moduli_esterni_trovati.add(radice)

    # Filtra via libreria standard e file locali del progetto
    terze_parti = moduli_esterni_trovati - moduli_stdlib - moduli_locali

    # Traduce i nomi d'import (es. cv2 -> opencv-python)
    pacchetti_pypi = {MAPPA_IMPORT_TO_PYPI.get(m, m) for m in terze_parti}
    return pacchetti_pypi


def scopri_dipendenze_attivamente(venv_pipreqs: Path) -> list:
    """
    Interroga il progetto combinando la scansione di pipreqs con la scansione AST
    e restituisce la lista attiva dei pacchetti da installare.
    """
    log("Avvio ricerca attiva delle dipendenze nei file sorgente del progetto...")

    # 1. Esegue pipreqs sul progetto per generare una prima stesura di requirements.txt
    esegui_comando(
        [
            str(venv_pipreqs),
            str(PROJECT_DIR),
            "--force",
            "--ignore", ".venv",
            "--savepath", str(REQ_FILE)
        ],
        "Scansione progetto tramite pipreqs"
    )

    pacchetti_unificati = {}

    # Legge cosa ha trovato pipreqs
    if REQ_FILE.exists():
        for riga in REQ_FILE.read_text(encoding="utf-8").splitlines():
            riga = riga.strip()
            if not riga or riga.startswith("#"):
                continue
            # Estrae il nome base del pacchetto (prima di ==, >=, ecc.)
            nome_base = re.split(r'[=<>!~]', riga)[0].strip()
            # Normalizza eventuali varianti opencv generate da pipreqs
            if nome_base.lower().startswith("opencv"):
                nome_base = "opencv-python"
            pacchetti_unificati[nome_base.lower()] = nome_base

    # 2. Integra con la scansione AST (cattura anche import dentro try/except ignorati da pipreqs)
    da_ast = scansiona_import_ast_progetto()
    for pkg in da_ast:
        if pkg.lower() not in pacchetti_unificati:
            log(f"  [AST Scanner] Rilevato import aggiuntivo non catturato da pipreqs: '{pkg}'")
            pacchetti_unificati[pkg.lower()] = pkg

    lista_finale = list(pacchetti_unificati.values())
    log(f"Ricerca completata. Pacchetti rilevati nel codice ({len(lista_finale)}): {', '.join(lista_finale)}")
    return lista_finale


def main():
    sgancia_da_venv_se_attivo()

    if LOG_FILE.exists():
        LOG_FILE.unlink()

    log("=== AVVIO RESET .VENV E RICERCA ATTIVA DIPENDENZE ===")
    python_sistema = trova_python_di_sistema()
    log(f"Progetto: {PROJECT_DIR}")
    log(f"Python di sistema: {python_sistema}")

    # 1. ELIMINAZIONE VECCHIO .venv
    if VENV_DIR.exists():
        log("Eliminazione della cartella '.venv' esistente...")
        shutil.rmtree(VENV_DIR)
        log("Vecchio .venv eliminato.")

    # 2. CREAZIONE NUOVO .venv
    esegui_comando(
        [str(python_sistema), "-m", "venv", str(VENV_DIR)],
        "Creazione nuovo .venv pulito"
    )

    venv_python = VENV_DIR / "Scripts" / "python.exe"
    venv_pipreqs = VENV_DIR / "Scripts" / "pipreqs.exe"

    # 3. AGGIORNAMENTO PIP E INSTALLAZIONE DI PIPREQS PER LA SCANSIONE
    esegui_comando(
        [str(venv_python), "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel", "pipreqs"],
        "Aggiornamento pip e installazione dello scanner 'pipreqs'"
    )

    # 4. RICERCA ATTIVA DEI PACCHETTI USATI NEL CODICE DEL PROGETTO
    pacchetti_scoperti = scopri_dipendenze_attivamente(venv_pipreqs)

    # 5. INSTALLAZIONE DEI PACCHETTI SCOPERTI E SCRITTURA LOG
    successi = []
    falliti = []
    for idx, pacchetto in enumerate(pacchetti_scoperti, 1):
        ok = esegui_comando(
            [str(venv_python), "-m", "pip", "install", pacchetto],
            f"[{idx}/{len(pacchetti_scoperti)}] Download e installazione di '{pacchetto}'",
            blocca_su_errore=False
        )
        if ok:
            successi.append(pacchetto)
        else:
            falliti.append(pacchetto)

    # 6. RIGENERAZIONE FINALE DI requirements.txt CON VERSIONI CONSOLIDATE
    # Ora che i pacchetti sono installati nel .venv, pipreqs può interrogare il .venv
    # per scrivere i numeri di versione reali nel file requirements.txt
    esegui_comando(
        [
            str(venv_pipreqs),
            str(PROJECT_DIR),
            "--force",
            "--ignore", ".venv",
            "--savepath", str(REQ_FILE)
        ],
        "Generazione finale di requirements.txt tramite pipreqs"
    )

    # Assicura che anche i pacchetti trovati via AST (dentro try/except) figurino in requirements.txt
    contenuto_req = REQ_FILE.read_text(encoding="utf-8") if REQ_FILE.exists() else ""
    nomi_gia_scritti = {
        re.split(r'[=<>!~]', r.strip())[0].lower()
        for r in contenuto_req.splitlines() if r.strip()
    }
    with open(REQ_FILE, mode="a", encoding="utf-8") as rf:
        for pkg in successi:
            if pkg.lower() not in nomi_gia_scritti:
                rf.write(f"{pkg}\n")

    log("=== RIEPILOGO FINALE ===")
    log(f"Pacchetti scoperti ed installati con successo: {len(successi)}/{len(pacchetti_scoperti)} ({', '.join(successi)})")
    if falliti:
        log(f"Pacchetti falliti: {', '.join(falliti)}")
    log(f"File requirements.txt salvato in: {REQ_FILE}")
    log(f"Log completo salvato in: {LOG_FILE}")


if __name__ == "__main__":
    main()