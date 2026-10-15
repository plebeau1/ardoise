# Ardoise — veille prix Mon Marché × Carrefour

Relève chaque matin les prix de mon-marche.fr (best-sellers, 12 rayons) et de carrefour.fr (13 rayons équivalents),
suit leur évolution et compare les deux enseignes sur les mêmes produits.

Page en ligne : https://ardoise-mon-marche.web.app

Tout tourne sur Firebase (projet `ardoise-mon-marche`) : deux Cloud Functions planifiées (7 h et 7 h 15) et une
qui sert la page. Les bases SQLite vivent dans le bucket `gs://ardoise-mon-marche-data`, pas dans ce dépôt.

```bash
firebase deploy --only functions,hosting        # déployer
python3 functions/scraper_carrefour.py --test   # tester le relevé Carrefour sans rien écrire
python3 functions/dashboard.py                  # régénérer la page en local (après avoir récupéré les bases)
```

Le détail (architecture, sources, pièges qui faussent les prix) est dans [CLAUDE.md](CLAUDE.md).
