"""Configuration depuis l'interface (issue #4).

Étapes : capteur d'index → source du tarif → préréglage ou prix fixe → dernière relève.
Les options permettent ensuite de changer de tarif ou de saisir une nouvelle relève.
Une entrée par capteur d'index : par exemple une par voisin du compteur wM-Bus.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlowWithReload
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry, entity_registry, selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CHAMPS_SOURCE,
    CONF_ASSAINISSEMENT,
    CONF_COMMUNE,
    CONF_DATE_RELEVE,
    CONF_DIAMETRE,
    CONF_ENTITE_PRIX,
    CONF_FACTURATION,
    CONF_FIN_PERIODE,
    CONF_INDEX,
    CONF_INDEX_RELEVE,
    CONF_MONTANT_FACTURE,
    CONF_NOM,
    CONF_PREREGLAGE,
    CONF_PRIX_FIXE,
    CONF_SOURCE,
    CONF_VOLUME_FACTURE,
    DOMAIN,
    FACTURATIONS,
    NOM_PAR_DEFAUT,
    SOURCE_ENTITE_PRIX,
    SOURCE_PREREGLAGE,
    SOURCE_PRIX_FIXE,
    SOURCE_SISPEA,
    SOURCE_TAUX_FACTURE,
    SOURCES,
    STATE_CLASSES_INDEX,
    UNITES_INDEX,
)
from .journal import resumer
from .modele import (
    PREFIXE_PERSO,
    Tarif,
    TarifInvalide,
    charger_tarif,
    diametres,
    prereglages,
    verifier_tarifs_perso,
)
from .sispea import charger_jeu, choix_commune, rechercher_communes
from .suivi import dossier_perso, prix_entite, unite_prix_valide

_LOGGER = logging.getLogger(__name__)

# Commune de la position de Home Assistant (API Découpage administratif, Etalab) : valeur par défaut.
URL_GEO_COMMUNES = "https://geo.api.gouv.fr/communes"
DELAI_GEO = 5  # secondes : au-delà, le champ reste vide


def index_actuel_m3(hass: HomeAssistant, entity_id: str) -> float | None:
    """Index actuel du capteur, converti en m³. ``None`` si indisponible ou non numérique."""
    _LOGGER.debug("index_actuel_m3: entrée | hass=%s, entity_id=%s", resumer(hass), resumer(entity_id))
    etat = hass.states.get(entity_id)
    if etat is None:
        _LOGGER.debug("index_actuel_m3: sortie | None")
        return None
    diviseur = UNITES_INDEX.get(etat.attributes.get(ATTR_UNIT_OF_MEASUREMENT, ""))
    try:
        _LOGGER.debug("index_actuel_m3: sortie")
        return float(etat.state) / diviseur if diviseur else None
    except ValueError:
        _LOGGER.debug("index_actuel_m3: sortie | None")
        return None


def _verifier_capteur(hass: HomeAssistant, entity_id: str) -> str | None:
    """Code d'erreur si le capteur ne convient pas comme index, sinon ``None``."""
    _LOGGER.debug("_verifier_capteur: entrée | hass=%s, entity_id=%s", resumer(hass), resumer(entity_id))
    etat = hass.states.get(entity_id)
    if etat is None:
        _LOGGER.debug("_verifier_capteur: sortie | 'capteur_absent'")
        return "capteur_absent"
    if etat.attributes.get(ATTR_UNIT_OF_MEASUREMENT) not in UNITES_INDEX:
        _LOGGER.debug("_verifier_capteur: sortie | 'unite'")
        return "unite"
    if etat.attributes.get("state_class") not in STATE_CLASSES_INDEX:
        _LOGGER.debug("_verifier_capteur: sortie | 'state_class'")
        return "state_class"
    _LOGGER.debug("_verifier_capteur: sortie | None")
    return None


# Marqueur stable d'Esphome-Compteur-wmbus (README « Marqueur stable pour les
# intégrations », Esphome-Compteur-wmbus#47) : fabricant et modèle de l'appareil
# (bloc `esphome: project:`), et nom d'origine du capteur d'index de la maison.
FABRICANT_WMBUS = "mathilde-team"
MODELE_WMBUS = "compteur-eau-wmbus"
NOM_INDEX_WMBUS = "Compteur eau - Volume total"


