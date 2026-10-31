"""Modèle de tarif d'eau français (issue #2), en Python pur (aucune dépendance à Home Assistant).

Un tarif décrit un **service** (eau potable, assainissement…) d'une collectivité, par
périodes de validité (dates d'effet, souvent au 1er janvier). Chaque période liste, par
service, les abonnements (€ HT/an) et les composantes au m³ (€ HT/m³), éventuellement par
tranches annuelles, avec un taux de TVA par service.

Format YAML (exemple complet : ``tarifs/grand_besancon_regie.yaml``) ::

    id: grand_besancon_regie
    nom: Grand Besançon Métropole (régie)
    facturation: semestrielle
    periodes:
      - debut: 2026-01-01
        source: https://…/deliberation.pdf
        date_source: 2025-12-11
        services:
          eau_potable:
            tva: 5.5
            abonnements:
              - nom: Abonnement
                montant_ht_an: 15.00        # compteur le plus courant (DN 15)
                par_diametre: {15: 15.00, 20: 22.50, 30: 60.00}   # facultatif : DN (mm) → € HT/an
            composantes:
              - nom: Part de la collectivité
                tranches:
                  - {jusqua: 3, prix_ht: 0}      # m³ gratuits
                  - {jusqua: 100, prix_ht: 1.38}
                  - {prix_ht: 1.44}              # dernière tranche : sans borne
              - {nom: Redevance consommation d'eau potable, prix_ht: 0.39, redevance: true}

Les seuils de tranches sont **annuels**, comme dans les délibérations. La façon de les
appliquer à une facture semestrielle relève du moteur (issue #3, ``tranches``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from itertools import pairwise
from pathlib import Path
from typing import Any

import yaml

_LOGGER = logging.getLogger(__name__)

try:
    from .journal import resumer
except ImportError as err:  # module chargé seul, hors paquet (scripts, test sans Home Assistant)
    _LOGGER.debug("hors paquet, résumé du journal réduit au type : %s", err)

    def resumer(valeur: object) -> str:
        """Repli hors paquet : le type seul, jamais la valeur."""
        return f"<{type(valeur).__name__}>"


SERVICES = ("eau_potable", "assainissement", "autre")
FACTURATIONS = ("mensuelle", "bimestrielle", "trimestrielle", "quadrimestrielle", "semestrielle", "annuelle")
REGLES_TRANCHES = ("cumul_annuel", "prorata")
DOSSIER_TARIFS = Path(__file__).parent / "tarifs"
# Tarifs personnels (issue #17) : fichiers YAML au même format, dans <config>/tarifeau/.
NOM_DOSSIER_PERSO = "tarifeau"
PREFIXE_PERSO = "perso:"
PREFIXE_SISPEA = "sispea:"  # tarifs nationaux (sispea.py, issue #43)
EXTENSIONS_PERSO = (".yaml", ".yml")


class TarifInvalide(ValueError):
    """Le tarif ne respecte pas le schéma. Le message donne le chemin de la valeur fautive."""


@dataclass(frozen=True)
class Tranche:
    """Prix d'un m³ jusqu'au seuil annuel ``jusqua`` (inclus). ``None`` : sans borne."""

    jusqua: Decimal | None
    prix_ht: Decimal


@dataclass(frozen=True)
class Composante:
    """Part au m³ : part de la collectivité, du délégataire, redevance de l'agence de l'eau…"""

    nom: str
    tranches: tuple[Tranche, ...]
    redevance: bool = False
    source: str | None = None

    def volumes_par_tranche(
        self, volume: Decimal, depuis: Decimal = Decimal(0), facteur: Decimal = Decimal(1)
    ) -> list[tuple[Tranche, Decimal]]:
        """Répartit ``volume`` m³ entre les tranches, dans l'ordre.

        ``depuis`` : volume déjà consommé dans l'année tarifaire (tranches cumulées).
        ``facteur`` : coefficient appliqué aux seuils annuels (tranches proratisées).
        """
        _LOGGER.debug(
            "Composante.volumes_par_tranche: entrée | volume=%s, depuis=%s, facteur=%s",
            resumer(volume),
            resumer(depuis),
            resumer(facteur),
        )
        debut, fin = depuis, depuis + volume
        resultat, borne_basse = [], Decimal(0)
        for tranche in self.tranches:
            borne_haute = None if tranche.jusqua is None else tranche.jusqua * facteur
            haut = fin if borne_haute is None else min(fin, borne_haute)
            resultat.append((tranche, max(haut - max(debut, borne_basse), Decimal(0))))
            if borne_haute is not None:
                borne_basse = borne_haute
        _LOGGER.debug("Composante.volumes_par_tranche: sortie | %s", resumer(resultat))
        return resultat

    def tranche_au(self, cumul: Decimal, facteur: Decimal = Decimal(1)) -> tuple[int, Decimal | None]:
        """Indice de la tranche du prochain m³ après ``cumul`` m³, et volume restant avant la suivante."""
        _LOGGER.debug(
            "Composante.tranche_au: entrée | cumul=%s, facteur=%s", resumer(cumul), resumer(facteur)
        )
        for i, tranche in enumerate(self.tranches):
            if tranche.jusqua is None or cumul < tranche.jusqua * facteur:
                _LOGGER.debug("Composante.tranche_au: sortie")
                return i, None if tranche.jusqua is None else tranche.jusqua * facteur - cumul
        _LOGGER.debug("Composante.tranche_au: sortie par AssertionError")
        raise AssertionError("dernière tranche sans borne")  # pragma: no cover  (garanti par la validation)


@dataclass(frozen=True)
class Abonnement:
    """Part fixe, en € HT par an (le moteur la proratise selon la période)."""

    nom: str
    montant_ht_an: Decimal
    source: str | None = None
    par_diametre: tuple[tuple[int, Decimal], ...] = ()  # (DN en mm, € HT/an), si le tarif en dépend


@dataclass(frozen=True)
class Service:
    """Un service facturé (eau potable, assainissement…), avec son taux de TVA en %."""

    code: str
    tva: Decimal
    abonnements: tuple[Abonnement, ...]
    composantes: tuple[Composante, ...]


@dataclass(frozen=True)
class FactureType:
    """Facture type publiée par la collectivité, pour vérifier le tarif (test automatique)."""

    volume_m3: Decimal
    total_ttc: Decimal


@dataclass(frozen=True)
class Periode:
    """Tarif valable de ``debut`` à ``fin`` inclus (``fin`` : ``None`` = toujours en vigueur)."""

    debut: date
    fin: date | None
    services: tuple[Service, ...]
    source: str
    date_source: date
    factures_types: tuple[FactureType, ...] = ()

    def couvre(self, jour: date) -> bool:
        """Vrai si ``jour`` tombe dans la période."""
        return self.debut <= jour and (self.fin is None or jour <= self.fin)


@dataclass(frozen=True)
class Tarif:
    """Tarif complet d'un service d'eau, période par période (triées, sans chevauchement)."""

    id: str
    nom: str
    periodes: tuple[Periode, ...]
    facturation: str = "semestrielle"
    tranches: str = "cumul_annuel"

    def periode_au(self, jour: date) -> Periode:
        """Période en vigueur le ``jour`` donné."""
        _LOGGER.debug("Tarif.periode_au: entrée | jour=%s", resumer(jour))
        for periode in self.periodes:
            if periode.couvre(jour):
                _LOGGER.debug("Tarif.periode_au: sortie | %s", resumer(periode))
                return periode
        _LOGGER.debug("Tarif.periode_au: sortie par LookupError")
        raise LookupError(f"{self.id} : aucun tarif en vigueur le {jour.isoformat()}")


# ── Lecture et validation ────────────────────────────────────────────────────


def _exiger(cond: bool, chemin: str, message: str) -> None:
    _LOGGER.debug(
        "_exiger: entrée | cond=%s, chemin=%s, message=%s", resumer(cond), resumer(chemin), resumer(message)
    )
    if not cond:
        _LOGGER.debug("_exiger: sortie par TarifInvalide")
        raise TarifInvalide(f"{chemin} : {message}")
    _LOGGER.debug("_exiger: sortie")


def _texte(valeur: Any, chemin: str) -> str:
    _LOGGER.debug("_texte: entrée | valeur=%s, chemin=%s", resumer(valeur), resumer(chemin))
    _exiger(isinstance(valeur, str) and valeur.strip() != "", chemin, "texte non vide attendu")
    _LOGGER.debug("_texte: sortie")
    return valeur.strip()


def _decimal(valeur: Any, chemin: str, *, positif: bool = True) -> Decimal:
    _LOGGER.debug(
        "_decimal: entrée | valeur=%s, chemin=%s, positif=%s",
        resumer(valeur),
        resumer(chemin),
        resumer(positif),
    )
    _exiger(
        isinstance(valeur, int | float | str | Decimal) and not isinstance(valeur, bool),
        chemin,
        "nombre attendu",
    )
    try:
        nombre = Decimal(str(valeur).replace(",", "."))
    except InvalidOperation:
        _LOGGER.debug("_decimal: sortie par TarifInvalide")
        raise TarifInvalide(f"{chemin} : nombre attendu, pas « {valeur} »") from None
    _exiger(nombre.is_finite(), chemin, "nombre fini attendu")
    if positif:
        _exiger(nombre >= 0, chemin, "doit être positif ou nul")
    _LOGGER.debug("_decimal: sortie | %s", resumer(nombre))
    return nombre


def _date(valeur: Any, chemin: str) -> date:
    _LOGGER.debug("_date: entrée | valeur=%s, chemin=%s", resumer(valeur), resumer(chemin))
    if isinstance(valeur, str):
        try:
            _LOGGER.debug("_date: sortie")
            return date.fromisoformat(valeur)
        except ValueError:
            _LOGGER.debug("_date: ValueError interceptée")
            pass
    _exiger(isinstance(valeur, date), chemin, "date AAAA-MM-JJ attendue")
    _LOGGER.debug("_date: sortie | %s", resumer(valeur))
    return valeur


def _url(valeur: Any, chemin: str) -> str:
    _LOGGER.debug("_url: entrée | valeur=%s, chemin=%s", resumer(valeur), resumer(chemin))
    texte = _texte(valeur, chemin)
    _exiger(texte.startswith(("https://", "http://")), chemin, "URL de la délibération attendue")
    _LOGGER.debug("_url: sortie | %s", resumer(texte))
    return texte


def _cles(donnees: Any, chemin: str, permises: set[str], requises: set[str]) -> dict:
    _LOGGER.debug(
        "_cles: entrée | donnees=%s, chemin=%s, permises=%s, requises=%s",
        resumer(donnees),
        resumer(chemin),
        resumer(permises),
        resumer(requises),
    )
    _exiger(isinstance(donnees, dict), chemin, "objet attendu")
    inconnues = set(donnees) - permises
    _exiger(not inconnues, chemin, f"clé(s) inconnue(s) : {', '.join(sorted(map(str, inconnues)))}")
    manquantes = requises - set(donnees)
    _exiger(not manquantes, chemin, f"clé(s) manquante(s) : {', '.join(sorted(manquantes))}")
    _LOGGER.debug("_cles: sortie | %s", resumer(donnees))
    return donnees


def _liste(valeur: Any, chemin: str, *, vide: bool = True) -> list:
    _LOGGER.debug(
        "_liste: entrée | valeur=%s, chemin=%s, vide=%s", resumer(valeur), resumer(chemin), resumer(vide)
    )
    _exiger(isinstance(valeur, list), chemin, "liste attendue")
    _exiger(vide or len(valeur) > 0, chemin, "au moins un élément attendu")
    _LOGGER.debug("_liste: sortie | %s", resumer(valeur))
    return valeur


def _tranches(donnees: dict, chemin: str) -> tuple[Tranche, ...]:
    _LOGGER.debug("_tranches: entrée | donnees=%s, chemin=%s", resumer(donnees), resumer(chemin))
    if "prix_ht" in donnees:
        _exiger("tranches" not in donnees, chemin, "« prix_ht » ou « tranches », pas les deux")
        _LOGGER.debug("_tranches: sortie")
        return (Tranche(None, _decimal(donnees["prix_ht"], f"{chemin}.prix_ht")),)
    _exiger("tranches" in donnees, chemin, "« prix_ht » ou « tranches » attendu")
    brutes = _liste(donnees["tranches"], f"{chemin}.tranches", vide=False)
    tranches, precedent = [], Decimal(0)
    for i, brute in enumerate(brutes):
        c = f"{chemin}.tranches[{i}]"
        brute = _cles(brute, c, {"jusqua", "prix_ht"}, {"prix_ht"})
        derniere = i == len(brutes) - 1
        if derniere:
            _exiger(brute.get("jusqua") is None, c, "la dernière tranche n'a pas de seuil « jusqua »")
            jusqua = None
        else:
            _exiger(brute.get("jusqua") is not None, c, "seuil « jusqua » (m³ par an) attendu")
            jusqua = _decimal(brute["jusqua"], f"{c}.jusqua")
            _exiger(jusqua > precedent, f"{c}.jusqua", "les seuils doivent croître strictement")
            precedent = jusqua
        tranches.append(Tranche(jusqua, _decimal(brute["prix_ht"], f"{c}.prix_ht")))
    _LOGGER.debug("_tranches: sortie")
    return tuple(tranches)


def _par_diametre(donnees: Any, chemin: str) -> tuple[tuple[int, Decimal], ...]:
    _LOGGER.debug("_par_diametre: entrée | donnees=%s, chemin=%s", resumer(donnees), resumer(chemin))
    _exiger(isinstance(donnees, dict), chemin, "dictionnaire attendu (DN en mm → € HT/an)")
    table = []
    for dn, montant in donnees.items():
        _exiger(
            isinstance(dn, int) and not isinstance(dn, bool) and dn > 0, chemin, f"diamètre invalide : {dn!r}"
        )
        table.append((dn, _decimal(montant, f"{chemin}.{dn}")))
    _LOGGER.debug("_par_diametre: sortie")
    return tuple(sorted(table))


def _service(code: str, donnees: Any, chemin: str) -> Service:
    _LOGGER.debug(
        "_service: entrée | code=%s, donnees=%s, chemin=%s", resumer(code), resumer(donnees), resumer(chemin)
    )
    _exiger(code in SERVICES, chemin, f"service inconnu (attendus : {', '.join(SERVICES)})")
    donnees = _cles(donnees, chemin, {"tva", "abonnements", "composantes"}, {"tva"})
    tva = _decimal(donnees["tva"], f"{chemin}.tva")
    _exiger(tva <= 100, f"{chemin}.tva", "taux en %, entre 0 et 100")
    abonnements = []
    for i, brut in enumerate(_liste(donnees.get("abonnements", []), f"{chemin}.abonnements")):
        c = f"{chemin}.abonnements[{i}]"
        brut = _cles(brut, c, {"nom", "montant_ht_an", "source", "par_diametre"}, {"nom", "montant_ht_an"})
        abonnements.append(
            Abonnement(
                _texte(brut["nom"], f"{c}.nom"),
                _decimal(brut["montant_ht_an"], f"{c}.montant_ht_an"),
                _url(brut["source"], f"{c}.source") if "source" in brut else None,
                _par_diametre(brut.get("par_diametre", {}), f"{c}.par_diametre"),
            )
        )
    composantes = []
    for i, brute in enumerate(_liste(donnees.get("composantes", []), f"{chemin}.composantes")):
        c = f"{chemin}.composantes[{i}]"
        brute = _cles(brute, c, {"nom", "prix_ht", "tranches", "redevance", "source"}, {"nom"})
        redevance = brute.get("redevance", False)
        _exiger(isinstance(redevance, bool), f"{c}.redevance", "true ou false attendu")
        composantes.append(
            Composante(
                _texte(brute["nom"], f"{c}.nom"),
                _tranches(brute, c),
                redevance,
                _url(brute["source"], f"{c}.source") if "source" in brute else None,
            )
        )
    _exiger(bool(abonnements or composantes), chemin, "ni abonnement ni composante")
    _LOGGER.debug("_service: sortie")
    return Service(code, tva, tuple(abonnements), tuple(composantes))


def _periode(donnees: Any, chemin: str) -> Periode:
    _LOGGER.debug("_periode: entrée | donnees=%s, chemin=%s", resumer(donnees), resumer(chemin))
    donnees = _cles(
        donnees,
        chemin,
        {"debut", "fin", "source", "date_source", "services", "factures_types"},
        {"debut", "source", "services"},
    )
    debut = _date(donnees["debut"], f"{chemin}.debut")
    fin = _date(donnees["fin"], f"{chemin}.fin") if donnees.get("fin") is not None else None
    _exiger(fin is None or fin >= debut, f"{chemin}.fin", "antérieure au début")
    services_bruts = donnees["services"]
    _exiger(isinstance(services_bruts, dict) and services_bruts, f"{chemin}.services", "au moins un service")
    services = tuple(_service(code, v, f"{chemin}.services.{code}") for code, v in services_bruts.items())
    date_source = _date(donnees.get("date_source", debut), f"{chemin}.date_source")
    factures_types = []
    for i, brute in enumerate(_liste(donnees.get("factures_types", []), f"{chemin}.factures_types")):
        c = f"{chemin}.factures_types[{i}]"
        brute = _cles(brute, c, {"volume_m3", "total_ttc"}, {"volume_m3", "total_ttc"})
        factures_types.append(
            FactureType(
                _decimal(brute["volume_m3"], f"{c}.volume_m3"), _decimal(brute["total_ttc"], f"{c}.total_ttc")
            )
        )
    source = _url(donnees["source"], f"{chemin}.source")
    _LOGGER.debug("_periode: sortie")
    return Periode(debut, fin, services, source, date_source, tuple(factures_types))


def tarif_depuis_dict(donnees: Any) -> Tarif:
    """Construit et valide un tarif depuis un dictionnaire (YAML ou JSON déjà lu)."""
    _LOGGER.debug("tarif_depuis_dict: entrée | donnees=%s", resumer(donnees))
    donnees = _cles(
        donnees, "tarif", {"id", "nom", "facturation", "tranches", "periodes"}, {"id", "nom", "periodes"}
    )
    facturation = donnees.get("facturation", "semestrielle")
    _exiger(facturation in FACTURATIONS, "tarif.facturation", f"attendu : {', '.join(FACTURATIONS)}")
    regle = donnees.get("tranches", "cumul_annuel")
    _exiger(regle in REGLES_TRANCHES, "tarif.tranches", f"attendu : {', '.join(REGLES_TRANCHES)}")
    brutes = _liste(donnees["periodes"], "tarif.periodes", vide=False)
    periodes = sorted(
        (_periode(p, f"tarif.periodes[{i}]") for i, p in enumerate(brutes)), key=lambda p: p.debut
    )
    for avant, apres in pairwise(periodes):
        chemin = f"tarif.periodes (au {apres.debut.isoformat()})"
        _exiger(avant.fin is not None, chemin, "la période précédente doit avoir une « fin »")
        _exiger(avant.fin < apres.debut, chemin, "chevauche la période précédente")
        _exiger(avant.fin + timedelta(days=1) == apres.debut, chemin, "trou depuis la période précédente")
    _LOGGER.debug("tarif_depuis_dict: sortie")
    return Tarif(
        _texte(donnees["id"], "tarif.id"),
        _texte(donnees["nom"], "tarif.nom"),
        tuple(periodes),
        facturation,
        regle,
    )


def chemin_tarif(source: str | Path, dossier_perso: Path | None = None) -> Path:
    """Fichier d'un tarif : chemin, identifiant de préréglage, ou ``perso:<nom>`` (dossier personnel)."""
    _LOGGER.debug(
        "chemin_tarif: entrée | source=%s, dossier_perso=%s", resumer(source), resumer(dossier_perso)
    )
    if isinstance(source, str) and source.startswith(PREFIXE_PERSO):
        nom = source.removeprefix(PREFIXE_PERSO)
        if dossier_perso is None:
            _LOGGER.debug("chemin_tarif: sortie par TarifInvalide")
            raise TarifInvalide(f"{nom} : dossier des tarifs personnels inconnu")
        for extension in EXTENSIONS_PERSO:
            if (chemin := dossier_perso / f"{nom}{extension}").is_file():
                _LOGGER.debug("chemin_tarif: sortie | %s", resumer(chemin))
                return chemin
        _LOGGER.debug("chemin_tarif: sortie par TarifInvalide")
        raise TarifInvalide(f"{nom}.yaml : fichier absent du dossier {dossier_perso}")
    chemin = Path(source)
    _LOGGER.debug("chemin_tarif: sortie")
    return chemin if chemin.suffix else DOSSIER_TARIFS / f"{source}.yaml"


