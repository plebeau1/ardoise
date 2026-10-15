"""Base SQLite des prix — `data/prix/prix.db`, calquée sur `concerts.db`.

    products        un produit par (enseigne, code) : l'équivalent de `events`.
                    Identité = `carrefour:<EAN>`. Comme le `VENUE_NAME` des
                    concerts, cette recette ne change JAMAIS : la changer coupe
                    l'historique de chaque produit en deux.
    price_history   un relevé par produit et par jour (Europe/Paris). Relancer
                    le même jour remplace la ligne du jour, rien d'autre : pas
                    de doublon, pas d'historique réécrit.
    scrape_runs     un passage par rayon et par relevé, réussi ou non. C'est ce
                    qui distingue « le produit a disparu » de « le relevé a
                    raté », et ce qui dira quand le scraper casse en silence.

Garde-fou repris du soft-delete des concerts : un relevé qui rend moins de la
moitié des produits du précédent est marqué `suspect` et ne déclare aucune
disparition — c'est le lecteur qui a raté, pas le rayon qui s'est vidé.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SUSPECT_RATIO = 0.5

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id          TEXT PRIMARY KEY,
    retailer    TEXT NOT NULL,
    code        TEXT NOT NULL,
    ean         TEXT,
    name        TEXT NOT NULL,
    brand       TEXT,
    format      TEXT,
    origin      TEXT,
    url         TEXT,
    image       TEXT,
    rayon       TEXT,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS price_history (
    product_id    TEXT NOT NULL REFERENCES products(id),
    day           TEXT NOT NULL,
    price         REAL NOT NULL,
    unit_price    REAL,
    unit          TEXT,
    regular_price REAL,
    promo_label   TEXT,
    available     INTEGER,
    recorded_at   TEXT NOT NULL,
    PRIMARY KEY (product_id, day)
);
CREATE INDEX IF NOT EXISTS idx_price_history_day ON price_history(day);
CREATE TABLE IF NOT EXISTS scrape_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    day         TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    retailer    TEXT NOT NULL,
    rayon       TEXT NOT NULL,
    method      TEXT,
    pages       INTEGER,
    status      TEXT NOT NULL,
    n_products  INTEGER NOT NULL DEFAULT 0,
    message     TEXT
);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def product_id(retailer: str, code: str) -> str:
    return f"{retailer}:{code}"


def log_run(conn, *, day, started_at, retailer, rayon, method, pages, status, n_products, message=None):
    conn.execute(
        "INSERT INTO scrape_runs (day, started_at, retailer, rayon, method, pages, status, n_products, message) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (day, started_at, retailer, rayon, method, pages, status, n_products, message),
    )
    conn.commit()


def _previous_run(conn, retailer, rayon, day):
    return conn.execute(
        "SELECT day, n_products FROM scrape_runs WHERE retailer = ? AND rayon = ? AND day < ? "
        "AND status IN ('ok', 'suspect') ORDER BY day DESC, id DESC LIMIT 1",
        (retailer, rayon, day),
    ).fetchone()


def record_snapshot(conn, *, retailer, rayon, products, day, recorded_at) -> dict:
    """Écrit le relevé du jour et rend ce qui a bougé depuis le précédent."""
    prev = _previous_run(conn, retailer, rayon, day)
    suspect = bool(prev and prev["n_products"] and len(products) < prev["n_products"] * SUSPECT_RATIO)
    diff = {"total": len(products), "prev_day": prev["day"] if prev else None, "suspect": suspect,
            "new": [], "back": [], "up": [], "down": [], "promo_started": [], "gone": []}

    seen_ids = set()
    for p in products:
        pid = product_id(retailer, p["code"])
        seen_ids.add(pid)
        before = conn.execute(
            "SELECT price, promo_label FROM price_history WHERE product_id = ? AND day < ? ORDER BY day DESC LIMIT 1",
            (pid, day),
        ).fetchone()
        known = conn.execute("SELECT first_seen FROM products WHERE id = ?", (pid,)).fetchone()
        conn.execute(
            """INSERT INTO products (id, retailer, code, ean, name, brand, format, origin, url, image, rayon,
                                     first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   name = excluded.name,
                   ean = COALESCE(excluded.ean, products.ean),
                   brand = COALESCE(excluded.brand, products.brand),
                   format = COALESCE(excluded.format, products.format),
                   origin = COALESCE(excluded.origin, products.origin),
                   url = COALESCE(excluded.url, products.url),
                   image = COALESCE(excluded.image, products.image),
                   rayon = excluded.rayon,
                   first_seen = MIN(products.first_seen, excluded.first_seen),
                   last_seen = MAX(products.last_seen, excluded.last_seen)""",
            (pid, retailer, p["code"], p.get("ean"), p["name"], p.get("brand"), p.get("format"), p.get("origin"),
             p.get("url"), p.get("image"), rayon, day, day),
        )
        conn.execute(
            """INSERT INTO price_history (product_id, day, price, unit_price, unit, regular_price, promo_label,
                                          available, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(product_id, day) DO UPDATE SET
                   price = excluded.price, unit_price = excluded.unit_price, unit = excluded.unit,
                   regular_price = excluded.regular_price, promo_label = excluded.promo_label,
                   available = excluded.available, recorded_at = excluded.recorded_at""",
            (pid, day, p["price"], p.get("unit_price"), p.get("unit"), p.get("regular_price"), p.get("promo_label"),
             None if p.get("available") is None else int(p["available"]), recorded_at),
        )
        if known is None or known["first_seen"] >= day:  # relancer le jour même le garde « nouveau »
            diff["new"].append(p)
        elif before is not None:
            if prev and not conn.execute("SELECT 1 FROM price_history WHERE product_id = ? AND day = ?",
                                         (pid, prev["day"])).fetchone():
                diff["back"].append(p)
            if p["price"] > before["price"]:
                diff["up"].append((p, before["price"]))
            elif p["price"] < before["price"]:
                diff["down"].append((p, before["price"]))
            if p.get("promo_label") and not before["promo_label"]:
                diff["promo_started"].append(p)

    if prev and not suspect:
        rows = conn.execute(
            """SELECT p.id, p.name, ph.price FROM products p
               JOIN price_history ph ON ph.product_id = p.id AND ph.day = ?
               WHERE p.retailer = ? AND p.rayon = ?""",
            (prev["day"], retailer, rayon),
        ).fetchall()
        diff["gone"] = [{"name": r["name"], "price": r["price"]} for r in rows if r["id"] not in seen_ids]

    conn.commit()
    return diff
