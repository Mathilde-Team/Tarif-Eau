"""Tarifs nationaux SISPEA : jeu embarqué, recherche de commune, formulaire (issue #43)."""

import gzip
import json
from datetime import date, timedelta
from decimal import Decimal

import aiohttp
import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tarifeau import sispea
from custom_components.tarifeau.calcul import facture_sur_un_an
from custom_components.tarifeau.config_flow import URL_GEO_COMMUNES
from custom_components.tarifeau.const import (
    CONF_ASSAINISSEMENT,
    CONF_COMMUNE,
    CONF_DATE_RELEVE,
    CONF_FACTURATION,
    CONF_INDEX,
    CONF_INDEX_RELEVE,
    CONF_NOM,
    CONF_PREREGLAGE,
    CONF_SOURCE,
    DOMAIN,
)
from custom_components.tarifeau.modele import TarifInvalide, charger_tarif
from custom_components.tarifeau.suivi import charger_tarif_config

INDEX = "sensor.compteur_eau_volume_total"
HIER = (date.today() - timedelta(days=1)).isoformat()
BESANCON = "sispea:25056:247674:255734"
JUIN = date(2026, 6, 1)


@pytest.fixture
def jeu() -> dict:
    return sispea.charger_jeu()


# ── Jeu embarqué ──


def test_jeu_embarque(jeu) -> None:
    assert jeu["format"] == sispea.FORMAT
    assert jeu["exercice"] == 2025
    assert jeu["debut"] == "2026-01-01"
    assert "Licence Ouverte" in jeu["licence"]
    assert len(jeu["communes"]) > 25000
    assert len(jeu["services"]) > 10000
    assert sispea.FICHIER_SISPEA.stat().st_size < 1_500_000  # reste raisonnable dans l'intégration


@pytest.mark.parametrize(
    ("insee", "prereglage"),
    [("25056", "grand_besancon_regie"), ("90010", "grand_belfort")],
)
@pytest.mark.parametrize("volume", [30, 120, 250])
def test_sispea_redonne_les_prereglages_verifies(jeu, insee, prereglage, volume) -> None:
    """Les préréglages relevés sur facture et le jeu national donnent la même facture."""
    (ident, _), *_ = sispea.choix_commune(jeu, insee)
    national = charger_tarif(ident).periode_au(JUIN)
    local = charger_tarif(prereglage).periode_au(JUIN)
    assert facture_sur_un_an(national, volume).total_ttc == facture_sur_un_an(local, volume).total_ttc


def test_tarif_detaille(jeu) -> None:
    tarif = sispea.tarif_sispea(BESANCON)
    assert tarif.id == BESANCON
    assert tarif.nom == "Besançon (25) — SISPEA 2025"
    periode = tarif.periode_au(JUIN)
    assert periode.debut == date(2026, 1, 1)
    assert periode.source == "https://www.services.eaufrance.fr/service/247674/2025"
    eau, assainissement = periode.services
    assert (eau.code, assainissement.code) == ("eau_potable", "assainissement")
    assert eau.tva == Decimal("5.5")
    assert [c.nom for c in eau.composantes][:1] == ["Eau potable"]
    assert any(c.redevance for c in eau.composantes)


def test_deux_parts_collectivite_et_delegataire(jeu) -> None:
    """Délégation : part collectivité et part délégataire nommées, abonnements compris."""
    insee, ident = next(
        (insee, sid)
        for insee, c in jeu["communes"].items()
        for sid in c.get("eau_potable", [])
        if len(jeu["services"][sid]["v"]) == 2
    )
    dico = sispea.tarif_dict(jeu, f"sispea:{insee}:{ident}:")
    noms = [c["nom"] for c in dico["periodes"][0]["services"]["eau_potable"]["composantes"]]
    assert "Eau potable (part collectivité)" in noms
    assert "Eau potable (part délégataire)" in noms
    assert "assainissement" not in dico["periodes"][0]["services"]


def test_tranches_et_seuil_final() -> None:
    service = {
        "c": "eau_potable",
        "t": 5.5,
        "a": [["collectivité", 15]],
        "v": [["collectivité", [[3, 0.0], [100, 1.38], [None, 1.44]]]],
        "r": [["Redevance", 0.39, True]],
    }
    resultat = sispea._service(service)
    assert resultat["composantes"][0]["tranches"] == [
        {"jusqua": 3, "prix_ht": 0.0},
        {"jusqua": 100, "prix_ht": 1.38},
        {"prix_ht": 1.44},
    ]
    assert resultat["abonnements"] == [
        {"nom": "Abonnement eau potable (part collectivité)", "montant_ht_an": 15}
    ]


