import json
import sys
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

API = "https://www.boamp.fr/api/explore/v2.1/catalog/datasets/boamp/records"
STATE_FILE = Path(__file__).resolve().parent / "opportunites_seen.json"
DRY_RUN = "--dry-run" in os.sys.argv
WEBHOOK = os.getenv("DISCORD_WEBHOOK", "").strip()

MOTS_CLES = ["nettoyage", "vitrerie", "vitres", "remise en état"]
EXCLUS = [
    "assainissement", "curage", "pompage", "dératisation",
    "fourniture de produits", "tracteur agricole", "pressing",
    "prestations de plonge", "maintenance des engins",
    "graffitis", "poteaux d’arrêt de bus", "nettoyage industriel"
]

def charger_historique():
    if not STATE_FILE.exists():
        return set()
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return set(data.get("sent_ids", []))
    except (json.JSONDecodeError, OSError):
        return set()

def rechercher():
    date_min = (datetime.now(timezone.utc) - timedelta(days=90)).date()
    resultats = {}

    for mot in MOTS_CLES:
        params = {
            "where": (
                f'dateparution >= "{date_min}" '
                f'AND search(objet, "{mot}") '
                'AND code_departement = "13"'
            ),
            "limit": 100,
            "order_by": "dateparution DESC",
            "select": (
                "idweb,objet,dateparution,datelimitereponse,"
                "nomacheteur,code_departement,url_avis"
            ),
        }
        reponse = requests.get(API, params=params, timeout=30)
        reponse.raise_for_status()

        for avis in reponse.json().get("results", []):
            identifiant = avis.get("idweb")
            if identifiant:
                resultats[identifiant] = avis

    maintenant = datetime.now(timezone.utc)
    candidats = []

    for identifiant, avis in resultats.items():
        limite = avis.get("datelimitereponse")
        if not limite:
            continue

        try:
            date_limite = datetime.fromisoformat(
                limite.replace("Z", "+00:00")
            )
        except ValueError:
            continue

        if date_limite <= maintenant:
            continue

        objet = (avis.get("objet") or "").lower()

        # Le titre doit réellement concerner une prestation de nettoyage.
        pertinents = ("nettoyage", "vitrerie", "vitres", "remise en état")
        if not any(mot in objet for mot in pertinents):
            continue

        if any(mot in objet for mot in EXCLUS):
            continue

        avis["_limite"] = date_limite
        candidats.append(avis)

    return sorted(candidats, key=lambda a: a["_limite"])

def lien_avis(avis):
    return avis.get("url_avis") or (
        "https://www.boamp.fr/pages/avis/?q=idweb:"
        + str(avis.get("idweb", ""))
    )


def main():
    print("Recherche des marchés publics pour Klineme...")
    avis = rechercher()
    deja_envoyes = charger_historique()

    if "--init-history" in sys.argv:
        if STATE_FILE.exists():
            print("Historique déjà présent : aucun changement.")
            return

        ids = sorted(a["idweb"] for a in avis if a.get("idweb"))
        STATE_FILE.write_text(
            json.dumps({"sent_ids": ids}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"Historique initialisé : {len(ids)} avis enregistrés.")
        print("Aucun message Discord envoyé.")
        return

    nouveaux = [
        a for a in avis if a.get("idweb") not in deja_envoyes
    ]

    selection = nouveaux[:5]
    paris = ZoneInfo("Europe/Paris")

    lignes = [
        "📊 **Bilan quotidien BOAMP — Klineme**",
        f"Date : {datetime.now(paris).strftime('%d/%m/%Y')}",
        f"Marchés ouverts repérés : {len(avis)}",
        f"Nouvelles opportunités : {len(nouveaux)}",
    ]

    if selection:
        lignes.extend(["", "🔎 **Nouvelles opportunités**"])

        for a in selection:
            limite = a["_limite"].astimezone(paris)
            lignes.extend([
                "",
                f"**{a.get('objet', 'Objet non précisé')}**",
                f"Acheteur : {a.get('nomacheteur') or 'Non précisé'}",
                f"Date limite : {limite.strftime('%d/%m/%Y à %H:%M')} (Paris)",
                lien_avis(a),
            ])
    else:
        lignes.extend([
            "",
            "Aucune nouvelle opportunité aujourd'hui.",
            "La veille continue demain.",
        ])

    message = "\n".join(lignes)
    if len(message) > 1900:
        message = message[:1800] + "\n\nMessage raccourci."

    print(f"Marchés ouverts : {len(avis)}")
    print(f"Nouvelles opportunités : {len(nouveaux)}")

    if "--dry-run" in sys.argv:
        print("\n--- TEST SANS ENVOI DISCORD ---\n")
        print(message)
        print("\nHistorique non modifié.")
        return

    if not WEBHOOK:
        raise RuntimeError(
            "DISCORD_WEBHOOK absent : configure-le dans GitHub Actions."
        )

    reponse = requests.post(
        WEBHOOK,
        json={"content": message},
        timeout=30,
    )
    reponse.raise_for_status()
    print("Bilan Discord envoyé.")

    deja_envoyes.update(a["idweb"] for a in selection)
    STATE_FILE.write_text(
        json.dumps(
            {"sent_ids": sorted(deja_envoyes)},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"Historique mis à jour : {len(selection)} annonce(s) mémorisée(s).")


if __name__ == "__main__":
    main()
