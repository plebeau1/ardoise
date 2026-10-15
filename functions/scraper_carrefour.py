#!/usr/bin/env python3
"""Relevé quotidien des prix Carrefour (carrefour.fr), rayon par rayon (liste RAYONS ci-dessous).

En local (base dans data/ à la racine du projet) :
    python3 functions/scraper_carrefour.py          # relevé du jour, écrit en base, affiche ce qui a bougé
    python3 functions/scraper_carrefour.py --test   # 1re page de chaque sous-rayon, affiche les produits, n'écrit rien

Source : la page publique d'un rayon, demandée en JSON (en-tête X-Requested-With) → JSON:API, 30 produits / page.
    GET /r/<chemin du rayon>?page=N     → data[] (produit + offre + promos), meta.totalPage
Cloudflare refuse l'empreinte TLS de Python, de curl et de Chrome récent (403 « vérifiez que vous êtes humain »)
mais laisse passer celle de Safari : d'où curl_cffi, seule dépendance hors stdlib.

Sorties (dans DATA_DIR) :
    carrefour.db                        → SQLite : products + price_snapshots (1 ligne / produit / jour)
    raw/carrefour_YYYY-MM-DD.json.gz    → réponses brutes du jour (pour retraiter plus tard)
"""
import argparse
import gzip
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from curl_cffi import requests as cffi

BASE_URL = "https://www.carrefour.fr"
# Rayons suivis : (slug, libellé, [(chemin sur carrefour.fr, nb de produits gardés)]). Un rayon peut regrouper
# plusieurs sous-rayons Carrefour ; on garde les N premiers de chacun, dans l'ordre du site (« Pertinence » : Carrefour
# n'a pas de tri « meilleures ventes »). None = tout le sous-rayon. Calqués sur les 12 rayons best-sellers Mon Marché.
# Suivre un rayon de plus = ajouter une ligne. Ne jamais renommer un slug déjà relevé : il relie un relevé au précédent.
# Un produit présent dans deux rayons reste dans le premier de la liste.
RAYONS = [
    ("legumes-de-saison", "Légumes de saison", [("/r/fruits-et-legumes/legumes/legumes-de-saison", None)]),
    ("fruits", "Fruits", [("/r/fruits-et-legumes/fruits", 60)]),
    ("legumes", "Légumes", [("/r/fruits-et-legumes/legumes", 60)]),
    ("boucherie-charcuterie", "Boucherie & Charcuterie", [
        ("/r/viandes-et-poissons/boucherie", 10), ("/r/viandes-et-poissons/volaille-et-rotisserie", 10),
        ("/r/charcuterie-traiteur/charcuterie", 10)]),
    ("poissonnerie", "Poissonnerie", [
        ("/r/viandes-et-poissons/poissonnerie", 20), ("/r/viandes-et-poissons/traiteur-mer", 10)]),
    ("fromagerie", "Fromagerie", [("/r/cremerie/fromage-deguster", 15), ("/r/cremerie/fromage-cuisiner", 15)]),
    ("cremerie", "Crèmerie", [
        ("/r/cremerie/yaourts-fromages-blancs", 12), ("/r/cremerie/beurres-cremes", 10), ("/r/cremerie/oeufs", 8)]),
    ("epicerie-salee", "Épicerie Salée", [
        ("/r/epicerie-salee/pates-riz-feculents", 10), ("/r/epicerie-salee/conserves-et-bocaux", 10),
        ("/r/epicerie-salee/huiles-vinaigres-et-vinaigrettes", 5), ("/r/epicerie-salee/sauces-chaudes", 5)]),
    ("epicerie-sucree", "Épicerie Sucrée", [
        ("/r/epicerie-sucree/biscuits", 8), ("/r/epicerie-sucree/petit-dejeuner", 8), ("/r/epicerie-sucree/chocolats", 7),
        ("/r/epicerie-sucree/sucres-farines-coulis-et-preparation-gateaux", 7)]),
    ("traiteur", "Traiteur", [
        ("/r/charcuterie-traiteur/pizzas-tartes-pates-fraiches", 10), ("/r/charcuterie-traiteur/plats-cuisines", 10),
        ("/r/charcuterie-traiteur/entrees-salades", 10)]),
    ("bebe", "Bébé", [("/r/bebe/alimentation", 30)]),
    ("entretien-hygiene", "Entretien & Hygiène", [
        ("/r/entretien-et-nettoyage/vaisselle", 6), ("/r/entretien-et-nettoyage/produits-entretien", 6),
        ("/r/entretien-et-nettoyage/sacs-conservation", 6), ("/r/hygiene-et-beaute/papiers", 6),
        ("/r/hygiene-et-beaute/hygiene-dentaire", 6)]),
    ("boissons", "Boissons", [
        ("/r/boissons/eaux", 8), ("/r/boissons/jus-fruits-legumes", 8), ("/r/cremerie/laits", 7),
        ("/r/epicerie-sucree/cafes", 7)]),
]
# Empreinte navigateur imitée. Cloudflare bloque chrome124 ; si un jour il bloque celle-ci : safari15_5, chrome110…
IMPERSONATE = "safari17_0"
HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",   # sans lui, le site renvoie la page HTML
    "Accept-Language": "fr-FR,fr;q=0.9",
}
REQUEST_DELAY = 1.5     # secondes entre deux pages, pour rester poli
PAGE_SIZE = 30          # produits par page sur carrefour.fr
MAX_PAGES = 40          # garde-fou par sous-rayon
SUSPECT_RATIO = 0.5     # un rayon qui rend moins de la moitié des produits du relevé précédent : c'est le relevé
                        # qui a raté, pas le rayon qui s'est vidé → aucune disparition annoncée
