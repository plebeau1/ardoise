"""Cloud Functions de l'Ardoise (Firebase, projet ardoise-mon-marche, région europe-west1).

- releve_quotidien : chaque jour à 7 h (Paris), relève les prix, met à jour la base et régénère la page.
- releve_carrefour : chaque jour à 7 h 15 (Paris), relève les rayons Carrefour suivis (base à part).
  Les deux relevés régénèrent la même page, qui affiche et compare les deux enseignes.
- page             : sert la page ; Firebase Hosting redirige tout le site vers cette fonction.

Tout l'état vit dans le bucket Cloud Storage BUCKET :
    mon_marche.db                        base SQLite (source de vérité)
    carrefour.db                         base SQLite Carrefour (source de vérité)
    dashboard.html                       page générée après chaque relevé
    raw/mon_marche_YYYY-MM-DD.json.gz    réponses brutes du site, une archive par jour
    raw/carrefour_YYYY-MM-DD.json.gz     idem pour Carrefour
"""
import os

# Dans le cloud, seul /tmp est inscriptible : à fixer AVANT d'importer le scraper et le générateur
os.environ.setdefault("ARDOISE_DATA_DIR", "/tmp/ardoise")

from pathlib import Path

from firebase_functions import https_fn, options, scheduler_fn
from google.cloud import storage

import dashboard
import scraper_carrefour
import scraper_mon_marche

REGION = "europe-west1"
BUCKET = os.environ.get("ARDOISE_BUCKET", "ardoise-mon-marche-data")
DATA_DIR = Path(os.environ["ARDOISE_DATA_DIR"])

_client = None


def bucket():
    global _client
    if _client is None:
        _client = storage.Client()
    return _client.bucket(BUCKET)


def download(name, path):
    """Copie locale d'un fichier du bucket, s'il existe. Chaque relevé lit aussi la base de l'AUTRE enseigne
    pour régénérer la page commune : celle-là n'est jamais renvoyée dans le bucket."""
    blob = bucket().blob(name)
    if blob.exists():
        blob.download_to_filename(path)
    return blob


def publish_page():
    page_blob = bucket().blob("dashboard.html")
    page_blob.cache_control = "no-cache"
    page_blob.upload_from_filename(dashboard.OUTPUT_PATH, content_type="text/html; charset=utf-8")


@scheduler_fn.on_schedule(
    schedule="0 7 * * *",
    timezone=scheduler_fn.Timezone("Europe/Paris"),
    region=REGION,
    timeout_sec=540,
    memory=options.MemoryOption.MB_512,
    max_instances=1,
    retry_count=2,
)
def releve_quotidien(event: scheduler_fn.ScheduledEvent) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = download("mon_marche.db", scraper_mon_marche.DB_PATH)
    download("carrefour.db", scraper_carrefour.DB_PATH)   # lecture seule, pour la page

    today = scraper_mon_marche.run()   # relève, écrit la base et régénère la page dans DATA_DIR

    # On ne publie qu'après un relevé complet : en cas d'erreur, la base du bucket reste intacte
    db.upload_from_filename(scraper_mon_marche.DB_PATH)
    raw = scraper_mon_marche.RAW_DIR / f"mon_marche_{today}.json.gz"
    if raw.exists():
        bucket().blob(f"raw/{raw.name}").upload_from_filename(raw)
    publish_page()
    print(f"✔ Relevé du {today} publié")


@scheduler_fn.on_schedule(
    schedule="15 7 * * *",
    timezone=scheduler_fn.Timezone("Europe/Paris"),
    region=REGION,
    timeout_sec=300,
    memory=options.MemoryOption.MB_512,
    max_instances=1,
    retry_count=2,
)
def releve_carrefour(event: scheduler_fn.ScheduledEvent) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = download("carrefour.db", scraper_carrefour.DB_PATH)
    download("mon_marche.db", scraper_mon_marche.DB_PATH)   # lecture seule, pour la page

    today = scraper_carrefour.run()    # relève, écrit la base et régénère la page dans DATA_DIR

    # Même règle que Mon Marché : rien n'est renvoyé si le relevé a planté
    db.upload_from_filename(scraper_carrefour.DB_PATH)
    raw = scraper_carrefour.RAW_DIR / f"carrefour_{today}.json.gz"
    if raw.exists():
        bucket().blob(f"raw/{raw.name}").upload_from_filename(raw)
    publish_page()
    print(f"✔ Relevé Carrefour du {today} publié")


_page_cache = {"etag": None, "html": None}


@https_fn.on_request(region=REGION, memory=options.MemoryOption.MB_256, max_instances=3)
def page(req: https_fn.Request) -> https_fn.Response:
    blob = bucket().get_blob("dashboard.html")
    if blob is None:
        return https_fn.Response("Premier relevé en cours, revenez dans quelques minutes.", status=503,
                                 content_type="text/plain; charset=utf-8")
    if blob.etag != _page_cache["etag"]:   # la page n'est retéléchargée que si un relevé l'a changée
        _page_cache.update(etag=blob.etag, html=blob.download_as_bytes())
    return https_fn.Response(_page_cache["html"], status=200, headers={
        "Content-Type": "text/html; charset=utf-8",
        "Cache-Control": "public, max-age=300, s-maxage=600",   # CDN Firebase : la fonction est rarement appelée
        "X-Robots-Tag": "noindex, nofollow",
    })
