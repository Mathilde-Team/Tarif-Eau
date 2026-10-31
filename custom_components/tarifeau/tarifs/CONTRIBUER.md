# Ajouter le tarif de ta collectivité

Chaque fichier de ce dossier est un **préréglage** : le tarif d'un **service** d'eau (une régie, un délégataire…), et non celui d'une adresse. Un même territoire peut avoir plusieurs zones ou services : par exemple, le Grand Besançon a une régie, d'anciens contrats de délégation (ex-Osselle, ex-Haute-Loue…) et le SIVOM de la Vallée.

Avant tout : la source « Ma commune (données nationales SISPEA) » donne déjà le tarif détaillé de plus de 30 000 communes (`sispea.json.gz`, produit par `scripts/generer_sispea.py`, voir le README). Un préréglage reste utile si SISPEA écarte ton service, s'il est en retard d'un an, ou si la facture applique une règle que SISPEA ne décrit pas (tranches proratisées, abonnement selon le diamètre…).

Plus simple : ouvre une issue **« Ajouter ma commune »** avec les valeurs, et elle sera saisie pour toi.

## 1. Trouver la source officielle

Dans l'ordre :

1. la **délibération tarifaire** du conseil communautaire ou syndical (souvent en décembre, pour le 1er janvier) ;
2. la page **« tarifs »** du site de la collectivité ou du service des eaux ;
3. le **RPQS**, rapport annuel sur le prix et la qualité du service. Il donne souvent les tarifs de l'année suivante et une **facture type de 120 m³** ;
4. la fiche du service sur [services.eaufrance.fr](https://www.services.eaufrance.fr/) (SISPEA), pour recouper. Elle est souvent en retard d'un an.

Le site de l'opérateur ou une page d'actualité ne suffit pas comme source unique.

## 2. Saisir les composantes

Copie `grand_besancon_regie.yaml` sous le nom `<id>.yaml`. L'`id` (minuscules, `_`) doit être le nom du fichier.

| Sur la délibération | Dans le fichier |
|---|---|
| Abonnement, part fixe, prime fixe (€ HT **par an**) | `abonnements: - {nom, montant_ht_an}`. Une part fixe semestrielle se multiplie par 2 |
| Abonnement selon le diamètre du compteur (DN 15, DN 20…) | `montant_ht_an` = montant du compteur le plus courant (DN 15 en général), plus `par_diametre: {15: 40.00, 20: 64.00}` (DN en mm → € HT par an). Le formulaire demande alors le diamètre |
| Part variable, prix au m³, consommation (€ HT/m³) | `composantes: - {nom, prix_ht}` |
| Tranches (0 à 30 m³, 30 à 100 m³…) | `tranches:` avec des seuils `jusqua` **annuels** croissants ; la dernière n'a pas de seuil |
| m³ gratuits | première tranche à `prix_ht: 0` |
| Redevances de l'agence de l'eau (préservation de la ressource, consommation, performance des réseaux, performance de l'assainissement) | composantes avec `redevance: true`, dans le service concerné |
| Part de la collectivité et part du délégataire | deux composantes (et deux abonnements) |
| TVA | `tva` du service : 5,5 pour l'eau potable, 10 pour l'assainissement, 0 s'il n'est pas assujetti |

Chaque période a un `debut`, une `source` (URL) et, sauf la dernière, une `fin`. Les périodes se suivent sans trou. Ajoute `date_source` (date de la délibération) et, si elle est publiée, la facture type (`factures_types: - {volume_m3: 120, total_ttc: …}`) : le test vérifie que le calcul redonne ce montant au centime près.

En haut du fichier, un commentaire indique ce qui est incertain : un rattachement de redevance, une règle de TVA, un tarif précédent non saisi…

## 3. Vérifier

```bash
uv run -p 3.13 --only-group test pytest tests/test_prereglages.py
```

Le test vérifie le schéma, les dates, la source et les factures types. Compare ensuite à **ta propre facture** : le volume facturé par tranche, le montant HT de chaque ligne et la TVA. Le capteur « Coût depuis la dernière relève » donne ce détail en attributs.

## 4. Mettre à jour chaque année

Les tarifs changent souvent au 1er janvier, parfois à une autre date (au 1er mai pour le Grand Belfort). Ajoute une nouvelle période, et mets une `fin` à la précédente. Tant que la nouvelle n'est pas saisie, l'intégration prolonge la dernière et le signale (attribut `tarif_prolonge`).
