# /// script
# requires-python = ">=3.13"
# dependencies = ["xlrd==2.0.2", "loguru==0.7.3"]
# ///
"""Jeu national des tarifs d'eau, tiré de l'open data SISPEA (issue #43).

Usage :
    uv run scripts/generer_sispea.py                      # télécharge l'exercice 2025
    uv run scripts/generer_sispea.py --exercice 2026
    uv run scripts/generer_sispea.py --aep tarifs_AEP_2025.xls --ac tarifs_AC_2025.xls

Lit les « données annuelles sur les tarifs » de l'observatoire SISPEA (feuilles « Détail tarifaire »
et « Zones tarifaires », eau potable et assainissement collectif) et écrit
``custom_components/tarifeau/tarifs/sispea.json.gz`` : pour chaque service, abonnements, tranches
(part collectivité et part délégataire), redevances et TVA ; pour chaque commune (code INSEE), ses
services. Les tarifs de l'exercice N s'appliquent au 1er janvier de N+1.

Contrôle : un service n'est gardé que si sa facture de 120 m³ recalculée redonne, au centime par m³,
le prix TTC publié (D102.0 pour l'eau potable, D204.0 pour l'assainissement) ; les autres sont
écartés et comptés dans le journal.

Données : Office français de la biodiversité, observatoire SISPEA, Licence Ouverte 2.0.
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import tempfile
import urllib.request
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from loguru import logger

RACINE = Path(__file__).resolve().parents[1]
SORTIE = RACINE / "custom_components/tarifeau/tarifs/sispea.json.gz"
URL_TARIFS = (
    "https://www.services.eaufrance.fr/documents/tarifsDescriptions/tarifs_{competence}_{exercice}.xls"
)
PAGE_TELECHARGEMENT = "https://www.services.eaufrance.fr/pro/telechargement"
FORMAT = 1  # version du format du jeu, lue par custom_components/tarifeau/sispea.py
VOLUME_CONTROLE = 120  # m³/an, facture de référence des indicateurs D102.0 et D204.0
TOLERANCE = 0.011  # €/m³ : le prix publié est arrondi au centime
TRANCHES_MAX = 10
PARTS = ("collectivité", "délégataire")

# Compétence SISPEA -> service du modèle, indicateur de prix TTC, redevances et taxes au m³.
# (variable, nom affiché, redevance au sens du modèle)
COMPETENCES: dict[str, dict[str, Any]] = {
    "AEP": {
        "service": "eau_potable",
        "prix": "D102.0",
        "redevances": (
            ("VP.215", "Redevance préservation des ressources en eau", True),
            ("VP.345", "Redevance consommation d'eau potable", True),
            ("VP.346", "Redevance performance des réseaux d'eau potable", True),
            ("VP.214", "Redevance Voies navigables de France (prélèvement)", True),
            ("VP.219", "Autres taxes", False),
        ),
    },
    "AC": {
        "service": "assainissement",
        "prix": "D204.0",
        "redevances": (
            ("VP.347", "Redevance performance des systèmes d'assainissement collectif", True),
            ("VP.218", "Redevance Voies navigables de France (rejet)", True),
            ("VP.219", "Autres taxes", False),
        ),
    },
}
COL_SERVICE = "Id SISPEA de l'entité de gestion"
COL_ZONE_SERVICE = "Identifiant SISPEA de l'entité de gestion à laquelle la commune adhère"


def nombre(valeur: Any) -> float | None:
    """Cellule numérique (nombre, texte « 1,38 » ou vide)."""
    logger.trace("nombre: entrée | valeur={!r:.40}", valeur)
    resultat = None
    if not (valeur is None or (isinstance(valeur, str) and not valeur.strip())):
        try:
            resultat = float(str(valeur).replace(",", ".").strip())
        except ValueError:
            logger.debug("nombre: ValueError interceptée | valeur={!r:.40}", valeur)
    logger.trace("nombre: sortie | {}", resultat)
    return resultat


def identifiant(valeur: Any) -> str | None:
    """Identifiant de service : « ServiceId=247674 », 247674.0 ou « 247674 » -> « 247674 »."""
    logger.trace("identifiant: entrée | valeur={!r:.40}", valeur)
    texte = str(valeur if valeur is not None else "").split("=")[-1].strip().removesuffix(".0")
    logger.trace("identifiant: sortie | {}", texte)
    return texte if texte.isdigit() else None


def tranches(ligne: dict[str, Any], part: str) -> list[list[float | None]] | None:
    """Tranches ``[[jusqua, prix_ht], …, [None, prix_ht]]`` d'une part ; ``None`` si incohérentes.

    Une part sans prix (toutes tranches à 0) donne ``[]``. Les tranches voisines de même prix sont
    fusionnées ; le seuil de la dernière est ignoré (elle court jusqu'à l'infini).
    """
    logger.trace("tranches: entrée | ligne={} cellules, part={}", len(ligne), part)
    lues: list[tuple[float | None, float]] = []
    for n in range(1, TRANCHES_MAX + 1):
        cellules = [ligne.get(f"{c} Tranche {n} (part {part})", "") for c in ("Borne supérieure", "Prix")]
        inf = ligne.get(f"Borne inférieure Tranche {n} (part {part})", "")
        if all(c in ("", None) for c in [inf, *cellules]):
            break
        lues.append((nombre(cellules[0]), nombre(cellules[1]) or 0.0))
    if not any(prix for _, prix in lues):
        logger.trace("tranches: sortie | part sans prix")
        return []
    fusion: list[list[float | None]] = []
    precedent = 0.0
    for i, (jusqua, prix) in enumerate(lues):
        derniere = i == len(lues) - 1
        if not derniere and (jusqua is None or jusqua <= precedent):
            logger.debug("tranches: sortie | seuils incohérents, part={} lues={}", part, lues)
            return None
        if fusion and fusion[-1][1] == prix:
            fusion[-1][0] = None if derniere else jusqua
        else:
            fusion.append([None if derniere else jusqua, prix])
        precedent = jusqua or precedent
    logger.trace("tranches: sortie | {} tranche(s)", len(fusion))
    return fusion


def cout_variable(liste: list[list[float | None]], volume: float) -> float:
    """Coût HT de ``volume`` m³ sur des tranches cumulées sur l'année."""
    logger.trace("cout_variable: entrée | tranches={} volume={}", len(liste), volume)
    total, bas = 0.0, 0.0
    for jusqua, prix in liste:
        haut = volume if jusqua is None else min(volume, jusqua)
        if haut > bas:
            total += (haut - bas) * (prix or 0.0)
        if jusqua is None or volume <= jusqua:
            break
        bas = jusqua
    logger.trace("cout_variable: sortie | {}", total)
    return total


