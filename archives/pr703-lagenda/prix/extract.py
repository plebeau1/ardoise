"""Extraction des produits d'une page rayon — sans réseau, testable sur un fichier.

Le relevé du 2026-10-14 n'a jamais vu la vraie page : Cloudflare sert une case
« Vérifiez que vous êtes humain » aux IP de datacenter. Ce module ne parie donc
pas sur UNE structure, il les lit toutes et fusionne par code produit :

  1. JSON (réponse d'API, `<script type="application/json">`, JSON-LD,
     `window.__X__ = {...}`, payload Nuxt 3 `__NUXT_DATA__`) : on parcourt
     l'arbre et on garde chaque objet qui porte un nom, un code (EAN, id
     numérique) ET un prix quelque part sous lui. Les formes JSON:API
     (`{"type": "product", "attributes": {...}}`) et schema.org (`offers.price`)
     passent par le même chemin.
  2. HTML rendu, en dernier recours : chaque lien `/p/<slug>-<EAN>` et le prix
     écrit dans sa carte produit.

Un produit sans prix n'est jamais rendu : ce qu'on suit, c'est le prix.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from collections import deque
from urllib.parse import urljoin

BASE_URL = "https://www.carrefour.fr"

_CODE_RE = re.compile(r"^\d{8,14}$")
_NAME_KEYS = ("title", "name", "productName", "label")
_EAN_KEYS = ("ean", "gtin13", "gtin", "ean13", "barcode", "gtin14", "gtin8")
_BRAND_KEYS = ("brand", "brandName", "marque")
_FORMAT_KEYS = ("format", "packaging", "quantity", "size", "conditionnement")
_URL_KEYS = ("url", "productUrl", "link", "href")
_ORIGIN_KEYS = ("origin", "countryOfOrigin", "originCountry", "provenance", "origine")
_REGULAR_KEYS = ("priceBeforeDiscount", "strikethroughPrice", "originalPrice", "regularPrice",
                 "oldPrice", "crossedPrice", "initialPrice", "basePrice", "priceWithoutDiscount")
_UNIT_LABEL_KEYS = ("perUnitLabel", "unitPriceLabel", "pricePerUnitLabel")
_UNIT_VALUE_KEYS = ("perUnit", "pricePerUnit", "unitPrice")
_UNIT_NAME_KEYS = ("unitOfMeasure", "unit", "perUnitUnit")
_AVAILABLE_KEYS = ("purchasable", "isPurchasable", "isAvailable", "available", "inStock")
_PROMO_TEXT_KEYS = ("label", "title", "shortLabel", "description", "message", "text", "name")

#: Profondeur max sous un produit pour chercher son prix (offres imbriquées).
_PRICE_DEPTH = 8

_UNITS = {
    "kg": "KG", "kilo": "KG", "kilogramme": "KG",
    "l": "L", "litre": "L", "lt": "L",
    "g": "G", "100g": "100G",
    "piece": "PIECE", "pièce": "PIECE", "pce": "PIECE", "pc": "PIECE", "u": "PIECE",
    "unite": "PIECE", "unité": "PIECE", "botte": "BOTTE",
}


# --------------------------------------------------------------------------
# Prix
# --------------------------------------------------------------------------

def _clean_text(value) -> str:
    return re.sub(r"\s+", " ", html_lib.unescape(str(value))).strip()


def parse_price(value):
    """1.49 / "1,49 €" / "1€49" / {"price": 1.49} -> 1.49 ; None si illisible."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 2) if 0 < value < 10000 else None
    if isinstance(value, dict):
        for key in ("price", "value", "amount"):
            if key in value:
                found = parse_price(value[key])
                if found is not None:
                    return found
        return None
    if not isinstance(value, str):
        return None
    s = value.replace(" ", " ").replace(" ", " ")
    m = re.search(r"(\d{1,4}[.,]\d{1,2})\s*€", s) or re.search(r"(?<![\d.,])(\d{1,4})\s?€\s?(\d{2})(?!\d)", s)
    if m and m.lastindex == 2:
        return float(f"{m.group(1)}.{m.group(2)}")
    if not m:
        m = re.fullmatch(r"\s*(\d{1,4}(?:[.,]\d{1,2})?)\s*(?:€|EUR)?\s*", s)
    if not m:
        return None
    price = round(float(m.group(1).replace(",", ".")), 2)
    return price if price > 0 else None


def normalize_unit(unit):
    if not unit:
        return None
    key = str(unit).strip().lower().rstrip(".")
    return _UNITS.get(key, key.upper())


