#!/usr/bin/env python3
"""Relevé des prix — un passage sur chaque rayon de `rayons.py`.

    python prix/run.py                   relevé complet, écrit en base, affiche ce qui a bougé
    python prix/run.py --test            1re page seulement, affiche les produits, n'écrit rien
    python prix/run.py --visible         navigateur en fenêtre (cocher la case Cloudflare)
    python prix/run.py --simulation      données inventées, sans réseau (base à part)
    python prix/run.py --fixture F       extrait un fichier sauvegardé (data/prix/debug/…), n'écrit rien

`PRIX_MODE=simulation` équivaut à `--simulation`. Code de sortie 1 si un rayon
n'a pas pu être relevé (pour qu'un lancement planifié le voie).

Sorties :
    data/prix/prix.db                         la base (prix_simulation.db en simulation)
    data/prix/raw/<enseigne>_<rayon>_<jour>.json   le relevé brut du jour
    data/prix/debug/                          dernières réponses brutes (si 0 produit, c'est là qu'on regarde)
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prix import carrefour, db  # noqa: E402
from prix.rayons import RAYONS  # noqa: E402

PARIS = ZoneInfo("Europe/Paris")
DATA_DIR = carrefour.DATA_DIR
SIMULATION_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "carrefour_simulation.json"


def euros(value) -> str:
    return "—" if value is None else f"{value:.2f}".replace(".", ",") + " €"


def simulate(day: str):
    """Le jeu inventé, avec des prix qui bougent d'un jour à l'autre (déterministe)."""
    products = carrefour.load_fixture(str(SIMULATION_FIXTURE))
    for p in products:
        rnd = random.Random(f"{day}|{p['code']}")
        if rnd.random() < 0.3:
            factor = rnd.choice([0.9, 0.95, 1.05, 1.1])
            p["price"] = round(p["price"] * factor, 2)
            if p.get("unit_price"):
                p["unit_price"] = round(p["unit_price"] * factor, 2)
    if random.Random(f"{day}|absent").random() < 0.4:
        products.pop(random.Random(day).randrange(len(products)))
    return products, {"method": "simulation", "pages": 1}


def print_products(products):
    for p in sorted(products, key=lambda x: x["name"].lower()):
        unit = f"{euros(p['unit_price'])}/{p['unit']}" if p.get("unit_price") and p.get("unit") else ""
        promo = f"  🏷️  {p['promo_label']}" if p.get("promo_label") else ""
        barre = f" (au lieu de {euros(p['regular_price'])})" if p.get("regular_price") else ""
        print(f"   {p['name'][:40]:<40} {(p.get('format') or '')[:28]:<28} {euros(p['price']):>9}{barre} {unit}{promo}")


def print_diff(diff):
    if diff["prev_day"] is None:
        print("   1er relevé de ce rayon : rien à comparer pour l'instant.")
        return
    print(f"   Depuis le {diff['prev_day']} :")
    if diff["suspect"]:
        print("   ⚠️  Relevé SUSPECT : moins de la moitié des produits du précédent. Aucune disparition déclarée.")
    for p in diff["new"]:
        print(f"   🆕 {p['name']} — {euros(p['price'])}")
    for p in diff["back"]:
        print(f"   🔙 {p['name']} revient au rayon — {euros(p['price'])}")
    for p, old in diff["up"]:
        print(f"   📈 {p['name']} : {euros(old)} → {euros(p['price'])} ({(p['price'] / old - 1) * 100:+.0f} %)")
    for p, old in diff["down"]:
        print(f"   📉 {p['name']} : {euros(old)} → {euros(p['price'])} ({(p['price'] / old - 1) * 100:+.0f} %)")
    for p in diff["promo_started"]:
        print(f"   🏷️  {p['name']} passe en promo : {p['promo_label']}")
    for g in diff["gone"]:
        print(f"   👋 {g['name']} n'est plus au rayon (dernier prix {euros(g['price'])})")
    if not any(diff[k] for k in ("new", "back", "up", "down", "promo_started", "gone")):
        print("   Aucun changement.")


def save_raw(rayon, day, products, meta, scraped_at):
    raw_dir = DATA_DIR / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / f"{rayon['retailer']}_{rayon['slug']}_{day}.json"
    payload = {"retailer": rayon["retailer"], "rayon": rayon["slug"], "url": rayon["url"], "day": day,
               "scraped_at": scraped_at, **meta, "products": products}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Relevé des prix des rayons suivis.")
    parser.add_argument("--test", action="store_true", help="1re page seulement, n'écrit rien")
    parser.add_argument("--visible", action="store_true", help="navigateur en fenêtre visible")
    parser.add_argument("--simulation", action="store_true", help="données inventées, sans réseau")
    parser.add_argument("--fixture", help="extraire un fichier sauvegardé au lieu d'aller sur le site")
    parser.add_argument("--method", choices=("auto", "http", "browser"), default="auto")
    parser.add_argument("--rayon", help="slug d'un seul rayon (défaut : tous)")
    parser.add_argument("--day", help="jour du relevé YYYY-MM-DD (défaut : aujourd'hui, heure de Paris)")
    args = parser.parse_args()

    simulation = args.simulation or os.environ.get("PRIX_MODE") == "simulation"
    dry_run = args.test or bool(args.fixture)
    now = datetime.now(PARIS)
    day = args.day or now.strftime("%Y-%m-%d")
    rayons = [r for r in RAYONS if not args.rayon or r["slug"] == args.rayon]
    if not rayons:
        print(f"❌ Rayon inconnu : {args.rayon}. Connus : {', '.join(r['slug'] for r in RAYONS)}")
        return 2

    db_path = DATA_DIR / ("prix_simulation.db" if simulation else "prix.db")
    if simulation:
        print(f"🧪 Mode simulation : données inventées, base {db_path.relative_to(carrefour.ROOT)}")
    conn = None if dry_run else db.connect(db_path)
    failures = 0

    for rayon in rayons:
        started = datetime.now(PARIS).isoformat(timespec="seconds")
        print(f"\n🛒 {rayon['retailer'].capitalize()} — {rayon['label']}")
        try:
            if simulation:
                products, meta = simulate(day)
            elif args.fixture:
                products, meta = carrefour.load_fixture(args.fixture), {"method": "fichier", "pages": 1}
            else:
                products, meta = carrefour.scrape_rayon(rayon, mode="test" if args.test else "full",
                                                        method=args.method, visible=args.visible)
        except carrefour.Blocked as e:
            failures += 1
            print(f"   ❌ Bloqué par Cloudflare ({e}).")
            print("      Depuis ton Mac : python prix/run.py --visible, coche la case une fois ;")
            print("      le cookie est gardé dans data/prix/navigateur/ pour les relevés suivants.")
            if conn:
                db.log_run(conn, day=day, started_at=started, retailer=rayon["retailer"], rayon=rayon["slug"],
                           method=None, pages=0, status="blocked", n_products=0, message=str(e))
            continue
        except Exception as e:
            failures += 1
            print(f"   ❌ Échec : {type(e).__name__}: {e}")
            if conn:
                db.log_run(conn, day=day, started_at=started, retailer=rayon["retailer"], rayon=rayon["slug"],
                           method=None, pages=0, status="error", n_products=0, message=f"{type(e).__name__}: {e}")
            continue

        print(f"   {len(products)} produits ({meta['pages']} page(s), via {meta['method']})")
        if not products:
            failures += 1
            print("   ⚠️  Aucun produit lisible. Réponses brutes gardées dans data/prix/debug/ :")
            print("      c'est ce fichier qu'il faut regarder pour adapter prix/extract.py.")
            if conn:
                db.log_run(conn, day=day, started_at=started, retailer=rayon["retailer"], rayon=rayon["slug"],
                           method=meta["method"], pages=meta["pages"], status="empty", n_products=0)
            continue

        if dry_run:
            print_products(products)
            print("   (mode test : rien n'est écrit en base)")
            continue

        raw_path = save_raw(rayon, day, products, meta, started)
        diff = db.record_snapshot(conn, retailer=rayon["retailer"], rayon=rayon["slug"], products=products,
                                  day=day, recorded_at=started)
        db.log_run(conn, day=day, started_at=started, retailer=rayon["retailer"], rayon=rayon["slug"],
                   method=meta["method"], pages=meta["pages"], status="suspect" if diff["suspect"] else "ok",
                   n_products=len(products))
        print(f"   Relevé brut : {raw_path.relative_to(carrefour.ROOT)}")
        print_diff(diff)

    if conn:
        conn.close()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