def _capteur_index_par_defaut(hass: HomeAssistant) -> str | None:
    """Capteur d'index à proposer par défaut, s'il est identifiable sans ambiguïté.

    [Esphome-Compteur-wmbus](https://github.com/Mathilde-Team/Esphome-Compteur-wmbus)
    expose l'index du compteur de la maison sous « Compteur eau - Volume total »
    (configuration par défaut `compteur-eau-lora`, entité
    `sensor.compteur_eau_lora_compteur_eau_volume_total`). Le capteur n'est
    proposé que s'il vient de l'intégration ESPHome (registre d'entités) et
    qu'il est le seul candidat (issue #43).

    Le marqueur stable prime : sur un appareil de fabricant « mathilde-team »
    et de modèle « compteur-eau-wmbus », seul le capteur de nom d'origine
    exactement « Compteur eau - Volume total » est candidat (jamais les index
    des voisins, quel que soit leur identifiant). À défaut d'appareil marqué
    (micrologiciel antérieur à Esphome-Compteur-wmbus#47), repli sur les
    conventions de nommage : identifiant finissant par ``volume_total`` ou nom
    contenant « compteur eau » et « volume total » — même renommé, le nom
    d'origine du registre est conservé.
    """
    _LOGGER.debug("_capteur_index_par_defaut: entrée | hass=%s", resumer(hass))
    registre = entity_registry.async_get(hass)
    appareils = device_registry.async_get(hass)
    # Un capteur déjà suivi par une entrée Tarif-Eau n'est plus proposé :
    # l'ajout d'une seconde facture (voisin) ne doit pas pré-choisir un
    # index qui mènerait à « déjà configuré ».
    deja_suivis = {entree.unique_id for entree in hass.config_entries.async_entries(DOMAIN)}
    marques: list[str] = []  # capteurs désignés par le marqueur stable
    candidats: list[str] = []  # repli : conventions de nommage
    for etat in hass.states.async_all("sensor"):
        if etat.entity_id in deja_suivis or _verifier_capteur(hass, etat.entity_id) is not None:
            continue
        entree = registre.async_get(etat.entity_id)
        if entree is None or entree.platform != "esphome":
            continue
        appareil = appareils.async_get(entree.device_id) if entree.device_id else None
        if appareil is not None and (appareil.manufacturer, appareil.model) == (
            FABRICANT_WMBUS,
            MODELE_WMBUS,
        ):
            # Appareil marqué : le contrat suffit, les conventions ne s'appliquent pas.
            if entree.original_name == NOM_INDEX_WMBUS:
                marques.append(etat.entity_id)
            continue
        nom = etat.entity_id.split(".", 1)[1]
        noms = " ".join(filter(None, [entree.original_name, etat.attributes.get("friendly_name")]))
        if nom.endswith("volume_total") or (
            "compteur eau" in noms.lower() and "volume total" in noms.lower()
        ):
            candidats.append(etat.entity_id)
    retenus = marques or candidats
    _LOGGER.debug(
        "_capteur_index_par_defaut: sortie | marqués=%d, par convention=%d", len(marques), len(candidats)
    )
    return retenus[0] if len(retenus) == 1 else None


async def _noms_prereglages(hass: HomeAssistant) -> list[selector.SelectOptionDict]:
    """Préréglages fournis, puis tarifs personnels de ``<config>/tarifeau/`` (même invalides, signalés)."""

    _LOGGER.debug("_noms_prereglages: entrée | hass=%s", resumer(hass))

    def lire() -> list[selector.SelectOptionDict]:
        _LOGGER.debug("_noms_prereglages.lire: entrée")
        options = [selector.SelectOptionDict(value=i, label=charger_tarif(i).nom) for i in prereglages()]
        for ident, tarif, _erreur in verifier_tarifs_perso(dossier_perso(hass)):
            fichier = ident.removeprefix(PREFIXE_PERSO)
            libelle = f"Mon tarif : {tarif.nom} ({fichier})" if tarif else f"Mon tarif : {fichier} (invalide)"
            options.append(selector.SelectOptionDict(value=ident, label=libelle))
        _LOGGER.debug("_noms_prereglages.lire: sortie | %s", resumer(options))
        return options

    _LOGGER.debug("_noms_prereglages: sortie")
    return await hass.async_add_executor_job(lire)