def parse_unit_label(label):
    """"1,99 € / KG" -> (1.99, "KG") ; (None, None) si illisible."""
    if not isinstance(label, str):
        return None, None
    s = label.replace(" ", " ").replace(" ", " ")
    m = re.search(r"(\d{1,4}(?:[.,]\d{1,2})?)\s*€\s*(?:/|par|le|la|l')\s*([A-Za-zéèû0-9]+)", s, re.I)
    if not m:
        return None, None
    return round(float(m.group(1).replace(",", ".")), 2), normalize_unit(m.group(2))


# --------------------------------------------------------------------------
# Parcours JSON
# --------------------------------------------------------------------------

def _iter_dicts(node, max_depth=None):
    """Tous les dicts de l'arbre, en largeur d'abord (le plus proche d'abord)."""
    queue = deque([(node, 0)])
    while queue:
        cur, depth = queue.popleft()
        if max_depth is not None and depth > max_depth:
            continue
        if isinstance(cur, dict):
            yield cur
            children = cur.values()
        elif isinstance(cur, list):
            children = cur
        else:
            continue
        for child in children:
            if isinstance(child, (dict, list)):
                queue.append((child, depth + 1))


def _flatten(node: dict) -> dict:
    """JSON:API : `{"type", "id", "attributes": {...}}` -> attributs + id."""
    attrs = node.get("attributes")
    if isinstance(attrs, dict):
        merged = dict(attrs)
        merged.setdefault("_node_id", node.get("id"))
        merged.setdefault("_node_type", node.get("type"))
        return merged
    return node


def _first_str(d: dict, keys):
    for key in keys:
        v = d.get(key)
        if isinstance(v, str) and v.strip():
            return _clean_text(v)
        if isinstance(v, dict):
            for sub in ("name", "label", "title", "value"):
                if isinstance(v.get(sub), str) and v[sub].strip():
                    return _clean_text(v[sub])
    return None


def _code_of(d: dict):
    for key in _EAN_KEYS:
        v = d.get(key)
        if isinstance(v, (int, str)) and _CODE_RE.match(str(v).strip()):
            return str(v).strip()
    for key in ("_node_id", "id", "sku", "productId"):
        v = d.get(key)
        if isinstance(v, (int, str)) and _CODE_RE.match(str(v).strip()):
            return str(v).strip()
    return None


def _promo_label(value):
    if isinstance(value, str) and 1 < len(value.strip()) < 120:
        return _clean_text(value)
    if isinstance(value, list):
        for item in value:
            found = _promo_label(item)
            if found:
                return found
    if isinstance(value, dict):
        for key in _PROMO_TEXT_KEYS:
            if isinstance(value.get(key), str) and value[key].strip():
                return _clean_text(value[key])
    return None


def _unit_from_block(block: dict, info: dict):
    if info.get("unit_price") is not None:
        return
    label = _first_str(block, _UNIT_LABEL_KEYS)
    value, unit = parse_unit_label(label) if label else (None, None)
    if value is None:
        for key in _UNIT_VALUE_KEYS:
            v = block.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                value = round(float(v), 2)
                break
            if isinstance(v, str):
                value, unit = parse_unit_label(v)
                if value is None:
                    value = parse_price(v)
                if value is not None:
                    break
    if unit is None:
        unit = normalize_unit(_first_str(block, _UNIT_NAME_KEYS))
    if value is not None:
        info["unit_price"] = value
        info["unit"] = unit
        info["unit_price_label"] = label


def _price_info(node: dict) -> dict:
    """Prix, prix au kilo, prix barré, promo, dispo : le plus proche gagne."""
    info: dict = {}
    for d in _iter_dicts(node, max_depth=_PRICE_DEPTH):
        if "price" not in info and "price" in d:
            p = d["price"]
            if isinstance(p, dict) and parse_price(p.get("price", p.get("value"))) is not None:
                info["price"] = parse_price(p.get("price", p.get("value")))
                _unit_from_block(p, info)
                _unit_from_block(d, info)
            elif not isinstance(p, (dict, list)) and parse_price(p) is not None:
                info["price"] = parse_price(p)
                _unit_from_block(d, info)
        if "price" not in info and "lowPrice" in d and parse_price(d["lowPrice"]) is not None:
            info["price"] = parse_price(d["lowPrice"])  # schema.org AggregateOffer
        if any(k in d for k in _UNIT_LABEL_KEYS):
            _unit_from_block(d, info)
        if "regular_price" not in info:
            for key in _REGULAR_KEYS:
                if key in d and parse_price(d[key]) is not None:
                    info["regular_price"] = parse_price(d[key])
                    break
        if "promo_label" not in info:
            for key, v in d.items():
                low = key.lower()
                if ("promo" in low or "discount" in low) and v:
                    label = _promo_label(v)
                    if label:
                        info["promo_label"] = label
                        break
        if "available" not in info:
            for key in _AVAILABLE_KEYS:
                if isinstance(d.get(key), bool):
                    info["available"] = d[key]
                    break
            else:
                avail = d.get("availability")
                if isinstance(avail, str) and ("InStock" in avail or "OutOfStock" in avail):
                    info["available"] = "InStock" in avail
    if info.get("regular_price") is not None and info.get("price") is not None \
            and info["regular_price"] <= info["price"]:
        info.pop("regular_price")
    return info


