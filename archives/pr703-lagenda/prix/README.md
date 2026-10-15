# Suivi des prix (Carrefour)

Un pipeline indépendant du site concerts, construit sur le même principe :
scraper → relevé brut JSON → base SQLite avec historique → rapport de ce qui a bougé.

| Concerts | Prix |
|---|---|
| `scrapers/venues/scrape_<salle>.py` | `prix/carrefour.py` (`scrape_rayon`) |
| registre des salles | `prix/rayons.py` (un rayon = une ligne) |
| `data/raw/<salle>_events.json` | `data/prix/raw/<enseigne>_<rayon>_<jour>.json` |
| `data/concerts.db` : `events` + `price_history` | `data/prix/prix.db` : `products` + `price_history` (1 ligne par produit et par jour) |
| identité MD5 `salle\|nom\|date` | identité `carrefour:<EAN>` |
| `scraping_report` / circuit breaker | table `scrape_runs` (ok / suspect / empty / blocked / error) |
| garde-fou soft-delete 25 % | relevé « suspect » sous 50 % du précédent : aucune disparition déclarée |

Rien ici n'est lu par `build.py`, la CI ou le déploiement. Tout `data/prix/` reste en local (gitignore).

## Lancer

```bash
python prix/run.py --simulation   # données inventées, sans réseau (base à part : prix_simulation.db)
python prix/run.py --test         # vrai site, 1re page, affiche les produits sans rien écrire
python prix/run.py                # relevé complet, écrit en base, affiche hausses / baisses / nouveautés
python prix/run.py --visible      # navigateur en fenêtre, pour cocher la case Cloudflare
```

Dépendances déjà dans `requirements.txt` (curl_cffi, playwright, beautifulsoup4).
Le navigateur utilisé est le Chrome du Mac. Sans Chrome : `python -m playwright install chromium`.

## Cloudflare

Carrefour est derrière Cloudflare. Depuis une IP de datacenter (CI GitHub, sessions cloud),
il sert une case « Vérifiez que vous êtes humain » : relevé impossible (constaté le 2026-10-14).
Depuis une connexion domestique, la page passe, au pire après avoir coché la case une fois.
Le scraper garde ses cookies dans `data/prix/navigateur/` : on coche une fois, les relevés suivants
passent seuls tant que le cookie vit. Si la case revient en headless et qu'on est devant l'écran,
une fenêtre s'ouvre d'elle-même. La case n'est jamais cochée automatiquement.

## Si un relevé rend 0 produit

La page n'a jamais pu être observée au moment d'écrire `extract.py`. Il lit donc toutes les formes
plausibles : JSON d'API, JSON embarqué (Nuxt, `window.__X__`, JSON-LD) et, en dernier recours,
les cartes produit du HTML. Si rien ne sort, les réponses brutes sont dans `data/prix/debug/`.
On adapte l'extraction sur ce fichier, sans retourner sur le site :

```bash
python prix/run.py --fixture data/prix/debug/carrefour_legumes-de-saison_p0.html
```