UNITS = {"KG": "kg", "L": "l", "U": "pièce"}
TZ = ZoneInfo("Europe/Paris")

# Données : data/ à la racine du projet en local, /tmp/ardoise dans le cloud (variable ARDOISE_DATA_DIR)
DATA_DIR = Path(os.environ.get("ARDOISE_DATA_DIR") or Path(__file__).resolve().parent.parent / "data")
DB_PATH = DATA_DIR / "carrefour.db"
RAW_DIR = DATA_DIR / "raw"

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    product_id          TEXT PRIMARY KEY,   -- EAN (stable)
    name                TEXT,
    brand               TEXT,
    slug                TEXT,
    url                 TEXT,
    aisle               TEXT,               -- rayon suivi (libellé de RAYONS)
    category_path       TEXT,               -- fil d'Ariane catalogue (Fruits et Légumes > Légumes > Courges)
    origin              TEXT,               -- FRANCE, ESPAGNE…
    sale_unit           TEXT,               -- libellé du site : "la pièce", "Le sachet de 1,5Kg"
    format              TEXT,               -- "1,5Kg", "500g", "la pièce"
    labels              TEXT,               -- JSON : ["organic", "qualityline"…]
    image_url           TEXT,
    first_seen          TEXT,
    last_seen           TEXT
);

