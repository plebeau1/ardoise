# CLAUDE.md — Ardoise, veille prix Mon Marché (+ Carrefour)

## Objectif
Relever chaque jour les prix des best-sellers de mon-marche.fr (12 rayons, ~233 produits) et suivre leur évolution : prix affiché, prix au kg/l, promos, rang, stock, fiche produit. Page en ligne : https://ardoise-mon-marche.web.app
Depuis le 2026-10-15, même relevé quotidien sur carrefour.fr : 13 rayons calqués sur les 12 best-sellers Mon Marché (+ Légumes
de saison), ~420 produits (liste `RAYONS` de `scraper_carrefour.py`, ~30 requêtes, ~1 min),
dans une base à part. La page affiche les deux enseignes (filtre « Enseigne ») et les compare sur les mêmes produits.

## Architecture (100 % Firebase ; dépôt GitHub PUBLIC `plebeau1/ardoise`, sans CI)
- Le dépôt contient tout sauf `functions/venv/` (à recréer, voir `.gitignore`). `data/` y est une COPIE datée du bucket
  (`gcloud storage rsync -r gs://ardoise-mon-marche-data data/` puis commit pour la rafraîchir) : la vérité reste le
  bucket, ne jamais y renvoyer la copie du dépôt (piège 7). Pas d'action GitHub, déploiement à la main avec
  `firebase deploy`. Commits : `feat(module): …` en anglais.
- Projet Firebase `ardoise-mon-marche` (forfait Blaze, facturation « l'agenda »), région `europe-west1`.
- **Cloud Function `releve_quotidien`** (`functions/main.py`) : Cloud Scheduler, tous les jours à 7 h Europe/Paris.
  Télécharge la base du bucket → lance le scraper → régénère la page → renvoie base + page + archive brute dans le bucket.
  Rien n'est renvoyé si le relevé plante : la base du bucket reste intacte (2 nouvelles tentatives automatiques).
- **Cloud Function `releve_carrefour`** : même principe à 7 h 15, base `carrefour.db`. Indépendante : un échec Carrefour
  ne touche jamais Mon Marché.
- Les deux relevés régénèrent la même page : chacun télécharge aussi la base de l'AUTRE enseigne, en lecture seule
  (jamais renvoyée), puis publie `dashboard.html`.
- **Cloud Function `page`** : sert `dashboard.html` depuis le bucket. Firebase Hosting redirige tout le site vers elle
  (`firebase.json`), CDN 10 min. `noindex` partout + `public/robots.txt`.
- **Bucket `gs://ardoise-mon-marche-data`** = source de vérité : `mon_marche.db`, `carrefour.db`, `dashboard.html`, `raw/*.json.gz`.

## Fichiers
- `functions/scraper_mon_marche.py` : le relevé (API JSON → SQLite).
- `functions/scraper_carrefour.py` : le relevé Carrefour (JSON:API → SQLite), même structure et mêmes noms de colonnes.
- `functions/dashboard.py` + `functions/dashboard_template.html` : génération de la page (données embarquées).
  `SOURCES` = enseignes affichées (ids Carrefour préfixés `cf-`, Mon Marché sans préfixe). `FAMILIES` = rapprochement
  des mêmes produits entre enseignes (motif sur le nom, fruits et légumes frais seulement, bio à part) : on compare le
  prix le plus bas de chaque enseigne, hors promo, au kilo (à la pièce si `piece=True` et pas de kilo commun).
  `STAPLES` = produits courants hors fruits et légumes (beurre, lait, jambon, sacs poubelle…), comparés au kilo/litre ou
  par unité comptée dans le libellé (« 10 sacs » → €/sac). C'est un prix d'entrée, toutes marques : Elmex contre dentifrice
  Carrefour, d'où des écarts forts. Comparer un produit de plus = ajouter une ligne dans `FAMILIES` ou `STAPLES`.
  Ne jamais éditer un `dashboard.html` généré : modifier le template. DA « ardoise » (sombre) : couleurs en variables CSS
  dans `:root` — citron = accent, mandarine = promo, framboise = hausse, menthe = baisse (toujours avec ▲/▼).
  Enseignes : `--mm` craie, `--cf` bleu (validées daltonisme ; une couleur chaude se confondait avec la mandarine promo),
  toujours accompagnées du nom. Vue par défaut « Liste » compacte sans images ; « Étal » = cartes avec photos.
  Pas d'images Carrefour (bloquées en partie hors de carrefour.fr).
  Graphiques en SVG faits main, sans librairie. Liens directs : `#produit=<id>`, `#comparer=<Famille>` (ex. `Carotte`).
- `functions/main.py` : les 3 Cloud Functions. `functions/venv/` : environnement local requis par `firebase deploy`.
- `evolution.py` (racine) : consultation en terminal + export CSV, lit `data/mon_marche.db` (copie locale).
- Dossier de données : variable `ARDOISE_DATA_DIR` (`/tmp/ardoise` dans le cloud), sinon `data/` à la racine en local.
- `archives/pr703-lagenda/` : 1er jet Carrefour écrit à l'aveugle (ex-PR #703 de L'agenda, fermée). Pour mémoire, jamais importé.
- Python 3.12, stdlib pour le scraper Mon Marché ; `curl_cffi` pour Carrefour ; `firebase_functions` + `google-cloud-storage` pour le cloud.
  Timezone Europe/Paris, contenu en français, code en anglais.