def _image_of(node: dict):
    for d in _iter_dicts(node, max_depth=4):
        for key, v in d.items():
            low = key.lower()
            if not any(t in low for t in ("image", "media", "picture", "visual", "thumbnail")):
                continue
            stack = [v]
            while stack:
                cur = stack.pop()
                if isinstance(cur, str) and re.match(r"^(https?:)?//|^/", cur) \
                        and re.search(r"\.(jpe?g|png|webp|avif)|media", cur, re.I):
                    return urljoin(BASE_URL, cur)
                if isinstance(cur, dict):
                    stack.extend(cur.values())
                elif isinstance(cur, list):
                    stack.extend(reversed(cur))
    return None


def _product_from_node(node: dict):
    d = _flatten(node)
    name = _first_str(d, _NAME_KEYS)
    if not name or not (2 <= len(name) <= 200):
        return None
    code = _code_of(d)
    if not code:
        return None
    info = _price_info(d)
    if info.get("price") is None:
        return None
    url = _first_str(d, _URL_KEYS)
    if not url and isinstance(d.get("slug"), str):
        slug = d["slug"].strip("/")
        url = f"/p/{slug}" if slug.endswith(code) else f"/p/{slug}-{code}"
    return {
        "code": code,
        "ean": code if len(code) in (8, 12, 13, 14) else None,
        "name": name,
        "brand": _first_str(d, _BRAND_KEYS),
        "format": _first_str(d, _FORMAT_KEYS),
        "origin": _first_str(d, _ORIGIN_KEYS),
        "price": info["price"],
        "unit_price": info.get("unit_price"),
        "unit": info.get("unit"),
        "unit_price_label": info.get("unit_price_label"),
        "regular_price": info.get("regular_price"),
        "promo_label": info.get("promo_label"),
        "available": info.get("available"),
        "url": urljoin(BASE_URL, url) if url else None,
        "image": _image_of(d),
    }


def products_from_json(data) -> list[dict]:
    found = []
    for node in _iter_dicts(data):
        product = _product_from_node(node)
        if product:
            found.append(product)
    return merge_products([found])


# --------------------------------------------------------------------------
# JSON embarqué dans le HTML
# --------------------------------------------------------------------------

_SCRIPT_RE = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.S | re.I)
_WINDOW_ASSIGN_RE = re.compile(r"window(?:\.|\[[\"'])(__[A-Za-z0-9_]+__)(?:[\"']\])?\s*=\s*")


def _devalue_unflatten(flat: list):
    """Payload Nuxt 3 (`devalue`) : tableau plat où chaque valeur est un index."""
    cache: dict = {}

    def hydrate(index):
        if not isinstance(index, int) or isinstance(index, bool):
            return index
        if index < 0:
            return None
        if index in cache:
            return cache[index]
        if index >= len(flat):
            return None
        value = flat[index]
        if isinstance(value, list):
            if value and isinstance(value[0], str):
                tag = value[0]
                if tag in ("Date", "RegExp", "BigInt"):
                    cache[index] = value[1] if len(value) > 1 else None
                elif tag in ("Set",):
                    cache[index] = out = []
                    out.extend(hydrate(i) for i in value[1:])
                elif tag in ("Map", "null", "Object"):
                    cache[index] = out = {}
                    pairs = value[1:]
                    for i in range(0, len(pairs) - 1, 2):
                        out[str(hydrate(pairs[i]))] = hydrate(pairs[i + 1])
                else:  # Reactive, ShallowReactive, Ref, EmptyRef, NuxtError…
                    cache[index] = None
                    cache[index] = hydrate(value[1]) if len(value) > 1 else None
                return cache[index]
            cache[index] = out = []
            out.extend(hydrate(i) for i in value)
            return out
        if isinstance(value, dict):
            cache[index] = out = {}
            for key, i in value.items():
                out[key] = hydrate(i)
            return out
        cache[index] = value
        return value

    return hydrate(0)


def json_documents_from_html(html: str) -> list:
    """Chaque document JSON lisible embarqué dans la page."""
    docs = []
    decoder = json.JSONDecoder()
    for attrs, body in _SCRIPT_RE.findall(html):
        body = body.strip()
        if not body:
            continue
        attrs_low = attrs.lower()
        if "json" in attrs_low:
            try:
                data = json.loads(html_lib.unescape(body) if body.startswith("&") else body)
            except ValueError:
                continue
            if "__nuxt_data__" in attrs_low or "data-nuxt-data" in attrs_low:
                try:
                    data = _devalue_unflatten(data)
                except (RecursionError, TypeError, IndexError):
                    continue
            docs.append(data)
            continue
        for m in _WINDOW_ASSIGN_RE.finditer(body):
            try:
                data, _ = decoder.raw_decode(body, m.end())
            except ValueError:
                continue
            docs.append(data)
    return docs