def _rapport_tarifs_perso(hass: HomeAssistant) -> str:
    """Texte Markdown : état de chaque fichier de ``<config>/tarifeau/`` (hors boucle d'événements)."""
    _LOGGER.debug("_rapport_tarifs_perso: entrée | hass=%s", resumer(hass))
    dossier = dossier_perso(hass)
    resultats = verifier_tarifs_perso(dossier)
    if not resultats:
        _LOGGER.debug("_rapport_tarifs_perso: sortie")
        return f"Aucun fichier `.yaml` dans `{dossier}`."
    lignes = []
    for ident, tarif, erreur in resultats:
        fichier = ident.removeprefix(PREFIXE_PERSO)
        if tarif:
            debut = tarif.periodes[0].debut.strftime("%d/%m/%Y")
            lignes.append(
                f"- **{fichier}** : valide, « {tarif.nom} », "
                f"{len(tarif.periodes)} période(s) depuis le {debut}"
            )
        else:
            lignes.append(f"- **{fichier}** : invalide. {erreur}")
    _LOGGER.debug("_rapport_tarifs_perso: sortie")
    return "\n".join(lignes)


def _schema_source(defaut: str = SOURCE_PREREGLAGE) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_SOURCE, default=defaut): selector.SelectSelector(
                selector.SelectSelectorConfig(options=list(SOURCES), translation_key=CONF_SOURCE)
            )
        }
    )


def _schema_prereglage(options: list[selector.SelectOptionDict], actuel: dict[str, Any]) -> vol.Schema:
    _LOGGER.debug("_schema_prereglage: entrée | options=%s, actuel=%s", resumer(options), resumer(actuel))
    defaut = actuel.get(CONF_PREREGLAGE, options[0]["value"])
    _LOGGER.debug("_schema_prereglage: sortie")
    return vol.Schema(
        {
            vol.Required(CONF_PREREGLAGE, default=defaut): selector.SelectSelector(
                selector.SelectSelectorConfig(options=options)
            ),
            vol.Required(CONF_ASSAINISSEMENT, default=actuel.get(CONF_ASSAINISSEMENT, True)): bool,
        }
    )


async def _commune_ha(hass: HomeAssistant) -> str | None:
    """« Nom (INSEE) » de la commune où se trouve Home Assistant, ``None`` si inconnue ou hors ligne."""
    _LOGGER.debug("_commune_ha: entrée | hass=%s", resumer(hass))
    params = {"lat": str(hass.config.latitude), "lon": str(hass.config.longitude), "fields": "nom,code"}
    try:
        reponse = await async_get_clientsession(hass).get(
            URL_GEO_COMMUNES, params=params, timeout=aiohttp.ClientTimeout(total=DELAI_GEO)
        )
        reponse.raise_for_status()
        commune = (await reponse.json())[0]
        resultat = f"{commune['nom']} ({commune['code']})"
    except (aiohttp.ClientError, TimeoutError, ValueError, KeyError, IndexError, TypeError) as err:
        _LOGGER.debug("_commune_ha: %s interceptée, pas de commune par défaut | %s", type(err).__name__, err)
        resultat = None
    _LOGGER.debug("_commune_ha: sortie | %s", resumer(resultat))
    return resultat


def _schema_commune(actuel: dict[str, Any], defaut: str | None) -> vol.Schema:
    _LOGGER.debug("_schema_commune: entrée | actuel=%s, defaut=%s", resumer(actuel), resumer(defaut))
    suggestion = actuel.get(CONF_COMMUNE) or defaut
    _LOGGER.debug("_schema_commune: sortie")
    return vol.Schema(
        {
            vol.Required(CONF_COMMUNE, description={"suggested_value": suggestion}): str,
            vol.Required(CONF_ASSAINISSEMENT, default=actuel.get(CONF_ASSAINISSEMENT, True)): bool,
        }
    )


def _schema_prix_fixe(actuel: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_PRIX_FIXE, default=actuel.get(CONF_PRIX_FIXE, 4.0)): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0,
                    max=50,
                    step="any",
                    unit_of_measurement="€/m³",
                    mode=selector.NumberSelectorMode.BOX,
                )
            )
        }
    )


def _schema_entite_prix() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_ENTITE_PRIX): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor")
            )
        }
    )