CREATE TABLE IF NOT EXISTS price_snapshots (
    snapshot_date       TEXT NOT NULL,      -- YYYY-MM-DD (Europe/Paris)
    scraped_at          TEXT NOT NULL,
    product_id          TEXT NOT NULL,
    aisle               TEXT,               -- slug du rayon
    rank                INTEGER,            -- position dans le rayon (tri par défaut du site)
    price               REAL,               -- prix payé pour 1 article (€)
    unit_price          REAL,               -- prix au kg / l / pièce (€)
    unit_price_unit     TEXT,               -- kg / l / pièce
    regular_price       REAL,               -- prix hors promo (€)
    regular_unit_price  REAL,               -- prix au kg / l / pièce hors promo (€)
    promo_label         TEXT,               -- "2 achetés = 1,95€", "Le 2ème à -50%"… (hors fidélité)
    promo_type          TEXT,               -- code Carrefour (ri…)
    promo_end           TEXT,
    loyalty_label       TEXT,               -- "-10% Club" : versé sur la carte fidélité, ne baisse pas le prix payé
    available           INTEGER,            -- 1 = achetable en ligne
    rating              REAL,
    rating_count        INTEGER,
    store               TEXT,               -- magasin de référence des prix (offerServiceId)
    PRIMARY KEY (snapshot_date, product_id)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_product ON price_snapshots (product_id, snapshot_date);
"""


# --- HTTP -------------------------------------------------------------------

class Blocked(Exception):
    """Cloudflare a servi sa page « vérifiez que vous êtes humain » au lieu du rayon."""


_session = None


def fetch_page(path, page, retries=3):
    global _session
    if _session is None:
        _session = cffi.Session(impersonate=IMPERSONATE)
        _session.headers.update(HEADERS)
    url = BASE_URL + path + (f"?page={page}" if page > 1 else "")
    for attempt in range(1, retries + 1):
        try:
            resp = _session.get(url, timeout=30)
            if resp.status_code in (403, 429, 503) and "_cf_chl" in resp.text:
                raise Blocked(f"HTTP {resp.status_code} sur {url}")
            resp.raise_for_status()
            data = resp.json()
            time.sleep(REQUEST_DELAY)
            return data
        except Blocked:
            raise
        except Exception as exc:
            if attempt == retries:
                raise
            print(f"  ⚠ {url} : {exc} — nouvel essai dans {2 ** attempt}s")
            time.sleep(2 ** attempt)


# --- Parsing ----------------------------------------------------------------

def num(value):
    """Prix de l'API : nombre, ou parfois texte ("0,12") selon les produits."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    try:
        return float(str(value).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


def parse_product(item):
    """Prix + promo + infos de base d'un produit de la liste d'un rayon. None si pas d'offre (pas de prix)."""
    a = item.get("attributes") or {}
    offers = (a.get("offers") or {}).get(a.get("ean")) or {}
    # Seulement l'offre vendue par Carrefour, pas celles des vendeurs partenaires (marketplace)
    offer = next((o for o in offers.values() if o.get("subType") == "carrefour"), None)
    prices = ((offer or {}).get("attributes") or {}).get("price") or {}
    if num(prices.get("price")) is None:
        return None
    o = offer["attributes"]
    shown, shown_unit = num(prices["price"]), num(prices.get("perUnit"))
    paid = regular = shown

    promo = loyalty = None
    for p in o.get("promotions") or []:
        if p.get("isDisplayed") is False:
            continue
        if p.get("isLoyalty"):      # Club, Mardi PASS : cagnotte sur la carte, le prix affiché reste le prix normal
            loyalty = loyalty or p
            continue
        promo = promo or p
        args = p.get("messageArgs") or {}
        # Promo par lot (« 2 achetés = 1,95€ », « le 2ème à -50% ») : le prix affiché reste le prix normal.
        # Remise sur 1 seul article (prix barré) : price.price = prix normal, le prix payé est discountedPrice.
        if args.get("quantity") == 1:
            regular = max(regular, num(args.get("initialPrice")) or 0)
            paid = min(paid, num(args.get("discountedPrice")) or paid)

    def per_unit(price):   # Carrefour calcule perUnit sur le prix PAYÉ (remisé s'il y a un prix barré)
        return round(shown_unit * price / paid, 2) if shown_unit is not None and paid else None

    images = (a.get("images") or {}).get("paths") or []
    rating = a.get("customerReviews") or {}
    return {
        "product": {
            "product_id": a["ean"],
            "name": " ".join((a.get("title") or "").split()),
            "brand": a.get("brand"),
            "slug": a.get("slug"),
            "url": f"{BASE_URL}/p/{a.get('slug')}-{a['ean']}",
            "category_path": " > ".join(c["label"] for c in a.get("categories") or []) or None,
            "origin": a.get("origin"),
            "sale_unit": a.get("packaging"),
            "format": a.get("format"),
            "labels": json.dumps([f.removeprefix("flag.common.") for f in a.get("flags") or []]),
            "image_url": images[0].replace("p_FORMAT", "p_540x540") if images else None,
        },
        "snapshot": {
            "price": round(paid, 2),
            "unit_price": per_unit(paid),
            "unit_price_unit": UNITS.get(prices.get("unitOfMeasure"), prices.get("unitOfMeasure")),
            "regular_price": round(regular, 2),
            "regular_unit_price": per_unit(regular),
            "promo_label": promo.get("label") if promo else None,
            "promo_type": promo.get("type") if promo else None,
            "promo_end": promo["promoEndDate"][:10] if promo and promo.get("promoEndDate") else None,
            "loyalty_label": loyalty.get("label") if loyalty else None,
            "available": int(bool((o.get("availability") or {}).get("purchasable"))),
            "rating": rating.get("average"),
            "rating_count": rating.get("count"),
            "store": o.get("offerServiceId"),
        },
    }


# --- Base de données ---------------------------------------------------------

def open_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def upsert_product(conn, product, today):
    cols = list(product.keys())
    conn.execute(
        f"""INSERT INTO products ({", ".join(cols)}, first_seen, last_seen)
            VALUES ({", ".join("?" * len(cols))}, ?, ?)
            ON CONFLICT(product_id) DO UPDATE SET
            {", ".join(f"{c}=excluded.{c}" for c in cols if c != "product_id")},
            last_seen=excluded.last_seen""",
        [product[c] for c in cols] + [today, today],
    )


# --- Relevé ------------------------------------------------------------------

def eur(value):
    return "?" if value is None else f"{value:.2f}".replace(".", ",")


def print_product(p, s):
    promo = f"  🏷️ {s['promo_label']}" if s["promo_label"] else ""
    regular = f" (au lieu de {eur(s['regular_price'])})" if s["regular_price"] != s["price"] else ""
    print(f"  {p['name'][:42]:<42} {(p['sale_unit'] or '')[:22]:<22} {eur(s['price']):>6} €{regular}"
          f"  {eur(s['unit_price'])} €/{s['unit_price_unit']}{promo}")


def run(test=False):
    now = datetime.now(TZ)
    today, scraped_at = now.strftime("%Y-%m-%d"), now.isoformat(timespec="seconds")
    conn = None if test else open_db()
    if conn:
        previous_date = conn.execute(
            "SELECT MAX(snapshot_date) FROM price_snapshots WHERE snapshot_date < ?", (today,)
        ).fetchone()[0]
        known_ids = {r[0] for r in conn.execute("SELECT product_id FROM products")}
    print(f"Carrefour — relevé du {today} — {len(RAYONS)} rayon{'s' if len(RAYONS) > 1 else ''}"
          + (" (test : 1re page, rien n'est écrit)" if test else ""))

    raw = {"scraped_at": scraped_at, "aisles": {}}
    seen_ids, seen = [], set()
    for slug, label, sources in RAYONS:
        pages, items = [], []
        for path, keep in sources:
            first = fetch_page(path, 1)
            wanted = MAX_PAGES if keep is None else -(-keep // PAGE_SIZE)   # pages nécessaires pour N produits
            n_pages = 1 if test else min(first["meta"].get("totalPage") or 1, wanted, MAX_PAGES)
            path_pages = [first] + [fetch_page(path, n) for n in range(2, n_pages + 1)]
            pages += path_pages
            items += [i for page in path_pages for i in page.get("data") or [] if i.get("type") == "product"][:keep]
        raw["aisles"][slug] = pages
        count = 0
        for rank, item in enumerate(items, 1):
            parsed = parse_product(item)
            if parsed is None or parsed["product"]["product_id"] in seen:
                continue
            product, snap = parsed["product"], parsed["snapshot"]
            product["aisle"] = label
            seen_ids.append(product["product_id"])
            seen.add(product["product_id"])
            count += 1
            if test:
                print_product(product, snap)
                continue
            upsert_product(conn, product, today)
            conn.execute(
                f"""INSERT OR REPLACE INTO price_snapshots
                    (snapshot_date, scraped_at, product_id, aisle, rank, {", ".join(snap)})
                    VALUES (?, ?, ?, ?, ?, {", ".join("?" * len(snap))})""",
                [today, scraped_at, product["product_id"], slug, rank, *snap.values()],
            )
        print(f"  {label:<25} {count:>3} produits ({len(pages)} page{'s' if len(pages) > 1 else ''},"
              f" magasin {snap['store'] if count else '?'})")
    if test:
        return today
    conn.commit()

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    with gzip.open(RAW_DIR / f"carrefour_{today}.json.gz", "wt", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False)

    print_summary(conn, today, previous_date, seen_ids, known_ids)
    conn.close()

    try:  # régénère la page visuelle ; un souci d'affichage ne doit jamais faire échouer le relevé
        import dashboard
        dashboard.build()
    except BaseException as exc:   # SystemExit compris (base Mon Marché absente)
        print(f"  ⚠ tableau de bord non régénéré : {exc}")
    return today


def print_summary(conn, today, previous_date, seen_ids, known_ids):
    new_ids = set(seen_ids) - known_ids
    promos = conn.execute(
        "SELECT COUNT(*) FROM price_snapshots WHERE snapshot_date=? AND promo_label IS NOT NULL", (today,)
    ).fetchone()[0]
    print(f"\n✔ {len(seen_ids)} produits relevés, {promos} en promo, {len(new_ids)} nouveaux → {DB_PATH}")
    if not previous_date:
        print("Premier relevé : les évolutions apparaîtront dès le prochain passage.")
        return

    # Variations sur le prix HORS PROMO (une promo n'est pas une hausse) et au kg quand il existe
    changes = conn.execute(
        """SELECT p.name, o.regular_price AS old, n.regular_price AS new,
                  o.regular_unit_price AS old_u, n.regular_unit_price AS new_u, n.unit_price_unit AS u
           FROM price_snapshots n
           JOIN price_snapshots o ON o.product_id = n.product_id AND o.snapshot_date = ?
           JOIN products p ON p.product_id = n.product_id
           WHERE n.snapshot_date = ?
             AND (o.regular_price IS NOT n.regular_price OR o.regular_unit_price IS NOT n.regular_unit_price)
           ORDER BY p.aisle, p.name""",
        (previous_date, today),
    ).fetchall()
    print(f"\nVariations de prix hors promo depuis le {previous_date} : {len(changes)}")
    for c in changes:
        old, new = (c["old_u"], c["new_u"]) if c["old_u"] and c["new_u"] else (c["old"], c["new"])
        arrow = "📈" if new > old else "📉"
        pct = f"{(new / old - 1) * 100:+.1f} %".replace(".", ",") if old else ""
        unit = f"  ({eur(c['old_u'])} → {eur(c['new_u'])} €/{c['u']})" if c["u"] and c["u"] != "pièce" else ""
        print(f"  {arrow} {c['name']} : {eur(c['old'])} → {eur(c['new'])} € {pct}{unit}")

    started = conn.execute(
        """SELECT p.name, n.promo_label FROM price_snapshots n
           JOIN price_snapshots o ON o.product_id = n.product_id AND o.snapshot_date = ?
           JOIN products p ON p.product_id = n.product_id
           WHERE n.snapshot_date = ? AND n.promo_label IS NOT NULL AND o.promo_label IS NULL""",
        (previous_date, today),
    ).fetchall()
    for r in started:
        print(f"  🏷️ {r['name']} passe en promo : {r['promo_label']}")
    if new_ids:
        names = [r[0] for r in conn.execute(
            f"SELECT name FROM products WHERE product_id IN ({','.join('?' * len(new_ids))})", list(new_ids))]
        print(f"\n🆕 Nouveaux : {', '.join(names)}")
    back = conn.execute(
        """SELECT p.name FROM price_snapshots n JOIN products p USING (product_id)
           WHERE n.snapshot_date = ? AND p.first_seen < ? AND n.product_id NOT IN
                 (SELECT product_id FROM price_snapshots WHERE snapshot_date = ?)""",
        (today, previous_date, previous_date),
    ).fetchall()
    if back:
        print(f"🔙 De retour : {', '.join(r[0] for r in back)}")

    # Disparitions, rayon par rayon : un rayon qui fond de moitié = relevé raté, on n'annonce rien
    counts = lambda d: dict(conn.execute(
        "SELECT aisle, COUNT(*) FROM price_snapshots WHERE snapshot_date=? GROUP BY aisle", (d,)).fetchall())
    now_counts, prev_counts = counts(today), counts(previous_date)
    trusted = []
    for aisle, n in now_counts.items():
        if prev_counts.get(aisle) and n < prev_counts[aisle] * SUSPECT_RATIO:
            print(f"⚠ Rayon {aisle} suspect : {n} produits contre {prev_counts[aisle]} le {previous_date}."
                  " Aucune disparition annoncée.")
        else:
            trusted.append(aisle)
    gone = conn.execute(
        f"""SELECT p.name FROM price_snapshots o JOIN products p USING (product_id)
            WHERE o.snapshot_date = ? AND o.aisle IN ({",".join("?" * len(trusted))}) AND o.product_id NOT IN
                  (SELECT product_id FROM price_snapshots WHERE snapshot_date = ?)""",
        (previous_date, *trusted, today),
    ).fetchall()
    if gone:
        print(f"👋 Plus au rayon : {', '.join(r[0] for r in gone)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Relevé des prix Carrefour")
    parser.add_argument("--test", action="store_true", help="1re page de chaque rayon, affiche, n'écrit rien")
    args = parser.parse_args()
    try:
        run(test=args.test)
    except Blocked as exc:
        sys.exit(f"✘ Bloqué par Cloudflare ({exc}). Essayer une autre empreinte dans IMPERSONATE"
                 " (safari15_5, chrome110…).")
