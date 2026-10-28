# tarifeau

Intégration Home Assistant qui calcule **ta facture d'eau réelle** (France) à partir de **ta consommation** : abonnement, tranches, eau potable, assainissement, redevances des agences de l'eau et TVA, sur ta période de facturation. Elle vient avec un tableau de bord.

> Projet en démarrage : la feuille de route est dans les [issues](https://github.com/Mathilde-Team/tarifeau/issues). Il est né de [Esphome-Compteur-wmbus#20](https://github.com/Mathilde-Team/Esphome-Compteur-wmbus/issues/20).

## Pourquoi

- Les intégrations d'opérateurs (Veolia, Suez, Eau d'Île-de-France, Eau du Grand Lyon…) lisent la consommation sur le portail client, avec un prix moyen.
- [HydroTarif](https://github.com/jul-fls/ha-hydrotarif) donne le **prix moyen par commune** d'après SISPEA : TTC, calculé sur 120 m³/an, abonnement réparti.
- Il manque le **calcul de la facture d'après la conso réelle**, à partir de n'importe quel capteur d'index : compteur wM-Bus [Esphome-Compteur-wmbus](https://github.com/Mathilde-Team/Esphome-Compteur-wmbus), capteur à impulsions, intégration d'opérateur… Par exemple, à Besançon, les 3 premiers m³ sont gratuits et la TVA vaut 5,5 % pour l'eau potable, 10 % pour l'assainissement.

tarifeau complète ces intégrations sans les refaire. Il peut se servir de HydroTarif comme source de prix par défaut.

## Installation (HACS)

> Dernière version : `v0.2.0`, voir le [journal des versions](CHANGELOG.md). Home Assistant 2026.3 ou plus récent.

1. HACS → menu ⋮ → **Dépôts personnalisés** → `https://github.com/Mathilde-Team/tarifeau`, catégorie **Intégration**.
2. Installer **tarifeau**, puis redémarrer Home Assistant (2026.3 ou plus récent, pour l'icône fournie dans `brand/`).
3. **Paramètres → Appareils et services → Ajouter une intégration → tarifeau**, puis :
   1. le **capteur d'index** du compteur (m³ ou L, `state_class` `total_increasing` ou `total`), par exemple « Compteur eau - Volume total » d'[Esphome-Compteur-wmbus](https://github.com/Mathilde-Team/Esphome-Compteur-wmbus). Une facture par compteur : une par voisin, par exemple ;
   2. la **source du tarif** : le tarif d'une collectivité (avec ou sans assainissement collectif), un prix fixe TTC au m³, le **taux de ta dernière facture** (montant TTC ÷ volume), ou une **entité de prix** (voir plus bas) ;
   3. la **dernière relève**, qui marque le début de la période : date et index repris de ta dernière facture, et durée de la période facturée (1, 2, 3, 4, 6 ou 12 mois, par exemple 3 mois à partir du 1er avril pour une facture trimestrielle). Si ta facture ne suit pas un rythme régulier, indique aussi la **fin de la période** (par exemple du 05/09/2025 au 18/08/2026) : elle remplace la durée.

   Ensuite, **Configurer** permet de changer de tarif ou de saisir une nouvelle relève à chaque facture.

   ![Ajout d'un compteur : nom de la facture et capteur d'index](docs/captures/ajout-compteur.png)

### Installation à la main

Copie le dossier `custom_components/tarifeau/` de la [dernière version](https://github.com/Mathilde-Team/tarifeau/releases) dans le dossier `custom_components/` de ta configuration Home Assistant (à côté de `configuration.yaml`), redémarre, puis reprends à l'étape 3. Les mises à jour se font de la même façon : remplacer le dossier, puis redémarrer.

### Exemple complet avec Esphome-Compteur-wmbus

Le guide [docs/esphome-compteur-wmbus.md](docs/esphome-compteur-wmbus.md) couvre une facture par voisin, le tableau de bord Énergie, et le prix du prochain m³ recopié dans l'ESP, pour que son écran et ses coûts suivent la vraie tranche.

## Capteurs

Un appareil « Facture d'eau » par entrée, recalculé à chaque nouvel index (et à minuit) :

| Capteur | Unité | Usage |
|---|---|---|
| Coût depuis la dernière relève | € TTC | détail HT, TVA et TTC par service et ligne par ligne en attributs, avec le tarif et sa source |
| Coût aujourd'hui, cette semaine, du mois | € TTC | depuis minuit, depuis lundi et depuis le 1er du mois (ou depuis la relève, si elle est plus récente), part d'abonnement comprise |
| Coût hier, semaine dernière, mois dernier | € TTC | coût de la période terminée, figé au changement de jour, de semaine ou de mois et gardé au redémarrage. « Inconnu » si Home Assistant ne tournait pas à ce moment-là |
| Estimation de la prochaine facture | € TTC | projection à la fin de la période (durée choisie après la relève), au rythme actuel |
| Prix marginal | €/m³ TTC | prix du prochain m³ selon la tranche : **entité de prix pour le tableau de bord Énergie** (Eau) |
| Prix moyen | €/m³ TTC | total / volume, abonnements compris |
| Volume depuis la dernière relève | m³ | |
| Tranche actuelle | 1, 2, 3… | attribut `volume_restant` avant la tranche suivante |

Les capteurs gardent leur dernière valeur si l'index devient indisponible, et la retrouvent au redémarrage. Si le tarif de l'année n'est pas encore dans le préréglage, le dernier tarif connu est prolongé : l'attribut `tarif_prolonge` passe alors à `true` et un avertissement est écrit dans le journal.

**Tableau de bord Énergie** : Paramètres → Tableaux de bord → Énergie → Consommation d'eau → ajouter le capteur d'index, puis « Utiliser une entité avec le prix actuel » → **Prix marginal**.

## Source « entité de prix » (HydroTarif)

Ta collectivité n'a pas encore de préréglage ? [HydroTarif](https://github.com/jul-fls/ha-hydrotarif) donne, pour chaque commune, le prix de l'eau publié dans l'observatoire national [SISPEA](https://www.services.eaufrance.fr/). Choisis la source « Entité de prix », puis son capteur « Prix total » (en €/m³). N'importe quel capteur en €/m³ convient aussi.

- Le calcul reprend chaque nouveau prix dès qu'il est publié. Si l'entité devient indisponible, le dernier prix connu est conservé.
- Les attributs `source` et `date_source` reprennent la page SISPEA et la date d'application (données de l'année N, appliquées au 1er janvier de N+1).
- **Limite** : c'est un **prix moyen** pour 120 m³ par an, abonnement compris. Il n'y a ni tranches ni abonnement séparé, et le « prix du prochain m³ » vaut ce prix moyen (attribut `prix_moyen_seul: true`). Un préréglage reste plus juste : voir [tarifs/CONTRIBUER.md](custom_components/tarifeau/tarifs/CONTRIBUER.md).

## Recaler sur une facture réelle

À chaque facture reçue, appelle l'action **« Saisir une facture »** (`tarifeau.saisir_facture`), depuis Outils de développement → Actions ou depuis une automatisation :

```yaml
action: tarifeau.saisir_facture
data:
  config_entry_id: 01J…  # l'entrée « Facture d'eau »
  debut: "2026-01-01"    # relève précédente
  fin: "2026-07-01"      # relève de cette facture
  volume: 60             # m³ facturés
  montant_ttc: 251.37
  index_releve: 160      # facultatif
```

- L'action compare la facture au calcul de l'intégration sur la même période. Le capteur **« Écart avec la dernière facture »** (en €) donne le montant facturé, le calcul, l'écart en %, le taux effectif et les 12 dernières factures. L'action renvoie aussi ces valeurs en réponse.
- La **relève est recalée** : la date et l'index deviennent ceux de la facture. Sans index, la relève avance du volume facturé, à condition que la facture parte de la relève actuelle. Une facture plus ancienne que la relève est seulement gardée dans l'historique.
- Si l'écart dépasse **10 %**, une alerte apparaît dans Paramètres → Réparations : tarif changé, fuite, compteur remplacé ou faute de frappe. Elle disparaît avec la prochaine facture qui reste sous le seuil.
- Avec la source « Taux de la dernière facture », chaque saisie met aussi le taux à jour.

### Notification ntfy

Au-delà du seuil, tarifeau émet aussi l'événement **`tarifeau_ecart_facture`**, avec ces données : `config_entry_id`, `nom`, `debut`, `fin`, `volume`, `montant_ttc`, `calcul_ttc`, `ecart`, `ecart_pct`, `seuil_pct`.

Le blueprint [`blueprints/automation/tarifeau/ecart_facture_ntfy.yaml`](blueprints/automation/tarifeau/ecart_facture_ntfy.yaml) en fait une notification ntfy, avec l'action `ntfy.publish` de l'intégration ntfy de Home Assistant (déjà utilisée par Esphome-Compteur-wmbus-ntfy). Il se règle en trois champs : le sujet ntfy, la facture (vide = toutes) et la priorité.

[![Importer le blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2FMathilde-Team%2Ftarifeau%2Fblob%2Fdev%2Fblueprints%2Fautomation%2Ftarifeau%2Fecart_facture_ntfy.yaml)

Pour un autre canal (application mobile, e-mail…), il suffit d'une automatisation déclenchée par cet événement.
- L'historique (60 factures au plus) est gardé dans le stockage de Home Assistant, et supprimé avec l'entrée.

## Tableau de bord

![Tableau de bord « Facture d'eau » : jauge, prix, coût par jour et par mois, détail de la facture en cours](docs/captures/tableau-de-bord.png)

[`dashboard/facture_eau.yaml`](dashboard/facture_eau.yaml) est une vue « Facture d'eau » faite uniquement de cartes natives : jauge du coût depuis la relève, estimation, prix marginal et prix moyen, volume, tranche, coût d'aujourd'hui, d'hier, de cette semaine, de la semaine dernière, du mois et du mois dernier, graphiques du coût par jour, par semaine et par mois (statistiques), et détail ligne par ligne, regroupé par service (carte Markdown avec la quantité, le prix, le HT, la TVA et le TTC de chaque service, ainsi que le lien vers la délibération). Trois sections par compteur : résumé, coûts, détail. Les captures viennent d'un vrai Home Assistant 2026.2.3, avec un compteur simulé et des statistiques simulées.

Il suppose une entrée nommée « Facture d'eau ». Les capteurs ont des identifiants fixes, quelle que soit la langue : `sensor.<nom>_<clé>`, par exemple `sensor.facture_d_eau_prix_marginal`. Pour d'autres noms, ou une section par compteur :

```bash
uv run --with pyyaml --with loguru python scripts/generer_tableau_de_bord.py "Maison" "Dupont" --max 400 -o mon_tableau.yaml
```

Puis Paramètres → Tableaux de bord → Ajouter un tableau de bord → à partir de zéro → ⋮ → Éditeur de configuration brute, et coller le fichier.

## Décrire un tarif

Un tarif est un fichier YAML, rangé dans `custom_components/tarifeau/tarifs/` (préréglages) ou dans `/config/tarifeau/` (tarifs personnels). Le format est documenté en tête de `modele.py` :

- une ou plusieurs **périodes** (dates d'effet), chacune avec l'URL de la délibération ;
- pour chaque **service** (`eau_potable`, `assainissement`, `autre`) : son taux de TVA, ses abonnements (€ HT/an) et ses composantes au m³ (€ HT/m³), éventuellement par **tranches annuelles** ;
- les **redevances** de l'agence de l'eau sont des composantes marquées `redevance: true`.

Préréglages fournis :

| Collectivité | Depuis | 120 m³ | Source |
|---|---|---|---|
| Grand Besançon Métropole (régie) | 01/01/2026 | 484,23 € TTC, égal à la facture type officielle | [RPQS 2025](https://datasets.grandbesancon.fr/fichiers/delibs/grandBesancon/conseilCommunautaire/2026/20260924/0000C9DA.PDF) |
| Grand Belfort (régie) | 01/05/2026 | 624,32 € TTC (assainissement non assujetti à la TVA) | [page Abonnements et tarifs](https://www.grandbelfort.fr/eau/abonnements-et-tarifs.html) |

Ta commune manque ? Ouvre une issue **« Ajouter ma commune »**, ou suis le guide [`tarifs/CONTRIBUER.md`](custom_components/tarifeau/tarifs/CONTRIBUER.md).

### Mon propre tarif (fichier YAML)

Sans attendre un préréglage, tu peux écrire ton tarif complet dans un fichier YAML personnel, au même format :

1. Crée le dossier `tarifeau` à côté de `configuration.yaml` (`/config/tarifeau/`), par exemple avec l'extension File editor ou Studio Code Server.
2. Copie-y [`docs/exemples/mon_tarif.yaml`](docs/exemples/mon_tarif.yaml) sous le nom `ma_commune.yaml`, puis remplace les valeurs par celles de ta facture.
3. **Configurer → Vérifier mes fichiers de tarif** : chaque fichier est déclaré valide, ou l'erreur est indiquée avec le chemin de la valeur fautive (par exemple `tarif.periodes[0].services.eau_potable.composantes[0].tranches[0].prix_ht`).
4. **Configurer → Changer de tarif → Tarif d'une collectivité** : ton fichier apparaît sous « Mon tarif : … ».

Après une modification d'un fichier déjà utilisé, recharge l'intégration (⋮ → Recharger). Si le fichier disparaît ou devient invalide, l'entrée s'arrête avec le message d'erreur, plutôt que de calculer une facture fausse. Le guide complet est dans [`docs/tarif-personnel.md`](docs/tarif-personnel.md). Un tarif qui marche peut ensuite être proposé au dépôt, pour en faire un préréglage.

## Limites

- **Le montant reste indicatif** : seule la facture de ton service fait foi. Saisis tes factures (action « Saisir une facture ») pour vérifier l'écart.
- **Zones et services** : un préréglage décrit **un service** (une régie, un contrat de délégation), pas une adresse. Une même agglomération peut avoir plusieurs services ou plusieurs zones tarifaires, avec des tarifs différents. Vérifie que la délibération du préréglage est bien celle qui figure sur ta facture.
- **Assainissement non collectif** (fosse septique) : décoche « Raccordé au tout-à-l'égout ». La redevance du SPANC (contrôles, vidanges) n'est pas calculée, car elle ne dépend pas du volume.
- **Tarifs annuels** : beaucoup changent au 1er janvier. Tant que le nouveau tarif n'est pas dans le préréglage, le dernier connu est prolongé (attribut `tarif_prolonge`).
- **Prix moyen** (sources « prix fixe », « entité de prix », « taux de la dernière facture ») : pas de tranches, et l'abonnement est réparti dans le prix au m³.
- **Hors France** : le modèle de facture (TVA par service, redevances des agences de l'eau) est propre à la France.

## Diagnostic

Pour suivre le calcul pas à pas (entrée et sortie de chaque fonction, exceptions interceptées), activer le journal de debug dans `configuration.yaml` :

```yaml
logger:
  logs:
    custom_components.tarifeau: debug
```

Les paramètres sont résumés (tailles, clés, types, identifiant et état des capteurs suivis) : le journal ne contient ni objet entier ni valeur de saisie.

## Comment la facture est calculée

Le moteur (`custom_components/tarifeau/calcul.py`, Python pur) reprend les règles des factures :

- la période facturée est découpée au 1er janvier et à chaque changement de tarif. Le volume est réparti **au prorata des jours** ;
- l'**abonnement** est proratisé au jour près (365 ou 366 jours) ;
- les **tranches** sont annuelles. Elles s'appliquent au volume cumulé depuis le 1er janvier (`cumul_annuel`, par défaut), ou avec des seuils proratisés selon la durée de la facture (`prorata`). Le préréglage Grand Besançon utilise `prorata`, vérifié sur une facture réelle (3 m³ x 348 / 365 = 2,86 m³ gratuits). La gratuité des premiers m³ est une tranche à 0 € ;
- on calcule le HT par ligne, la TVA **par service** (5,5 % pour l'eau potable, 10 % pour l'assainissement), puis le TTC. Seul le total est arrondi au centime ;
- sorties pour les capteurs : **prix marginal** du prochain m³ (TTC), **prix moyen**, tranche actuelle et volume restant avant la tranche suivante ;
- si l'index revient en arrière, le compteur est considéré comme remplacé.

## Données et licences

- **Origine des barèmes** : chaque préréglage de `custom_components/tarifeau/tarifs/` recopie les tarifs votés par la collectivité qui gère le service : abonnements, prix au m³, tranches, redevances de l'agence de l'eau perçues sur la facture, taux de TVA. Chaque période du fichier donne l'URL de sa source (`source`) et, s'il y a lieu, la date de la délibération (`date_source`). Ces deux informations sont reprises dans les attributs du capteur « Coût depuis la dernière relève ».
  - Grand Besançon Métropole : délibération tarifaire du 11/12/2025, reprise dans le RPQS 2025 (conseil communautaire du 24/09/2026). Les délibérations sont publiées sur [OpenData Besançon](https://data.grandbesancon.fr/opendata/dataset/conseilsCommunautaires) sous **Licence Ouverte** (Etalab).
  - Grand Belfort : page publique [Abonnements et tarifs](https://www.grandbelfort.fr/eau/abonnements-et-tarifs.html) de la Direction du cycle de l'eau.
- **TVA** : taux légaux (5,5 % pour l'eau potable, 10 % pour l'assainissement collectif), tels qu'ils figurent sur les délibérations et les factures. La source fait foi si elle en indique d'autres, comme pour l'assainissement du Grand Belfort, qui n'est pas soumis à la TVA.
- **Conditions de réutilisation** : les tarifs votés par une collectivité sont des informations publiques, librement réutilisables à condition de ne pas les altérer et d'en mentionner la source et la date de mise à jour (articles L321-1 et L322-1 du Code des relations entre le public et l'administration). Les préréglages respectent cette règle : valeurs recopiées sans modification, URL et date de la source conservées. Seuls les montants sont repris, jamais les documents eux-mêmes.
- **Source « entité de prix »** : les prix moyens lus sur une entité [HydroTarif](https://github.com/jul-fls/ha-hydrotarif) proviennent de SISPEA / Eaufrance. Leur source est l'URL exposée par cette entité (`water_source_url`).
- **Tarifs personnels** : un fichier de `/config/tarifeau/` reste sous la responsabilité de son auteur, qui doit indiquer sa propre source.
- **Code** : licence [MIT](LICENSE) (intégration Home Assistant, exception SOCLE §1).

## Développement

```bash
uv run --only-group lint ruff check . && uv run --only-group lint ruff format --check .
uv run -p 3.13 --only-group test pytest --cov     # Home Assistant 2026.2.3 (pytest-homeassistant-custom-component)
```

| Dossier | Rôle |
|---|---|
| `custom_components/tarifeau/` | L'intégration (seul dossier installé par HACS) |
| `custom_components/tarifeau/calcul.py` | Moteur de calcul en Python pur, sans Home Assistant |
| `custom_components/tarifeau/brand/` | Icône (256 et 512 px) |
| `tests/` | Tests pytest |

La CI lance aussi **hassfest** et la **validation HACS** (job « Validation Home Assistant »). Ce sont des actions Docker : elles tournent sur `ubuntu-latest` tant que la variable `RUNS_ON_DOCKER` n'est pas définie.

## Socle qualité Mathilde-Team

Ce dépôt est créé depuis [`modele-depot`](https://github.com/Mathilde-Team/modele-depot). Il suit le [socle qualité de l'équipe](https://github.com/Mathilde-Team/.github/blob/main/SOCLE.md) :

- **`main`** (livraison) et **`dev`** (intégration) protégées. On n'y pousse jamais directement : PR vers `dev`, puis PR `dev` → `main`.
- **CI** : job « Qualité (lint, tests) », requis sur les deux branches. À adapter au langage du projet dans `.github/workflows/ci.yml`, en gardant le nom du job.
- **Garde** : `.github/workflows/guard-main.yml` refuse les PR vers `main` qui ne viennent pas de `dev`.
- **Protections versionnées** : `.github/branch-protection/{main,dev}.json`, vérifiées chaque jour par le workflow « Protection des branches ».
- **Licence** : [MIT](LICENSE).

### Après « Use this template »

Les protections ne sont pas copiées par GitHub avec le modèle. Un administrateur les applique une fois :

```bash
gh repo create Mathilde-Team/<nom> --public --template Mathilde-Team/modele-depot --clone
cd <nom>
scripts/appliquer_socle.sh Mathilde-Team/<nom>     # crée dev + applique les protections
```

Puis pour chaque sujet :

```bash
git switch dev && git pull && git switch -c feat/<n°>-description
# …
gh pr create --base dev
```