def charger_tarif(source: str | Path, dossier_perso: Path | None = None) -> Tarif:
    """Lit un tarif : chemin YAML, préréglage de ``tarifs/``, ``perso:<nom>`` ou ``sispea:<commune>…``."""
    _LOGGER.debug(
        "charger_tarif: entrée | source=%s, dossier_perso=%s", resumer(source), resumer(dossier_perso)
    )
    if isinstance(source, str) and source.startswith(PREFIXE_SISPEA):
        from .sispea import tarif_sispea  # import différé : sispea importe ce module

        _LOGGER.debug("charger_tarif: sortie | jeu SISPEA")
        return tarif_sispea(source)
    chemin = chemin_tarif(source, dossier_perso)
    try:
        donnees = yaml.safe_load(chemin.read_text(encoding="utf-8"))
    except yaml.YAMLError as err:
        _LOGGER.debug("charger_tarif: sortie par TarifInvalide")
        raise TarifInvalide(f"{chemin.name} : YAML invalide ({err})") from None
    except (OSError, UnicodeDecodeError) as err:
        _LOGGER.debug("charger_tarif: sortie par TarifInvalide")
        raise TarifInvalide(f"{chemin.name} : fichier illisible ({err})") from None
    try:
        _LOGGER.debug("charger_tarif: sortie")
        return tarif_depuis_dict(donnees)
    except TarifInvalide as err:
        _LOGGER.debug("charger_tarif: sortie par TarifInvalide")
        raise TarifInvalide(f"{chemin.name} : {err}") from None


