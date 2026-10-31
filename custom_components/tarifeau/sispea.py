"""Tarifs nationaux tirés de l'open data SISPEA (issue #43), en Python pur.

Le jeu ``tarifs/sispea.json.gz`` est produit par ``scripts/generer_sispea.py`` depuis les
« données annuelles sur les tarifs » de l'observatoire des services d'eau et d'assainissement
(Office français de la biodiversité, Licence Ouverte 2.0). Il donne, pour chaque commune (code
INSEE), ses services d'eau potable et d'assainissement collectif, et pour chaque service ses
abonnements, ses tranches (part collectivité et part délégataire), ses redevances et sa TVA.

Un tarif SISPEA s'identifie par ``sispea:<INSEE>:<service eau>:<service assainissement>``
(service vide s'il n'y en a pas), et se charge comme un préréglage (``modele.charger_tarif``).
"""

from __future__ import annotations

import gzip
import json
import logging
import re
import unicodedata
from functools import cache
from pathlib import Path
from typing import Any

from .journal import resumer
from .modele import DOSSIER_TARIFS, PREFIXE_SISPEA, Tarif, TarifInvalide, tarif_depuis_dict

_LOGGER = logging.getLogger(__name__)

FICHIER_SISPEA = DOSSIER_TARIFS / "sispea.json.gz"
FORMAT = 1
URL_SERVICE = "https://www.services.eaufrance.fr/service/{service}/{exercice}"
NOMS_SERVICE = {"eau_potable": "eau potable", "assainissement": "assainissement"}
_INSEE = re.compile(r"^(\d{5}|2[AB]\d{3})$")
_CODE_FINAL = re.compile(r"\((\d{5}|2[AB]\d{3})\)\s*$")


@cache
def charger_jeu(chemin: Path = FICHIER_SISPEA) -> dict[str, Any]:
    """Jeu SISPEA décompressé (lecture de fichier : hors de la boucle d'événements), mis en cache."""
    _LOGGER.debug("charger_jeu: entrée | chemin=%s", resumer(chemin))
    try:
        jeu = json.loads(gzip.decompress(chemin.read_bytes()))
    except (OSError, EOFError, ValueError) as err:
        _LOGGER.debug("charger_jeu: sortie par TarifInvalide | %s", resumer(err))
        raise TarifInvalide(f"{chemin.name} : jeu SISPEA illisible ({err})") from None
    if not isinstance(jeu, dict) or jeu.get("format") != FORMAT:
        _LOGGER.debug("charger_jeu: sortie par TarifInvalide")
        raise TarifInvalide(f"{chemin.name} : format de jeu SISPEA inattendu")
    _LOGGER.debug("charger_jeu: sortie | %s", resumer(jeu))
    return jeu


def normaliser(texte: str) -> str:
    """Nom de commune comparable : sans accents ni casse, « St » = « Saint »."""
    _LOGGER.debug("normaliser: entrée | texte=%s", resumer(texte))
    sans_accent = unicodedata.normalize("NFKD", texte).encode("ascii", "ignore").decode().lower()
    mots = re.sub(r"[^a-z0-9]+", " ", sans_accent).split()
    mots = [{"st": "saint", "ste": "sainte"}.get(m, m) for m in mots]
    _LOGGER.debug("normaliser: sortie")
    return " ".join(mots)


def rechercher_communes(jeu: dict[str, Any], saisie: str) -> list[str]:
    """Codes INSEE de la saisie : code INSEE, « Nom (code INSEE) » ou nom de commune."""
    _LOGGER.debug("rechercher_communes: entrée | saisie=%s", resumer(saisie))
    communes = jeu["communes"]
    texte = saisie.strip().upper()
    if entre_parentheses := _CODE_FINAL.search(texte):  # « Besançon (25056) », proposé par défaut
        texte = entre_parentheses.group(1)
    if _INSEE.match(texte):
        trouvees = [texte] if texte in communes else []
        _LOGGER.debug("rechercher_communes: sortie | %s", resumer(trouvees))
        return trouvees
    cible = normaliser(saisie)
    trouvees = [insee for insee, c in communes.items() if cible and normaliser(c["n"]) == cible]
    _LOGGER.debug("rechercher_communes: sortie | %s", resumer(trouvees))
    return trouvees


def _libelle_service(jeu: dict[str, Any], ident: str | None, code: str) -> str:
    _LOGGER.debug("_libelle_service: entrée | ident=%s, code=%s", resumer(ident), resumer(code))
    if ident is None:
        _LOGGER.debug("_libelle_service: sortie | aucun service")
        return f"{NOMS_SERVICE[code]} : aucun service"
    service = jeu["services"][ident]
    _LOGGER.debug("_libelle_service: sortie")
    return f"{NOMS_SERVICE[code]} : {service['o']} ({service['g'].lower()}, {service['p']} €/m³ TTC)"


