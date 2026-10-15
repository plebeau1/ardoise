#!/usr/bin/env python3
"""Génère dashboard.html (tableau de bord visuel) à partir des bases de chaque enseigne (SOURCES).

En production : régénérée après chaque relevé par les Cloud Functions, servie par la fonction « page ».
En local :
    python3 functions/dashboard.py             # génère data/dashboard.html puis l'ouvre
    python3 functions/dashboard.py --no-open   # génère seulement

La page est autonome : les données sont embarquées dedans.
Mise en page et graphiques : dashboard_template.html.
"""
import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import unicodedata
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

CODE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("ARDOISE_DATA_DIR") or CODE_DIR.parent / "data")
TEMPLATE_PATH = CODE_DIR / "dashboard_template.html"
OUTPUT_PATH = DATA_DIR / "dashboard.html"
TZ = ZoneInfo("Europe/Paris")

# Enseignes affichées : (id, nom, base). Mon Marché garde ses identifiants tels quels (liens #produit= existants),
# les autres sont préfixés par leur id pour ne jamais se mélanger.
SOURCES = [
    ("mm", "Mon Marché", "mon_marche.db"),
    ("cf", "Carrefour", "carrefour.db"),
]
DB_PATH = DATA_DIR / SOURCES[0][2]

# Comparaison entre enseignes. Un produit entre dans une famille si son nom (minuscules, sans accents) correspond au
# motif ; bio et non bio sont comparés séparément. On compare le prix le plus bas de chaque enseigne, au kilo.
# piece=True : à défaut de kilo des deux côtés, comparable à la pièce (seulement si les pièces se valent en taille).
# Ajouter une famille = ajouter une ligne.
FAMILIES = [
    # Légumes
    ("Carotte", r"\bcarottes?\b", False),
    ("Poireau", r"\bpoireaux?\b", False),
    ("Patate douce", r"\bpatates? douces?\b", False),
    ("Pomme de terre", r"\bpommes? de terre\b", False),
    ("Haricot vert", r"\bharicots? verts?\b", False),
    ("Endive", r"\bendives?\b", False),
    ("Champignon de Paris", r"\bchampignons?\b.*\b(blancs?|paris)\b", False),
    ("Brocoli", r"\bbrocolis?\b", True),
    ("Chou-fleur", r"\bchoux? ?-?fleurs?\b", True),
    ("Chou de Bruxelles", r"\bchoux? de bruxelles\b", False),
    ("Courgette", r"\bcourgettes?\b", False),
    ("Aubergine", r"\baubergines?\b", False),
    ("Poivron", r"\bpoivrons?\b", False),
    ("Tomate cerise", r"\btomates? cerises?\b", False),
    ("Tomate", r"\btomates?\b(?!.*cerise)", False),
    ("Oignon", r"\boignons?\b", False),
    ("Ail", r"\bail\b", False),
    ("Fenouil", r"\bfenouils?\b", False),
    ("Salade sucrine", r"^(?!.*courge).*\bsucrines?\b", True),
    ("Courge butternut", r"\bbutternut\b", False),
    ("Potimarron", r"\bpotimarrons?\b", False),
    ("Céleri branche", r"\bceleri branche\b", False),
    ("Céleri-rave", r"\bceleri ?-?rave\b", True),
    ("Navet", r"\bnavets?\b", False),
    ("Blette", r"\bblettes?\b", False),
    ("Épinard", r"\bepinards?\b", False),
    ("Concombre", r"\bconcombres?\b", True),
    ("Avocat", r"\bavocats?\b", True),
    # Fruits
    ("Banane", r"\bbananes?\b", False),
    ("Pomme", r"\bpommes?\b(?! de terre)", False),
    ("Poire", r"\bpoires?\b", False),
    ("Citron vert", r"\bcitrons? verts?\b|\blimes?\b", True),
    ("Citron jaune", r"\bcitrons?\b", True),
    ("Orange", r"\boranges?\b", False),
    ("Mandarine", r"\bmandarines?\b", False),
    ("Clémentine", r"\bclementines?\b", False),
    ("Kiwi", r"\bkiwis?\b", True),
    ("Raisin", r"\braisins?\b", False),
    ("Framboise", r"\bframboises?\b", False),
    ("Myrtille", r"\bmyrtilles?\b", False),
    ("Prune", r"\bprunes?\b", False),
    ("Kaki", r"\bkakis?\b", True),
    ("Mangue", r"\bmangues?\b", True),
    ("Grenade", r"\bgrenades?\b", True),
    ("Fruit de la passion", r"\bfruits? de la passion\b", True),
    ("Pomelo", r"\bpomelos?\b|\bpamplemousses?\b", True),
]
# Produits courants hors fruits et légumes : (nom, motif, unité de comptage). Comparés au kilo ou au litre ; avec une
# unité de comptage (sac, rouleau…), au prix par unité, comptée dans le libellé (« 10 sacs »), si le kilo manque.
# C'est un prix d'entrée : le moins cher de chaque enseigne, quelles que soient les marques.
STAPLES = [
    ("Beurre doux", r"\bbeurre\b.*\bdoux\b", None),
    ("Beurre demi-sel", r"\bbeurre\b.*\bdemi.?sel\b", None),
    ("Lait demi-écrémé", r"^(?!.*(bebe|croissance|infantile)).*\blait\b.*\bdemi.?ecreme\b", None),
    ("Lait entier", r"^(?!.*(bebe|croissance|infantile|yaourt|coco)).*\blait\b.*\bentier\b", None),
    ("Crème fraîche épaisse", r"\bcreme fraiche epaisse\b", None),
    ("Yaourt nature", r"^(?!.*(grec|brasse|chevre|sucre|skyr)).*\byaourts?\b.*\bnatures?\b", None),
    ("Skyr nature", r"\bskyr\b.*\bnature\b", None),
    ("Œufs", r"\boeufs?\b(?!.*(truite|saumon|lump|poisson))", "œuf"),
    ("Emmental râpé", r"\bemmental rape\b", None),
    ("Comté", r"^(?!.*rape).*\bcomte\b", None),
    ("Mozzarella", r"^(?!.*(rapee|bufala|stracciatella)).*\bmozzarella\b", None),
    ("Feta", r"\bfeta\b", None),
    ("Jambon blanc", r"^(?!.*(chiffonnade|herbes|\bcru\b|serrano|fume|jambonneau|pizza|coquillettes)).*\bjambon\b.*\b(cuit|superieur|paris)\b", None),
    ("Lardons fumés", r"\blardons?\b.*\bfumes?\b", None),
    ("Lardons nature", r"\blardons?\b.*\bnatures?\b", None),
    ("Filet de poulet", r"\b(filets?|blancs?|aiguillettes?) de poulet\b", None),
    ("Viande hachée", r"\bsteaks? haches?\b|\bviande hachee\b", None),
    ("Pavé de saumon", r"\bpaves? de saumon\b", None),
    ("Saumon fumé", r"\bsaumon fume\b", None),
    ("Crevettes cuites", r"^(?!.*decortiq).*\bcrevettes? cuites?\b", None),
    ("Bar", r"\bbar\b.*\bvide\b", None),
    ("Carottes râpées", r"\bcarottes? rapees?\b", None),
    ("Double concentré de tomates", r"\bdouble concentre", None),
    ("Spaghetti", r"\bspaghett?i\b", None),
    ("Penne", r"\bpenne\b", None),
    ("Riz basmati", r"\briz basmati\b", None),
    ("Huile d'olive", r"^(l.)?huile d.?olive\b", None),   # pas le thon « à l'huile d'olive »
    ("Sucre en poudre", r"^(?!.*(vanill|confiture|edulcor)).*\bsucre (blanc|en poudre|semoule)\b", None),
    ("Farine de blé", r"\bfarine de ble\b", None),
    ("Café moulu", r"\bcafe moulu\b", None),
    ("Jus d'orange", r"^(?!.*(nectar|multi)).*\bjus d.?orange\b", None),
    ("Jus de pomme", r"\bjus de pommes?\b", None),
    ("Eau minérale plate", r"^(?!.*gazeu).*\beaux?\b.*\b(minerale|de source)\b", None),
    ("Dentifrice", r"\bdentifrice\b", None),
    ("Sacs poubelle 30 L", r"\bsacs? poubelle\b.*\b30 ?l\b", "sac"),
    ("Sacs poubelle 50 L", r"\bsacs? poubelle\b.*\b50 ?l\b", "sac"),
    ("Papier toilette", r"\bpapier toilette\b", "rouleau"),
    ("Essuie-tout", r"\bessuie.?tout\b", "rouleau"),
    ("Tablettes lave-vaisselle", r"\b(tablettes?|capsules?)\b.*\blave.?vaisselle\b", "lavage"),
]
# Nombre d'unités dans un libellé (« 10 sacs », « le paquet de 50 tablettes », « la boîte de 12 œufs »)
COUNT_RE = re.compile(r"(\d+)\s*(sacs?|rouleaux|tablettes|capsules|lavages|dosettes|oeufs)\b")