def tarifs_perso(dossier: Path | None) -> list[str]:
    """Identifiants ``perso:<nom>`` des fichiers YAML du dossier personnel, valides ou non."""
    _LOGGER.debug("tarifs_perso: entrée | dossier=%s", resumer(dossier))
    if dossier is None or not dossier.is_dir():
        _LOGGER.debug("tarifs_perso: sortie")
        return []
    noms = {p.stem for p in dossier.iterdir() if p.suffix in EXTENSIONS_PERSO and p.is_file()}
    _LOGGER.debug("tarifs_perso: sortie")
    return [f"{PREFIXE_PERSO}{nom}" for nom in sorted(noms)]


def verifier_tarifs_perso(dossier: Path | None) -> list[tuple[str, Tarif | None, str | None]]:
    """Pour chaque fichier personnel : (identifiant, tarif, ``None``) ou (identifiant, ``None``, erreur)."""
    _LOGGER.debug("verifier_tarifs_perso: entrée | dossier=%s", resumer(dossier))
    resultats: list[tuple[str, Tarif | None, str | None]] = []
    for ident in tarifs_perso(dossier):
        try:
            resultats.append((ident, charger_tarif(ident, dossier), None))
        except TarifInvalide as err:
            _LOGGER.debug("verifier_tarifs_perso: TarifInvalide interceptée | %s", err)
            resultats.append((ident, None, str(err)))
    _LOGGER.debug("verifier_tarifs_perso: sortie | %s", resumer(resultats))
    return resultats


