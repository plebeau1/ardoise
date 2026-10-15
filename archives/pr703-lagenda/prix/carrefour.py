"""Relevé d'un rayon carrefour.fr — l'équivalent de `scrape_venue()` côté concerts.

    scrape_rayon(rayon, mode='full'|'test', method='auto'|'http'|'browser',
                 visible=False) -> (produits, meta)

Le mur à franchir (sondé le 2026-10-14) : Cloudflare sert une page « C'est une
formalité… si vous êtes un humain ! » avec une case Turnstile à cocher, en 403,
à toute requête venue d'une IP de datacenter — `requests`, `curl_cffi` en
impersonation et Chromium headless compris. Depuis une connexion domestique
(le Mac), la page passe normalement, au pire après avoir coché la case une fois.

D'où la chaîne :
  1. HTTP direct (`curl_cffi`, empreinte TLS de Chrome) : rapide, suffit si
     Cloudflare laisse passer.
  2. Navigateur (Playwright, Chrome installé de préférence) avec un PROFIL
     PERSISTANT dans `data/prix/navigateur/` : le cookie `cf_clearance` obtenu
     une fois est réutilisé aux relevés suivants. Les pages suivantes sont
     demandées par `fetch()` DEPUIS la page (mêmes cookies, même origine).
  3. Si Cloudflare demande la case en headless et qu'on est devant un écran,
     on rouvre le navigateur en fenêtre visible et on attend qu'un humain
     coche. Jamais de résolution automatique de la case.

Chaque réponse brute est gardée dans `data/prix/debug/` (écrasée à chaque
relevé) : si l'extraction rend 0 produit, c'est ce fichier qu'on regarde pour
adapter `extract.py`.
"""

from __future__ import annotations

import os
import random
import sys
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from prix import extract

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data" / "prix"
DEBUG_DIR = DATA_DIR / "debug"
PROFILE_DIR = DATA_DIR / "navigateur"

#: Garde-fou : un rayon ne dépasse pas quelques pages de 30 produits.
MAX_PAGES = 15

#: Attente que Cloudflare lève sa page d'attente : en headless elle se lève
#: seule ou jamais ; en fenêtre visible, le temps qu'un humain coche la case.
CHALLENGE_WAIT_HEADLESS = 25
CHALLENGE_WAIT_VISIBLE = 180

_JSON_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
}

_FETCH_JS = """async ([url, headers]) => {
  const r = await fetch(url, {headers, credentials: 'include'});
  return {status: r.status, contentType: r.headers.get('content-type') || '', body: await r.text()};
}"""

_CHALLENGE_JS = "() => !!window._cf_chl_opt || /un instant|just a moment|juste un instant/i.test(document.title)"


class Blocked(Exception):
    """Cloudflare a servi sa page de vérification au lieu du rayon."""


def is_challenge(status, body: str) -> bool:
    # `_cf_chl_opt` n'existe que sur la page de vérification. Ne PAS tester
    # `/cdn-cgi/challenge-platform/` seul : Cloudflare l'injecte aussi dans
    # les pages normales (script de détection passif).
    return status in (403, 429, 503) and ("_cf_chl_opt" in body or "cf_chl_" in body)


def page_url(url: str, n: int) -> str:
    if n <= 1:
        return url
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "page"] + [("page", str(n))]
    return urlunsplit(parts._replace(query=urlencode(query)))


def _save_debug(slug: str, n: int, content_type: str, body: str):
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    ext = "json" if "json" in content_type.lower() or body.lstrip()[:1] in ("{", "[") else "html"
    (DEBUG_DIR / f"carrefour_{slug}_p{n}.{ext}").write_text(body, encoding="utf-8")


def _proxy_url():
    """Proxy sortant facultatif (`PRIX_PROXY_URL`, sinon `HTTPS_PROXY`). Jamais loggé."""
    return os.environ.get("PRIX_PROXY_URL") or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")


# --------------------------------------------------------------------------
# Parcours des pages, commun aux deux méthodes
# --------------------------------------------------------------------------

def _crawl(fetch, url, slug, max_pages, log, seed=None):
    """fetch(url) -> (status, content_type, body). Rend (produits, nb de pages lues)."""
    products: dict = {}
    for p in seed or []:
        products[p["code"]] = p
    pages = 0
    for n in range(1, max_pages + 1):
        status, content_type, body = fetch(page_url(url, n))
        _save_debug(slug, n, content_type, body)
        if is_challenge(status, body):
            if not products:
                raise Blocked(f"page {n} : vérification Cloudflare (HTTP {status})")
            log(f"   page {n} : vérification Cloudflare, arrêt sur ce qui est déjà lu")
            break
        if status != 200:
            if not products:
                raise RuntimeError(f"page {n} : HTTP {status}")
            log(f"   page {n} : HTTP {status}, arrêt")
            break
        found = extract.extract_products(body, content_type)
        new = [p for p in found if p["code"] not in products]
        products.update({p["code"]: p for p in extract.merge_products([list(products.values()), found])})
        pages = n if found else pages
        log(f"   page {n} : {len(found)} produits lus, {len(new)} nouveaux")
        # page vide, ou page qui ne rapporte rien de neuf (au-delà de la 1re,
        # que la page affichée a pu déjà couvrir) : fin du rayon
        if not found or (not new and n > 1):
            break
        if n < max_pages:
            time.sleep(random.uniform(1.5, 3.0))  # un humain ne tourne pas les pages plus vite
    return list(products.values()), pages


# --------------------------------------------------------------------------
# 1. HTTP direct
# --------------------------------------------------------------------------