# --------------------------------------------------------------------------
# HTML rendu (dernier recours)
# --------------------------------------------------------------------------

_PRODUCT_HREF_RE = re.compile(r"/p/[^?#\"']*?-(\d{8,14})(?:[/?#]|$)")
_UNIT_IN_TEXT_RE = re.compile(r"\d{1,4}(?:[.,]\d{1,2})?\s*€\s*/\s*[A-Za-zéèû0-9]+", re.I)
_PROMO_IN_TEXT_RE = re.compile(r"(-\s?\d{1,2}\s?%(?:\s+sur\s+le\s+\d+\s?(?:e|ème|eme))?|"
                               r"\d+\s?(?:e|ème|eme)\s?(?:à|a)\s?-?\s?\d{1,2}\s?%|"
                               r"\d+\s?(?:\+|achetés?,?)\s?\d+\s?offerts?)", re.I)


def products_from_dom(html: str) -> list[dict]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    found: dict = {}
    for a in soup.find_all("a", href=True):
        m = _PRODUCT_HREF_RE.search(a["href"])
        if not m or m.group(1) in found:
            continue
        code = m.group(1)
        card, node = None, a
        for _ in range(8):  # remonte jusqu'à la carte produit
            node = node.parent
            if node is None or node.name in ("body", "html"):
                break
            if node.name in ("article", "li"):
                card = node
                break
            if card is None and "€" in node.get_text(" ", strip=True):
                card = node
        if card is None:
            continue
        text = card.get_text(" ", strip=True).replace(" ", " ").replace(" ", " ")
        unit_match = _UNIT_IN_TEXT_RE.search(text)
        unit_price, unit = parse_unit_label(unit_match.group(0)) if unit_match else (None, None)
        prices = [parse_price(p) for p in re.findall(r"\d{1,4}[.,]\d{2}\s*€|\d{1,4}\s?€\s?\d{2}(?!\d)",
                                                      _UNIT_IN_TEXT_RE.sub(" ", text))]
        prices = [p for p in prices if p is not None]
        if not prices:
            continue
        heading = card.find(["h2", "h3", "h4"])
        img = card.find("img")
        name = (a.get("title") or a.get("aria-label") or (heading.get_text(" ", strip=True) if heading else "")
                or a.get_text(" ", strip=True) or (img.get("alt") if img else "") or "")
        name = _clean_text(name)
        if not name:
            continue
        promo = _PROMO_IN_TEXT_RE.search(text)
        image = (img.get("src") or img.get("data-src")) if img else None
        found[code] = {
            "code": code,
            "ean": code if len(code) in (8, 12, 13, 14) else None,
            "name": name,
            "brand": None,
            "format": None,
            "origin": None,
            "price": prices[0],
            # deux prix dans la carte = prix promo + prix barré, le plus haut est le barré
            "regular_price": max(prices[1:]) if len(prices) > 1 and max(prices[1:]) > prices[0] else None,
            "unit_price": unit_price,
            "unit": unit,
            "unit_price_label": unit_match.group(0) if unit_match else None,
            "promo_label": _clean_text(promo.group(0)) if promo else None,
            "available": None,
            "url": urljoin(BASE_URL, a["href"]),
            "image": urljoin(BASE_URL, image) if image and not image.startswith("data:") else None,
        }
    return list(found.values())


# --------------------------------------------------------------------------
# Point d'entrée
# --------------------------------------------------------------------------

def merge_products(lists) -> list[dict]:
    """Fusion par code, la première source non vide gagne champ par champ."""
    merged: dict = {}
    for products in lists:
        for p in products:
            cur = merged.get(p["code"])
            if cur is None:
                merged[p["code"]] = dict(p)
                continue
            for key, value in p.items():
                if cur.get(key) is None and value is not None:
                    cur[key] = value
    return list(merged.values())


def extract_products(body: str, content_type: str = "") -> list[dict]:
    """Produits d'une réponse (JSON ou HTML), quelle que soit sa forme."""
    text = body.lstrip()
    if "json" in (content_type or "").lower() or text[:1] in ("{", "["):
        try:
            return products_from_json(json.loads(text))
        except ValueError:
            pass
    from_json = [products_from_json(doc) for doc in json_documents_from_html(body)]
    from_json = merge_products(from_json)
    if from_json:
        # le DOM complète (prix barré, promo affichée) sans jamais remplacer
        return merge_products([from_json, products_from_dom(body)])
    return products_from_dom(body)