def prereglages() -> list[str]:
    """Identifiants des tarifs fournis avec l'intégration."""
    return sorted(p.stem for p in DOSSIER_TARIFS.glob("*.yaml"))


def tarif_prix_fixe(
    prix_ttc_m3: Decimal | int | float | str,
    debut: date = date(2000, 1, 1),
    *,
    nom: str | None = None,
    source: str = "https://github.com/Mathilde-Team/tarifeau",
    date_source: date | None = None,
) -> Tarif:
    """Tarif minimal : un prix TTC au m³, sans abonnement ni TVA séparée (sources « prix fixe » et entité)."""
    _LOGGER.debug(
        "tarif_prix_fixe: entrée | prix_ttc_m3=%s, debut=%s, nom=%s, source=%s, date_source=%s",
        resumer(prix_ttc_m3),
        resumer(debut),
        resumer(nom),
        resumer(source),
        resumer(date_source),
    )
    prix = _decimal(prix_ttc_m3, "prix_fixe")
    composante = Composante("Prix au m³ (TTC)", (Tranche(None, prix),))
    service = Service("autre", Decimal(0), (), (composante,))
    periode = Periode(debut, None, (service,), source, date_source or debut)
    _LOGGER.debug("tarif_prix_fixe: sortie")
    return Tarif("prix_fixe", nom or f"Prix fixe {prix} €/m³", (periode,))


