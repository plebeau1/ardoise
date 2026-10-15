#!/usr/bin/env python3
"""Relevé quotidien des prix des best-sellers Mon Marché (mon-marche.fr).

En production : lancé chaque jour par la Cloud Function releve_quotidien (main.py).
En local (base dans data/ à la racine du projet) :
    python3 functions/scraper_mon_marche.py              # relevé du jour (tous les rayons best-sellers)
    python3 functions/scraper_mon_marche.py --details    # force le rafraîchissement des fiches produit

Source : API JSON publique du site (aucune dépendance, aucun navigateur).
    /api/category/best-sellers          → liste des 12 rayons
    /api/category/<rayon>               → produits + prix + promo
    /api/articleDetailBySlug/<slug>     → fiche complète (description, variété, calibre…)

Sorties (dans DATA_DIR) :
    mon_marche.db                       → SQLite : products + price_snapshots (1 ligne / produit / jour)
    raw/mon_marche_YYYY-MM-DD.json.gz   → réponses brutes du jour (pour retraiter plus tard)
"""
import argparse
import gzip
import html
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_URL = "https://www.mon-marche.fr"
ROOT_CATEGORY = "best-sellers"
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")
REQUEST_DELAY = 0.3          # secondes entre deux appels, pour rester poli
DETAILS_MAX_AGE_DAYS = 7     # les fiches produit changent peu : on les relit 1 fois / semaine
TZ = ZoneInfo("Europe/Paris")

# Données : data/ à la racine du projet en local, /tmp/ardoise dans le cloud (variable ARDOISE_DATA_DIR)
DATA_DIR = Path(os.environ.get("ARDOISE_DATA_DIR") or Path(__file__).resolve().parent.parent / "data")
DB_PATH = DATA_DIR / "mon_marche.db"
RAW_DIR = DATA_DIR / "raw"

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    product_id          TEXT PRIMARY KEY,   -- canonicalId Mon Marché (stable)
    sku                 TEXT,
    name                TEXT,
    slug                TEXT,
    url                 TEXT,
    aisle               TEXT,               -- rayon best-seller (Fruits, Légumes…)
    category_path       TEXT,               -- fil d'Ariane catalogue (Fruits > Bananes, kiwis et raisins)
    pim_category        TEXT,               -- catégorie interne (Banane vrac)
    origin              TEXT,
    variety             TEXT,
    caliber             TEXT,
    quality_grade       TEXT,               -- catégorie de qualité F&L (Extra, I, II)
    sale_mode           TEXT,               -- piece / pieceWeight / arbitraryQuantity
    sale_unit           TEXT,               -- libellé du site : "par 4 (440 g)", "barquette (125 g)"…
    pack_size           INTEGER,
    weight              REAL,               -- poids/volume de ce qu'on achète (lot entier si "par 4")
    weight_unit         TEXT,               -- kg / l
    labels              TEXT,               -- JSON : ["Label BIO AB", …]
    short_description   TEXT,
    description         TEXT,
    attributes          TEXT,               -- JSON : {"Variété": "HAYWARD", …}
    image_url           TEXT,
    first_seen          TEXT,
    last_seen           TEXT,
    details_fetched_at  TEXT
);