def choix_commune(jeu: dict[str, Any], insee: str) -> list[tuple[str, str]]:
    """Tarifs possibles pour une commune : ``(identifiant, libellé)`` par couple de services.

    Une commune peut relever de plusieurs services (zones de desserte) : un choix par couple.
    """
    _LOGGER.debug("choix_commune: entrée | insee=%s", resumer(insee))
    commune = jeu["communes"][insee]
    eaux = commune.get("eau_potable") or [None]
    assainissements = commune.get("assainissement") or [None]
    choix = []
    for eau in eaux:
        for assainissement in assainissements:
            ident = f"{PREFIXE_SISPEA}{insee}:{eau or ''}:{assainissement or ''}"
            libelle = (
                f"{commune['n']} ({commune['d']}) — {_libelle_service(jeu, eau, 'eau_potable')} ; "
                f"{_libelle_service(jeu, assainissement, 'assainissement')}"
            )
            choix.append((ident, libelle))
    _LOGGER.debug("choix_commune: sortie | %s", resumer(choix))
    return choix


def _service(service: dict[str, Any]) -> dict[str, Any]:
    """Service du modèle de tarif (même forme qu'un préréglage YAML)."""
    _LOGGER.debug("_service: entrée | service=%s", resumer(service))
    nom = NOMS_SERVICE[service["c"]]
    deux_parts = len(service["v"]) > 1 or bool(service["a"] and service["a"][0][0] != service["v"][0][0])
    abonnements = [
        {"nom": f"Abonnement {nom} (part {part})", "montant_ht_an": montant} for part, montant in service["a"]
    ]
    composantes: list[dict[str, Any]] = []
    for part, tranches in service["v"]:
        composante: dict[str, Any] = {
            "nom": f"{nom.capitalize()} (part {part})" if deux_parts else nom.capitalize()
        }
        if len(tranches) == 1:
            composante["prix_ht"] = tranches[0][1]
        else:
            composante["tranches"] = [
                {"prix_ht": prix} if jusqua is None else {"jusqua": jusqua, "prix_ht": prix}
                for jusqua, prix in tranches
            ]
        composantes.append(composante)
    composantes += [
        {"nom": n, "prix_ht": prix, "redevance": redevance} for n, prix, redevance in service["r"]
    ]
    _LOGGER.debug("_service: sortie")
    return {"tva": service["t"], "abonnements": abonnements, "composantes": composantes}


def tarif_dict(jeu: dict[str, Any], ident: str) -> dict[str, Any]:
    """Tarif ``sispea:<INSEE>:<eau>:<assainissement>`` sous forme de dictionnaire (format YAML)."""
    _LOGGER.debug("tarif_dict: entrée | ident=%s", resumer(ident))
    morceaux = ident.removeprefix(PREFIXE_SISPEA).split(":")
    if len(morceaux) != 3 or morceaux[0] not in jeu["communes"]:
        _LOGGER.debug("tarif_dict: sortie par TarifInvalide")
        raise TarifInvalide(f"{ident} : commune absente du jeu SISPEA {jeu['exercice']}")
    insee, eau, assainissement = morceaux
    commune = jeu["communes"][insee]
    services: dict[str, Any] = {}
    for code, service in (("eau_potable", eau), ("assainissement", assainissement)):
        if not service:
            continue
        if service not in commune.get(code, []):
            _LOGGER.debug("tarif_dict: sortie par TarifInvalide")
            raise TarifInvalide(f"{ident} : service {service} absent de la commune {commune['n']}")
        services[code] = _service(jeu["services"][service])
    if not services:
        _LOGGER.debug("tarif_dict: sortie par TarifInvalide")
        raise TarifInvalide(f"{ident} : aucun service")
    premier = eau or assainissement
    _LOGGER.debug("tarif_dict: sortie")
    return {
        "id": ident,
        "nom": f"{commune['n']} ({commune['d']}) — SISPEA {jeu['exercice']}",
        "periodes": [
            {
                "debut": jeu["debut"],
                "source": URL_SERVICE.format(service=premier, exercice=jeu["exercice"]),
                "date_source": jeu["date_jeu"],
                "services": services,
            }
        ],
    }


def tarif_sispea(ident: str, chemin: Path = FICHIER_SISPEA) -> Tarif:
    """Tarif SISPEA validé par le modèle, comme un préréglage."""
    _LOGGER.debug("tarif_sispea: entrée | ident=%s, chemin=%s", resumer(ident), resumer(chemin))
    tarif = tarif_depuis_dict(tarif_dict(charger_jeu(chemin), ident))
    _LOGGER.debug("tarif_sispea: sortie | %s", resumer(tarif))
    return tarif