def ttc_au_m3(service: dict[str, Any], volume: float = VOLUME_CONTROLE) -> float:
    """Prix TTC moyen au m³ d'une facture annuelle de ``volume`` m³ (même calcul que D102.0/D204.0)."""
    logger.trace("ttc_au_m3: entrée | volume={}", volume)
    ht = sum(montant for _, montant in service["abonnements"])
    ht += sum(cout_variable(t, volume) for _, t in service["parts"])
    ht += volume * sum(prix for _, prix, _ in service["redevances"])
    resultat = ht * (1 + service["tva"] / 100) / volume
    logger.trace("ttc_au_m3: sortie | {}", resultat)
    return resultat


def service_depuis_ligne(ligne: dict[str, Any], competence: str, stats: Counter) -> dict[str, Any] | None:
    """Service depuis une ligne « Détail tarifaire » ; ``None`` si écarté (motif compté dans ``stats``)."""
    logger.trace("service_depuis_ligne: entrée | ligne={} cellules, competence={}", len(ligne), competence)
    regles = COMPETENCES[competence]
    publie = nombre(ligne.get(regles["prix"]))
    if publie is None:
        stats["sans prix publié"] += 1
        logger.trace("service_depuis_ligne: sortie | sans prix publié")
        return None
    parts = []
    for part in PARTS:
        liste = tranches(ligne, part)
        if liste is None:
            stats["tranches incohérentes"] += 1
            logger.trace("service_depuis_ligne: sortie | tranches incohérentes")
            return None
        if liste:
            parts.append((part, liste))
    if not parts:
        stats["sans part variable"] += 1
        logger.trace("service_depuis_ligne: sortie | sans part variable")
        return None
    abonnements = [
        (part, montant)
        for part, variable in (("collectivité", "VP.191"), ("délégataire", "VP.190"))
        if (montant := nombre(ligne.get(variable)))
    ]
    redevances = [
        (nom, prix, redevance)
        for variable, nom, redevance in regles["redevances"]
        if (prix := nombre(ligne.get(variable)))
    ]
    service = {
        "abonnements": abonnements,
        "parts": parts,
        "redevances": redevances,
        "tva": nombre(ligne.get("VP.213")) or 0.0,
    }
    if abs(ttc_au_m3(service) - publie) > TOLERANCE:
        stats["prix 120 m³ incohérent"] += 1
        logger.trace("service_depuis_ligne: sortie | prix 120 m³ incohérent")
        return None
    stats["gardés"] += 1
    logger.trace("service_depuis_ligne: sortie | gardé")
    return {
        "c": regles["service"],
        "n": str(ligne.get("Nom de l'entité de gestion") or ligne.get("Nom collectivité") or "").strip(),
        "o": str(ligne.get("Nom collectivité") or "").strip(),
        "g": str(ligne.get("Mode de gestion") or "").strip(),
        "s": str(ligne.get("Statut") or "").strip(),
        "p": publie,
        "t": service["tva"],
        "a": [[part, montant] for part, montant in abonnements],
        "v": [[part, liste] for part, liste in parts],
        "r": [[nom, prix, redevance] for nom, prix, redevance in redevances],
    }