def _schema_taux_facture(actuel: dict[str, Any]) -> vol.Schema:
    _LOGGER.debug("_schema_taux_facture: entrée | actuel=%s", resumer(actuel))

    def nombre(unite: str) -> selector.NumberSelector:
        return selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0, step="any", unit_of_measurement=unite, mode=selector.NumberSelectorMode.BOX
            )
        )

    _LOGGER.debug("_schema_taux_facture: sortie")
    return vol.Schema(
        {
            vol.Required(CONF_MONTANT_FACTURE, default=actuel.get(CONF_MONTANT_FACTURE, 0)): nombre("€"),
            vol.Required(CONF_VOLUME_FACTURE, default=actuel.get(CONF_VOLUME_FACTURE, 0)): nombre("m³"),
        }
    )


def _verifier_entite_prix(hass: HomeAssistant, entity_id: str) -> str | None:
    """Code d'erreur si l'entité ne donne pas un prix au m³ exploitable, sinon ``None``."""
    _LOGGER.debug("_verifier_entite_prix: entrée | hass=%s, entity_id=%s", resumer(hass), resumer(entity_id))
    etat = hass.states.get(entity_id)
    if etat is None:
        _LOGGER.debug("_verifier_entite_prix: sortie | 'capteur_absent'")
        return "capteur_absent"
    if not unite_prix_valide(etat.attributes.get(ATTR_UNIT_OF_MEASUREMENT)):
        _LOGGER.debug("_verifier_entite_prix: sortie | 'unite_prix'")
        return "unite_prix"
    if prix_entite(etat) is None:
        _LOGGER.debug("_verifier_entite_prix: sortie | 'prix_indisponible'")
        return "prix_indisponible"
    _LOGGER.debug("_verifier_entite_prix: sortie | None")
    return None


def _schema_releve(actuel: dict[str, Any], index_m3: float | None) -> vol.Schema:
    _LOGGER.debug("_schema_releve: entrée | actuel=%s, index_m3=%s", resumer(actuel), resumer(index_m3))
    index_defaut = actuel.get(CONF_INDEX_RELEVE, round(index_m3, 3) if index_m3 is not None else 0)
    _LOGGER.debug("_schema_releve: sortie")
    return vol.Schema(
        {
            vol.Required(CONF_FACTURATION, default=actuel.get(CONF_FACTURATION, "semestrielle")): (
                selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=list(FACTURATIONS), translation_key=CONF_FACTURATION
                    )
                )
            ),
            vol.Required(CONF_DATE_RELEVE, default=actuel.get(CONF_DATE_RELEVE, date.today().isoformat())): (
                selector.DateSelector()
            ),
            vol.Optional(CONF_FIN_PERIODE, description={"suggested_value": actuel.get(CONF_FIN_PERIODE)}): (
                selector.DateSelector()
            ),
            vol.Required(CONF_INDEX_RELEVE, default=index_defaut): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, step="any", unit_of_measurement="m³", mode=selector.NumberSelectorMode.BOX
                )
            ),
        }
    )


def _verifier_releve(hass: HomeAssistant, entity_id: str, saisie: dict[str, Any]) -> dict[str, str]:
    _LOGGER.debug(
        "_verifier_releve: entrée | hass=%s, entity_id=%s, saisie=%s",
        resumer(hass),
        resumer(entity_id),
        resumer(saisie),
    )
    erreurs: dict[str, str] = {}
    if date.fromisoformat(saisie[CONF_DATE_RELEVE]) > date.today():
        erreurs[CONF_DATE_RELEVE] = "date_future"
    if (fin := saisie.get(CONF_FIN_PERIODE)) and fin <= saisie[CONF_DATE_RELEVE]:
        erreurs[CONF_FIN_PERIODE] = "fin_avant_debut"
    index_m3 = index_actuel_m3(hass, entity_id)
    if index_m3 is not None and saisie[CONF_INDEX_RELEVE] > index_m3 + 1e-6:
        erreurs[CONF_INDEX_RELEVE] = "index_incoherent"
    _LOGGER.debug("_verifier_releve: sortie | %s", resumer(erreurs))
    return erreurs