# Produits transformés : jamais comparés au produit frais
NOT_FRESH = re.compile(r"\b(soupe|puree|jus|compote|surgel\w*|rapee?s?|chips|sauce|confiture|veloute|gaspacho|"
                       r"smoothie|cuite?s?|arilles|seche?s?|sirop)\b")

FRESH_AISLES = ("best-sellers-fruits", "best-sellers-legumes")   # rayons Mon Marché, si le fil d'Ariane manque

CF_LABELS = {"organic": "Bio", "qualityline": "Filière Qualité Carrefour", "fruitslegumesfr": "Fruits et Légumes de France"}
CF_COUNTRIES = {"EGYPTE": "Égypte", "PEROU": "Pérou", "EQUATEUR": "Équateur", "ETATS-UNIS": "États-Unis",
                "AFRIQUE DU SUD": "Afrique du Sud", "NOUVELLE-ZELANDE": "Nouvelle-Zélande", "BRESIL": "Brésil",
                "ISRAEL": "Israël", "SENEGAL": "Sénégal", "GRECE": "Grèce", "COTE D'IVOIRE": "Côte d'Ivoire",
                "REUNION": "La Réunion", "VIET NAM": "Viêt Nam", "MEXIQUE": "Mexique", "PAYS-BAS": "Pays-Bas"}