def test_abonnement_delegataire_seul_nomme_les_parts() -> None:
    service = {
        "c": "assainissement",
        "t": 10,
        "a": [["délégataire", 20]],
        "v": [["collectivité", [[None, 1]]]],
        "r": [],
    }
    assert sispea._service(service)["composantes"] == [
        {"nom": "Assainissement (part collectivité)", "prix_ht": 1}
    ]


# ── Recherche ──


@pytest.mark.parametrize(
    ("saisie", "attendu"),
    [
        ("25056", ["25056"]),
        ("Besançon (25056)", ["25056"]),
        ("  besancon ", ["25056"]),
        ("BESANÇON", ["25056"]),
        ("99999", []),
        ("Atlantide", []),
        ("", []),
    ],
)
def test_rechercher_communes(jeu, saisie, attendu) -> None:
    assert sispea.rechercher_communes(jeu, saisie) == attendu


def test_normaliser_saint() -> None:
    assert sispea.normaliser("St-Jean-de-Luz") == sispea.normaliser("Saint Jean de Luz")
    assert sispea.normaliser("Saint Jean de Luz") == "saint jean de luz"
    assert sispea.normaliser("Ste-Marie") == "sainte marie"


def test_homonymes(jeu) -> None:
    trouvees = sispea.rechercher_communes(jeu, "Saint-Martin")
    assert len(trouvees) > 1
    assert all(jeu["communes"][c]["n"] == "Saint-Martin" for c in trouvees)


def test_commune_sans_assainissement_collectif() -> None:
    jeu = {
        "exercice": 2025,
        "services": {"1": {"o": "Syndicat", "g": "Régie", "p": 2.5}},
        "communes": {"01001": {"n": "Village", "d": "01", "eau_potable": ["1"]}},
    }
    assert sispea.choix_commune(jeu, "01001") == [
        (
            "sispea:01001:1:",
            "Village (01) — eau potable : Syndicat (régie, 2.5 €/m³ TTC) ; assainissement : aucun service",
        )
    ]


# ── Erreurs ──


@pytest.mark.parametrize(
    ("ident", "message"),
    [
        ("sispea:00000:1:2", "commune absente"),
        ("sispea:25056", "commune absente"),
        ("sispea:25056:1:", "service 1 absent"),
        ("sispea:25056::", "aucun service"),
    ],
)
def test_identifiant_invalide(ident, message) -> None:
    with pytest.raises(TarifInvalide, match=message):
        sispea.tarif_sispea(ident)


def test_jeu_illisible(tmp_path) -> None:
    chemin = tmp_path / "sispea.json.gz"
    chemin.write_bytes(b"pas du gzip")
    with pytest.raises(TarifInvalide, match="illisible"):
        sispea.charger_jeu(chemin)


def test_jeu_format_inattendu(tmp_path) -> None:
    chemin = tmp_path / "sispea.json.gz"
    chemin.write_bytes(gzip.compress(json.dumps({"format": 99}).encode()))
    with pytest.raises(TarifInvalide, match="format"):
        sispea.charger_jeu(chemin)


def test_charger_tarif_config() -> None:
    config = {CONF_SOURCE: "sispea", CONF_PREREGLAGE: BESANCON, CONF_ASSAINISSEMENT: False}
    tarif = charger_tarif_config(config)
    assert [s.code for s in tarif.periode_au(JUIN).services] == ["eau_potable"]


# ── Formulaire ──


@pytest.fixture(autouse=True)
def capteur_index(hass: HomeAssistant) -> None:
    hass.states.async_set(INDEX, "123.456", {"unit_of_measurement": "m³", "state_class": "total_increasing"})


async def _jusqua_commune(hass: HomeAssistant):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NOM: "Maison", CONF_INDEX: INDEX}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_SOURCE: "sispea"})
    assert result["step_id"] == "commune"
    return result


def _suggestion(result) -> str | None:
    cle = next(k for k in result["data_schema"].schema if k.schema == CONF_COMMUNE)
    return (cle.description or {}).get("suggested_value")


