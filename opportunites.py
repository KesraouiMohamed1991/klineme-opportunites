
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

API = "https://www.boamp.fr/api/explore/v2.1/catalog/datasets/boamp/records"
STATE_FILE = Path(__file__).resolve().parent / "opportunites_seen.json"

DRY_RUN = "--dry-run" in sys.argv
INIT_HISTORY = "--init-history" in sys.argv

WEBHOOK = os.getenv("DISCORD_WEBHOOK", "").strip()
PARIS = ZoneInfo("Europe/Paris")

MAX_ALERTS = 4
MAX_CHARS = 1800
REQUEST_TIMEOUT = 30

MOTS_CLES = ["nettoyage", "vitrerie", "vitres", "remise en état"]

EXCLUS = [
    "assainissement",
    "curage",
    "pompage",
    "dératisation",
    "fourniture de produits",
    "tracteur agricole",
    "pressing",
    "prestations de plonge",
    "maintenance des engins",
    "graffitis",
    "nettoyage industriel",
    "poteaux d'arrêt de bus",
    "poteaux d’arrêt de bus",
]


def charger_historique():
    if not STATE_FILE.exists():
        return set()

    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        ids = data.get("sent_ids", [])
        if not isinstance(ids, list):
            print("AVERTISSEMENT : historique invalide.")
            return set()
        return {str(item) for item in ids}
    except (json.JSONDecodeError, OSError) as exc:
        print(f"AVERTISSEMENT : lecture historique impossible : {exc}")
        return set()


