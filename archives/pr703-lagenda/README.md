# Archive — PR #703 de L'agenda (2026-10-14)

Premier jet du suivi des prix Carrefour, écrit par erreur dans le dépôt `L-AGENDA-CONCERTS/lagenda`
(branche `claude/product-price-scraper-tjdo0l`, PR fermée le 2026-10-15). Copie telle quelle, pour mémoire.

**Remplacé par `functions/scraper_carrefour.py`.** Ce code avait été écrit sans jamais voir la vraie page
(Cloudflare bloquait la session cloud) : `extract.py` devine la structure. Relancé depuis le Mac le
2026-10-15, l'API réelle s'est révélée simple (JSON:API, voir CLAUDE.md) : le scraper a été réécrit
directement dessus, au format du projet Ardoise. Rien ici n'est importé ni déployé.