## Source : API JSON publique (pas de navigateur nécessaire)
- `GET /api/category/best-sellers` → `subcategories[]` (slug, title) = les rayons
- `GET /api/category/<slug>` → `items[]` : prix, promo, poids, origine, labels, note, stock, rang (`indexes.global`)
- `GET /api/articleDetailBySlug/<slug>` → fiche : description HTML, `attributes[]` (variété, calibre, catégorie F&L, ingrédients…), `breadcrumbs`
- URL publique d'un produit : `https://www.mon-marche.fr/produit/<slug>`
- Les prix de l'API sont en **centimes**, parfois décimaux (424.15).

## Source Carrefour : la page rayon demandée en JSON
- Un rayon Ardoise = un ou plusieurs sous-rayons Carrefour, on garde les N premiers de chacun dans l'ordre du site
  (« Pertinence » : Carrefour n'a pas de tri « meilleures ventes », c'est surtout de la mise en avant). Un produit vu dans
  deux rayons reste dans le premier de la liste. Seules les offres vendues par Carrefour sont gardées (pas la marketplace).
- `GET https://www.carrefour.fr/r/<chemin du rayon>?page=N` avec `X-Requested-With: XMLHttpRequest` → JSON:API :
  `data[]` (30 produits / page), `meta.totalPage`. Sans cet en-tête : la page HTML.
- Produit : `attributes` (ean, title, brand, packaging, format, origin, categories, flags, customerReviews) ;
  prix dans `attributes.offers[<ean>][<offerServiceId>].attributes` : `price.price`, `price.perUnit` + `unitOfMeasure` (KG / U),
  `promotions[]`, `availability.purchasable`. URL publique : `https://www.carrefour.fr/p/<slug>-<ean>`. Prix en **euros**.
- **Cloudflare** bloque l'empreinte TLS de Python/curl/Chrome (403 « vérifiez que vous êtes humain ») mais laisse passer
  Safari : `curl_cffi` avec `impersonate="safari17_0"`. Testé OK depuis le Mac ET depuis Google Cloud (2026-10-15).
  `chrome110` passe du Mac mais pas du cloud. Si bloqué un jour : essayer `safari15_5` (constante `IMPERSONATE`).

## Pièges (les violer fausse les prix)
1. **`packSize` > 1 ("par 4")** : `itemPrice` et le poids sont ceux d'UNE pièce. Le site affiche le lot → multiplier par `packSize`. `perWeightUnit.net` (prix au kg) n'est pas à multiplier.
2. **Promo `IMMEDIATE_DISCOUNT`** : `itemPrice` est déjà remisé, le prix normal est `promo.itemOriginalPrice` (par pièce lui aussi).
   **Promo `BATCH_DISCOUNT`** (2e à -50 %) : `itemPrice` reste le prix normal.
3. **Les évolutions se calculent sur `regular_unit_price`** (hors promo, au kg/l) : une promo n'est pas une variation de prix, et le kg résiste aux changements de conditionnement.
4. `product_id` = `canonicalId` (stable). Ne pas utiliser `id` (suffixe `$hash` variable).
5. Un relevé = 1 ligne par produit et par jour (`PRIMARY KEY (snapshot_date, product_id)`) : relancer le même jour écrase le relevé du jour.
6. Les fiches détaillées ne sont relues que si absentes ou vieilles de plus de 7 jours (`--details` pour forcer).
7. **Ne pas lancer le scraper en local puis renvoyer la base locale dans le bucket** sans avoir d'abord récupéré celle du bucket : on écraserait l'historique cloud.

Carrefour :
8. **Promos fidélité (`isLoyalty`, « -10% Club », « Mardi PASS »)** : cagnotte sur la carte, le prix payé ne baisse pas →
   colonne `loyalty_label`, jamais `promo_label`. Quasi tous les fruits et légumes l'ont en permanence.
9. **Promos par lot (`messageArgs.quantity` ≥ 2 : « 2 achetés = 1,95€ », « Le 2ème à -50% »)** : `price.price` reste le prix normal.
   Remise sur 1 article (prix barré, `quantity` = 1) : `price.price` = prix normal, prix payé = `discountedPrice`, et
   **`perUnit` est calculé sur le prix PAYÉ** (jus 1 L : 5,89 € barré → 4,12 €/L). Le prix au kilo normal se recalcule.
   Certains `perUnit` arrivent en texte ("0,12") : toujours passer par `num()`.
10. `product_id` = EAN. Les prix sont ceux du **magasin de référence** des visiteurs anonymes (`store` = `0261-150-6`) :
    si ce code change d'un jour à l'autre, une « variation » peut n'être qu'un changement de magasin.
11. Un rayon qui rend moins de la moitié des produits du relevé précédent est « suspect » : aucune disparition annoncée.

## Commandes
```bash
firebase deploy --only functions,hosting          # déployer (depuis la racine)
gcloud scheduler jobs run firebase-schedule-releve_quotidien-europe-west1 --location=europe-west1 --project=ardoise-mon-marche   # relevé immédiat
gcloud storage cp gs://ardoise-mon-marche-data/mon_marche.db data/   # récupérer la base cloud en local
python3 evolution.py --rayon fruits               # puis consulter en terminal / --csv pour Excel
python3 functions/scraper_carrefour.py --test     # Carrefour : 1re page de chaque rayon, n'écrit rien
gcloud scheduler jobs run firebase-schedule-releve_carrefour-europe-west1 --location=europe-west1 --project=ardoise-mon-marche   # relevé Carrefour immédiat
gcloud storage cp gs://ardoise-mon-marche-data/carrefour.db data/    # récupérer la base Carrefour cloud
gcloud logging read 'resource.labels.service_name="releve-quotidien"' --project=ardoise-mon-marche --freshness=1d --limit=30 --format='value(textPayload)'   # journaux du relevé
```