def sauvegarder_historique(ids):
    contenu = {"sent_ids": sorted({str(item) for item in ids})}
    temporaire = STATE_FILE.with_suffix(".json.tmp")
    temporaire.write_text(
        json.dumps(contenu, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporaire.replace(STATE_FILE)


def dans_les_bouches_du_rhone(avis):
    dep = avis.get("code_departement")

    if dep is None:
        return True

    if isinstance(dep, list):
        return "13" in [str(item) for item in dep]

    return str(dep) == "13"


def lire_date_limite(valeur):
    if not valeur:
        return None

    try:
        date_limite = datetime.fromisoformat(
            str(valeur).replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        return None

    if date_limite.tzinfo is None:
        date_limite = date_limite.replace(tzinfo=PARIS)

    return date_limite


def rechercher():
    date_min = (
        datetime.now(timezone.utc) - timedelta(days=90)
    ).date()

    resultats = {}

    with requests.Session() as session:
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

            try:
                reponse = session.get(
                    API,
                    params=params,
                    timeout=REQUEST_TIMEOUT,
                )
                reponse.raise_for_status()
                donnees = reponse.json()
            except (requests.RequestException, ValueError) as exc:
                raise RuntimeError(
                    f"Recherche BOAMP impossible pour {mot!r} : {exc}"
                ) from exc

            for avis in donnees.get("results", []):
                identifiant = avis.get("idweb")
                if identifiant:
                    resultats[str(identifiant)] = avis

    maintenant = datetime.now(timezone.utc)
    candidats = []

    for avis in resultats.values():
        if not dans_les_bouches_du_rhone(avis):
            continue

        date_limite = lire_date_limite(
            avis.get("datelimitereponse")
        )

        if date_limite is None:
            continue

        if date_limite.astimezone(timezone.utc) <= maintenant:
            continue

        objet = (avis.get("objet") or "").casefold()

        if not any(mot.casefold() in objet for mot in MOTS_CLES):
            continue

        if any(mot.casefold() in objet for mot in EXCLUS):
            continue

        avis["_limite"] = date_limite
        candidats.append(avis)

    return sorted(
        candidats,
        key=lambda item: item["_limite"].astimezone(timezone.utc),
    )


def lien_avis(avis):
    url = avis.get("url_avis")

    if isinstance(url, str) and url.strip().startswith(
        ("https://", "http://")
    ):
        return url.strip()

    identifiant = str(avis.get("idweb", "")).strip()

    if not identifiant:
        return "https://www.boamp.fr/"

    return (
        "https://www.boamp.fr/pages/avis/?q="
        + requests.utils.quote(identifiant)
    )


def construire_message(avis, nouveaux):
    maintenant = datetime.now(PARIS)

    entete = [
        "📊 Bilan quotidien BOAMP — Klineme",
        f"Date : {maintenant.strftime('%d/%m/%Y')}",
        f"Marchés ouverts pertinents : {len(avis)}",
        f"Nouvelles opportunités : {len(nouveaux)}",
    ]

    if not avis:
        entete.extend([
            "",
            "Aucun marché ouvert pertinent trouvé aujourd'hui.",
            "La veille continue demain.",
        ])
        return "\n".join(entete), []

    nouveaux_ids = {
        str(item.get("idweb", "")).strip()
        for item in nouveaux
    }

    anciens = [
        item for item in avis
        if str(item.get("idweb", "")).strip() not in nouveaux_ids
    ]

    ordre = list(nouveaux) + anciens
    lignes = entete + ["", "🔎 MARCHÉS À EXAMINER"]
    affiches = []

    for item in ordre[:MAX_ALERTS]:
        limite = item["_limite"].astimezone(PARIS)
        identifiant = str(item.get("idweb", "")).strip()
        objet = (item.get("objet") or "Objet non précisé").strip()
        acheteur = (item.get("nomacheteur") or "Non précisé").strip()

        objet = objet[:177] + "..." if len(objet) > 180 else objet
        acheteur = acheteur[:97] + "..." if len(acheteur) > 100 else acheteur

        statut = (
            "🆕 NOUVEAU"
            if identifiant in nouveaux_ids
            else "📌 Déjà signalé"
        )

        bloc = [
            "",
            f"{statut} — **{objet}**",
            f"Acheteur : {acheteur}",
            f"Date limite : {limite.strftime('%d/%m/%Y à %H:%M')} (Paris)",
            f"<{lien_avis(item)}>",
        ]

        candidat = "\n".join(lignes + bloc)
        if len(candidat) > MAX_CHARS:
            break

        lignes.extend(bloc)
        affiches.append(item)

    restantes = len(ordre) - len(affiches)
    if restantes > 0:
        lignes.extend([
            "",
            f"{restantes} autre(s) marché(s) non affiché(s).",
        ])

    message = "\n".join(lignes)
    if len(message) > 2000:
        message = message[:1950] + "\nMessage raccourci."

    return message, affiches


def envoyer_discord(message):
    if not WEBHOOK:
        raise RuntimeError(
            "Secret Discord absent : vérifie le secret GitHub WEBHOOK."
        )

    prefixes_valides = (
        "https://discord.com/api/webhooks/",
        "https://discordapp.com/api/webhooks/",
    )

    if not WEBHOOK.startswith(prefixes_valides):
        raise ValueError(
            "URL Discord invalide : vérifie le secret GitHub WEBHOOK."
        )

    try:
        reponse = requests.post(
            WEBHOOK,
            json={"content": message},
            timeout=REQUEST_TIMEOUT,
        )
        reponse.raise_for_status()
    except requests.RequestException as exc:
        statut = getattr(
            getattr(exc, "response", None),
            "status_code",
            None,
        )
        detail = f" (HTTP {statut})" if statut else ""
        raise RuntimeError(
            f"Échec de l'envoi Discord{detail}. "
            "Vérifie le webhook et la connexion."
        ) from None


def main():
    print("Recherche des marchés publics pour Klineme...")

    avis = rechercher()
    deja_envoyes = charger_historique()

    print(f"Marchés ouverts repérés : {len(avis)}")

    if INIT_HISTORY:
        if STATE_FILE.exists():
            print("Historique déjà présent : aucun changement.")
            return

        ids = {
            str(item["idweb"])
            for item in avis
            if item.get("idweb")
        }

        sauvegarder_historique(ids)
        print(f"Historique initialisé : {len(ids)} avis enregistrés.")
        print("Aucun message Discord envoyé.")
        return

    nouveaux = [
        item for item in avis
        if str(item.get("idweb", "")) not in deja_envoyes
    ]

    print(f"Nouvelles opportunités : {len(nouveaux)}")

    message, affiches = construire_message(avis, nouveaux)

    if DRY_RUN:
        print("\n--- TEST SANS ENVOI DISCORD ---\n")
        print(message)
        print("\nHistorique non modifié.")
        return

    envoyer_discord(message)
    print("Bilan quotidien envoyé sur Discord.")

    if affiches:
        deja_envoyes.update(
            str(item["idweb"]) for item in affiches
        )
        sauvegarder_historique(deja_envoyes)

    print(
        f"Historique mis à jour : {len(affiches)} "
        "nouvelle(s) annonce(s) mémorisée(s)."
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERREUR : {exc}", file=sys.stderr)
        sys.exit(1)