async def test_formulaire_commune_unique(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(URL_GEO_COMMUNES, json=[{"nom": "Besançon", "code": "25056"}])
    result = await _jusqua_commune(hass)
    assert _suggestion(result) == "Besançon (25056)"
    assert result["description_placeholders"]["exercice"] == "2025"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_COMMUNE: "Besançon (25056)", CONF_ASSAINISSEMENT: True}
    )
    assert result["step_id"] == "releve"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_FACTURATION: "semestrielle", CONF_DATE_RELEVE: HIER, CONF_INDEX_RELEVE: 120}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SOURCE] == "sispea"
    assert result["data"][CONF_PREREGLAGE] == BESANCON
    assert result["data"][CONF_COMMUNE] == "Besançon (25056)"


@pytest.mark.parametrize(
    "reponse",
    [
        {"exc": aiohttp.ClientError()},
        {"exc": TimeoutError()},
        {"json": []},
        {"status": 500},
    ],
)
async def test_position_inconnue_pas_de_suggestion(hass: HomeAssistant, aioclient_mock, reponse) -> None:
    aioclient_mock.get(URL_GEO_COMMUNES, **reponse)
    result = await _jusqua_commune(hass)
    assert _suggestion(result) is None


async def test_formulaire_commune_inconnue(hass: HomeAssistant, aioclient_mock) -> None:
    aioclient_mock.get(URL_GEO_COMMUNES, json=[])
    result = await _jusqua_commune(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_COMMUNE: "Atlantide", CONF_ASSAINISSEMENT: True}
    )
    assert result["step_id"] == "commune"
    assert result["errors"] == {CONF_COMMUNE: "commune_inconnue"}


async def test_formulaire_homonymes(hass: HomeAssistant, aioclient_mock, jeu) -> None:
    aioclient_mock.get(URL_GEO_COMMUNES, json=[])
    result = await _jusqua_commune(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_COMMUNE: "Saint-Martin", CONF_ASSAINISSEMENT: False}
    )
    assert result["step_id"] == "commune_service"
    homonymes = sispea.rechercher_communes(jeu, "Saint-Martin")
    choix = [c for code in homonymes for c in sispea.choix_commune(jeu, code)]
    (cle, champ), *_ = result["data_schema"].schema.items()
    assert cle.default() == choix[0][0]
    assert [o["value"] for o in champ.config["options"]] == [v for v, _ in choix]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PREREGLAGE: choix[-1][0]}
    )
    assert result["step_id"] == "releve"


async def test_formulaire_jeu_illisible(hass: HomeAssistant, aioclient_mock, monkeypatch) -> None:
    aioclient_mock.get(URL_GEO_COMMUNES, json=[])

    def _illisible():
        raise TarifInvalide("sispea.json.gz : jeu SISPEA illisible")

    monkeypatch.setattr("custom_components.tarifeau.config_flow.charger_jeu", _illisible)
    result = await _jusqua_commune(hass)
    assert result["errors"] == {"base": "tarif_invalide"}
    assert "illisible" in result["description_placeholders"]["erreur"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_COMMUNE: "25056", CONF_ASSAINISSEMENT: True}
    )
    assert result["step_id"] == "commune"


async def test_formulaire_tarif_refuse(hass: HomeAssistant, aioclient_mock, monkeypatch) -> None:
    aioclient_mock.get(URL_GEO_COMMUNES, json=[])

    def _refuse(ident):
        raise TarifInvalide(f"{ident} : refusé")

    monkeypatch.setattr("custom_components.tarifeau.config_flow.charger_tarif", _refuse)
    result = await _jusqua_commune(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_COMMUNE: "25056", CONF_ASSAINISSEMENT: True}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "tarif_invalide"


async def test_options_reprennent_la_commune(hass: HomeAssistant, aioclient_mock) -> None:
    """En options, la commune déjà saisie est proposée (pas d'appel à la géolocalisation)."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=INDEX,
        data={
            CONF_NOM: "Maison",
            CONF_INDEX: INDEX,
            CONF_SOURCE: "sispea",
            CONF_PREREGLAGE: BESANCON,
            CONF_COMMUNE: "Besançon (25056)",
            CONF_ASSAINISSEMENT: True,
            CONF_FACTURATION: "semestrielle",
            CONF_DATE_RELEVE: "2026-01-02",
            CONF_INDEX_RELEVE: 100,
        },
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "tarif"})
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_SOURCE: "sispea"})
    assert result["step_id"] == "commune"
    assert _suggestion(result) == "Besançon (25056)"
    assert aioclient_mock.call_count == 0
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_COMMUNE: "Belfort", CONF_ASSAINISSEMENT: True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_PREREGLAGE] == "sispea:90010:117545:117546"
