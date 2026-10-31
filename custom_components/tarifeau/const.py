"""Constantes de l'intégration tarifeau."""

DOMAIN = "tarifeau"
NOM_PAR_DEFAUT = "Facture d'eau"

CONF_NOM = "name"
CONF_INDEX = "index_entity"  # capteur d'index (m³ ou L, total_increasing)
CONF_SOURCE = "source_tarif"
CONF_PREREGLAGE = "prereglage"
CONF_PRIX_FIXE = "prix_fixe"  # € TTC/m³
CONF_ENTITE_PRIX = "entite_prix"  # capteur de prix moyen €/m³ (HydroTarif…)
CONF_MONTANT_FACTURE = "montant_facture"  # € TTC de la dernière facture (source « taux de la facture »)
CONF_VOLUME_FACTURE = "volume_facture"  # m³ de la dernière facture
CONF_FACTURATION = "facturation"
CONF_DATE_RELEVE = "date_releve"  # AAAA-MM-JJ
CONF_FIN_PERIODE = "fin_periode"  # AAAA-MM-JJ, facultatif : fin réelle de la période (sinon durée)
CONF_INDEX_RELEVE = "index_releve"  # m³, repris de la facture
CONF_ASSAINISSEMENT = "assainissement"
CONF_DIAMETRE = "diametre"  # DN du compteur en mm, si l'abonnement en dépend
CONF_COMMUNE = "commune"  # saisie de la source SISPEA : « Besançon (25056) », nom ou code INSEE

SOURCE_PREREGLAGE = "prereglage"
SOURCE_PRIX_FIXE = "prix_fixe"
SOURCE_ENTITE_PRIX = "entite_prix"
SOURCE_TAUX_FACTURE = "taux_facture"
SOURCE_SISPEA = "sispea"  # tarif national de la commune (open data SISPEA, issue #43)
SOURCES = (SOURCE_PREREGLAGE, SOURCE_SISPEA, SOURCE_PRIX_FIXE, SOURCE_ENTITE_PRIX, SOURCE_TAUX_FACTURE)
# Champs propres à chaque source : effacés quand on change de source.
CHAMPS_SOURCE = {
    SOURCE_PREREGLAGE: ("prereglage", "assainissement", "diametre"),
    SOURCE_SISPEA: ("prereglage", "assainissement", "commune"),
    SOURCE_PRIX_FIXE: ("prix_fixe",),
    SOURCE_ENTITE_PRIX: ("entite_prix",),
    SOURCE_TAUX_FACTURE: ("montant_facture", "volume_facture"),
}

SERVICE_SAISIR_FACTURE = "saisir_facture"
EVENEMENT_ECART = "tarifeau_ecart_facture"  # écart au-delà du seuil (automatisations, ntfy)
SEUIL_ECART = 10  # % : au-delà, alerte dans Paramètres > Réparations
HISTORIQUE_MAX = 60  # factures gardées par entrée (30 ans en semestriel)

# Durée d'une période de facturation, à partir de la date de relève (issue #20).
MOIS_PAR_FACTURATION = {
    "mensuelle": 1,
    "bimestrielle": 2,
    "trimestrielle": 3,
    "quadrimestrielle": 4,
    "semestrielle": 6,
    "annuelle": 12,
}
FACTURATIONS = tuple(MOIS_PAR_FACTURATION)
UNITES_INDEX = {"m³": 1, "L": 1000}  # unité → diviseur vers le m³
STATE_CLASSES_INDEX = ("total_increasing", "total")