def _http_fetcher():
    from curl_cffi import requests as cffi

    verify = os.environ.get("CURL_CA_BUNDLE") or os.environ.get("REQUESTS_CA_BUNDLE") or True
    proxy = os.environ.get("PRIX_PROXY_URL")
    proxies = {"http": proxy, "https": proxy} if proxy else None
    session = cffi.Session(impersonate="chrome124", verify=verify, proxies=proxies)
    session.headers.update({"Accept-Language": "fr-FR,fr;q=0.9"})

    def fetch(url):
        r = session.get(url, headers=_JSON_HEADERS, timeout=30)
        return r.status_code, r.headers.get("content-type", ""), r.text

    return fetch


# --------------------------------------------------------------------------
# 2. Navigateur
# --------------------------------------------------------------------------

def _playwright_proxy():
    raw = _proxy_url()
    if not raw:
        return None
    parts = urlsplit(raw)
    proxy = {"server": f"{parts.scheme}://{parts.hostname}:{parts.port}" if parts.port else f"{parts.scheme}://{parts.hostname}"}
    if parts.username:
        proxy["username"] = parts.username
        proxy["password"] = parts.password or ""
    return proxy


def _launch(playwright, visible):
    options = {
        "headless": not visible,
        "locale": "fr-FR",
        "timezone_id": "Europe/Paris",
        "viewport": {"width": 1366, "height": 900},
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    proxy = _playwright_proxy()
    if proxy:
        options["proxy"] = proxy
    # Chrome installé d'abord (celui du Mac), Chromium de Playwright sinon
    # (`python -m playwright install chromium`), ou un binaire imposé par
    # `PRIX_BROWSER_PATH`. Un profil par moteur : leurs formats diffèrent.
    executable = os.environ.get("PRIX_BROWSER_PATH")
    if executable:
        return playwright.chromium.launch_persistent_context(str(PROFILE_DIR / "chromium"),
                                                             executable_path=executable, **options)
    try:
        return playwright.chromium.launch_persistent_context(str(PROFILE_DIR / "chrome"), channel="chrome", **options)
    except Exception:
        return playwright.chromium.launch_persistent_context(str(PROFILE_DIR / "chromium"), **options)


def _wait_challenge(page, timeout_s) -> bool:
    """True quand la page de vérification est levée, False au bout du délai."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if not page.evaluate(_CHALLENGE_JS):
                return True
        except Exception:  # navigation en cours : contexte JS détruit
            pass
        page.wait_for_timeout(1000)
    return False


def _crawl_browser(rayon, max_pages, visible, log):
    from playwright.sync_api import sync_playwright

    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        context = _launch(p, visible)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(rayon["url"], wait_until="domcontentloaded", timeout=60000)
            wait = CHALLENGE_WAIT_VISIBLE if visible else CHALLENGE_WAIT_HEADLESS
            if not _wait_challenge(page, wait):
                _save_debug(rayon["slug"], 1, "text/html", page.content())
                raise Blocked("vérification Cloudflare non levée")
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            # la page rendue d'abord : c'est ce que voit un visiteur
            rendered = page.content()
            _save_debug(rayon["slug"], 0, "text/html", rendered)
            seed = extract.extract_products(rendered, "text/html")
            log(f"   page affichée : {len(seed)} produits lus")

            def fetch(url):
                res = page.evaluate(_FETCH_JS, [url, _JSON_HEADERS])
                return res["status"], res["contentType"], res["body"]

            products, pages = _crawl(fetch, rayon["url"], rayon["slug"], max_pages, log, seed=seed)
            return products, max(pages, 1 if seed else 0)
        finally:
            context.close()


def _can_show_window() -> bool:
    return sys.stdin.isatty() and (sys.platform == "darwin" or bool(os.environ.get("DISPLAY")))


# --------------------------------------------------------------------------
# Point d'entrée
# --------------------------------------------------------------------------

def scrape_rayon(rayon: dict, mode: str = "full", method: str = "auto", visible: bool = False, log=print):
    """Produits du rayon. `mode='test'` : première page seulement."""
    max_pages = 1 if mode == "test" else MAX_PAGES

    if method in ("auto", "http") and not visible:
        try:
            products, pages = _crawl(_http_fetcher(), rayon["url"], rayon["slug"], max_pages, log)
            if products or method == "http":
                return products, {"method": "http", "pages": pages}
            log("   HTTP direct : page reçue mais aucun produit lisible → navigateur")
        except Blocked as e:
            if method == "http":
                raise
            log(f"   🛡️  HTTP direct bloqué ({e}) → navigateur")
        except Exception as e:  # réseau, curl_cffi absent…
            if method == "http":
                raise
            log(f"   HTTP direct KO ({type(e).__name__}: {e}) → navigateur")

    try:
        products, pages = _crawl_browser(rayon, max_pages, visible, log)
        return products, {"method": "navigateur" + (" visible" if visible else ""), "pages": pages}
    except Blocked:
        if visible or not _can_show_window():
            raise
    log("   🛡️  Cloudflare demande une vérification : une fenêtre Chrome s'ouvre,")
    log("       coche la case « Je suis humain » (3 min max). Le cookie est gardé pour la suite.")
    products, pages = _crawl_browser(rayon, max_pages, True, log)
    return products, {"method": "navigateur visible", "pages": pages}


def load_fixture(path: str):
    """Produits d'un fichier sauvegardé (HTML ou JSON), sans réseau."""
    body = Path(path).read_text(encoding="utf-8")
    content_type = "application/json" if path.endswith(".json") else "text/html"
    return extract.extract_products(body, content_type)
