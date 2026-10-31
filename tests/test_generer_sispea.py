"""Générateur du jeu national SISPEA (scripts/generer_sispea.py, issue #43)."""

import gzip
import importlib.util
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path

import pytest

RACINE = Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location("generer_sispea", RACINE / "scripts" / "generer_sispea.py")
generer = importlib.util.module_from_spec(_spec)
sys.modules["generer_sispea"] = generer
_spec.loader.exec_module(generer)


def _tranches(part: str, *tranches: tuple) -> dict:
    ligne = {}
    for n, (inf, sup, prix) in enumerate(tranches, start=1):
        ligne |= {
            f"Borne inférieure Tranche {n} (part {part})": inf,
            f"Borne supérieure Tranche {n} (part {part})": sup,
            f"Prix Tranche {n} (part {part})": prix,
        }
    return ligne


def _besancon_eau() -> dict:
    """Ligne « Détail tarifaire » du service Grand Besançon régie (exercice 2025)."""
    return {
        "Id SISPEA de l'entité de gestion": "ServiceId=247674",
        "Nom collectivité": "CU GRAND BESANCON METROPOLE",
        "Nom de l'entité de gestion": "Eau potable régie",
        "Mode de gestion": "Régie",
        "Statut": "Publié non vérifié",
        "D102.0": "2.07",
        "VP.191": "15",
        "VP.190": "",
        "VP.213": "5.5",
        "VP.215": "0.065",
        "VP.345": "0.39",
        "VP.346": "0.03",
        **_tranches("collectivité", ("0", "3", ""), ("3", "100", "1.38"), ("100", "", "1.44")),
    }


def test_nombre_et_identifiant() -> None:
    assert generer.nombre("1,38") == 1.38
    assert generer.nombre(2) == 2.0
    assert generer.nombre("") is None
    assert generer.nombre(None) is None
    assert generer.nombre("n.c.") is None
    assert generer.identifiant("ServiceId=247674") == "247674"
    assert generer.identifiant(247674.0) == "247674"
    assert generer.identifiant("") is None
    assert generer.identifiant(None) is None


def test_tranches_gratuites_et_fusion() -> None:
    seuils = (("0", "3", ""), ("3", "100", "1.38"), ("100", "500", "1.38"), ("500", "999999", "1.2"))
    ligne = _tranches("collectivité", *seuils)
    assert generer.tranches(ligne, "collectivité") == [[3.0, 0.0], [500.0, 1.38], [None, 1.2]]
    assert generer.tranches({}, "délégataire") == []
    assert generer.tranches(_tranches("délégataire", ("0", "", "0")), "délégataire") == []


def test_fusion_sur_la_derniere_tranche() -> None:
    ligne = _tranches("collectivité", ("0", "50", "1"), ("50", "", "1"))
    assert generer.tranches(ligne, "collectivité") == [[None, 1.0]]


def test_tranches_incoherentes() -> None:
    ligne = _tranches("collectivité", ("0", "100", "1"), ("100", "50", "2"), ("50", "", "3"))
    assert generer.tranches(ligne, "collectivité") is None


def test_cout_variable() -> None:
    attendu = pytest.approx(97 * 1.38 + 20 * 1.44)
    assert generer.cout_variable([[3, 0.0], [100, 1.38], [None, 1.44]], 120) == attendu
    assert generer.cout_variable([[3, 0.0], [None, 2.0]], 2) == 0


def test_service_garde_besancon() -> None:
    stats = Counter()
    service = generer.service_depuis_ligne(_besancon_eau(), "AEP", stats)
    assert stats == {"gardés": 1}
    assert service["c"] == "eau_potable"
    assert service["a"] == [["collectivité", 15.0]]
    assert service["v"] == [["collectivité", [[3.0, 0.0], [100.0, 1.38], [None, 1.44]]]]
    assert service["r"][0] == ["Redevance préservation des ressources en eau", 0.065, True]
    assert service["t"] == 5.5


@pytest.mark.parametrize(
    ("modif", "motif"),
    [
        ({"D102.0": ""}, "sans prix publié"),
        ({"D102.0": "3.5"}, "prix 120 m³ incohérent"),
        (_tranches("collectivité", ("0", "", "0"), ("", "", ""), ("", "", "")), "sans part variable"),
        (
            _tranches("délégataire", ("0", "100", "1"), ("100", "50", "2"), ("50", "", "3")),
            "tranches incohérentes",
        ),
    ],
)
def test_service_ecarte(modif, motif) -> None:
    stats = Counter()
    assert generer.service_depuis_ligne(_besancon_eau() | modif, "AEP", stats) is None
    assert stats == {motif: 1}


def test_construire_et_ecrire(tmp_path) -> None:
    ac = {
        "Id SISPEA de l'entité de gestion": "ServiceId=255734",
        "Nom collectivité": "CU GRAND BESANCON METROPOLE",
        "D204.0": "1.96",
        "VP.191": "10",
        "VP.213": "10",
        "VP.347": "0.04",
        **_tranches("collectivité", ("0", "", "1.66")),
    }
    zone = {
        "Code INSEE de la commune adhérente": "25056",
        "Nom de la commune adhérente": "Besançon",
        "DPT de la commune": "25",
        generer.COL_ZONE_SERVICE: 247674.0,
    }
    feuilles = {
        "AEP": (generer.lignes(list(_besancon_eau()), [list(_besancon_eau().values()), [""]]), [zone, zone]),
        "AC": (
            [ac, {}],
            [zone | {generer.COL_ZONE_SERVICE: "255734"}, zone | {generer.COL_ZONE_SERVICE: "1"}],
        ),
    }
    jeu, bilan = generer.construire(feuilles, 2025, date(2026, 10, 24))
    assert bilan["AEP"] == {"gardés": 1, "sans identifiant": 1}
    assert bilan["AC"] == {"gardés": 1, "sans identifiant": 1}
    assert jeu["debut"] == "2026-01-01"
    assert jeu["communes"] == {
        "25056": {"n": "Besançon", "d": "25", "eau_potable": ["247674"], "assainissement": ["255734"]}
    }
    assert list(jeu["services"]) == ["247674", "255734"]
    sortie = tmp_path / "sispea.json.gz"
    taille = generer.ecrire(jeu, sortie)
    assert taille == sortie.stat().st_size
    assert json.loads(gzip.decompress(sortie.read_bytes())) == jeu
    assert generer.ecrire(jeu, sortie) == taille  # sortie reproductible (gzip sans horodatage)


def test_date_excel() -> None:
    assert generer.date_excel(46319.625) == date(2026, 10, 24)
    assert generer.date_excel("") == date.today()