def lignes(entete: list[Any], brutes: list[list[Any]]) -> list[dict[str, Any]]:
    """Lignes d'une feuille en dictionnaires indexés par l'en-tête."""
    logger.debug("lignes: entrée | entete={} colonnes, brutes={} lignes", len(entete), len(brutes))
    noms = [str(c).strip() for c in entete]
    logger.debug("lignes: sortie")
    return [dict(zip(noms, ligne, strict=False)) for ligne in brutes]


def construire(
    feuilles: dict[str, tuple[list[dict[str, Any]], list[dict[str, Any]]]],
    exercice: int,
    date_jeu: date,
) -> tuple[dict[str, Any], dict[str, Counter]]:
    """Jeu complet depuis, par compétence, les lignes « Détail tarifaire » et « Zones tarifaires »."""
    logger.debug("construire: entrée | competences={} exercice={}", sorted(feuilles), exercice)
    services: dict[str, dict[str, Any]] = {}
    communes: dict[str, dict[str, Any]] = {}
    bilan: dict[str, Counter] = {}
    for competence, (detail, zones) in feuilles.items():
        stats: Counter = Counter()
        for ligne in detail:
            ident = identifiant(ligne.get(COL_SERVICE))
            if ident is None:
                stats["sans identifiant"] += 1
                continue
            if (service := service_depuis_ligne(ligne, competence, stats)) is not None:
                services[ident] = service
        cle = COMPETENCES[competence]["service"]
        for zone in zones:
            insee = str(zone.get("Code INSEE de la commune adhérente") or "").strip()
            ident = identifiant(zone.get(COL_ZONE_SERVICE))
            if not insee or ident not in services:
                continue
            commune = communes.setdefault(
                insee,
                {
                    "n": str(zone.get("Nom de la commune adhérente") or "").strip(),
                    "d": str(zone.get("DPT de la commune") or "").strip(),
                },
            )
            if ident not in commune.setdefault(cle, []):
                commune[cle].append(ident)
        bilan[competence] = stats
    jeu = {
        "format": FORMAT,
        "exercice": exercice,
        "debut": date(exercice + 1, 1, 1).isoformat(),
        "date_jeu": date_jeu.isoformat(),
        "source": PAGE_TELECHARGEMENT,
        "licence": "Licence Ouverte 2.0 (Office français de la biodiversité, SISPEA)",
        "services": dict(sorted(services.items(), key=lambda e: int(e[0]))),
        "communes": dict(sorted(communes.items())),
    }
    logger.debug("construire: sortie | services={} communes={}", len(services), len(communes))
    return jeu, bilan