def diametres(tarif: Tarif) -> list[int]:
    """Diamètres de compteur (DN, mm) dont l'abonnement est connu, toutes périodes confondues."""
    return sorted(
        {dn for p in tarif.periodes for s in p.services for a in s.abonnements for dn, _ in a.par_diametre}
    )


def avec_diametre(tarif: Tarif, diametre: int) -> Tarif:
    """Même tarif, avec l'abonnement du compteur ``diametre`` (DN, mm).

    Un abonnement sans montant pour ce diamètre garde son montant par défaut. Les factures types
    (calculées pour le compteur le plus courant) ne s'appliquent plus si un montant change.
    """

    _LOGGER.debug("avec_diametre: entrée | tarif=%s, diametre=%s", resumer(tarif), resumer(diametre))

    def abonnement(a: Abonnement) -> Abonnement:
        return replace(a, montant_ht_an=dict(a.par_diametre).get(diametre, a.montant_ht_an))

    periodes = []
    for p in tarif.periodes:
        services = tuple(
            replace(s, abonnements=tuple(abonnement(a) for a in s.abonnements)) for s in p.services
        )
        change = services != p.services
        periodes.append(replace(p, services=services, factures_types=() if change else p.factures_types))
    _LOGGER.debug("avec_diametre: sortie")
    return replace(tarif, periodes=tuple(periodes))