CREATE TABLE IF NOT EXISTS price_snapshots (
    snapshot_date       TEXT NOT NULL,      -- YYYY-MM-DD (Europe/Paris)
    scraped_at          TEXT NOT NULL,
    product_id          TEXT NOT NULL,
    aisle               TEXT,
    rank                INTEGER,            -- position dans le rayon best-seller
    price               REAL,               -- prix affiché (€) pour l'unité vendue (lot entier), promo incluse
    unit_price          REAL,               -- prix au kg / au litre affiché (€)
    unit_price_unit     TEXT,               -- kg / l
    regular_price       REAL,               -- prix hors promo (€)
    regular_unit_price  REAL,               -- prix au kg / l hors promo (€)
    promo_label         TEXT,               -- "-15 %", "2e à -50 %"…
    promo_mechanism     TEXT,               -- IMMEDIATE_DISCOUNT / BATCH_DISCOUNT
    promo_campaign      TEXT,
    promo_end           TEXT,
    available_quantity  INTEGER,
    rating              REAL,
    rating_count        INTEGER,
    freshness_days      INTEGER,            -- garantie fraîcheur (jours)
    PRIMARY KEY (snapshot_date, product_id)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_product ON price_snapshots (product_id, snapshot_date);
"""


# --- HTTP -------------------------------------------------------------------

def fetch_json(path, retries=3):
    req = urllib.request.Request(BASE_URL + path, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Accept-Language": "fr-FR,fr;q=0.9",
    })
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.load(resp)
            time.sleep(REQUEST_DELAY)
            return data
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            if attempt == retries:
                raise
            print(f"  ⚠ {path} : {exc} — nouvel essai dans {2 ** attempt}s")
            time.sleep(2 ** attempt)


# --- Parsing ----------------------------------------------------------------

def cents_to_eur(value):
    return None if value is None else round(value / 100, 2)


def html_to_text(raw):
    if not raw:
        return None
    text = re.sub(r"</p>|<br\s*/?>", "\n", raw)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"\n{2,}", "\n", text).strip() or None


def ms_to_date(ms):
    return datetime.fromtimestamp(ms / 1000, TZ).strftime("%Y-%m-%d") if ms else None


def promo_label(promo, price_cents):
    """Résumé lisible de la promo, ex. "-15 %", "2e à -50 %"."""
    cond = promo.get("conditions") or {}
    kind, value = cond.get("type"), cond.get("value")
    if promo.get("mechanism") == "BATCH_DISCOUNT":
        nth = cond.get("nthQuantity")
        if kind == "PERCENT" and value is not None:
            return f"{nth}e à -{value:g} %" if nth else f"-{value:g} % par lot"
        return f"lot : {kind} {value}"
    original = promo.get("itemOriginalPrice")
    if kind == "PERCENT" and value is not None:
        return f"-{value:g} %"
    if original and price_cents:
        return f"-{round((1 - price_cents / original) * 100)} %"
    return f"{kind} {value}"


def format_quantity(value, unit):
    """0.44 kg → "440 g", 1.2 kg → "1,2 kg", 0.33 l → "33 cl"."""
    if not value or not unit:
        return None
    if value < 1 and unit in ("kg", "l"):
        value, unit = value * (1000 if unit == "kg" else 100), "g" if unit == "kg" else "cl"
    return f"{round(value, 2):g} {unit}".replace(".", ",")


def parse_listing_item(item):
    """Extrait prix + promo + infos de base d'un produit de la liste d'un rayon."""
    prices = (item.get("pricing") or {}).get("sellPrices") or {}
    per_unit = prices.get("perWeightUnit") or {}
    definition = item.get("itemDefinition") or {}
    promo = item.get("promo") if (item.get("promo") or {}).get("active") else None
    # Produits vendus par lot ("par 4") : l'API donne le prix et le poids d'UNE pièce,
    # le site affiche ceux du lot → on multiplie pour coller à l'affichage.
    pack_size = item.get("packSize") or 1
    piece_weight = (definition.get("weight") or {}).get("value") or (item.get("weightPrice") or {}).get("weight")
    weight = round(piece_weight * pack_size, 3) if piece_weight else None
    weight_unit = per_unit.get("unit") or (definition.get("weight") or {}).get("unit")

    price_cents = item["itemPrice"] * pack_size if item.get("itemPrice") is not None else None
    unit_price_cents = per_unit.get("net")
    regular_cents, regular_unit_cents = price_cents, unit_price_cents
    # Remise immédiate : le prix affiché est déjà remisé, on reconstitue le prix normal.
    # Remise par lot (2e à -50 %) : le prix unitaire affiché reste le prix normal.
    if promo and promo.get("mechanism") == "IMMEDIATE_DISCOUNT" and promo.get("itemOriginalPrice"):
        regular_cents = promo["itemOriginalPrice"] * pack_size
        if unit_price_cents and price_cents:
            regular_unit_cents = unit_price_cents * regular_cents / price_cents

    # Libellé comme sur le site : "par 4 (440 g)", "barquette (125 g)", "350 g"
    unit_name = definition.get("terminologyOverride") or (definition.get("terminology") or {}).get("singular")
    quantity = format_quantity(weight, weight_unit)
    sale_unit = f"{unit_name} ({quantity})" if unit_name and quantity else unit_name or quantity
    images = item.get("images") or []
    return {
        "product": {
            "product_id": item.get("canonicalId") or item["id"].split("$")[0],
            "sku": item.get("sku"),
            "name": " ".join((item.get("name") or "").split()),
            "slug": item.get("slug"),
            "url": f"{BASE_URL}/produit/{item.get('slug')}",
            "pim_category": item.get("pimCategoryName"),
            "origin": item.get("origin"),
            "sale_mode": definition.get("type"),
            "sale_unit": sale_unit,
            "pack_size": pack_size,
            "weight": weight,
            "weight_unit": weight_unit,
            "labels": json.dumps([l["label"] for l in item.get("labels") or []], ensure_ascii=False),
            "short_description": item.get("shortDescription"),
            "image_url": images[0]["url"] if images else None,
        },
        "snapshot": {
            "rank": (item.get("indexes") or {}).get("global"),
            "price": cents_to_eur(price_cents),
            "unit_price": cents_to_eur(unit_price_cents),
            "unit_price_unit": per_unit.get("unit"),
            "regular_price": cents_to_eur(regular_cents),
            "regular_unit_price": cents_to_eur(regular_unit_cents),
            "promo_label": promo_label(promo, item.get("itemPrice")) if promo else None,  # prix d'une pièce, comme itemOriginalPrice
            "promo_mechanism": promo.get("mechanism") if promo else None,
            "promo_campaign": promo.get("campaignName") if promo else None,
            "promo_end": ms_to_date((promo.get("conditions") or {}).get("endDate")) if promo else None,
            "available_quantity": item.get("availableQuantity"),
            "rating": (item.get("rating") or {}).get("average"),
            "rating_count": (item.get("rating") or {}).get("nbRatings"),
            "freshness_days": item.get("freshnessWarranty"),
        },
    }


def parse_details(detail):
    """Champs enrichis depuis la fiche produit complète."""
    attributes = {}
    for attr in detail.get("attributes") or []:
        value = str(attr.get("value", "")).strip()
        unit = attr.get("valueUnit") or ""
        attributes[attr["label"]] = f"{value} {unit}".strip()
    return {
        "category_path": " > ".join(b["name"] for b in detail.get("breadcrumbs") or []) or None,
        "variety": attributes.get("Variété"),
        "caliber": attributes.get("Calibre"),
        "quality_grade": attributes.get("Catégorie de qualité F&L"),
        "description": html_to_text(detail.get("description")),
        "attributes": json.dumps(attributes, ensure_ascii=False),
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


def needs_details(conn, product_id, force):
    if force:
        return True
    row = conn.execute("SELECT details_fetched_at FROM products WHERE product_id=?", (product_id,)).fetchone()
    if not row or not row["details_fetched_at"]:
        return True
    age = datetime.now(TZ) - datetime.fromisoformat(row["details_fetched_at"])
    return age > timedelta(days=DETAILS_MAX_AGE_DAYS)


# --- Relevé ------------------------------------------------------------------

def run(force_details=False):
    now = datetime.now(TZ)
    today, scraped_at = now.strftime("%Y-%m-%d"), now.isoformat(timespec="seconds")
    conn = open_db()
    previous_date = conn.execute(
        "SELECT MAX(snapshot_date) FROM price_snapshots WHERE snapshot_date < ?", (today,)
    ).fetchone()[0]
    known_ids = {r[0] for r in conn.execute("SELECT product_id FROM products")}

    root = fetch_json(f"/api/category/{ROOT_CATEGORY}")
    aisles = root.get("subcategories") or []
    print(f"Mon Marché — relevé du {today} — {len(aisles)} rayons best-sellers")

    raw = {"scraped_at": scraped_at, "aisles": {}, "details": {}}
    seen_ids = []
    for aisle in aisles:
        data = fetch_json(f"/api/category/{aisle['slug']}")
        items = [i for i in data.get("items") or [] if i.get("type") == "PRODUCT"]
        raw["aisles"][aisle["slug"]] = data
        for item in items:
            parsed = parse_listing_item(item)
            product, snap = parsed["product"], parsed["snapshot"]
            product["aisle"] = aisle["title"]
            pid = product["product_id"]
            upsert_product(conn, product, today)

            if needs_details(conn, pid, force_details):
                try:
                    detail = fetch_json(f"/api/articleDetailBySlug/{product['slug']}")
                    raw["details"][pid] = detail
                    extra = parse_details(detail)
                    conn.execute(
                        f"UPDATE products SET {', '.join(f'{k}=?' for k in extra)}, details_fetched_at=? "
                        "WHERE product_id=?",
                        [*extra.values(), scraped_at, pid],
                    )
                except Exception as exc:  # une fiche en erreur ne bloque pas le relevé des prix
                    print(f"  ⚠ fiche {product['name']} : {exc}")

            conn.execute(
                f"""INSERT OR REPLACE INTO price_snapshots
                    (snapshot_date, scraped_at, product_id, aisle, {", ".join(snap)})
                    VALUES (?, ?, ?, ?, {", ".join("?" * len(snap))})""",
                [today, scraped_at, pid, aisle["slug"], *snap.values()],
            )
            seen_ids.append(pid)
        print(f"  {aisle['title']:<25} {len(items):>3} produits")
    conn.commit()

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    with gzip.open(RAW_DIR / f"mon_marche_{today}.json.gz", "wt", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False)

    print_summary(conn, today, previous_date, seen_ids, known_ids)
    conn.close()

    try:  # régénère la page visuelle ; un souci d'affichage ne doit jamais faire échouer le relevé
        import dashboard
        dashboard.build()
    except Exception as exc:
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

    changes = conn.execute(
        """SELECT p.name, p.aisle, o.regular_price AS old, n.regular_price AS new,
                  o.regular_unit_price AS old_u, n.regular_unit_price AS new_u, n.unit_price_unit AS u
           FROM price_snapshots n
           JOIN price_snapshots o ON o.product_id = n.product_id AND o.snapshot_date = ?
           JOIN products p ON p.product_id = n.product_id
           WHERE n.snapshot_date = ?
             AND (o.regular_price IS NOT n.regular_price OR o.regular_unit_price IS NOT n.regular_unit_price)
           ORDER BY p.aisle, p.name""",
        (previous_date, today),
    ).fetchall()
    gone = conn.execute(
        """SELECT p.name FROM price_snapshots o JOIN products p USING (product_id)
           WHERE o.snapshot_date = ? AND o.product_id NOT IN
                 (SELECT product_id FROM price_snapshots WHERE snapshot_date = ?)""",
        (previous_date, today),
    ).fetchall()

    print(f"\nVariations de prix hors promo depuis le {previous_date} : {len(changes)}")
    fr = lambda v: "?" if v is None else f"{v:.2f}".replace(".", ",")
    for c in changes:
        pct = f"{(c['new'] / c['old'] - 1) * 100:+.1f} %".replace(".", ",") if c["old"] and c["new"] else ""
        unit = f"  ({fr(c['old_u'])} → {fr(c['new_u'])} €/{c['u']})" if c["u"] else ""
        print(f"  [{c['aisle']}] {c['name']} : {fr(c['old'])} → {fr(c['new'])} € {pct}{unit}")
    if new_ids:
        names = [r[0] for r in conn.execute(
            f"SELECT name FROM products WHERE product_id IN ({','.join('?' * len(new_ids))})", list(new_ids))]
        print(f"\nNouveaux dans les best-sellers : {', '.join(names)}")
    if gone:
        print(f"Sortis des best-sellers : {', '.join(r[0] for r in gone)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Relevé des prix best-sellers Mon Marché")
    parser.add_argument("--details", action="store_true", help="force la relecture de toutes les fiches produit")
    args = parser.parse_args()
    try:
        run(force_details=args.details)
    except urllib.error.HTTPError as exc:
        sys.exit(f"✘ Erreur HTTP {exc.code} sur {exc.url} — le site a peut-être changé son API.")