def date_excel(valeur: Any) -> date:
    """Date d'une cellule Excel (nombre de jours depuis le 30/12/1899)."""
    logger.debug("date_excel: entrée | valeur={!r:.40}", valeur)
    jours = nombre(valeur)
    resultat = date.today() if jours is None else date(1899, 12, 30) + timedelta(days=int(jours))
    logger.debug("date_excel: sortie | {}", resultat)
    return resultat


def lire_xls(chemin: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], date]:
    """Feuilles « Détail tarifaire », « Zones tarifaires » et date du jeu (métadonnées)."""
    logger.debug("lire_xls: entrée | chemin={}", chemin)
    import xlrd  # dépendance du seul script (en-tête PEP 723)

    logger.info("lecture de {}", chemin.name)
    classeur = xlrd.open_workbook(str(chemin), on_demand=True)
    feuilles = []
    for nom in ("Détail tarifaire", "Zones tarifaires"):
        feuille = classeur.sheet_by_name(nom)
        brutes = [feuille.row_values(i) for i in range(feuille.nrows)]
        feuilles.append(lignes(brutes[0], brutes[1:]))
    meta = classeur.sheet_by_name("Metadonnees")
    date_jeu = date.today()
    for i in range(meta.nrows):
        ligne = meta.row_values(i)
        if "Date du jeu de données" in ligne:
            date_jeu = date_excel(ligne[ligne.index("Date du jeu de données") + 1])
    logger.debug("lire_xls: sortie | date_jeu={}", date_jeu)
    return feuilles[0], feuilles[1], date_jeu


def telecharger(competence: str, exercice: int, dossier: Path) -> Path:
    logger.debug("telecharger: entrée | competence={} exercice={}", competence, exercice)
    url = URL_TARIFS.format(competence=competence, exercice=exercice)
    chemin = dossier / Path(url).name
    logger.info("téléchargement de {}", url)
    with urllib.request.urlopen(url, timeout=300) as reponse:
        chemin.write_bytes(reponse.read())
    logger.debug("telecharger: sortie | {}", chemin)
    return chemin


def ecrire(jeu: dict[str, Any], sortie: Path) -> int:
    """Écrit le jeu (JSON compact, gzip reproductible) ; renvoie la taille en octets."""
    logger.debug("ecrire: entrée | services={} sortie={}", len(jeu["services"]), sortie)
    texte = json.dumps(jeu, ensure_ascii=False, separators=(",", ":"), sort_keys=False)
    sortie.write_bytes(gzip.compress(texte.encode("utf-8"), compresslevel=9, mtime=0))
    taille = sortie.stat().st_size
    logger.debug("ecrire: sortie | {} octets", taille)
    return taille


def main(argv: list[str] | None = None) -> int:
    logger.debug("main: entrée | argv={!r:.80}", argv)
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--exercice", type=int, default=2025)
    parser.add_argument("--aep", type=Path, help="fichier tarifs_AEP_<exercice>.xls déjà téléchargé")
    parser.add_argument("--ac", type=Path, help="fichier tarifs_AC_<exercice>.xls déjà téléchargé")
    parser.add_argument("--sortie", type=Path, default=SORTIE)
    args = parser.parse_args(argv)
    feuilles, dates = {}, []
    with tempfile.TemporaryDirectory() as tmp:
        for competence, chemin in (("AEP", args.aep), ("AC", args.ac)):
            try:
                chemin = chemin or telecharger(competence, args.exercice, Path(tmp))
                detail, zones, date_jeu = lire_xls(chemin)
            except (OSError, ValueError) as err:
                logger.error("{} : {}", competence, err)
                logger.debug("main: sortie | 1")
                return 1
            feuilles[competence] = (detail, zones)
            dates.append(date_jeu)
    jeu, bilan = construire(feuilles, args.exercice, min(dates))
    for competence, stats in bilan.items():
        logger.info("{} : {}", competence, dict(stats))
    taille = ecrire(jeu, args.sortie)
    logger.info(
        "{} : {} services, {} communes, {} Ko",
        args.sortie.name,
        len(jeu["services"]),
        len(jeu["communes"]),
        taille // 1024,
    )
    logger.debug("main: sortie | 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
