from typing import List, Tuple
from difflib import SequenceMatcher
from recep_reader import estrai_tuple_da_immagine

ItemSpesa = Tuple[str, float, str, int]


def main():
    immagine_test = "material_fro_leggi_scontrini.jpeg"
    tuple_estratte = estrai_tuple_da_immagine(immagine_test)

    ground_truth: List[ItemSpesa] = [
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
        ("SEITAN BIO", 1.0, "pz", 2)
    ]

    print("\n" + "=" * 75)
    print("REPORT DI CONFRONTO TUPLE: ESTRATTI vs GROUND TRUTH")
    print("=" * 75)

    nomi_corretti = 0
    tuple_complete_corrette = 0

    for idx, atteso in enumerate(ground_truth, 1):
        nome_att, qta_att, unita_att, conf_att = atteso

        miglior_match = None
        max_sim = 0.0
        for item in tuple_estratte:
            n_pulito_est = re.sub(r'[^A-Z0-9]', '', item[0].upper()) if 're' in globals() else ''.join(
                c for c in item[0].upper() if c.isalnum())
            n_pulito_att = ''.join(c for c in nome_att.upper() if c.isalnum())
            sim = SequenceMatcher(None, n_pulito_est, n_pulito_att).ratio()
            if sim > max_sim:
                max_sim = sim
                miglior_match = item

        if miglior_match and max_sim >= 0.60:
            nomi_corretti += 1
            nome_est, qta_est, unita_est, conf_est = miglior_match

            qta_ok = abs(qta_est - qta_att) < 0.01 and unita_est.lower() == unita_att.lower()
            conf_ok = (conf_est == conf_att)

            if qta_ok and conf_ok:
                tuple_complete_corrette += 1
                esito = "[OK COMPLETO]"
            else:
                esito = "[ERRORE QTA/PESO]"

            print(f"{idx:02d}. {esito:17} | Atteso:   {atteso}")
            print(f"    {'':17} | Estratto: {miglior_match} (Sim. nome: {max_sim * 100:.0f}%)")
        else:
            print(f"{idx:02d}. [NON TROVATO]     | Atteso:   {atteso}")

    totale = len(ground_truth)
    print("-" * 75)
    print(f"Totale tuple estratte:               {len(tuple_estratte)}/{totale}")
    print(f"Nomi identificati correttamente:     {nomi_corretti}/{totale} ({(nomi_corretti / totale) * 100:.1f}%)")
    print(
        f"Tuple perfette (Nome + Peso + Pz):   {tuple_complete_corrette}/{totale} ({(tuple_complete_corrette / totale) * 100:.1f}%)")
    print("=" * 75)


if __name__ == "__main__":
    import re

    main()