class _EtapesTarif:
    """Étapes communes à la configuration et aux options : tarif, puis relève."""

    hass: HomeAssistant
    _donnees: dict[str, Any]
    _diametres: list[int]
    _prereglage: dict[str, Any]
    _choix_sispea: list[tuple[str, str]]
    _saisie_sispea: dict[str, Any]

    def _actuel(self) -> dict[str, Any]:
        return self._donnees

    def _index_entity(self) -> str:
        return self._donnees[CONF_INDEX]

    async def async_step_tarif(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Source du tarif."""
        _LOGGER.debug("_EtapesTarif.async_step_tarif: entrée | user_input=%s", resumer(user_input))
        if user_input is not None:
            self._donnees[CONF_SOURCE] = user_input[CONF_SOURCE]
            if user_input[CONF_SOURCE] == SOURCE_PREREGLAGE:
                _LOGGER.debug("_EtapesTarif.async_step_tarif: sortie")
                return await self.async_step_prereglage()
            if user_input[CONF_SOURCE] == SOURCE_SISPEA:
                _LOGGER.debug("_EtapesTarif.async_step_tarif: sortie")
                return await self.async_step_commune()
            if user_input[CONF_SOURCE] == SOURCE_PRIX_FIXE:
                _LOGGER.debug("_EtapesTarif.async_step_tarif: sortie")
                return await self.async_step_prix_fixe()
            if user_input[CONF_SOURCE] == SOURCE_TAUX_FACTURE:
                _LOGGER.debug("_EtapesTarif.async_step_tarif: sortie")
                return await self.async_step_taux_facture()
            _LOGGER.debug("_EtapesTarif.async_step_tarif: sortie")
            return await self.async_step_entite_prix()
        _LOGGER.debug("_EtapesTarif.async_step_tarif: sortie")
        return self.async_show_form(
            step_id="tarif", data_schema=_schema_source(self._actuel().get(CONF_SOURCE, SOURCE_PREREGLAGE))
        )

    async def async_step_prereglage(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Choix d'un tarif de collectivité."""
        _LOGGER.debug("_EtapesTarif.async_step_prereglage: entrée | user_input=%s", resumer(user_input))
        erreurs: dict[str, str] = {}
        details = {"erreur": ""}
        if user_input is not None:
            try:
                tarif = await self.hass.async_add_executor_job(
                    charger_tarif, user_input[CONF_PREREGLAGE], dossier_perso(self.hass)
                )
            except TarifInvalide as err:
                _LOGGER.debug("_EtapesTarif.async_step_prereglage: TarifInvalide interceptée | %s", err)
                erreurs["base"], details["erreur"] = "tarif_invalide", str(err)
            else:
                _LOGGER.debug("_EtapesTarif.async_step_prereglage: sortie")
                return await self._apres_prereglage(user_input, tarif)
        options = await _noms_prereglages(self.hass)
        _LOGGER.debug("_EtapesTarif.async_step_prereglage: sortie")
        return self.async_show_form(
            step_id="prereglage",
            data_schema=_schema_prereglage(options, user_input or self._actuel()),
            errors=erreurs,
            description_placeholders={**details, "dossier": str(dossier_perso(self.hass))},
        )

    async def _apres_prereglage(self, user_input: dict[str, Any], tarif: Tarif) -> ConfigFlowResult:
        """Diamètre si l'abonnement en dépend, sinon le tarif est gardé."""
        _LOGGER.debug(
            "_EtapesTarif._apres_prereglage: entrée | user_input=%s, tarif=%s",
            resumer(user_input),
            resumer(tarif),
        )
        self._diametres = diametres(tarif)
        if self._diametres:
            self._prereglage = user_input
            _LOGGER.debug("_EtapesTarif._apres_prereglage: sortie")
            return await self.async_step_diametre()
        self._donnees.pop(CONF_DIAMETRE, None)  # préréglage précédent avec diamètre
        _LOGGER.debug("_EtapesTarif._apres_prereglage: sortie")
        return await self._garder(SOURCE_PREREGLAGE, user_input)

    async def async_step_diametre(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Diamètre du compteur, quand l'abonnement du préréglage en dépend."""
        _LOGGER.debug("_EtapesTarif.async_step_diametre: entrée | user_input=%s", resumer(user_input))
        if user_input is not None:
            _LOGGER.debug("_EtapesTarif.async_step_diametre: sortie")
            return await self._garder(
                SOURCE_PREREGLAGE, {**self._prereglage, CONF_DIAMETRE: int(user_input[CONF_DIAMETRE])}
            )
        actuel = self._actuel().get(CONF_DIAMETRE)
        defaut = actuel if actuel in self._diametres else self._diametres[0]
        schema = vol.Schema(
            {
                vol.Required(CONF_DIAMETRE, default=str(defaut)): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(value=str(dn), label=f"DN {dn}")
                            for dn in self._diametres
                        ]
                    )
                )
            }
        )
        _LOGGER.debug("_EtapesTarif.async_step_diametre: sortie")
        return self.async_show_form(step_id="diametre", data_schema=schema)

    async def async_step_commune(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Tarif national de la commune (open data SISPEA, issue #43)."""
        _LOGGER.debug("_EtapesTarif.async_step_commune: entrée | user_input=%s", resumer(user_input))
        erreurs: dict[str, str] = {}
        details = {"erreur": "", "exercice": "", "communes": ""}
        try:
            jeu = await self.hass.async_add_executor_job(charger_jeu)
        except TarifInvalide as err:
            _LOGGER.debug("_EtapesTarif.async_step_commune: TarifInvalide interceptée | %s", err)
            erreurs["base"], details["erreur"] = "tarif_invalide", str(err)
            jeu = None
        if jeu is not None:
            details |= {"exercice": str(jeu["exercice"]), "communes": str(len(jeu["communes"]))}
        if jeu is not None and user_input is not None:
            codes = await self.hass.async_add_executor_job(rechercher_communes, jeu, user_input[CONF_COMMUNE])
            if not codes:
                erreurs[CONF_COMMUNE] = "commune_inconnue"
            else:
                self._saisie_sispea = user_input
                self._choix_sispea = [c for code in codes for c in choix_commune(jeu, code)]
                if len(self._choix_sispea) == 1:
                    _LOGGER.debug("_EtapesTarif.async_step_commune: sortie | choix unique")
                    return await self._garder_sispea(self._choix_sispea[0][0])
                _LOGGER.debug("_EtapesTarif.async_step_commune: sortie | plusieurs choix")
                return await self.async_step_commune_service()
        defaut = None if self._actuel().get(CONF_COMMUNE) else await _commune_ha(self.hass)
        _LOGGER.debug("_EtapesTarif.async_step_commune: sortie")
        return self.async_show_form(
            step_id="commune",
            data_schema=_schema_commune({**self._actuel(), **(user_input or {})}, defaut),
            errors=erreurs,
            description_placeholders=details,
        )

    async def async_step_commune_service(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Plusieurs services (ou homonymes) pour la saisie : choix du couple eau / assainissement."""
        _LOGGER.debug("_EtapesTarif.async_step_commune_service: entrée | user_input=%s", resumer(user_input))
        if user_input is not None:
            _LOGGER.debug("_EtapesTarif.async_step_commune_service: sortie")
            return await self._garder_sispea(user_input[CONF_PREREGLAGE])
        options = [selector.SelectOptionDict(value=v, label=libelle) for v, libelle in self._choix_sispea]
        actuel = self._actuel().get(CONF_PREREGLAGE)
        defaut = actuel if actuel in {v for v, _ in self._choix_sispea} else self._choix_sispea[0][0]
        schema = vol.Schema(
            {
                vol.Required(CONF_PREREGLAGE, default=defaut): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=options)
                )
            }
        )
        _LOGGER.debug("_EtapesTarif.async_step_commune_service: sortie")
        return self.async_show_form(step_id="commune_service", data_schema=schema)

    async def _garder_sispea(self, ident: str) -> ConfigFlowResult:
        """Vérifie le tarif SISPEA choisi, puis le garde avec la saisie de la commune."""
        _LOGGER.debug("_EtapesTarif._garder_sispea: entrée | ident=%s", resumer(ident))
        try:
            await self.hass.async_add_executor_job(charger_tarif, ident)
        except TarifInvalide as err:
            _LOGGER.debug("_EtapesTarif._garder_sispea: TarifInvalide interceptée | %s", err)
            _LOGGER.debug("_EtapesTarif._garder_sispea: sortie")
            return self.async_abort(reason="tarif_invalide", description_placeholders={"erreur": str(err)})
        _LOGGER.debug("_EtapesTarif._garder_sispea: sortie")
        return await self._garder(SOURCE_SISPEA, {**self._saisie_sispea, CONF_PREREGLAGE: ident})

    async def async_step_prix_fixe(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Prix TTC au m³, abonnement compris."""
        _LOGGER.debug("_EtapesTarif.async_step_prix_fixe: entrée | user_input=%s", resumer(user_input))
        if user_input is not None:
            _LOGGER.debug("_EtapesTarif.async_step_prix_fixe: sortie")
            return await self._garder(SOURCE_PRIX_FIXE, user_input)
        _LOGGER.debug("_EtapesTarif.async_step_prix_fixe: sortie")
        return self.async_show_form(step_id="prix_fixe", data_schema=_schema_prix_fixe(self._actuel()))

    async def async_step_entite_prix(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Capteur de prix moyen au m³ (HydroTarif : prix total eau + assainissement)."""
        _LOGGER.debug("_EtapesTarif.async_step_entite_prix: entrée | user_input=%s", resumer(user_input))
        erreurs: dict[str, str] = {}
        if user_input is not None:
            erreur = _verifier_entite_prix(self.hass, user_input[CONF_ENTITE_PRIX])
            if erreur:
                erreurs[CONF_ENTITE_PRIX] = erreur
            else:
                _LOGGER.debug("_EtapesTarif.async_step_entite_prix: sortie")
                return await self._garder(SOURCE_ENTITE_PRIX, user_input)
        actuel = {**self._actuel(), **(user_input or {})}
        schema = self.add_suggested_values_to_schema(_schema_entite_prix(), actuel)
        _LOGGER.debug("_EtapesTarif.async_step_entite_prix: sortie")
        return self.async_show_form(step_id="entite_prix", data_schema=schema, errors=erreurs)

    async def async_step_taux_facture(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Montant TTC et volume de la dernière facture : taux effectif = montant ÷ volume."""
        _LOGGER.debug("_EtapesTarif.async_step_taux_facture: entrée | user_input=%s", resumer(user_input))
        erreurs: dict[str, str] = {}
        if user_input is not None:
            if user_input[CONF_VOLUME_FACTURE] <= 0:
                erreurs[CONF_VOLUME_FACTURE] = "volume_nul"
            else:
                _LOGGER.debug("_EtapesTarif.async_step_taux_facture: sortie")
                return await self._garder(SOURCE_TAUX_FACTURE, user_input)
        actuel = {**self._actuel(), **(user_input or {})}
        _LOGGER.debug("_EtapesTarif.async_step_taux_facture: sortie")
        return self.async_show_form(
            step_id="taux_facture", data_schema=_schema_taux_facture(actuel), errors=erreurs
        )

    async def _garder(self, source: str, saisie: dict[str, Any]) -> ConfigFlowResult:
        """Enregistre les champs de ``source`` et efface ceux des autres sources."""
        _LOGGER.debug("_EtapesTarif._garder: entrée | source=%s, saisie=%s", resumer(source), resumer(saisie))
        for autre, champs in CHAMPS_SOURCE.items():
            if autre != source:
                for champ in champs:
                    self._donnees.pop(champ, None)
        self._donnees.update(saisie)
        _LOGGER.debug("_EtapesTarif._garder: sortie")
        return await self._apres_tarif()

    async def async_step_releve(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Date et index de la dernière relève (repris de la facture)."""
        _LOGGER.debug("_EtapesTarif.async_step_releve: entrée | user_input=%s", resumer(user_input))
        erreurs: dict[str, str] = {}
        if user_input is not None:
            erreurs = _verifier_releve(self.hass, self._index_entity(), user_input)
            if not erreurs:
                self._donnees.pop(CONF_FIN_PERIODE, None)  # champ vidé : retour à la durée
                self._donnees.update(user_input)
                _LOGGER.debug("_EtapesTarif.async_step_releve: sortie")
                return self._terminer()
        actuel = {**self._actuel(), **(user_input or {})}
        schema = _schema_releve(actuel, index_actuel_m3(self.hass, self._index_entity()))
        _LOGGER.debug("_EtapesTarif.async_step_releve: sortie")
        return self.async_show_form(step_id="releve", data_schema=schema, errors=erreurs)

    async def _apres_tarif(self) -> ConfigFlowResult:
        return await self.async_step_releve()

    def _terminer(self) -> ConfigFlowResult:
        raise NotImplementedError  # pragma: no cover


class TarifeauConfigFlow(_EtapesTarif, ConfigFlow, domain=DOMAIN):
    """Assistant « Ajouter une intégration »."""

    VERSION = 1

    def __init__(self) -> None:
        _LOGGER.debug("TarifeauConfigFlow.__init__: entrée")
        self._donnees: dict[str, Any] = {}
        _LOGGER.debug("TarifeauConfigFlow.__init__: sortie")

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Nom de la facture et capteur d'index."""
        _LOGGER.debug("TarifeauConfigFlow.async_step_user: entrée | user_input=%s", resumer(user_input))
        erreurs: dict[str, str] = {}
        if user_input is not None:
            erreur = _verifier_capteur(self.hass, user_input[CONF_INDEX])
            if erreur:
                erreurs[CONF_INDEX] = erreur
            else:
                await self.async_set_unique_id(user_input[CONF_INDEX])
                self._abort_if_unique_id_configured()
                nom = user_input[CONF_NOM].strip() or NOM_PAR_DEFAUT
                self._donnees = {CONF_NOM: nom, CONF_INDEX: user_input[CONF_INDEX]}
                _LOGGER.debug("TarifeauConfigFlow.async_step_user: sortie")
                return await self.async_step_tarif()

        champ_index: Any = vol.Required(CONF_INDEX)
        if (defaut := _capteur_index_par_defaut(self.hass)) is not None:
            champ_index = vol.Required(CONF_INDEX, default=defaut)
        schema = vol.Schema(
            {
                vol.Required(CONF_NOM, default=NOM_PAR_DEFAUT): str,
                champ_index: selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor")),
            }
        )
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(schema, user_input)
        _LOGGER.debug("TarifeauConfigFlow.async_step_user: sortie")
        return self.async_show_form(step_id="user", data_schema=schema, errors=erreurs)

    def _terminer(self) -> ConfigFlowResult:
        return self.async_create_entry(title=self._donnees[CONF_NOM], data=self._donnees)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> TarifeauOptionsFlow:
        """Options : changer de tarif, nouvelle relève."""
        return TarifeauOptionsFlow()


class TarifeauOptionsFlow(_EtapesTarif, OptionsFlowWithReload):
    """Options modifiables après coup. L'entrée est rechargée à l'enregistrement."""

    def __init__(self) -> None:
        _LOGGER.debug("TarifeauOptionsFlow.__init__: entrée")
        self._donnees: dict[str, Any] = {}
        _LOGGER.debug("TarifeauOptionsFlow.__init__: sortie")

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Menu : changer de tarif, ou saisir une nouvelle relève."""
        _LOGGER.debug("TarifeauOptionsFlow.async_step_init: entrée | user_input=%s", resumer(user_input))
        self._donnees = {**self.config_entry.data, **self.config_entry.options}
        _LOGGER.debug("TarifeauOptionsFlow.async_step_init: sortie")
        return self.async_show_menu(step_id="init", menu_options=["tarif", "releve", "verifier"])

    async def async_step_verifier(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Vérifie les fichiers de tarif personnels, sans rien modifier (issue #17)."""
        _LOGGER.debug("TarifeauOptionsFlow.async_step_verifier: entrée | user_input=%s", resumer(user_input))
        if user_input is not None:
            _LOGGER.debug("TarifeauOptionsFlow.async_step_verifier: sortie")
            return await self.async_step_init()
        rapport = await self.hass.async_add_executor_job(_rapport_tarifs_perso, self.hass)
        _LOGGER.debug("TarifeauOptionsFlow.async_step_verifier: sortie")
        return self.async_show_form(
            step_id="verifier",
            data_schema=vol.Schema({}),
            description_placeholders={"dossier": str(dossier_perso(self.hass)), "rapport": rapport},
        )

    def _terminer(self) -> ConfigFlowResult:
        _LOGGER.debug("TarifeauOptionsFlow._terminer: entrée")
        options = {k: v for k, v in self._donnees.items() if k not in (CONF_NOM, CONF_INDEX)}
        _LOGGER.debug("TarifeauOptionsFlow._terminer: sortie")
        return self.async_create_entry(data=options)

    async def _apres_tarif(self) -> ConfigFlowResult:
        return self._terminer()  # changer de tarif ne demande pas de nouvelle relève
