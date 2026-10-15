#!/usr/bin/env python3
"""Consulter l'évolution des prix Mon Marché relevés par scraper_mon_marche.py.

Usage :
    python3 evolution.py                    # tableau de bord : prix actuel + variations, tous rayons
    python3 evolution.py --rayon fruits     # un seul rayon (recherche partielle sur le nom du rayon)
    python3 evolution.py pomme              # produits dont le nom contient "pomme" + historique jour par jour
    python3 evolution.py --csv              # exporte tout l'historique dans exports/historique_mon_marche.csv

Les variations sont calculées sur le prix HORS PROMO : une promo n'est pas une hausse/baisse de prix.
"""
import argparse
import csv
import sqlite3
import unicodedata
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
DB_PATH = ROOT_DIR / "data" / "mon_marche.db"
CSV_PATH = ROOT_DIR / "exports" / "historique_mon_marche.csv"


def normalize(text):
    """Minuscules sans accents, pour chercher "legumes" ou "épicerie" indifféremment."""
    return unicodedata.normalize("NFKD", (text or "").lower()).encode("ascii", "ignore").decode()


def pct(old, new):
    if not old or new is None or old == new:
        return ""
    return f"{(new / old - 1) * 100:+.1f} %".replace(".", ",")


def eur(value):
    return "" if value is None else f"{value:.2f}".replace(".", ",")


def load_history(conn, search=None, aisle=None):
    rows = conn.execute(
        """SELECT s.*, p.name, p.aisle AS aisle_name, p.origin, p.sale_unit, p.variety
           FROM price_snapshots s JOIN products p USING (product_id)
           ORDER BY p.aisle, s.product_id, s.snapshot_date"""
    ).fetchall()
    if search:
        rows = [r for r in rows if normalize(search) in normalize(r["name"])]
    if aisle:
        rows = [r for r in rows if normalize(aisle) in normalize(r["aisle_name"])]
    history = {}
    for r in rows:
        history.setdefault(r["product_id"], []).append(r)
    return history


def print_dashboard(history, dates):
    last_date = dates[-1]
    print(f"Relevés : {len(dates)} (du {dates[0]} au {last_date})\n")
    header = (f"{'Produit':<40} {'Unité':<20} {'Prix':>6} {'€/kg-l':>7} {'Promo':<11}"
              f" {'vs veille':>9} {'vs 1er':>8} {'€/kg min':>8} {'€/kg max':>8}")
    current_aisle = None
    for snaps in sorted(history.values(), key=lambda s: (s[-1]["aisle_name"], s[-1]["rank"] or 999)):
        now = snaps[-1]
        if now["snapshot_date"] != last_date:
            continue  # sorti des best-sellers : visible via la recherche par nom
        if now["aisle_name"] != current_aisle:
            current_aisle = now["aisle_name"]
            print(f"\n── {current_aisle} " + "─" * 40)
            print(header)
        prev = snaps[-2] if len(snaps) > 1 else None
        # Comparaison sur le prix au kg/l hors promo s'il existe (insensible aux changements de conditionnement)
        key = "regular_unit_price" if now["regular_unit_price"] is not None else "regular_price"
        values = [s[key] for s in snaps if s[key] is not None]
        print(f"{now['name'][:40]:<40} {(now['sale_unit'] or '')[:20]:<20} {eur(now['price']):>6}"
              f" {eur(now['unit_price']):>7} {(now['promo_label'] or ''):<11}"
              f" {pct(prev[key], now[key]) if prev else '':>9} {pct(snaps[0][key], now[key]):>8}"
              f" {eur(min(values)) if key == 'regular_unit_price' and values else '':>8}"
              f" {eur(max(values)) if key == 'regular_unit_price' and values else '':>8}")


def print_product_history(snaps):
    first = snaps[-1]
    details = " — ".join(filter(None, [first["origin"], first["variety"], first["sale_unit"]]))
    print(f"\n{first['name']}" + (f" ({details})" if details else ""))
    print(f"  {'Date':<11} {'Prix':>6} {'€/kg-l':>7} {'Normal':>7} {'€/kg norm.':>10} {'Promo':<11} {'Rang':>4} {'Stock':>5}")
    for s in snaps:
        print(f"  {s['snapshot_date']:<11} {eur(s['price']):>6} {eur(s['unit_price']):>7}"
              f" {eur(s['regular_price']):>7} {eur(s['regular_unit_price']):>10} {(s['promo_label'] or ''):<11}"
              f" {s['rank'] or '':>4} {s['available_quantity'] if s['available_quantity'] is not None else '':>5}")


def export_csv(history):
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    columns = ["snapshot_date", "aisle_name", "rank", "name", "origin", "variety", "sale_unit", "price",
               "unit_price", "unit_price_unit", "regular_price", "regular_unit_price", "promo_label",
               "promo_end", "available_quantity", "rating", "rating_count", "product_id"]
    headers = ["date", "rayon", "rang", "produit", "origine", "variété", "unité", "prix", "prix_kg_l", "unité_prix",
               "prix_normal", "prix_kg_l_normal", "promo", "fin_promo", "stock", "note", "nb_avis", "id"]
    # Point-virgule + BOM : s'ouvre directement dans Excel FR avec les accents et les décimales
    with CSV_PATH.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(headers)
        for snaps in history.values():
            for s in snaps:
                writer.writerow([str(s[c]).replace(".", ",") if isinstance(s[c], float) else s[c] for c in columns])
    print(f"✔ {sum(len(v) for v in history.values())} lignes exportées → {CSV_PATH.relative_to(ROOT_DIR)}")


def main():
    parser = argparse.ArgumentParser(description="Évolution des prix Mon Marché")
    parser.add_argument("search", nargs="?", help="filtre sur le nom du produit (ex. pomme)")
    parser.add_argument("--rayon", help="filtre sur le rayon (ex. fruits, legumes)")
    parser.add_argument("--csv", action="store_true", help="exporte l'historique en CSV")
    args = parser.parse_args()

    if not DB_PATH.exists():
        raise SystemExit("Aucune donnée : lance d'abord python3 scraper_mon_marche.py")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    history = load_history(conn, args.search, args.rayon)
    if not history:
        raise SystemExit("Aucun produit ne correspond.")

    if args.csv:
        export_csv(history)
    elif args.search and len(history) <= 10:
        for snaps in history.values():
            print_product_history(snaps)
    else:
        dates = [r[0] for r in conn.execute("SELECT DISTINCT snapshot_date FROM price_snapshots ORDER BY 1")]
        print_dashboard(history, dates)


if __name__ == "__main__":
    main()