def sans_assainissement(tarif: Tarif) -> Tarif:
    """Même tarif, sans le service d'assainissement (logement en assainissement non collectif)."""
    _LOGGER.debug("sans_assainissement: entrée | tarif=%s", resumer(tarif))
    periodes = tuple(
        replace(p, services=tuple(s for s in p.services if s.code != "assainissement"), factures_types=())
        for p in tarif.periodes
    )
    _LOGGER.debug("sans_assainissement: sortie")
    return replace(tarif, periodes=periodes)


def prolonger(tarif: Tarif, du: date, au: date) -> tuple[Tarif, bool]:
    """Tarif utilisable sur ``[du, au[`` même hors des dates connues.

    Le premier tarif est étendu avant son début et le dernier n'a plus de fin : quand la
    délibération de l'année n'est pas encore saisie, on garde le dernier tarif connu.
    Renvoie aussi ``True`` si ``[du, au[`` sort des dates connues.
    """
    _LOGGER.debug("prolonger: entrée | tarif=%s, du=%s, au=%s", resumer(tarif), resumer(du), resumer(au))
    periodes = list(tarif.periodes)
    premier, dernier = periodes[0], periodes[-1]
    hors = du < premier.debut or (dernier.fin is not None and au - timedelta(days=1) > dernier.fin)
    periodes[0] = replace(premier, debut=min(du, premier.debut))
    periodes[-1] = replace(periodes[-1], fin=None)
    _LOGGER.debug("prolonger: sortie")
    return replace(tarif, periodes=tuple(periodes)), hors