def normalize(text):
    text = (text or "").lower().replace("œ", "oe").replace("æ", "ae")
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def count_of(*texts):
    for text in texts:
        found = COUNT_RE.search(normalize(text))
        if found:
            return int(found.group(1))
    return None


def country_of(origin):
    """'élaborée en Pologne' → ('Pologne', True) ; 'France' → ('France', False)."""
    if not origin:
        return None, False
    match = re.match(r"élabor\w*\s+(?:en|aux|au|à)\s+(.+)", origin.strip(), re.I)
    country = (match.group(1) if match else origin).strip()
    return country[:1].upper() + country[1:], bool(match)


def cf_country(origin):
    """Carrefour écrit l'origine en capitales ('EGYPTE', 'ORIGINE C.E.E.') : on la remet au format Mon Marché."""
    if not origin:
        return None
    o = origin.strip()
    if re.search(r"pays tiers", o, re.I):
        return "Hors UE"
    if re.search(r"C\.E\.E|\bU\.?E\b", o, re.I):
        return "UE et hors UE" if re.search(r"hors", o, re.I) else "Union européenne"
    return CF_COUNTRIES.get(o.upper(), o[:1].upper() + o[1:].lower() if o.isupper() else o)


def family_of(product):
    """(famille, comparable à la pièce, unité de comptage) d'un produit, ou None s'il n'entre dans aucune famille."""
    n = normalize(product["name"])
    top = normalize((product["cat"] or "").split(" > ")[0])
    # Familles de fruits et légumes : seulement les produits frais (pas les raviolis à l'ail, ni la soupe de carotte)
    if (top in ("fruits", "legumes", "fruits et legumes") or product["aisle"] in FRESH_AISLES) and not NOT_FRESH.search(n):
        for family, pattern, piece in FAMILIES:
            if re.search(pattern, n):
                return family, piece, None
    for family, pattern, per in STAPLES:
        if re.search(pattern, n):
            return family, bool(per), per
    return None


