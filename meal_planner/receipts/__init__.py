"""Receipt reading and manual review pipeline."""

from meal_planner.receipts.reader import (
    ItemSpesa,
    Profilo,
    PROFILO_DEFAULT,
    RisultatoScontrino,
    estrai_tuple_da_immagine,
    leggi_scontrino,
)
from meal_planner.receipts.pipeline import (
    FinestraModifica,
    Voce,
    apri_finestra,
    leggi_voci,
    scrivi_out,
    valida_voci,
)

__all__ = [
    "Voce",
    "valida_voci",
    "FinestraModifica",
    "leggi_voci",
    "leggi_scontrino",
    "estrai_tuple_da_immagine",
]