def load_source(src, name, db_path, dates):
    """Produits + historique d'une enseigne. `dates` : index commun à toutes les enseignes."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    prefix = "" if src == "mm" else f"{src}-"
    last_date = conn.execute("SELECT MAX(snapshot_date) FROM price_snapshots").fetchone()[0]
    aisle_names = dict(conn.execute(
        "SELECT s.aisle, p.aisle FROM price_snapshots s JOIN products p USING (product_id) GROUP BY s.aisle"))
    # Rayons dans l'ordre du site (= ordre d'insertion du dernier relevé)
    aisles = [{"slug": prefix + slug, "name": aisle_names.get(slug, slug), "src": src} for (slug,) in conn.execute(
        "SELECT aisle FROM price_snapshots WHERE snapshot_date = ? GROUP BY aisle ORDER BY MIN(rowid)", (last_date,))]

    # Historique compact : [date, prix, €/kg, prix normal, €/kg normal, promo, rang, stock, fin promo]
    history, latest = {}, {}
    for r in conn.execute("SELECT * FROM price_snapshots ORDER BY snapshot_date"):
        history.setdefault(r["product_id"], []).append([
            dates[r["snapshot_date"]], r["price"], r["unit_price"], r["regular_price"], r["regular_unit_price"],
            r["promo_label"], r["rank"], r["available_quantity"] if "available_quantity" in r.keys() else None,
            r["promo_end"],
        ])
        latest[r["product_id"]] = r

    products = []
    for p in conn.execute("SELECT * FROM products"):
        pid = p["product_id"]
        if pid not in history:
            continue
        snap, cols = latest[pid], p.keys()
        if src == "mm":
            country, made = country_of(p["origin"])
            labels = json.loads(p["labels"] or "[]")
            # Vendu à la pièce, par N ou en N têtes : permet la comparaison à la pièce
            unit = p["sale_unit"] or ""
            heads = re.match(r"(\d+) t[eê]tes", unit)
            pieces = int(heads.group(1)) if heads else (p["pack_size"] or 1) if re.match(r"(pi[eè]ce|par \d+)", unit, re.I) \
                else count_of(unit, p["name"])
        else:
            country, made, pieces = cf_country(p["origin"]), False, count_of(p["sale_unit"], p["name"])
            labels = [CF_LABELS[l] for l in json.loads(p["labels"] or "[]") if l in CF_LABELS]
        get = lambda k: p[k] if k in cols else None
        products.append({
            "id": prefix + pid,
            "src": src,
            "name": p["name"],
            "aisle": prefix + snap["aisle"],
            "url": p["url"],
            "img": p["image_url"],
            "origin": p["origin"],
            "country": country,
            "made": made,
            "brand": get("brand"),
            "variety": get("variety"),
            "caliber": get("caliber"),
            "quality": get("quality_grade"),
            "labels": labels,
            "unit": p["sale_unit"],
            "pu": snap["unit_price_unit"],
            "pieces": pieces,
            "short": get("short_description"),
            "desc": get("description"),
            "attrs": json.loads(get("attributes") or "{}") if src == "mm"
            else {k: v for k, v in (("Marque", p["brand"]), ("Format", p["format"])) if v},
            "cat": p["category_path"],
            "rating": snap["rating"],
            "ratingCount": snap["rating_count"],
            "fresh": snap["freshness_days"] if "freshness_days" in snap.keys() else None,
            "h": history[pid],
        })
    conn.close()
    return aisles, products


def build_payload():
    sources = [(src, name, DATA_DIR / db) for src, name, db in SOURCES if (DATA_DIR / db).exists()]
    if not sources or sources[0][0] != "mm":
        raise SystemExit("Aucune donnée : lance d'abord python3 scraper_mon_marche.py")
    all_dates = set()
    for _, _, path in sources:
        with sqlite3.connect(path) as conn:
            all_dates |= {r[0] for r in conn.execute("SELECT DISTINCT snapshot_date FROM price_snapshots")}
    dates = sorted(all_dates)
    date_index = {d: i for i, d in enumerate(dates)}

    aisles, products = [], []
    for src, name, path in sources:
        a, p = load_source(src, name, path, date_index)
        aisles += a
        products += p

    # Familles présentes chez au moins deux enseignes
    families = {}
    for p in products:
        found = family_of(p)
        if not found:
            continue
        bio = any(re.search(r"\bBIO\b", l.upper()) for l in p["labels"]) or bool(re.search(r"\bbio\b", normalize(p["name"])))
        fam = families.setdefault((found[0], bio), {"name": found[0], "bio": bio, "piece": found[1], "per": found[2],
                                                    "ids": [], "srcs": set()})
        fam["ids"].append(p["id"])
        fam["srcs"].add(p["src"])

    return {
        "generatedAt": datetime.now(TZ).isoformat(timespec="minutes"),
        "sources": [{"id": src, "name": name} for src, name, _ in sources],
        "dates": dates,
        "aisles": aisles,
        "products": products,
        "families": [{k: v for k, v in f.items() if k != "srcs"} for f in families.values() if len(f["srcs"]) > 1],
    }


def build():
    payload = build_payload()
    # "</" échappé pour que les données ne puissent jamais fermer la balise <script>
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = TEMPLATE_PATH.read_text(encoding="utf-8").replace("__DATA__", data)
    OUTPUT_PATH.write_text(html, encoding="utf-8")
    print(f"✔ Tableau de bord → {OUTPUT_PATH} ({len(payload['products'])} produits, "
          f"{len(payload['dates'])} relevé{'s' if len(payload['dates']) > 1 else ''}, "
          f"{len(payload['families'])} produits comparables entre enseignes)")
    return OUTPUT_PATH


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Génère le tableau de bord HTML")
    parser.add_argument("--no-open", action="store_true", help="ne pas ouvrir le navigateur")
    args = parser.parse_args()
    path = build()
    if not args.no_open and sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
